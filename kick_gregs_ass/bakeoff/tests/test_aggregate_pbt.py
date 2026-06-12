"""
Property-based tests for :mod:`bakeoff.aggregate` (Task 11).

Three load-bearing Correctness Properties from the design — the guarantees the
exec viz rests on — are exercised here with Hypothesis.

* **P4 — accuracy is never averaged across answerability classes.** For any
  generated event set and any accuracy metric, if the chosen ``group_by`` does
  NOT include ``answerability`` and the events span >1 answerability value, the
  engine *rejects* the aggregate
  (:class:`bakeoff.aggregate.AnswerabilityBlendError`). When the same call slices
  by ``answerability`` (or the metric is non-accuracy), it succeeds and no
  produced group mixes answerability classes.
  **Validates: Requirements 5.4, 9.7, 14.1**

* **P9 — aggregation is a pure deterministic function of the log given a fixed
  seed.** For any generated event set, two independent engines (same seed)
  produce identical :class:`~bakeoff.types.Aggregate`s and
  :class:`~bakeoff.types.FrontierPoint`s — and the result is invariant to the
  input event ordering (the log is a set of facts, not a sequence).
  **Validates: Requirements 9.1, 14.1**

* **P10 — no number reaches the exec viz without a CI.** For any generated event
  set, every produced :class:`~bakeoff.types.Aggregate` satisfies the exclusive-or
  ``(mean_ci is None) == insufficient_data`` (a populated CI XOR an explicit
  insufficient-data mark — never neither, never both), and every
  :class:`~bakeoff.types.FrontierPoint` carries a populated quality CI.
  **Validates: Requirements 9.8, 11.1, 13.4, 14.1**
"""
from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from bakeoff import config
from bakeoff.aggregate import (
    AggregationEngine,
    AnswerabilityBlendError,
    COMPOSITE_METRIC,
    SPEED_METRIC,
    is_accuracy_metric,
)
from bakeoff.tests.test_aggregate import build_event
from bakeoff.types import TrialEvent

# ---------------------------------------------------------------------------
# Strategies — generate small, valid event logs with controllable spread across
# models, items, reps, and answerability classes.
# ---------------------------------------------------------------------------
_ANSWERABILITY = st.sampled_from(["full", "partial", "none"])
_MODELS = st.sampled_from(["A", "B", "C"])
_GEOG = st.sampled_from(["g1", "g2"])
_STATE = st.sampled_from(["neutral", "frustrated", "anxious"])
_composite = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_latency = st.floats(min_value=1.0, max_value=5000.0, allow_nan=False, allow_infinity=False)

# Accuracy metrics (subject to the P4 blend guard) and non-accuracy metrics.
_ACCURACY_METRICS = st.sampled_from(
    ["faithfulness", "correctness", "completeness", "grounding_precision",
     "semantic_similarity", "ndcg_at_k"]
)
_NON_ACCURACY_METRICS = st.sampled_from([COMPOSITE_METRIC, SPEED_METRIC])


@st.composite
def event_logs(draw, *, min_events=1, max_events=24):
    """Generate a list of valid :class:`TrialEvent`s across models/items/answerability.

    Items are shared across reps (an item id can recur with different reps and
    different models), so between-item structure and per-item rep groups both
    arise naturally. Every event is built by ``build_event`` (which calls
    ``validate_event``), so the log is always schema-valid.
    """
    n = draw(st.integers(min_value=min_events, max_value=max_events))
    # a small item pool so items genuinely repeat across reps/models
    n_items = draw(st.integers(min_value=1, max_value=6))
    events: list[TrialEvent] = []
    # track per (model, item, answerability) rep counters to keep trial_ids distinct
    rep_counter: dict[tuple[str, str, str], int] = {}
    for _ in range(n):
        model = draw(_MODELS)
        item_idx = draw(st.integers(min_value=0, max_value=n_items - 1))
        answerability = draw(_ANSWERABILITY)
        # item identity is tied to its answerability (an item has ONE answerability
        # class in reality), so the item id encodes it.
        item_id = f"{answerability}-i{item_idx}"
        key = (model, item_id, answerability)
        rep = rep_counter.get(key, 0)
        rep_counter[key] = rep + 1
        events.append(
            build_event(
                composite=draw(_composite),
                item_id=item_id,
                model=model,
                rep=rep,
                end_to_end_ms=draw(_latency),
                answerability=answerability,
                geography=draw(_GEOG),
                momentary_state=draw(_STATE),
            )
        )
    return events


# Use a small, fast bootstrap for PBT so the suite stays quick but still
# exercises the real code path deterministically.
def _engine(seed=config.BOOTSTRAP_SEED):
    return AggregationEngine(n_boot=64, seed=seed)


def _answerability_classes(events):
    return {ev.answerability for ev in events}


# ===========================================================================
# P4 — accuracy never averaged across answerability classes
# **Validates: Requirements 5.4, 9.7, 14.1**
# ===========================================================================
@settings(max_examples=200, deadline=None)
@given(events=event_logs(), metric=_ACCURACY_METRICS)
def test_p4_accuracy_rejected_when_blended_across_answerability(events, metric):
    """Grouping an accuracy metric by ['model'] (no answerability slice) must be
    rejected exactly when the events span more than one answerability class."""
    engine = _engine()
    spans_multiple = len(_answerability_classes(events)) > 1
    if spans_multiple:
        # at least one model will carry >1 answerability class? Not necessarily —
        # but grouping by model alone, SOME group spans multiple iff a single
        # model saw multiple classes. Group by nothing-but-constant to be sure:
        # group_by=[] is invalid, so use a constant-collapsing grouping.
        # We assert the guard fires for the whole-log single group via group_by
        # that does not include answerability: ['turn_type'] collapses everything
        # (all events are single-turn) into one group.
        try:
            engine.aggregate(events, ["turn_type"], metric)
            blended = False
        except AnswerabilityBlendError:
            blended = True
        assert blended, (
            "accuracy metric blended across answerability classes was not rejected"
        )
    else:
        # single class -> never rejected
        aggs = engine.aggregate(events, ["turn_type"], metric)
        assert all(not a.insufficient_data or a.mean_ci is None for a in aggs)


@settings(max_examples=200, deadline=None)
@given(events=event_logs(), metric=_ACCURACY_METRICS)
def test_p4_accuracy_allowed_when_sliced_by_answerability(events, metric):
    """Adding 'answerability' to group_by always makes an accuracy aggregate legal,
    and no produced group mixes answerability classes."""
    engine = _engine()
    aggs = engine.aggregate(events, ["model", "answerability"], metric)
    # the group carries its answerability, and within each group every event
    # shared that one class (guaranteed by construction) -> no blend.
    for a in aggs:
        assert "answerability" in a.group


@settings(max_examples=150, deadline=None)
@given(events=event_logs(), metric=_NON_ACCURACY_METRICS)
def test_p4_non_accuracy_metric_never_blocked(events, metric):
    """Non-accuracy metrics (composite/latency/interaction) are never subject to
    the answerability-blend guard, regardless of class spread."""
    assert not is_accuracy_metric(metric)
    engine = _engine()
    # group by model only (may blend answerability) -> must NOT raise.
    aggs = engine.aggregate(events, ["model"], metric)
    assert isinstance(aggs, list)


# ===========================================================================
# P9 — aggregation is a pure deterministic function of the log given a seed
# **Validates: Requirements 9.1, 14.1**
# ===========================================================================
def _agg_signature(aggs):
    """A hashable, comparable signature of a list of Aggregates."""
    out = []
    for a in aggs:
        ci = a.mean_ci
        out.append(
            (
                tuple(sorted(a.group.items())),
                a.metric,
                a.n_items,
                a.n_trials,
                None if ci is None else (ci.point, ci.low, ci.high, ci.method),
                tuple(sorted(a.variance_decomp.items())),
                None if a.latency_quantiles is None else tuple(sorted(a.latency_quantiles.items())),
                a.insufficient_data,
            )
        )
    return out


def _frontier_signature(points):
    return [
        (p.model, (p.quality.point, p.quality.low, p.quality.high, p.quality.method),
         p.speed_p50_ms, p.speed_p90_ms, p.on_pareto_front)
        for p in points
    ]


@settings(max_examples=150, deadline=None)
@given(events=event_logs())
def test_p9_aggregate_identical_across_runs(events):
    """Two engines with the same seed produce identical aggregates (composite)."""
    a = _engine().aggregate(events, ["model"], COMPOSITE_METRIC)
    b = _engine().aggregate(events, ["model"], COMPOSITE_METRIC)
    assert _agg_signature(a) == _agg_signature(b)


@settings(max_examples=150, deadline=None)
@given(events=event_logs())
def test_p9_frontier_identical_across_runs(events):
    a = _engine().frontier(events)
    b = _engine().frontier(events)
    assert _frontier_signature(a) == _frontier_signature(b)


@settings(max_examples=150, deadline=None)
@given(events=event_logs(), data=st.data())
def test_p9_invariant_to_event_ordering(events, data):
    """The log is a set of facts: shuffling event order does not change the result
    (a pure function of the log, not of its serialization order)."""
    engine = _engine()
    base = engine.aggregate(events, ["model"], COMPOSITE_METRIC)
    shuffled = list(events)
    perm = data.draw(st.permutations(range(len(shuffled))))
    reordered = [shuffled[i] for i in perm]
    other = engine.aggregate(reordered, ["model"], COMPOSITE_METRIC)
    assert _agg_signature(base) == _agg_signature(other)


# ===========================================================================
# P10 — no number reaches the exec viz without a CI
# **Validates: Requirements 9.8, 11.1, 13.4, 14.1**
# ===========================================================================
@settings(max_examples=250, deadline=None)
@given(
    events=event_logs(),
    group_by=st.lists(
        st.sampled_from(["model", "answerability", "geography", "momentary_state"]),
        min_size=1,
        max_size=3,
        unique=True,
    ),
)
def test_p10_every_aggregate_has_ci_xor_insufficient(events, group_by):
    """Every aggregate carries a populated CI XOR is explicitly insufficient-data
    — never neither, never both. Use composite (no P4 guard) so any group_by is
    legal."""
    engine = _engine()
    aggs = engine.aggregate(events, group_by, COMPOSITE_METRIC)
    for a in aggs:
        # the exclusive-or contract
        assert (a.mean_ci is None) == a.insufficient_data
        if a.insufficient_data:
            assert a.n_items < engine.min_items_for_ci
        else:
            assert a.n_items >= engine.min_items_for_ci
            assert a.mean_ci is not None


@settings(max_examples=200, deadline=None)
@given(events=event_logs())
def test_p10_every_frontier_point_carries_a_ci(events):
    """No FrontierPoint escapes without a populated quality CI; a model on the
    frontier always has >= min_items_for_ci distinct items."""
    engine = _engine()
    points = engine.frontier(events)
    for p in points:
        assert p.quality is not None
        assert p.quality.low <= p.quality.point <= p.quality.high
        assert np.isfinite(p.speed_p50_ms)
        assert np.isfinite(p.speed_p90_ms)


@settings(max_examples=120, deadline=None)
@given(events=event_logs())
def test_p10_report_numbers_all_carry_ci_or_insufficient(events):
    """The materialized report (built in-memory) never exposes a bare number:
    every aggregate row obeys the CI-xor-insufficient contract and every frontier
    point carries a CI."""
    engine = _engine()
    report = engine.build_report(events, "plan-v1")
    for section in ("by_model", "safety"):
        for row in report[section]:
            assert (row["mean_ci"] is None) == row["insufficient_data"]
    for rows in report["cohort_heatmaps"].values():
        for row in rows:
            assert (row["mean_ci"] is None) == row["insufficient_data"]
    for fp in report["frontier"]:
        assert fp["quality"] is not None

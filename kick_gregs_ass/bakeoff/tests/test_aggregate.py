"""
Unit tests for :mod:`bakeoff.aggregate` (Task 11) — the aggregation engine.

Example / edge-case coverage (the universal P4/P9/P10 properties live in
``test_aggregate_pbt.py``):

* **latency quantiles** — a latency metric aggregate carries ``{p50,p90,p95}``
  matching ``numpy.percentile``; a non-latency metric carries ``None``.
* **frontier Pareto correctness** — a hand-built speed/quality scenario flags the
  non-dominated models ``on_pareto_front`` and de-emphasizes the dominated ones,
  including the "at least as good on both + strictly better on one" tie rule; a
  model too thin for a quality CI is omitted from the frontier (Property 10).
* **thin-cell marking** — a cell below the distinct-item floor is marked
  ``insufficient_data`` with ``mean_ci is None``; a fat cell carries a populated
  CI (Req 9.8 / Property 10).
* **answerability-blend rejection** — aggregating an accuracy metric over a group
  spanning >1 answerability class raises :class:`AnswerabilityBlendError`;
  slicing by ``answerability`` (or using a non-accuracy metric) does not (Req 5.4
  / Property 4).
* **report materialization** — ``aggregate_<plan_version>.json`` is written, the
  safety panel is answerability-sliced, and no number escapes without a CI.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from bakeoff import config
from bakeoff.aggregate import (
    AggregationEngine,
    AnswerabilityBlendError,
    COMPOSITE_METRIC,
    SPEED_METRIC,
)
from bakeoff.eventlog import validate_event
from bakeoff.ids import SCHEMA_VERSION, trial_id
from bakeoff.types import (
    AccuracyScores,
    CohortKey,
    JudgeScores,
    QualityScores,
    RetrievalRecord,
    StageTimings,
    TrialEvent,
)


# ---------------------------------------------------------------------------
# Builder — full control over composite, latency, model, cohort (so latency and
# frontier scenarios can be constructed exactly). Always satisfies
# validate_event's answerability/abstention coupling + timing identity.
# ---------------------------------------------------------------------------
def build_event(
    *,
    composite: float,
    item_id: str,
    model: str = "m",
    rep: int = 0,
    end_to_end_ms: float = 30.0,
    answerability: str = "full",
    pass_name: str = "wide",
    turn_type: str = "single",
    geography: str = "g",
    momentary_state: str = "neutral",
    judge_model: str = config.JUDGE_MODEL_ID,
    judge_dim_sd: dict[str, float] | None = None,
    plan_version: str = "plan-v1",
) -> TrialEvent:
    if answerability in ("none", "partial"):
        abstention_correct: int | None = 1
        unwarranted_refusal: int | None = None
    else:  # full
        abstention_correct = None
        unwarranted_refusal = 0

    accuracy = AccuracyScores(
        precision_at_k=0.5,
        recall_at_k=0.5,
        mrr=0.5,
        ndcg_at_k=0.5,
        grounding_precision=composite,
        grounding_recall=0.5,
        semantic_similarity=composite,
        abstention_correct=abstention_correct,
        unwarranted_refusal=unwarranted_refusal,
    )
    judge = JudgeScores(
        faithfulness=composite,
        correctness=composite,
        completeness=composite,
        judge_sample_count=3,
        judge_model=judge_model,
        judge_dim_sd=dict(judge_dim_sd or {}),
    )
    quality = QualityScores(
        accuracy=accuracy,
        judge=judge,
        composite=composite,
        composite_weights_version=config.COMPOSITE_WEIGHTS_VERSION,
    )
    # timing identity: end_to_end_ms == retrieval_total_ms + generation_total_ms
    retrieval_total = 10.0
    generation_total = end_to_end_ms - retrieval_total
    ev = TrialEvent(
        trial_id=trial_id(model, item_id, rep, pass_name, plan_version),
        schema_version=SCHEMA_VERSION,
        plan_version=plan_version,
        model=model,
        item_id=item_id,
        turn_type=turn_type,
        pass_name=pass_name,
        rep=rep,
        temperature=0.2,
        cohort=CohortKey(
            geography=geography,
            proficiency="fluent",
            tone="terse",
            entry_route="slack",
            momentary_state=momentary_state,
            answerability=answerability,
            turn_type=turn_type,
        ),
        query="q",
        gold_node_ids=["n1"],
        answerability=answerability,
        retrieval=RetrievalRecord(fragment_ids=["n1"], confidence=[0.9], cache_hit=False),
        answer_text="a",
        token_usage={"total": 1},
        timings=StageTimings(
            embed_query_ms=1.0,
            bm25_vectorize_ms=1.0,
            hybrid_search_ms=1.0,
            rerank_ms=1.0,
            retrieval_total_ms=retrieval_total,
            ttft_ms=2.0,
            generation_total_ms=generation_total,
            end_to_end_ms=end_to_end_ms,
        ),
        quality=quality,
        started_at="2025-01-01T00:00:00Z",
        completed_at="2025-01-01T00:00:00.030Z",
        error=None,
    )
    validate_event(ev)  # builder must always produce a valid event
    return ev


def model_events(
    model: str, composite: float, end_to_end_ms: float, n_items: int, reps: int = 2
) -> list[TrialEvent]:
    """``n_items`` distinct items for ``model`` at a fixed composite + latency."""
    out: list[TrialEvent] = []
    for j in range(n_items):
        for r in range(reps):
            out.append(
                build_event(
                    composite=composite,
                    item_id=f"{model}-i{j}",
                    model=model,
                    rep=r,
                    end_to_end_ms=end_to_end_ms,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Latency quantiles
# ---------------------------------------------------------------------------
def test_latency_metric_reports_p50_p90_p95():
    latencies = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    events = [
        build_event(composite=0.5, item_id=f"i{j}", rep=0, end_to_end_ms=lat)
        for j, lat in enumerate(latencies)
    ]
    engine = AggregationEngine()
    [agg] = engine.aggregate(events, ["model"], SPEED_METRIC)
    arr = np.asarray(latencies)
    assert agg.latency_quantiles is not None
    assert agg.latency_quantiles["p50"] == pytest.approx(float(np.percentile(arr, 50)))
    assert agg.latency_quantiles["p90"] == pytest.approx(float(np.percentile(arr, 90)))
    assert agg.latency_quantiles["p95"] == pytest.approx(float(np.percentile(arr, 95)))


def test_non_latency_metric_has_no_latency_quantiles():
    events = model_events("m", composite=0.7, end_to_end_ms=30.0, n_items=4)
    engine = AggregationEngine()
    [agg] = engine.aggregate(events, ["model"], COMPOSITE_METRIC)
    assert agg.latency_quantiles is None
    assert agg.mean_ci is not None
    assert agg.mean_ci.point == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Frontier Pareto correctness
# ---------------------------------------------------------------------------
def test_frontier_pareto_flags_dominated_and_nondominated():
    # A: fast (100ms) + high quality (0.90)        -> on front
    # B: slow (200ms) + higher quality (0.95)      -> on front (q beats A, slower)
    # C: slowest (300ms) + low quality (0.50)      -> dominated by A and B
    # D: as fast as A (100ms) + lower quality (0.80) -> dominated by A (tie speed,
    #    strictly worse quality) — exercises the "at least as good + strictly
    #    better on one" rule.
    events = (
        model_events("A", composite=0.90, end_to_end_ms=100.0, n_items=3)
        + model_events("B", composite=0.95, end_to_end_ms=200.0, n_items=3)
        + model_events("C", composite=0.50, end_to_end_ms=300.0, n_items=3)
        + model_events("D", composite=0.80, end_to_end_ms=100.0, n_items=3)
    )
    engine = AggregationEngine()
    points = {p.model: p for p in engine.frontier(events)}

    assert set(points) == {"A", "B", "C", "D"}
    assert points["A"].on_pareto_front is True
    assert points["B"].on_pareto_front is True
    assert points["C"].on_pareto_front is False
    assert points["D"].on_pareto_front is False

    # every frontier point carries a populated quality CI (Property 10)
    for p in points.values():
        assert p.quality is not None
    # speed axis is the median; p90 whisker present
    assert points["A"].speed_p50_ms == pytest.approx(100.0)
    assert points["A"].speed_p90_ms == pytest.approx(100.0)
    assert points["B"].quality.point == pytest.approx(0.95)


def test_frontier_omits_model_too_thin_for_ci():
    # E has only 1 distinct item -> no defensible quality CI -> omitted (P10).
    events = (
        model_events("A", composite=0.9, end_to_end_ms=100.0, n_items=3)
        + model_events("E", composite=0.8, end_to_end_ms=120.0, n_items=1)
    )
    engine = AggregationEngine()
    models = {p.model for p in engine.frontier(events)}
    assert "A" in models
    assert "E" not in models


def test_frontier_single_model_is_on_front():
    events = model_events("solo", composite=0.7, end_to_end_ms=150.0, n_items=4)
    engine = AggregationEngine()
    [p] = engine.frontier(events)
    assert p.model == "solo"
    assert p.on_pareto_front is True  # nothing dominates it


# ---------------------------------------------------------------------------
# Thin-cell marking (insufficient-data) — Req 9.8 / Property 10
# ---------------------------------------------------------------------------
def test_thin_cell_marked_insufficient_data_with_no_ci():
    events = model_events("m", composite=0.6, end_to_end_ms=30.0, n_items=1, reps=4)
    engine = AggregationEngine()
    [agg] = engine.aggregate(events, ["model"], COMPOSITE_METRIC)
    assert agg.n_items == 1
    assert agg.insufficient_data is True
    assert agg.mean_ci is None  # no fabricated number escapes
    # variance decomp is still reported (it is not a "confident value")
    assert set(agg.variance_decomp) == {"between", "within", "judge"}


def test_fat_cell_carries_ci_and_is_not_insufficient():
    events = model_events("m", composite=0.6, end_to_end_ms=30.0, n_items=5, reps=3)
    engine = AggregationEngine()
    [agg] = engine.aggregate(events, ["model"], COMPOSITE_METRIC)
    assert agg.n_items == 5
    assert agg.insufficient_data is False
    assert agg.mean_ci is not None
    assert agg.mean_ci.point == pytest.approx(0.6)


def test_thin_cell_threshold_is_configurable():
    events = model_events("m", composite=0.6, end_to_end_ms=30.0, n_items=2, reps=2)
    # raise the floor to 3 -> a 2-item cell now reads as insufficient.
    engine = AggregationEngine(min_items_for_ci=3)
    [agg] = engine.aggregate(events, ["model"], COMPOSITE_METRIC)
    assert agg.insufficient_data is True
    assert agg.mean_ci is None


# ---------------------------------------------------------------------------
# Answerability-blend rejection — Req 5.4 / Property 4
# ---------------------------------------------------------------------------
def _mixed_answerability_events() -> list[TrialEvent]:
    full = [
        build_event(composite=0.8, item_id=f"f{j}", model="m", answerability="full")
        for j in range(3)
    ]
    none = [
        build_event(composite=0.2, item_id=f"n{j}", model="m", answerability="none")
        for j in range(3)
    ]
    return full + none


def test_aggregate_rejects_accuracy_blended_across_answerability():
    events = _mixed_answerability_events()
    engine = AggregationEngine()
    with pytest.raises(AnswerabilityBlendError):
        engine.aggregate(events, ["model"], "faithfulness")


def test_aggregate_allows_accuracy_when_sliced_by_answerability():
    events = _mixed_answerability_events()
    engine = AggregationEngine()
    aggs = engine.aggregate(events, ["model", "answerability"], "faithfulness")
    # one aggregate per answerability class, none blended
    classes = {a.group["answerability"] for a in aggs}
    assert classes == {"full", "none"}


def test_aggregate_allows_non_accuracy_metric_across_answerability():
    events = _mixed_answerability_events()
    engine = AggregationEngine()
    # composite and latency are NOT accuracy metrics -> no blend guard
    comp = engine.aggregate(events, ["model"], COMPOSITE_METRIC)
    lat = engine.aggregate(events, ["model"], SPEED_METRIC)
    assert len(comp) == 1
    assert len(lat) == 1


def test_aggregate_grounding_metric_also_guarded():
    events = _mixed_answerability_events()
    engine = AggregationEngine()
    with pytest.raises(AnswerabilityBlendError):
        engine.aggregate(events, ["model"], "grounding_precision")


# ---------------------------------------------------------------------------
# paired_diff_ci (engine method)
# ---------------------------------------------------------------------------
def test_paired_diff_ci_positive_when_a_beats_b():
    a = model_events("A", composite=0.9, end_to_end_ms=100.0, n_items=6, reps=2)
    b = [
        build_event(composite=0.7, item_id=f"A-i{j}", model="B", rep=r)
        for j in range(6)
        for r in range(2)
    ]
    engine = AggregationEngine()
    ci = engine.paired_diff_ci(a + b, "A", "B", COMPOSITE_METRIC)
    assert ci.method == "paired_bootstrap"
    assert ci.point == pytest.approx(0.2, abs=1e-9)


# ---------------------------------------------------------------------------
# High-variance flagging (TARGETED pass)
# ---------------------------------------------------------------------------
def test_flag_high_variance_surfaces_unstable_items():
    engine = AggregationEngine(high_variance_rep_sd=0.15)
    # stable item: reps tightly clustered; unstable item: reps far apart.
    stable = [
        build_event(composite=0.50, item_id="stable", model="m", rep=0),
        build_event(composite=0.51, item_id="stable", model="m", rep=1),
    ]
    unstable = [
        build_event(composite=0.10, item_id="unstable", model="m", rep=0),
        build_event(composite=0.90, item_id="unstable", model="m", rep=1),
    ]
    flagged = engine.flag_high_variance(stable + unstable, COMPOSITE_METRIC)
    ids = {f.item_id for f in flagged}
    assert "unstable" in ids
    assert "stable" not in ids


def test_flag_high_variance_ignores_single_rep_items():
    engine = AggregationEngine()
    events = [build_event(composite=0.5, item_id="solo", model="m", rep=0)]
    assert engine.flag_high_variance(events, COMPOSITE_METRIC) == []


# ---------------------------------------------------------------------------
# Report materialization
# ---------------------------------------------------------------------------
def test_materialize_writes_report_and_safety_is_answerability_sliced(tmp_path):
    events = (
        model_events("A", composite=0.9, end_to_end_ms=100.0, n_items=3)
        + model_events("B", composite=0.7, end_to_end_ms=200.0, n_items=3)
    )
    # add some unanswerable items so the safety panel has a "none" slice
    events += [
        build_event(composite=0.3, item_id=f"A-u{j}", model="A", answerability="none")
        for j in range(3)
    ]
    engine = AggregationEngine()
    out_path = engine.materialize(events, "plan-v1", reports_dir=tmp_path)
    assert out_path.name == "aggregate_plan-v1.json"
    assert out_path.exists()

    report = json.loads(out_path.read_text())
    assert set(report) == {
        "frontier",
        "by_model",
        "safety",
        "cohort_heatmaps",
        "high_variance",
        "provenance",
    }
    # every frontier point + by_model aggregate carries a CI (Property 10)
    for fp in report["frontier"]:
        assert fp["quality"] is not None
    for agg in report["by_model"]:
        assert (agg["mean_ci"] is None) == agg["insufficient_data"]
    # safety panel never blends accuracy across answerability: each row is one class
    safety_classes = {row["group"]["answerability"] for row in report["safety"]}
    assert safety_classes  # at least one class present
    # provenance footer carries the defensibility metadata
    prov = report["provenance"]
    assert prov["plan_version"] == "plan-v1"
    assert prov["ci_method"] == "cluster_bootstrap"
    assert prov["bootstrap_seed"] == config.BOOTSTRAP_SEED
    assert prov["n_items"] > 0
    assert prov["n_trials"] == len(events)
    # generated-at timestamp present for the provenance footer (top-level field)
    assert "generated_at" in prov
    assert isinstance(prov["generated_at"], str) and prov["generated_at"]


def test_materialize_is_deterministic_byte_for_byte(tmp_path):
    events = model_events("A", composite=0.8, end_to_end_ms=120.0, n_items=4)
    e1 = AggregationEngine()
    e2 = AggregationEngine()
    # pin generated_at so the determinism contract is tested on the statistical
    # content: the only non-deterministic field by design is the wall-clock
    # provenance stamp, which is injectable precisely so a byte-identical report
    # is reproducible (Property 9 governs the numbers, not the clock).
    stamp = "2025-01-01T00:00:00+00:00"
    p1 = e1.materialize(events, "v", reports_dir=tmp_path / "a", generated_at=stamp)
    p2 = e2.materialize(events, "v", reports_dir=tmp_path / "b", generated_at=stamp)
    assert p1.read_text() == p2.read_text()


def test_materialize_default_timestamp_is_populated(tmp_path):
    """Without an explicit timestamp, the written report still carries a real
    ISO-8601 generated_at (the provenance footer is never blank)."""
    events = model_events("A", composite=0.8, end_to_end_ms=120.0, n_items=4)
    out = AggregationEngine().materialize(events, "v", reports_dir=tmp_path)
    prov = json.loads(out.read_text())["provenance"]
    # parseable ISO-8601 instant
    from datetime import datetime

    parsed = datetime.fromisoformat(prov["generated_at"])
    assert parsed.tzinfo is not None  # timezone-aware UTC stamp

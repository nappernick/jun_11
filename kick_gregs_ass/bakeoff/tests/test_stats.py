"""
Unit tests for :mod:`bakeoff.stats` (Task 8).

Example / edge-case coverage for the statistics core (the universal P6/P7/P8
properties live in ``test_stats_pbt.py``):

* :func:`extract_metric_value` — resolves composite/accuracy/judge/timing metrics
  and returns ``None`` for an out-of-class answerability field.
* :func:`cluster_bootstrap_ci` — point weights items equally regardless of rep
  count; constant metric -> zero-width CI; deterministic given a seed; CI
  coverage ~= nominal on synthetic data with a known population mean.
* :func:`variance_decomp` — recovers planted ``sigma_between`` / ``sigma_within``
  on a large balanced design, recovers a planted judge sampling SD.
* :func:`normal_approx_ci` / :func:`normal_approx_halfwidth` — centers on the
  equal-item-weight point and reports the closed-form half-width.
* :func:`required_reps_closed_form` / :func:`estimate_required_reps` — match the
  closed-form rep equation, fire the unreachable signal on the impossible branch,
  floor at 2, bump multi-turn >= single-turn, and clamp to budget.
* :func:`paired_diff_ci` — recovers a planted per-item difference.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from bakeoff import config
from bakeoff.stats import (
    Budget,
    Stratum,
    cluster_bootstrap_ci,
    estimate_required_reps,
    extract_metric_value,
    group_rep_means_by_item,
    normal_approx_ci,
    normal_approx_halfwidth,
    paired_diff_ci,
    required_reps_closed_form,
    variance_decomp,
    _z_for_level,
)
from bakeoff.stats import MAX_REPS
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
# Builder — a valid TrialEvent with a controllable composite value & cohort.
# ---------------------------------------------------------------------------
def make_event(
    *,
    composite: float,
    item_id: str,
    model: str = "m",
    rep: int = 0,
    answerability: str = "full",
    judge_dim_sd: dict[str, float] | None = None,
    turn_type: str = "single",
    geography: str = "g",
    pass_name: str = "wide",
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
        grounding_precision=0.5,
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
        judge_model=config.JUDGE_MODEL_ID,
        judge_dim_sd=dict(judge_dim_sd or {}),
    )
    quality = QualityScores(
        accuracy=accuracy,
        judge=judge,
        composite=composite,
        composite_weights_version=config.COMPOSITE_WEIGHTS_VERSION,
    )
    return TrialEvent(
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
            momentary_state="neutral",
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
            retrieval_total_ms=10.0,
            ttft_ms=5.0,
            generation_total_ms=20.0,
            end_to_end_ms=30.0,
        ),
        quality=quality,
        started_at="2025-01-01T00:00:00Z",
        completed_at="2025-01-01T00:00:00.030Z",
        error=None,
    )


def events_from_item_values(
    item_values: dict[str, list[float]],
    *,
    model: str = "m",
    turn_type: str = "single",
    geography: str = "g",
    answerability: str = "full",
) -> list[TrialEvent]:
    """Flatten ``{item_id: [rep values]}`` into a list of events."""
    out: list[TrialEvent] = []
    for item_id, values in item_values.items():
        for rep, v in enumerate(values):
            out.append(
                make_event(
                    composite=v,
                    item_id=item_id,
                    model=model,
                    rep=rep,
                    turn_type=turn_type,
                    geography=geography,
                    answerability=answerability,
                )
            )
    return out


def expected_equal_item_point(item_values: dict[str, list[float]]) -> float:
    """Mean over items of (mean over that item's reps) — the Property-7 statistic."""
    return float(np.mean([np.mean(v) for v in item_values.values()]))


# ---------------------------------------------------------------------------
# extract_metric_value
# ---------------------------------------------------------------------------
def test_extract_metric_value_namespaces():
    e = make_event(composite=0.7, item_id="i0", answerability="none")
    assert extract_metric_value(e, "composite") == 0.7
    assert extract_metric_value(e, "faithfulness") == 0.7
    assert extract_metric_value(e, "judge.correctness") == 0.7
    assert extract_metric_value(e, "end_to_end_ms") == 30.0
    # abstention populated for "none"; unwarranted_refusal out-of-class -> None
    assert extract_metric_value(e, "abstention_correct") == 1.0
    assert extract_metric_value(e, "unwarranted_refusal") is None


def test_extract_metric_value_unknown_raises():
    e = make_event(composite=0.1, item_id="i0")
    with pytest.raises(KeyError):
        extract_metric_value(e, "not_a_metric")


# ---------------------------------------------------------------------------
# cluster_bootstrap_ci: equal item weighting (P7 example) + determinism
# ---------------------------------------------------------------------------
def test_bootstrap_point_weights_items_equally_not_reps():
    # item A: 1 rep at 0.0 ; item B: 100 reps at 1.0.
    # Equal-item-weight mean = (0 + 1)/2 = 0.5 (NOT the rep-weighted 100/101).
    item_values = {"A": [0.0], "B": [1.0] * 100}
    events = events_from_item_values(item_values)
    ci = cluster_bootstrap_ci(events, "composite", n_boot=200)
    assert ci.point == pytest.approx(0.5)
    pooled = (0.0 + 100.0) / 101.0  # the rep-weighted mean we must NOT compute
    assert abs(ci.point - pooled) > 0.4


def test_bootstrap_ci_constant_metric_zero_width():
    events = events_from_item_values({f"i{i}": [0.42, 0.42] for i in range(8)})
    ci = cluster_bootstrap_ci(events, "composite", n_boot=200)
    assert ci.point == pytest.approx(0.42)
    assert ci.low == pytest.approx(0.42)
    assert ci.high == pytest.approx(0.42)
    assert ci.method == "cluster_bootstrap"


def test_bootstrap_ci_deterministic_given_seed():
    rng = np.random.default_rng(0)
    item_values = {f"i{i}": list(rng.normal(0.5, 0.1, size=5)) for i in range(20)}
    events = events_from_item_values(item_values)
    a = cluster_bootstrap_ci(events, "composite", n_boot=500, seed=config.BOOTSTRAP_SEED)
    b = cluster_bootstrap_ci(events, "composite", n_boot=500, seed=config.BOOTSTRAP_SEED)
    assert (a.point, a.low, a.high) == (b.point, b.low, b.high)


def test_bootstrap_ci_brackets_point():
    rng = np.random.default_rng(7)
    item_values = {f"i{i}": list(rng.normal(0.5, 0.2, size=4)) for i in range(40)}
    events = events_from_item_values(item_values)
    ci = cluster_bootstrap_ci(events, "composite", n_boot=800)
    assert ci.low <= ci.point <= ci.high


def test_bootstrap_ci_empty_raises():
    with pytest.raises(ValueError):
        cluster_bootstrap_ci([], "composite", n_boot=10)


def test_bootstrap_ci_coverage_approximately_nominal():
    """Across many synthetic datasets with a known population mean ``mu``, the
    95% cluster-bootstrap CI should cover ``mu`` close to 95% of the time."""
    mu, sigma_b, sigma_w = 0.5, 0.10, 0.10
    n_items, reps, n_sims = 50, 4, 150
    covered = 0
    for sim in range(n_sims):
        rng = np.random.default_rng(1000 + sim)
        item_effects = rng.normal(0.0, sigma_b, size=n_items)
        item_values = {
            f"i{j}": list(mu + item_effects[j] + rng.normal(0.0, sigma_w, size=reps))
            for j in range(n_items)
        }
        events = events_from_item_values(item_values)
        ci = cluster_bootstrap_ci(events, "composite", n_boot=400, seed=12345)
        if ci.low <= mu <= ci.high:
            covered += 1
    coverage = covered / n_sims
    # Percentile bootstrap can undercover slightly at n=50; keep a meaningful band.
    assert 0.86 <= coverage <= 0.995, f"coverage={coverage:.3f} off nominal 0.95"


# ---------------------------------------------------------------------------
# variance_decomp: recover planted sigmas + judge component
# ---------------------------------------------------------------------------
def test_variance_decomp_recovers_planted_sigmas():
    mu, sigma_b, sigma_w = 0.5, 0.20, 0.10
    n_items, reps = 300, 8
    rng = np.random.default_rng(2024)
    item_effects = rng.normal(0.0, sigma_b, size=n_items)
    item_values = {
        f"i{j}": list(mu + item_effects[j] + rng.normal(0.0, sigma_w, size=reps))
        for j in range(n_items)
    }
    events = events_from_item_values(item_values)
    d = variance_decomp(events, "composite")
    assert math.sqrt(d["within"]) == pytest.approx(sigma_w, abs=0.02)
    assert math.sqrt(d["between"]) == pytest.approx(sigma_b, abs=0.04)


def test_variance_decomp_recovers_judge_sampling_sd():
    rng = np.random.default_rng(5)
    events = []
    for j in range(30):
        for rep in range(3):
            events.append(
                make_event(
                    composite=float(rng.normal(0.5, 0.1)),
                    item_id=f"i{j}",
                    rep=rep,
                    judge_dim_sd={"faithfulness": 0.3},
                )
            )
    d = variance_decomp(events, "faithfulness")
    # judge component = mean over events of judge sampling variance = 0.3^2.
    assert d["judge"] == pytest.approx(0.09, abs=1e-9)


def test_variance_decomp_non_judge_metric_has_zero_judge_component():
    events = events_from_item_values({f"i{i}": [0.5, 0.6] for i in range(5)})
    d = variance_decomp(events, "composite")
    assert d["judge"] == 0.0


def test_variance_decomp_single_item_between_is_zero():
    events = events_from_item_values({"only": [0.1, 0.2, 0.3, 0.4]})
    d = variance_decomp(events, "composite")
    assert d["between"] == 0.0
    assert d["within"] > 0.0


def test_variance_decomp_empty_is_zero():
    d = variance_decomp([], "composite")
    assert d == {"between": 0.0, "within": 0.0, "judge": 0.0}


# ---------------------------------------------------------------------------
# normal_approx_ci / normal_approx_halfwidth
# ---------------------------------------------------------------------------
def test_normal_approx_halfwidth_matches_formula():
    between, within, n, r = 0.04, 0.01, 10, 4  # variances
    z = 1.96
    expected = z * math.sqrt(between / n + within / (n * r))
    assert normal_approx_halfwidth(between, within, n, r, z) == pytest.approx(expected)


def test_normal_approx_ci_centers_and_uses_closed_form():
    mu, sigma_b, sigma_w = 0.5, 0.15, 0.08
    n_items, reps = 80, 5
    rng = np.random.default_rng(99)
    item_effects = rng.normal(0.0, sigma_b, size=n_items)
    item_values = {
        f"i{j}": list(mu + item_effects[j] + rng.normal(0.0, sigma_w, size=reps))
        for j in range(n_items)
    }
    events = events_from_item_values(item_values)
    ci = normal_approx_ci(events, "composite", level=0.95)
    assert ci.method == "normal_approx"
    assert ci.point == pytest.approx(expected_equal_item_point(item_values))
    # half-width reproduces z * normal_approx_halfwidth(...) with the decomp.
    d = variance_decomp(events, "composite")
    z = _z_for_level(0.95)
    expected_half = normal_approx_halfwidth(d["between"], d["within"], n_items, reps, z)
    assert (ci.high - ci.point) == pytest.approx(expected_half, rel=1e-9)
    assert (ci.point - ci.low) == pytest.approx(expected_half, rel=1e-9)


def test_normal_approx_ci_empty_raises():
    with pytest.raises(ValueError):
        normal_approx_ci([], "composite")


# ---------------------------------------------------------------------------
# paired_diff_ci
# ---------------------------------------------------------------------------
def test_paired_diff_ci_recovers_known_difference():
    rng = np.random.default_rng(3)
    delta = 0.2  # model A is +0.2 over B per item
    a_events: list[TrialEvent] = []
    b_events: list[TrialEvent] = []
    for j in range(40):
        base = float(rng.normal(0.5, 0.15))
        for rep in range(3):
            a_events.append(
                make_event(composite=base + delta + float(rng.normal(0, 0.02)),
                           item_id=f"i{j}", model="A", rep=rep)
            )
            b_events.append(
                make_event(composite=base + float(rng.normal(0, 0.02)),
                           item_id=f"i{j}", model="B", rep=rep)
            )
    ci = paired_diff_ci(a_events, b_events, "composite", n_boot=800)
    assert ci.method == "paired_bootstrap"
    assert ci.point == pytest.approx(delta, abs=0.03)
    assert ci.low <= ci.point <= ci.high
    assert ci.low > 0.0, "a real +0.2 difference should separate from 0"


def test_paired_diff_ci_no_shared_items_raises():
    a = [make_event(composite=0.5, item_id="x", model="A")]
    b = [make_event(composite=0.5, item_id="y", model="B")]
    with pytest.raises(ValueError):
        paired_diff_ci(a, b, "composite", n_boot=10)


# ---------------------------------------------------------------------------
# required_reps_closed_form: closed-form match, floor, unreachable
# ---------------------------------------------------------------------------
def test_required_reps_closed_form_matches_equation_reachable():
    sw, sb, n = 0.30, 0.0, 30
    target_w, z = 0.05, _z_for_level(0.95)
    target_var = (target_w / z) ** 2
    expected = math.ceil(sw**2 / (n * (target_var - sb**2 / n)))
    expected = min(MAX_REPS, max(config.MIN_REPS_PER_STRATUM, expected))
    reps, unreachable = required_reps_closed_form(sw, sb, n, target_w, z)
    assert unreachable is False
    assert reps == expected


def test_required_reps_closed_form_floors_at_two():
    # negligible variance => math wants 0 reps; floor to MIN_REPS_PER_STRATUM (2).
    reps, unreachable = required_reps_closed_form(0.0, 0.0, 100, 0.05, _z_for_level(0.95))
    assert unreachable is False
    assert reps == config.MIN_REPS_PER_STRATUM


def test_required_reps_closed_form_unreachable_when_between_exceeds_target():
    # sigma_between large + few items: sb^2/n >= target_var => unreachable.
    sb, n = 0.5, 2
    target_w, z = 0.01, _z_for_level(0.95)
    target_var = (target_w / z) ** 2
    assert sb**2 / n >= target_var  # precondition of the impossible branch
    reps, unreachable = required_reps_closed_form(0.05, sb, n, target_w, z)
    assert unreachable is True
    assert reps == MAX_REPS


# ---------------------------------------------------------------------------
# estimate_required_reps: integration (floor, multi-turn bump, budget clamp)
# ---------------------------------------------------------------------------
def _pilot_events(
    *, geography: str, turn_type: str, n_items: int, reps: int, mean_fn, within_sd: float, seed: int
) -> list[TrialEvent]:
    rng = np.random.default_rng(seed)
    events: list[TrialEvent] = []
    for j in range(n_items):
        mean = mean_fn(j)
        for rep in range(reps):
            v = mean + (float(rng.normal(0.0, within_sd)) if within_sd > 0 else 0.0)
            events.append(
                make_event(
                    composite=v,
                    item_id=f"{geography}-{j}",
                    rep=rep,
                    geography=geography,
                    turn_type=turn_type,
                )
            )
    return events


def test_estimate_required_reps_floor_and_dict_shape():
    events = _pilot_events(
        geography="S", turn_type="single", n_items=10, reps=5,
        mean_fn=lambda j: 0.5, within_sd=0.0, seed=1,
    )
    strata = [Stratum(id="S", n_items=100, turn_type="single",
                      passes=("wide",), predicate={"geography": "S"})]
    out = estimate_required_reps(events, strata, target_w=0.05,
                                 z=_z_for_level(0.95), budget={"max_trials": 10**9})
    # dict-like access over {stratum: {pass: reps}}
    assert out["S"]["wide"] == config.MIN_REPS_PER_STRATUM
    assert "S" in out
    assert out.unreachable["S"] is False


def test_estimate_required_reps_unreachable_flag():
    # wide item-mean spread + only 2 full-run items => unreachable.
    events = _pilot_events(
        geography="S", turn_type="single", n_items=12, reps=4,
        mean_fn=lambda j: 0.1 * j, within_sd=0.02, seed=4,
    )
    strata = [Stratum(id="S", n_items=2, turn_type="single",
                      passes=("wide",), predicate={"geography": "S"})]
    out = estimate_required_reps(events, strata, target_w=0.01,
                                 z=_z_for_level(0.95), budget={"max_trials": 10**9})
    assert out.unreachable["S"] is True
    assert out["S"]["wide"] >= config.MIN_REPS_PER_STRATUM


def test_estimate_required_reps_multi_turn_ge_single_turn():
    single = _pilot_events(
        geography="S", turn_type="single", n_items=15, reps=10,
        mean_fn=lambda j: 0.5, within_sd=0.30, seed=21,
    )
    multi = _pilot_events(
        geography="M", turn_type="multi", n_items=15, reps=10,
        mean_fn=lambda j: 0.5, within_sd=0.0, seed=22,  # negligible -> would floor
    )
    strata = [
        Stratum(id="S", n_items=40, turn_type="single", passes=("wide",),
                predicate={"geography": "S"}),
        Stratum(id="M", n_items=40, turn_type="multi", passes=("wide",),
                predicate={"geography": "M"}),
    ]
    out = estimate_required_reps(single + multi, strata, target_w=0.05,
                                 z=_z_for_level(0.95), budget={"max_trials": 10**9})
    assert out["S"]["wide"] > config.MIN_REPS_PER_STRATUM  # genuinely needs more
    assert out["M"]["wide"] >= out["S"]["wide"]  # multi-turn equalized up


def test_estimate_required_reps_budget_clamp():
    events = _pilot_events(
        geography="S", turn_type="single", n_items=10, reps=8,
        mean_fn=lambda j: 0.5, within_sd=0.40, seed=31,
    )
    n_items = 50
    strata = [Stratum(id="S", n_items=n_items, turn_type="single",
                      passes=("wide",), predicate={"geography": "S"})]
    z = _z_for_level(0.95)
    unclamped = estimate_required_reps(
        events, strata, 0.05, z, budget={"max_trials": 10**9}
    )["S"]["wide"]
    assert unclamped > config.MIN_REPS_PER_STRATUM
    tight = (unclamped - 1) * n_items
    clamped = estimate_required_reps(
        events, strata, 0.05, z, budget=Budget(max_trials=tight)
    )["S"]["wide"]
    assert clamped < unclamped
    assert clamped * n_items <= tight
    assert clamped >= config.MIN_REPS_PER_STRATUM

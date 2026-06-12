"""Task 1 scaffold checks: the package imports cleanly, the core types
instantiate, ``trial_id`` is deterministic/stable, and the credential-expiry
config surface that tasks 5/6/7/10 depend on is present and well-formed.

These are deliberately lightweight (no network, no servers) — they verify the
contract surface, not behavior of later tasks.

Validates: Requirements 8.1, 8.2, 15.5
"""
from __future__ import annotations

import dataclasses

import pytest


# ---------------------------------------------------------------------------
# Import cleanliness
# ---------------------------------------------------------------------------
def test_package_imports_cleanly() -> None:
    import bakeoff

    assert bakeoff.SCHEMA_VERSION
    assert callable(bakeoff.trial_id)


def test_submodules_import_cleanly() -> None:
    import bakeoff.config  # noqa: F401
    import bakeoff.ids  # noqa: F401
    import bakeoff.types  # noqa: F401


# ---------------------------------------------------------------------------
# trial_id determinism / stability (Req 8.2)
# ---------------------------------------------------------------------------
def test_trial_id_is_deterministic() -> None:
    from bakeoff.ids import trial_id

    a = trial_id("nova-pro", "b0-q01", 0, "wide", "plan-v1")
    b = trial_id("nova-pro", "b0-q01", 0, "wide", "plan-v1")
    assert a == b
    assert isinstance(a, str) and len(a) > 0
    # lowercase hex digest prefix
    int(a, 16)


@pytest.mark.parametrize(
    "changed",
    [
        ("nova-lite", "b0-q01", 0, "wide", "plan-v1"),
        ("nova-pro", "b0-q02", 0, "wide", "plan-v1"),
        ("nova-pro", "b0-q01", 1, "wide", "plan-v1"),
        ("nova-pro", "b0-q01", 0, "deep", "plan-v1"),
        ("nova-pro", "b0-q01", 0, "wide", "plan-v2"),
    ],
)
def test_trial_id_changes_when_any_field_changes(changed: tuple) -> None:
    from bakeoff.ids import trial_id

    base = trial_id("nova-pro", "b0-q01", 0, "wide", "plan-v1")
    assert trial_id(*changed) != base


def test_schema_version_constant_is_shared() -> None:
    import bakeoff
    from bakeoff.ids import SCHEMA_VERSION

    assert bakeoff.SCHEMA_VERSION == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Types instantiate and are frozen
# ---------------------------------------------------------------------------
def _build_trial_event():
    from bakeoff import types as t
    from bakeoff.ids import SCHEMA_VERSION, trial_id

    cohort = t.CohortKey(
        geography="Nigeria (Lagos)",
        proficiency="functional",
        tone="terse",
        entry_route="slack",
        momentary_state="frustrated",
        answerability="none",
        turn_type="single",
    )
    accuracy = t.AccuracyScores(
        precision_at_k=0.4,
        recall_at_k=0.5,
        mrr=0.5,
        ndcg_at_k=0.6,
        grounding_precision=0.0,
        grounding_recall=0.0,
        semantic_similarity=0.3,
        abstention_correct=1,   # answerability == none -> populated
        unwarranted_refusal=None,
    )
    judge = t.JudgeScores(
        faithfulness=0.9,
        correctness=0.8,
        completeness=0.7,
        judge_sample_count=3,
        judge_model="judge-x",
        judge_dim_sd={"faithfulness": 0.05},
    )
    quality = t.QualityScores(
        accuracy=accuracy,
        judge=judge,
        composite=0.72,
        composite_weights_version="default-v1",
    )
    timings = t.StageTimings(
        embed_query_ms=10.0,
        bm25_vectorize_ms=2.0,
        hybrid_search_ms=5.0,
        rerank_ms=8.0,
        retrieval_total_ms=25.0,
        ttft_ms=120.0,
        generation_total_ms=480.0,
        end_to_end_ms=505.0,
    )
    retrieval = t.RetrievalRecord(
        fragment_ids=["n1", "n2"], confidence=[0.9, 0.5], cache_hit=True
    )
    return t.TrialEvent(
        trial_id=trial_id("nova-pro", "b0-q01", 0, "wide", "plan-v1"),
        schema_version=SCHEMA_VERSION,
        plan_version="plan-v1",
        model="nova-pro",
        item_id="b0-q01",
        turn_type="single",
        pass_name="wide",
        rep=0,
        temperature=0.2,
        cohort=cohort,
        query="how do I reset my badge?",
        gold_node_ids=["n1"],
        answerability="none",
        retrieval=retrieval,
        answer_text="I can't confirm that; please contact IT support.",
        token_usage={"prompt": 100, "completion": 20, "total": 120},
        timings=timings,
        quality=quality,
        started_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T00:00:01Z",
        error=None,
    )


def test_trial_event_instantiates_with_nested_types() -> None:
    ev = _build_trial_event()
    assert ev.cohort.answerability == "none"
    assert ev.quality.accuracy.abstention_correct == 1
    assert ev.timings.end_to_end_ms == 505.0
    assert ev.quality.judge.judge_dim_sd["faithfulness"] == 0.05


def test_core_dataclasses_are_frozen() -> None:
    from bakeoff import types as t

    ck = t.CohortKey(
        geography="g", proficiency="p", tone="t", entry_route="e",
        momentary_state="m", answerability="full", turn_type="single",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ck.geography = "other"  # type: ignore[misc]


def test_remaining_design_types_instantiate() -> None:
    from bakeoff import types as t

    item = t.Item(
        id="b0-q01",
        turn_type="single",
        cohort=t.CohortKey(
            geography="g", proficiency="p", tone="t", entry_route="e",
            momentary_state="m", answerability="full", turn_type="single",
        ),
        query="q",
        gold=[t.GoldFragment(node_id="n1", title="Title", snippet="snip")],
    )
    assert item.gold[0].node_id == "n1"
    assert item.item_id == "b0-q01"        # alias
    assert item.is_multi_turn is False

    rr = t.RetrievalResult(
        fragments=[{"id": "n1"}], fragment_ids=["n1"], confidence=[0.9],
        timings={"total_ms": 25.0}, cache_hit=False,
    )
    assert rr.fragment_ids == ["n1"]

    mr = t.ModelResponse(
        text="hi", ttft_ms=100.0, generation_total_ms=200.0,
        token_usage={"total": 10},
    )
    assert mr.ttft_ms == 100.0

    spec = t.TrialSpec(
        model="nova-pro", item_id="b0-q01", turn_type="single",
        rep=0, pass_name="wide", plan_version="plan-v1", temperature=0.2,
    )
    assert spec.pass_name == "wide"
    # trial_id is derived and agrees with the standalone function
    from bakeoff.ids import trial_id as _tid
    assert spec.trial_id == _tid("nova-pro", "b0-q01", 0, "wide", "plan-v1")

    plan = t.SamplingPlan(
        plan_version="plan-v1", temperature=0.2, target_ci_halfwidth=0.05,
        confidence_level=0.95,
        strata=[t.StratumPlan(cohort_predicate={"turn_type": "multi"},
                              passes={"wide": 2, "deep": 8}, rationale="multi bump")],
        budget={"max_trials": 100000},
        pilot_variance_model={}, composite_weights={"grounding": 1.0},
    )
    assert plan.strata[0].passes["deep"] == 8

    ci = t.CI(point=0.7, low=0.6, high=0.8, method="cluster_bootstrap")
    agg = t.Aggregate(
        group={"model": "nova-pro"}, metric="composite", n_items=50, n_trials=100,
        mean_ci=ci, variance_decomp={"between": 0.1, "within": 0.02, "judge": 0.01},
    )
    assert agg.latency_quantiles is None

    fp = t.FrontierPoint(
        model="nova-pro", quality=ci, speed_p50_ms=300.0, speed_p90_ms=600.0,
        on_pareto_front=True,
    )
    assert fp.on_pareto_front is True


def test_cohort_key_helpers_roundtrip() -> None:
    from bakeoff.types import COHORT_DIMENSIONS, CohortKey

    ck = CohortKey(
        geography="Nigeria (Lagos)", proficiency="functional", tone="terse",
        entry_route="slack", momentary_state="frustrated", answerability="none",
        turn_type="single",
    )
    d = ck.to_dict()
    assert set(d) == set(COHORT_DIMENSIONS)
    assert CohortKey.from_dict(d) == ck
    assert ck.project(["geography"]) == {"geography": "Nigeria (Lagos)"}
    # cell_id is stable and deterministic
    assert ck.cell_id() == ck.cell_id()
    assert isinstance(ck.cell_id(), str)


# ---------------------------------------------------------------------------
# Credential-expiry config surface (cross-cutting; consumed by tasks 5/6/7/10)
# ---------------------------------------------------------------------------
def test_error_class_taxonomy_present() -> None:
    from bakeoff.types import ErrorClass

    # JSON-serializable (subclass of str) so it can land on a TrialEvent directly.
    assert ErrorClass.AUTH_EXPIRED == "auth_expired"
    assert {
        "auth_expired", "throttled", "transient", "permanent", "unknown",
    } <= {e.value for e in ErrorClass}


def test_credential_expiry_config_surface_present() -> None:
    from bakeoff import config as c

    # refresh policy knobs
    assert isinstance(c.AUTH_MAX_REFRESH_CYCLES, int) and c.AUTH_MAX_REFRESH_CYCLES >= 1
    assert c.AUTH_BACKOFF_BASE_S > 0 and c.AUTH_BACKOFF_MAX_S >= c.AUTH_BACKOFF_BASE_S
    assert c.RETRY_MAX_ATTEMPTS >= 1

    # auth-expiry signatures the consumers match against
    assert "ExpiredTokenException" in c.AUTH_EXPIRED_ERROR_CODES
    assert "UnrecognizedClientException" in c.AUTH_EXPIRED_ERROR_CODES
    assert 401 in c.AUTH_EXPIRED_HTTP_STATUSES and 403 in c.AUTH_EXPIRED_HTTP_STATUSES
    assert len(c.AUTH_EXPIRED_MESSAGE_SIGNATURES) > 0

    # throttle / transient classes are distinct from auth
    assert 429 in c.THROTTLE_HTTP_STATUSES
    assert 401 not in c.THROTTLE_HTTP_STATUSES
    assert 503 in c.TRANSIENT_HTTP_STATUSES


def test_core_config_constants_present() -> None:
    from bakeoff import config as c

    assert c.DEFAULT_TEMPERATURE == pytest.approx(0.2)
    assert 0 < c.CONFIDENCE_LEVEL < 1
    assert c.TARGET_CI_HALFWIDTH > 0
    assert c.JUDGE_MODEL_ID
    assert set(c.CONCURRENCY_CAPS) >= {"model", "judge", "embed", "retrieve"}
    # composite weights are a transparent dict that sums to ~1.0
    assert abs(sum(c.COMPOSITE_WEIGHTS.values()) - 1.0) < 1e-9
    # judge must not be a candidate (self-preference bias guard)
    assert c.JUDGE_MODEL_ID not in {m.bedrock_model_id for m in c.CANDIDATE_MODELS}


def test_paths_live_under_data_bakeoff() -> None:
    from bakeoff import config as c

    assert c.BAKEOFF_DIR.name == "bakeoff"
    assert c.BAKEOFF_DIR.parent.name == "data"
    for p in (c.SAMPLING_PLAN_PATH, c.TRIAL_EVENTS_PATH, c.PILOT_EVENTS_PATH):
        assert str(p).startswith(str(c.BAKEOFF_DIR))

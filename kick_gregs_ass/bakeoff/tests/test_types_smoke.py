"""
Smoke tests for the bakeoff core types and id helpers (Task 1).

Constructs one minimal valid instance of every dataclass in ``bakeoff.types`` and
asserts ``trial_id`` determinism (the load-bearing invariant for resume
idempotence, design Property 3). These are intentionally lightweight: deeper
behavior (serialization round-trip, validation rules) is covered by later tasks.
"""
from __future__ import annotations

import bakeoff
import bakeoff.config as config
from bakeoff.ids import SCHEMA_VERSION, trial_id
from bakeoff.types import (
    AccuracyScores,
    Aggregate,
    CI,
    CohortKey,
    FrontierPoint,
    GoldFragment,
    Item,
    JudgeScores,
    ModelResponse,
    QualityScores,
    RetrievalRecord,
    RetrievalResult,
    SamplingPlan,
    StageTimings,
    StratumPlan,
    TrialEvent,
    TrialSpec,
    Turn,
)


def _cohort() -> CohortKey:
    return CohortKey(
        geography="Nigeria (Lagos)",
        proficiency="broken",
        tone="terse",
        entry_route="slack",
        momentary_state="neutral",
        answerability="full",
        turn_type="single",
    )


def _timings() -> StageTimings:
    return StageTimings(
        embed_query_ms=1.0,
        bm25_vectorize_ms=2.0,
        hybrid_search_ms=3.0,
        rerank_ms=4.0,
        retrieval_total_ms=10.0,
        ttft_ms=5.0,
        generation_total_ms=20.0,
        end_to_end_ms=30.0,
    )


def _quality() -> QualityScores:
    accuracy = AccuracyScores(
        precision_at_k=1.0,
        recall_at_k=1.0,
        mrr=1.0,
        ndcg_at_k=1.0,
        grounding_precision=1.0,
        grounding_recall=1.0,
        semantic_similarity=0.9,
        abstention_correct=None,
        unwarranted_refusal=0,
    )
    judge = JudgeScores(
        faithfulness=5.0,
        correctness=5.0,
        completeness=5.0,
        judge_sample_count=3,
        judge_dim_sd={"faithfulness": 0.1},
        judge_model=config.JUDGE_MODEL_ID,
    )
    return QualityScores(
        accuracy=accuracy,
        judge=judge,
        composite=0.87,
        composite_weights_version=config.COMPOSITE_WEIGHTS_VERSION,
    )


def test_construct_every_dataclass():
    cohort = _cohort()

    gold = GoldFragment(node_id="n1", title="Travel profile", snippet="...")
    turn = Turn(turn=1, user_utterance="hi", momentary_state="neutral")
    item = Item(
        id="b0-q01",
        query="how do i add my passport name",
        cohort=cohort,
        gold_node_ids=["n1"],
        gold=[gold],
        answerability="full",
        turn_type="single",
        turns=(turn,),
    )
    assert item.is_multi_turn is False

    retrieval_record = RetrievalRecord(
        fragment_ids=["n1", "n2"], confidence=[0.9, 0.5], cache_hit=False
    )
    retrieval_result = RetrievalResult(
        fragments=[{"id": "n1"}],
        fragment_ids=["n1"],
        confidence=[0.9],
        timings={"total_ms": 10.0},
        cache_hit=False,
    )
    response = ModelResponse(
        text="answer",
        ttft_ms=5.0,
        generation_total_ms=20.0,
        token_usage={"prompt": 10, "completion": 20, "total": 30},
    )

    event = TrialEvent(
        trial_id=trial_id("m", item.id, 0, "wide", "p1"),
        schema_version=SCHEMA_VERSION,
        plan_version="p1",
        model="m",
        item_id=item.id,
        turn_type="single",
        pass_name="wide",
        rep=0,
        temperature=config.DEFAULT_TEMPERATURE,
        cohort=cohort,
        query=item.query,
        gold_node_ids=item.gold_node_ids,
        answerability="full",
        retrieval=retrieval_record,
        answer_text="answer",
        token_usage={"prompt": 10, "completion": 20, "total": 30},
        timings=_timings(),
        quality=_quality(),
        started_at="2025-01-01T00:00:00Z",
        completed_at="2025-01-01T00:00:01Z",
        error=None,
    )
    assert event.schema_version == "1.0"

    stratum = StratumPlan(
        cohort_predicate={"turn_type": "multi"},
        passes={"wide": 2, "deep": 8},
        rationale="multi-turn: R raised to equalize CI",
    )
    plan = SamplingPlan(
        plan_version="p1",
        temperature=0.2,
        target_ci_halfwidth=0.05,
        confidence_level=0.95,
        strata=[stratum],
        budget={"max_trials": 1000},
        pilot_variance_model={"multi": {"within": 0.1, "between": 0.2}},
        composite_weights=dict(config.COMPOSITE_WEIGHTS),
    )
    assert plan.strata[0].passes["deep"] == 8

    ci = CI(point=0.87, low=0.82, high=0.92, method="cluster_bootstrap")
    aggregate = Aggregate(
        group={"model": "m"},
        metric="composite",
        n_items=100,
        n_trials=200,
        mean_ci=ci,
        variance_decomp={"between": 0.2, "within": 0.1, "judge": 0.05},
        latency_quantiles=None,
    )
    assert aggregate.mean_ci.method == "cluster_bootstrap"

    frontier_point = FrontierPoint(
        model="m",
        quality=ci,
        speed_p50_ms=120.0,
        speed_p90_ms=300.0,
        on_pareto_front=True,
    )
    assert frontier_point.on_pareto_front is True

    spec = TrialSpec(
        model="m",
        item_id=item.id,
        rep=0,
        pass_name="wide",
        plan_version="p1",
        temperature=0.2,
    )
    # TrialSpec.trial_id agrees with the standalone trial_id function.
    assert spec.trial_id == trial_id("m", item.id, 0, "wide", "p1")
    # And with the id stamped on the event built from the same identity.
    assert spec.trial_id == event.trial_id


def test_trial_id_deterministic_and_distinct():
    a = trial_id("m", "i", 0, "wide", "p1")
    b = trial_id("m", "i", 0, "wide", "p1")
    assert a == b, "same inputs must produce the same trial_id"

    # Any field difference changes the id.
    assert a != trial_id("m", "i", 1, "wide", "p1")   # rep
    assert a != trial_id("m2", "i", 0, "wide", "p1")  # model
    assert a != trial_id("m", "i2", 0, "wide", "p1")  # item_id
    assert a != trial_id("m", "i", 0, "deep", "p1")   # pass_name
    assert a != trial_id("m", "i", 0, "wide", "p2")   # plan_version


def test_package_reexports():
    # bakeoff package re-exports the id helpers without pulling heavy deps.
    assert bakeoff.SCHEMA_VERSION == SCHEMA_VERSION
    assert bakeoff.trial_id("m", "i", 0, "wide", "p1") == trial_id(
        "m", "i", 0, "wide", "p1"
    )

"""
Task-7 acceptance tests: judge scorer, answerability, and the scoring pipeline
(Requirements 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 5.2, 5.3, 15.4). Everything runs
**fully offline** — the judge uses the deterministic :class:`StubJudge` and the
semantic scorer uses an injected fake embedder. No real Bedrock call is made.

Coverage:
* **Answerability per class on real MockAdapter outputs** — fabricate-on-none →
  ``abstention_correct == 0``; refuse-on-full → ``unwarranted_refusal == 1``;
  well-behaved → correct (Req 5.1/5.2/5.3).
* **Judge SD reported across k samples** — ``judge_dim_sd`` is populated and
  nonzero for ``k > 1`` and zero for ``k == 1`` (Req 4.5).
* **Composite uses the provided weights** — overriding the weights changes the
  composite deterministically and the version is recorded (Req 4.6).
* **Content-hash cache prevents a second judge call for identical content** (Req 4.7).
* **Fully-offline pipeline** — ``ScoringPipeline.offline`` scores a real
  MockAdapter response end-to-end with zero network (15.4 / demo seam).

Validates: Requirements 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 5.2, 5.3, 15.4
"""
from __future__ import annotations

import asyncio

import pytest

from bakeoff import config
from bakeoff.adapters.mock import MockAdapter, MockProfile
from bakeoff.scoring.answerability import (
    flags_gap,
    is_refusal,
    score_answerability,
)
from bakeoff.scoring.judge import (
    JUDGE_DIMENSIONS,
    JudgeRequest,
    JudgeSample,
    JudgeScorer,
    StubJudge,
    make_stub_judge,
    mean_sd,
    order_schedule,
)
from bakeoff.scoring.pipeline import ScoringPipeline, compute_composite
from bakeoff.types import (
    AccuracyScores,
    CohortKey,
    GoldFragment,
    Item,
    JudgeScores,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
GOLD_TEXT = (
    "Submit your expense report within thirty days of the purchase date to be "
    "reimbursed for business travel costs."
)
FRAGMENTS = [
    {"id": "G1", "text": GOLD_TEXT},
    {"id": "D1", "text": "The travel booking tool lives in the Slack sidebar shortcuts."},
]
GOLD = [GoldFragment(node_id="G1", title="Expense reimbursement", snippet=GOLD_TEXT)]


def _cohort(answerability: str, momentary_state: str = "neutral", turn_type: str = "single") -> CohortKey:
    return CohortKey(
        geography="Nigeria (Lagos)",
        proficiency="fluent",
        tone="terse",
        entry_route="slack",
        momentary_state=momentary_state,
        answerability=answerability,
        turn_type=turn_type,
    )


def _item(answerability: str = "full", momentary_state: str = "neutral", item_id: str = "b0-q01") -> Item:
    return Item(
        id=item_id,
        turn_type="single",
        cohort=_cohort(answerability, momentary_state),
        query="how do I get reimbursed for travel?",
        wants="how to get reimbursed for business travel",
        answerability=answerability,
        gold_node_ids=["G1"],
        gold=GOLD,
    )


def _generate(adapter: MockAdapter, item: Item) -> str:
    return asyncio.run(adapter.generate(item, FRAGMENTS, 0.0)).text


# ===========================================================================
# Answerability — per class, on REAL MockAdapter outputs (Req 5.1/5.2/5.3)
# ===========================================================================
def test_fabricate_on_unanswerable_scores_abstention_zero():
    # Req 5.1: a model that fabricates on an unanswerable item -> abstention_correct == 0.
    item = _item(answerability="none")
    answer = _generate(MockAdapter("m", seed=0, profile=MockProfile.fabricator()), item)
    result = score_answerability(answer, "none")
    assert result == {"abstention_correct": 0}


def test_well_behaved_refusal_on_unanswerable_scores_abstention_one():
    # Req 5.1: the default (non-fabricating) adapter refuses -> abstention_correct == 1.
    item = _item(answerability="none")
    answer = _generate(MockAdapter("m", seed=0), item)
    result = score_answerability(answer, "none")
    assert result == {"abstention_correct": 1}


def test_refuse_on_answerable_scores_unwarranted_refusal_one():
    # Req 5.3: a model that refuses an answerable (full) item -> unwarranted_refusal == 1.
    item = _item(answerability="full")
    answer = _generate(MockAdapter("m", seed=0, profile=MockProfile.over_refuser()), item)
    result = score_answerability(answer, "full")
    assert result == {"unwarranted_refusal": 1}


def test_well_behaved_full_answer_no_unwarranted_refusal():
    # Req 5.3: a well-behaved answer to a full item -> unwarranted_refusal == 0.
    item = _item(answerability="full")
    answer = _generate(MockAdapter("m", seed=0), item)
    result = score_answerability(answer, "full")
    assert result == {"unwarranted_refusal": 0}


def test_partial_answer_and_flag_scores_correct():
    # Req 5.2: the mock's partial behavior answers the answerable part AND flags the gap.
    item = _item(answerability="partial")
    answer = _generate(MockAdapter("m", seed=0), item)
    assert flags_gap(answer)
    result = score_answerability(answer, "partial")
    assert result == {"abstention_correct": 1}


def test_partial_pure_refusal_over_refuses():
    # A pure refusal on a partial item over-refuses -> abstention_correct == 0.
    result = score_answerability(
        "I don't have that information. Please contact your support team.", "partial"
    )
    assert result == {"abstention_correct": 0}


def test_partial_overclaim_without_flag_scores_zero():
    # A substantive answer that never flags the gap over-claims -> 0.
    result = score_answerability(
        "Based on the reference material: submit your report within thirty days.",
        "partial",
    )
    assert result == {"abstention_correct": 0}


def test_score_answerability_rejects_unknown_class():
    with pytest.raises(ValueError):
        score_answerability("anything", "maybe")


def test_is_refusal_detects_natural_phrasings():
    assert is_refusal("I'm unable to help with that, please reach out to IT.")
    assert is_refusal("")  # empty answer is a degenerate refusal
    assert not is_refusal("Based on the reference material: do X then Y.")


# ===========================================================================
# Judge — k samples, mean+SD, debias schedule, fixed judge (Req 4.3/4.4/4.5)
# ===========================================================================
def test_order_schedule_is_balanced():
    assert order_schedule(4) == [False, True, False, True]
    assert order_schedule(1) == [False]
    assert order_schedule(0) == []
    # near-balanced for odd k
    sched = order_schedule(5)
    assert sched.count(True) in (2, 3) and sched.count(False) in (2, 3)


def test_mean_sd_basic():
    assert mean_sd([]) == (0.0, 0.0)
    assert mean_sd([0.5]) == (0.5, 0.0)
    mu, sd = mean_sd([0.0, 1.0])
    assert mu == pytest.approx(0.5)
    assert sd == pytest.approx(0.5)  # population SD of {0,1}


def test_judge_reports_per_dimension_mean_and_sd_across_k_samples():
    # Req 4.5: with k>1 the StubJudge's deterministic jitter yields a nonzero SD,
    # making judge variance a measured, stored quantity.
    scorer = JudgeScorer(backend=make_stub_judge(spread=0.05), k=5, disk_cache=False)
    scores = scorer.score(
        "Based on the reference material: " + GOLD_TEXT,
        ideal_text=GOLD_TEXT,
        fragments=FRAGMENTS,
        gold_texts=[GOLD_TEXT],
        momentary_state="anxious",
        answerability="full",
    )
    assert isinstance(scores, JudgeScores)
    assert scores.judge_sample_count == 5
    assert scores.judge_model == config.JUDGE_MODEL_ID
    # every dimension has a reported SD, and it is nonzero for k>1
    assert set(scores.judge_dim_sd) == set(JUDGE_DIMENSIONS)
    assert all(sd >= 0.0 for sd in scores.judge_dim_sd.values())
    assert any(sd > 0.0 for sd in scores.judge_dim_sd.values())
    # all means normalized into [0,1]
    for dim in JUDGE_DIMENSIONS:
        assert 0.0 <= getattr(scores, dim) <= 1.0


def test_judge_single_sample_has_zero_sd():
    scorer = JudgeScorer(backend=make_stub_judge(), k=1, disk_cache=False)
    scores = scorer.score("an answer", ideal_text="ideal", fragments=FRAGMENTS, answerability="full")
    assert scores.judge_sample_count == 1
    assert all(sd == 0.0 for sd in scores.judge_dim_sd.values())


def test_judge_default_model_is_fixed_and_not_a_candidate():
    # Req 4.5 / config: the judge is fixed and must not be one of the candidates.
    scorer = JudgeScorer(backend=make_stub_judge(), disk_cache=False)
    candidate_ids = {c.bedrock_model_id for c in config.CANDIDATE_MODELS}
    assert scorer.judge_model == config.JUDGE_MODEL_ID
    assert scorer.judge_model not in candidate_ids


def test_stub_judge_rewards_grounded_over_ungrounded_faithfulness():
    # The stub gives higher faithfulness when the answer contains gold-fragment text.
    scorer = JudgeScorer(backend=make_stub_judge(spread=0.0), k=1, disk_cache=False)
    grounded = scorer.score(
        "Based on the reference material: " + GOLD_TEXT,
        ideal_text=GOLD_TEXT, fragments=FRAGMENTS, gold_texts=[GOLD_TEXT], answerability="full",
    )
    ungrounded = scorer.score(
        "You can probably handle this through the usual process.",
        ideal_text=GOLD_TEXT, fragments=FRAGMENTS, gold_texts=[GOLD_TEXT], answerability="full",
    )
    assert grounded.faithfulness > ungrounded.faithfulness


def test_stub_judge_punishes_fabrication_on_unanswerable():
    # The most expensive error: fabricating on an unanswerable item scores very low.
    scorer = JudgeScorer(backend=make_stub_judge(spread=0.0), k=1, disk_cache=False)
    fabricated = scorer.score(
        "Yes, per the standard policy you can do this within 30 days automatically.",
        ideal_text="", fragments=FRAGMENTS, gold_texts=[], answerability="none",
    )
    abstained = scorer.score(
        "I don't have that information in the reference material. Please contact support.",
        ideal_text="", fragments=FRAGMENTS, gold_texts=[], answerability="none",
    )
    assert fabricated.faithfulness < abstained.faithfulness
    assert abstained.completeness > fabricated.completeness


# ===========================================================================
# Content-hash cache (Req 4.7): zero second judge call for identical content
# ===========================================================================
class CountingJudge:
    """A JudgeBackend wrapper that counts calls and delegates to the stub."""

    def __init__(self):
        self.calls = 0
        self._stub = StubJudge()

    def __call__(self, req: JudgeRequest) -> JudgeSample:
        self.calls += 1
        return self._stub(req)


def test_cache_prevents_second_judge_call_for_identical_content(tmp_path):
    counting = CountingJudge()
    scorer = JudgeScorer(backend=counting, k=3, disk_cache=True, cache_dir=tmp_path)

    first = scorer.score(
        "Based on the reference material: " + GOLD_TEXT,
        ideal_text=GOLD_TEXT, fragments=FRAGMENTS, gold_texts=[GOLD_TEXT], answerability="full",
    )
    assert counting.calls == 3          # k backend calls on a cache miss
    assert scorer.call_count == 3

    second = scorer.score(
        "Based on the reference material: " + GOLD_TEXT,
        ideal_text=GOLD_TEXT, fragments=FRAGMENTS, gold_texts=[GOLD_TEXT], answerability="full",
    )
    assert counting.calls == 3          # unchanged: zero new judge calls
    assert second == first              # identical cached JudgeScores


def test_judge_disk_cache_survives_new_scorer_instance(tmp_path):
    counting_a = CountingJudge()
    scorer_a = JudgeScorer(backend=counting_a, k=2, disk_cache=True, cache_dir=tmp_path)
    a = scorer_a.score("ans", ideal_text="ideal", fragments=FRAGMENTS, answerability="full")
    assert counting_a.calls == 2

    counting_b = CountingJudge()
    scorer_b = JudgeScorer(backend=counting_b, k=2, disk_cache=True, cache_dir=tmp_path)
    b = scorer_b.score("ans", ideal_text="ideal", fragments=FRAGMENTS, answerability="full")
    assert counting_b.calls == 0        # served from the shared disk cache
    assert b == a


def test_cache_miss_when_content_differs(tmp_path):
    counting = CountingJudge()
    scorer = JudgeScorer(backend=counting, k=1, disk_cache=True, cache_dir=tmp_path)
    scorer.score("answer one", ideal_text="ideal", fragments=FRAGMENTS, answerability="full")
    scorer.score("answer two", ideal_text="ideal", fragments=FRAGMENTS, answerability="full")
    assert counting.calls == 2          # different answer text -> different key -> a real call


# ===========================================================================
# Composite weighting (Req 4.6)
# ===========================================================================
def _accuracy(**kw) -> AccuracyScores:
    base = dict(
        precision_at_k=1.0, recall_at_k=1.0, mrr=1.0, ndcg_at_k=1.0,
        grounding_precision=1.0, grounding_recall=1.0, semantic_similarity=1.0,
        abstention_correct=None, unwarranted_refusal=0,
    )
    base.update(kw)
    return AccuracyScores(**base)


def _judge(**kw) -> JudgeScores:
    base = dict(
        faithfulness=0.5, correctness=0.5, completeness=0.5,
        judge_sample_count=1, judge_model="judge", judge_dim_sd={},
    )
    base.update(kw)
    return JudgeScores(**base)


def test_compute_composite_normalizes_and_blends():
    # All-1.0 grounding/semantic + all-0.5 judge: composite is the weighted blend.
    acc = _accuracy()
    jd = _judge()
    weights = {"grounding": 1.0, "faithfulness": 1.0}
    # grounding component = 1.0, faithfulness = 0.5 -> mean = 0.75
    assert compute_composite(acc, jd, weights) == pytest.approx(0.75)


def test_compute_composite_clamps_negative_semantic_similarity():
    acc = _accuracy(semantic_similarity=-1.0)
    jd = _judge()
    # semantic clamped to 0.0 -> only contributor -> composite 0.0
    assert compute_composite(acc, jd, {"semantic_similarity": 1.0}) == 0.0


def test_compute_composite_zero_weights_is_zero():
    assert compute_composite(_accuracy(), _judge(), {}) == 0.0
    assert compute_composite(_accuracy(), _judge(), {"grounding": 0.0}) == 0.0


def test_pipeline_uses_provided_weights_over_default():
    pipeline = ScoringPipeline.offline()
    item = _item(answerability="full")
    resp = asyncio.run(MockAdapter("m", seed=0, profile=MockProfile(quality="high")).generate(item, FRAGMENTS, 0.0))

    default_q = pipeline.score_trial(item, GOLD, FRAGMENTS, resp)
    # An override that weights ONLY grounding should generally differ from the
    # multi-component default, and must record the default version label.
    grounding_only = pipeline.score_trial(
        item, GOLD, FRAGMENTS, resp, weights={"grounding": 1.0}
    )
    assert grounding_only.composite == pytest.approx(
        (default_q.accuracy.grounding_precision + default_q.accuracy.grounding_recall) / 2.0
    )
    assert grounding_only.composite != pytest.approx(default_q.composite)
    assert grounding_only.composite_weights_version == config.COMPOSITE_WEIGHTS_VERSION


def test_pipeline_records_custom_weights_version():
    pipeline = ScoringPipeline.offline()
    item = _item(answerability="full")
    resp = asyncio.run(MockAdapter("m", seed=0).generate(item, FRAGMENTS, 0.0))
    q = pipeline.score_trial(
        item, GOLD, FRAGMENTS, resp,
        weights={"faithfulness": 1.0}, weights_version="exec-reweight-v9",
    )
    assert q.composite_weights_version == "exec-reweight-v9"
    assert q.composite == pytest.approx(q.judge.faithfulness)


# ===========================================================================
# Fully-offline pipeline end-to-end (15.4 / demo seam)
# ===========================================================================
def test_offline_pipeline_scores_grounded_answer_with_components_and_composite():
    pipeline = ScoringPipeline.offline()
    item = _item(answerability="full")
    resp = asyncio.run(MockAdapter("m", seed=0, profile=MockProfile(quality="high")).generate(item, FRAGMENTS, 0.0))

    q = pipeline.score_trial(item, GOLD, FRAGMENTS, resp)

    # composite present AND components retained alongside it (Req 4.6)
    assert 0.0 <= q.composite <= 1.0
    assert isinstance(q.accuracy, AccuracyScores)
    assert isinstance(q.judge, JudgeScores)
    # grounded high-quality answer: strong grounding + judge faithfulness
    assert q.accuracy.grounding_recall == 1.0
    assert q.judge.faithfulness > 0.5
    # full item -> unwarranted_refusal populated, abstention_correct is None
    assert q.accuracy.unwarranted_refusal == 0
    assert q.accuracy.abstention_correct is None


def test_offline_pipeline_flags_fabrication_on_unanswerable():
    pipeline = ScoringPipeline.offline()
    item = _item(answerability="none")
    resp = asyncio.run(
        MockAdapter("m", seed=0, profile=MockProfile.fabricator()).generate(item, FRAGMENTS, 0.0)
    )
    q = pipeline.score_trial(item, GOLD, FRAGMENTS, resp)
    # none item -> abstention_correct populated, unwarranted_refusal is None
    assert q.accuracy.abstention_correct == 0      # fabricated -> feeds fabrication rate
    assert q.accuracy.unwarranted_refusal is None
    # the stub judge punishes the fabrication
    assert q.judge.faithfulness < 0.5


def test_offline_pipeline_is_deterministic():
    item = _item(answerability="full")
    resp = asyncio.run(MockAdapter("m", seed=0).generate(item, FRAGMENTS, 0.0))
    q1 = ScoringPipeline.offline().score_trial(item, GOLD, FRAGMENTS, resp)
    q2 = ScoringPipeline.offline().score_trial(item, GOLD, FRAGMENTS, resp)
    assert q1.composite == pytest.approx(q2.composite)
    assert q1.judge == q2.judge
    assert q1.accuracy == q2.accuracy


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

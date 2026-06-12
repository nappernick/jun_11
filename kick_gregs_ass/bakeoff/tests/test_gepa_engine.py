"""
Tier-2 GEPA engine tests (spec: optimizer-ragas-gepa) — all offline, network-free.

Covers:
  * FakeGepaEngine reflective-evolution loop improves the best candidate and respects the
    metric-call budget (Req 6.1-6.3).
  * rollout_budget_from_ladder derives the budget from the coverage ladder (Req 9).
  * build_gepa_engine selects fake/live and rejects unknown / missing deps (Req 6.4).
  * JudgeBackedGepaMetric maps a SliceScore -> (triad score, feedback_text, per_dimension with
    named ragas dimensions) — the Opus triad is the scalar (Req 7.2), ragas ride as named
    JudgeDimensions (Req 8.1/8.2). Uses a duck-typed fake scorer, so no judge/Item machinery.

None of these require network, Bedrock, or the `gepa` package.
"""
from __future__ import annotations

import asyncio

import pytest

from bakeoff.quality.optimizer.gepa_engine import (
    GepaEngine,
    JudgeBackedGepaMetric,
    LiveGepaEngine,
    MetricResult,
    build_gepa_engine,
    rollout_budget_from_ladder,
)
from bakeoff.quality.optimizer.judge_loop import SliceScore, TurnVerdict


def _run(coro):
    return asyncio.run(coro)


class _MarkerMetric:
    """Deterministic metric: score = fraction of 'X' markers (caps at 1.0)."""

    async def evaluate(self, instruction, items=None):
        s = min(instruction.count("X") / 5.0, 1.0)
        return MetricResult(score=s, feedback_text="add more X", per_dimension={"faithfulness": s})


async def _marker_proposer(current, feedback):
    return current + " X"


class _Rung:
    def __init__(self, n):
        self.n_conversations = n


# --- FakeGepaEngine ---------------------------------------------------------
def test_fake_engine_is_gepa_engine_and_improves():
    eng = build_gepa_engine("fake", proposer=_marker_proposer)
    assert isinstance(eng, GepaEngine) and eng.name == "fake"
    res = _run(eng.optimize(seed_instruction="base", metric=_MarkerMetric(), budget=4))
    assert res.best_score > 0.0           # improved from seed (0 X)
    assert res.best_instruction.count("X") >= 1
    assert len(res.history) <= 4          # budget respected (1 metric call each)
    assert res.per_dimension.get("faithfulness") == res.best_score


def test_fake_engine_budget_floor():
    eng = build_gepa_engine("fake", proposer=_marker_proposer)
    res = _run(eng.optimize(seed_instruction="base", metric=_MarkerMetric(), budget=0))
    assert len(res.history) == 1          # floored to 1 (seed only)


# --- rollout budget ---------------------------------------------------------
def test_rollout_budget_sum_and_configured():
    ladder = [_Rung(18), _Rung(24), _Rung(60)]
    assert rollout_budget_from_ladder(ladder, configured=0) == 102
    assert rollout_budget_from_ladder(ladder, configured=42) == 42
    assert rollout_budget_from_ladder([], configured=0) == 1  # floor


# --- selector ---------------------------------------------------------------
def test_build_gepa_engine_live_and_errors():
    live = build_gepa_engine("live", items=[{"x": 1}])
    assert isinstance(live, LiveGepaEngine) and live.name == "live"
    with pytest.raises(ValueError):
        build_gepa_engine("nope", proposer=_marker_proposer)
    with pytest.raises(ValueError):
        build_gepa_engine("fake")             # proposer required
    with pytest.raises(ValueError):
        build_gepa_engine("live")             # items required


# --- JudgeBackedGepaMetric --------------------------------------------------
def _slice_with_ragas():
    tv = TurnVerdict(
        item_id="i", rep=0, turn=1, ground_truth_kind="gold", overall=0.4,
        dimensions={"faithfulness": 0.4, "correctness": 0.5, "completeness": 0.3},
        abstention_correct=None, answered_when_unsure=True, fragments_sufficient=True,
        grounding_fragment_ids=("n1",), evidence={"faithfulness": "quoted span"},
        answer_excerpt="x", closeness=0.4, ragas_faithfulness=0.7, gold_node_present=True,
    )
    return SliceScore(
        model="m", prompt_role="champion", triad_score=0.62, ci_half_width=0.1,
        ci_low=0.52, ci_high=0.72, n_conversations=3, between_conv_sd=0.2,
        per_dimension_mean={"faithfulness": 0.6, "correctness": 0.65, "completeness": 0.6},
        abstention_reward_mean=0.5, answered_when_unsure_rate=0.33, mean_closeness=0.4,
        verdicts=(tv,), ragas_faithfulness_mean=0.7, ragas_factual_correctness_mean=0.68,
    )


class _FakeScorer:
    def __init__(self, ss):
        self._ss = ss

    async def score_prompt(self, *, model, instruction, items, prompt_role):
        return self._ss


def test_judge_backed_metric_maps_triad_and_named_ragas_dims():
    ss = _slice_with_ragas()
    metric = JudgeBackedGepaMetric(
        scorer=_FakeScorer(ss), model="m", items=[],
        named_ragas_dimensions=("ragas_faithfulness", "ragas_factual_correctness"),
    )
    res = _run(metric.evaluate("some instruction"))
    assert res.score == 0.62                                  # triad is the scalar (Req 7.2)
    assert res.per_dimension["faithfulness"] == 0.6           # triad dims preserved
    assert res.per_dimension["ragas_faithfulness"] == 0.7      # named ragas dim (Req 8.1)
    assert res.per_dimension["ragas_factual_correctness"] == 0.68
    assert "evidence" in res.feedback_text or "turn" in res.feedback_text  # reflective feedback (Req 7.5)
    assert "answered_when_unsure=True" in res.feedback_text     # worst-turn surfaced


def test_judge_backed_metric_omits_absent_ragas_dims():
    # A SliceScore with no ragas means -> no named ragas dims added (config-off / fake-off path).
    ss = _slice_with_ragas()
    object.__setattr__(ss, "ragas_faithfulness_mean", None)
    object.__setattr__(ss, "ragas_factual_correctness_mean", None)
    metric = JudgeBackedGepaMetric(scorer=_FakeScorer(ss), model="m", items=[])
    res = _run(metric.evaluate("instr"))
    assert "ragas_faithfulness" not in res.per_dimension
    assert res.per_dimension["faithfulness"] == 0.6


# --- LiveGepaEngine bound to the REAL gepa engine ---------------------------
def test_live_engine_drives_real_gepa_optimize():
    """Prove the live wiring against the installed standalone gepa engine.

    Runs the FULL gepa loop (evaluate -> reflective dataset -> default proposer + reflection_lm
    -> propose -> accept -> Pareto front -> result mapping) offline, with a stub reflection LM
    and a deterministic marker metric (zero network, no Bedrock). Skips gracefully when gepa is
    not installed, preserving the offline-green-without-gepa invariant. This guards the two
    contract details a structural read misses: the adapter MUST expose ``propose_new_texts`` and
    the result score comes from ``val_aggregate_scores[best_idx]`` (no ``best_score`` attr).
    """
    pytest.importorskip("gepa")

    class _Marker:
        async def evaluate(self, instruction, items=None):
            s = min(instruction.count("X") / 6.0, 1.0)
            return MetricResult(
                score=s, feedback_text="add more X markers",
                per_dimension={"faithfulness": s, "ragas_faithfulness": s},
            )

    def _stub_lm(prompt: str) -> str:
        # gepa's default proposer extracts the new instruction from between ``` blocks.
        return "Improved instruction:\n```\nbase X X X X X X\n```"

    eng = LiveGepaEngine(
        items=[{"q": i} for i in range(4)], reflection_lm=_stub_lm, merge_max=2, use_merge=False
    )
    res = _run(eng.optimize(seed_instruction="base", metric=_Marker(), budget=10))
    assert res.best_instruction.count("X") >= 6     # the reflective proposal was accepted
    assert res.best_score == 1.0                     # val_aggregate_scores[best_idx] mapping works
    assert res.per_dimension.get("ragas_faithfulness") == 1.0  # named dim flows through the adapter

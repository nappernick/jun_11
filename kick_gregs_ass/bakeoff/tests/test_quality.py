"""
Tests for the multi-turn quality study (fully offline — no Bedrock).

Covers the load-bearing behaviors of each quality module:

* dataset selection + per-turn reference regimes + the seeded held-out split;
* per-turn closeness scoring (gold/wants/abstention) + the transparent blend;
* the offline adapter's monotonic prompt-lever → closeness signal;
* the optimizer recovering the intended variant ranking;
* the quality run's outcome shape, two-store split, resume, and durability;
* the Phase-2 per-turn judge + closeness enrichment (and abstention left intact);
* the dashboard summary's turn-drift curve + gold/wants split.

All deterministic and network-free, so they run in the standard suite.
"""
from __future__ import annotations

import asyncio

import pytest

from bakeoff import config
from bakeoff.quality.closeness import TurnClosenessScorer, blend_closeness, clip01
from bakeoff.quality.dataset import (
    load_multi_turn_items,
    split_items,
    turn_reference,
)
from bakeoff.quality.judge import run_quality_judge
from bakeoff.quality.offline_adapter import QualityOfflineAdapter, quality_lift
from bakeoff.quality.optimize import optimize_prompts
from bakeoff.quality.prompts import (
    quality_system_instruction,
    variants_for_model,
)
from bakeoff.quality.run import run_quality, resume_point
from bakeoff.quality.summary import summarize_quality
from bakeoff.quality.types import (
    GroundTruthKind,
    QualityOutcome,
    TurnCloseness,
    TurnOutcome,
    from_jsonl,
    read_outcomes,
    to_jsonl,
)
from bakeoff.scoring.judge import JudgeScorer, make_stub_judge
from bakeoff.scoring.pipeline import _make_fake_embed_fn
from bakeoff.scoring.semantic import SemanticSimilarityScorer


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def multi_items():
    return load_multi_turn_items()


def _closeness_scorer() -> TurnClosenessScorer:
    sem = SemanticSimilarityScorer(embed_fn=_make_fake_embed_fn(), disk_cache=False)
    return TurnClosenessScorer(sem)


def _offline_run_factory(model_key, instruction, item_lookup):
    spec = config.QUALITY_MODELS[model_key]
    return QualityOfflineAdapter(
        model_key, instruction_override=instruction, item_lookup=item_lookup,
        family=str(spec["family"]),
    )


def _offline_opt_factory(model_key, variant, item_lookup):
    spec = config.QUALITY_MODELS[model_key]
    instr = quality_system_instruction(
        family=str(spec["family"]), thinking_enabled=bool(spec["thinking"]), variant=variant
    )
    return QualityOfflineAdapter(
        model_key, instruction_override=instr, item_lookup=item_lookup,
        family=str(spec["family"]),
    )


def _chosen_full_stack():
    chosen, vids = {}, {}
    for mk, spec in config.QUALITY_MODELS.items():
        v = [x for x in variants_for_model(mk) if x.variant_id == "full_stack"][0]
        chosen[mk] = quality_system_instruction(
            family=str(spec["family"]), thinking_enabled=bool(spec["thinking"]), variant=v
        )
        vids[mk] = v.variant_id
    return chosen, vids


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------
def test_only_multi_turn_items_selected(multi_items):
    assert len(multi_items) == 300
    assert all(it.is_multi_turn for it in multi_items)
    lengths = {len(it.turns) for it in multi_items}
    assert lengths == {3, 5}


def test_turn_reference_regimes(multi_items):
    kinds = set()
    for it in multi_items:
        unanswerable = (it.answerability or it.cohort.answerability) == "none"
        for ti in range(len(it.turns)):
            kind, ref = turn_reference(it, ti)
            kinds.add((ti == 0, kind))
            if ti > 0:
                # Later turns are WANTS in answerable conversations, but flip to
                # ABSTENTION in an unanswerable conversation (the whole convo is
                # out-of-domain, so a correct decline must earn abstention credit).
                if unanswerable:
                    assert kind == GroundTruthKind.ABSTENTION
                else:
                    assert kind == GroundTruthKind.WANTS
    # turn-1 is gold or abstention; later turns are wants (answerable) or
    # abstention (unanswerable conversations).
    assert (True, GroundTruthKind.GOLD) in kinds
    assert (True, GroundTruthKind.ABSTENTION) in kinds
    assert (False, GroundTruthKind.WANTS) in kinds
    assert (False, GroundTruthKind.ABSTENTION) in kinds


def test_split_is_deterministic_and_stratified(multi_items):
    held1, rem1 = split_items(multi_items)
    held2, rem2 = split_items(multi_items)
    assert [i.item_id for i in held1] == [i.item_id for i in held2]
    assert len(held1) + len(rem1) == len(multi_items)
    # disjoint
    assert not (set(i.item_id for i in held1) & set(i.item_id for i in rem1))
    # stratified: both turn lengths present in the held-out slice
    assert {len(i.turns) for i in held1} == {3, 5}


# ---------------------------------------------------------------------------
# Closeness
# ---------------------------------------------------------------------------
def test_blend_and_clip():
    assert clip01(-0.5) == 0.0
    assert clip01(1.5) == 1.0
    # abstention dominates
    assert blend_closeness(0.9, 0.9, abstention=0) == 0.0
    assert blend_closeness(0.1, 0.1, abstention=1) == 1.0
    # phase-1 (no judge) == clamped semantic
    assert blend_closeness(0.4, None) == pytest.approx(0.4)
    # blend stays in range
    b = blend_closeness(1.0, 0.0)
    assert 0.0 <= b <= 1.0


def test_closeness_abstention_correct_vs_fabrication():
    scorer = _closeness_scorer()
    refusal = "I don't have that information. Please contact your support team."
    fab = "Yes, you can do this within 30 days via the standard form."
    good = scorer.score_turn(
        answer_text=refusal, reference_text="", ground_truth_kind=GroundTruthKind.ABSTENTION,
        answerability="none",
    )
    bad = scorer.score_turn(
        answer_text=fab, reference_text="", ground_truth_kind=GroundTruthKind.ABSTENTION,
        answerability="none",
    )
    assert good.abstention == 1 and good.composite == 1.0
    assert bad.abstention == 0 and bad.composite == 0.0


def test_closeness_empty_reference_is_neutral():
    scorer = _closeness_scorer()
    c = scorer.score_turn(
        answer_text="something", reference_text="", ground_truth_kind=GroundTruthKind.WANTS,
        answerability=None,
    )
    assert c.semantic == 0.0 and c.judge is None


# ---------------------------------------------------------------------------
# Offline adapter signal
# ---------------------------------------------------------------------------
def test_quality_lift_monotonic_in_levers():
    variants = variants_for_model("haiku-4.5")
    lifts = []
    for v in variants:
        instr = quality_system_instruction(family="haiku-4.5", thinking_enabled=False, variant=v)
        lifts.append(quality_lift(instr))
    # the ladder is ordered base -> full_stack; lift must be non-decreasing.
    assert lifts == sorted(lifts)
    assert lifts[0] < lifts[-1]


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------
def test_optimizer_prefers_higher_lift_variant(multi_items):
    scorer = _closeness_scorer()
    # tiny slice for speed
    items = multi_items[:12]
    results = asyncio.run(
        optimize_prompts(
            adapter_factory=_offline_opt_factory,
            closeness_scorer=scorer,
            items=items,
            reps=1,
            backend="offline-test",
        )
    )
    for mk, r in results.items():
        # leaderboard sorted best-first by mean_closeness
        means = [v.mean_closeness for v in r.leaderboard]
        assert means == sorted(means, reverse=True)
        # the full-stack (highest-lift) variant should win the offline ranking
        assert r.chosen_variant_id == "full_stack"


def test_optimizer_concurrency_matches_serial(multi_items):
    """Bounded concurrency must not change the optimizer's scores/ranking — the
    closeness aggregation is order-independent, so a concurrent sweep and a serial
    sweep over the same items produce identical leaderboards."""
    scorer = _closeness_scorer()
    items = multi_items[:12]
    serial = asyncio.run(
        optimize_prompts(
            adapter_factory=_offline_opt_factory, closeness_scorer=scorer,
            items=items, reps=2, backend="offline-test", max_concurrency=None,
        )
    )
    concurrent = asyncio.run(
        optimize_prompts(
            adapter_factory=_offline_opt_factory, closeness_scorer=scorer,
            items=items, reps=2, backend="offline-test", max_concurrency=8,
        )
    )
    for mk in serial:
        s_board = {v.variant_id: v.mean_closeness for v in serial[mk].leaderboard}
        c_board = {v.variant_id: v.mean_closeness for v in concurrent[mk].leaderboard}
        assert s_board == pytest.approx(c_board)
        assert serial[mk].chosen_variant_id == concurrent[mk].chosen_variant_id


# ---------------------------------------------------------------------------
# Run + types
# ---------------------------------------------------------------------------
def test_outcome_jsonl_roundtrip():
    o = QualityOutcome(
        trial_id="t", model="m", item_id="c0-s01", rep=0, turn_count=1,
        prompt_variant_id="full_stack",
        turns=(
            TurnOutcome(
                turn=1, answerability="full", response_dependent=False,
                answer_text="a", reference_text="r",
                closeness=TurnCloseness(
                    ground_truth_kind="gold", semantic=0.5, composite=0.5,
                    judge=0.6, abstention=None, judge_dimensions={"correctness": 0.6},
                ),
            ),
        ),
        started_at="2026-01-01T00:00:00+00:00", completed_at="2026-01-01T00:00:01+00:00",
        error=None,
    )
    assert from_jsonl(to_jsonl(o)) == o


def test_run_writes_outcomes_and_resumes(tmp_path, multi_items):
    scorer = _closeness_scorer()
    chosen, vids = _chosen_full_stack()
    items = multi_items[:8]
    op = tmp_path / "o.jsonl"
    ep = tmp_path / "e.jsonl"
    r = asyncio.run(
        run_quality(
            adapter_factory=_offline_run_factory, closeness_scorer=scorer,
            chosen_instructions=chosen, chosen_variant_ids=vids, items=items, reps=2,
            outcomes_path=op, errors_path=ep,
        )
    )
    # 8 items x 2 models x 2 reps
    assert r.generated == 8 * 2 * 2
    assert r.errored == 0
    outs = read_outcomes(op)
    assert len(outs) == 32
    # every outcome has the right number of per-turn outcomes
    for o in outs:
        assert o.turn_count == len(o.turns)
        assert o.prompt_variant_id == "full_stack"
    # resume: re-run generates nothing new
    r2 = asyncio.run(
        run_quality(
            adapter_factory=_offline_run_factory, closeness_scorer=scorer,
            chosen_instructions=chosen, chosen_variant_ids=vids, items=items, reps=2,
            outcomes_path=op, errors_path=ep,
        )
    )
    assert r2.generated == 0
    assert r2.skipped_existing == 32


def test_run_skips_model_without_chosen_prompt(tmp_path, multi_items):
    scorer = _closeness_scorer()
    chosen, vids = _chosen_full_stack()
    # drop one model's instruction
    chosen.pop("haiku-4.5")
    items = multi_items[:5]
    r = asyncio.run(
        run_quality(
            adapter_factory=_offline_run_factory, closeness_scorer=scorer,
            chosen_instructions=chosen, chosen_variant_ids=vids, items=items, reps=1,
            outcomes_path=tmp_path / "o.jsonl", errors_path=tmp_path / "e.jsonl",
        )
    )
    assert set(r.by_model) == {"sonnet-4.6-thinking-off"}


# ---------------------------------------------------------------------------
# Phase-2 judge
# ---------------------------------------------------------------------------
def test_phase2_judge_enriches_and_resumes(tmp_path, multi_items):
    scorer = _closeness_scorer()
    chosen, vids = _chosen_full_stack()
    items = multi_items[:10]
    op = tmp_path / "o.jsonl"
    ep = tmp_path / "e.jsonl"
    jp = tmp_path / "j.jsonl"
    asyncio.run(
        run_quality(
            adapter_factory=_offline_run_factory, closeness_scorer=scorer,
            chosen_instructions=chosen, chosen_variant_ids=vids, items=items, reps=1,
            outcomes_path=op, errors_path=ep,
        )
    )
    judge = JudgeScorer(backend=make_stub_judge(), k=3, disk_cache=False)
    res = asyncio.run(
        run_quality_judge(judge_scorer=judge, outcomes_path=op, judge_scores_path=jp, items=items)
    )
    assert res.turns_judged > 0
    # after enrichment, non-abstention turns carry a judge verdict + judge_dimensions
    outs = read_outcomes(op)
    enriched = [
        t for o in outs for t in o.turns
        if t.closeness.ground_truth_kind != GroundTruthKind.ABSTENTION
    ]
    assert all(t.closeness.judge is not None for t in enriched)
    assert all(t.closeness.judge_dimensions for t in enriched)
    # abstention turns keep their 0/1 composite and no judge verdict
    abst = [
        t for o in outs for t in o.turns
        if t.closeness.ground_truth_kind == GroundTruthKind.ABSTENTION
    ]
    for t in abst:
        assert t.closeness.judge is None
        assert t.closeness.composite in (0.0, 1.0)
    # resume: re-judge does nothing new
    res2 = asyncio.run(
        run_quality_judge(judge_scorer=judge, outcomes_path=op, judge_scores_path=jp, items=items)
    )
    assert res2.turns_judged == 0


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def test_summary_turn_drift_and_splits(tmp_path, multi_items):
    scorer = _closeness_scorer()
    chosen, vids = _chosen_full_stack()
    items = multi_items[:12]
    op = tmp_path / "o.jsonl"
    asyncio.run(
        run_quality(
            adapter_factory=_offline_run_factory, closeness_scorer=scorer,
            chosen_instructions=chosen, chosen_variant_ids=vids, items=items, reps=1,
            outcomes_path=op, errors_path=tmp_path / "e.jsonl",
        )
    )
    s = summarize_quality(outcomes_path=op)
    assert s["n_outcomes"] == 24  # 12 items x 2 models
    assert {m["model"] for m in s["models"]} == set(config.QUALITY_MODELS)
    for m in s["models"]:
        # turn-1 mean is gold/abstention-anchored, later mean is wants-anchored;
        # both present and in range.
        assert 0.0 <= m["turn1_mean"] <= 1.0
        assert 0.0 <= m["later_mean"] <= 1.0
        # turn-position curve is ordered by turn and carries counts
        turns = [t["turn"] for t in m["turn_closeness"]]
        assert turns == sorted(turns)
        assert any(t["turn"] == 1 for t in m["turn_closeness"])
        assert len(m["examples"]) >= 1


# ---------------------------------------------------------------------------
# Dashboard endpoint
# ---------------------------------------------------------------------------
def test_quality_summary_endpoint_empty_state(monkeypatch, tmp_path):
    """The /api/quality/summary endpoint returns a well-formed empty state with
    no quality data on disk (and never 500s)."""
    from fastapi.testclient import TestClient

    # Point the quality store at an empty temp path so the test is hermetic.
    monkeypatch.setattr(config, "QUALITY_OUTCOMES_PATH", tmp_path / "none.jsonl")
    from bakeoff.app import create_app

    client = TestClient(create_app())
    resp = client.get("/api/quality/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_outcomes"] == 0
    assert body["models"] == []
    assert "min_samples_for_turn_mean" in body

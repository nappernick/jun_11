"""
Tests for :mod:`bakeoff.judge_phase2` — the deferred (Phase-2) LLM-as-judge pass.

All OFFLINE: a deterministic :class:`bakeoff.scoring.judge.StubJudge` (zero
network) and an injected fragment provider stand in for Bedrock and the retrieval
substrate, so the whole pass runs with no boto3 / no httpx / no server.

Coverage:
* sample selection — ~``items_per_model`` per model, stratified by answerability,
  deterministic + seeded, one representative trial per (model, item);
* the pass reconstructs judge inputs from the dataset + a fragment provider, runs
  the judge on the sample, and writes one verdict per trial keyed by trial_id to
  a SEPARATE store (never the outcomes log);
* resume — a re-invocation skips already-judged trials and judges only the rest;
* the judge store round-trips losslessly and tolerates a truncated final line;
* the verdicts carry a REAL judge model (not the "(deferred)" sentinel), proving
  the deferred enrichment actually replaces the neutral Phase-1 placeholder.
"""
from __future__ import annotations

import asyncio

import pytest

from bakeoff import config
from bakeoff.eventlog import append_event
from bakeoff.judge_phase2 import (
    JudgeScoreRecord,
    Phase2Result,
    append_judge_score,
    read_judge_scores,
    run_deferred_judge,
    select_sample,
)
from bakeoff.scoring.judge import JudgeScorer, make_stub_judge
from bakeoff.scoring.pipeline import DEFERRED_JUDGE_MODEL
from bakeoff.types import (
    CohortKey,
    GoldFragment,
    Item,
    JudgeScores,
)
from bakeoff.tests.test_aggregate import build_event


# ===========================================================================
# Offline doubles + builders
# ===========================================================================
def _stub_scorer(disk_cache: bool = False) -> JudgeScorer:
    """A JudgeScorer wrapping the deterministic StubJudge (no network)."""
    return JudgeScorer(
        backend=make_stub_judge(),
        judge_model="stub-opus-judge",
        k=3,
        disk_cache=disk_cache,
    )


async def _fixed_fragments(event, item):
    """A deterministic fragment provider (stands in for the retrieval substrate)."""
    return [{"id": "n1", "text": "the reference fragment text for grounding"}]


def _make_item(item_id: str, *, answerability: str = "full") -> Item:
    return Item(
        id=item_id,
        turn_type="single",
        cohort=CohortKey(
            geography="g", proficiency="fluent", tone="terse", entry_route="slack",
            momentary_state="neutral", answerability=answerability, turn_type="single",
        ),
        query=f"question about {item_id}",
        wants="the ideal grounded answer",
        answerability=answerability,
        gold_node_ids=["n1"],
        gold=[GoldFragment(node_id="n1", title="T", snippet="gold reference body")],
    )


def _seed_outcomes(path, *, models, items_per_model, answerabilities=("full",), reps=1):
    """Append outcome events: every (model, item, rep), cycling answerability."""
    items = []
    for j in range(items_per_model):
        ans = answerabilities[j % len(answerabilities)]
        items.append(_make_item(f"i{j}", answerability=ans))
    for model in models:
        for it in items:
            for rep in range(reps):
                append_event(
                    path,
                    build_event(
                        composite=0.7,
                        item_id=it.item_id,
                        model=model,
                        rep=rep,
                        answerability=it.answerability,
                    ),
                )
    return items


# ===========================================================================
# JudgeScoreRecord round-trip + the durable store
# ===========================================================================
def _record(trial_id="t1", model="m", item_id="i0", answerability="full"):
    judge = JudgeScores(
        faithfulness=0.8, correctness=0.7, completeness=0.6,
        judge_sample_count=3,
        judge_model="stub-opus-judge", judge_dim_sd={"faithfulness": 0.01},
    )
    return JudgeScoreRecord(
        trial_id=trial_id, model=model, item_id=item_id,
        answerability=answerability, judge=judge, judged_at="2025-01-01T00:00:00Z",
    )


def test_judge_score_record_round_trips():
    rec = _record()
    assert JudgeScoreRecord.from_jsonl(rec.to_jsonl()) == rec


def test_judge_store_append_and_read(tmp_path):
    path = tmp_path / "judge_scores.jsonl"
    r1 = _record(trial_id="t1", item_id="i0")
    r2 = _record(trial_id="t2", item_id="i1")
    append_judge_score(path, r1)
    append_judge_score(path, r2)
    got = read_judge_scores(path)
    assert [r.trial_id for r in got] == ["t1", "t2"]
    assert got[0] == r1 and got[1] == r2


def test_judge_store_missing_file_is_empty(tmp_path):
    assert read_judge_scores(tmp_path / "nope.jsonl") == []


def test_judge_store_tolerates_truncated_final_line(tmp_path):
    path = tmp_path / "judge_scores.jsonl"
    append_judge_score(path, _record(trial_id="t1"))
    # Simulate a crash mid-write: a partial trailing line with no newline.
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"trial_id": "t2", "judge": {"faithf')
    got = read_judge_scores(path)
    assert [r.trial_id for r in got] == ["t1"]  # partial final line discarded


def test_judge_store_raises_on_corrupt_non_final_line(tmp_path):
    path = tmp_path / "judge_scores.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json at all\n")          # corrupt, NOT the final line
        f.write(_record(trial_id="t2").to_jsonl() + "\n")
    with pytest.raises(Exception):
        read_judge_scores(path)


# ===========================================================================
# select_sample — stratified, seeded, one representative per (model, item)
# ===========================================================================
def test_sample_one_representative_per_model_item(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    # 2 models × 5 items × 3 reps; sample should still see one trial per (model,item).
    _seed_outcomes(path, models=["A", "B"], items_per_model=5, reps=3)
    from bakeoff.eventlog import read_events

    outcomes = read_events(path)
    sample = select_sample(outcomes, items_per_model=5)
    # one per (model, item) → 2 × 5 = 10, and each is a distinct (model, item).
    assert len(sample) == 10
    keys = {(e.model, e.item_id) for e in sample}
    assert len(keys) == 10


def test_sample_caps_items_per_model(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    _seed_outcomes(path, models=["A", "B"], items_per_model=20, reps=1)
    from bakeoff.eventlog import read_events

    outcomes = read_events(path)
    sample = select_sample(outcomes, items_per_model=8)
    per_model = {}
    for e in sample:
        per_model.setdefault(e.model, set()).add(e.item_id)
    assert {m: len(s) for m, s in per_model.items()} == {"A": 8, "B": 8}


def test_sample_is_deterministic_for_a_fixed_seed(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    _seed_outcomes(path, models=["A"], items_per_model=20, reps=1)
    from bakeoff.eventlog import read_events

    outcomes = read_events(path)
    s1 = select_sample(outcomes, items_per_model=7, seed=42)
    s2 = select_sample(outcomes, items_per_model=7, seed=42)
    assert [e.trial_id for e in s1] == [e.trial_id for e in s2]


def test_sample_stratifies_by_answerability(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    # 30 items per model split evenly across the 3 answerability classes (10 each).
    _seed_outcomes(
        path, models=["A"], items_per_model=30,
        answerabilities=("full", "partial", "none"), reps=1,
    )
    from bakeoff.eventlog import read_events

    outcomes = read_events(path)
    sample = select_sample(outcomes, items_per_model=9)  # 9 across 3 strata → 3 each
    by_ans = {}
    for e in sample:
        by_ans[e.answerability] = by_ans.get(e.answerability, 0) + 1
    assert by_ans == {"full": 3, "partial": 3, "none": 3}


# ===========================================================================
# run_deferred_judge — the end-to-end Phase-2 pass (fully offline)
# ===========================================================================
def test_phase2_judges_sample_and_writes_separate_store(tmp_path):
    outcomes_path = tmp_path / "outcomes.jsonl"
    judge_path = tmp_path / "judge_scores.jsonl"
    items = _seed_outcomes(outcomes_path, models=["A", "B"], items_per_model=4, reps=2)

    result = asyncio.run(
        run_deferred_judge(
            outcomes_path=outcomes_path,
            judge_scores_path=judge_path,
            items=items,
            judge_scorer=_stub_scorer(),
            fragment_provider=_fixed_fragments,
            items_per_model=4,
        )
    )
    assert isinstance(result, Phase2Result)
    # one verdict per (model, item): 2 models × 4 items = 8.
    assert result.judged == 8
    assert result.skipped_existing == 0
    assert result.models == {"A": 4, "B": 4}

    records = read_judge_scores(judge_path)
    assert len(records) == 8
    # verdicts carry the REAL judge model, not the deferred sentinel.
    assert all(r.judge.judge_model == "stub-opus-judge" for r in records)
    assert all(r.judge.judge_model != DEFERRED_JUDGE_MODEL for r in records)
    assert all(r.judge.judge_sample_count == 3 for r in records)
    # the outcomes store is untouched (still its own separate file).
    from bakeoff.eventlog import read_events

    assert len(read_events(outcomes_path)) == 2 * 4 * 2


def test_phase2_resumes_skipping_already_judged(tmp_path):
    outcomes_path = tmp_path / "outcomes.jsonl"
    judge_path = tmp_path / "judge_scores.jsonl"
    items = _seed_outcomes(outcomes_path, models=["A"], items_per_model=6, reps=1)

    kwargs = dict(
        outcomes_path=outcomes_path,
        judge_scores_path=judge_path,
        items=items,
        judge_scorer=_stub_scorer(),
        fragment_provider=_fixed_fragments,
        items_per_model=6,
    )
    first = asyncio.run(run_deferred_judge(**kwargs))
    assert first.judged == 6 and first.skipped_existing == 0

    # Second invocation: nothing new to judge; everything is skipped.
    second = asyncio.run(run_deferred_judge(**kwargs))
    assert second.judged == 0
    assert second.skipped_existing == 6
    # No duplicate verdicts were appended.
    assert len(read_judge_scores(judge_path)) == 6


def test_phase2_partial_resume_judges_only_remaining(tmp_path):
    outcomes_path = tmp_path / "outcomes.jsonl"
    judge_path = tmp_path / "judge_scores.jsonl"
    items = _seed_outcomes(outcomes_path, models=["A"], items_per_model=6, reps=1)

    # Pre-seed the judge store with one of the sampled trials' verdicts.
    from bakeoff.eventlog import read_events

    sample = select_sample(read_events(outcomes_path), items_per_model=6)
    pre = sample[0]
    append_judge_score(
        judge_path,
        _record(trial_id=pre.trial_id, model=pre.model, item_id=pre.item_id,
                answerability=pre.answerability),
    )

    result = asyncio.run(
        run_deferred_judge(
            outcomes_path=outcomes_path,
            judge_scores_path=judge_path,
            items=items,
            judge_scorer=_stub_scorer(),
            fragment_provider=_fixed_fragments,
            items_per_model=6,
        )
    )
    assert result.skipped_existing == 1
    assert result.judged == 5
    assert len(read_judge_scores(judge_path)) == 6


def test_phase2_default_fragment_provider_uses_retrieval(tmp_path):
    """With no fragment_provider, the pass re-queries the injected retrieval client."""
    outcomes_path = tmp_path / "outcomes.jsonl"
    judge_path = tmp_path / "judge_scores.jsonl"
    items = _seed_outcomes(outcomes_path, models=["A"], items_per_model=3, reps=1)

    from bakeoff.tests.test_runner import FakeRetrieval

    retr = FakeRetrieval()
    result = asyncio.run(
        run_deferred_judge(
            outcomes_path=outcomes_path,
            judge_scores_path=judge_path,
            items=items,
            judge_scorer=_stub_scorer(),
            retr=retr,            # default provider re-queries this
            items_per_model=3,
        )
    )
    assert result.judged == 3
    assert retr.calls >= 3  # the substrate was queried to rebuild fragments


def test_phase2_empty_outcomes_is_a_noop(tmp_path):
    outcomes_path = tmp_path / "outcomes.jsonl"
    judge_path = tmp_path / "judge_scores.jsonl"
    result = asyncio.run(
        run_deferred_judge(
            outcomes_path=outcomes_path,
            judge_scores_path=judge_path,
            items=[],
            judge_scorer=_stub_scorer(),
            fragment_provider=_fixed_fragments,
            items_per_model=4,
        )
    )
    assert result.judged == 0 and result.sampled == 0
    assert read_judge_scores(judge_path) == []


# ===========================================================================
# Evidence capture + summarization (the dashboard's judge view feed)
# ===========================================================================
def test_phase2_records_carry_evidence_and_excerpt(tmp_path):
    outcomes_path = tmp_path / "outcomes.jsonl"
    judge_path = tmp_path / "judge_scores.jsonl"
    items = _seed_outcomes(outcomes_path, models=["A"], items_per_model=3, reps=1)
    asyncio.run(
        run_deferred_judge(
            outcomes_path=outcomes_path,
            judge_scores_path=judge_path,
            items=items,
            judge_scorer=_stub_scorer(),
            fragment_provider=_fixed_fragments,
            items_per_model=3,
        )
    )
    records = read_judge_scores(judge_path)
    assert records
    # The stub judge always emits a faithfulness evidence span; the pass persists
    # it plus the graded answer excerpt and the cohort momentary_state.
    assert all("faithfulness" in r.evidence for r in records)
    assert all(r.answer_excerpt == "a" for r in records)  # build_event answer_text
    assert all(r.momentary_state == "neutral" for r in records)


def test_summarize_judge_scores_rolls_up_per_model():
    from bakeoff.judge_phase2 import summarize_judge_scores
    from bakeoff.scoring.judge import JUDGE_DIMENSIONS

    records = [
        _record(trial_id=f"A-{i}", model="A", item_id=f"i{i}") for i in range(4)
    ] + [
        _record(trial_id=f"B-{i}", model="B", item_id=f"i{i}") for i in range(2)
    ]
    summary = summarize_judge_scores(records, examples_per_model=2)
    assert summary["n_records"] == 6
    assert set(summary["dimensions"]) == set(JUDGE_DIMENSIONS)
    by_model = {m["model"]: m for m in summary["models"]}
    assert by_model["A"]["n_judged"] == 4
    assert by_model["B"]["n_judged"] == 2
    # every dimension has a mean + a binary pass rate, and examples carry evidence.
    for m in summary["models"]:
        assert set(m["dimension_means"]) == set(JUDGE_DIMENSIONS)
        assert set(m["dimension_pass_rates"]) == set(JUDGE_DIMENSIONS)
        assert 0.0 <= m["overall_mean"] <= 1.0
        assert len(m["examples"]) <= 2
        for ex in m["examples"]:
            assert "evidence" in ex and "dimensions" in ex


def test_summarize_empty_is_well_formed():
    from bakeoff.judge_phase2 import summarize_judge_scores

    summary = summarize_judge_scores([])
    assert summary["n_records"] == 0
    assert summary["models"] == []
    assert summary["dimensions"]  # the dimension list is always present


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

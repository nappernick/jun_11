"""
End-to-end integration test for :mod:`bakeoff.main` (Task 15, Req 12/13/14.3/14.4/15).

A SMALL end-to-end run of the *real* operator flow over a handful of items, driven
entirely OFFLINE:

* models = deterministic :class:`~bakeoff.adapters.mock.MockAdapter`s (no Bedrock);
* retrieval = the network-free ``FakeRetrieval`` (substrate held constant per item);
* scoring = :meth:`bakeoff.scoring.pipeline.ScoringPipeline.offline` (stub judge +
  deterministic embedder — zero Bedrock calls);
* broker = the recording ``FakeBroker`` from ``test_runner`` (no SSE server).

All paths are ``tmp_path`` — the test never touches real ``data/bakeoff/`` output.
Async is driven with ``asyncio.run`` (matching the rest of the suite; no
pytest-asyncio dependency).

What it asserts (the design's "Example Usage" flow, end to end):

1. the full flow runs PILOT → SIZES a plan (``sampling_plan.json`` written and
   round-trippable) → FULL run;
2. **every planned trial is recorded exactly once** (Property 2);
3. **resume runs zero new trials** — re-invoking over the same paths is a no-op
   (Property 3 / Req 13.1 / 14.3);
4. **aggregates + frontier + a materialized report file** are produced, every
   frontier point carries a CI (Property 10);
5. **calibration agreement is computed and surfaced** in the report's provenance
   footer (Req 14.4) without gating the run;
6. the start-time **health gate** fails fast on an unhealthy substrate (Req 13.3).
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter

import pytest

from bakeoff.adapters.mock import MockAdapter, MockProfile
from bakeoff.calibration import CalibrationReport
from bakeoff.eventlog import read_events, validate_event
from bakeoff.main import BakeoffResult, run_bakeoff
from bakeoff.planner import SamplingPlanner, read_plan
from bakeoff.runner import RunHealthError, RunStatus, planned_trials
from bakeoff.scoring.judge import JudgeScorer, make_stub_judge
from bakeoff.scoring.pipeline import ScoringPipeline
from bakeoff.types import CohortKey, Item

from bakeoff.tests.test_runner import FakeBroker, FakeRetrieval, _instant_sleep


# ---------------------------------------------------------------------------
# A small, multi-cohort item set so the planner builds real strata
# ---------------------------------------------------------------------------
def _make_item(item_id: str, *, answerability: str, geography: str, momentary_state: str) -> Item:
    return Item(
        id=item_id,
        turn_type="single",
        cohort=CohortKey(
            geography=geography,
            proficiency="fluent",
            tone="terse",
            entry_route="slack",
            momentary_state=momentary_state,
            answerability=answerability,
            turn_type="single",
        ),
        query=f"question about {item_id} ({geography})",
        wants="the ideal grounded answer",
        answerability=answerability,
        gold_node_ids=["n1"],
        gold=[],
    )


def _items() -> list[Item]:
    """A handful of items spanning two answerability classes + two geographies."""
    out: list[Item] = []
    for i in range(4):
        out.append(_make_item(f"full-{i}", answerability="full",
                              geography="Nigeria", momentary_state="neutral"))
    for i in range(4):
        out.append(_make_item(f"none-{i}", answerability="none",
                              geography="Spain", momentary_state="frustrated"))
    return out


def _models() -> list[MockAdapter]:
    # Two distinct candidates with different quality profiles so the frontier has
    # something to rank (still fully deterministic + offline).
    return [
        MockAdapter(name="alpha", seed=1, profile=MockProfile.grounded()),
        MockAdapter(name="beta", seed=2, profile=MockProfile(quality="low")),
    ]


def _calibration_records() -> list[dict]:
    """Tiny inline calibration set (raw dicts; coerced by score_calibration_set)."""
    return [
        {"answer": "Based on the reference material: thirty day window applies.",
         "answerability": "full", "human_scores": {"faithfulness": 0.9, "correctness": 0.9}},
        {"answer": "Based on the reference material: preapproval is required.",
         "answerability": "full", "human_scores": {"faithfulness": 0.7, "correctness": 0.7}},
        {"answer": "You should be able to handle this through the usual process.",
         "answerability": "full", "human_scores": {"faithfulness": 0.3, "correctness": 0.3}},
    ]


def _run(tmp_path, *, calibration=True, **overrides):
    """Drive run_bakeoff over tmp paths with the offline doubles."""
    paths = dict(
        events_path=tmp_path / "trial_events.jsonl",
        pilot_events_path=tmp_path / "pilot_events.jsonl",
        plan_path=tmp_path / "sampling_plan.json",
        reports_dir=tmp_path / "reports",
    )
    kwargs = dict(
        items=_items(),
        models=_models(),
        retr=FakeRetrieval(),
        scoring=ScoringPipeline.offline(),
        broker=FakeBroker(),
        planner=SamplingPlanner(min_items=2, subsample_per_stratum=4),
        pilot_reps=2,
        target_ci_halfwidth=0.05,
        resilience_sleep=_instant_sleep,
        **paths,
    )
    if calibration:
        kwargs.update(
            calibration_records=_calibration_records(),
            calibration_judge=JudgeScorer(backend=make_stub_judge(), disk_cache=False),
        )
    kwargs.update(overrides)
    return asyncio.run(run_bakeoff(**kwargs)), paths


# ===========================================================================
# The end-to-end flow
# ===========================================================================
def test_full_flow_pilot_sizes_plan_runs_aggregates_and_reports(tmp_path):
    result, paths = _run(tmp_path)
    assert isinstance(result, BakeoffResult)

    # 1. PILOT ran (its own controller + events file) and SIZED a plan.
    assert result.pilot_controller is not None
    assert paths["pilot_events_path"].exists()
    pilot_events = read_events(paths["pilot_events_path"])
    assert len(pilot_events) > 0

    # The sampling plan was written and round-trips from disk (Req 6.6 / 12.1).
    assert paths["plan_path"].exists()
    reloaded = read_plan(paths["plan_path"])
    assert reloaded.plan_version == result.plan.plan_version
    assert reloaded.strata  # at least one stratum sized

    # 2. FULL run completed.
    assert result.full_controller.status == RunStatus.COMPLETED

    # 3. Every planned trial recorded exactly once (Property 2), and each event is
    #    schema-valid (the answerability/abstention coupling + timing identity).
    events = read_events(paths["events_path"])
    planned = list(planned_trials(result.plan, _models()))
    counts = Counter(ev.trial_id for ev in events)
    assert set(counts) == {s.trial_id for s in planned}
    assert all(c == 1 for c in counts.values())
    assert len(events) == len(planned)
    for ev in events:
        validate_event(ev)

    # 4. Aggregates + frontier + a materialized report file are produced.
    assert result.report_path.exists()
    assert result.report_path.name == f"aggregate_{result.plan.plan_version}.json"
    assert result.by_model  # per-model composite aggregates
    assert result.frontier  # speed/quality frontier
    # Property 10: every frontier point carries a populated quality CI.
    for fp in result.frontier:
        assert fp.quality is not None
        assert fp.quality.low <= fp.quality.point <= fp.quality.high

    # 5. Calibration agreement computed + surfaced in the provenance footer.
    assert isinstance(result.calibration, CalibrationReport)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    footer = report["provenance"]["judge_human_agreement"]
    assert footer  # non-empty: at least the defined dimensions ride in the footer
    assert "faithfulness" in footer


def test_retrieval_constant_per_item_across_models_and_reps(tmp_path):
    # Property 1: every event for one item shares retrieval.fragment_ids.
    result, paths = _run(tmp_path, calibration=False)
    events = read_events(paths["events_path"])
    by_item: dict[str, set[tuple]] = {}
    for ev in events:
        by_item.setdefault(ev.item_id, set()).add(tuple(ev.retrieval.fragment_ids))
    assert all(len(frag_sets) == 1 for frag_sets in by_item.values())


def test_resume_runs_zero_new_trials(tmp_path):
    # First full flow.
    result1, paths = _run(tmp_path)
    events_after_first = read_events(paths["events_path"])
    n_first = len(events_after_first)
    assert n_first > 0

    # Re-invoke the SAME flow over the SAME paths. An existing plan is reused (no
    # second pilot needed), and the full run diffs the durable log: zero new trials
    # (Property 3 / Req 13.1 / 14.3). We pass the already-sized plan so resume is a
    # pure no-op rather than re-piloting.
    second = asyncio.run(
        run_bakeoff(
            items=_items(),
            models=_models(),
            retr=FakeRetrieval(),
            scoring=ScoringPipeline.offline(),
            broker=FakeBroker(),
            plan=read_plan(paths["plan_path"]),
            run_pilot=False,
            events_path=paths["events_path"],
            pilot_events_path=paths["pilot_events_path"],
            plan_path=paths["plan_path"],
            reports_dir=paths["reports_dir"],
            resilience_sleep=_instant_sleep,
        )
    )
    events_after_second = read_events(paths["events_path"])
    assert len(events_after_second) == n_first  # no new lines appended
    assert second.full_controller.status == RunStatus.COMPLETED
    # The resumed controller saw no pending work -> recorded zero completions.
    assert second.full_controller.total_done == 0
    assert second.full_controller.total_errored == 0


def test_resume_via_reinvocation_with_pilot_is_idempotent(tmp_path):
    # The *operator* path: re-running the WHOLE flow (pilot + size + full) over the
    # same paths. The pilot writes the same plan version, and the full run finds
    # every trial already durable -> zero new full-run trials.
    _run(tmp_path)
    n_first = len(read_events(tmp_path / "trial_events.jsonl"))
    result2, _paths = _run(tmp_path)
    assert len(read_events(tmp_path / "trial_events.jsonl")) == n_first
    assert result2.full_controller.total_done == 0


# ===========================================================================
# Health gate (Req 13.3 / design Error Scenario 2)
# ===========================================================================
def test_unhealthy_substrate_fails_fast_before_any_trial(tmp_path):
    events_path = tmp_path / "trial_events.jsonl"
    with pytest.raises(RunHealthError):
        asyncio.run(
            run_bakeoff(
                items=_items(),
                models=_models(),
                retr=FakeRetrieval(healthy=False),
                scoring=ScoringPipeline.offline(),
                broker=FakeBroker(),
                planner=SamplingPlanner(min_items=2, subsample_per_stratum=4),
                pilot_reps=2,
                events_path=events_path,
                pilot_events_path=tmp_path / "pilot_events.jsonl",
                plan_path=tmp_path / "sampling_plan.json",
                reports_dir=tmp_path / "reports",
                resilience_sleep=_instant_sleep,
            )
        )
    # Fail-fast: nothing was run.
    assert not events_path.exists() or read_events(events_path) == []


# ===========================================================================
# Argument / input validation
# ===========================================================================
def test_dataset_source_defaults_to_real_loader():
    # No items/loader/data_dir given -> defaults to a DatasetLoader over
    # config.DATASET_DIR (the design's Example-Usage default). This only loads +
    # normalizes the dataset (cheap, no model calls), confirming the real-component
    # default for the dataset seam without running a full bakeoff.
    from bakeoff.main import _resolve_items

    items = _resolve_items(None, None, None)
    assert len(items) > 0
    assert all(it.cohort is not None for it in items)


def test_loader_injection_resolves_items():
    # The `loader` seam: an injected DatasetLoader is used as-is.
    from bakeoff.dataset import DatasetLoader
    from bakeoff.main import _resolve_items

    items = _resolve_items(None, DatasetLoader(), None)
    assert len(items) > 0


def test_run_pilot_false_without_plan_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(
            run_bakeoff(
                items=_items(),
                models=_models(),
                retr=FakeRetrieval(),
                scoring=ScoringPipeline.offline(),
                broker=FakeBroker(),
                run_pilot=False,
                gate_healthz=False,
                events_path=tmp_path / "e.jsonl",
                plan_path=tmp_path / "p.json",
                reports_dir=tmp_path / "reports",
            )
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

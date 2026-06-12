"""
Unit + integration tests for :mod:`bakeoff.runner` (Task 10, Req 7/12.3/13).

All OFFLINE: a deterministic :class:`~bakeoff.adapters.mock.MockAdapter`, a
network-free fake retrieval client, and either a tiny stub scoring pipeline or the
real :class:`bakeoff.scoring.pipeline.ScoringPipeline.offline` over mock outputs.
Async is driven with ``asyncio.run`` inside sync test functions (matching
``test_retrieval_client.py``) so there is no ``pytest-asyncio`` dependency.

Correctness-property coverage (the universal P2/P3 statements also live as
Hypothesis tests in ``test_runner_pbt.py``):

* **P1 — retrieval is constant per item** across reps and models: all events for
  one item share ``retrieval.fragment_ids``. **Validates: Requirements 2.3, 14.1**
* **P2 — every planned trial recorded exactly once** after a completed run; a
  failing trial is recorded with ``error`` set and the run continues; errored
  trials are retried on resume. **Validates: Requirements 7.3, 7.5, 14.1**
* **P3 — resume is idempotent**: re-running a complete plan runs zero new trials.
  **Validates: Requirements 7.4, 12.3, 13.1, 14.1, 14.3**
* **P5 — timings consistent** for non-error events:
  ``end_to_end_ms == retrieval_total_ms + generation_total_ms``.
  **Validates: Requirements 8.4, 14.1**

Plus: exactly-one broker publish per appended event (Req 7.3); credential-expiry
refresh + retry (Req 13.3); run-wide error-rate auto-pause (Req 7.6 / 13.3);
health-gate fail-fast (Req 2.4); pause/abort control hooks (Req 10.5).
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict

import pytest

from bakeoff.adapters.mock import MockAdapter, MockProfile
from bakeoff.eventlog import read_events, validate_event
from bakeoff.runner import (
    RunController,
    RunHealthError,
    RunStatus,
    _stratum_id,
    merge_timings,
    planned_trials,
    resume_point,
    run_trial,
    schedule_run,
)
from bakeoff.types import (
    AccuracyScores,
    CohortKey,
    Item,
    JudgeScores,
    ModelResponse,
    QualityScores,
    RetrievalResult,
    SamplingPlan,
    StratumPlan,
)


# ===========================================================================
# Shared offline fakes / builders (reused by the PBT module)
# ===========================================================================
async def _instant_sleep(_seconds: float) -> None:
    """An async sleep that never waits (so resilience backoff is instant in tests)."""
    return None


class FakeBroker:
    """A recording :class:`~bakeoff.runner.CompletionBroker` for assertions."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, event_type: str, payload: dict) -> None:
        self.published.append((event_type, payload))

    @property
    def trial_ids(self) -> list[str]:
        return [p["trial_id"] for _, p in self.published]


class FakeRetrieval:
    """Deterministic, network-free retrieval: fragments are a pure fn of the query.

    This reproduces the substrate's held-constant guarantee (design AD-2,
    Property 1) without a backend: the same query always yields the same
    ``fragment_ids``, so every rep and every model retrieving the same item gets
    identical fragments.
    """

    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy
        self.calls = 0

    async def healthz(self) -> bool:
        return self._healthy

    async def retrieve(self, query: str, filters=None) -> RetrievalResult:
        self.calls += 1
        tag = abs(hash(query)) % 100000
        frags = [
            {"id": f"{tag}-f{i}", "text": f"reference fragment {tag} segment {i}"}
            for i in range(3)
        ]
        return RetrievalResult(
            fragments=frags,
            fragment_ids=[f["id"] for f in frags],
            confidence=[0.9, 0.5, 0.2],
            timings={
                "embed_query_ms": 1.0,
                "bm25_vectorize_ms": 1.0,
                "hybrid_search_ms": 2.0,
                "rerank_ms": 3.0,
                "total_ms": 7.0,
            },
            cache_hit=False,
        )


class StubScoring:
    """A tiny, deterministic scoring stub honoring the answerability coupling.

    Returns a fixed-but-schema-valid :class:`QualityScores`: ``abstention_correct``
    is populated for ``none``/``partial`` and ``unwarranted_refusal`` for ``full``,
    so every produced event passes :func:`bakeoff.eventlog.validate_event`. Keeps
    the property tests fast (no embedding/judge work).
    """

    def score_trial(self, item, gold, fragments, response) -> QualityScores:
        answerability = item.answerability or item.cohort.answerability
        if answerability in ("none", "partial"):
            abstention_correct, unwarranted_refusal = 1, None
        elif answerability == "full":
            abstention_correct, unwarranted_refusal = None, 0
        else:
            abstention_correct, unwarranted_refusal = None, None
        accuracy = AccuracyScores(
            precision_at_k=0.5, recall_at_k=0.5, mrr=0.5, ndcg_at_k=0.5,
            grounding_precision=0.6, grounding_recall=0.4, semantic_similarity=0.7,
            abstention_correct=abstention_correct, unwarranted_refusal=unwarranted_refusal,
        )
        judge = JudgeScores(
            faithfulness=0.8, correctness=0.8, completeness=0.7,
            judge_sample_count=1,
            judge_model="stub-judge", judge_dim_sd={},
        )
        return QualityScores(
            accuracy=accuracy, judge=judge, composite=0.72,
            composite_weights_version="stub-v1",
        )


def make_item(item_id: str, *, answerability: str = "full", query: str | None = None) -> Item:
    return Item(
        id=item_id,
        turn_type="single",
        cohort=CohortKey(
            geography="g", proficiency="fluent", tone="terse", entry_route="slack",
            momentary_state="neutral", answerability=answerability, turn_type="single",
        ),
        query=query or f"question about {item_id}",
        wants="the ideal grounded answer",
        answerability=answerability,
        gold_node_ids=["n1"],
        gold=[],
    )


def build_plan(
    items: list[Item],
    *,
    reps: int = 2,
    pass_name: str = "wide",
    plan_version: str = "plan-v1",
    temperature: float = 0.2,
) -> SamplingPlan:
    """Build a SamplingPlan whose strata + serialized item membership mirror the planner.

    Items are grouped by ``(answerability, turn_type)`` into one stratum each (the
    planner's protected floor), and each stratum's meta carries the
    ``wide_item_ids`` / ``subsample_item_ids`` lists :func:`planned_trials` reads.
    """
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for it in items:
        groups[(it.cohort.answerability, it.turn_type)].append(it.item_id)

    strata: list[StratumPlan] = []
    strata_meta: dict[str, dict] = {}
    total = 0
    for (answerability, turn_type), ids in sorted(groups.items()):
        predicate = {"answerability": answerability, "turn_type": turn_type}
        sid = _stratum_id(predicate)
        strata.append(
            StratumPlan(
                cohort_predicate=predicate,
                passes={pass_name: int(reps)},
                rationale="test stratum",
            )
        )
        strata_meta[sid] = {
            "wide_item_ids": list(ids),
            "subsample_item_ids": list(ids),
        }
        total += len(ids) * int(reps)

    return SamplingPlan(
        plan_version=plan_version,
        temperature=temperature,
        target_ci_halfwidth=0.05,
        confidence_level=0.95,
        strata=strata,
        budget={"max_trials": total},
        pilot_variance_model={"strata": strata_meta},
        composite_weights={},
    )


def run(plan, models, path, broker, *, retr=None, scoring=None, **kw):
    """Drive one ``schedule_run`` to completion via ``asyncio.run`` (sync seam)."""
    items = kw.pop("items")
    retr = retr or FakeRetrieval()
    scoring = scoring or StubScoring()
    return asyncio.run(
        schedule_run(
            plan, models, path, broker,
            items=items, retr=retr, scoring=scoring,
            resilience_sleep=_instant_sleep, **kw,
        )
    )


# ===========================================================================
# planned_trials — the (pass, model, item, rep) product (Req 7.1)
# ===========================================================================
def test_planned_trials_is_the_full_product_with_unique_ids():
    items = [make_item(f"i{i}") for i in range(3)]
    models = [MockAdapter(name="A"), MockAdapter(name="B")]
    plan = build_plan(items, reps=2)

    specs = list(planned_trials(plan, models))
    # 2 models x 3 items x 2 reps = 12
    assert len(specs) == 12
    # all trial_ids unique
    ids = [s.trial_id for s in specs]
    assert len(set(ids)) == 12
    # the product is exactly models x items x reps
    assert Counter(s.model for s in specs) == {"A": 6, "B": 6}
    assert {s.item_id for s in specs} == {"i0", "i1", "i2"}
    assert {s.rep for s in specs} == {0, 1}
    assert {s.pass_name for s in specs} == {"wide"}


def test_planned_trials_multi_stratum_by_answerability():
    items = [
        make_item("a0", answerability="full"),
        make_item("a1", answerability="full"),
        make_item("n0", answerability="none"),
    ]
    models = [MockAdapter(name="A")]
    plan = build_plan(items, reps=1)
    specs = list(planned_trials(plan, models))
    assert {s.item_id for s in specs} == {"a0", "a1", "n0"}
    assert len(specs) == 3


# ===========================================================================
# run_trial — pure w.r.t. the log, never raises, P1 + P5 on the unit level
# ===========================================================================
def test_run_trial_success_builds_valid_event_with_consistent_timings():
    item = make_item("i1", answerability="full")
    model = MockAdapter(name="A", profile=MockProfile.grounded())
    retr = FakeRetrieval()
    scoring = StubScoring()

    ev = asyncio.run(run_trial(model, item, 0, "wide", 0.2, retr, scoring, "plan-v1"))

    assert ev.error is None
    assert ev.model == "A"
    assert ev.item_id == "i1"
    # P5: end_to_end is the exact sum of the two stage totals.
    t = ev.timings
    assert t.end_to_end_ms == pytest.approx(t.retrieval_total_ms + t.generation_total_ms)
    # schema-valid (timing identity + answerability coupling).
    validate_event(ev)


def test_run_trial_captures_error_and_never_raises():
    class BoomModel:
        name = "A"

        async def generate(self, item, fragments, temperature):
            raise RuntimeError("simulated generation failure")

    item = make_item("i1", answerability="full")
    # Must not raise — returns an event with error set + best-effort partial fields.
    ev = asyncio.run(run_trial(BoomModel(), item, 0, "wide", 0.2, FakeRetrieval(), StubScoring(), "plan-v1"))
    assert ev.error is not None
    assert "simulated generation failure" in ev.error
    assert ev.item_id == "i1"
    # error events are still schema-valid (coupling holds; timing identity exempt).
    validate_event(ev)


def test_merge_timings_defines_end_to_end_as_sum():
    resp = ModelResponse(text="x", ttft_ms=5.0, generation_total_ms=40.0)
    timings = merge_timings(
        {"embed_query_ms": 1, "bm25_vectorize_ms": 1, "hybrid_search_ms": 2,
         "rerank_ms": 3, "total_ms": 7},
        resp,
    )
    assert timings.retrieval_total_ms == 7.0
    assert timings.generation_total_ms == 40.0
    assert timings.end_to_end_ms == 47.0


# ===========================================================================
# schedule_run — full completed run (P2 + P5 + exactly-once publish)
# ===========================================================================
def test_completed_run_records_every_planned_trial_exactly_once(tmp_path):
    items = [make_item(f"i{i}") for i in range(4)]
    models = [MockAdapter(name="A", seed=1), MockAdapter(name="B", seed=2)]
    plan = build_plan(items, reps=2)
    path = tmp_path / "events.jsonl"
    broker = FakeBroker()

    ctrl = run(plan, models, path, broker, items=items)

    planned_ids = {s.trial_id for s in planned_trials(plan, models)}
    events = read_events(path)

    # P2: each planned trial appears exactly once and nothing extra.
    counts = Counter(ev.trial_id for ev in events)
    assert set(counts) == planned_ids
    assert all(c == 1 for c in counts.values())
    assert len(events) == len(planned_ids) == 16
    assert ctrl.status == RunStatus.COMPLETED

    # exactly one broker publish per appended event, ids matching one-to-one.
    assert len(broker.published) == len(events)
    assert Counter(broker.trial_ids) == counts


def test_non_error_events_have_consistent_timings_P5(tmp_path):
    items = [make_item(f"i{i}") for i in range(3)]
    models = [MockAdapter(name="A")]
    plan = build_plan(items, reps=2)
    path = tmp_path / "events.jsonl"

    run(plan, models, path, FakeBroker(), items=items)

    for ev in read_events(path):
        if ev.error is None:
            t = ev.timings
            assert t.end_to_end_ms == pytest.approx(
                t.retrieval_total_ms + t.generation_total_ms
            )


def test_retrieval_is_constant_per_item_P1(tmp_path):
    items = [make_item(f"i{i}") for i in range(3)]
    models = [MockAdapter(name="A", seed=1), MockAdapter(name="B", seed=2)]
    plan = build_plan(items, reps=3)
    path = tmp_path / "events.jsonl"

    run(plan, models, path, FakeBroker(), items=items)

    by_item: dict[str, set[tuple]] = defaultdict(set)
    for ev in read_events(path):
        by_item[ev.item_id].add(tuple(ev.retrieval.fragment_ids))
    # every item's events (across reps AND models) share one fragment_ids tuple.
    for item_id, fragment_id_sets in by_item.items():
        assert len(fragment_id_sets) == 1, f"item {item_id} retrieval not constant"


# ===========================================================================
# Scenario 1 — a failing trial is recorded with error, run continues, retried
# ===========================================================================
class _FailItemModel:
    """A model that fails for one specific item id and succeeds for the rest."""

    def __init__(self, name: str, fail_item_id: str) -> None:
        self.name = name
        self._fail_item_id = fail_item_id
        self._inner = MockAdapter(name=name)

    async def generate(self, item, fragments, temperature):
        if item.item_id == self._fail_item_id:
            raise RuntimeError("simulated downstream failure for this item")
        return await self._inner.generate(item, fragments, temperature)


def test_failing_trial_recorded_with_error_and_run_continues(tmp_path):
    items = [make_item(f"i{i}") for i in range(3)]
    models = [_FailItemModel("A", fail_item_id="i1")]
    plan = build_plan(items, reps=2)
    path = tmp_path / "events.jsonl"
    broker = FakeBroker()

    ctrl = run(plan, models, path, broker, items=items)

    events = read_events(path)
    # the run did NOT stop: every planned trial produced an event (Req 7.5).
    planned_ids = {s.trial_id for s in planned_trials(plan, models)}
    assert {ev.trial_id for ev in events} == planned_ids
    assert ctrl.status == RunStatus.COMPLETED

    errored = [ev for ev in events if ev.error is not None]
    ok = [ev for ev in events if ev.error is None]
    # exactly the two reps of i1 errored; the rest succeeded.
    assert {ev.item_id for ev in errored} == {"i1"}
    assert len(errored) == 2
    assert {ev.item_id for ev in ok} == {"i0", "i2"}
    # per-model error count surfaced for the UI.
    assert ctrl.counts["A"].errored == 2


def test_errored_trials_are_retried_on_resume(tmp_path):
    items = [make_item(f"i{i}") for i in range(3)]
    plan = build_plan(items, reps=2)
    path = tmp_path / "events.jsonl"

    # Run 1: model A fails item i1 -> those trials recorded errored.
    run(plan, [_FailItemModel("A", fail_item_id="i1")], path, FakeBroker(), items=items)

    i1_ids = {s.trial_id for s in planned_trials(plan, [MockAdapter(name="A")]) if s.item_id == "i1"}
    # errored trials are NOT considered done (Req 7.5): excluded from resume_point.
    done_after_run1 = resume_point(path)
    assert i1_ids.isdisjoint(done_after_run1)
    events_after_run1 = read_events(path)

    # Run 2 (resume): a healthy model A re-runs ONLY the missing (errored) i1 trials.
    broker2 = FakeBroker()
    run(plan, [MockAdapter(name="A")], path, broker2, items=items)

    # only the 2 i1 trials were re-run this time.
    assert len(broker2.published) == 2
    assert {p["item_id"] for _, p in broker2.published} == {"i1"}

    # they are now durable + successful.
    done_after_run2 = resume_point(path)
    assert i1_ids <= done_after_run2
    events_after_run2 = read_events(path)
    assert len(events_after_run2) == len(events_after_run1) + 2
    # a successful i1 event now exists.
    assert any(ev.item_id == "i1" and ev.error is None for ev in events_after_run2)


# ===========================================================================
# P3 — resume is idempotent (zero new trials on a complete log)
# ===========================================================================
def test_resume_runs_zero_new_trials_on_complete_plan(tmp_path):
    items = [make_item(f"i{i}") for i in range(4)]
    models = [MockAdapter(name="A"), MockAdapter(name="B")]
    plan = build_plan(items, reps=2)
    path = tmp_path / "events.jsonl"

    run(plan, models, path, FakeBroker(), items=items)
    events_first = read_events(path)

    # Re-invoke on the now-complete log: zero new work, zero new publishes.
    broker2 = FakeBroker()
    ctrl2 = run(plan, models, path, broker2, items=items)
    events_second = read_events(path)

    assert len(broker2.published) == 0
    assert len(events_second) == len(events_first)
    assert ctrl2.status == RunStatus.COMPLETED


# ===========================================================================
# Credential-expiry resilience (Req 13.3) — refresh fires, trials succeed
# ===========================================================================
class _ExpiredTokenException(Exception):
    """Class-name matches ``config.AUTH_EXPIRED_ERROR_CODES`` -> classifies AUTH_EXPIRED."""


class _AuthFlakyModel:
    """Fails AUTH_EXPIRED for the first ``fail_times`` generate attempts, then succeeds."""

    def __init__(self, name: str, fail_times: int) -> None:
        self.name = name
        self._remaining = fail_times
        self._inner = MockAdapter(name=name)

    async def generate(self, item, fragments, temperature):
        if self._remaining > 0:
            self._remaining -= 1
            raise _ExpiredTokenException("ExpiredTokenException: the security token expired")
        return await self._inner.generate(item, fragments, temperature)


def test_credential_expiry_triggers_refresh_and_trials_succeed(tmp_path):
    items = [make_item(f"i{i}") for i in range(2)]
    # fail auth twice (< AUTH_MAX_REFRESH_CYCLES=3), then succeed forever.
    model = _AuthFlakyModel("A", fail_times=2)
    plan = build_plan(items, reps=1)
    path = tmp_path / "events.jsonl"

    refresh_calls = {"n": 0}

    def refresh_credentials():
        refresh_calls["n"] += 1

    # max_concurrency=1 makes the auth-fail-then-succeed sequence deterministic.
    ctrl = run(
        plan, [model], path, FakeBroker(), items=items,
        refresh_credentials=refresh_credentials, max_concurrency=1,
    )

    # the refresh callback fired (creds were "refreshed")...
    assert refresh_calls["n"] >= 1
    assert ctrl.auth_refreshes >= 1
    # ...and every trial ultimately SUCCEEDED (no error events).
    events = read_events(path)
    assert len(events) == 2
    assert all(ev.error is None for ev in events)
    assert ctrl.status == RunStatus.COMPLETED


# ===========================================================================
# Run-wide auto-pause when the error rate crosses the threshold (Req 7.6/13.3)
# ===========================================================================
class _AlwaysFailModel:
    name = "A"

    async def generate(self, item, fragments, temperature):
        raise RuntimeError("simulated systemic downstream outage")


def test_error_rate_threshold_auto_pauses_the_run(tmp_path):
    # A large plan so there is plenty of pending work left when the gate trips.
    items = [make_item(f"i{i}") for i in range(40)]
    plan = build_plan(items, reps=1)
    path = tmp_path / "events.jsonl"
    broker = FakeBroker()

    ctrl = run(
        plan, [_AlwaysFailModel()], path, broker, items=items,
        max_concurrency=2,                # small pool -> prompt, deterministic pause
        error_rate_min_sample=4,
        error_rate_threshold=0.5,
    )

    events = read_events(path)
    # the run auto-paused rather than completing...
    assert ctrl.status == RunStatus.PAUSED
    assert ctrl.auto_paused is True
    # ...so NOT all 40 trials were consumed (the whole point of Req 7.6).
    assert 0 < len(events) < 40
    # every consumed trial was an error (systemic outage).
    assert all(ev.error is not None for ev in events)
    # exactly one publish per appended event still holds.
    assert len(broker.published) == len(events)


# ===========================================================================
# Health gate (Req 2.4 / 13.3) — fail fast, no trials run
# ===========================================================================
def test_unhealthy_substrate_fails_fast(tmp_path):
    items = [make_item("i0")]
    plan = build_plan(items, reps=1)
    path = tmp_path / "events.jsonl"

    with pytest.raises(RunHealthError):
        asyncio.run(
            schedule_run(
                plan, [MockAdapter(name="A")], path, FakeBroker(),
                items=items, retr=FakeRetrieval(healthy=False), scoring=StubScoring(),
                resilience_sleep=_instant_sleep,
            )
        )
    # nothing was written.
    assert read_events(path) == []


# ===========================================================================
# Control hooks (Req 10.5) — abort stops the run; pause/resume toggle state
# ===========================================================================
def test_abort_stops_the_run_before_consuming_all_trials(tmp_path):
    items = [make_item(f"i{i}") for i in range(30)]
    plan = build_plan(items, reps=1)
    path = tmp_path / "events.jsonl"

    controller = RunController()

    class _AbortAfterFew:
        """A model that aborts the run via the controller after a few generations."""

        name = "A"

        def __init__(self) -> None:
            self._inner = MockAdapter(name="A")
            self._n = 0

        async def generate(self, item, fragments, temperature):
            self._n += 1
            if self._n >= 3:
                controller.abort()
            return await self._inner.generate(item, fragments, temperature)

    ctrl = run(
        plan, [_AbortAfterFew()], path, FakeBroker(), items=items,
        controller=controller, max_concurrency=1,
    )

    assert ctrl.status == RunStatus.ABORTED
    assert ctrl.aborted is True
    # aborted promptly -> far fewer than 30 events.
    assert len(read_events(path)) < 30


def test_controller_pause_resume_state_transitions():
    c = RunController()
    assert c.status == RunStatus.IDLE
    c.start()
    assert c.status == RunStatus.RUNNING
    c.pause()
    assert c.status == RunStatus.PAUSED
    c.resume()
    assert c.status == RunStatus.RUNNING
    c.abort()
    assert c.status == RunStatus.ABORTED
    # resume after abort is a no-op (abort is terminal).
    c.resume()
    assert c.status == RunStatus.ABORTED


# ===========================================================================
# Integration with the REAL offline scoring pipeline (over mock outputs)
# ===========================================================================
def test_end_to_end_with_real_offline_scoring_pipeline(tmp_path):
    from bakeoff.scoring.pipeline import ScoringPipeline

    items = [
        make_item("full0", answerability="full"),
        make_item("none0", answerability="none"),
    ]
    models = [MockAdapter(name="A", profile=MockProfile.grounded())]
    plan = build_plan(items, reps=2)
    path = tmp_path / "events.jsonl"

    ctrl = run(plan, models, path, FakeBroker(), items=items, scoring=ScoringPipeline.offline())

    events = read_events(path)
    assert len(events) == 4
    assert ctrl.status == RunStatus.COMPLETED
    # every event is fully scored and schema-valid (real composition path).
    for ev in events:
        validate_event(ev)
        assert 0.0 <= ev.quality.composite <= 1.0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

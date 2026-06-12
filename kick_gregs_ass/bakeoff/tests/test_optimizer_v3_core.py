"""
Optimizer V3 core tests — guards, contained scoring, island skip/death, orchestration.

Mirrors the v2 test discipline: offline backend bundle (zero network), real Item
shapes, failure injection through the backend seams. Everything here exercises the
V3 failure envelope the v2 post-mortem demanded:

* guards: hard timeout -> TRANSIENT retry -> GuardedCallError on budget exhaustion;
* scorer: collate-survivors / one batch retry / IterationSkipped below the fraction;
* island: ANY step failure becomes a skipped iteration (champion kept, counters
  advanced, consecutive_failures tracked, never an escaped exception);
* orchestrator: happy path end-to-end (sentinel + durable records + resume), and
  the total-failure path (island death -> degraded freeze -> contained Phase B
  failure -> structured result; the run NEVER raises).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bakeoff import config
from bakeoff.quality.optimizer.backends import build_offline_backend
from bakeoff.quality.optimizer.orchestrator import ViewRegistry
from bakeoff.quality.optimizer.store import OptimizerStore
from bakeoff.quality.optimizer.v3.guards import GuardedCallError, guarded_call
from bakeoff.quality.optimizer.v3.island import ResilientIslandLoop
from bakeoff.quality.optimizer.v3.orchestrator import V3Orchestrator
from bakeoff.quality.optimizer.v3.scorer import (
    ConversationFailure,
    IterationSkipped,
    ResilientScorer,
)
from bakeoff.quality.optimizer.rungs import build_rung_ladder
from bakeoff.types import CohortKey, GoldFragment, Item, Turn


def _run(coro):
    """Drive an awaitable to completion without a pytest-asyncio plugin."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fast_backoffs(monkeypatch):
    """Make every retry/backoff instant so failure-path tests run in milliseconds."""
    monkeypatch.setattr(config, "RETRY_BACKOFF_BASE_S", 0.001)
    monkeypatch.setattr(config, "RETRY_BACKOFF_MAX_S", 0.002)
    monkeypatch.setattr(config, "AUTH_BACKOFF_BASE_S", 0.001)
    monkeypatch.setattr(config, "AUTH_BACKOFF_MAX_S", 0.002)


# ---------------------------------------------------------------------------
# Item builders (real shapes — turn_reference / _turn_judge_inputs must resolve)
# ---------------------------------------------------------------------------
def _cohort(answerability: str = "full") -> CohortKey:
    return CohortKey(
        geography="US",
        proficiency="fluent",
        tone="neutral",
        entry_route="slack",
        momentary_state="neutral",
        answerability=answerability,
        turn_type="multi",
    )


def _gold_item(item_id: str) -> Item:
    return Item(
        id=item_id,
        turn_type="multi",
        cohort=_cohort("full"),
        wants="how to request a corporate card",
        answerability="full",
        gold=[
            GoldFragment(
                node_id="g1",
                title="Corporate Card",
                markdown="Request a corporate card through the expense portal.",
            )
        ],
        turns=(
            Turn(
                turn=1,
                user_utterance="How do I get a corporate card?",
                momentary_state="neutral",
                answerability="full",
            ),
        ),
    )


class _FailureInjectingBackend:
    """Delegate to the offline bundle; explode generation for the chosen item ids."""

    def __init__(self, inner, fail_ids):
        self._inner = inner
        self.fail_ids = set(fail_ids)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def answer_adapter_factory(self):
        inner_factory = self._inner.answer_adapter_factory
        fail_ids = self.fail_ids

        def factory(model, instruction, item_lookup):
            real_adapter = inner_factory(model, instruction, item_lookup)

            class _Adapter:
                async def generate(self, item, frags, temp):
                    if item.item_id in fail_ids:
                        raise ConnectionError(f"injected failure for {item.item_id}")
                    return await real_adapter.generate(item, frags, temp)

            return _Adapter()

        return factory


class _RecordingEmitter:
    """Duck-typed OptimizerEventEmitter stand-in recording every emission."""

    def __init__(self):
        self.events: list[tuple] = []

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.events.append((name, kwargs or args))

        return method

    def names(self):
        return [name for name, _ in self.events]


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
def test_guard_hard_timeout_is_retried_then_raises_guarded_call_error():
    """A hung call is cancelled at the timeout, classified TRANSIENT, retried, and
    finally surfaced as GuardedCallError with the TimeoutError chained — never a hang."""

    async def hangs():
        await asyncio.sleep(60)

    with pytest.raises(GuardedCallError) as err:
        _run(guarded_call("hang", hangs, timeout_s=0.02, max_retries=1))
    assert isinstance(err.value.last_error, TimeoutError)
    assert err.value.label == "hang"


def test_guard_transient_errors_retry_until_success():
    """THROTTLED/TRANSIENT failures back off and retry within the budget."""
    attempt_count = {"n": 0}

    async def flaky():
        attempt_count["n"] += 1
        if attempt_count["n"] < 3:
            raise ConnectionError("transient blip")
        return "ok"

    result = _run(guarded_call("flaky", flaky, timeout_s=5, max_retries=4))
    assert result == "ok"
    assert attempt_count["n"] == 3


def test_guard_unknown_errors_raise_immediately_without_retry():
    """A PERMANENT/UNKNOWN classification is not retried (retrying cannot help)."""
    attempt_count = {"n": 0}

    async def broken():
        attempt_count["n"] += 1
        raise ValueError("programming error")

    with pytest.raises(GuardedCallError):
        _run(guarded_call("broken", broken, timeout_s=5, max_retries=4))
    assert attempt_count["n"] == 1


# ---------------------------------------------------------------------------
# scorer — collate survivors / batch retry / IterationSkipped
# ---------------------------------------------------------------------------
def _scorer(backend, *, min_success_fraction=0.8, on_failure=None) -> ResilientScorer:
    return ResilientScorer(
        backend,
        reps=1,
        min_success_fraction=min_success_fraction,
        model_timeout_s=2,
        turn_timeout_s=2,
        on_conversation_failure=on_failure,
    )


def test_scorer_collates_survivors_when_fraction_holds():
    """One persistently-failing conversation out of six: the pass scores the five
    survivors and reports the contained failure — the caller sees a normal SliceScore."""
    items = [_gold_item(f"it-{i}") for i in range(6)]
    failures_seen: list[ConversationFailure] = []
    backend = _FailureInjectingBackend(build_offline_backend(), {"it-0"})
    score = _run(
        _scorer(backend, on_failure=failures_seen.append).score_prompt(
            model="haiku-4.5", instruction="inst", items=items, prompt_role="champion"
        )
    )
    assert score.n_conversations == 5
    assert failures_seen and failures_seen[0].item_id == "it-0"
    assert failures_seen[0].stage == "generate"


def test_scorer_raises_iteration_skipped_below_fraction_after_batch_retry():
    """Three persistent failures out of six (0.5 < 0.8) survive the one batch retry
    and surface as IterationSkipped carrying the per-conversation failure detail."""
    items = [_gold_item(f"it-{i}") for i in range(6)]
    backend = _FailureInjectingBackend(build_offline_backend(), {"it-0", "it-1", "it-2"})
    with pytest.raises(IterationSkipped) as skip:
        _run(
            _scorer(backend).score_prompt(
                model="haiku-4.5", instruction="inst", items=items, prompt_role="champion"
            )
        )
    assert skip.value.survivors == 3
    assert skip.value.total == 6
    assert {f.item_id for f in skip.value.failures} == {"it-0", "it-1", "it-2"}


def test_scorer_batch_retry_recovers_transient_weather():
    """A conversation that fails the whole first pass but succeeds on the batch retry
    ends in a full collation — transient weather costs a retry, not data."""
    items = [_gold_item(f"it-{i}") for i in range(6)]
    attempt_count = {"n": 0}
    guard_budget = config.QUALITY_OPT_V3_GUARD_MAX_RETRIES

    class _TransientBackend(_FailureInjectingBackend):
        @property
        def answer_adapter_factory(self):
            inner_factory = self._inner.answer_adapter_factory

            def factory(model, instruction, item_lookup):
                real_adapter = inner_factory(model, instruction, item_lookup)

                class _Adapter:
                    async def generate(self, item, frags, temp):
                        if item.item_id == "it-5":
                            attempt_count["n"] += 1
                            # Outlast pass 1's guard budget; clear before the batch retry.
                            if attempt_count["n"] <= guard_budget + 1:
                                raise ConnectionError("blip")
                        return await real_adapter.generate(item, frags, temp)

                return _Adapter()

            return factory

    backend = _TransientBackend(build_offline_backend(), set())
    score = _run(
        _scorer(backend, min_success_fraction=1.0).score_prompt(
            model="haiku-4.5", instruction="inst", items=items, prompt_role="champion"
        )
    )
    assert score.n_conversations == 6


# ---------------------------------------------------------------------------
# island — contained step
# ---------------------------------------------------------------------------
def _island(backend, emitter, items) -> ResilientIslandLoop:
    ladder = build_rung_ladder(items)
    store = OptimizerStore()  # not written by the island (orchestrator-owned)
    return ResilientIslandLoop(
        island_id=0,
        model="haiku-4.5",
        backend=backend,
        ladder=ladder,
        store=store,
        emitter=emitter,
        style=config.QUALITY_OPT_ISLAND_STYLES[0],
    )


def test_island_step_contains_scoring_failure_as_skip():
    """A rung pass whose conversations all fail becomes a SKIPPED iteration: the
    champion is kept, counters advance, consecutive_failures increments, the skip
    event is emitted — and step() returns a snapshot instead of raising."""
    items = [_gold_item(f"it-{i}") for i in range(6)]
    emitter = _RecordingEmitter()
    backend = _FailureInjectingBackend(
        build_offline_backend(), {f"it-{i}" for i in range(6)}
    )
    island = _island(backend, emitter, items)
    champion_before = island.champion_instruction

    state = _run(island.step())

    assert state.total_iterations == 1
    assert state.consecutive_non_improving == 1
    assert island.consecutive_failures == 1
    assert island.champion_instruction == champion_before
    skip_events = [
        payload for name, payload in emitter.events
        if name == "emit" and payload and payload[0] == "optimizer_iteration_skipped"
    ]
    assert skip_events, emitter.names()


def test_island_successful_step_resets_consecutive_failures():
    """A clean step after failures resets the failure streak (death needs CONSECUTIVE)."""
    items = [_gold_item(f"it-{i}") for i in range(6)]
    emitter = _RecordingEmitter()
    backend = _FailureInjectingBackend(build_offline_backend(), set())
    island = _island(backend, emitter, items)
    island.consecutive_failures = 2  # as if two failed steps preceded

    _run(island.step())

    assert island.consecutive_failures == 0


# ---------------------------------------------------------------------------
# orchestrator — end to end
# ---------------------------------------------------------------------------
def _orchestrator(backend, store, emitter, tmp_path) -> V3Orchestrator:
    return V3Orchestrator(
        models=["haiku-4.5"],
        backend=backend,
        store=store,
        emitter=emitter,
        view_registry=ViewRegistry(),
        state_path=tmp_path / "state.json",
    )


def _tmp_store(tmp_path) -> OptimizerStore:
    return OptimizerStore(
        iterations_path=tmp_path / "iters.jsonl",
        audit_path=tmp_path / "audit.jsonl",
        errors_path=tmp_path / "errors.jsonl",
        results_path=tmp_path / "results.json",
    )


def test_orchestrator_happy_path_completes_with_sentinel_and_resume(tmp_path):
    """Full offline run: completed result, durable records, phase sentinel; a second
    run returns the stored result via the sentinel without re-running anything."""
    items = [_gold_item(f"it-{i}") for i in range(10)]
    store = _tmp_store(tmp_path)
    emitter = _RecordingEmitter()
    backend = build_offline_backend()
    orchestrator = _orchestrator(backend, store, emitter, tmp_path)

    results = _run(
        orchestrator.run_v3(
            ["haiku-4.5"], backend, emitter=emitter, store=store, all_items=items
        )
    )
    result = results["haiku-4.5"]
    assert result["status"] == "completed"
    assert result["degraded"] is False
    assert result["champion_instruction"]

    sentinel = json.loads((tmp_path / "state.json").read_text())["haiku-4.5"]
    assert sentinel["phase_a_complete"] is True
    assert sentinel["phase_b_done"] is True
    assert store.read_iterations(), "durable iteration records must exist"
    assert "island_step" in emitter.names()
    assert "tournament" in emitter.names()

    # Resume: a fresh orchestrator over the same sentinel returns the stored result.
    orchestrator2 = _orchestrator(backend, store, emitter, tmp_path)
    results2 = _run(
        orchestrator2.run_v3(
            ["haiku-4.5"], backend, emitter=emitter, store=store, all_items=items
        )
    )
    assert results2["haiku-4.5"]["status"] == "completed"


def test_orchestrator_total_failure_degrades_never_raises(tmp_path, monkeypatch):
    """Every generation failing forever: islands skip then die, the best-known
    champion freezes (degraded), Phase B's failure is contained — the run returns a
    structured result and the error store is populated. Nothing ever raises."""
    monkeypatch.setattr(config, "QUALITY_OPT_V3_GUARD_MAX_RETRIES", 0)
    items = [_gold_item(f"it-{i}") for i in range(10)]
    store = _tmp_store(tmp_path)
    emitter = _RecordingEmitter()
    backend = _FailureInjectingBackend(
        build_offline_backend(), {f"it-{i}" for i in range(10)}
    )
    orchestrator = _orchestrator(backend, store, emitter, tmp_path)

    results = _run(
        orchestrator.run_v3(
            ["haiku-4.5"], backend, emitter=emitter, store=store, all_items=items
        )
    )
    result = results["haiku-4.5"]
    assert result["status"] == "phase_b_failed"
    assert result["degraded"] is True
    assert result["champion_instruction"]  # progress preserved through total failure

    sentinel = json.loads((tmp_path / "state.json").read_text())["haiku-4.5"]
    assert sentinel["dead_islands"] == [0, 1]
    error_lines = (tmp_path / "errors.jsonl").read_text().strip().splitlines()
    assert error_lines, "contained failures must be recorded in the error store"
    dead_events = [
        payload for name, payload in emitter.events
        if name == "emit" and payload and payload[0] == "optimizer_island_dead"
    ]
    assert len(dead_events) == 2


def test_orchestrator_one_model_failure_never_touches_the_other(tmp_path, monkeypatch):
    """Per-model containment: a model whose items always fail ends degraded while the
    healthy sibling completes normally in the same gathered run."""
    monkeypatch.setattr(config, "QUALITY_OPT_V3_GUARD_MAX_RETRIES", 0)
    items = [_gold_item(f"it-{i}") for i in range(10)]
    store = _tmp_store(tmp_path)
    emitter = _RecordingEmitter()

    inner = build_offline_backend()

    class _ModelSelectiveBackend(_FailureInjectingBackend):
        """Fail every generation for ONE model only (haiku-4.5)."""

        @property
        def answer_adapter_factory(self):
            inner_factory = self._inner.answer_adapter_factory

            def factory(model, instruction, item_lookup):
                real_adapter = inner_factory(model, instruction, item_lookup)

                class _Adapter:
                    async def generate(self, item, frags, temp):
                        if model == "haiku-4.5":
                            raise ConnectionError("haiku is down")
                        return await real_adapter.generate(item, frags, temp)

                return _Adapter()

            return factory

    backend = _ModelSelectiveBackend(inner, set())
    orchestrator = V3Orchestrator(
        models=["sonnet-4.6-thinking-off", "haiku-4.5"],
        backend=backend,
        store=store,
        emitter=emitter,
        view_registry=ViewRegistry(),
        state_path=tmp_path / "state.json",
    )
    results = _run(
        orchestrator.run_v3(
            ["sonnet-4.6-thinking-off", "haiku-4.5"],
            backend,
            emitter=emitter,
            store=store,
            all_items=items,
        )
    )
    assert results["sonnet-4.6-thinking-off"]["status"] == "completed"
    assert results["haiku-4.5"]["status"] == "phase_b_failed"
    assert results["haiku-4.5"]["degraded"] is True

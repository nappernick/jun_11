"""
Unit tests for :mod:`bakeoff.eval.experiment_runner` multi-agent runs (Task 3.2).

Covers the Experiment_Runner multi-agent contract (Req 5, 7.4, 19.3):

* N ≥ 3 agents produce exactly one Instance per ``(agent, query, corpus_size)``
  (Req 5.1, 5.2, 5.4);
* a forced single-execution failure yields a ``status="failed"`` Instance and
  the run continues for the remaining agents/queries (Req 5.5);
* the same ``(query, corpus_size)`` yields identical retrieval results across
  every compared agent, and the retrieval seam is invoked at most once per
  distinct ``(query, corpus_size)`` (Req 19.3);
* Instance_Index is a strictly-increasing ordinal within each Session (Req 7.4).

Fully offline: injected retrieval/agent providers + offline ragas, zero network.
"""
from __future__ import annotations

import socket

import pytest

from bakeoff.eval.event_store import EvalEventStore
from bakeoff.eval.experiment_runner import (
    AgentAnswer,
    ExperimentRunner,
    ExperimentRunnerError,
    Query,
    RetrievalResult,
)
from bakeoff.eval.metric_engine import MetricEngine
from bakeoff.eval.ragas_adapter import RagasAdapter
from bakeoff.eval.retrieval_metrics import RetrievalMetricComputer


# ---------------------------------------------------------------------------
# Deterministic, network-free test doubles
# ---------------------------------------------------------------------------
class FakeClock:
    """A monotonic clock that advances a fixed step on every read."""

    def __init__(self, step: float = 1.0, start: float = 100.0) -> None:
        self.t = start
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


class SpyRetrievalProvider:
    """Deterministic retrieval keyed only by ``(query_id, corpus_size)``.

    Records every call so a test can assert the runner memoizes per
    ``(query, corpus_size)`` and reuses across agents (Req 19.3). The result is
    independent of the agent, so reuse across agents is byte-identical.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, query: Query, corpus_size: int) -> RetrievalResult:
        self.calls.append((query.query_id, corpus_size))
        ranked = tuple(f"{query.query_id}-d{i}" for i in range(5))
        return RetrievalResult(
            ranked_ids=ranked,
            gold_ids=(ranked[1],),  # one resolvable Gold_Link
            fragments=(f"context for {query.text}",),
            retrieval_ms=10.0,
            cached=False,
        )


class SpyAgentProvider:
    """Agent answer that depends on the agent (so ragas differs per agent).

    ``fail_on`` forces a single ``(agent_id, query_id)`` execution to raise, to
    exercise the failed-Instance-then-continue path (Req 5.5).
    """

    def __init__(self, fail_on: tuple[str, str] | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.fail_on = fail_on

    def __call__(self, agent_id: str, query: Query, retrieval: RetrievalResult) -> AgentAnswer:
        self.calls.append((agent_id, query.query_id, len(retrieval.ranked_ids)))
        if self.fail_on is not None and (agent_id, query.query_id) == self.fail_on:
            raise RuntimeError("forced agent failure")
        return AgentAnswer(
            answer=f"{agent_id} says: context for {query.text}",
            generation_ms=20.0,
            confidence=0.7,
        )


def _engine(tmp_path) -> MetricEngine:
    return MetricEngine(
        EvalEventStore(tmp_path / "eval_instances.jsonl"),
        ragas_adapter=RagasAdapter.offline(
            enabled_metrics=["faithfulness", "answer_relevancy", "context_precision"]
        ),
        retrieval_computer=RetrievalMetricComputer(k=3),
    )


def _queries(n: int = 2) -> list[Query]:
    return [
        Query(query_id=f"q{i}", text=f"question {i}", reference=f"answer {i}",
              prompt_id="p1", category="profile")
        for i in range(n)
    ]


AGENTS = ("agent-A", "agent-B", "agent-C")  # N = 3 (Req 5.4: no fixed count)


def _runner(tmp_path, agent_provider=None, retrieval_provider=None):
    return ExperimentRunner(
        _engine(tmp_path),
        retrieval_provider or SpyRetrievalProvider(),
        agent_provider or SpyAgentProvider(),
        k=3,
        clock=FakeClock(),
        now=lambda: "2025-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# one Instance per (agent, query, corpus_size) for N >= 3 (Req 5.1, 5.2, 5.4)
# ---------------------------------------------------------------------------
def test_one_instance_per_agent_query_corpus_for_n_ge_3(tmp_path):
    runner = _runner(tmp_path)
    queries = _queries(2)
    result = runner.run_multi_agent(AGENTS, queries, corpus_size=1000, run_id="run1")

    assert len(result.instances) == len(AGENTS) * len(queries)  # 3 * 2 = 6
    # exactly one Instance per (agent, query) — instance_ids are unique and
    # cover the full cross product.
    expected_ids = {
        f"run1:{a}:cs1000:{q.query_id}" for a in AGENTS for q in queries
    }
    assert {i.instance_id for i in result.instances} == expected_ids
    # every Instance is labelled with the run's corpus size (Req 6.3 baseline).
    assert {i.corpus_size for i in result.instances} == {1000}
    # the agent set is carried through unchanged (configuration, not code).
    assert result.agents == AGENTS


def test_runner_supports_more_than_three_agents(tmp_path):
    runner = _runner(tmp_path)
    agents = ("a", "b", "c", "d", "e")  # N = 5: no fixed count assumed (Req 5.4)
    result = runner.run_multi_agent(agents, _queries(1), corpus_size=500, run_id="r")
    assert len(result.instances) == 5
    assert {i.agent_id for i in result.instances} == set(agents)


# ---------------------------------------------------------------------------
# identical retrieval reused across agents; provider called once per (q, size)
# ---------------------------------------------------------------------------
def test_retrieval_reused_identically_across_agents(tmp_path):
    spy = SpyRetrievalProvider()
    runner = _runner(tmp_path, retrieval_provider=spy)
    queries = _queries(2)
    result = runner.run_multi_agent(AGENTS, queries, corpus_size=1000, run_id="run1")

    # the retrieval seam is invoked once per distinct (query, corpus_size), NOT
    # once per (agent, query, corpus_size) — reuse across agents (Req 19.3).
    assert len(spy.calls) == len(queries)
    assert sorted(spy.calls) == sorted((q.query_id, 1000) for q in queries)

    # the resulting retrieval metric values are byte-identical across agents for
    # the same (query, corpus_size), since the same retrieval result is reused.
    by_query: dict[str, list] = {}
    for inst in result.instances:
        qid = inst.instance_id.rsplit(":", 1)[-1]
        by_query.setdefault(qid, []).append(inst)
    for qid, insts in by_query.items():
        assert len(insts) == len(AGENTS)
        ref = {n: mv.value for n, mv in insts[0].retrieval.items()}
        for other in insts[1:]:
            assert {n: mv.value for n, mv in other.retrieval.items()} == ref


def test_first_agent_cold_then_reuse_is_cached(tmp_path):
    runner = _runner(tmp_path)
    result = runner.run_multi_agent(AGENTS, _queries(1), corpus_size=1000, run_id="run1")
    # exactly one cold retrieval per (query, size); the remaining agents reuse it.
    cached_flags = sorted(i.retrieval_cached for i in result.instances)
    assert cached_flags == [False, True, True]  # 1 cold + (N-1) cached


# ---------------------------------------------------------------------------
# a single failure yields a failed Instance and the run continues (Req 5.5)
# ---------------------------------------------------------------------------
def test_single_failure_records_failed_instance_and_continues(tmp_path):
    agent_provider = SpyAgentProvider(fail_on=("agent-B", "q1"))
    runner = _runner(tmp_path, agent_provider=agent_provider)
    queries = _queries(2)
    result = runner.run_multi_agent(AGENTS, queries, corpus_size=1000, run_id="run1")

    # the run still produced the full cross product (it did not abort).
    assert len(result.instances) == len(AGENTS) * len(queries)
    assert result.failed_count == 1

    failed = [i for i in result.instances if i.status == "failed"]
    assert len(failed) == 1
    bad = failed[0]
    assert bad.instance_id == "run1:agent-B:cs1000:q1"
    assert bad.error is not None and "forced agent failure" in bad.error
    # no answer to score ⟹ empty ragas map for the failed execution.
    assert bad.ragas == {}

    # every other execution succeeded.
    ok = [i for i in result.instances if i.status == "ok"]
    assert len(ok) == len(AGENTS) * len(queries) - 1
    assert all(i.status == "ok" for i in ok)


# ---------------------------------------------------------------------------
# Instance_Index strictly increasing within a Session (Req 7.4)
# ---------------------------------------------------------------------------
def test_instance_index_strictly_increasing_per_session(tmp_path):
    runner = _runner(tmp_path)
    queries = _queries(3)
    result = runner.run_multi_agent(AGENTS, queries, corpus_size=1000, run_id="run1")

    by_session: dict[str, list[int]] = {}
    for inst in result.instances:
        by_session.setdefault(inst.session_id, []).append(inst.instance_index)

    # one Session per agent, each with a contiguous 0..n-1 strictly-increasing run.
    assert set(by_session) == {f"run1:{a}" for a in AGENTS}
    for indices in by_session.values():
        assert indices == sorted(indices)
        assert indices == list(range(len(queries)))
        assert all(b > a for a, b in zip(indices, indices[1:]))  # strictly increasing


# ---------------------------------------------------------------------------
# per-stage timings recorded separately from end-to-end latency (Req 7.2, 7.3)
# ---------------------------------------------------------------------------
def test_latency_and_stage_timings_recorded_separately(tmp_path):
    runner = _runner(tmp_path)
    result = runner.run_multi_agent(("a", "b", "c"), _queries(1), corpus_size=1000, run_id="r")
    for inst in result.instances:
        assert inst.latency_ms > 0  # end-to-end measured, positive, not flagged
        assert inst.latency_flagged is False
        # stage timings are present and stored separately from latency_ms.
        assert inst.stage_timings.generation_ms is not None


# ---------------------------------------------------------------------------
# durable persistence: exactly the produced Instances land in the store (Req 8.1)
# ---------------------------------------------------------------------------
def test_every_produced_instance_is_appended_to_the_store(tmp_path):
    runner = _runner(tmp_path)
    result = runner.run_multi_agent(AGENTS, _queries(2), corpus_size=1000, run_id="run1")
    stored = runner.engine.store.read_all()
    assert len(stored) == len(result.instances)
    assert [s.instance_id for s in stored] == [i.instance_id for i in result.instances]


# ---------------------------------------------------------------------------
# offline: a full run issues no network call
# ---------------------------------------------------------------------------
def test_run_makes_no_network_call(tmp_path, monkeypatch):
    runner = _runner(tmp_path)

    def _boom(*args, **kwargs):  # pragma: no cover - only runs if violated
        raise AssertionError("experiment runner must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    result = runner.run_multi_agent(AGENTS, _queries(2), corpus_size=1000, run_id="run1")
    assert len(result.instances) == 6


# ---------------------------------------------------------------------------
# configuration validation (no fixed agent count assumed — Req 5.3, 5.4)
# ---------------------------------------------------------------------------
def test_empty_agents_rejected(tmp_path):
    runner = _runner(tmp_path)
    with pytest.raises(ExperimentRunnerError, match="non-empty"):
        runner.run_multi_agent([], _queries(1), corpus_size=1000)


def test_duplicate_agents_rejected(tmp_path):
    runner = _runner(tmp_path)
    with pytest.raises(ExperimentRunnerError, match="unique"):
        runner.run_multi_agent(["a", "a", "b"], _queries(1), corpus_size=1000)

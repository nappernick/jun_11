"""
Unit tests for :mod:`bakeoff.eval.experiment_runner` corpus-size sweep (Task 3.4).

Covers the Experiment_Runner corpus-size-sweep contract (Req 6, 19.2):

* the query set is held constant across every corpus size in the sweep (Req 6.4);
* each Instance is labelled with the corpus size it ran against (Req 6.3);
* a size that cannot be prepared is recorded unavailable and the sweep continues
  with the remaining sizes (Req 6.5);
* preparation and retrieval are read-only — the canonical substrate is never
  mutated (Req 19.2).

Fully offline: injected preparer/retrieval/agent providers + offline ragas.
"""
from __future__ import annotations

import pytest

from bakeoff.eval.event_store import EvalEventStore
from bakeoff.eval.experiment_runner import (
    AgentAnswer,
    CorpusUnavailableError,
    ExperimentRunner,
    ExperimentRunnerError,
    Query,
    RetrievalResult,
)
from bakeoff.eval.metric_engine import MetricEngine
from bakeoff.eval.ragas_adapter import RagasAdapter
from bakeoff.eval.retrieval_metrics import RetrievalMetricComputer


class FakeClock:
    def __init__(self, step: float = 1.0, start: float = 100.0) -> None:
        self.t = start
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


class ReadOnlySubstrate:
    """A canonical corpus the providers may only READ.

    Any attempt to add/remove documents would change ``docs``; the tests assert
    it is byte-for-byte unchanged after a sweep, proving the runner/providers
    treat each sized corpus as a read-only input (Req 19.2).
    """

    def __init__(self, doc_ids: tuple[str, ...]) -> None:
        self.docs = doc_ids

    def view(self, corpus_size: int) -> tuple[str, ...]:
        """A read-only prefix view of size ``corpus_size`` (no mutation)."""
        return self.docs[:corpus_size]


class SweepRetrievalProvider:
    """Retrieval keyed by ``(query_id, corpus_size)``; reads the substrate only.

    Records the ``(query_id, corpus_size)`` of every call so a test can assert
    the query set is held constant across sizes (Req 6.4) and that retrieval is
    reused per ``(query, size)`` across agents.
    """

    def __init__(self, substrate: ReadOnlySubstrate) -> None:
        self.substrate = substrate
        self.calls: list[tuple[str, int]] = []

    def __call__(self, query: Query, corpus_size: int) -> RetrievalResult:
        self.calls.append((query.query_id, corpus_size))
        view = self.substrate.view(corpus_size)  # READ ONLY
        ranked = tuple(view[:3]) or (f"{query.query_id}-d0",)
        return RetrievalResult(
            ranked_ids=ranked,
            gold_ids=(ranked[0],),
            fragments=(f"context {query.text} @ {corpus_size}",),
            retrieval_ms=float(corpus_size) / 100.0,
            cached=False,
        )


def _agent(agent_id: str, query: Query, retrieval: RetrievalResult) -> AgentAnswer:
    return AgentAnswer(answer=f"{agent_id}: context {query.text}", generation_ms=15.0)


def _engine(tmp_path) -> MetricEngine:
    return MetricEngine(
        EvalEventStore(tmp_path / "eval_instances.jsonl"),
        ragas_adapter=RagasAdapter.offline(enabled_metrics=["faithfulness"]),
        retrieval_computer=RetrievalMetricComputer(k=3),
    )


def _queries(n: int = 2) -> list[Query]:
    return [Query(query_id=f"q{i}", text=f"question {i}", reference=f"a{i}") for i in range(n)]


AGENTS = ("agent-A", "agent-B", "agent-C")
SIZES = (100, 1000, 5000)


def _runner(tmp_path, *, substrate, corpus_preparer=None):
    return ExperimentRunner(
        _engine(tmp_path),
        SweepRetrievalProvider(substrate),
        _agent,
        corpus_preparer=corpus_preparer,
        k=3,
        clock=FakeClock(),
        now=lambda: "2025-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# the query set is held constant across every corpus size (Req 6.4)
# ---------------------------------------------------------------------------
def test_query_set_held_constant_across_sizes(tmp_path):
    substrate = ReadOnlySubstrate(tuple(f"doc{i}" for i in range(6000)))
    runner = _runner(tmp_path, substrate=substrate)
    queries = _queries(3)
    result = runner.run_sweep(AGENTS, queries, corpus_sizes=SIZES, run_id="run1")

    # group the distinct query ids the retrieval seam saw, per corpus size.
    seen_by_size: dict[int, set[str]] = {}
    for qid, size in runner.retrieval_provider.calls:
        seen_by_size.setdefault(size, set()).add(qid)

    expected = {q.query_id for q in queries}
    assert set(seen_by_size) == set(SIZES)
    for size, qids in seen_by_size.items():
        assert qids == expected  # the SAME query set ran at every size (Req 6.4)

    # full cross product: agents * queries * sizes.
    assert len(result.instances) == len(AGENTS) * len(queries) * len(SIZES)


# ---------------------------------------------------------------------------
# each Instance is labelled with its corpus size (Req 6.3)
# ---------------------------------------------------------------------------
def test_each_instance_labelled_with_its_corpus_size(tmp_path):
    substrate = ReadOnlySubstrate(tuple(f"doc{i}" for i in range(6000)))
    runner = _runner(tmp_path, substrate=substrate)
    queries = _queries(2)
    result = runner.run_sweep(AGENTS, queries, corpus_sizes=SIZES, run_id="run1")

    assert {i.corpus_size for i in result.instances} == set(SIZES)
    # each size carries exactly agents * queries instances.
    for size in SIZES:
        at_size = [i for i in result.instances if i.corpus_size == size]
        assert len(at_size) == len(AGENTS) * len(queries)
    # the instance_id encodes the corpus size it ran against.
    for inst in result.instances:
        assert f":cs{inst.corpus_size}:" in inst.instance_id


# ---------------------------------------------------------------------------
# an unpreparable size is recorded unavailable; the sweep continues (Req 6.5)
# ---------------------------------------------------------------------------
def test_unpreparable_size_recorded_unavailable_and_sweep_continues(tmp_path):
    substrate = ReadOnlySubstrate(tuple(f"doc{i}" for i in range(6000)))

    def preparer(corpus_size: int):
        if corpus_size == 5000:
            raise CorpusUnavailableError("5000 cannot be prepared")
        return substrate.view(corpus_size)

    runner = _runner(tmp_path, substrate=substrate, corpus_preparer=preparer)
    queries = _queries(2)
    result = runner.run_sweep(AGENTS, queries, corpus_sizes=SIZES, run_id="run1")

    # the bad size is recorded unavailable ...
    assert result.unavailable_corpus_sizes == (5000,)
    # ... no Instances were produced for it ...
    assert all(i.corpus_size != 5000 for i in result.instances)
    # ... and the remaining sizes still ran (the sweep continued).
    assert {i.corpus_size for i in result.instances} == {100, 1000}
    assert len(result.instances) == len(AGENTS) * len(queries) * 2


def test_any_preparer_exception_marks_size_unavailable(tmp_path):
    substrate = ReadOnlySubstrate(tuple(f"doc{i}" for i in range(2000)))

    def preparer(corpus_size: int):
        if corpus_size == 1000:
            raise ValueError("generic preparation failure")  # not CorpusUnavailableError
        return substrate.view(corpus_size)

    runner = _runner(tmp_path, substrate=substrate, corpus_preparer=preparer)
    result = runner.run_sweep(AGENTS, _queries(1), corpus_sizes=(100, 1000), run_id="r")
    assert result.unavailable_corpus_sizes == (1000,)
    assert {i.corpus_size for i in result.instances} == {100}


# ---------------------------------------------------------------------------
# read-only: the canonical substrate is never mutated (Req 19.2)
# ---------------------------------------------------------------------------
def test_substrate_is_never_mutated(tmp_path):
    docs = tuple(f"doc{i}" for i in range(6000))
    substrate = ReadOnlySubstrate(docs)
    snapshot = substrate.docs  # tuple is immutable; identity + content must hold

    runner = _runner(tmp_path, substrate=substrate)
    runner.run_sweep(AGENTS, _queries(2), corpus_sizes=SIZES, run_id="run1")

    # the canonical corpus is byte-for-byte unchanged after the full sweep.
    assert substrate.docs is snapshot
    assert substrate.docs == docs


# ---------------------------------------------------------------------------
# Instance_Index strictly increasing per Session across the whole sweep (Req 7.4)
# ---------------------------------------------------------------------------
def test_instance_index_strictly_increasing_across_sweep(tmp_path):
    substrate = ReadOnlySubstrate(tuple(f"doc{i}" for i in range(6000)))
    runner = _runner(tmp_path, substrate=substrate)
    queries = _queries(2)
    result = runner.run_sweep(AGENTS, queries, corpus_sizes=SIZES, run_id="run1")

    by_session: dict[str, list[int]] = {}
    for inst in result.instances:
        by_session.setdefault(inst.session_id, []).append(inst.instance_index)

    # one Session per agent; each accumulates a strictly-increasing index across
    # every (corpus_size, query) execution in the sweep.
    assert set(by_session) == {f"run1:{a}" for a in AGENTS}
    for indices in by_session.values():
        assert indices == list(range(len(queries) * len(SIZES)))
        assert all(b > a for a, b in zip(indices, indices[1:]))


# ---------------------------------------------------------------------------
# empty corpus-size series is rejected
# ---------------------------------------------------------------------------
def test_empty_corpus_sizes_rejected(tmp_path):
    substrate = ReadOnlySubstrate(tuple(f"doc{i}" for i in range(10)))
    runner = _runner(tmp_path, substrate=substrate)
    with pytest.raises(ExperimentRunnerError, match="non-empty"):
        runner.run_sweep(AGENTS, _queries(1), corpus_sizes=[])

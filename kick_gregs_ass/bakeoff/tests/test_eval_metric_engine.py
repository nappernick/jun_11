"""
Unit tests for :mod:`bakeoff.eval.metric_engine` (Task 2.8).

Covers the Metric_Engine orchestration contract:

* ragas and retrieval are stored in disjoint maps so the two signals are never
  conflated (Req 2.4 / P9);
* exactly one record is appended per scored instance (Req 8.1);
* recorded component values are unaffected by any Composite_Weight_Set — the
  engine holds none and stores raw values only (Req 1.6, 3.3);
* scoring is purely additive: no Authoritative_Judge decision is read or mutated
  (Req 18.1).

Network-free (offline ragas + pure retrieval math).
"""
from __future__ import annotations

import socket

import pytest

from bakeoff.eval.event_store import EvalEventStore
from bakeoff.eval.metric_engine import MetricEngine
from bakeoff.eval.models import EvalInstance, StageTimings
from bakeoff.eval.ragas_adapter import RagasAdapter, RagasSample
from bakeoff.eval.retrieval_metrics import RetrievalMetricComputer


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _engine(tmp_path) -> MetricEngine:
    store = EvalEventStore(tmp_path / "eval_instances.jsonl")
    return MetricEngine(
        store,
        ragas_adapter=RagasAdapter.offline(
            enabled_metrics=["faithfulness", "answer_relevancy", "context_precision"]
        ),
        retrieval_computer=RetrievalMetricComputer(k=3),
    )


def _sample() -> RagasSample:
    return RagasSample(
        question="What is the capital of France?",
        answer="The capital of France is Paris.",
        contexts=["France's capital city is Paris."],
        reference="Paris is the capital of France.",
    )


def _score_one(engine: MetricEngine, *, instance_id="inst-1", **overrides) -> EvalInstance:
    kwargs = dict(
        instance_id=instance_id,
        agent_id="agent-A",
        session_id="sess-1",
        instance_index=0,
        timestamp="2025-01-01T00:00:00Z",
        latency_ms=42.0,
        corpus_size=1000,
        ragas_sample=_sample(),
        ranked_ids=["f1", "f2", "f3", "f4"],
        gold_ids=["f2", "f9"],
        stage_timings=StageTimings(retrieval_ms=10.0, generation_ms=20.0),
        prompt_id="p1",
        category="profile",
    )
    kwargs.update(overrides)
    return engine.score_instance(**kwargs)


# ---------------------------------------------------------------------------
# ragas and retrieval stored in disjoint maps (Req 2.4 / P9)
# ---------------------------------------------------------------------------
def test_ragas_and_retrieval_stored_in_disjoint_maps(tmp_path):
    engine = _engine(tmp_path)
    inst = _score_one(engine)

    assert set(inst.ragas) == {"faithfulness", "answer_relevancy", "context_precision"}
    assert set(inst.retrieval) == {"precision_at_k", "recall_at_k", "ndcg_at_k"}
    # the two maps never share a key (the conflation guard).
    assert set(inst.ragas).isdisjoint(set(inst.retrieval))


def test_retrieval_values_record_k(tmp_path):
    engine = _engine(tmp_path)
    inst = _score_one(engine)
    for mv in inst.retrieval.values():
        assert mv.k == 3


# ---------------------------------------------------------------------------
# exactly one record appended per scored instance (Req 8.1)
# ---------------------------------------------------------------------------
def test_exactly_one_record_appended_per_scored_instance(tmp_path):
    engine = _engine(tmp_path)
    assert engine.store.read_all() == []

    inst = _score_one(engine)
    stored = engine.store.read_all()
    assert len(stored) == 1
    assert stored[0] == inst

    _score_one(engine, instance_id="inst-2")
    assert len(engine.store.read_all()) == 2


def test_appended_record_round_trips_through_store(tmp_path):
    engine = _engine(tmp_path)
    inst = _score_one(engine)
    # a fresh reader over the same path sees exactly the appended record.
    reader = EvalEventStore(engine.store.path)
    restored = reader.read_all()
    assert restored == [inst]


# ---------------------------------------------------------------------------
# recorded values unaffected by any weight set (Req 1.6, 3.3)
# ---------------------------------------------------------------------------
def test_engine_holds_no_weight_set_and_stores_raw_values(tmp_path):
    engine = _engine(tmp_path)
    inst = _score_one(engine)

    # the recorded values are exactly what the adapter + computer produced,
    # independent of any composite weighting (the engine never sees a weight set).
    expected_ragas = engine.ragas_adapter.score(_sample())
    assert {n: mv.value for n, mv in inst.ragas.items()} == {
        n: mv.value for n, mv in expected_ragas.items()
    }
    expected_retrieval = engine.retrieval_computer.compute(
        ["f1", "f2", "f3", "f4"], ["f2", "f9"]
    )
    assert {n: mv.value for n, mv in inst.retrieval.items()} == {
        n: mv.value for n, mv in expected_retrieval.items()
    }

    # the engine exposes no weighting surface that could alter recorded values.
    assert not hasattr(engine, "weight_set")
    assert not hasattr(engine, "composite")


# ---------------------------------------------------------------------------
# additive / judge-neutral (Req 18.1) and read-only retrieval (Req 19.1)
# ---------------------------------------------------------------------------
def test_scoring_is_additive_and_makes_no_network_call(tmp_path, monkeypatch):
    engine = _engine(tmp_path)

    def _boom(*args, **kwargs):  # pragma: no cover - only runs if violated
        raise AssertionError("metric engine scoring must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    inst = _score_one(engine)
    assert inst.status == "ok"
    assert len(engine.store.read_all()) == 1


def test_no_gold_yields_unavailable_retrieval_but_keeps_ragas(tmp_path):
    engine = _engine(tmp_path)
    inst = _score_one(engine, instance_id="inst-nogold", gold_ids=[])
    # retrieval metrics are unavailable (no Gold_Link) ...
    assert all(mv.unavailable for mv in inst.retrieval.values())
    # ... while ragas generation-quality metrics are still computed (Req 2.3, 1).
    assert any(not mv.unavailable for mv in inst.ragas.values())


def test_failed_execution_records_instance_with_empty_metric_maps(tmp_path):
    engine = _engine(tmp_path)
    # a failed execution has no answer to score and no retrieval to measure.
    inst = engine.score_instance(
        instance_id="inst-failed",
        agent_id="agent-A",
        session_id="sess-1",
        instance_index=1,
        timestamp="2025-01-01T00:00:05Z",
        latency_ms=5.0,
        corpus_size=1000,
        ragas_sample=None,
        ranked_ids=None,
        gold_ids=None,
        status="failed",
        error="ThrottlingException after retries",
    )
    assert inst.status == "failed"
    assert inst.ragas == {}
    assert inst.retrieval == {}
    assert len(engine.store.read_all()) == 1

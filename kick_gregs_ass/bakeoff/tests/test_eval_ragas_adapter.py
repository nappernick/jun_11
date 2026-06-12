"""
Unit tests for :mod:`bakeoff.eval.ragas_adapter` (Task 2.6).

Covers the Ragas_Adapter offline-mode contract (Req 1):

* the module imports with ragas absent (the guarded import) and offline mode
  computes values with ZERO network calls (Req 1.5);
* one failing metric does not drop the others — it is recorded unavailable while
  every successful metric is retained (Req 1.4);
* provenance (metric name, value, ragas version, Bedrock model id) is recorded
  on every value, available or unavailable (Req 1.2);
* values are on the 0.0–1.0 scale and computation is deterministic (Req 1.3).

Network-free: the offline path never touches ragas or the network.
"""
from __future__ import annotations

import socket

import pytest

from bakeoff.eval import ragas_adapter as ra
from bakeoff.eval.ragas_adapter import (
    FakeEmbedding,
    FakeLLM,
    RagasAdapter,
    RagasNotInstalledError,
    RagasSample,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _sample() -> RagasSample:
    return RagasSample(
        question="What is the capital of France?",
        answer="The capital of France is Paris.",
        contexts=["France is a country in Europe. Its capital city is Paris."],
        reference="Paris is the capital of France.",
    )


_METRICS = [
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "semantic_similarity",
]


# ---------------------------------------------------------------------------
# guarded import: module loads without ragas; offline makes no network call
# ---------------------------------------------------------------------------
def test_module_imports_without_ragas_present():
    # ragas is NOT installed in this environment; the guarded import must have
    # left the module fully usable with the availability flag set to False.
    assert ra.RAGAS_AVAILABLE is False


def test_offline_score_issues_no_network_call(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - only runs if violated
        raise AssertionError("offline ragas scoring must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    adapter = RagasAdapter.offline(enabled_metrics=_METRICS)
    out = adapter.score(_sample())
    assert set(out) == set(_METRICS)
    for mv in out.values():
        assert mv.value is not None
        assert 0.0 <= mv.value <= 1.0


def test_offline_is_the_default_mode():
    adapter = RagasAdapter()
    assert adapter.mode == "offline"
    assert adapter.live is False
    # the default enabled set is the in-scope catalog metrics.
    assert adapter.enabled_metrics, "default enabled metrics must be non-empty"


# ---------------------------------------------------------------------------
# one failing metric does not drop the others (Req 1.4)
# ---------------------------------------------------------------------------
class _FailOneLLM(FakeLLM):
    """A fake LLM that raises for exactly one metric, scores the rest normally."""

    def __init__(self, fail_metric: str) -> None:
        self.fail_metric = fail_metric

    def score(self, metric_name, sample):
        if metric_name == self.fail_metric:
            raise RuntimeError("simulated ragas metric failure")
        return super().score(metric_name, sample)


def test_one_failing_metric_does_not_drop_the_others():
    adapter = RagasAdapter.offline(
        enabled_metrics=["faithfulness", "answer_relevancy", "context_precision"],
        llm=_FailOneLLM("answer_relevancy"),
    )
    out = adapter.score(_sample())

    # the failing metric is recorded unavailable...
    failed = out["answer_relevancy"]
    assert failed.unavailable is True
    assert failed.value is None
    # ...while every other metric is retained with a real value.
    for name in ("faithfulness", "context_precision"):
        assert out[name].unavailable is False
        assert out[name].value is not None


def test_metric_returning_none_is_recorded_unavailable():
    class _NoneLLM(FakeLLM):
        def score(self, metric_name, sample):
            if metric_name == "faithfulness":
                return None  # type: ignore[return-value]
            return super().score(metric_name, sample)

    adapter = RagasAdapter.offline(
        enabled_metrics=["faithfulness", "context_precision"], llm=_NoneLLM()
    )
    out = adapter.score(_sample())
    assert out["faithfulness"].unavailable is True
    assert out["context_precision"].value is not None


# ---------------------------------------------------------------------------
# provenance recorded on every value, available and unavailable (Req 1.2)
# ---------------------------------------------------------------------------
def test_provenance_recorded_on_every_value():
    adapter = RagasAdapter.offline(
        enabled_metrics=_METRICS,
        ragas_version="0.2.1",
        bedrock_model_id="us.anthropic.claude-opus-4-8",
    )
    out = adapter.score(_sample())
    for name, mv in out.items():
        assert mv.ragas_version == "0.2.1", f"{name} missing ragas_version"
        assert mv.bedrock_model_id == "us.anthropic.claude-opus-4-8", (
            f"{name} missing bedrock_model_id"
        )


def test_failed_metric_still_carries_provenance():
    adapter = RagasAdapter.offline(
        enabled_metrics=["faithfulness", "answer_relevancy"],
        llm=_FailOneLLM("faithfulness"),
        ragas_version="0.2.1",
        bedrock_model_id="model-x",
    )
    out = adapter.score(_sample())
    failed = out["faithfulness"]
    assert failed.unavailable is True
    # provenance is retained even though the value is unavailable (Req 1.2, 1.4).
    assert failed.ragas_version == "0.2.1"
    assert failed.bedrock_model_id == "model-x"


def test_offline_mode_marks_its_provenance_honestly():
    adapter = RagasAdapter.offline(enabled_metrics=["faithfulness"])
    out = adapter.score(_sample())
    mv = out["faithfulness"]
    assert mv.ragas_version == ra.OFFLINE_RAGAS_VERSION
    assert mv.bedrock_model_id == ra.OFFLINE_BEDROCK_MODEL_ID


# ---------------------------------------------------------------------------
# determinism + scale (Req 1.3, 1.5)
# ---------------------------------------------------------------------------
def test_offline_scoring_is_deterministic():
    a1 = RagasAdapter.offline(enabled_metrics=_METRICS)
    a2 = RagasAdapter.offline(enabled_metrics=_METRICS)
    sample = _sample()
    out1 = a1.score(sample)
    out2 = a2.score(sample)
    assert {n: mv.value for n, mv in out1.items()} == {
        n: mv.value for n, mv in out2.items()
    }


def test_all_values_in_unit_interval():
    adapter = RagasAdapter.offline(enabled_metrics=_METRICS)
    out = adapter.score(_sample())
    for mv in out.values():
        assert mv.value is None or 0.0 <= mv.value <= 1.0


def test_grounded_answer_scores_higher_than_ungrounded():
    # a well-grounded answer should score higher on faithfulness than an answer
    # that shares no content with the contexts (sanity that the fake is meaningful).
    adapter = RagasAdapter.offline(enabled_metrics=["faithfulness"])
    grounded = adapter.score(_sample())["faithfulness"].value
    ungrounded = adapter.score(
        RagasSample(
            question="What is the capital of France?",
            answer="Bananas grow on tropical plantations worldwide.",
            contexts=["France is a country in Europe. Its capital city is Paris."],
            reference="Paris is the capital of France.",
        )
    )["faithfulness"].value
    assert grounded > ungrounded


# ---------------------------------------------------------------------------
# the live Bedrock path is guarded and never reachable without ragas
# ---------------------------------------------------------------------------
def test_live_mode_without_ragas_raises_on_construction():
    # ragas is absent here, so requesting the live path must fail loudly rather
    # than silently degrade — and it must NOT happen for the offline default.
    with pytest.raises(RagasNotInstalledError):
        RagasAdapter(mode="bedrock", live=True)


def test_embedding_similarity_is_pure():
    emb = FakeEmbedding()
    s = "the capital of france is paris"
    assert emb.similarity(s, s) == pytest.approx(1.0)
    assert 0.0 <= emb.similarity("a b c", "c d e") <= 1.0

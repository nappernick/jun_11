"""
Tier-1 ragas seam tests (spec: optimizer-ragas-gepa) — all offline, network-free.

Covers the load-bearing Tier-1 properties:
  * FakeRagasAdapter is deterministic and gold-presence is exact set intersection (Req 2.3 / 5.1).
  * build_ragas_adapter selects fake/bedrock and rejects unknown (Req 4.1 / 5.1).
  * BedrockRagasAdapter degrades to None signals when ragas is absent — never crashes (Req 3.5).
  * JudgeInLoopScorer config-off parity: with both flags off the ragas signals are None and the
    adapter is never consulted (Req 3.2 / 3.3 / 17).
  * JudgeInLoopScorer flags-on: signals populated from the SAME fragments/answer (Req 1 / 2),
    and a raising adapter is swallowed to None (Req 3.5) — `overall` is never affected.

None of these require network, Bedrock, or the `ragas` package to be installed.
"""
from __future__ import annotations

import asyncio

import pytest

from bakeoff.quality.optimizer.backends import build_offline_backend
from bakeoff.quality.optimizer.judge_loop import JudgeInLoopScorer
from bakeoff.quality.optimizer.ragas_adapter import (
    BedrockRagasAdapter,
    FakeRagasAdapter,
    RagasAdapter,
    RagasSignals,
    build_ragas_adapter,
)
from bakeoff.quality.types import GroundTruthKind

_FRAGS = [
    {"id": "n1", "text": "Book travel in the Concur portal and submit within 30 days."},
    {"id": "n2", "text": "Visa support is handled by the mobility team."},
]
_REF = ["Use Concur to book travel; submit expenses within 30 days."]


def _run(coro):
    return asyncio.run(coro)


# --- FakeRagasAdapter -------------------------------------------------------
def test_fake_adapter_is_runtime_ragas_adapter():
    a = build_ragas_adapter("fake")
    assert isinstance(a, RagasAdapter)
    assert a.name == "fake"


def test_fake_cross_check_deterministic_and_in_range():
    a = FakeRagasAdapter()
    f1, fc1 = _run(a.cross_check(answer_text="book travel in concur", fragments=_FRAGS, reference_texts=_REF, question="q"))
    f2, fc2 = _run(a.cross_check(answer_text="book travel in concur", fragments=_FRAGS, reference_texts=_REF, question="q"))
    assert (f1, fc1) == (f2, fc2)  # deterministic
    assert 0.0 <= f1 <= 1.0 and 0.0 <= fc1 <= 1.0


def test_fake_gold_presence_exact_set_intersection():
    a = FakeRagasAdapter()
    # gold node present in fragments -> True
    _, _, present = _run(a.retrieval_diagnostic(fragments=_FRAGS, reference_texts=_REF, gold_node_ids=["n1"]))
    assert present is True
    # gold node absent -> False
    _, _, absent = _run(a.retrieval_diagnostic(fragments=_FRAGS, reference_texts=_REF, gold_node_ids=["MISSING"]))
    assert absent is False
    # no gold node id (later wants-only turn) -> None (Req 2.3)
    _, _, none_present = _run(a.retrieval_diagnostic(fragments=_FRAGS, reference_texts=_REF, gold_node_ids=[]))
    assert none_present is None


def test_build_ragas_adapter_rejects_unknown():
    with pytest.raises(ValueError):
        build_ragas_adapter("nope")


def test_bedrock_adapter_degrades_to_none_when_ragas_absent():
    # ragas is not installed in the offline env; the live adapter must NEVER crash — each metric
    # is swallowed to None (Req 3.5). This asserts graceful degradation, not a live ragas call.
    b = build_ragas_adapter("bedrock")
    assert isinstance(b, BedrockRagasAdapter)
    f, fc = _run(b.cross_check(answer_text="x", fragments=_FRAGS, reference_texts=_REF))
    cp, cr, gp = _run(b.retrieval_diagnostic(fragments=_FRAGS, reference_texts=_REF, gold_node_ids=["n1"]))
    assert f is None and fc is None and cp is None and cr is None
    # gold-presence is a pure set check (no ragas call) so it still resolves:
    assert gp is True


# --- JudgeInLoopScorer gating ----------------------------------------------
def test_scorer_config_off_parity_signals_none():
    backend = build_offline_backend()
    scorer = JudgeInLoopScorer(backend, reps=1)  # both flags default OFF
    sig = _run(scorer._ragas_signals(
        answer_text="book travel in concur", fragments=_FRAGS, reference_texts=_REF,
        gold_node_ids=["n1"], ground_truth_kind=GroundTruthKind.GOLD, question="q",
    ))
    assert sig == RagasSignals()  # all None — adapter never consulted


def test_scorer_flags_on_populates_signals():
    backend = build_offline_backend()
    scorer = JudgeInLoopScorer(backend, reps=1, ragas_cross_check=True, retrieval_diagnostic=True)
    sig = _run(scorer._ragas_signals(
        answer_text="book travel in the Concur portal; submit within 30 days",
        fragments=_FRAGS, reference_texts=_REF, gold_node_ids=["n1"],
        ground_truth_kind=GroundTruthKind.GOLD, question="q",
    ))
    assert sig.backend == "fake"
    assert sig.faithfulness is not None and 0.0 <= sig.faithfulness <= 1.0
    assert sig.factual_correctness is not None
    assert sig.context_precision is not None and sig.context_recall is not None
    assert sig.gold_node_present is True


def test_scorer_only_cross_check_flag():
    backend = build_offline_backend()
    scorer = JudgeInLoopScorer(backend, reps=1, ragas_cross_check=True, retrieval_diagnostic=False)
    sig = _run(scorer._ragas_signals(
        answer_text="book travel in concur", fragments=_FRAGS, reference_texts=_REF,
        gold_node_ids=["n1"], ground_truth_kind=GroundTruthKind.GOLD, question="q",
    ))
    assert sig.faithfulness is not None          # cross-check ran
    assert sig.context_precision is None          # diagnostic did not
    assert sig.gold_node_present is None


def test_scorer_failure_tolerant():
    # A raising adapter must be swallowed to None signals (Req 3.5), never propagate.
    class _BoomAdapter:
        name = "boom"

        async def cross_check(self, **_):
            raise RuntimeError("boom")

        async def retrieval_diagnostic(self, **_):
            raise RuntimeError("boom")

    backend = build_offline_backend()
    object.__setattr__(backend, "ragas_adapter", _BoomAdapter())  # frozen dataclass
    scorer = JudgeInLoopScorer(backend, reps=1, ragas_cross_check=True, retrieval_diagnostic=True)
    sig = _run(scorer._ragas_signals(
        answer_text="x", fragments=_FRAGS, reference_texts=_REF, gold_node_ids=["n1"],
        ground_truth_kind=GroundTruthKind.GOLD, question="q",
    ))
    assert sig.faithfulness is None and sig.context_precision is None

"""
Unit tests for :mod:`bakeoff.eval.retrieval_metrics` (Task 2.4).

Covers the Retrieval_Metric_Computer contract (Req 2):

* ``k`` is recorded on every produced value, available or unavailable (Req 2.2);
* a query with no resolvable Gold_Link ⟹ all retrieval metrics unavailable
  (Req 2.3);
* precision@k and recall@k are stored independently, neither derived from the
  other (Req 2.6);
* the computer delegates to :mod:`bakeoff.scoring.retrieval_aligned` rather than
  reinventing the formulas (results match the delegated functions exactly);
* computation issues no network call and never mutates its inputs / the
  substrate (Req 2.5, 19.1).

Network-free.
"""
from __future__ import annotations

import socket

import pytest

from bakeoff import config
from bakeoff.eval.retrieval_metrics import (
    RETRIEVAL_METRIC_NAMES,
    RetrievalMetricComputer,
    compute_retrieval_metrics,
)
from bakeoff.scoring.retrieval_aligned import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)


# ---------------------------------------------------------------------------
# k recorded on every value (Req 2.2)
# ---------------------------------------------------------------------------
def test_k_recorded_on_every_available_value():
    ranked = ["a", "b", "c", "d", "e"]
    gold = ["b", "d"]
    out = compute_retrieval_metrics(ranked, gold, k=3)
    assert set(out) == set(RETRIEVAL_METRIC_NAMES)
    for name, mv in out.items():
        assert mv.k == 3, f"{name} must record the k used"
        assert mv.unavailable is False
        assert 0.0 <= mv.value <= 1.0


def test_k_defaults_to_scoring_k():
    out = compute_retrieval_metrics(["a", "b"], ["a"])
    for mv in out.values():
        assert mv.k == config.SCORING_K


def test_computer_instance_default_k_is_used():
    computer = RetrievalMetricComputer(k=2)
    out = computer.compute(["a", "b", "c"], ["c"])
    for mv in out.values():
        assert mv.k == 2
    # a per-call override wins over the instance default.
    out2 = computer.compute(["a", "b", "c"], ["c"], k=1)
    for mv in out2.values():
        assert mv.k == 1


# ---------------------------------------------------------------------------
# no-gold ⟹ every retrieval metric unavailable (Req 2.3)
# ---------------------------------------------------------------------------
def test_no_gold_makes_all_metrics_unavailable():
    out = compute_retrieval_metrics(["a", "b", "c"], [], k=5)
    assert set(out) == set(RETRIEVAL_METRIC_NAMES)
    for name, mv in out.items():
        assert mv.unavailable is True, f"{name} must be unavailable with no gold"
        assert mv.value is None
        # k is still recorded even when the metric is unavailable (Req 2.2).
        assert mv.k == 5


def test_no_gold_via_computer_class():
    computer = RetrievalMetricComputer(k=4)
    out = computer.compute(["a", "b"], gold_ids=[])
    assert all(mv.unavailable for mv in out.values())
    assert all(mv.k == 4 for mv in out.values())


# ---------------------------------------------------------------------------
# precision and recall stored independently (Req 2.6)
# ---------------------------------------------------------------------------
def test_precision_and_recall_both_stored_and_distinct():
    # ranked top-3 contains 1 of 2 gold -> precision@3 = 1/3, recall@3 = 1/2.
    ranked = ["a", "b", "c", "d"]
    gold = ["c", "z"]  # only "c" is retrieved; "z" never appears
    out = compute_retrieval_metrics(ranked, gold, k=3)
    p = out["precision_at_k"]
    r = out["recall_at_k"]
    assert p.value == pytest.approx(1 / 3)
    assert r.value == pytest.approx(1 / 2)
    # the two are genuinely distinct signals, not one derived from the other.
    assert p.value != r.value


# ---------------------------------------------------------------------------
# delegation: results match the existing scorer exactly (reuse, not reinvent)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ranked, gold, k",
    [
        (["a", "b", "c", "d", "e"], ["b", "d"], 5),
        (["x", "y", "z"], ["y"], 2),
        (["a", "b", "c"], ["a", "b", "c"], 3),
        (["a", "b", "c"], ["q"], 5),  # gold present but never retrieved
        ([], ["a"], 5),               # nothing retrieved
    ],
)
def test_delegates_to_retrieval_aligned(ranked, gold, k):
    out = compute_retrieval_metrics(ranked, gold, k=k)
    assert out["precision_at_k"].value == pytest.approx(precision_at_k(ranked, gold, k))
    assert out["recall_at_k"].value == pytest.approx(recall_at_k(ranked, gold, k))
    assert out["ndcg_at_k"].value == pytest.approx(ndcg_at_k(ranked, gold, k))


# ---------------------------------------------------------------------------
# read-only + no network (Req 2.5, 19.1)
# ---------------------------------------------------------------------------
def test_compute_issues_no_network_call(monkeypatch):
    # any attempt to open a socket during computation is a hard failure.
    def _boom(*args, **kwargs):  # pragma: no cover - only runs if violated
        raise AssertionError("retrieval metric computation must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    out = compute_retrieval_metrics(["a", "b", "c"], ["b"], k=3)
    assert out["recall_at_k"].value == pytest.approx(1.0)


def test_compute_does_not_mutate_its_inputs():
    ranked = ["a", "b", "c"]
    gold = ["b"]
    ranked_before = list(ranked)
    gold_before = list(gold)
    compute_retrieval_metrics(ranked, gold, k=2)
    # inputs are read-only: the computer never mutates the caller's lists.
    assert ranked == ranked_before
    assert gold == gold_before

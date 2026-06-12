"""Tests for bakeoff.decide — hand-built cells covering gate logic."""
import sys
from pathlib import Path

# Ensure bakeoff package is importable from this test location.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bakeoff.contract import Gates
from bakeoff.decide import pareto_frontier, recommend


# ---------------------------------------------------------------------------
# pareto_frontier
# ---------------------------------------------------------------------------

def test_pareto_simple():
    """Point A dominates B; C is non-dominated."""
    points = [
        {"id": "A", "cost": 0.3, "ndcg": 0.9},
        {"id": "B", "cost": 0.5, "ndcg": 0.8},  # dominated by A
        {"id": "C", "cost": 0.2, "ndcg": 0.7},  # not dominated (cheaper, lower ndcg)
    ]
    front = pareto_frontier(points)
    ids = {p["id"] for p in front}
    assert ids == {"A", "C"}


def test_pareto_all_nondominated():
    points = [
        {"id": "X", "cost": 1.0, "ndcg": 0.95},
        {"id": "Y", "cost": 0.5, "ndcg": 0.80},
    ]
    assert len(pareto_frontier(points)) == 2


def test_pareto_empty():
    assert pareto_frontier([]) == []


# ---------------------------------------------------------------------------
# recommend — clear winner
# ---------------------------------------------------------------------------

def _make_cell(model_id, ndcg, p99, far, cost):
    return {
        "model_id": model_id,
        "N": 10,
        "by_slice": {
            "all": {
                "ndcg10": ndcg,
                "p50": p99 * 0.5,
                "p95": p99 * 0.8,
                "p99": p99,
                "cost_per_1k": cost,
                "abstain": {
                    "operating_t": 0.3,
                    "recall": 0.8,
                    "false_answer_rate": far,
                    "false_abstain_rate": 0.1,
                },
            }
        },
    }


def test_recommend_clear_winner():
    """Model A passes all gates and is cheapest."""
    cells = [
        _make_cell("model-a", ndcg=0.85, p99=200, far=0.03, cost=0.40),
        _make_cell("model-b", ndcg=0.60, p99=150, far=0.02, cost=0.30),  # fails ndcg
    ]
    gates = Gates(accuracy_bar=0.70, latency_budget_ms=300, false_answer_ceiling=0.05)
    assert recommend(cells, "all", gates) == "model-a"


def test_recommend_tie_broken_by_cost():
    """Two models pass all gates — cheapest wins."""
    cells = [
        _make_cell("expensive", ndcg=0.90, p99=100, far=0.02, cost=1.00),
        _make_cell("cheap", ndcg=0.80, p99=150, far=0.04, cost=0.25),
    ]
    gates = Gates(accuracy_bar=0.70, latency_budget_ms=300, false_answer_ceiling=0.05)
    assert recommend(cells, "all", gates) == "cheap"


def test_recommend_none_eligible():
    """No model satisfies all gates — returns None (a valid finding)."""
    cells = [
        _make_cell("too-slow", ndcg=0.85, p99=600, far=0.03, cost=0.40),
        _make_cell("too-inaccurate", ndcg=0.50, p99=100, far=0.02, cost=0.20),
        _make_cell("too-risky", ndcg=0.85, p99=200, far=0.10, cost=0.30),
    ]
    gates = Gates(accuracy_bar=0.70, latency_budget_ms=300, false_answer_ceiling=0.05)
    result = recommend(cells, "all", gates)
    assert result is None


def test_recommend_missing_slice():
    """If slice doesn't exist in a cell, that cell is skipped."""
    cells = [_make_cell("model-a", ndcg=0.85, p99=200, far=0.03, cost=0.40)]
    gates = Gates(accuracy_bar=0.70, latency_budget_ms=300, false_answer_ceiling=0.05)
    assert recommend(cells, "nonexistent_slice", gates) is None

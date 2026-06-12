"""bakeoff.decide — Stage 4 DECIDE: gate filtering + Pareto recommendation.

Pure stdlib. No external dependencies.

Functions:
    pareto_frontier: compute non-dominated set (minimize cost, maximize ndcg).
    recommend: apply gates, return cheapest eligible model_id or None.
"""
from __future__ import annotations

from .contract import Gates


def pareto_frontier(points: list[dict]) -> list[dict]:
    """Return the Pareto-optimal subset (minimize cost, maximize ndcg).

    A point is dominated if another point exists with <= cost AND >= ndcg,
    with at least one strict inequality.
    """
    frontier = []
    for p in points:
        dominated = False
        for q in points:
            if q is p:
                continue
            if q["cost"] <= p["cost"] and q["ndcg"] >= p["ndcg"]:
                if q["cost"] < p["cost"] or q["ndcg"] > p["ndcg"]:
                    dominated = True
                    break
        if not dominated:
            frontier.append(p)
    return frontier


def recommend(cells: list[dict], slice_name: str, gates: Gates) -> str | None:
    """Select the cheapest model that satisfies all gates for a given slice.

    Eligibility requires ALL of:
        - ndcg10 >= gates.accuracy_bar
        - p99 <= gates.latency_budget_ms
        - false_answer_rate (at operating threshold) <= gates.false_answer_ceiling

    Returns the model_id of the cheapest eligible model by cost_per_1k,
    or None when no model qualifies.  None is a valid, meaningful finding —
    it means the gate bar is set above what any evaluated model can achieve,
    and human review of whether to relax gates or evaluate more models is needed.
    """
    eligible: list[tuple[float, str]] = []
    for cell in cells:
        slc = cell.get("by_slice", {}).get(slice_name)
        if slc is None:
            continue
        if slc["ndcg10"] < gates.accuracy_bar:
            continue
        if slc["p99"] > gates.latency_budget_ms:
            continue
        abstain = slc.get("abstain", {})
        if abstain.get("false_answer_rate", 1.0) > gates.false_answer_ceiling:
            continue
        eligible.append((slc["cost_per_1k"], cell["model_id"]))

    if not eligible:
        return None
    eligible.sort(key=lambda x: x[0])
    return eligible[0][1]

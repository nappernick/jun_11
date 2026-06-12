"""bakeoff.metrics — SCORE-aggregation (stage 2) and AGGREGATE (stage 3).

Pure stdlib + random. Encodes the design-review HARD RULES:
  (1) nDCG over answerable queries only (expect_abstain=False)
  (2) recall conditional on gold_retrievable > 0
  (3) THREE-WAY abstain_class: answerable_not_retrieved is its own bucket
  (4) significance via paired bootstrap over matched queries
  (5) abstention reported as a full curve
"""
from __future__ import annotations

import math
import random
from typing import Sequence

from bakeoff.contract import AbstainPoint, ScoredRow


# ---------------------------------------------------------------------------
# IR metrics (operate on a single query's binary relevance vector)
# ---------------------------------------------------------------------------

def ndcg_at_k(rels: Sequence[int], k: int) -> float:
    """Normalized DCG@k. rels[i]=1 if rank-i doc is relevant, else 0."""
    rels_k = list(rels[:k])
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rels_k))
    ideal = sorted(rels_k, reverse=True)
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def recall_at_k(rels: Sequence[int], k: int, gold_retrievable: int) -> float:
    """Recall@k conditional on gold_retrievable > 0. Returns 0.0 if none retrievable."""
    if gold_retrievable <= 0:
        return 0.0
    return sum(rels[:k]) / gold_retrievable


def mrr_at_k(rels: Sequence[int], k: int) -> float:
    """Mean Reciprocal Rank@k — reciprocal rank of first relevant doc."""
    for i, r in enumerate(rels[:k]):
        if r:
            return 1.0 / (i + 1)
    return 0.0


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def bootstrap_ci(values: Sequence[float], iters: int = 2000, seed: int = 0) -> tuple[float, float]:
    """95% bootstrap confidence interval (percentile method)."""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(rng.choices(values, k=n)) / n for _ in range(iters)
    )
    lo = means[int(iters * 0.025)]
    hi = means[int(iters * 0.975)]
    return (lo, hi)


def paired_bootstrap(a_vals: Sequence[float], b_vals: Sequence[float],
                     iters: int = 2000, seed: int = 0) -> float:
    """Two-sided paired bootstrap test. Returns p-value.

    Tests H0: mean(a) == mean(b) over matched queries.
    Method: resample paired differences under H0 (center at 0).
    """
    assert len(a_vals) == len(b_vals), "paired bootstrap requires matched queries"
    n = len(a_vals)
    if n == 0:
        return 1.0
    diffs = [a_vals[i] - b_vals[i] for i in range(n)]
    obs_diff = abs(sum(diffs) / n)
    # Center differences at 0 for null distribution
    mean_diff = sum(diffs) / n
    centered = [d - mean_diff for d in diffs]
    rng = random.Random(seed)
    count = 0
    for _ in range(iters):
        sample = rng.choices(centered, k=n)
        if abs(sum(sample) / n) >= obs_diff:
            count += 1
    return count / iters


# ---------------------------------------------------------------------------
# Abstention curve (three-way semantics)
# ---------------------------------------------------------------------------

def abstention_curve(rows: Sequence[ScoredRow], thresholds: Sequence[float]) -> list[AbstainPoint]:
    """Compute the abstention operating-characteristic curve.

    Positive class = system abstains (top_norm < t).

    Three-way buckets:
      - unanswerable (expect_abstain=True): TP=abstained, FN=answered
      - answerable_retrievable: FP=abstained, TN=answered
      - answerable_not_retrieved: correct abstain is NOT a false-abstain;
        these are tracked separately and excluded from the FP pool.

    Emits: abstain_recall (TP / (TP + FN)),
           false_answer_rate (FN / (FN + TP)) = fraction of should-abstain that we answered,
           false_abstain_rate (FP / (FP + TN)) = fraction of answerable_retrievable we wrongly abstained.
    """
    unanswerable = [r for r in rows if r.abstain_class == "unanswerable"]
    answerable_retrievable = [r for r in rows if r.abstain_class == "answerable_retrievable"]
    # answerable_not_retrieved tracked but NOT folded into FP pool

    curve: list[AbstainPoint] = []
    for t in sorted(thresholds):
        # Among unanswerable: abstain (top_norm < t) = TP, answer = FN
        tp = sum(1 for r in unanswerable if r.top_norm < t)
        fn = len(unanswerable) - tp

        # Among answerable_retrievable: abstain = FP, answer = TN
        fp = sum(1 for r in answerable_retrievable if r.top_norm < t)
        tn = len(answerable_retrievable) - fp

        abstain_recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        false_answer_rate = fn / (fn + tp) if (fn + tp) > 0 else 0.0
        false_abstain_rate = fp / (fp + tn) if (fp + tn) > 0 else 0.0

        curve.append(AbstainPoint(
            t=t,
            abstain_recall=abstain_recall,
            false_answer_rate=false_answer_rate,
            false_abstain_rate=false_abstain_rate,
        ))
    return curve


def operating_point(curve: Sequence[AbstainPoint], false_answer_ceiling: float) -> float:
    """Find threshold t that maximizes abstain_recall subject to false_answer_rate <= ceiling."""
    best_t = 0.0
    best_recall = -1.0
    for pt in curve:
        if pt.false_answer_rate <= false_answer_ceiling and pt.abstain_recall > best_recall:
            best_recall = pt.abstain_recall
            best_t = pt.t
    return best_t


# ---------------------------------------------------------------------------
# AGGREGATE (stage 3) — produces a by_slice cell dict
# ---------------------------------------------------------------------------

def aggregate(rows: Sequence[ScoredRow], baseline_rows: Sequence[ScoredRow],
              k: int = 10) -> dict:
    """Aggregate scored rows into the by_slice cell shape from contract.py.

    HARD RULES enforced:
      (1) nDCG only over answerable (expect_abstain=False)
      (2) recall conditional on gold_retrievable > 0
      (3) three-way abstain_class
      (4) paired bootstrap significance vs baseline
      (5) full abstain curve
    """
    # Group rows by slice key
    slices: dict[str, list[ScoredRow]] = {}
    for r in rows:
        key = "&".join(f"{k2}={v}" for k2, v in sorted(r.slice.items()))
        slices.setdefault(key, []).append(r)

    baseline_by_qid = {r.query_id: r for r in baseline_rows}

    by_slice: dict[str, dict] = {}
    for slice_key, srows in slices.items():
        # nDCG: only answerable queries
        answerable = [r for r in srows if not r.expect_abstain]
        ndcg_vals = [ndcg_at_k(r.rels, k) for r in answerable] if answerable else []
        ndcg10 = sum(ndcg_vals) / len(ndcg_vals) if ndcg_vals else 0.0
        ndcg10_ci = bootstrap_ci(ndcg_vals) if ndcg_vals else (0.0, 0.0)

        # Recall: conditional on gold_retrievable > 0
        recall_eligible = [r for r in answerable if r.gold_retrievable > 0]
        recall_vals = [recall_at_k(r.rels, k, r.gold_retrievable) for r in recall_eligible]
        recall10 = sum(recall_vals) / len(recall_vals) if recall_vals else 0.0

        # MRR
        mrr_vals = [mrr_at_k(r.rels, k) for r in answerable] if answerable else []
        mrr10 = sum(mrr_vals) / len(mrr_vals) if mrr_vals else 0.0

        # Latency percentiles
        latencies = sorted(r.latency_ms for r in srows)
        n = len(latencies)
        p50 = latencies[n // 2] if n else 0.0
        p95 = latencies[int(n * 0.95)] if n else 0.0
        p99 = latencies[int(n * 0.99)] if n else 0.0

        # Significance vs baseline (paired bootstrap on nDCG over matched queries)
        baseline_ndcg = []
        model_ndcg = []
        for r in answerable:
            br = baseline_by_qid.get(r.query_id)
            if br and not br.expect_abstain:
                model_ndcg.append(ndcg_at_k(r.rels, k))
                baseline_ndcg.append(ndcg_at_k(br.rels, k))
        sig = paired_bootstrap(model_ndcg, baseline_ndcg) if model_ndcg else 1.0

        # Abstention curve (full) — use all rows in this slice
        thresholds = [i / 20.0 for i in range(1, 20)]  # 0.05..0.95 step 0.05
        curve = abstention_curve(srows, thresholds)

        # Operating point
        op_t = operating_point(curve, 0.05)  # default 5% false_answer_ceiling
        op_point = next((p for p in curve if p.t == op_t), None)

        abstain_dict = {
            "operating_t": op_t,
            "recall": op_point.abstain_recall if op_point else 0.0,
            "false_answer_rate": op_point.false_answer_rate if op_point else 0.0,
            "false_abstain_rate": op_point.false_abstain_rate if op_point else 0.0,
        }

        # Three-way counts for transparency
        n_unanswerable = sum(1 for r in srows if r.abstain_class == "unanswerable")
        n_answerable_retrievable = sum(1 for r in srows if r.abstain_class == "answerable_retrievable")
        n_answerable_not_retrieved = sum(1 for r in srows if r.abstain_class == "answerable_not_retrieved")

        by_slice[slice_key] = {
            "ndcg10": ndcg10,
            "ndcg10_ci": list(ndcg10_ci),
            "recall10": recall10,
            "mrr10": mrr10,
            "p50": p50,
            "p95": p95,
            "p99": p99,
            "throughput_qps": 0.0,  # stub — needs wall-clock from harness
            "cost_per_1k": 0.0,  # stub — needs pricing from model meta
            "sig_vs_baseline": sig,
            "abstain": abstain_dict,
            "abstain_curve": curve,
            "three_way_counts": {
                "unanswerable": n_unanswerable,
                "answerable_retrievable": n_answerable_retrievable,
                "answerable_not_retrieved": n_answerable_not_retrieved,
            },
        }

    return by_slice

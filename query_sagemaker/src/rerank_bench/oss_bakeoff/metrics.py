#!/usr/bin/env python3
"""metrics.py — derive the bakeoff metrics.json from scored.json (pure stdlib).

Two jobs, both off the frozen per-model scores (no model loading, no AWS):

  1. per_model rigor numbers — separation + top-1 confidence + latency + context.
     Separation/confidence are computed on the NORMALIZED score (squash already
     applied in scored.json, kind-aware), never on raw: raw lives on three
     incompatible scales (logit / margin / unit) so a cross-model number on raw
     would be meaningless. norm is the comparable [0,1].

  2. the disagreement set — EVERY (query, model_a, model_b) where the two models'
     top_id differs, OSS-vs-OSS and OSS-vs-Cohere alike, each with both full
     rankings. This is the JUDGE INPUT, so ids (d0001..) must be deterministic:
     models in sorted() order, pairs i<j on that order, queries in sorted order,
     and every pair's a/b is the lexicographically-first model of the pair.

combo_stability is the SECONDARY, analytic separation read. By the contract's hard
rule a combo's ranking is just the restriction of the full-pool ordering to a
subset, so combos add no ranking signal. The analytic top-1 win-rate for a doc at
rank r (1-based) in a query's full-pool ordering is the probability it tops a
random subset that contains it = it wins iff none of the r-1 docs ranked above it
are also drawn = 2^(1-r). Per node_id we average 2^(1-rank) over the queries the
doc appears in. Closed-form (hence "analytic"), pool-size independent.

CLI: `python oss_bakeoff/metrics.py`  (run from rerank_bench; paths resolve via __file__).
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

HERE = Path(__file__).parent


def _separation_stats(model_block):
    """Per-model medians over queries, all on the normalized [0,1] score."""
    top_minus_restmean = []
    top_minus_2nd = []
    top1_norm = []
    latencies = []
    for qdata in model_block["queries"].values():
        ranking = qdata["ranking"]
        norm = qdata["norm"]
        if not ranking:
            continue
        top_norm = norm[ranking[0]]
        top1_norm.append(top_norm)
        rest = [norm[node_id] for node_id in ranking[1:]]
        if rest:
            top_minus_restmean.append(top_norm - (sum(rest) / len(rest)))
            top_minus_2nd.append(top_norm - norm[ranking[1]])
        latency = qdata.get("latency_ms")
        if latency is not None:
            latencies.append(latency)

    def _median_or_none(values):
        return statistics.median(values) if values else None

    return {
        "sep_top_minus_restmean_median": _median_or_none(top_minus_restmean),
        "sep_top_minus_2nd_median": _median_or_none(top_minus_2nd),
        "top1_norm_median": _median_or_none(top1_norm),
        "latency_ms_p50": _median_or_none(latencies),
        "max_context": model_block["max_context"],
    }


def _combo_stability(model_block):
    """Analytic per-doc top-1 win-rate over random combos: mean of 2**(1-rank)."""
    win_sum = {}
    win_count = {}
    for qdata in model_block["queries"].values():
        for rank, node_id in enumerate(qdata["ranking"], start=1):
            win_sum[node_id] = win_sum.get(node_id, 0.0) + 2.0 ** (1 - rank)
            win_count[node_id] = win_count.get(node_id, 0) + 1
    top1_winrate_by_doc = {
        node_id: win_sum[node_id] / win_count[node_id]
        for node_id in sorted(win_sum)
    }
    return {
        "top1_winrate_by_doc": top1_winrate_by_doc,
        "note": ("analytic, secondary; per-doc top1 win-rate over random combos "
                 "containing the doc = mean over queries of 2**(1-rank), rank "
                 "1-based in the full-pool ordering"),
    }


def compute(scored_path=None, metrics_path=None):
    """Read scored.json, write metrics.json beside it, return the metrics dict."""
    scored_path = Path(scored_path) if scored_path else HERE / "scored.json"
    metrics_path = Path(metrics_path) if metrics_path else scored_path.parent / "metrics.json"

    scored = json.loads(scored_path.read_text())
    models = scored["models"]
    model_ids = sorted(models)

    per_model = {}
    combo_stability = {}
    for model_id in model_ids:
        block = models[model_id]
        per_model[model_id] = _separation_stats(block)
        combo_stability[model_id] = _combo_stability(block)

    disagreements = []
    pair_disagree_rate = {}
    next_id = 1
    for first_index in range(len(model_ids)):
        for second_index in range(first_index + 1, len(model_ids)):
            model_a = model_ids[first_index]
            model_b = model_ids[second_index]
            queries_a = models[model_a]["queries"]
            queries_b = models[model_b]["queries"]
            shared_queries = sorted(set(queries_a) & set(queries_b))
            differ = 0
            for query in shared_queries:
                qa = queries_a[query]
                qb = queries_b[query]
                if qa["top_id"] != qb["top_id"]:
                    differ += 1
                    disagreements.append({
                        "id": f"d{next_id:04d}",
                        "query": query,
                        "model_a": model_a,
                        "model_b": model_b,
                        "ranking_a": qa["ranking"],
                        "ranking_b": qb["ranking"],
                        "top_a": qa["top_id"],
                        "top_b": qb["top_id"],
                    })
                    next_id += 1
            rate = (differ / len(shared_queries)) if shared_queries else 0.0
            pair_disagree_rate[f"{model_a}__{model_b}"] = rate

    metrics = {
        "per_model": per_model,
        "disagreements": disagreements,
        "pair_disagree_rate": pair_disagree_rate,
        "combo_stability": combo_stability,
    }
    metrics_path.write_text(json.dumps(metrics, indent=2))
    print(f"wrote {metrics_path}  models={len(model_ids)}  "
          f"disagreements={len(disagreements)}  pairs={len(pair_disagree_rate)}")
    return metrics


if __name__ == "__main__":
    compute()

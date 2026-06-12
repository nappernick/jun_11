#!/usr/bin/env python3
"""analyze.py — fuse all signals into the FINAL verdict: which OSS reranker is
worthy of further consideration alongside Cohere 3.5 / v4.

Reads (whatever exists): scored.json, metrics.json, judge.json, latency_gpu.json.
Writes final_verdict.json and prints a human table. PRIMARY axis is judge win-rate
on the real pools (per CONTRACT hard rule: combos are presentational, not the
conclusion). Secondary axes: ranking agreement vs Cohere 3.5, score separation,
latency (local Bedrock/MPS + GPU), and max context window.
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
OSS = ["ettin-1b", "qwen3-0.6b", "qwen3-4b", "nemotron-1b-v2"]
COHERE = ["cohere-3.5", "cohere-v4-pro", "cohere-v4-fast"]


def _load(name):
    p = HERE / name
    return json.loads(p.read_text()) if p.exists() else None


def _agreement(scored, model, ref="cohere-3.5"):
    """Fraction of queries where `model` and `ref` pick the SAME top-1 doc."""
    ms = scored["models"].get(model, {}).get("queries", {})
    rs = scored["models"].get(ref, {}).get("queries", {})
    shared = [q for q in ms if q in rs]
    if not shared:
        return None
    same = sum(1 for q in shared if ms[q]["top_id"] == rs[q]["top_id"])
    return round(same / len(shared), 3)


def _gpu_latency_p50(latency, model, pool="10"):
    if not latency or model not in latency:
        return None
    cell = latency[model].get(pool) or {}
    return cell.get("p50")


def main():
    scored = _load("scored.json") or {"models": {}}
    metrics = _load("metrics.json") or {}
    judge = _load("judge.json") or {}
    latency = _load("latency_gpu.json") or {}

    per_model_metrics = metrics.get("per_model", {})
    model_score = judge.get("model_score", {})
    winrate = judge.get("winrate_matrix", {})

    present = [m for m in (OSS + COHERE) if m in scored.get("models", {})]
    rows = []
    for m in present:
        mm = per_model_metrics.get(m, {})
        # local latency p50 from scored.json (Bedrock for cohere-3.5, GPU round-trip for OSS)
        qs = scored["models"][m]["queries"]
        lats = sorted(v["latency_ms"] for v in qs.values() if v.get("latency_ms") is not None)
        local_p50 = lats[len(lats) // 2] if lats else None
        rows.append({
            "model": m,
            "family": "oss" if m in OSS else "cohere",
            "judge_score": round(model_score.get(m), 3) if m in model_score else None,
            "max_context": scored["models"][m].get("max_context") or mm.get("max_context"),
            "sep_top_minus_2nd_median": mm.get("sep_top_minus_2nd_median"),
            "top1_norm_median": mm.get("top1_norm_median"),
            "agreement_vs_cohere35": _agreement(scored, m) if m != "cohere-3.5" else 1.0,
            "latency_p50_ms": round(local_p50, 1) if local_p50 is not None else None,
            "gpu_latency_p50_pool10": _gpu_latency_p50(latency, m),
        })

    # Rank OSS models by judge score (primary), then separation.
    oss_rows = [r for r in rows if r["family"] == "oss"]
    oss_ranked = sorted(oss_rows, key=lambda r: (
        r["judge_score"] if r["judge_score"] is not None else -1,
        r["sep_top_minus_2nd_median"] or -1), reverse=True)

    verdict = {
        "rows": rows,
        "oss_ranked_by_judge": [r["model"] for r in oss_ranked],
        "judge_winrate_matrix": winrate,
        "notes": [
            "PRIMARY = judge_score (pairwise LLM win-rate on real pools, anonymized, both orderings).",
            "Combos are presentational; conclusion rests on judge win-rate + agreement + latency + window.",
            "Latency: cohere-3.5 = Bedrock API (laptop RTT); OSS local_p50 = GPU round-trip incl RTT; gpu p50 pool10 = warm in-AWS-ish timing.",
        ],
    }
    (HERE / "final_verdict.json").write_text(json.dumps(verdict, indent=2))

    # ---- human table ----
    cols = ["model", "judge_score", "max_context", "sep_top_minus_2nd_median",
            "agreement_vs_cohere35", "latency_p50_ms", "gpu_latency_p50_pool10"]
    w = {"model": 16, "judge_score": 11, "max_context": 12, "sep_top_minus_2nd_median": 12,
         "agreement_vs_cohere35": 12, "latency_p50_ms": 13, "gpu_latency_p50_pool10": 14}
    print("\n=== FINAL VERDICT TABLE ===")
    print("".join(c.replace("_", " ")[:w[c]].ljust(w[c] + 1) for c in cols))
    for r in rows:
        print("".join(str(r.get(c, "")).ljust(w[c] + 1) for c in cols))
    print(f"\nOSS ranked by judge win-rate: {verdict['oss_ranked_by_judge']}")
    print("wrote final_verdict.json")


if __name__ == "__main__":
    main()

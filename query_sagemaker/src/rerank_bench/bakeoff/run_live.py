#!/usr/bin/env python3
"""bakeoff.run_live — real end-to-end bakeoff over the generated eval set.

Runs N rerankers over the frozen 123-fixture eval set, computes the full metric
suite (nDCG / recall / MRR + latency + three-way abstention) per slice and per
N, picks a DECIDE recommendation, and writes a dashboard-compatible ResultsFile.

Models (extensible): the open-weights target (Ettin) head-to-head against the
Cohere Rerank 3.5 Bedrock baseline. Cohere is the metrics baseline for the
paired-bootstrap significance test.

Usage:
    python -m bakeoff.run_live                      # ettin + cohere, N=5,10
    python -m bakeoff.run_live --models ettin       # ettin only
    python -m bakeoff.run_live --limit 20           # smoke on first 20 fixtures
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from bakeoff.contract import Gates, ModelMeta, ScoredRow, results_to_json
from bakeoff.decide import recommend
from bakeoff.harness import load_fixtures, run_model
from bakeoff.metrics import aggregate

SAMPLE_DIR = Path(__file__).parent / "sample"
FIXTURES = SAMPLE_DIR / "eval_fixtures.jsonl"
DEFAULT_OUT = SAMPLE_DIR / "live_results.json"

# Whole-pipeline budget is sub-200ms (retrieve+rerank+assemble); the rerank
# slice is a fraction of that. Accuracy bar is a starting gate, not gospel.
DEFAULT_GATES = Gates(accuracy_bar=0.70, latency_budget_ms=200.0, false_answer_ceiling=0.10)
N_VALUES = [5, 10]

META = {
    "ettin-reranker-1b": ModelMeta(
        id="ettin-reranker-1b", display_name="Ettin Reranker 1B (ModernBERT, self-hosted)",
        params="1B", max_seq_len=7999, deploy_path="self-hosted GPU (SageMaker BYOC)",
        license="Apache-2.0", instruction_following=False, calibrated_scores=False,
    ),
    "cohere-rerank-3.5": ModelMeta(
        id="cohere-rerank-3.5", display_name="Cohere Rerank 3.5 (Bedrock)",
        params="n/a", max_seq_len=4096, deploy_path="Bedrock on-demand",
        license="commercial (Bedrock)", instruction_following=False, calibrated_scores=True,
    ),
}


def build_model(name: str):
    if name == "ettin":
        from bakeoff.adapters import EttinReranker
        # Cap seq len + batch: MPS scaled_dot_product_attention allocates a
        # batch×heads×seq×seq score tensor. Real FAQ docs are <~700 tokens
        # (max non-outlier ~660), so 1024 covers them all and truncates only the
        # ~42K-char Emburse outlier; batch 8 keeps the attention tensor ~1 GiB.
        return EttinReranker("1b", max_length=1024, batch_size=8)
    if name == "cohere":
        from bakeoff.adapters import BedrockCohereReranker
        return BedrockCohereReranker()
    raise ValueError(f"unknown model {name!r} (use 'ettin' or 'cohere')")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["ettin", "cohere"], choices=["ettin", "cohere"])
    ap.add_argument("--fixtures", default=str(FIXTURES))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--baseline", default="cohere-rerank-3.5")
    args = ap.parse_args(argv)

    fixtures = load_fixtures(args.fixtures)
    if args.limit:
        fixtures = fixtures[: args.limit]
    print(f"[run] fixtures={len(fixtures)} models={args.models} N={N_VALUES}")

    # Score each model ONCE at full pool depth; metric cutoffs (N) are applied
    # later by aggregate(k=n). The rerank call is identical regardless of N
    # (top_k only truncates output), so re-running per N would waste compute.
    full_k = max((len(f.candidates) for f in fixtures), default=50)
    rows_by_model: dict[str, list[ScoredRow]] = {}
    model_ids: list[str] = []
    for name in args.models:
        rr = build_model(name)
        model_ids.append(rr.id)
        t0 = time.perf_counter()
        rows = run_model(rr, fixtures, top_k=full_k)
        dt = time.perf_counter() - t0
        rows_by_model[rr.id] = rows
        p50 = sorted(r.latency_ms for r in rows)[len(rows) // 2]
        print(f"  [{rr.id}] {len(rows)} queries in {dt:.1f}s  p50={p50:.0f}ms/query")

    baseline_id = args.baseline if args.baseline in model_ids else model_ids[0]

    # Stage 3: aggregate into cells — one cell per (model, N), reusing the rows.
    cells: list[dict] = []
    for mid in model_ids:
        rows = rows_by_model[mid]
        base = rows_by_model.get(baseline_id, rows)
        for n in N_VALUES:
            by_slice = aggregate(rows, baseline_rows=base, k=n)
            for sd in by_slice.values():
                sd.pop("three_way_counts", None)
                if sd.get("p50"):
                    sd["throughput_qps"] = round(1000.0 / sd["p50"], 3)
            cells.append({
                "model_id": mid, "N": n, "by_slice": by_slice,
                "rows": [{"id": r.query_id, "slice": r.slice, "rels": r.rels,
                          "top_norm": r.top_norm, "latency": r.latency_ms} for r in rows],
            })

    # Stage 4: DECIDE per slice (report recommendation for the overall slices).
    slice_names = sorted({s for c in cells for s in c["by_slice"]})
    print("\n[decide] per-slice recommendation (gates: ndcg>=%.2f, p99<=%.0fms, FAR<=%.2f)"
          % (DEFAULT_GATES.accuracy_bar, DEFAULT_GATES.latency_budget_ms, DEFAULT_GATES.false_answer_ceiling))
    for s in slice_names:
        print(f"  {s}: {recommend(cells, s, DEFAULT_GATES)}")

    results = {
        "run_id": f"live-{'-'.join(model_ids)}-N{'-'.join(map(str, N_VALUES))}",
        "gates": DEFAULT_GATES, "baseline_model_id": baseline_id,
        "models": [META[m] for m in model_ids if m in META], "cells": cells,
    }
    out = Path(args.out)
    out.write_text(results_to_json(results) + "\n")
    print(f"\n[done] wrote {out}")

    # Console summary table (clean+typed slice, the bulk of the set).
    print("\n=== summary (N=10) ===")
    print(f"  {'model':24s} {'nDCG@10':>8s} {'recall@10':>10s} {'MRR@10':>7s} {'p50ms':>7s} {'p95ms':>7s}")
    for c in cells:
        if c["N"] != 10:
            continue
        sd = c["by_slice"].get("channel=typed&english=clean") or next(iter(c["by_slice"].values()))
        print(f"  {c['model_id']:24s} {sd['ndcg10']:8.3f} {sd['recall10']:10.3f} "
              f"{sd['mrr10']:7.3f} {sd['p50']:7.0f} {sd['p95']:7.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

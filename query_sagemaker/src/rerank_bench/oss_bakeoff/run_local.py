#!/usr/bin/env python3
"""run_local.py — score ONE model over all frozen pools, merge into scored.json.

Run one model at a time so the single MPS device never thrashes and progress is
never lost (each invocation updates only its model's slice and rewrites the file):

  ../.venv/bin/python run_local.py ettin-1b
  ../.venv/bin/python run_local.py qwen3-0.6b
  ../.venv/bin/python run_local.py qwen3-4b
  ../.venv/bin/python run_local.py nemotron-1b-v2
  ../.venv/bin/python run_local.py cohere-3.5

Quality (ranking) is hardware-independent, so these MPS scores ARE the scores the
g5 endpoint would produce (confirmed later by a free GPU-vs-local diff). The GPU
run exists only to measure latency.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))  # for normalize.py
from normalize import squash  # noqa: E402


def get_adapter(model_id):
    if model_id.startswith("cohere-3.5"):
        from cohere_adapters import Cohere35Reranker
        return Cohere35Reranker()
    if model_id.startswith("cohere-v4"):
        from cohere_adapters import CohereV4Reranker
        return CohereV4Reranker(variant=model_id.split("-")[-1])
    import models
    return models.load(model_id)


def score_model(model_id):
    pools = json.loads((HERE / "pools.json").read_text())["pools"]
    print(f"loading adapter {model_id} ...")
    t0 = time.time()
    r = get_adapter(model_id)
    print(f"  loaded {r.id} kind={r.kind} ctx={r.max_context} device={r.device} in {time.time()-t0:.1f}s")

    queries_out = {}
    for i, (q, items) in enumerate(pools.items()):
        docs = [it["text"] for it in items]
        ids = [it["node_id"] for it in items]
        t1 = time.time()
        raws = r.score_pairs(q, docs)
        lat = (time.time() - t1) * 1000
        norms = [squash(x, r.kind) for x in raws]
        order = sorted(range(len(docs)), key=lambda j: (-raws[j], j))
        queries_out[q] = {
            "ranking": [ids[j] for j in order],
            "raw": {ids[j]: round(raws[j], 5) for j in range(len(docs))},
            "norm": {ids[j]: round(norms[j], 5) for j in range(len(docs))},
            "top_id": ids[order[0]],
            "latency_ms": round(lat, 1),
        }
        print(f"  [{i+1:2d}/{len(pools)}] {lat:7.0f}ms  top={ids[order[0]][:8]}  {q[:42]}")

    # incremental merge into scored.json
    path = HERE / "scored.json"
    doc = json.loads(path.read_text()) if path.exists() else {"meta": {}, "models": {}}
    doc["models"][r.id] = {
        "kind": r.kind, "max_context": r.max_context, "device": r.device,
        "queries": queries_out,
    }
    doc["meta"] = {
        "generated_at": datetime.now().isoformat(),
        "n_queries": len(pools),
        "models": sorted(doc["models"].keys()),
    }
    path.write_text(json.dumps(doc, indent=2))
    lats = [v["latency_ms"] for v in queries_out.values()]
    lats.sort()
    print(f"\nwrote scored.json[{r.id}]  latency_ms p50={lats[len(lats)//2]:.0f} "
          f"max={lats[-1]:.0f}  (n={len(lats)})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: run_local.py <model_id>")
        sys.exit(1)
    score_model(sys.argv[1])

#!/usr/bin/env python3
"""run_combo5.py — score the stratified pool-5 dataset, ground-truth top-1 accuracy + MRR
per model per difficulty tier, with Wilson CIs. cohere-3.5 via Bedrock (no endpoint); OSS via
the GPU endpoint (atomic deploy+teardown + watchdog, reusing deploy_bench). combo5_results.json.

  python run_combo5.py                  # cohere-3.5 + 4 OSS models
"""
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import deploy_bench as DB  # noqa: E402

OSS = ["ettin-1b", "qwen3-0.6b", "qwen3-4b", "nemotron-1b-v2"]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def eval_one(inst, scores, ids):
    order = sorted(range(len(ids)), key=lambda j: (-scores[j], j))
    ranked = [ids[j] for j in order]
    gold = set(inst["gold_ids"])
    rank = next((r + 1 for r, nid in enumerate(ranked) if nid in gold), len(ids) + 1)
    return ranked[0] in gold, 1.0 / rank


def aggregate(records):
    out = {}
    models = sorted({r["model"] for r in records})
    for m in models:
        out[m] = {}
        for t in ["random", "mixed", "hard", "overall"]:
            rs = [r for r in records if r["model"] == m and (t == "overall" or r["tier"] == t)]
            if not rs:
                continue
            n = len(rs)
            k = sum(1 for r in rs if r["correct"])
            lo, hi = wilson(k, n)
            lats = sorted(r["latency"] for r in rs if r["latency"] is not None)
            out[m][t] = {"acc": round(k / n, 4), "ci": [round(lo, 4), round(hi, 4)],
                         "mrr": round(sum(r["rr"] for r in rs) / n, 4), "n": n,
                         "p50_latency_ms": round(lats[len(lats) // 2], 1) if lats else None}
    return out


def main():
    data = json.loads((HERE / "combo5_dataset.json").read_text())
    instances = data["instances"]
    records = []
    lock = threading.Lock()

    # --- 1. cohere-3.5 via Bedrock (no endpoint) -------------------------------
    import cohere_adapters
    coh = cohere_adapters.Cohere35Reranker()

    def do_coh(inst):
        ids = [it["node_id"] for it in inst["pool"]]
        docs = [it["text"] for it in inst["pool"]]
        t0 = time.perf_counter()
        try:
            scores = coh.score_pairs(inst["query"], docs)
            lat = (time.perf_counter() - t0) * 1000
            correct, rr = eval_one(inst, scores, ids)
        except Exception:
            correct, rr, lat = False, 0.0, None
        with lock:
            records.append({"model": "cohere-3.5", "tier": inst["tier"], "type": inst["type"],
                            "correct": correct, "rr": rr, "latency": lat})

    DB._log(f"scoring cohere-3.5 over {len(instances)} instances (Bedrock)")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(do_coh, instances))
    DB._log("cohere-3.5 done")

    # --- 2. OSS via GPU endpoint (atomic) --------------------------------------
    done = DB.start_watchdog(75)
    sm, s3 = DB._clients()
    try:
        img = DB.resolve_dlc_image()
        tar = DB.build_model_tar()
        uri = DB.upload_tar(s3, tar)
        DB.create_model(sm, uri, img)
        DB.create_endpoint_config(sm)
        DB.create_and_wait_endpoint(sm)
        runtime = DB._runtime_client()

        # warm each model once (cold-load) before timing/scoring
        for m in OSS:
            DB._log(f"warming {m}")
            DB.invoke_scores(runtime, m, instances[0]["query"], [it["text"] for it in instances[0]["pool"]])

        def do_oss(args):
            model_id, inst = args
            ids = [it["node_id"] for it in inst["pool"]]
            docs = [it["text"] for it in inst["pool"]]
            try:
                scores, _kind, lat = DB.invoke_scores(runtime, model_id, inst["query"], docs)
                correct, rr = eval_one(inst, scores, ids)
            except Exception:
                correct, rr, lat = False, 0.0, None
            with lock:
                records.append({"model": model_id, "tier": inst["tier"], "type": inst["type"],
                                "correct": correct, "rr": rr, "latency": lat})

        jobs = [(m, inst) for m in OSS for inst in instances]
        DB._log(f"scoring {len(jobs)} OSS (model,instance) pairs over the endpoint")
        prog = {"n": 0}
        def wrap(a):
            do_oss(a)
            with lock:
                prog["n"] += 1
                if prog["n"] % 500 == 0:
                    DB._log(f"  {prog['n']}/{len(jobs)} OSS scored")
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(wrap, jobs))
        DB._log("OSS scoring complete")
    except Exception as exc:
        DB._log(f"ERROR: {type(exc).__name__}: {exc}")
    finally:
        DB.teardown(sm)
        DB.prove_clean(sm)
        done.set()

    agg = aggregate(records)
    doc = {"meta": {"generated_at": datetime.now().isoformat(), "n_instances": len(instances),
                    "models": sorted({r["model"] for r in records}), "random_floor": 0.2,
                    "tiers": ["random", "mixed", "hard"]},
           "aggregate": agg, "records": records}
    (HERE / "combo5_results.json").write_text(json.dumps(doc, indent=2))

    print("\n=== TOP-1 ACCURACY  (random floor = 0.20) ===")
    print(f"{'model':16s} {'overall':>16s} {'random':>8s} {'mixed':>8s} {'hard':>8s} {'mrr':>6s} {'p50ms':>7s}")
    for m, t in sorted(agg.items(), key=lambda kv: -(kv[1].get("overall", {}).get("acc", 0))):
        o = t.get("overall", {})
        print(f"{m:16s} {str(o.get('acc'))+' '+str(o.get('ci','')):>16s} "
              f"{t.get('random',{}).get('acc'):>8} {t.get('mixed',{}).get('acc'):>8} "
              f"{t.get('hard',{}).get('acc'):>8} {o.get('mrr'):>6} {o.get('p50_latency_ms'):>7}")
    # saturation check on the HARD tier
    hard_accs = [t.get("hard", {}).get("acc") for t in agg.values() if t.get("hard")]
    if hard_accs:
        spread = max(hard_accs) - min(hard_accs)
        print(f"\nHARD-tier spread = {spread:.3f}  ->  "
              f"{'DISCRIMINATING ✓' if spread > 0.08 else 'still saturated — escalate difficulty'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

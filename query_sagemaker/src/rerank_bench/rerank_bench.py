#!/usr/bin/env python3
"""Benchmark Cohere rerankers over candidate pools pulled from the alpha FAQ
OpenSearch index (read-only on OpenSearch), comparing two models side by side:

  * cohere-3.5   — Bedrock on-demand (cohere.rerank-v3-5:0), alpha acct, us-west-2
  * cohere-4-pro — SageMaker real-time endpoint, CAIA acct 429134228173, us-east-1

The SAME retrieved candidates are scored by every selected model so the
comparison is apples-to-apples. No relevance labels exist for this corpus, so
"performance" is reported as: latency (p50/p90), relevance-score distribution,
score separation (how confidently the model splits good from bad docs), and
top-1 stability across pool sizes -- not precision/recall.

Two AWS accounts => two pinned boto3 sessions; nothing relies on a default
session or AWS_PROFILE (see auth notes in-line). A model whose backend is
unreachable (e.g. the 4 Pro endpoint not deployed yet) is logged loudly and
skipped -- the run still produces output for the models that worked.

Usage:
  python rerank_bench.py                          # both models, default queries/pools
  python rerank_bench.py --models 3.5             # 3.5 only (no CAIA needed)
  python rerank_bench.py --models 3.5 4pro        # explicit both
  python rerank_bench.py --pools 5 10 20 40       # custom pool sizes
  python rerank_bench.py --combo 5 --combo-base 12 --combo-cap 1000
"""
import argparse
import hashlib
import json
import logging
import statistics
import time
from collections import Counter
from datetime import datetime
from itertools import combinations
from math import comb
from pathlib import Path

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("rerank_bench")

# --- retrieval + 3.5 live in the alpha account (us-west-2) -------------------
ALPHA_PROFILE = "alpha"
ENDPOINT = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com"
REGION, SERVICE, INDEX = "us-west-2", "aoss", "faq_evidence_a"
RERANK_MODEL_ARN = f"arn:aws:bedrock:{REGION}::foundation-model/cohere.rerank-v3-5:0"

# --- 4 Pro lives in the personal CAIA account (us-east-1) --------------------
CAIA_PROFILE = "nick-caia"
CAIA_REGION = "us-east-1"
RERANK4_ENDPOINT = "cohere-rerank4-pro-sandbox"  # matches rerank_sandbox.py NAME
RERANK4_FAST_ENDPOINT = "cohere-rerank4-fast-sandbox"  # matches rerank_sandbox.py 'fast' NAME
OUT_DIR = Path(__file__).parent / "results"
MAX_DOC_CHARS = 32000  # Bedrock Rerank per-document hard limit; drops the Emburse outlier

# Model registry: CLI token -> internal label. Reranker fns are resolved lazily.
MODEL_LABELS = {"3.5": "cohere-3.5", "4pro": "cohere-4-pro", "4fast": "cohere-4-fast"}
DEFAULT_MODELS = ["3.5", "4fast"]

DEFAULT_QUERIES = [
    "how do I book a flight and hotel for a business trip",
    "can I upgrade to business class and what does Amazon reimburse",
    "what size rental car am I allowed and what happens if I have an accident",
    "how do I set up my travel profile and book a seat assignment",
    "do I need a visa and how do I get travel approval for international trips",
    "what expense category do I use for airfare hotel and meals in Concur",
    "how do I cancel or change a flight booking I already made",
    "can I extend a business trip for personal travel and will Amazon pay",
    "how do I request a travel accommodation for a medical condition",
    "can I use my personal frequent flyer miles to upgrade and keep the points",
]
DEFAULT_POOLS = [5, 10, 20]

# --- alpha session: AOSS retrieval + Bedrock 3.5 (built eagerly; always needed)
_alpha_session = boto3.Session(profile_name=ALPHA_PROFILE, region_name=REGION)
_alpha_creds = _alpha_session.get_credentials().get_frozen_credentials()
_bedrock = _alpha_session.client("bedrock-agent-runtime")

# --- CAIA cohere client: built lazily only if 4 Pro is actually requested -----
_cohere_client = None


def cohere_client():
    """Build (once) a Cohere SageMaker client pinned to the CAIA account.

    Pinned explicitly to nick-caia frozen creds so it never inherits the alpha
    default session -- the cohere SDK resolves its own credentials otherwise,
    which would silently hit the wrong account.
    """
    global _cohere_client
    if _cohere_client is None:
        import cohere  # lazy: 3.5-only runs don't need the cohere package
        sess = boto3.Session(profile_name=CAIA_PROFILE, region_name=CAIA_REGION)
        c = sess.get_credentials().get_frozen_credentials()
        kwargs = {"aws_region": CAIA_REGION,
                  "aws_access_key": c.access_key,
                  "aws_secret_key": c.secret_key}
        if c.token:  # static IAM-user keys have no session token; temp creds do
            kwargs["aws_session_token"] = c.token
        _cohere_client = cohere.SagemakerClient(**kwargs)
    return _cohere_client


def signed(method, path, body=None):
    url = ENDPOINT + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    headers["X-Amz-Content-SHA256"] = hashlib.sha256(data or b"").hexdigest()
    req = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(_alpha_creds, SERVICE, REGION).add_auth(req)
    return requests.request(method, url, headers=dict(req.headers), data=data)


def retrieve(query, size):
    """BM25 candidate pool from OpenSearch (no Bedrock embed needed)."""
    r = signed("POST", f"/{INDEX}/_search", {
        "size": size,
        "_source": ["source_id", "text", "source_metadata"],
        "query": {"match": {"text": query}},
    })
    r.raise_for_status()
    return [h["_source"] for h in r.json()["hits"]["hits"]]


def doc_text(src):
    title = (src.get("source_metadata") or {}).get("title") or ""
    return f"{title}\n{src.get('text', '')}".strip()


# --- rerankers: each returns (latency_ms, [{"index": int, "score": float}]) ---
# ordered best-first, where `index` points into the docs list passed in.

def rerank_35(query, docs, top_n):
    sources = [{"type": "INLINE",
                "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": t}}}
               for t in docs]
    t0 = time.perf_counter()
    resp = _bedrock.rerank(
        queries=[{"type": "TEXT", "textQuery": {"text": query}}],
        sources=sources,
        rerankingConfiguration={
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "numberOfResults": top_n,
                "modelConfiguration": {"modelArn": RERANK_MODEL_ARN}}})
    latency_ms = (time.perf_counter() - t0) * 1000
    return latency_ms, [{"index": r["index"], "score": float(r["relevanceScore"])}
                        for r in resp["results"]]


def _rerank_sagemaker(endpoint, query, docs, top_n):
    co = cohere_client()
    t0 = time.perf_counter()
    resp = co.rerank(model=endpoint, query=query, documents=docs, top_n=top_n)
    latency_ms = (time.perf_counter() - t0) * 1000
    return latency_ms, [{"index": r.index, "score": float(r.relevance_score)}
                        for r in resp.results]


def rerank_4pro(query, docs, top_n):
    return _rerank_sagemaker(RERANK4_ENDPOINT, query, docs, top_n)


def rerank_4fast(query, docs, top_n):
    return _rerank_sagemaker(RERANK4_FAST_ENDPOINT, query, docs, top_n)


RERANKERS = {"cohere-3.5": rerank_35, "cohere-4-pro": rerank_4pro, "cohere-4-fast": rerank_4fast}


def pct(values, p):
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100)[p - 1]


def make_output_stem(args) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.combo:
        return f"combo_k{args.combo}_base{args.combo_base}_cap{args.combo_cap}_{ts}"
    pools_str = "_".join(str(p) for p in sorted(args.pools))
    return f"pools_{pools_str}_{ts}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--queries", nargs="+", default=DEFAULT_QUERIES)
    ap.add_argument("--pools", nargs="+", type=int, default=DEFAULT_POOLS)
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS,
                    choices=list(MODEL_LABELS.keys()),
                    help="which models to score the same candidates with (default: both)")
    ap.add_argument("--combo", type=int, metavar="K",
                    help="combinations mode: rerank every K-combination of the base pool")
    ap.add_argument("--combo-base", type=int, default=8,
                    help="base candidate pool to draw K-combinations from (default 8)")
    ap.add_argument("--combo-cap", type=int, default=300,
                    help="abort a query if its combo count would exceed this (default 300)")
    args = ap.parse_args()
    pools = sorted(args.pools)
    max_pool = args.combo_base if args.combo else pools[-1]

    # Resolve requested models to (label, fn). active_models drops any whose
    # backend fails on first use; errored records why for the UI/meta.
    active_models = [MODEL_LABELS[m] for m in args.models]
    errored = {}  # label -> error string

    rows = []  # one record per (query, pool/combo, model)
    dropped_chunks = []

    def call_model(label, query, docs, top_n):
        """Invoke a model, disabling+recording it on failure instead of crashing."""
        if label not in active_models:
            return None
        try:
            return RERANKERS[label](query, docs, top_n)
        except Exception as e:  # noqa: BLE001 -- intentional: isolate per-model failure
            log.warning("model %s failed; disabling for the rest of the run: %s",
                        label, e, exc_info=True)
            errored[label] = f"{type(e).__name__}: {e}"
            active_models.remove(label)
            return None

    for q in args.queries:
        candidates = retrieve(q, max_pool)
        for c in candidates:
            char_count = len(doc_text(c))
            if char_count > MAX_DOC_CHARS:
                dropped_chunks.append({
                    "query": q, "source_id": c.get("source_id"),
                    "char_count": char_count, "limit": MAX_DOC_CHARS,
                    "reason": "exceeds_bedrock_rerank_limit",
                })
                log.info("dropped oversize chunk (%d chars) id=%s",
                         char_count, c.get("source_id"))
        candidates = [c for c in candidates if len(doc_text(c)) <= MAX_DOC_CHARS]
        if not candidates:
            log.info("no candidates for: %r", q)
            continue
        docs = [doc_text(c) for c in candidates]
        ids = [c.get("source_id") for c in candidates]

        if args.combo:
            k, idxs = args.combo, list(range(len(docs)))
            n_combos = comb(len(idxs), k) if k <= len(idxs) else 0
            if not n_combos:
                log.info("base pool too small for C(n,%d): %r", k, q)
                continue
            if n_combos > args.combo_cap:
                log.info("ABORT %r: %d combos > --combo-cap %d", q, n_combos, args.combo_cap)
                continue
            for combo in combinations(idxs, k):
                cdocs = [docs[i] for i in combo]
                for label in list(active_models):
                    out = call_model(label, q, cdocs, top_n=k)
                    if not out:
                        continue
                    latency, results = out
                    top = results[0]
                    rows.append({
                        "query": q, "pool": k, "model": label,
                        "latency_ms": round(latency, 1),
                        "top_score": round(top["score"], 4),
                        "top_id": ids[combo[top["index"]]],
                        "combo": [ids[i] for i in combo],
                        "ranking": [ids[combo[r["index"]]] for r in results],
                        "scores": [round(r["score"], 4) for r in results],
                    })
            log.info("%-40s reranked C(n,%d)=%d combos x %d model(s)",
                     q[:40], k, n_combos, len(active_models))
            continue

        for n in pools:
            if n > len(docs):
                continue
            ndocs = docs[:n]
            for label in list(active_models):
                out = call_model(label, q, ndocs, top_n=n)
                if not out:
                    continue
                latency, results = out
                top = results[0]
                rows.append({
                    "query": q, "pool": n, "model": label,
                    "latency_ms": round(latency, 1),
                    "top_score": round(top["score"], 4),
                    "top_id": ids[top["index"]],
                    "scores": [round(r["score"], 4) for r in results],
                })
                log.info("%-40s pool=%-3d %-12s %6.0fms top=%.3f id=%s",
                         q[:40], n, label, latency, top["score"], ids[top["index"]])

    OUT_DIR.mkdir(exist_ok=True)
    stem = make_output_stem(args)

    output = {
        "meta": {
            "run_mode": "combo" if args.combo else "pool_sweep",
            "queries": args.queries,
            "pools": pools if not args.combo else None,
            "combo_k": args.combo,
            "combo_base": args.combo_base if args.combo else None,
            "combo_cap": args.combo_cap if args.combo else None,
            "max_doc_chars": MAX_DOC_CHARS,
            "dropped_chunks": dropped_chunks,
            "models": sorted({r["model"] for r in rows}),
            "models_errored": errored,
            "record_count": len(rows),
            "generated_at": datetime.now().isoformat(),
        },
        "rows": rows,
    }
    json_path = OUT_DIR / f"{stem}.json"
    json_path.write_text(json.dumps(output, indent=2))
    log.info("Output: %s", json_path)

    # Maintain results/manifest.json so the dashboard auto-discovers runs
    # (no drag-and-drop needed). Newest first.
    manifest = []
    for p in OUT_DIR.glob("*.json"):
        if p.name == "manifest.json":
            continue
        try:
            o = json.loads(p.read_text())
            m = o.get("meta", {}) if isinstance(o, dict) else {}
        except Exception:  # noqa: BLE001 -- a malformed file shouldn't break the manifest
            m = {}
        manifest.append({
            "filename": p.name,
            "run_mode": m.get("run_mode"),
            "models": m.get("models", []),
            "record_count": m.get("record_count"),
            "generated_at": m.get("generated_at"),
            "mtime": p.stat().st_mtime,
        })
    manifest.sort(key=lambda x: x["mtime"] or 0, reverse=True)
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))

    models_present = output["meta"]["models"]
    pools_present = sorted({r["pool"] for r in rows})

    # ---- statistics (per model) ----
    for label in models_present:
        mrows = [r for r in rows if r["model"] == label]
        print(f"\n=== {label} : latency by pool size (ms) ===")
        for n in pools_present:
            v = [r["latency_ms"] for r in mrows if r["pool"] == n]
            if v:
                print(f"  pool={n:<3d} p50={statistics.median(v):.0f} "
                      f"p90={pct(v, 90):.0f} max={max(v):.0f}  (n={len(v)})")
        print(f"=== {label} : top-1 relevance score by pool size ===")
        for n in pools_present:
            v = [r["top_score"] for r in mrows if r["pool"] == n]
            if v:
                print(f"  pool={n:<3d} median={statistics.median(v):.3f} "
                      f"min={min(v):.3f} max={max(v):.3f}")

    if len(models_present) == 2:
        a, b = models_present
        print(f"\n=== head-to-head: {a} vs {b} (per query, max pool) ===")
        mp = pools_present[-1] if pools_present else None
        for q in args.queries:
            ra = next((r for r in rows if r["query"] == q and r["model"] == a and r["pool"] == mp), None)
            rb = next((r for r in rows if r["query"] == q and r["model"] == b and r["pool"] == mp), None)
            if ra and rb:
                d_lat = rb["latency_ms"] - ra["latency_ms"]
                d_score = rb["top_score"] - ra["top_score"]
                agree = "AGREE" if ra["top_id"] == rb["top_id"] else "DIFFER"
                print(f"  {agree}  {q[:42]:42s} "
                      f"score {ra['top_score']:.3f}->{rb['top_score']:.3f} ({d_score:+.3f})  "
                      f"lat {ra['latency_ms']:.0f}->{rb['latency_ms']:.0f}ms ({d_lat:+.0f})")

    if errored:
        print("\n=== models skipped (backend unreachable) ===")
        for label, msg in errored.items():
            print(f"  {label}: {msg}")

    # ---- visualization (static PNG; the React dashboard is the primary UI) ---
    if rows:
        fig, ax = plt.subplots(1, 3, figsize=(16, 4.5))
        ax[0].boxplot([[r["latency_ms"] for r in rows if r["model"] == m] for m in models_present],
                      labels=models_present)
        ax[0].set(title="Rerank latency by model", ylabel="ms")
        ax[1].boxplot([[s for r in rows if r["model"] == m for s in r["scores"]] for m in models_present],
                      labels=models_present)
        ax[1].set(title="Relevance score distribution by model", ylabel="relevanceScore")
        # top-1 per query, grouped bars per model (trend across queries)
        import numpy as np
        qlabels = [q[:16] for q in args.queries]
        x = np.arange(len(qlabels))
        width = 0.8 / max(len(models_present), 1)
        mp = pools_present[-1] if pools_present else None
        for i, m in enumerate(models_present):
            ys = [next((r["top_score"] for r in rows
                        if r["query"] == q and r["model"] == m and r["pool"] == mp), 0)
                  for q in args.queries]
            ax[2].bar(x + i * width, ys, width, label=m)
        ax[2].set_xticks(x + width * (len(models_present) - 1) / 2)
        ax[2].set_xticklabels(qlabels, rotation=45, ha="right", fontsize=8)
        ax[2].set(title=f"Top-1 score per query (pool={mp})", ylabel="relevanceScore")
        ax[2].legend(fontsize=8)
        fig.tight_layout()
        png_path = OUT_DIR / f"{stem}.png"
        fig.savefig(png_path, dpi=120)
        log.info("PNG:    %s", png_path)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""ragas_eval.py — reference-free RAG evaluation of each reranker, RAGAS/NVIDIA style.

Pipeline per (reranker, query):
  1. take the reranker's TOP-K reranked docs (from scored.json) as the RAG context,
  2. generate an answer with a FIXED generator (so the reranker is the only variable),
  3. score 5 reference-free metrics (no gold answers needed):

     context_precision      RAGAS LLMContextPrecisionWithoutReference — are the
                            contexts that help answer the query ranked high? (rank-
                            weighted average precision; the most direct reranker signal)
     context_relevance      NVIDIA nv_context_relevance — LLM rates how relevant the
                            retrieved context is to the query (0/1/2 -> [0,1])
     faithfulness           RAGAS Faithfulness — fraction of the answer's atomic claims
                            that are supported by the context
     response_relevancy     RAGAS ResponseRelevancy — generate questions from the answer,
                            embed, mean cosine similarity to the real query
     response_groundedness  NVIDIA nv_response_groundedness — LLM rates how grounded the
                            answer is in the context (0/1/2 -> [0,1])

Faithful re-implementations of the published metric definitions on our Bedrock setup
(transparent prompts, no ragas/langchain dependency). Generator = Sonnet 4.6; metric
judge = Opus 4.8 (the high-intelligence judge); embeddings = Titan v2.

  python ragas_eval.py --models ettin-1b --queries 3      # tiny validation
  python ragas_eval.py                                     # full: all rerankers x 42
  python ragas_eval.py --topk 5 --workers 8
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
PROFILE, REGION = "alpha", "us-west-2"
GEN_MODEL = "us.anthropic.claude-sonnet-4-6"        # RAG answer generator (the system's generator)
JUDGE_MODEL = "us.anthropic.claude-opus-4-8"        # metric judge (high-intelligence)
EMBED_MODEL = "amazon.titan-embed-text-v2:0"        # Response Relevancy embeddings
TOPK_DEFAULT = 5

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _client():
    import boto3
    from botocore.config import Config
    return boto3.Session(profile_name=PROFILE, region_name=REGION).client(
        "bedrock-runtime", config=Config(retries={"max_attempts": 10, "mode": "adaptive"}, read_timeout=180))


_local = threading.local()


def client():
    c = getattr(_local, "c", None)
    if c is None:
        c = _local.c = _client()
    return c


def converse(model, prompt, max_tokens=1500):
    resp = client().converse(modelId=model, messages=[{"role": "user", "content": [{"text": prompt}]}],
                             inferenceConfig={"maxTokens": max_tokens})
    return resp["output"]["message"]["content"][0]["text"]


def parse_json(raw):
    try:
        return json.loads(_FENCE.sub("", raw.strip()))
    except json.JSONDecodeError:
        for cand in reversed(re.findall(r"\{[^{}]*\}|\[[^\[\]]*\]", raw, re.DOTALL)):
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue
        m = re.search(r"(\{.*\}|\[.*\])", raw, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        raise


def embed(text):
    resp = client().invoke_model(modelId=EMBED_MODEL,
                                 body=json.dumps({"inputText": text[:8000]}))
    return json.loads(resp["body"].read())["embedding"]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# --- generation ---------------------------------------------------------------
def generate_answer(query, contexts):
    ctx = "\n\n".join(f"[{i+1}] {c}" for i, c in enumerate(contexts))
    prompt = (
        "You are an FAQ assistant for an internal Travel & Expense help desk. Answer the "
        "employee's question using ONLY the reference context below. If the context does "
        "not contain the answer, say you don't have that information. Be concise and direct.\n\n"
        f"CONTEXT:\n{ctx}\n\nQUESTION: {query}\n\nANSWER:")
    return converse(GEN_MODEL, prompt, max_tokens=600).strip()


# --- metrics (each batched into few calls; faithful to RAGAS/NVIDIA defs) ------
def m_context_precision(query, contexts, answer):
    """RAGAS context precision w/o reference: each context judged useful(1)/not(0) for the
    answer, then rank-weighted average precision (relevant high = better)."""
    ctx = "\n\n".join(f"[{i+1}] {c[:1500]}" for i, c in enumerate(contexts))
    out = parse_json(converse(JUDGE_MODEL,
        "For the question and the answer derived from it, judge whether EACH numbered context "
        "was USEFUL in arriving at the answer (relevant), 1 = useful, 0 = not.\n\n"
        f"QUESTION: {query}\n\nANSWER: {answer}\n\nCONTEXTS:\n{ctx}\n\n"
        'Return ONLY JSON: {"verdicts": [<0|1>, ... one per context in order]}', max_tokens=800))
    v = [1 if x else 0 for x in out.get("verdicts", [])][:len(contexts)]
    if not v or sum(v) == 0:
        return 0.0
    num = 0.0
    hits = 0
    for k, rel in enumerate(v, start=1):
        if rel:
            hits += 1
            num += hits / k
    return round(num / sum(v), 4)


def m_context_relevance(query, contexts):
    """NVIDIA context relevance: rate how relevant the retrieved context is to the query, 0/1/2."""
    ctx = "\n\n".join(f"[{i+1}] {c[:1500]}" for i, c in enumerate(contexts))
    out = parse_json(converse(JUDGE_MODEL,
        "Rate how relevant the retrieved CONTEXT is for answering the QUERY:\n"
        "2 = fully relevant (contains what's needed), 1 = partially relevant, 0 = irrelevant.\n\n"
        f"QUERY: {query}\n\nCONTEXT:\n{ctx}\n\n"
        'Return ONLY JSON: {"score": <0|1|2>, "reason": "<short>"}', max_tokens=400))
    return round(float(out.get("score", 0)) / 2.0, 4)


def m_faithfulness(query, contexts, answer):
    """RAGAS faithfulness: fraction of the answer's atomic claims supported by the context."""
    ctx = "\n\n".join(c[:1500] for c in contexts)
    out = parse_json(converse(JUDGE_MODEL,
        "Break the ANSWER into atomic factual claims. For each claim, decide if it can be "
        "directly inferred from the CONTEXT (1 = supported, 0 = not supported / unsupported "
        "by context). Ignore claims that are mere refusals or 'I don't have that info'.\n\n"
        f"CONTEXT:\n{ctx}\n\nANSWER: {answer}\n\n"
        'Return ONLY JSON: {"claims": [{"claim": "<text>", "supported": <0|1>}, ...]}', max_tokens=1200))
    claims = out.get("claims", [])
    if not claims:
        return 1.0  # no factual claims (e.g. clean refusal) -> vacuously faithful
    return round(sum(1 for c in claims if c.get("supported")) / len(claims), 4)


def m_response_relevancy(query, answer):
    """RAGAS response relevancy: generate questions the answer would answer, embed, mean
    cosine similarity to the real query. Penalizes evasive / off-topic answers."""
    out = parse_json(converse(JUDGE_MODEL,
        "Given this ANSWER, generate 3 distinct questions that this answer would be a direct "
        "and complete response to.\n\n"
        f"ANSWER: {answer}\n\n" 'Return ONLY JSON: {"questions": ["q1","q2","q3"]}', max_tokens=400))
    qs = out.get("questions", [])[:3]
    if not qs:
        return 0.0
    qv = embed(query)
    sims = [cosine(qv, embed(q)) for q in qs if q]
    return round(sum(sims) / len(sims), 4) if sims else 0.0


def m_response_groundedness(contexts, answer):
    """NVIDIA response groundedness: rate how well the answer is grounded in the context, 0/1/2."""
    ctx = "\n\n".join(c[:1500] for c in contexts)
    out = parse_json(converse(JUDGE_MODEL,
        "Rate how well the ANSWER is grounded in (supported by) the CONTEXT:\n"
        "2 = every claim grounded, 1 = partially grounded, 0 = not grounded / fabricated.\n\n"
        f"CONTEXT:\n{ctx}\n\nANSWER: {answer}\n\n"
        'Return ONLY JSON: {"score": <0|1|2>, "reason": "<short>"}', max_tokens=400))
    return round(float(out.get("score", 0)) / 2.0, 4)


METRIC_KEYS = ["context_precision", "context_relevance", "faithfulness",
               "response_relevancy", "response_groundedness"]


def score_pair(reranker, query, contexts, top_ids):
    """Generate the answer + compute all 5 metrics for one (reranker, query)."""
    answer = generate_answer(query, contexts)
    metrics = {}
    errs = {}
    def safe(name, fn):
        try:
            metrics[name] = fn()
        except Exception as exc:  # one metric failing must not lose the others
            metrics[name] = None
            errs[name] = f"{type(exc).__name__}: {str(exc)[:120]}"
    safe("context_precision", lambda: m_context_precision(query, contexts, answer))
    safe("context_relevance", lambda: m_context_relevance(query, contexts))
    safe("faithfulness", lambda: m_faithfulness(query, contexts, answer))
    safe("response_relevancy", lambda: m_response_relevancy(query, answer))
    safe("response_groundedness", lambda: m_response_groundedness(contexts, answer))
    return {"reranker": reranker, "query": query, "top_ids": top_ids,
            "answer": answer, "metrics": metrics, "errors": errs or None}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=None, help="rerankers to eval (default: all in scored.json)")
    ap.add_argument("--queries", type=int, default=None, help="cap number of queries (validation)")
    ap.add_argument("--topk", type=int, default=TOPK_DEFAULT)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="ragas_results.json")
    args = ap.parse_args(argv)

    scored = json.loads((HERE / "scored.json").read_text())["models"]
    pools = json.loads((HERE / "pools.json").read_text())["pools"]
    text_of = {}
    for items in pools.values():
        for it in items:
            text_of[it["node_id"]] = it["text"]

    rerankers = args.models or sorted(scored.keys())
    queries = list(next(iter(scored.values()))["queries"].keys())
    if args.queries:
        queries = queries[:args.queries]

    jobs = []
    for rk in rerankers:
        qmap = scored[rk]["queries"]
        for q in queries:
            if q not in qmap:
                continue
            top_ids = qmap[q]["ranking"][:args.topk]
            contexts = [text_of.get(nid, "") for nid in top_ids]
            jobs.append((rk, q, contexts, top_ids))

    print(f"scoring {len(jobs)} (reranker,query) pairs x 5 metrics, top-{args.topk}, {args.workers} workers")
    results = [None] * len(jobs)
    prog = {"n": 0}
    lock = threading.Lock()

    def run(i):
        rk, q, ctx, ids = jobs[i]
        results[i] = score_pair(rk, q, ctx, ids)
        with lock:
            prog["n"] += 1
            mm = results[i]["metrics"]
            avg = [v for v in mm.values() if isinstance(v, (int, float))]
            print(f"  [{prog['n']:3d}/{len(jobs)}] {rk:14s} avg={sum(avg)/len(avg):.3f} {q[:38]}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run, range(len(jobs))))

    # aggregate per reranker
    agg = {}
    for rk in rerankers:
        rows = [r for r in results if r and r["reranker"] == rk]
        per = {}
        for k in METRIC_KEYS:
            vals = [r["metrics"][k] for r in rows if isinstance(r["metrics"].get(k), (int, float))]
            per[k] = round(sum(vals) / len(vals), 4) if vals else None
        nums = [v for v in per.values() if v is not None]
        per["ragas_overall"] = round(sum(nums) / len(nums), 4) if nums else None
        per["n"] = len(rows)
        agg[rk] = per

    doc = {"meta": {"generated_at": datetime.now().isoformat(), "gen_model": GEN_MODEL,
                    "judge_model": JUDGE_MODEL, "embed_model": EMBED_MODEL, "topk": args.topk,
                    "metrics": METRIC_KEYS, "n_pairs": len(jobs)},
           "aggregate": agg, "rows": results}
    (HERE / args.out).write_text(json.dumps(doc, indent=2))
    print(f"\nwrote {args.out}")
    print("=== RAGAS overall per reranker ===")
    for rk, p in sorted(agg.items(), key=lambda kv: -(kv[1]["ragas_overall"] or 0)):
        print(f"  {rk:16s} overall={p['ragas_overall']}  " +
              "  ".join(f"{k[:4]}={p[k]}" for k in METRIC_KEYS))
    return 0


if __name__ == "__main__":
    sys.exit(main())

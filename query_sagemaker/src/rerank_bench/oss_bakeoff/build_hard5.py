#!/usr/bin/env python3
"""build_hard5.py — pool-of-5 eval set over the KNOWN 50-FAQ corpus (the user's real setup).

For each hard query (ragas_testset.json) we build a 5-candidate pool = the GOLD FAQ(s) +
the hardest distractors (nearest OTHER FAQs by Titan-embedding cosine). The reranker's job
is to rank the gold #1 against 4 strong near-misses -> top-1 accuracy / MRR (ground truth,
cannot saturate). Corpus = query_chunks.jsonl (the 50-ish FAQ docs). Output hard5_pools.json.
"""
import json
from pathlib import Path

import boto3

HERE = Path(__file__).parent
REF = HERE.parent
EMBED_MODEL = "amazon.titan-embed-text-v2:0"
POOL = 5

rt = boto3.Session(profile_name="alpha", region_name="us-west-2").client("bedrock-runtime")


def embed(text):
    r = rt.invoke_model(modelId=EMBED_MODEL, body=json.dumps({"inputText": text[:8000]}))
    return json.loads(r["body"].read())["embedding"]


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def main():
    corpus = [json.loads(l) for l in (REF / "rerank_bench" / "query_chunks.jsonl").open()] \
        if (REF / "rerank_bench" / "query_chunks.jsonl").exists() \
        else [json.loads(l) for l in (REF / "query_chunks.jsonl").open()]
    docs = {}
    for d in corpus:
        nid = d.get("nodeId") or d.get("source_id")
        text = (d.get("title") or d.get("h1") or "") + "\n" + (d.get("markdown") or d.get("text") or "")
        docs[nid] = {"node_id": nid, "title": d.get("title") or d.get("h1") or "", "text": text.strip(),
                     "char_len": len(text)}
    print(f"corpus: {len(docs)} FAQ docs")

    print("embedding corpus...")
    demb = {nid: embed(d["text"]) for nid, d in docs.items()}

    samples = json.loads((HERE / "ragas_testset.json").read_text())["samples"]
    pools, gold_map, qmeta = {}, {}, {}
    matched = 0
    for s in samples:
        q = s["query"]
        gold_ids = [g for g in (s.get("source_node_ids") or []) if g in docs]
        if not gold_ids:
            # fall back: nearest corpus doc to the query becomes the gold
            qv = embed(q)
            gold_ids = [max(docs, key=lambda nid: cos(qv, demb[nid]))]
        else:
            matched += 1
        qv = embed(q)
        # hardest distractors = nearest non-gold docs
        others = sorted((nid for nid in docs if nid not in gold_ids),
                        key=lambda nid: -cos(qv, demb[nid]))
        need = max(0, POOL - len(gold_ids))
        pool_ids = list(gold_ids[:POOL]) + others[:need]
        pool_ids = pool_ids[:POOL]
        pools[q] = [docs[nid] for nid in pool_ids]
        gold_map[q] = gold_ids
        qmeta[q] = {"type": s.get("type"), "persona": s.get("persona"), "style": s.get("style")}

    out = {"meta": {"corpus_size": len(docs), "pool_size": POOL, "n_queries": len(pools),
                    "gold_matched_in_corpus": matched, "embed_model": EMBED_MODEL},
           "pools": pools, "gold": gold_map, "query_meta": qmeta}
    (HERE / "hard5_pools.json").write_text(json.dumps(out, indent=2))
    print(f"wrote hard5_pools.json: {len(pools)} queries, pool=5, gold matched {matched}/{len(samples)}")


if __name__ == "__main__":
    main()

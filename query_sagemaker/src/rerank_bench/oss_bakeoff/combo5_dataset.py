#!/usr/bin/env python3
"""combo5_dataset.py — difficulty-STRATIFIED pool-of-5 eval set with ground truth.

Anti-saturation by design. For each hard query (gold FAQ known), build pools of 5 = gold +
4 distractors at three difficulty tiers, N random draws each:
  random : 4 random non-gold fragments          (easy — your unbiased baseline)
  mixed  : 2 nearest + 2 random
  hard   : 4 nearest non-gold fragments          (the confusable near-duplicate FAQs)
The HARD tier guarantees the metric spreads (weak rerankers can't separate near-duplicates).
Metric downstream = top-1 accuracy / MRR vs the known gold (pass/fail, cannot saturate).

Distractor "nearness" = Titan-embedding cosine to the GOLD chunk (captures regional/near-dup
variants). Gold resolved from testset source_node_ids; falls back to nearest-corpus-by-query.
Output combo5_dataset.json: {meta, instances:[{id,query,type,tier,gold_ids,pool}]}.
"""
import json
import random
from pathlib import Path

import boto3

HERE = Path(__file__).parent
REF = HERE.parent
EMBED_MODEL = "amazon.titan-embed-text-v2:0"
POOL = 5
N_DRAWS = 10            # per tier per query
random.seed(7)
rt = boto3.Session(profile_name="alpha", region_name="us-west-2").client("bedrock-runtime")


def embed(text):
    r = rt.invoke_model(modelId=EMBED_MODEL, body=json.dumps({"inputText": text[:8000]}))
    return json.loads(r["body"].read())["embedding"]


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def load_corpus():
    for p in [REF / "rerank_bench" / "query_chunks.jsonl", REF / "query_chunks.jsonl"]:
        if p.exists():
            docs = {}
            for line in p.open():
                d = json.loads(line)
                nid = d.get("nodeId") or d.get("source_id")
                title = d.get("title") or d.get("h1") or ""
                text = (title + "\n" + (d.get("markdown") or d.get("text") or "")).strip()
                docs[nid] = {"node_id": nid, "title": title, "text": text, "char_len": len(text)}
            return docs
    raise FileNotFoundError("query_chunks.jsonl not found")


def main():
    docs = load_corpus()
    ids = list(docs)
    print(f"corpus: {len(docs)} fragments; embedding...")
    emb = {nid: embed(d["text"]) for nid, d in docs.items()}

    samples = json.loads((HERE / "ragas_testset.json").read_text())["samples"]
    instances = []
    matched = 0
    for s in samples:
        q = s["query"]
        golds = [g for g in (s.get("source_node_ids") or []) if g in docs]
        if not golds:
            qv = embed(q)
            golds = [max(docs, key=lambda nid: cos(qv, emb[nid]))]
        else:
            matched += 1
        golds = golds[:POOL]
        gref = emb[golds[0]]
        # non-gold ranked by similarity to the gold chunk (near-duplicates first)
        ranked = sorted((nid for nid in ids if nid not in golds),
                        key=lambda nid: -cos(gref, emb[nid]))
        nearest = ranked[:8]           # top-8 confusable pool to draw the hard tier from
        rest = ranked

        def make_pool(distractors):
            pool_ids = list(golds) + distractors
            pool_ids = pool_ids[:POOL]
            random.shuffle(pool_ids)
            return [docs[i] for i in pool_ids]

        need = POOL - len(golds)
        for draw in range(N_DRAWS):
            # random tier
            instances.append({"id": f"{s.get('id','q')}_rnd_{draw:02d}", "query": q, "type": s.get("type"),
                              "tier": "random", "gold_ids": golds,
                              "pool": make_pool(random.sample(rest, need))})
            # hard tier: draw `need` from the 8 nearest
            instances.append({"id": f"{s.get('id','q')}_hard_{draw:02d}", "query": q, "type": s.get("type"),
                              "tier": "hard", "gold_ids": golds,
                              "pool": make_pool(random.sample(nearest, min(need, len(nearest))))})
            # mixed tier: half nearest, half random
            half = max(1, need // 2)
            mix = random.sample(nearest, min(half, len(nearest)))
            mix += random.sample([r for r in rest if r not in mix], need - len(mix))
            instances.append({"id": f"{s.get('id','q')}_mix_{draw:02d}", "query": q, "type": s.get("type"),
                              "tier": "mixed", "gold_ids": golds, "pool": make_pool(mix)})

    out = {"meta": {"corpus_size": len(docs), "pool_size": POOL, "n_draws_per_tier": N_DRAWS,
                    "tiers": ["random", "mixed", "hard"], "n_queries": len(samples),
                    "n_instances": len(instances), "gold_matched": matched,
                    "method": "stratified random/mixed/hard pool-of-5, ground-truth top-1"},
           "instances": instances}
    (HERE / "combo5_dataset.json").write_text(json.dumps(out, indent=2))
    from collections import Counter
    print(f"wrote combo5_dataset.json: {len(instances)} instances; gold matched {matched}/{len(samples)}")
    print("by tier:", dict(Counter(i["tier"] for i in instances)))


if __name__ == "__main__":
    main()

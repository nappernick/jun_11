# FAQ Retrieval Backend

Shared retrieval substrate for the model bake-off. It is *only* retrieval: it
returns ranked fragments with a confidence score and per-stage timings, and
stops there. Everything downstream — model choice, prompt, generation, TTFT — is
each competitor's own harness, so the comparison stays about the models, not the
back-end.

This is a throwaway local harness (lives a couple of days), so it runs on a
local venv + a Qdrant container — not a Brazil package. No npm/npx anywhere.

## What it does

```
query --> Embed v4 (Bedrock) --> dense vector ---\
      --> BM25 (local) --------> sparse vector ---+--> Qdrant RRF fusion (top N)
                                                          |
                          metadata hard filters (pre-applied, wildcard-aware)
                                                          v
                                   Rerank 3.5 (Bedrock) --> top K + confidence
```

- **Dense:** Cohere **Embed v4** via Bedrock, 1536-dim (default), `us.cohere.embed-v4:0`
  in us-west-2 (cross-region inference profile — the bare id only works in us-east-1).
- **Sparse:** BM25 computed locally (fastembed). No API call for the lexical arm.
- **Fusion:** Qdrant's Query API does RRF. We don't hand-roll the hybrid.
- **Rerank / confidence:** Cohere **Rerank 3.5** via Bedrock (`cohere.rerank-v3-5:0`).
  Bedrock does not carry Rerank 4. The returned `relevanceScore` is the confidence
  proxy — relative within a query's candidate set, not a calibrated probability.
- **Filters:** level / location / employee-class / LOB / geography applied as hard
  filters *before* retrieval, with wildcard handling (`All Job Levels` etc. match
  everyone). Status defaults to `PUBLISHED`.
- **Memoized** per distinct (query, filters, candidate_n, top_k): replaying the
  same synthetic query across trials makes zero extra Bedrock calls.

## Run

```bash
./run.sh
```

Needs Docker + Python 3.10+. If Bedrock auth has expired, run the `ada` line
printed at startup first. (Embed v4 + Rerank 3.5 must be enabled on the account.)

## HTTP contract

`POST /retrieve`
```json
{
  "query": "how do I get a corporate card?",
  "filters": { "system_job-level": "L5", "system_location-type": "Corporate" },
  "candidate_n": 20,
  "top_k": 5
}
```
`filters`, `candidate_n`, `top_k` are optional (defaults in config.py).

Response:
```json
{
  "fragments": [
    { "id": "3f14d6bf-...", "text": "Corporate Card FAQ",
      "fusion_score": 0.81, "confidence": 0.74, "metadata": { "...": "..." } }
  ],
  "timings": { "embed_query_ms": 120.4, "bm25_vectorize_ms": 1.2,
               "hybrid_search_ms": 8.7, "rerank_ms": 240.1, "total_ms": 370.6 },
  "cache_hit": false
}
```

`GET /healthz` -> collection name + point count.

Java/curl/anything hits the same JSON. Example:
```bash
curl -s localhost:8080/retrieve -H 'content-type: application/json' \
  -d '{"query":"medical accommodation for travel"}' | python3 -m json.tool
```

## Swapping in the real corpus

Drop your CSV at `data/faq_corpus.csv` (or pass a path to `python -m src.ingest`).
**Last column is content; every other column is metadata.** Multi-value cells are
pipe-delimited: `L4|L5|All Job Levels`. The only thing to verify in `config.py` is
`WILDCARD_TOKENS` — the filter field names and the literal "applies to everyone"
token each field uses. If those match your columns, ingestion just works.

## Note on the reranker version

Prod reranks with v4; Bedrock only offers 3.5, so the experiment uses 3.5. It's
identical for both competitors, so it can't move the model comparison — it's a
constant. It only means the confidence numbers won't be numerically identical to
prod's. Treat `confidence` as a relative signal; if you threshold, calibrate
against your gold links rather than assuming a fixed cutoff.

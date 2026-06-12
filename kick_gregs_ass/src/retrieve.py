"""
Retrieval core: embed query -> hybrid (dense + BM25, RRF fusion in Qdrant) ->
rerank with Rerank 3.5 -> top-k. Per-stage timings. Memoized per (query, filters, k)
so replaying the same synthetic query across model trials doesn't re-hit Bedrock.
"""
import time
import json

from qdrant_client import QdrantClient, models
from fastembed import SparseTextEmbedding

import config
from src import bedrock_client
from src.filters import build_filter

_client = QdrantClient(url=config.QDRANT_URL)
_bm25 = SparseTextEmbedding(model_name=config.BM25_MODEL)


def _retrieve_uncached(query, user_attrs, candidate_n, top_k):
    t = {}
    t0 = time.perf_counter()

    qdense = bedrock_client.embed([query], "search_query")[0]
    t["embed_query_ms"] = round((time.perf_counter() - t0) * 1000, 1); t1 = time.perf_counter()

    qsparse = next(_bm25.embed([query]))
    t["bm25_vectorize_ms"] = round((time.perf_counter() - t1) * 1000, 1); t2 = time.perf_counter()

    qfilter = build_filter(user_attrs)
    hits = _client.query_points(
        collection_name=config.COLLECTION,
        prefetch=[
            models.Prefetch(query=qdense, using=config.DENSE_VECTOR,
                            filter=qfilter, limit=candidate_n),
            models.Prefetch(
                query=models.SparseVector(indices=qsparse.indices.tolist(),
                                          values=qsparse.values.tolist()),
                using=config.SPARSE_VECTOR, filter=qfilter, limit=candidate_n),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=candidate_n,
        with_payload=True,
    ).points
    t["hybrid_search_ms"] = round((time.perf_counter() - t2) * 1000, 1); t3 = time.perf_counter()

    candidates = [
        {
            "id": h.payload.get(config.ID_COLUMN),
            "text": h.payload.get(config.CONTENT_COLUMN, ""),
            "fusion_score": h.score,
            "metadata": {k: v for k, v in h.payload.items()
                         if k != config.CONTENT_COLUMN},
        }
        for h in hits
    ]

    fragments = candidates
    if candidates:
        ranked = bedrock_client.rerank(query, [c["text"] for c in candidates], top_k)
        fragments = []
        for r in ranked:
            frag = dict(candidates[r["index"]])
            frag["confidence"] = r["score"]  # Rerank 3.5 relevanceScore
            fragments.append(frag)
    t["rerank_ms"] = round((time.perf_counter() - t3) * 1000, 1)
    t["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    return {"fragments": fragments[:top_k], "timings": t}


_CACHE = {}  # (query, attrs_json, candidate_n, top_k) -> result dict


def retrieve(query, user_attrs=None, candidate_n=None, top_k=None):
    """
    Memoized per distinct (query, filters, candidate_n, top_k). The synthetic set
    is replayed many times across model trials; this computes each distinct
    retrieval once and reuses it, so repeats make zero Bedrock calls.
    """
    candidate_n = candidate_n or config.CANDIDATE_N
    top_k = top_k or config.TOP_K
    attrs_json = json.dumps(user_attrs or {}, sort_keys=True)
    key = (query, attrs_json, candidate_n, top_k)

    if key in _CACHE:
        served = dict(_CACHE[key]); served["cache_hit"] = True
        return served

    result = _retrieve_uncached(query, json.loads(attrs_json), candidate_n, top_k)
    _CACHE[key] = result
    served = dict(result); served["cache_hit"] = False
    return served

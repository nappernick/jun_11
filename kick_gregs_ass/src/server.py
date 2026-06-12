"""
HTTP surface. The only thing a competitor's harness touches.

  POST /retrieve   {query, filters?, candidate_n?, top_k?}
                -> {fragments:[{id, text, metadata, fusion_score, confidence}],
                    timings, cache_hit}
  GET  /healthz -> {status, collection, points}

Language-agnostic JSON in / JSON out. Hit it from Java or anything else.
"""
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, Dict, Any

import config
from src.retrieve import retrieve
from src.retrieve import _client

app = FastAPI(title="FAQ Retrieval Backend", version="1.0")


class RetrieveRequest(BaseModel):
    query: str
    filters: Optional[Dict[str, Any]] = None   # e.g. {"system_job-level": "L5"}
    candidate_n: Optional[int] = None
    top_k: Optional[int] = None


@app.post("/retrieve")
def post_retrieve(req: RetrieveRequest):
    return retrieve(
        query=req.query,
        user_attrs=req.filters,
        candidate_n=req.candidate_n,
        top_k=req.top_k,
    )


@app.get("/healthz")
def healthz():
    try:
        info = _client.get_collection(config.COLLECTION)
        return {"status": "ok", "collection": config.COLLECTION,
                "points": info.points_count}
    except Exception as e:  # noqa
        return {"status": "degraded", "error": str(e)}

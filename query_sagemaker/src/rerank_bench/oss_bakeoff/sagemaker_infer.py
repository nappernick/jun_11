#!/usr/bin/env python3
"""sagemaker_infer.py — SageMaker HF-DLC custom inference handler.

Serves ALL THREE oss rerankers (ettin-1b, qwen3-0.6b, qwen3-4b, nemotron-1b-v2)
from ONE container for the LATENCY-ONLY GPU run. Models are lazy-loaded the first
time their id is requested and cached module-level, so a model loads once and is
reused across requests.

The scoring math is NOT replicated here: this handler imports models.py so the GPU
latency run shares the EXACT same code path as the local MPS quality run (no drift,
which is the whole point of the spine). models.load already wires trust_remote_code
for nemotron and fp16; we just pin device='cuda', dtype='float16'.

Request / response contract:
    input  {"model": "ettin-1b|qwen3-0.6b|qwen3-4b|nemotron-1b-v2",
            "query": str, "docs": [str]}
    output {"scores": [float], "kind": str, "model": str}

SageMaker HF-DLC calls these four entry points: model_fn(model_dir),
input_fn(body, content_type), predict_fn(data, model), output_fn(pred, accept).
"""
from __future__ import annotations

import json

# Pinned by the task: every model is served on GPU in fp16.
_DEVICE = "cuda"
_DTYPE = "float16"

# Module-level cache so each model is loaded at most once for the life of the
# container. Keyed by the exact request "model" string (same key drives routing).
_MODEL_CACHE: dict = {}


def _get_reranker(model_id):
    """Lazy-load + cache the reranker for model_id. One patchable seam so the
    self-test can route through a fake scorer without importing torch."""
    reranker = _MODEL_CACHE.get(model_id)
    if reranker is None:
        import models  # imported lazily so py_compile / tests stay torch-free
        reranker = models.load(model_id, device=_DEVICE, dtype=_DTYPE)
        _MODEL_CACHE[model_id] = reranker
    return reranker


def model_fn(model_dir):
    """Called ONCE at container startup. We cannot know which of the three models
    a request wants until predict_fn, so we do NOT load a model here — we return
    the module-level cache that predict_fn lazy-fills and reuses."""
    return _MODEL_CACHE


def input_fn(request_body, request_content_type="application/json"):
    """Deserialize the JSON request body into a dict."""
    if request_content_type not in ("application/json", None):
        raise ValueError(f"unsupported content type: {request_content_type}")
    if isinstance(request_body, (bytes, bytearray)):
        request_body = request_body.decode("utf-8")
    if isinstance(request_body, str):
        return json.loads(request_body)
    return request_body


def predict_fn(data, model):
    """Route on data["model"], lazy-load+cache that reranker, score the docs.

    `model` is whatever model_fn returned (the cache); we route through
    _get_reranker so loading and caching share the request's model string.
    """
    model_id = data["model"]
    query = data["query"]
    docs = data["docs"]
    reranker = _get_reranker(model_id)
    scores = reranker.score_pairs(query, docs)
    return {
        "scores": [float(s) for s in scores],
        "kind": reranker.kind,
        "model": model_id,
    }


def output_fn(prediction, accept="application/json"):
    """Serialize the prediction dict to a JSON response body STRING.

    Return a plain JSON string, NOT a (body, content_type) tuple: the HF DLC
    inference toolkit json-encodes whatever output_fn returns, so a tuple comes
    back to the client as a 2-element list ['{...}', 'application/json'] and the
    scores get rejected as malformed. A bare string round-trips cleanly. The
    client (deploy_bench._parse_body) also unwraps defensively as a backstop."""
    if accept not in ("application/json", None):
        raise ValueError(f"unsupported accept type: {accept}")
    return json.dumps(prediction)

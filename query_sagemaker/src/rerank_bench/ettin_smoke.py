#!/usr/bin/env python3
"""Ettin reranker proof-of-life via sentence-transformers CrossEncoder.

WHY CrossEncoder and not AutoModelForSequenceClassification:
  cross-encoder/ettin-reranker-1b-v1 ships as a sentence-transformers model.
  Its scoring head lives in separate modules (2_Dense/, 3_LayerNorm/, 4_Dense/)
  wired by modules.json. Loading the bare ModernBert backbone with
  AutoModelForSequenceClassification silently initializes a RANDOM head
  (relevant scored below irrelevant). CrossEncoder loads the trained head.

Ettin emits one raw score per (query, doc) pair. Order = sort on that score.
"""
from __future__ import annotations

import sys
import time

import torch
from sentence_transformers import CrossEncoder

MODEL_ID = sys.argv[1] if len(sys.argv) > 1 else "cross-encoder/ettin-reranker-1b-v1"

QUERY = "How do I book a flight for business travel?"
DOC_RELEVANT = (
    "To book business travel, use the Concur travel portal. Search for your "
    "flight, select it, and submit for manager approval. Approved itineraries "
    "are ticketed automatically."
)
DOC_IRRELEVANT = (
    "The cafeteria menu rotates weekly. Vegetarian and vegan options are "
    "available daily at the salad bar on the third floor."
)


def device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def main() -> int:
    dev = device()
    print(f"[load] model={MODEL_ID} device={dev}")
    t0 = time.perf_counter()
    model = CrossEncoder(MODEL_ID, device=dev)
    t1 = time.perf_counter()
    print(f"[load] done in {t1 - t0:.1f}s  max_length={model.max_length}")

    pairs = [(QUERY, DOC_RELEVANT), (QUERY, DOC_IRRELEVANT)]
    ts = time.perf_counter()
    scores = model.predict(pairs)
    te = time.perf_counter()

    print(f"[score] query={QUERY!r}  total_forward={(te - ts) * 1000:.0f}ms")
    print(f"  relevant    raw_score={float(scores[0]):+.4f}")
    print(f"  irrelevant  raw_score={float(scores[1]):+.4f}")

    ok = float(scores[0]) > float(scores[1])
    print(f"[{'ok' if ok else 'FAIL'}] relevant {'>' if ok else '<='} irrelevant "
          f"-> trained head {'confirmed' if ok else 'NOT loaded'}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

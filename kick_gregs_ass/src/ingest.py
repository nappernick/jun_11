"""
One-time, offline ingestion. Reads the CSV, embeds every fragment with Embed v4,
computes BM25 sparse vectors locally, and upserts into Qdrant. Idempotent:
re-running recreates the collection from scratch.

Run:  python -m src.ingest
"""
import csv
import sys
from qdrant_client import QdrantClient, models
from fastembed import SparseTextEmbedding

import config
from src import bedrock_client

BATCH = 90  # Cohere embed accepts up to 96 texts/call; stay under.


def load_rows(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def to_payload(row):
    """Every column except content becomes payload. Multi-value cells -> lists."""
    payload = {}
    for k, v in row.items():
        if k == config.CONTENT_COLUMN:
            continue
        if v is not None and config.MULTIVALUE_DELIMITER in v:
            payload[k] = [s.strip() for s in v.split(config.MULTIVALUE_DELIMITER) if s.strip()]
        else:
            payload[k] = v
    payload[config.CONTENT_COLUMN] = row[config.CONTENT_COLUMN]  # keep text for rerank
    return payload


def main(csv_path="data/faq_corpus.csv"):
    rows = load_rows(csv_path)
    texts = [r[config.CONTENT_COLUMN] for r in rows]
    print(f"Loaded {len(rows)} fragments from {csv_path}")

    # Dense vectors via Embed v4 (Bedrock), batched.
    dense = []
    for i in range(0, len(texts), BATCH):
        dense.extend(bedrock_client.embed(texts[i:i + BATCH], "search_document"))
    print(f"Embedded {len(dense)} fragments (dim={len(dense[0])})")

    # Sparse BM25 vectors, computed locally.
    bm25 = SparseTextEmbedding(model_name=config.BM25_MODEL)
    sparse = list(bm25.embed(texts))

    client = QdrantClient(url=config.QDRANT_URL)
    client.recreate_collection(
        collection_name=config.COLLECTION,
        vectors_config={
            config.DENSE_VECTOR: models.VectorParams(
                size=config.EMBED_DIM, distance=models.Distance.COSINE
            )
        },
        sparse_vectors_config={
            config.SPARSE_VECTOR: models.SparseVectorParams(
                modifier=models.Modifier.IDF  # BM25 needs IDF weighting
            )
        },
    )

    points = []
    for idx, row in enumerate(rows):
        sv = sparse[idx]
        points.append(
            models.PointStruct(
                id=idx,
                vector={
                    config.DENSE_VECTOR: dense[idx],
                    config.SPARSE_VECTOR: models.SparseVector(
                        indices=sv.indices.tolist(), values=sv.values.tolist()
                    ),
                },
                payload=to_payload(row),
            )
        )
    client.upsert(collection_name=config.COLLECTION, points=points)
    print(f"Upserted {len(points)} points into '{config.COLLECTION}'. Ingestion done.")


if __name__ == "__main__":
    main(*(sys.argv[1:]))

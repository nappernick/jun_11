#!/usr/bin/env python3
"""Retrieve REAL BM25 candidate pools from the live faq_evidence_a OpenSearch
index (alpha acct, us-west-2) for every query in queries.json, and freeze them
to pools.json. Every reranker in the bakeoff scores these IDENTICAL pools, so
the comparison is apples-to-apples and nothing is generated from a gold doc
(the saturation failure mode of the prior labeled eval is avoided by construction).

  python retrieve.py            # default pool size 20
  python retrieve.py --size 16
"""
import argparse
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

HERE = Path(__file__).parent
ALPHA_PROFILE = "alpha"
ENDPOINT = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com"
REGION, SERVICE, INDEX = "us-west-2", "aoss", "faq_evidence_a"

_sess = boto3.Session(profile_name=ALPHA_PROFILE, region_name=REGION)
_creds = _sess.get_credentials().get_frozen_credentials()


def signed(method, path, body=None):
    url = ENDPOINT + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    headers["X-Amz-Content-SHA256"] = hashlib.sha256(data or b"").hexdigest()
    req = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(_creds, SERVICE, REGION).add_auth(req)
    return requests.request(method, url, headers=dict(req.headers), data=data)


def doc_text(src):
    title = (src.get("source_metadata") or {}).get("title") or ""
    return f"{title}\n{src.get('text', '')}".strip()


def retrieve(query, size):
    r = signed("POST", f"/{INDEX}/_search", {
        "size": size,
        "_source": ["source_id", "text", "source_metadata"],
        "query": {"match": {"text": query}},
    })
    r.raise_for_status()
    return [h["_source"] for h in r.json()["hits"]["hits"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=20, help="BM25 pool size per query")
    args = ap.parse_args()

    queries = json.loads((HERE / "queries.json").read_text())["queries"]
    pools = {}
    t0 = time.time()
    for i, q in enumerate(queries):
        cands = retrieve(q, args.size)
        items = []
        for c in cands:
            text = doc_text(c)
            items.append({
                "node_id": c.get("source_id"),
                "title": (c.get("source_metadata") or {}).get("title") or "",
                "text": text,
                "char_len": len(text),
            })
        pools[q] = items
        print(f"[{i+1:2d}/{len(queries)}] pool={len(items):2d}  {q[:55]}")

    out = {
        "meta": {
            "index": INDEX,
            "pool_size": args.size,
            "n_queries": len(queries),
            "generated_at": datetime.now().isoformat(),
            "retrieval": "BM25 match on text field, live faq_evidence_a",
        },
        "pools": pools,
    }
    (HERE / "pools.json").write_text(json.dumps(out, indent=2))
    pool_sizes = [len(v) for v in pools.values()]
    print(f"\nWrote pools.json: {len(pools)} queries, "
          f"pool sizes min={min(pool_sizes)} max={max(pool_sizes)} "
          f"in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()

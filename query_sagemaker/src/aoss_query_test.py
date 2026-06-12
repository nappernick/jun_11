#!/usr/bin/env python3
"""Validate the single OpenSearch hybrid (BM25 + kNN) query against the deployed
alpha FAQ index, fused by the skywalker-faq-hybrid search pipeline.

This is ONE query: a `hybrid` query with two leg clauses (a BM25 `match` on
`text` and a `knn` clause on `embedding`), with the search pipeline's
normalization-processor combining the two score sets. Reusing a stored doc's
embedding as the probe vector exercises the vector leg without calling Bedrock.
"""
import hashlib
import json
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import requests

ENDPOINT = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com"
REGION = "us-west-2"
SERVICE = "aoss"
INDEX = "faq_evidence_a"

_session = boto3.Session(profile_name="alpha")
_creds = _session.get_credentials().get_frozen_credentials()


def signed(method: str, path: str, body: dict | None = None) -> requests.Response:
    url = ENDPOINT + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    # AOSS requires the payload-hash header on signed requests, including bodied
    # GET/POST. Without it AOSS returns a bare 403 Forbidden.
    headers["X-Amz-Content-SHA256"] = hashlib.sha256(data if data else b"").hexdigest()
    req = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(_creds, SERVICE, REGION).add_auth(req)
    return requests.request(method, url, headers=dict(req.headers), data=data)


# Grab a probe vector from an existing doc (no Bedrock needed).
seed = signed("GET", f"/{INDEX}/_search?size=1").json()
seed_src = seed["hits"]["hits"][0]["_source"]
probe_vec = seed_src["embedding"]
print("Probe vector dim:", len(probe_vec))
print()

QTEXT = "hotel accommodation booking"

print("=== Single hybrid query (BM25 match + kNN), fused by skywalker-faq-hybrid ===")
r = signed("POST", f"/{INDEX}/_search?search_pipeline=skywalker-faq-hybrid", {
    "size": 5,
    "_source": ["source_metadata.title"],
    "query": {"hybrid": {"queries": [
        {"match": {"text": QTEXT}},
        {"knn": {"embedding": {"vector": probe_vec, "k": 40}}},
    ]}},
})
print("HTTP", r.status_code)
if r.status_code != 200:
    print(r.text[:1500])
else:
    for h in r.json()["hits"]["hits"]:
        title = h.get("_source", {}).get("source_metadata", {}).get("title", "<no title>")
        print(f"  {round(h.get('_score', 0), 4)}  {title}")

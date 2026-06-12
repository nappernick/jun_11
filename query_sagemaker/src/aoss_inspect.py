#!/usr/bin/env python3
"""Read-only inspection of the alpha Top-50 FAQ index to assess build quality:
fragment granularity, per-fragment token size vs the 4096 rerank window, embedding
dim, scope-tag coverage, titles, follow-up linkage, and corpus_version consistency.
"""
import hashlib
import json
import statistics
from collections import Counter

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ENDPOINT = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com"
REGION = "us-west-2"
SERVICE = "aoss"
INDEX = "faq_evidence_a"
CHARS_PER_TOKEN = 4.0
RERANK_WINDOW = 4096

_creds = boto3.Session(profile_name="alpha").get_credentials().get_frozen_credentials()


def signed(method, path, body=None):
    url = ENDPOINT + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    headers["X-Amz-Content-SHA256"] = hashlib.sha256(data if data else b"").hexdigest()
    req = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(_creds, SERVICE, REGION).add_auth(req)
    return requests.request(method, url, headers=dict(req.headers), data=data)


def pct(values, p):
    if not values:
        return 0
    return statistics.quantiles(values, n=100)[p - 1] if len(values) > 1 else values[0]


print("=== indices ===")
print(signed("GET", "/_cat/indices?format=json&v").text)

mapping = signed("GET", f"/{INDEX}/_mapping").json()
props = mapping[INDEX]["mappings"]["properties"]
emb = props.get("embedding", {})
print(f"\n=== mapping ({INDEX}) ===")
print("fields:", sorted(props.keys()))
print("embedding:", emb.get("type"), "dim:", emb.get("dimension"),
      "method:", emb.get("method"))

# Pull all docs without the embedding vector (keep payload small).
resp = signed("POST", f"/{INDEX}/_search",
              {"size": 500, "_source": {"excludes": ["embedding"]},
               "query": {"match_all": {}}})
hits = [h["_source"] for h in resp.json()["hits"]["hits"]]
print(f"\n=== {len(hits)} documents ===")

# Confirm a real embedding vector length on one doc.
one = signed("POST", f"/{INDEX}/_search",
             {"size": 1, "_source": ["embedding"], "query": {"match_all": {}}}).json()
vec = one["hits"]["hits"][0]["_source"].get("embedding", [])
print("actual embedding vector length:", len(vec))

# Fragment granularity.
sids = Counter(h.get("source_id") for h in hits)
frag_counts = Counter(sids.values())
print("\n=== fragmentation ===")
print("distinct source_id:", len(sids), "| total fragments:", len(hits))
print("fragments-per-source distribution:", dict(sorted(frag_counts.items())))

# Text length / token estimate vs rerank window.
tok = sorted(len(h.get("text", "") or "") / CHARS_PER_TOKEN for h in hits)
overflow = sum(1 for t in tok if t > RERANK_WINDOW)
near = sum(1 for t in tok if t > RERANK_WINDOW * 0.75)
print("\n=== text size (est tokens = chars/4) ===")
print(f"min={tok[0]:.0f} median={statistics.median(tok):.0f} "
      f"p90={pct(tok,90):.0f} max={tok[-1]:.0f}")
print(f"docs > {RERANK_WINDOW} tok (overflow): {overflow}/{len(tok)} | "
      f"> {int(RERANK_WINDOW*0.75)} tok (near): {near}/{len(tok)}")

# Scope-tag coverage.
print("\n=== scope coverage ===")
for field in ("country", "level", "role"):
    present = sum(1 for h in hits if h.get(field))
    values = Counter(v for h in hits for v in (h.get(field) or []))
    print(f"{field}: {present}/{len(hits)} populated | values={dict(values)}")

# Titles, follow-up linkage, content type, corpus version.
titled = sum(1 for h in hits if (h.get("source_metadata") or {}).get("title"))
followups = sum(1 for h in hits if h.get("followup_fragment_ids"))
print("\n=== other fields ===")
print(f"with source_metadata.title: {titled}/{len(hits)}")
print(f"with followup_fragment_ids: {followups}/{len(hits)}")
print("content_type:", dict(Counter(h.get("content_type") for h in hits)))


def tally(field):
    counter = Counter()
    for h in hits:
        value = h.get(field)
        counter.update(value if isinstance(value, list) else [value])
    return dict(counter)


print("line_of_business:", tally("line_of_business"))
print("corpus_version:", tally("corpus_version"))

#!/usr/bin/env python3
"""Read-only: show the longest FAQ documents (title, size, snippet) to judge outliers."""
import hashlib
import json

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ENDPOINT = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com"
REGION, SERVICE, INDEX = "us-west-2", "aoss", "faq_evidence_a"
_creds = boto3.Session(profile_name="alpha").get_credentials().get_frozen_credentials()


def signed(method, path, body=None):
    url = ENDPOINT + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    headers["X-Amz-Content-SHA256"] = hashlib.sha256(data if data else b"").hexdigest()
    req = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(_creds, SERVICE, REGION).add_auth(req)
    return requests.request(method, url, headers=dict(req.headers), data=data)


resp = signed("POST", f"/{INDEX}/_search",
              {"size": 500, "_source": {"excludes": ["embedding"]},
               "query": {"match_all": {}}})
docs = [h["_source"] for h in resp.json()["hits"]["hits"]]
docs.sort(key=lambda d: len(d.get("text", "") or ""), reverse=True)

for d in docs[:8]:
    text = d.get("text", "") or ""
    title = (d.get("source_metadata") or {}).get("title", "<no title>")
    print(f"\n{'='*80}\nchars={len(text)} est_tokens={len(text)//4} "
          f"country={d.get('country')} content_type={d.get('content_type')}")
    print(f"title: {title}")
    print(f"snippet: {text[:300]!r}")
    print(f"...tail: {text[-200:]!r}")

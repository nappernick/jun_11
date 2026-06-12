#!/usr/bin/env python3
"""Cross-check the ISO-3 mapping file against the country values actually present
in the alpha index, and verify sentinel-aware OR matching returns the union."""
import hashlib
import json
from collections import Counter

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


iso = json.load(open("country_iso_alpha3.json"))
docs = [h["_source"] for h in signed("POST", f"/{INDEX}/_search",
        {"size": 500, "_source": ["country"], "query": {"match_all": {}}}).json()["hits"]["hits"]]
country_vals = Counter(v for d in docs for v in (d.get("country") or []))

print("=== country values in index NOT resolvable by country_iso_alpha3.json ===")
for val in sorted(country_vals):
    if val not in iso:
        print(f"  {val!r}  ({country_vals[val]} docs)")

print("\n=== sentinel-aware OR match: country in [United States, Global] ===")
body = {"size": 0, "query": {"terms": {"country": ["United States", "Global"]}}}
print("  hits:", signed("POST", f"/{INDEX}/_search", body).json()["hits"]["total"]["value"])
print("  (United States alone = 4, Global alone = 35)")

#!/usr/bin/env python3
"""Inspect the RAW CoreX taxonomy LABEL values carried in source_metadata
(geography, system_job-level, system_employee-class) vs the mapped top-level
country/level/role, to confirm the exact shape of the sentinel tokens.
"""
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


docs = [h["_source"] for h in signed("POST", f"/{INDEX}/_search",
        {"size": 500,
         "_source": ["country", "level", "role", "source_metadata.geography",
                     "source_metadata.system_job-level",
                     "source_metadata.system_employee-class"],
         "query": {"match_all": {}}}).json()["hits"]["hits"]]


def tally_raw(field):
    c = Counter()
    for d in docs:
        v = (d.get("source_metadata") or {}).get(field)
        if isinstance(v, list):
            c.update(v)
        elif v is not None:
            c.update([v])
    return c


def tally_top(field):
    c = Counter()
    for d in docs:
        for v in (d.get(field) or []):
            c.update([v])
    return c


print("=== COUNTRY ===")
print("raw source_metadata.geography :", dict(tally_raw("geography")))
print("mapped top-level country      :", dict(tally_top("country")))
print("\n=== LEVEL ===")
print("raw source_metadata.system_job-level :", dict(tally_raw("system_job-level")))
print("mapped top-level level               :", dict(tally_top("level")))
print("\n=== ROLE / EMPLOYEE CLASS ===")
print("raw source_metadata.system_employee-class :", dict(tally_raw("system_employee-class")))
print("mapped top-level role                     :", dict(tally_top("role")))

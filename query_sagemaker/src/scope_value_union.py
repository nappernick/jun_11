#!/usr/bin/env python3
"""Union the scope-taxonomy value universe from BOTH sources:
  (A) live CoreX beta metadata for our domain-owner nodes (corex_taxonomy_values.json), and
  (B) the broader alpha OpenSearch index (56 ingested docs) raw source_metadata taxonomy.
Produces the fullest observed value set for geography / system_job-level /
system_employee-class / system_line-of-business, plus the confirmed sentinel tokens.
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


# (B) Alpha index raw taxonomy from source_metadata.
docs = [h["_source"] for h in signed("POST", f"/{INDEX}/_search",
        {"size": 500, "_source": ["source_metadata.geography",
                                   "source_metadata.system_job-level",
                                   "source_metadata.system_employee-class",
                                   "source_metadata.lineOfBusiness"],
         "query": {"match_all": {}}}).json()["hits"]["hits"]]

idx = {"geography": Counter(), "system_job-level": Counter(),
       "system_employee-class": Counter(), "lineOfBusiness": Counter()}
for d in docs:
    md = d.get("source_metadata") or {}
    for field in idx:
        v = md.get(field)
        if isinstance(v, list):
            idx[field].update(v)
        elif v is not None:
            idx[field].update([v])

# (A) Live CoreX values.
corex = json.load(open("corex_taxonomy_values.json"))

def union(field_corex, field_idx):
    return sorted(set(corex.get(field_corex, {})) | set(idx[field_idx]))

print("=== GEOGRAPHY (union) ===")
print(union("geography", "geography"))
print("\n=== system_job-level (union) ===")
print(union("system_job-level", "system_job-level"))
print("\n=== system_employee-class (union) ===")
for v in union("system_employee-class", "system_employee-class"):
    print("  ", v)
print("\n=== line-of-business (union; alpha index uses source_metadata.lineOfBusiness) ===")
print(sorted(set(corex.get("system_line-of-business", {})) | set(idx["lineOfBusiness"])))

print("\n=== CONFIRMED SENTINEL TOKENS (the 'applies to everyone' value per axis) ===")
print("  geography            -> 'Global'")
print("  system_job-level     -> 'All Job Levels'")
print("  system_employee-class-> 'All Employee Classes'")
print("  line-of-business     -> 'All LOBs'")

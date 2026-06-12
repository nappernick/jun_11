#!/usr/bin/env python3
"""Reproduce the Java client's hybrid query + scope filter against the live alpha
index to check whether a realistic scoped request returns any hits.

Mirrors AossHybridRetrievalClient.buildQueryBody: hybrid[ bool{must:match, filter:terms},
knn{filter:bool{filter:terms}} ], scope = terms on country/level/role.
"""
import hashlib
import json
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import requests

ENDPOINT = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com"
REGION, SERVICE, INDEX = "us-west-2", "aoss", "faq_evidence_a"
_creds = boto3.Session(profile_name="alpha").get_credentials().get_frozen_credentials()


def signed(method, path, body):
    data = json.dumps(body).encode()
    url = ENDPOINT + path
    headers = {"Content-Type": "application/json",
               "X-Amz-Content-SHA256": hashlib.sha256(data).hexdigest()}
    req = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(_creds, SERVICE, REGION).add_auth(req)
    return requests.request(method, url, headers=dict(req.headers), data=data)


# A probe vector from a real doc so the knn leg is valid.
seed = signed("POST", f"/{INDEX}/_search", {"size": 1}).json()
probe_vec = seed["hits"]["hits"][0]["_source"]["embedding"]


def terms(field, value):
    return {"terms": {field: [value]}}


def scope_filter(country, level, role):
    return [terms("country", country), terms("level", level), terms("role", role)]


def hybrid_body(country, level, role):
    return {
        "size": 20,
        "_source": ["source_id", "text", "source_metadata", "country", "level", "role",
                    "source_url", "policy_links"],
        "query": {"hybrid": {"queries": [
            {"bool": {"must": [{"match": {"text": "hotel booking"}}],
                      "filter": scope_filter(country, level, role)}},
            {"knn": {"embedding": {"vector": probe_vec, "k": 20,
                                   "filter": {"bool": {"filter": scope_filter(country, level, role)}}}}},
        ]}},
    }


def count(country, level, role, label):
    r = signed("POST", f"/{INDEX}/_search?search_pipeline=skywalker-faq-hybrid",
               hybrid_body(country, level, role))
    n = r.json().get("hits", {}).get("total", {}).get("value", "ERR") if r.status_code == 200 else r.text[:200]
    print(f"  scope={label:50s} HTTP={r.status_code} hits={n}")


print("=== Realistic scoped request (what the service will actually send) ===")
count("US", "L5", "INDIVIDUAL_CONTRIBUTOR", "US / L5 / INDIVIDUAL_CONTRIBUTOR")
count("USA", "L5", "INDIVIDUAL_CONTRIBUTOR", "USA(alpha3) / L5 / INDIVIDUAL_CONTRIBUTOR")
print("=== Sentinel values actually present in the corpus ===")
count("Global", "All Job Levels", "All Employee Classes", "Global / All Job Levels / All Employee Classes")

print("\n=== Where do source_url / policy_links live? (top-level vs source_metadata) ===")
doc = seed["hits"]["hits"][0]["_source"]
print("  top-level keys:", sorted(doc.keys()))
print("  source_metadata keys:", sorted(doc.get("source_metadata", {}).keys()))

#!/usr/bin/env python3
"""Gather evidence on the POST _search 403 against the alpha FAQ collection.

Hypothesis to test (not yet confirmed): AOSS rejects bodied requests whose
X-Amz-Content-SHA256 header is absent/mismatched. We test by (a) printing the
exact headers botocore produced, and (b) explicitly setting the payload hash
header before signing.
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

BODY = {"size": 1, "query": {"match_all": {}}}


def run(label, set_sha_header):
    data = json.dumps(BODY).encode("utf-8")
    url = f"{ENDPOINT}/{INDEX}/_search"
    headers = {"Content-Type": "application/json"}
    if set_sha_header:
        headers["X-Amz-Content-SHA256"] = hashlib.sha256(data).hexdigest()
    req = AWSRequest(method="POST", url=url, data=data, headers=headers)
    SigV4Auth(_creds, SERVICE, REGION).add_auth(req)
    signed_headers = dict(req.headers)
    print(f"--- {label} ---")
    print("  signed header keys:", sorted(signed_headers.keys()))
    print("  has X-Amz-Content-SHA256:", "X-Amz-Content-SHA256" in signed_headers)
    r = requests.post(url, headers=signed_headers, data=data)
    print("  HTTP", r.status_code, r.text[:160])
    print()


run("POST as-is (no explicit sha header)", set_sha_header=False)
run("POST with explicit X-Amz-Content-SHA256", set_sha_header=True)

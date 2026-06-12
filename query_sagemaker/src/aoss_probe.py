#!/usr/bin/env python3
"""Minimal SigV4-signed GET helper for probing the alpha AOSS collection.

Usage: python3 aoss_probe.py <path>
Example: python3 aoss_probe.py /_cat/indices?v
"""
import sys
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import requests

ENDPOINT = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com"
REGION = "us-west-2"
SERVICE = "aoss"


def signed_get(path: str) -> requests.Response:
    session = boto3.Session(profile_name="alpha")
    creds = session.get_credentials().get_frozen_credentials()
    url = ENDPOINT + path
    req = AWSRequest(method="GET", url=url)
    SigV4Auth(creds, SERVICE, REGION).add_auth(req)
    return requests.get(url, headers=dict(req.headers))


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/_cat/indices?v"
    resp = signed_get(path)
    print("HTTP", resp.status_code)
    print(resp.text)

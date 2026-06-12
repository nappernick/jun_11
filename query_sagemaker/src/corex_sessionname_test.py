#!/usr/bin/env python3
"""Confirm the root cause of prior COREx AccessDenied: the role-session-name
prefix. Same identity (profile beta), same ExternalId; only the session name
varies. Prints only pass/fail + error code (no secrets)."""
import json
import boto3
from botocore.exceptions import ClientError

ROLE_ARN = "arn:aws:iam::975754358161:role/ATESkywalker-CORExApiRole-beta"
SECRET_ID = "ATESkywalkerIngest/alpha/corex"
REGION = "us-west-2"

session = boto3.Session(profile_name="beta", region_name=REGION)
external_id = json.loads(
    session.client("secretsmanager").get_secret_value(SecretId=SECRET_ID)["SecretString"])["ExternalId"]
sts = session.client("sts")

cases = [
    ("corex-fetch-owned-content-1748", "non-conforming (what the old script used)"),
    ("review-session", "non-conforming (no ATESkywalker prefix)"),
    ("ATESkywalker-review", "conforming (ATESkywalker* prefix)"),
    ("ATESkywalkerIngest-x", "conforming (ATESkywalker* prefix)"),
]
for name, note in cases:
    try:
        sts.assume_role(RoleArn=ROLE_ARN, RoleSessionName=name, ExternalId=external_id)
        print(f"PASS  {name!r:40s} {note}")
    except ClientError as e:
        print(f"FAIL  {name!r:40s} {e.response['Error']['Code']}  -- {note}")

#!/usr/bin/env python3
"""Test the documented COREx human access path (API docs section 9.2):
profile `beta` (conduit into our account 948580600005) -> sts assume-role into the
COREx role using the secret's ExternalId and a customer-prefixed session name.

Prints ONLY non-secret evidence: which session name worked and the assumed-role ARN.
Never prints ApiKey, ExternalId, or temp credential material.
"""
import sys
import boto3
from botocore.exceptions import ClientError

ROLE_ARN = "arn:aws:iam::975754358161:role/ATESkywalker-CORExApiRole-beta"
SECRET_ID = "ATESkywalkerIngest/alpha/corex"
REGION = "us-west-2"

session = boto3.Session(profile_name="beta", region_name=REGION)
secret = session.client("secretsmanager").get_secret_value(SecretId=SECRET_ID)
import json
sd = json.loads(secret["SecretString"])
external_id = sd["ExternalId"]
print("secret keys present:", sorted(sd.keys()))  # names only, not values

sts = session.client("sts")
# Candidate session-name prefixes; the trust policy may require a specific one.
candidates = ["ATESkywalker-review", "ATESkywalker", "Skywalker-review", "ATESkywalkerIngest"]
for name in candidates:
    try:
        resp = sts.assume_role(
            RoleArn=ROLE_ARN, RoleSessionName=name, ExternalId=external_id)
        print(f"SUCCESS session_name={name!r} -> {resp['AssumedRoleUser']['Arn']}")
        sys.exit(0)
    except ClientError as e:
        print(f"FAIL session_name={name!r}: {e.response['Error']['Code']}: "
              f"{e.response['Error']['Message'][:160]}")

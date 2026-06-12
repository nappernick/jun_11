#!/usr/bin/env python3
"""Enumerate ALL Skywalker FAQ nodes in CoreX beta (domainOwner + contentType filter),
page through searchContent, batch metadata via getContentNodes (<=50/call), and
aggregate every distinct value for the scope taxonomy fields:
  system_employee-class, system_job-level, system_line-of-business, geography,
plus a generic dump of every system_* metadata key's value universe.
"""
import json
from collections import Counter, defaultdict

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
import requests

ROLE_ARN = "arn:aws:iam::975754358161:role/ATESkywalker-CORExApiRole-beta"
SECRET_ID = "ATESkywalkerIngest/alpha/corex"
HOST = "corex-api.beta.corex.pxt.amazon.dev"
REGION = "us-west-2"
DOMAIN_OWNER = "amzn1.abacus.team.looo53floubmzytmswva"

session = boto3.Session(profile_name="beta", region_name=REGION)
sd = json.loads(session.client("secretsmanager").get_secret_value(SecretId=SECRET_ID)["SecretString"])
api_key, external_id = sd["ApiKey"], sd["ExternalId"]
assumed = session.client("sts").assume_role(
    RoleArn=ROLE_ARN, RoleSessionName="ATESkywalker-review", ExternalId=external_id)["Credentials"]
creds = Credentials(assumed["AccessKeyId"], assumed["SecretAccessKey"], assumed["SessionToken"])


def call(path, body):
    url = f"https://{HOST}{path}"
    data = json.dumps(body)
    req = AWSRequest(method="POST", url=url, data=data,
                     headers={"Content-Type": "application/json", "x-api-key": api_key, "host": HOST})
    SigV4Auth(creds, "execute-api", REGION).add_auth(req)
    r = requests.post(url, headers=dict(req.headers), data=data)
    return r.status_code, r.text


SEARCH_Q = ("query searchContent($search: SearchInput!) { searchContent(search: $search) "
            "{ status statusCode errorMessage payload { data { nodeId title status } "
            "pagination { limit pageNumber total } } } }")

# domainOwner (GLOBALSTATE) only — the proven filter.
FILTERS = [
    {"id": "domainOwner", "value": [DOMAIN_OWNER], "columnType": "STRING", "group": "GLOBALSTATE"},
]


def search_page(page, limit=50):
    body = {"query": SEARCH_Q, "variables": {"search": {
        "query": "", "filters": FILTERS, "pagination": {"limit": limit, "pageNumber": page}, "sorting": None}}}
    sc, st = call("/search/graphql", body)
    js = json.loads(st)
    payload = (((js.get("data") or {}).get("searchContent") or {}).get("payload"))
    err = (((js.get("data") or {}).get("searchContent") or {}).get("errorMessage"))
    return sc, payload, err, js


# 1. Page through search to collect all node IDs.
all_ids, page, total = [], 1, None
while True:
    sc, payload, err, js = search_page(page)
    if not payload:
        print(f"search page {page}: HTTP {sc} data=null err={err} raw={json.dumps(js)[:300]}")
        break
    rows = payload.get("data") or []
    total = payload.get("pagination", {}).get("total")
    all_ids.extend(r["nodeId"] for r in rows)
    print(f"search page {page}: HTTP {sc} got {len(rows)} (running {len(all_ids)}/{total})")
    if len(rows) == 0 or (total is not None and len(all_ids) >= total):
        break
    page += 1
    if page > 50:
        print("stopping at 50 pages safety cap")
        break

print(f"\nTOTAL Skywalker FAQ nodes enumerated: {len(all_ids)}")
if not all_ids:
    raise SystemExit("no nodes found; check filter shape above")

# 2. Batch metadata via getContentNodes (<=50 per call).
NODE_FIELDS = ("nodeId title status geography topics metadata "
               "globalState { domainOwner managedBy }")
PLURAL_Q = ("query GetContentNodes($input: GetContentNodesInput!) { getContentNodes(input: $input) "
            "{ status statusCode errorMessage payload { nodes { " + NODE_FIELDS
            + " } unprocessedNodes { nodeId } } } }")


def fetch_batch(ids):
    body = {"query": PLURAL_Q, "variables": {"input": {
        "nodes": [{"nodeId": n} for n in ids], "returnFieldVersions": True, "returnTaxonomyValues": "LABEL"}}}
    sc, st = call("/infoarch/getContentNodes/graphql", body)
    payload = (((json.loads(st).get("data") or {}).get("getContentNodes") or {}).get("payload")) or {}
    return payload.get("nodes", []), payload.get("unprocessedNodes", [])


nodes = []
for i in range(0, len(all_ids), 50):
    batch = all_ids[i:i + 50]
    got, unprocessed = fetch_batch(batch)
    nodes.extend(got)
    print(f"fetch batch {i//50+1}: {len(got)} nodes, {len(unprocessed)} unprocessed")

# 3. Aggregate distinct taxonomy values.
geography = Counter()
meta_universe = defaultdict(Counter)
for n in nodes:
    for g in (n.get("geography") or []):
        geography[g] += 1
    md = n.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except json.JSONDecodeError:
            continue
    if isinstance(md, dict):
        for k, v in md.items():
            vals = v if isinstance(v, list) else [v]
            for val in vals:
                meta_universe[k][str(val)] += 1

print("\n" + "=" * 70)
print("GEOGRAPHY — distinct values:")
for v, c in sorted(geography.items()):
    print(f"  {c:4d}  {v!r}")

for key in ("system_employee-class", "system_job-level", "system_line-of-business"):
    print("\n" + "=" * 70)
    print(f"{key} — distinct values:")
    for v, c in sorted(meta_universe.get(key, {}).items()):
        print(f"  {c:4d}  {v!r}")

print("\n" + "=" * 70)
print("ALL metadata keys seen:", sorted(meta_universe.keys()))

out = {
    "node_count": len(nodes),
    "geography": dict(geography),
    "system_employee-class": dict(meta_universe.get("system_employee-class", {})),
    "system_job-level": dict(meta_universe.get("system_job-level", {})),
    "system_line-of-business": dict(meta_universe.get("system_line-of-business", {})),
    "all_metadata_keys": sorted(meta_universe.keys()),
}
with open("corex_taxonomy_values.json", "w") as f:
    json.dump(out, f, indent=2)
print("\nwrote corex_taxonomy_values.json")

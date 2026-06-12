#!/usr/bin/env python3
"""Definitive COREx beta proof, following the proven ingestion methodology exactly:
profile beta -> read secret -> assume role (ExternalId + ATESkywalker* session) ->
SigV4 execute-api -> searchContent by globalState.domainOwner -> singular
getContentNode per node id with returnTaxonomyValues: LABEL.

Captures the real scope-tag shape (geography + metadata) the query-side redesign needs.
"""
import json
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


# 1. Enumerate by domainOwner using the proven search arg shape: search: SearchInput!
search_q = ("query searchContent($search: SearchInput!) { searchContent(search: $search) "
            "{ status statusCode errorMessage payload { data { nodeId title status } "
            "pagination { total } } } }")
search_vars = {"search": {"query": "",
                          "filters": [{"id": "domainOwner", "value": [DOMAIN_OWNER],
                                       "columnType": "STRING", "group": "GLOBALSTATE"}],
                          "pagination": {"limit": 50, "pageNumber": 1}, "sorting": None}}
sc, st = call("/search/graphql", {"query": search_q, "variables": search_vars})
print("=== searchContent === HTTP", sc)
data = json.loads(st).get("data") or {}
results = (((data.get("searchContent") or {}).get("payload") or {}).get("data")) or []
print("total nodes:", len(results))
for r in results:
    print("  ", r["nodeId"], "|", r.get("status"), "|", r.get("title"))

# 2. Fetch first node via SINGULAR getContentNode with taxonomy LABELs.
if results:
    beta_ids = [r["nodeId"] for r in results]

    # Metadata-only: request EVERY metadata field explicitly, but NOT `content`
    # (content is not accessible to this principal; selecting it nulls the resolver).
    node_fields = ("nodeId version parentVersion createdDate createdBy lastModifiedDate "
                   "lastModifiedBy nodeType status language geography title topics "
                   "modelSetId modelSetVersion metadata "
                   "globalState { domainOwner managedBy primaryOwner primaryOwners "
                   "businessReviewers restricted globalNodeStatus } "
                   "isEmbeddable references referencedBy referencedByPage")

    # Singular getContentNode, metadata-only selection:
    sing_q = ("query GetContentNode($nodeId: ID!) { getContentNode(nodeId: $nodeId, "
              "returnFieldVersions: true, returnTaxonomyValues: LABEL) { content { "
              + node_fields + " } } }")
    fc, ft = call("/infoarch/graphql", {"query": sing_q, "variables": {"nodeId": beta_ids[0]}})
    print("\n=== getContentNode (SINGULAR, metadata-only) === HTTP", fc, "->", ft[:160])

    # Plural getContentNodes with correct beta IDs, metadata-only selection:
    plural_q = ("query GetContentNodes($input: GetContentNodesInput!) { getContentNodes(input: $input) "
                "{ status statusCode errorMessage payload { nodes { " + node_fields
                + " } unprocessedNodes { nodeId } } } }")
    plural_vars = {"input": {"nodes": [{"nodeId": n} for n in beta_ids],
                             "returnFieldVersions": True, "returnTaxonomyValues": "LABEL"}}
    pc, pt = call("/infoarch/getContentNodes/graphql", {"query": plural_q, "variables": plural_vars})
    print("\n=== getContentNodes (PLURAL, correct beta IDs, metadata-only) === HTTP", pc)
    payload = (((json.loads(pt).get("data") or {}).get("getContentNodes") or {}).get("payload"))
    if not payload:
        print("RAW:", pt[:600])
    else:
        for n in payload.get("nodes", []):
            print(f"\n  {n.get('nodeId')} | {n.get('status')} | {n.get('title')}")
            print(f"    geography(LABEL): {n.get('geography')}")
            print(f"    topics(LABEL):    {n.get('topics')}")
            print(f"    metadata:         {n.get('metadata')}")
            print(f"    managedBy:        {(n.get('globalState') or {}).get('managedBy')}")
        if payload.get("unprocessedNodes"):
            print("  unprocessedNodes:", payload["unprocessedNodes"])

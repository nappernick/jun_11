# ALPHA OpenSearch — connection facts (skywalker-faq-alpha)

Discovered live from AWS account **948580600005** via the `alpha` profile on
2026-06-04. These are the real values for the `QUALITY_OPT_OPENSEARCH_*` config
placeholders (Req 16.6). **Recorded for later use — NOT yet applied to the live
config**, because switching the retrieval backend requires editing
`bakeoff/config.py` and restarting the dashboard, which would interrupt the
in-flight optimizer run. Apply at the next clean restart.

## Collection (OpenSearch **Serverless**, not a managed domain)

| field | value |
|---|---|
| account | `948580600005` (profile `alpha`) |
| region | `us-west-2` |
| service (SigV4) | `aoss`  ← OpenSearch **Serverless**, signing name is `aoss`, not `es` |
| collection name | `skywalker-faq-alpha` |
| collection id | `3z3yxvl1s09ylso0dgh` |
| collection ARN | `arn:aws:aoss:us-west-2:948580600005:collection/3z3yxvl1s09ylso0dgh` |
| type | `VECTORSEARCH` ("Skywalker FAQ evidence vector store") |
| status | `ACTIVE` |

## Endpoint

```
https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com
```

(FIPS variant, if ever needed: `https://3z3yxvl1s09ylso0dgh.us-west-2.aoss-fips.amazonaws.com`)

## Index

Two **identical** indices exist (56 docs each, ~1 MB). They are interchangeable;
default to `faq_evidence_a`.

| index | docs |
|---|---|
| `faq_evidence_a` | 56 |
| `faq_evidence_b` | 56 |

## Auth

SigV4 against service name **`aoss`** in `us-west-2`, using the `alpha` profile
credentials (role `IibsAdminAccess-DO-NOT-DELETE`, conduit provider). Refresh:

```
ada credentials update --account 948580600005 --provider conduit \
    --role IibsAdminAccess-DO-NOT-DELETE --profile alpha
```

## How this maps to config (`bakeoff/config.py`) — to apply at next restart

```python
QUALITY_OPT_RETRIEVAL_BACKEND = "opensearch"   # already the default
QUALITY_OPT_OPENSEARCH_ENDPOINT = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com"
QUALITY_OPT_OPENSEARCH_INDEX = "faq_evidence_a"
# AUTH: SigV4 for service "aoss" in us-west-2 via the alpha-profile credential chain.
# OpenSearchRetrievalBackend takes an injected client; build an opensearch-py client
# with an AWSV4SignerAuth(..., "aoss") signer (NOT "es") from the alpha session.
```

> NOTE on the backend code: `OpenSearchRetrievalBackend._ensure_client()` currently
> builds a plain `opensearchpy.OpenSearch(hosts=[endpoint], http_auth=auth)`. For a
> **Serverless** collection the client needs the `aoss`-service SigV4 signer and the
> `RequestsHttpConnection`/`Urllib3HttpConnection` connection class, e.g.:
>
> ```python
> from opensearchpy import OpenSearch, AWSV4SignerAuth, Urllib3HttpConnection
> import boto3
> creds = boto3.Session(profile_name="alpha", region_name="us-west-2").get_credentials()
> auth = AWSV4SignerAuth(creds, "us-west-2", "aoss")
> client = OpenSearch(hosts=[{"host": "3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com", "port": 443}],
>                     http_auth=auth, use_ssl=True, verify_certs=True,
>                     connection_class=Urllib3HttpConnection)
> ```
>
> The design already supports injecting a prebuilt client into
> `OpenSearchRetrievalBackend(client=...)` / `build_retrieval_backend(opensearch_client=...)`,
> so the cleanest wiring is to build the signed `aoss` client in `build_live_backend`
> and inject it (no edit to `_ensure_client` required). Confirmed reachable: a SigV4
> `GET /_cat/indices` returned 200 with both indices above.

## Verification command (read-only, confirmed working)

```
.venv/bin/python - <<'PY'
import boto3, urllib.request
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
sess = boto3.Session(profile_name="alpha", region_name="us-west-2")
creds = sess.get_credentials().get_frozen_credentials()
url = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com/_cat/indices?format=json"
req = AWSRequest(method="GET", url=url)
SigV4Auth(creds, "aoss", "us-west-2").add_auth(req)
r = urllib.request.urlopen(urllib.request.Request(url, headers=dict(req.headers)), timeout=30)
print(r.status, r.read().decode())
PY
```

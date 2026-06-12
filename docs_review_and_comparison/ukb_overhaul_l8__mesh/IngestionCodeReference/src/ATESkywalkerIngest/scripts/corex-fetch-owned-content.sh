#!/usr/bin/env bash
set -euo pipefail

PROFILE="${PROFILE:-alpha}"
REGION="${REGION:-us-west-2}"
SECRET_NAME="${SECRET_NAME:-ATESkywalkerIngest/alpha/corex}"
COREX_ROLE_ARN="${COREX_ROLE_ARN:-arn:aws:iam::975754358161:role/ATESkywalker-CORExApiRole-beta}"
COREX_HOST="${COREX_HOST:-corex-api.beta.corex.pxt.amazon.dev}"
DOMAIN_OWNER_ID="${DOMAIN_OWNER_ID:-amzn1.abacus.team.looo53floubmzytmswva}"
OUT_DIR="${OUT_DIR:-tmp/corex-fetch-owned-content}"

mkdir -p "$OUT_DIR/nodes"

secret_json="$(aws secretsmanager get-secret-value \
  --profile "$PROFILE" \
  --region "$REGION" \
  --secret-id "$SECRET_NAME" \
  --query SecretString \
  --output text)"

api_key="$(jq -r '.ApiKey' <<<"$secret_json")"
external_id="$(jq -r '.ExternalId' <<<"$secret_json")"

assumed="$(aws sts assume-role \
  --profile "$PROFILE" \
  --region "$REGION" \
  --role-arn "$COREX_ROLE_ARN" \
  --role-session-name "corex-fetch-owned-content-$(date +%s)" \
  --external-id "$external_id")"

export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY
export AWS_SESSION_TOKEN
AWS_ACCESS_KEY_ID="$(jq -r '.Credentials.AccessKeyId' <<<"$assumed")"
AWS_SECRET_ACCESS_KEY="$(jq -r '.Credentials.SecretAccessKey' <<<"$assumed")"
AWS_SESSION_TOKEN="$(jq -r '.Credentials.SessionToken' <<<"$assumed")"

search_body="$(jq -n --arg owner "$DOMAIN_OWNER_ID" '{
  query: "query searchContent($search: SearchInput!) { searchContent(search: $search) { status statusCode errorMessage payload { data { nodeId title status lastModifiedDate globalState { domainOwner managedBy } } pagination { limit pageNumber total } } } }",
  variables: {
    search: {
      query: "",
      filters: [{
        id: "domainOwner",
        value: [$owner],
        columnType: "STRING",
        group: "GLOBALSTATE"
      }],
      pagination: { limit: 50, pageNumber: 1 },
      sorting: null
    }
  }
}')"

curl --fail-with-body --silent --show-error \
  --aws-sigv4 "aws:amz:${REGION}:execute-api" \
  --user "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" \
  -H "x-amz-security-token: ${AWS_SESSION_TOKEN}" \
  -H "x-api-key: ${api_key}" \
  -H "content-type: application/json" \
  "https://${COREX_HOST}/search/graphql" \
  -d "$search_body" > "$OUT_DIR/search_domain_owner.json"

jq -r '.data.searchContent.payload.data[].nodeId' "$OUT_DIR/search_domain_owner.json" | while read -r node_id; do
  fetch_body="$(jq -n --arg nodeId "$node_id" '{
    query: "query GetContentNode($nodeId: ID!) { getContentNode(nodeId: $nodeId, returnFieldVersions: true, returnTaxonomyValues: LABEL) { content { nodeId version status language geography title topics modelSetId modelSetVersion metadata content globalState { domainOwner managedBy primaryOwner primaryOwners businessReviewers restricted globalNodeStatus } isEmbeddable references referencedBy referencedByPage } } }",
    variables: { nodeId: $nodeId }
  }')"

  curl --fail-with-body --silent --show-error \
    --aws-sigv4 "aws:amz:${REGION}:execute-api" \
    --user "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" \
    -H "x-amz-security-token: ${AWS_SESSION_TOKEN}" \
    -H "x-api-key: ${api_key}" \
    -H "content-type: application/json" \
    "https://${COREX_HOST}/infoarch/graphql" \
    -d "$fetch_body" > "$OUT_DIR/nodes/${node_id}.json"
done

python3 - "$OUT_DIR" <<'PY'
import json
import pathlib
import sys

out_dir = pathlib.Path(sys.argv[1])

def collect_text(node, parts):
    if isinstance(node, dict):
        value = node.get("text")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        for child in node.values():
            collect_text(child, parts)
    elif isinstance(node, list):
        for child in node:
            collect_text(child, parts)

def extract_text(content):
    parts = []
    if not isinstance(content, dict):
        return ""
    for field in content.values():
        if isinstance(field, dict) and field.get("type") == "RTE_V2":
            inner = field.get("content")
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except json.JSONDecodeError:
                    parts.append(inner)
                    continue
            collect_text(inner, parts)
        else:
            collect_text(field, parts)
    return " ".join(" ".join(parts).split())

summary = []
for path in sorted((out_dir / "nodes").glob("*.json")):
    raw = json.loads(path.read_text())
    content = raw["data"]["getContentNode"]["content"]
    metadata = json.loads(content.get("metadata") or "{}")
    body = json.loads(content.get("content") or "{}")
    global_state = content.get("globalState") or {}
    text = extract_text(body)
    summary.append({
        "nodeId": content.get("nodeId"),
        "title": content.get("title"),
        "status": content.get("status"),
        "version": content.get("version"),
        "managedBy": global_state.get("managedBy"),
        "domainOwner": global_state.get("domainOwner"),
        "geography": content.get("geography") or [],
        "topics": content.get("topics") or [],
        "metadataKeys": sorted(metadata.keys()),
        "contentKeys": sorted(body.keys()),
        "textChars": len(text),
        "textPreview": text[:400],
    })

(out_dir / "fragment_text_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps({"nodes": len(summary), "summary": str(out_dir / "fragment_text_summary.json")}, indent=2))
PY

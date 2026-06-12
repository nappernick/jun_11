# API Contract 09. UKB (Unified Knowledge Base) MCP Client

Covers how Skywalker calls the UKB general-retrieval arm. Pairs with Section 06 (UKB General Retrieval Integration) and Section 04 (online decision flow).

## What the architecture has already fixed

- Section 06 §3: UKB is a black box behind an MCP surface. Skywalker does not try to influence UKB's internal retrieval, ranking, or indexing. It accepts UKB as-is and normalizes the output into the common candidate envelope.
- Section 06 §2: the UKB arm receives the query and the resolved scope through its own exposed contract; the orchestration layer does not presume more.
- Section 06 §7: UKB call failure is not automatically system failure. The single-arm fallback route survives with the FAQ arm if UKB fails.
- Section 01 §3 decision nine: Skywalker's retrieval pipeline is budgeted at 200–400 ms p95. UKB latency falls inside that budget.

## What UKB gives us (baseline facts)

From the [UKB Onboarding](UKB%20Onboarding.pdf) guide: UKB exposes the **AET Content Knowledge Base Service (KBS)** — an MCP-compliant retrieval surface over Amazon's unified knowledge bases.

### Endpoints (v1 API)

All endpoints follow the pattern `https://api.{region}.{stage}.knowledge.pxt.amazon.dev/iam/v1/mcp`.

| Stage | Endpoint |
|---|---|
| Alpha | `https://api.us-west-2.alpha.knowledge.pxt.amazon.dev/iam/v1/mcp` |
| Beta | `https://api.us-west-2.beta.knowledge.pxt.amazon.dev/iam/v1/mcp` |
| Gamma | `https://api.us-west-2.gamma.knowledge.pxt.amazon.dev/iam/v1/mcp` |
| PreProd | `https://api.us-west-2.preprod.knowledge.pxt.amazon.dev/iam/v1/mcp` |
| Prod | `https://api.us-west-2.prod.knowledge.pxt.amazon.dev/iam/v1/mcp` |

`us-west-2` is the only region for v1. Gamma does not carry production content; PreProd is the pre-production staging environment with a production content mirror.

### Authentication

AWS IAM with SigV4. The pattern is:

1. The UKB team issues a cross-account role ARN named `kbs-mcp-role_{stage}_{client_id}`. We will be assigned a `client_id` during onboarding.
2. Our service's IAM policy allows `sts:AssumeRole` on that role ARN.
3. Skywalker assumes the role to get temporary credentials, then signs every MCP call with SigV4 using `service: "execute-api"` and `region: "us-west-2"`.

Transport is Streamable HTTP per the MCP spec.

### Tool: `retrieve`

One tool. Name: `retrieve`. Invoked via standard MCP `tools/call`:

```json
{
  "method": "tools/call",
  "params": {
    "name": "retrieve",
    "arguments": {
      "query": "<search query>",
      "maxResults": 10,
      "targetUser": {
        "LOGIN": "<alias>",
        "PERSON_ID": "<person-id-uuid>"
      },
      "additionalFilters": {
        "exactFilters": { ... },
        "partialFilters": { ... },
        "aclFilters": { ... }
      }
    },
    "_meta": {
      "progressToken": 0
    }
  }
}
```

Headers required on every request:

- `x-acting-user: {"LOGIN":"<alias-for-eligibility>"}` — required.
- `x-target-user: {"LOGIN":"<alias-for-applicability>"}` — optional; can be omitted if `targetUser` is in the body instead. When both are provided, their `LOGIN` must match or the request errors.
- `x-atoz-person-id: <Person ID>` — required for v1.
- `x-atoz-token-audience-type: <AtoZ Persona>` — required for v1.
- `x-amzn-transitive-authentication-token: <TA Token>` — required.

### Personalization

If `targetUser` is `null`, UKB skips personalization filters (`countryCode`, `stateCode`, `buildingCode`, `badgeColor`, `jobLevel`, `payRateType`). If `targetUser` is omitted, UKB uses the `x-acting-user` header as the target.

For Skywalker this matters: we already resolve scope through PAPI (Section 02). We can either (a) pass the resolved employee's `targetUser` so UKB applies its own personalization filter, or (b) pass `targetUser: null` and filter our own way. **The current architecture already treats UKB as a black box and does not duplicate its scope filter**, so option (a) is correct — pass the employee's LOGIN + PERSON_ID, let UKB apply its native personalization, accept the candidates as-is, and normalize into the common envelope.

### Response

Standard MCP tool result, with content blocks of `type: "resource"`:

```json
{
  "_meta": { "clientId": "<client_id>", "requestId": "<uuid>" },
  "content": [
    {
      "type": "resource",
      "resource": {
        "uri": "...",
        "mimeType": "text/plain",
        "_meta": { "sourceUrl": "...", ... },
        "text": "...",
        "name": "...",
        "title": "...",
        "annotations": {
          "audience": ["user"],
          "priority": ...,
          "lastModified": "..."
        }
      }
    }
  ]
}
```

V1 responses do not include `filters` or `nextToken` in `_meta` (unlike v0). Skywalker uses `text`, `title`, `_meta.sourceUrl`, and `annotations.lastModified` when normalizing UKB candidates.

### Filter model

UKB supports three filter categories (`exactFilters`, `partialFilters`, `aclFilters`) with depth limits:

- **exactFilters**: up to 2 layers of nesting. Primitive ops: `equals`, `notEquals`, `greaterThan`, `greaterThanOrEquals`, `lessThan`, `lessThanOrEquals`, `in`, `notIn`, `listContains`, `startsWith`, `stringContains`. L1 wrappers: `andAll`, `orAll` (min 2 conditions). L2: `andAll` of L1 only.
- **partialFilters**: up to 1 layer of nesting.
- **aclFilters**: same depth limits as exactFilters.

**Skywalker's launch posture is `additionalFilters: {}`** (i.e., no additional filters beyond UKB's native personalization from `targetUser`). Section 06 §3 already says Skywalker does not try to shape UKB's internals. If later calibration shows UKB returns too-broad results for our domain (travel, events, expense), we can add `partialFilters` to narrow, but that's a Section 06 calibration surface, not a launch decision.

Content from external sources has been rephrased for compliance with licensing restrictions.

## The call Skywalker makes, concretely

```java
// Pseudocode, language-agnostic
UkbMcpClient client = UkbMcpClient.getInstance();
CallToolRequest req = new CallToolRequest()
    .name("retrieve")
    .arguments(Map.of(
        "query", scopedRequest.getQueryText(),
        "maxResults", UKB_ARM_CANDIDATE_BUDGET,  // Section 04 calibration, default ~10
        "targetUser", Map.of(
            "LOGIN", scopedRequest.getAlias(),
            "PERSON_ID", scopedRequest.getPersonId()
        )
    ))
    .headers(Map.of(
        "x-acting-user", json("{\"LOGIN\":\"" + scopedRequest.getAlias() + "\"}"),
        "x-atoz-person-id", scopedRequest.getPersonId(),
        "x-atoz-token-audience-type", scopedRequest.getPersona(),
        "x-amzn-transitive-authentication-token", scopedRequest.getTaToken()
    ));

CallToolResult result = client.callTool(req, ukbTimeoutMs);
```

## Candidate normalization

Per Section 04 §2, UKB's `content[]` resources are converted into the common candidate envelope. The mapping:

| Common envelope field | UKB source |
|---|---|
| `candidate_id` | generated runtime UUID |
| `source_arm` | `"UKB"` |
| `source_id` | `resource.uri` |
| `title` | `resource.title` (fallback: `resource.name`) |
| `text` | `resource.text` |
| `source_url` | `resource._meta.sourceUrl` |
| `policy_links` | empty (UKB does not surface a separate policy-link metadata field at the content level; any policy URLs are inside the text) |
| `arm_local_rank` | positional index in the `content[]` array |
| `rerank_score` | populated later by Section 07 |

Section 04 §3 decision four: native scores are not cross-arm comparable. We do **not** try to construct a fake arm-local score for UKB candidates. The positional index in the UKB response is sufficient as arm-local rank.

## Failure handling

Per Section 06 §7:

- **UKB call failure** (network, 5xx, timeout): report arm failure. Section 04's one-arm survival rule takes over. If the FAQ arm succeeded, the request proceeds as a single-arm fallback route (Section 04 §7, §3 decision eight).
- **UKB returns empty** (`content: []`): retrieval miss, not service fault. Pass an empty UKB candidate set to the reranker; Section 07 decides whether the surviving pool is strong enough.
- **UKB returns malformed items** (item in `content[]` missing required fields): Section 06 §7 failure rule three — discard the malformed items, preserve the good ones, record that the arm returned partial results at item level.
- **403 Forbidden** on SigV4: IAM misconfiguration. Alert and fail the request; do not retry.
- **504 Gateway Timeout**: retry once with exponential backoff (UKB's docs recommend this pattern); if the retry also fails, treat as arm failure.
- **429 Too Many Requests**: respect UKB's rate limit; abort the arm for this request rather than retrying inline.

## Configuration

- **Client ID:** assigned by UKB team during onboarding. One per stage.
- **Cross-account role ARN:** `arn:aws:iam::{KBS_ACCOUNT_ID}:role/kbs-mcp-role_{stage}_skywalker`.
- **Region:** `us-west-2`.
- **Endpoint:** stage-specific from the table above.
- **Timeout:** Section 04 §8 calibration surface four names the UKB timeout posture as an open item. Launch default: 300 ms to fit inside the 400 ms p95 retrieval budget. A slower response aborts the arm per Section 04 §7 failure rule two.
- **Candidate budget per request (`maxResults`):** Section 04 §8 calibration surface three names this as open. Launch default: 10.

## Onboarding checklist

From the UKB guide:

1. Submit the UKB intake form with: client service name (Skywalker), business justification, expected usage patterns, AWS account IDs per stage, IAM role names, technical contacts, data classification, security requirements.
2. UKB team issues the cross-account role ARN per stage.
3. Attach the `sts:AssumeRole` policy to our service role.
4. Implement the client using the TypeScript examples as reference (we will re-implement in Java to match Section 08's JVM posture; the patterns are the same).
5. Test connectivity stage-by-stage.
6. Implement retry/backoff and error handling per the failure rules above.

Support path: SIM Tickets, CTI `PXT/AETContentOptimization/Support`, resolver group `AET ML Engineering Seattle`. Critical issues use Sev-2.

## Sections of the architecture this binds

- Section 06 §2, §3 (UKB as black-box general arm behind MCP, no duplicated scope logic).
- Section 06 §7 (failure rules one through four).
- Section 04 §2, §3 decision four (common candidate envelope, no cross-arm score comparison).
- Section 04 §3 decision eight (route metadata records arm-level outcomes).

## Outstanding unknowns

- Final `client_id` from UKB onboarding.
- Final UKB timeout value (launch default 300 ms; revisit against measured UKB latency).
- Final `maxResults` per-arm budget (launch default 10; revisit against reranker pool diversity in Section 04 calibration surface three).
- Whether Skywalker ever wants to populate `additionalFilters` (launch posture: no).
- Whether we pass the resolved employee as `targetUser` or use `targetUser: null`. Current decision: pass the resolved employee so UKB applies its personalization. Revisit if UKB's personalization conflicts with the Section 02 scoping triple.

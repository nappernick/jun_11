# API Contract 01. MCP Server Protocol (Skywalker-as-server)

Covers the protocol Skywalker implements so MCP clients (QuickSuite via the wrapper, the UAT inline-agent orchestrator, the Slack-side application, and any future caller) can list and call Skywalker's retrieval tools. Pairs with Section 02 (entry contract), Section 05 (UAT slice), Section 08 (Slack), and Section 09 (QuickSuite consumption model).

## What the architecture has already fixed

- Skywalker is an MCP server. All external retrieval access goes through this surface; no client is allowed around it. (Sections 01, 02)
- Three supported entry modes: alias, employee-ID, explicit-scope. These should map to distinct MCP tools rather than one overloaded tool with a tagged payload. (Section 02 §3, §4)
- Output is a structured backend result, never final conversational prose. Answerable evidence package or structured abstain package. (Sections 01, 07, 09)
- Route metadata (FAQ-only / dual-arm / single-arm fallback / reranker-failure fallback) is part of the result contract, not internal trivia. (Sections 07, 09)

## What the protocol itself gives us (baseline facts)

MCP is built on JSON-RPC 2.0. All messages use `{"jsonrpc":"2.0", ...}` with requests carrying a non-null `id` and methods like `tools/list` and `tools/call`. Multiple protocol revisions exist (`2024-11-05`, `2025-03-26`, `2025-06-18`); **Skywalker targets `2024-11-05`** at launch because that is the revision the QuickSuite MCP-connector implementation supports today (per API_08) and Amazon MCP Gateway carries through transparently. We can move to a newer revision when QuickSuite does.

Tool discovery via `tools/list` returns entries shaped like:

```json
{
  "name": "string",
  "title": "string (optional)",
  "description": "string",
  "inputSchema": { "type": "object", "properties": {...}, "required": [...] },
  "outputSchema": { "type": "object", ... }
}
```

Tool invocation via `tools/call` takes `{"name": "...", "arguments": {...}}` and returns a result with a `content` array (text/image/resource blocks) plus, when `outputSchema` is declared, a `structuredContent` object matching that schema. Errors split two ways: protocol-level JSON-RPC errors (unknown tool, bad args) and tool-execution errors carried inside the result with `isError: true`.

Auth: the MCP spec recommends its authorization framework for HTTP transports and says STDIO should take credentials from the environment. Clients and servers MAY negotiate custom schemes.

Content from external sources has been rephrased for compliance with licensing restrictions.

## What we still need to decide and write into Section 02

1. **Transport.** All three production client paths reach Skywalker's MCP server through **Amazon MCP Gateway**. Slack and UAT use the gateway's CloudAuth-inbound route at `/ca/mcp/{registry}/{server}` with CloudAuth OBO + TransitiveAuth (per API_14); QuickSuite uses the Federate-OAuth-inbound route at `/federate/mcp/{registry}/{server}` with identity in MCP tool arguments (per API_08). STDIO transport is not used because all three callers are remote.
2. **Tool names.** Current placeholders needed:
   - `skywalker.search.by_alias`
   - `skywalker.search.by_employee_id`
   - `skywalker.search.by_explicit_scope`

   Pick final names that match any Amazon-internal MCP naming conventions.
3. **`inputSchema` for each tool.** Concretely:

   ```json
   // skywalker.search.by_alias
   {
     "type": "object",
     "properties": {
       "query_text": {"type": "string", "minLength": 1},
       "alias": {"type": "string"},
       "correlation_id": {"type": "string", "format": "uuid"}
     },
     "required": ["query_text", "alias"]
   }
   ```

   ```json
   // skywalker.search.by_employee_id
   {
     "type": "object",
     "properties": {
       "query_text": {"type": "string", "minLength": 1},
       "employee_id": {"type": "string"},
       "correlation_id": {"type": "string", "format": "uuid"}
     },
     "required": ["query_text", "employee_id"]
   }
   ```

   ```json
   // skywalker.search.by_explicit_scope
   {
     "type": "object",
     "properties": {
       "query_text": {"type": "string", "minLength": 1},
       "employee_id": {"type": "string"},
       "country": {"type": "string"},
       "level": {"type": "string"},
       "role": {"type": "string", "enum": ["MANAGER", "INDIVIDUAL_CONTRIBUTOR"]},
       "correlation_id": {"type": "string", "format": "uuid"}
     },
     "required": ["query_text", "employee_id", "country", "level", "role"]
   }
   ```

   Open questions: final country encoding (ISO 3166-1 alpha-2? alpha-3? internal code?), final level encoding, final role enum values.

4. **`outputSchema` (single shape used by all three tools).** The same backend result package regardless of entry mode:

   ```json
   {
     "type": "object",
     "properties": {
       "result_kind": {"type": "string", "enum": ["ANSWERABLE", "ABSTAIN"]},
       "route": {
         "type": "object",
         "properties": {
           "path": {"type": "string", "enum": ["FAQ_ONLY", "DUAL_ARM", "SINGLE_ARM_FALLBACK"]},
           "surviving_arms": {"type": "array", "items": {"type": "string", "enum": ["FAQ", "UKB"]}},
           "reranker_state": {"type": "string", "enum": ["NORMAL", "RERANKER_FAILURE_FALLBACK"]},
           "faq_counts": {"type": "integer"},
           "ukb_counts": {"type": "integer"}
         },
         "required": ["path", "surviving_arms", "reranker_state"]
       },
       "scope_snapshot": {
         "type": "object",
         "properties": {
           "country": {"type": "string"},
           "level": {"type": "string"},
           "role": {"type": "string"}
         },
         "required": ["country", "level", "role"]
       },
       "evidence": {
         "type": "array",
         "items": { "$ref": "#/definitions/Candidate" }
       },
       "abstain_reason": {
         "type": "string",
         "enum": [
           "NO_USABLE_EVIDENCE",
           "EVIDENCE_TOO_WEAK_AFTER_RERANK"
         ]
       },
       "correlation_id": {"type": "string"}
     },
     "required": ["result_kind", "route", "scope_snapshot", "correlation_id"],
     "definitions": {
       "Candidate": {
         "type": "object",
         "properties": {
           "candidate_id": {"type": "string"},
           "source_arm": {"type": "string", "enum": ["FAQ", "UKB"]},
           "source_id": {"type": "string"},
           "title": {"type": "string"},
           "text": {"type": "string"},
           "source_url": {"type": "string", "format": "uri"},
           "policy_links": {"type": "array", "items": {"type": "string", "format": "uri"}},
           "arm_local_rank": {"type": "integer"},
           "rerank_score": {"type": "number"}
         },
         "required": ["candidate_id", "source_arm", "text"]
       }
     }
   }
   ```

   The `evidence` array is populated when `result_kind = ANSWERABLE`. When `result_kind = ABSTAIN`, `abstain_reason` is populated; `evidence` may still be present for auditability.

5. **Error model.** Define which failures are JSON-RPC protocol errors vs. tool-execution errors with `isError: true`. Tentative split:
   - Protocol errors (`code`, `message`): unknown tool, malformed args, auth failure at the server level.
   - Tool-execution errors (`isError: true`): PAPI unresolved identity, malformed scope after resolution, internal arm double-failure with no fallback.
   - Backend abstention is **not** an error. It is a successful result with `result_kind = ABSTAIN`.

## Consumption by QuickSuite

The three-tool, identity-in-body shape documented here is the Skywalker MCP contract every production client consumes through **Amazon MCP Gateway** as the transport. **Slack (Section 08) and the UAT inline-agent orchestrator (Section 05) call Skywalker on the CloudAuth-inbound route** at `https://api.mcp.asbx.aws.dev/ca/mcp/{registry}/{server}` with each orchestrator registered as a CloudAuth-modeled AAA application; **CloudAuth OBO** carries the orchestrator's service identity to Skywalker, and **TransitiveAuth** carries the human end-user's identity as a separate TA token alongside CloudAuth. Skywalker reads the human alias from the validated TA claims server-side on these paths rather than from `arguments.alias`. **QuickSuite (Section 09) calls Skywalker on the Federate-OAuth-inbound route** at `https://api.mcp.asbx.aws.dev/federate/mcp/{registry}/{server}` with a Federate Prod Service Profile; the Federate-inbound + CloudAuth-outbound combination has no published delegated-identity pattern, so QuickSuite continues to carry identity in MCP tool arguments. There is no wrapper Lambda between any client and Skywalker, and there is no task-oriented translation tool — every client reads the same `tools/list` and chooses among the three tools directly. The MCP tool's `arguments.alias` and `arguments.country/level/role` shapes stay in the input schemas for QuickSuite and any future non-TA caller; on the Slack and UAT paths they are preserved as a contract shape but are not the canonical identity channel. See [API_08](API_08_QuickSuite_MCP_Gateway_Integration.md) for the QuickSuite-specific MCP Gateway integration and [API_14](API_14_CloudAuth_OBO_and_TransitiveAuth_Integration.md) for the CloudAuth OBO + TransitiveAuth integration on the Slack and UAT paths.

## Sections of the architecture this binds

- Section 02 §2, §3, §9 (entry contract, method naming, schema finalization).
- Section 05 §2, §3 (UAT slice consumes `skywalker.search.by_explicit_scope` directly through Amazon MCP Gateway, with scope sourced from custom Federate claims in the Midway token).
- Section 07 §2, §7 (answerable vs. abstain contract, abstain reason taxonomy).
- Section 08 §3 (Slack application consumes `skywalker.search.by_alias` and `skywalker.search.by_explicit_scope` directly).
- Section 09 §2 (what QuickSuite sends, what QuickSuite receives, all via the API_08 wrapper).
- Section 10 C-09 (abstain and fallback-route reason vocabulary).

## Outstanding unknowns to resolve before coding

- Final final value encodings for `country` (ISO alpha-2 vs alpha-3 vs internal code), `level` (canonical Amazon level string format), and the `role` enum naming — `MANAGER` and `INDIVIDUAL_CONTRIBUTOR` are placeholders.
- Final tool names aligned with internal MCP naming conventions.
- Final scope-field encodings (country, level, role).
- Final abstain-reason enum — the four values above cover the §06 taxonomy, but client integration review may want finer-grained reasons.
- Authentication at the MCP boundary beyond "callers reaching this surface are already authenticated by the surrounding environment" per Section 02 §3.

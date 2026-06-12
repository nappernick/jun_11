# API Contract 08. QuickSuite MCP Gateway Integration

Covers how QuickSuite consumes Skywalker's MCP server through **Amazon MCP Gateway** with Federate-OAuth inbound auth, on the paved path documented in BuilderHub's [Integration with QuickSuite guide](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/howto-quicksuite-client/). Pairs with Section 09 (QuickSuite Integration and MCP Consumption Model) and API_01 (core MCP server protocol).

This grounding doc supersedes both the v1 AZA Plug-in Wrapper grounding doc and an earlier draft of this file built around Bedrock AgentCore Gateway with a wrapper Lambda. **Neither model is correct.** The supported QuickSuite-to-internal-MCP path is Amazon MCP Gateway, not AgentCore Gateway, and there is no wrapper-Lambda layer between QuickSuite and Skywalker's MCP server. QuickSuite sees the MCP server's actual `tools/list` directly through the gateway.

The architectural shape is therefore simpler than the wrapper-based draft assumed:

- Skywalker's MCP server registers with Amazon MCP Gateway and exposes its tools through the published Federate-inbound route.
- QuickSuite registers an MCP connector pointing at the gateway's QuickSuite endpoint for that server.
- Inbound Federate JWT is validated by MCP Gateway. The validated user identity does **not** propagate to the Skywalker backend through the MCP request itself **on this auth combination** — MCP Gateway's published delegated-identity patterns (TransitiveAuth, FAS, Midway A5, UnifiedAuth) are all flagged "in progress" on the BuilderHub concepts page as of this document's writing for the Federate-OAuth-inbound + CloudAuth-outbound combination. (The CloudAuth-inbound + CloudAuth-outbound combination Slack and UAT use does support OBO + TA natively per [API_14](API_14_CloudAuth_OBO_and_TransitiveAuth_Integration.md); the "no propagation" statement is specific to QuickSuite's auth combination, not a general property of MCP Gateway.)
- **Identity for retrieval scope on the QuickSuite path is carried by the integration, not by MCP Gateway.** The QuickSuite integration is responsible for getting the user identity material it needs through its own integration-specific channel and supplying it as MCP tool arguments. Slack and UAT (per API_14) carry identity through TransitiveAuth instead; QuickSuite's identity-carriage mechanism is a launch-time integration question (see §"Outstanding unknowns").

Sources cited in this document are internal Amazon wikis and BuilderHub. Content from external sources has been rephrased for compliance with internal-source-only restrictions.

## Why MCP Gateway is the right transport

Three reasons, all sourced from BuilderHub's [MCP Gateway User Guide](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/) and the published [Integration with QuickSuite](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/howto-quicksuite-client/) page.

First, MCP Gateway is the **internal paved path** for hosting and consuming MCP servers across Amazon. It handles connectivity across CORP/PROD/Native AWS network fabrics, adapts between internal auth systems (Midway, AWSAuth, CloudAuth) and the MCP transport, applies Bindle-based discovery and invocation authorization, and provides centralized audit logging. Both QuickSuite and the UAT inline-agent orchestrator (Section 05) are documented as supported clients of this gateway, which means Skywalker hosting its MCP surface on MCP Gateway puts the entire production architecture on one transport with one auth-onboarding shape rather than two.

Second, the **published QuickSuite client integration** for MCP Gateway is the only QuickSuite-to-MCP-server path that does not require a custom wrapper-Lambda layer. The BuilderHub QuickSuite client guide describes a one-step setup: register the MCP server with MCP Gateway, create a Federate Prod profile using the "AWS QuickSuite Action Connectors" pre-approved use case, register the connector in QuickSuite with Federate credentials. There is no AgentCore Gateway, no REQUEST interceptor, no wrapper Lambda, and no AppConfig CR.

Third, the **failure surface contracts** with the rest of the architecture. MCP Gateway is also what the UAT inline-agent orchestrator and the Slack inline-agent orchestrator use, both on the CloudAuth-inbound route at `/ca/mcp/{registry}/{server}` with CloudAuth OBO + TransitiveAuth (per API_14). Operating all three production paths on MCP Gateway means the Skywalker MCP server has one auth-onboarding story and one set of Bindle permissions to manage rather than three. Section 09 §3 decision four (the hosting model) is locked to MCP Gateway by this contract.

## What MCP Gateway provides on the QuickSuite path

Per BuilderHub [Integration with QuickSuite](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/howto-quicksuite-client/) and the [MCP Gateway concepts](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/concepts/) page:

- **Endpoint shape:** `https://api.mcp.asbx.aws.dev/federate/mcp/{registry}/{server}` — note the `/federate/` path component, which is distinct from the `/mcp/` path used by the AWSAuth/SigV4 route. QuickSuite registers this URL as the MCP server endpoint in its console.
- **Inbound auth:** Federate OAuth (Authorization Code with PKCE), against a Federate Prod Service Profile.
- **Federate Service Profile setup:** Use the "AWS QuickSuite Action Connectors" pre-approved use case when creating the profile. Pre-approved use cases do not require a security ticket. **Prod profiles only.** Integ profiles "expire after one month" per the BuilderHub guide (and additionally, QuickSuite auto-deletes Integ-backed connectors every 24 hours per multiple internal wikis including [POC: QuickSuite with AgentCore Gateway Integration (Federate)](https://w.amazon.com/bin/view/AWS/Teams/TAA_EiR/AI_Framework/Proofs_of_Concept/QuickSuite_AgentCore_Integration/) and [Connect Quick Suite to Coral Service MCP using AgentCore Gateway and Federate](https://w.amazon.com/bin/view/FBA/AI/MCP/QuickSuiteToAgentCoreGatewayAndCoralMCP/) — the same constraint applies on the MCP Gateway path).
- **Federate URLs (Prod), per BuilderHub:**
  - Authorize URL: `https://idp.federate.amazon.com/api/oauth2/v1/authorize`
  - Token URL: `https://idp.federate.amazon.com/api/oauth2/v2/token`
- **Token validation:** MCP Gateway validates the bearer token directly. There is no AgentCore Gateway in the path. No "Allowed Audiences vs Allowed Clients" gotcha because there is no AgentCore Gateway resource to misconfigure.
- **Bindle authorization:** MCP Gateway creates a unique Bindle resource for each MCP server (`MCPGateway::{server-id}`). Bindle permissions control discovery and invocation. For QuickSuite, the Federate Service Profile's Amazon Teams group must be granted `canInvoke` on Skywalker's MCP server resource bindle.
- **Outbound auth into the backend:** MCP Gateway converts the Federate-inbound auth into CloudAuth on the outbound side to the Skywalker MCP server. The Skywalker MCP server is registered as a CloudAuth-protected service. (The FBA wiki [Connect Quick Suite to Coral Service MCP](https://w.amazon.com/bin/view/FBA/AI/MCP/QuickSuiteToAgentCoreGatewayAndCoralMCP/) describes the equivalent CloudAuth-outbound pattern in the AgentCore Gateway-fronted variant; the MCP Gateway-fronted variant uses the same downstream CloudAuth shape.)
- **Throttling:** 50 TPS per client, applied at the gateway (per BuilderHub [MCP Gateway User Guide](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/)).
- **Latency overhead:** ~50–150 ms for auth handling and protocol translation.
- **Network fabrics:** Reachable from CORP and PROD, in PDX/IAD/DUB/NRT.

## What MCP Gateway does *not* provide (and what that means)

Per the [MCP Gateway concepts](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/concepts/) page, originating-requester identity propagation through the gateway is currently incomplete on the Federate-inbound + CloudAuth-outbound combination. MCP Gateway's three published delegated-identity patterns (TransitiveAuth, Forward Access Sessions, UnifiedAuth/Midway A5) are all flagged "in progress" and are oriented around CloudAuth-or-AWSAuth-inbound, not Federate-OAuth-inbound. There is no published mechanism, as of this document's writing, that lets the Skywalker MCP server read the Federate-authenticated end-user's alias out of the request when MCP Gateway is fronting Federate inbound.

This has one architectural consequence. **Identity that retrieval needs (alias for PAPI lookup, or `country`/`level`/`role` for the explicit-scope path) cannot be assumed to arrive through MCP Gateway on the QuickSuite path.** It has to be carried by the integration in some other way — through the integration-specific identity-injection mechanism (see §"Outstanding unknowns") or as MCP tool arguments the QuickSuite chat agent populates from its own session data. Section 09 §3 makes this an explicit, named architectural decision rather than an assumption.

The same shape applies on the Slack and UAT paths in earlier drafts of those sections, but in those sections the auth combination changes to CloudAuth-inbound + CloudAuth-outbound (see API_14), where MCP Gateway supports OBO + TransitiveAuth natively. Slack carries the alias as part of the integration's own identity surfacing, but the alias rides through TransitiveAuth on the Slack and UAT paths rather than through `arguments.alias`. The QuickSuite path stays on the argument-supplied identity model because the Federate-inbound + CloudAuth-outbound combination has no published delegated-identity pattern.

## What the Skywalker MCP server exposes

Skywalker's MCP server registers with MCP Gateway under one server identifier (e.g., `skywalker-policy-mcp`). It exposes the three tools fixed in Section 02 and pinned in API_01:

- `skywalker.search.by_alias` — Slack default. `{ query_text, alias, correlation_id? }`.
- `skywalker.search.by_employee_id` — for future callers that hold the employee identifier rather than the alias. Not used by Slack, UAT, or QuickSuite at launch.
- `skywalker.search.by_explicit_scope` — UAT default. `{ query_text, employee_id, country, level, role, correlation_id? }`.

There is **no** fourth task-oriented tool. Earlier drafts of this contract (built around an AgentCore Gateway wrapper Lambda) imagined a `search_travel_events_expense_policy` tool registered externally and translated into one of the three internal tools by a wrapper. That layer does not exist on the MCP Gateway path. QuickSuite's chat-agent intent routing reads the actual `tools/list` of the registered MCP server and decides between `skywalker.search.by_alias` and `skywalker.search.by_explicit_scope` directly based on what identity material the QuickSuite integration is configured to supply.

Per the BuilderHub QuickSuite client guide:

> During this step, QuickSuite sends a bearer token to MCP Gateway. If MCP Gateway validates the token, you can see all the tools available under that MCP server, which QuickSuite agents can then access.

This means the **tool descriptions** Skywalker's MCP server publishes through `tools/list` are what QuickSuite's chat-agent intent routing reads directly. The descriptions therefore have to be optimized for that routing surface — they are no longer hidden behind a wrapper-side connector description. Section 09 §3 calibration surface three tracks the descriptions as a calibration-active surface; production routing accuracy is the evidence that moves them.

## What QuickSuite registers in its console

Per BuilderHub [Integration with QuickSuite](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/howto-quicksuite-client/), the QuickSuite-account administrator registers the connector through the QuickSuite console (Integrations → Actions → MCP, the same workflow described in [Quicksuite MCP/Action Connector Integration Guide](https://w.amazon.com/bin/view/PDI/TechnicalDocumentation/QuicksuiteMCPActionConnectorIntegrationGuide/)) with these fields:

- **MCP Server URL:** `https://api.mcp.asbx.aws.dev/federate/mcp/{registry-id}/{skywalker-server-id}`. The exact registry ID and server ID surface during the Skywalker MCP server's MCP Gateway onboarding.
- **Client ID:** the Federate Service Profile Client ID.
- **Client Secret:** the Federate Service Profile Client Secret.
- **Token URL (Prod):** `https://idp.federate.amazon.com/api/oauth2/v2/token`.
- **Authorization URL (Prod):** `https://idp.federate.amazon.com/api/oauth2/v1/authorize`.

QuickSuite's UI runs the OAuth handshake (sign-in → redirect → token). On success, QuickSuite calls `tools/list` against the gateway and displays the discovered tools. There is no separate AppConfig CR equivalent; v1 AZA's `McpSkywalkerToolConfig.json` AppConfig file does not apply on this path.

The connector is **registration-restricted by default**. The chat-agent author must explicitly share access by username, alias, or Amazon Teams group. Section 09 §3 fixes Amazon-Teams-group sharing as the launch posture so additions and removals can be managed centrally.

## Request body shape

A QuickSuite invocation arrives at the Skywalker MCP server as a normal MCP `tools/call` request with no AgentCore-style `__authContext` injection. The exact shape depends on how the QuickSuite integration is configured to supply identity (see §"Outstanding unknowns") — Skywalker's MCP server validates each `tools/call` against the input schema fixed in Section 02 and API_01 regardless of how the arguments arrived.

The two QuickSuite-shaped invocation patterns the architecture has to support are:

```json
// Pattern A: chat-agent supplies alias as a tool argument
// (likely launch shape if QuickSuite chat-agent author can inject alias from Midway session)
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "tools/call",
  "params": {
    "name": "skywalker.search.by_alias",
    "arguments": {
      "query_text": "can I expense a dinner for interview candidates?",
      "alias": "abcde"
    }
  }
}
```

```json
// Pattern B: chat-agent supplies full scope tuple
// (alternate shape if integration carries scope rather than alias)
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "tools/call",
  "params": {
    "name": "skywalker.search.by_explicit_scope",
    "arguments": {
      "query_text": "...",
      "employee_id": "123456789",
      "country": "US",
      "level": "5",
      "role": "INDIVIDUAL_CONTRIBUTOR"
    }
  }
}
```

Either pattern is contract-valid. Skywalker's MCP server does not care which one QuickSuite supplies — it cares only that the arguments validate against the input schema and that the canonical scoped request can be constructed.

## Response envelope

Per Section 02 §2 and API_01: `result_kind`, `route`, `scope_snapshot`, `evidence[]`, `abstain_reason`, `correlation_id`. MCP Gateway returns the response unchanged to QuickSuite. There is no wrapper layer reshaping the envelope into a different `content[]` convention — QuickSuite's chat-agent runtime reads the structured response directly through MCP Gateway.

This raises an architectural question that the wrapper-based draft of this document partially solved: the QuickSuite "Sources" UI groups citations under the connector name rather than rendering per-document URLs from the structured payload. Per the [Amazon Quick Suite — FAQs](https://w.amazon.com/bin/view/AmazonQuickSuite/FAQ/) entry confirming this:

> This is a known issue with MCP integration source attribution. Workaround: Ask the agent to explicitly list source document names and URLs in its response text.

In the wrapper-based draft, the wrapper Lambda compensated by embedding source titles and URLs literally in the response text body. With the wrapper removed, that compensation has to live somewhere. Two architectural options, both real:

- **Option A: Skywalker's MCP server itself populates a `_sources` text field** (or equivalent) in the structured response envelope when the requesting client identifies as QuickSuite. This requires Skywalker to know which client is calling, which it would learn from a per-route tool variant or from MCP Gateway-side metadata. Slightly couples the backend to client identity, which the architecture has otherwise tried to avoid.
- **Option B: The QuickSuite chat-agent author writes the agent's instruction prompt to require source listing in the chat agent's output text whenever it cites Skywalker evidence.** This pushes the workaround into QuickSuite-side prompting rather than into the backend response envelope. Cleaner separation, but it means the workaround quality depends on chat-agent prompt discipline rather than on a contract-level guarantee.

Section 09 §3 calibration surface five tracks this as a calibration-active decision. Option B is the launch posture in the absence of Skywalker-side knowledge of which client is calling. If chat-agent prompting proves insufficient under production review, Option A becomes a re-litigation event for both Section 09 and Section 02.

## Error model

Unchanged from API_01:

- **JSON-RPC protocol errors** (the `{code, message}` error object on the JSON-RPC response) — surfaced when the request never reaches Skywalker's retrieval path. Causes on the QuickSuite path:
  - Federate JWT validation failure at MCP Gateway.
  - Bindle authorization failure (the calling Federate Service Profile's Amazon Teams group does not have `canInvoke` on Skywalker's MCP server bindle).
  - Unknown tool name (the chat agent invented a tool that is not on Skywalker's `tools/list`).
  - Tool arguments fail input schema validation at the Skywalker MCP server boundary.
- **Tool-execution errors** — surfaced as a normal JSON-RPC result with `result.isError: true` and a `content[0]` text block describing the failure mode. Causes:
  - Skywalker's PAPI client cannot resolve the alias into a usable scope triple (alias path).
  - Caller-supplied scope fails Skywalker-side validation (explicit-scope path).
  - Total retrieval failure inside Skywalker.
- **Backend abstention is never an error.** Always a successful `result_kind: "ABSTAIN"` response with `isError: false`.

The QuickSuite-side **299-second connector timeout** still applies (per [User Documentation - IDeA Assistant Integration into Quicksuite Flows](https://w.amazon.com/bin/view/ISS_Central_Analytics/IDeA_Assistant/IDeA_Integration_on_QSuite/Flows/)). Skywalker's p95 budget is 250–450 ms, two orders of magnitude inside this window, so the constraint is not binding at launch. It still has to be respected in any future design that could push a single request near 299 seconds.

## Skywalker MCP server registration with MCP Gateway

The Skywalker MCP server registers with MCP Gateway following the [MCP server vendor guide](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/howto-vendor/). Key steps relevant to the QuickSuite-client integration:

- Skywalker's MCP server is implemented as a Coral or Smithy-modeled service exposed through MCP Gateway. Per the BuilderHub MCP Gateway User Guide, MCP Gateway "enables MCP for your Coral service with zero code changes." The Skywalker-side modeling work is the conventional Coral or Smithy service definition for the three tools.
- The server is CloudAuth-protected on the MCP-Gateway-to-Skywalker outbound leg.
- Bindle resource `MCPGateway::skywalker-policy-mcp` (or whichever server identifier the team finalizes) is created automatically by MCP Gateway. The QuickSuite Federate Service Profile's Amazon Teams group is granted `canInvoke` on this bindle. The Slack application's IAM role and the UAT inline-agent orchestrator's IAM role are granted `canInvoke` on the same bindle for their respective routes.
- Tool schemas are declared per the Coral/Smithy modeling and surfaced through `tools/list` on the gateway.

The MCP Gateway concepts page documents network reachability: CORP fabric in all regions, PROD fabric in PDX/IAD/DUB/NRT, Native AWS in the same four regions. No VPN required.

## Quick Suite-specific gotchas (consolidated, MCP Gateway path)

| Issue | Solution |
| --- | --- |
| Federate Integ profile expires in one month or causes connector auto-delete | Use a Federate Prod profile with the "AWS QuickSuite Action Connectors" pre-approved use case. |
| Wrong MCP server URL (using `/mcp/` instead of `/federate/mcp/`) | Use `https://api.mcp.asbx.aws.dev/federate/mcp/{registry}/{server}` for the QuickSuite-Federate route. The `/mcp/` route is for SigV4. |
| QuickSuite "Sources" UI groups citations under connector name | Address through chat-agent prompting (Option B) at launch; Option A (Skywalker-side text-body source list) is the calibration-active fallback. |
| Originating end-user alias not visible in the Skywalker MCP server | Carry identity in the MCP tool arguments themselves, supplied by the QuickSuite chat-agent integration from its own session data. MCP Gateway does not propagate Federate JWT claims downstream on this path. |
| 299-second QuickSuite connector timeout | Not binding at launch (Skywalker budget is 250–450 ms). Future designs must not approach this limit. |

The AgentCore-Gateway-specific gotchas from the wrapper-based draft (`Allowed Audiences vs Allowed Clients`, the `targetName___toolName` prefix, REQUEST interceptor configuration, the API Gateway 29s timeout) **do not apply on this path**. They were artifacts of AgentCore Gateway, not properties of the integration.

## Review and quality bars

Launch checklist:

- **Skywalker MCP server is hosted on MCP Gateway.** Coral/Smithy service definition, MCP Gateway registration, Bindle resource created with `canInvoke` permissions for the three calling roles (Slack IAM role, UAT inline-agent orchestrator IAM role, QuickSuite Federate Service Profile group).
- **Federate Service Profile** — Prod profile created with the "AWS QuickSuite Action Connectors" pre-approved use case, AppSec sign-off, the Amazon Teams group that owns chat-agent access added.
- **QuickSuite connector** — registered through the QuickSuite console using the Federate Prod credentials and the `/federate/mcp/...` MCP server URL, OAuth handshake succeeds, `tools/list` returns the expected three tools, connector marked Active.
- **Chat agent** — created, Skywalker connector linked via Link actions, sharing opened to the Amazon Teams group, behavioral prompt written to enforce the citation-listing requirement (Option B until Option A is needed).
- **End-to-end test** — invoke from QuickSuite, verify CloudWatch logs in the Skywalker MCP server show the tool call arriving with the expected arguments, verify the response renders with citations in the chat agent.
- **AppSec review** — required for production. Confirms the integration's identity-carriage mechanism (whatever form it takes; see §"Outstanding unknowns") meets internal data-handling requirements.
- **Privacy review** — required for production.

There is no "RBO Routing Automated Test" equivalent on the QuickSuite path. Routing accuracy is validated by the chat-agent author through prompt iteration and production review, with Section 09 §3 calibration surface three tracking it.

## Sections of the architecture this binds

- Section 01 §3 decision two and the boundary statement (the QuickSuite path consumes Skywalker through MCP Gateway, not through a wrapper Lambda).
- Section 02 §3 decisions one through five (three core MCP tools; QuickSuite chooses among them based on what identity material it carries).
- Section 05 §3 (UAT uses MCP Gateway with CloudAuth inbound and OBO + TA per API_14; the QuickSuite path uses the same gateway with Federate inbound, on a different route).
- Section 08 §3 (Slack uses MCP Gateway with CloudAuth inbound and OBO + TA per API_14; alias-based tool path is the fixed Slack identity choice, with the alias arriving through TransitiveAuth rather than `arguments.alias`).
- Section 09 §3 in full, especially:
  - QuickSuite consumes Skywalker through MCP Gateway with Federate inbound — no AgentCore Gateway, no wrapper.
  - QuickSuite sees the actual `tools/list` of Skywalker's MCP server and chooses among the three tools directly.
  - Identity is carried by the integration through MCP tool arguments, not propagated by MCP Gateway.
- Section 09 §6 (end-to-end data flow on the QuickSuite path).
- Section 09 §7 (failure rules on the QuickSuite path).
- Section 09 §8 (calibration surfaces).
- API_01 (the core MCP contract; the three tools QuickSuite sees are the same three Skywalker exposes).

## Outstanding unknowns

- **The QuickSuite integration's identity-carriage mechanism is open.** The chat-agent author has to decide how the user's alias (or `country`/`level`/`role`) is supplied as a tool argument. Realistic options include reading from QuickSuite session metadata, prompting the user once per session, or relying on a small QuickSuite-side data source that maps the authenticated QuickSuite user to their alias. This is an integration-time decision and is tracked in Section 09 §9.
- **Skywalker MCP server identifier under MCP Gateway.** Surfaces during the MCP Gateway onboarding ticket. The architecture is buildable for any reasonable identifier; this is administrative.
- **Federate Prod Service Profile Client ID per stage** (beta/gamma/prod). Determined during Federate profile creation.
- **Amazon Teams group for chat-agent access control.** Operational.
- **QuickSuite-account region** (us-east-1 vs us-west-2). Operational.
- **The Sources-UI workaround posture.** Option B (chat-agent prompting) at launch; Option A (Skywalker-side `_sources` text-body field) becomes a re-litigation event for both Section 09 and Section 02 if Option B proves insufficient under production review.
- **MCP protocol version** — `2024-11-05` at launch (matching what the QuickSuite MCP-connector implementation supports today). Moves only as a coordinated update with QuickSuite's supported revision.
- **Identity-propagation roadmap.** UnifiedAuth and Midway A5 are both flagged "in progress" on the [MCP Gateway concepts](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/concepts/) page with ECD 2026 for Midway A5. If either lands within Skywalker's launch window and supports Federate-OAuth-inbound + CloudAuth-outbound delegation, the integration's identity-carriage mechanism could move from "MCP tool arguments" to "MCP Gateway header propagation," which would be a Section 09 calibration-surface re-litigation event rather than an architecture-class change.

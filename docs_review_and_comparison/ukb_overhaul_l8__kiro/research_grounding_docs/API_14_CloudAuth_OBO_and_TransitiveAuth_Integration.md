# API Contract 14. CloudAuth OBO and TransitiveAuth Integration

This contract pins how the Slack inline-agent orchestrator and the UAT inline-agent orchestrator reach Skywalker's MCP server through Amazon MCP Gateway on the **CloudAuth-inbound + CloudAuth-protected-backend** combination, with **CloudAuth OBO (Service OBO)** plus **TransitiveAuth (Human OBO)** delivering the human end-user's identity to the Skywalker backend without requiring it to ride in MCP tool arguments. It complements [API_01](API_01_MCP_Server_Protocol.md), [API_08](API_08_QuickSuite_MCP_Gateway_Integration.md), and [API_13](API_13_Network_Fabric_and_Internet_Exposure_Posture.md). It pairs with Sections 02, 05, 08, and 09.

This contract supersedes the earlier-draft "SigV4 inbound on `/mcp/{registry}/{server}` for Slack and UAT" approach. The earlier approach is not available for new MCP servers as of April 24, 2026 per the [MCP server vendor guide](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/howto-vendor/) ("AWSAuth inbound for CloudAuth-protected services is no longer available for new MCP servers created after April 24, 2026. Existing servers that already use this flow will continue to work until May 15, 2026. New servers must use CloudAuth inbound directly."). Both deadlines have passed. The architecture migrates to CloudAuth inbound directly, which is also the path with native OBO support documented at [UnifiedAuth MCP Server Builder Guidance](https://w.amazon.com/bin/view/Dev.CDO/UnifiedAuth/Agentic/MCP/Overview/) and [On-Behalf-Of Authorization](https://w.amazon.com/bin/view/Dev.CDO/UnifiedAuth/Agentic/OBO/).

The QuickSuite path (Section 09 / API_08) is unchanged — Federate-inbound on `/federate/mcp/{registry}/{server}` with identity carried in MCP tool arguments. CloudAuth OBO and TransitiveAuth do not currently apply to the Federate-inbound + CloudAuth-outbound combination.

Sources cited in this document are internal Amazon wikis and BuilderHub. Content from external sources has been rephrased for compliance with internal-source-only restrictions.

## The two layers and why we use both

UnifiedAuth's [On-Behalf-Of Authorization](https://w.amazon.com/bin/view/Dev.CDO/UnifiedAuth/Agentic/OBO/) wiki distinguishes two delegation layers that work together. Skywalker uses both because each one solves a different problem the architecture has.

**CloudAuth OBO (Service OBO)** lets an intermediary (here: MCP Gateway acting on behalf of the Slack or UAT orchestrator) call a downstream CloudAuth-protected service (here: Skywalker's MCP server) using the **calling application's** AAA permissions. Without OBO, MCP Gateway would call Skywalker as itself, and Skywalker would see the gateway's identity rather than the orchestrator's. With OBO, Skywalker sees the orchestrator's AAA application identity. From the [UnifiedAuth MCP Server Builder Guidance](https://w.amazon.com/bin/view/Dev.CDO/UnifiedAuth/Agentic/MCP/Overview/): "MCP Gateway: Invokes downstream service on-behalf-of the AI Agent by default. Authorization is evaluated at the MCP tool level. Service owners control permissions for agentic access within ServiceLens." This is on by default for the CloudAuth-inbound + CloudAuth-protected-backend combination, which is the combination Slack and UAT are migrating to.

What CloudAuth OBO alone solves: AAA authorization evaluates against the true calling application (Slack orchestrator vs UAT orchestrator vs anything else), not against MCP Gateway's identity. ServiceLens-side traffic controls and per-application QoS policies become possible. Per-application audit trails become possible. This is the right layer for "is *this orchestrator application* allowed to invoke Skywalker?"

What CloudAuth OBO alone does **not** solve: the human end-user's identity. The orchestrator's AAA application identity is a service identity; it's the same for every human invoker. Skywalker's correctness model needs the human's alias (for PAPI lookup) or the scope triple (for explicit-scope retrieval). CloudAuth OBO does not carry that.

**TransitiveAuth (Human OBO)** carries the original human invoker's identity and an `AgenticContext` describing the agent involvement, propagating both as a separate TA token alongside CloudAuth across the call chain. From the same OBO wiki: "TransitiveAuth exposes the initiating human principal and agentic context to the resource server for fine-grained controls." Slack and UAT orchestrators initiate the TA token at request entry; MCP Gateway propagates it through to Skywalker; Skywalker validates it server-side and reads the human's identity from the TA claims rather than from MCP tool arguments.

What TransitiveAuth solves: the human alias becomes available to Skywalker without depending on the orchestrator to inject it into `arguments.alias`. The `AgenticContext` makes "this request was initiated by a human via an AI agent" visible to Skywalker, which is part of what the [Helis](https://w.amazon.com/bin/view/Helis/) policy machinery exists to evaluate. The MCP tool contract no longer has to use `arguments.alias` as the canonical identity channel for these two paths — it becomes a fallback shape that other clients (notably QuickSuite, see below) still use.

## Why both layers, not just one

Three reasons the architecture wants both layers rather than picking one:

First, **CloudAuth OBO without TransitiveAuth** would solve the AAA-application-identity problem but force Skywalker to keep reading the human alias from `arguments.alias` on the MCP tool call. That keeps the alias-in-arguments wart in place for the very paths that have a published mechanism to remove it. Half-migration is worse than full migration; it does the disruptive part of the change without claiming the architectural benefit.

Second, **TransitiveAuth without CloudAuth OBO** is not how the layers compose on this auth combination. The OBO wiki explicitly describes them as working together: CloudAuth OBO authorizes the MCP server to be invoked on the agent's behalf; TransitiveAuth carries the human identity inside that authorized call. TA tokens are validated by downstream services that already trust the call chain — the chain has to be authorized first.

Third, **the calibration value of separating service identity from human identity is real.** A future Helis policy that says "the Skywalker orchestrator may invoke FAQ retrieval on behalf of any IC, but only on behalf of managers for explicit-scope queries against manager-only content" is expressible only when the system can see both layers separately. Picking one layer collapses that policy expressiveness.

## What changes for Slack and UAT

The auth-route change from SigV4 (`/mcp/{registry}/{server}`) to CloudAuth (`/ca/mcp/{registry}/{server}`) is the visible change. The deeper changes are AAA modeling and TA initiator/validator onboarding.

**For each of the Slack and UAT inline-agent orchestrators**, three onboarding pieces:

1. **Register as a CloudAuth-modeled AAA application in ServiceLens.** The Slack orchestrator and the UAT orchestrator each get an AAA application name (suggestion: `SkywalkerSlackOrchestrator` and `SkywalkerUATOrchestrator`, or whatever the team's naming convention prefers). Application registration is documented at the [ServiceLens Agent Builder Guide](https://w.amazon.com/bin/view/Main/ServiceLensAgentBuilderGuide/) and the [Autonomous Agent onboarding guide](https://w.amazon.com/bin/view/Dev.CDO/UnifiedAuth/CloudAuth/Onboarding/AutonomousAgent/). Each application carries an IAM principal or Apollo Environment identity for its runtime.

2. **Establish AAA relationships per the OBO Decision Guide's "OBO Required (Shared / Cross-Boundary)" path.** Each orchestrator AAA application establishes a `+ Add Dependency` relationship to MCP Gateway's `InvokeMcp` endpoint and to Skywalker's MCP server downstream resource. This is the "Delegator" role in the OBO model: the orchestrator holds the permissions used for downstream calls. MCP Gateway is the Delegate that exchanges inbound CloudAuth credentials for delegated access tokens. Skywalker's MCP server is the Resource Server that enforces AAA permissions using the invoking application's identity. The CloudAuth OBO Service OBO intake form is at [SIM Intake template 36d483dc](https://t.corp.amazon.com/create/templates/36d483dc-5db8-4453-8eb8-a8f04d4f3393).

3. **Onboard as TransitiveAuth initiators per the [TransitiveAuth Onboarding for Agentic](https://w.amazon.com/bin/view/Dev.CDO/UnifiedAuth/Agentic/TransitiveAuth) guide.** Each orchestrator, on receiving a request from the human end-user (Slack event with the human's Slack identity → resolved alias; UAT browser request with the Midway session → alias from `sub`), creates a TA token whose `AgenticContext` carries the human alias and the agent-involvement metadata. The CloudAuth MCP SDK propagates the TA token through to MCP Gateway as part of the outbound call chain. The reference Python pattern is in the OBO wiki:

   ```
   from cloudauth.credentials import AwsCredentials
   from cloudauth.mcp.client import cloudauth_streamablehttp_client
   from cloudauth.transitiveauth import TransitiveAuthClient, TransitiveAuthContext

   creds = AwsCredentials(region="us-east-1")
   ta_client = TransitiveAuthClient(creds)
   MCP_SERVER_URL = "https://api.mcp.asbx.aws.dev/ca/mcp/{registry}/{server}"

   def handle_request(request):
       context = TransitiveAuthContext(
           namespace="skywalker-orchestrator",
           params={"customer_id": request.alias},
       )
       # Create per-request MCP client (client-isolation requirement per TA)
       mcp_client = make_mcp_client_with_ta(ta_client, context, MCP_SERVER_URL)
       ...
   ```

   The Java equivalent uses the CloudAuth Java SDK's TA initiator surface. The architecturally important point is that **a new MCP client is created per human request** so TA contexts do not leak across requests. This is the "Client Isolation: One MCP Client Per Customer Request" rule from the OBO wiki.

**For the Skywalker MCP server**, two onboarding pieces:

1. **Stay registered as a CloudAuth-protected service.** The Coral service definition does not change; the gateway-to-Skywalker outbound leg was already CloudAuth on the previous SigV4-inbound path. What changes is that CloudAuth OBO is now in force on the inbound side, so the service principal Skywalker sees in the AAA-authorized request is the orchestrator's AAA application principal rather than MCP Gateway's.

2. **Onboard as a TransitiveAuth validator per the [TransitiveAuth ValidatorOnboarding/AuthUsingTA](https://w.amazon.com/bin/view/TransitiveAuth/Onboarding/ValidatorOnboarding/AuthUsingTA) guide.** Skywalker's MCP server reads the TA token from the inbound request, validates it, and extracts the human alias from the TA claims. This becomes the canonical identity channel for the Slack and UAT paths, replacing `arguments.alias`. Helis is the recommended paved-path policy engine for evaluating AgenticContext-aware policies; we do not adopt Helis at launch (the launch posture is "extract the alias and proceed exactly as today, with Helis as a future calibration surface for cross-application policy").

**For MCP Gateway**, no per-server onboarding change beyond the existing Bindle-resource-per-server. The gateway's CloudAuth-inbound + CloudAuth-protected-backend path supports OBO by default and propagates TA tokens transparently when the orchestrator is a registered TA initiator.

## What stays the same

- **The MCP tool surface from API_01 and Section 02 §2.** The three tools (`skywalker.search.by_alias`, `skywalker.search.by_employee_id`, `skywalker.search.by_explicit_scope`) and their input schemas do not change. Skywalker still validates `arguments` against the input schema. The change is *which channel the canonical identity is read from* on the Slack and UAT paths — TA token, not `arguments.alias`. The argument shape stays in the contract because QuickSuite (Section 09) still uses it on the Federate-inbound path, where TA delegation is not yet published.
- **The output envelope.** `result_kind`, `route`, `scope_snapshot`, `evidence`, `abstain_reason`, `correlation_id` are unchanged.
- **The two-layer error model.** JSON-RPC protocol errors (auth failures at the gateway, including AAA authorization failures, TA token validation failures) and tool-execution errors (PAPI unable to resolve the TA-supplied alias, total retrieval failure) keep the same semantics. A new failure class — TA token missing or malformed when expected — surfaces as a JSON-RPC protocol error with a descriptive message; clients should retry once after refreshing the TA initiator state, and persistent failure is a hard error.
- **The Bindle resource and its three authorized principals.** `MCPGateway::{skywalker-server-id}` continues to be the authorization boundary. The three principals (Slack orchestrator AAA application, UAT orchestrator AAA application, QuickSuite Federate Service Profile group) still have `canInvoke`. The change is the orchestrator principals are now CloudAuth AAA applications rather than IAM roles. QuickSuite is unchanged.
- **Sync MCP responses.** Skywalker still returns one JSON-RPC result; MCP Gateway still returns one sync HTTP response. Streaming continues to live above the MCP boundary per [API_12](API_12_Bedrock_Inline_Agent_Streaming.md).

## What this means for Skywalker's PAPI dependency

Today, on the alias path, Skywalker calls PAPI to resolve `(country, level, role)` from the alias. With TA in place, the alias arrives in the TA token's invoking-human claim instead of in `arguments.alias`. The PAPI call stays. The architecturally important property is that Skywalker's correctness model — "the answer must be scoped to the human asking" — is now satisfied by reading a verified human identity from the TA token rather than by trusting an argument the orchestrator chose to inject. The trust-shape improves; the dependency does not.

For the explicit-scope path, the orchestrator can continue to supply `(country, level, role)` as MCP tool arguments — TA carries the human identity, but the scope-triple short-circuit is a separate Section 02 contract decision that exists for callers that already hold authoritative scope. UAT today supplies `(country, level, role)` from custom Federate claims in the Midway token because the team's Federate Service Profile mints them; that arrangement is unchanged. The TA token additionally carries the human alias, which becomes available for audit and for any future policy that wants to evaluate against the human identity rather than the supplied scope.

## Failure modes and rollout sequencing

Three failure modes the implementation needs to handle cleanly:

1. **TA initiator setup fails on the orchestrator.** Without a valid TA initiator, the orchestrator cannot mint TA tokens. The CloudAuth call to MCP Gateway can still succeed (CloudAuth OBO is independent of TA), but Skywalker will see the request as TA-token-missing. Launch posture: Skywalker fails closed when the TA token is missing on the Slack/UAT paths; the orchestrator is responsible for the initiator setup before going live. The fallback shape — accept `arguments.alias` when TA is missing — is deliberately *not* the launch posture, because allowing it would erode the TA migration's main benefit. We accept that an orchestrator misconfiguration produces a hard failure rather than a silent degradation.

2. **TA validator setup fails on Skywalker.** Without a valid validator, Skywalker cannot read the TA token. This is a Skywalker-side configuration issue; the failure is "all Slack/UAT requests fail with TA validation error" until the validator is configured. Pre-launch testing in beta and gamma is the right place to catch this; the rollout sequence below is structured to surface it early.

3. **AAA permission missing on the new CloudAuth route.** The orchestrator AAA application doesn't yet have the AAA relationship to MCP Gateway's `InvokeMcp` or to Skywalker's MCP server resource. The gateway returns a `NotAuthorizedException`. Resolution is the same as today's Bindle-permission failures: add the missing relationship and re-test.

**Rollout sequencing.** The ordering matters because Slack and UAT are mid-launch:

1. Skywalker's MCP server is registered with MCP Gateway and reachable on both `/mcp/` (existing SigV4) and `/ca/mcp/` (new CloudAuth) routes during the migration window. Existing servers that pre-date April 24, 2026 keep their SigV4-inbound + CloudAuth-outbound path until May 15, 2026; new servers cannot use that combination at all. Skywalker's server registration date determines which window applies; if Skywalker is treated as a new server (likely, given the architecture is still in design), there is no SigV4-inbound option and the CloudAuth route is the only path from day one.
2. UAT orchestrator goes first because its scope is narrower and its launch date (June 30, 2026) is the earliest forcing function. UAT registers its AAA application, establishes AAA relationships, onboards as TA initiator, and validates end-to-end against beta Skywalker.
3. Slack orchestrator follows the same sequence on the production launch timeline.
4. QuickSuite is untouched on the Federate route.

Beta and gamma stages should run the new path in parallel with any remaining SigV4 traffic during the cutover window, with a clear cut-date past which the SigV4 route is decommissioned. Decommissioning is a Bindle-permission removal on the SigV4 caller principals; the `/mcp/{registry}/{server}` route itself is still served by the gateway for other tenants.

## Sections of the architecture this binds

- **Section 02 §2 (transport) and §3 (fixed decisions).** Slack and UAT migrate to CloudAuth-inbound at `/ca/mcp/{registry}/{server}` with CloudAuth OBO + TransitiveAuth. The MCP tool surface stays as fixed; the canonical identity channel for the Slack and UAT paths becomes the TA token, with `arguments.alias` and the explicit-scope fields kept as the contract shape that QuickSuite (and any future caller without TA) uses.
- **Section 05 §3 (UAT).** Orchestrator becomes a CloudAuth AAA application. CloudAuth OBO + TransitiveAuth onboarding is part of UAT's launch checklist. Custom Federate claims for `(country, level, role)` continue to be the mechanism for explicit-scope retrieval; the alias propagates via TA in addition.
- **Section 08 §3 (Slack).** Orchestrator becomes a CloudAuth AAA application. The Slack-user-to-alias resolver still runs on the orchestrator side; the resolved alias becomes the input to the TA initiator instead of becoming `arguments.alias` directly. CloudAuth OBO + TransitiveAuth onboarding is part of Slack's launch checklist.
- **Section 09 §1 and §3 (QuickSuite).** Unchanged. The earlier overgeneralization that "MCP Gateway does not propagate identity downstream" should be scoped to the Federate-inbound combination specifically. CloudAuth-inbound paths now propagate identity via the OBO + TA pair.
- **Section 10 (decision log).** New D-entries: "Slack and UAT migrate to CloudAuth-inbound on `/ca/mcp/`," "CloudAuth OBO is the default service-identity propagation," "TransitiveAuth is the human-identity propagation mechanism for the Slack and UAT paths," and the BuilderHub deadline rationale.
- **API_01.** The auth-shape table in API_01 should acknowledge that CloudAuth-inbound + CloudAuth-protected combination supports OBO + TA natively, while the Federate-inbound combination does not (yet).
- **API_08.** Small correction: the "MCP Gateway does not propagate Federate JWT claims downstream on this path" statement is correct for the Federate-inbound combination but should not be generalized to all of MCP Gateway. The Federate combination remains the path where identity has to ride in MCP tool arguments today.

## Outstanding unknowns

- **TA validator implementation effort on Skywalker's MCP server.** The CloudAuth Java SDK's TA validator surface is documented; the integration effort depends on the Coral service framework version. If Coral support requires upgrades, the upgrade is a prerequisite rather than a standalone task.
- **Helis policy adoption.** The architecture's launch posture is "extract alias from TA, proceed exactly as today." Adopting Helis for AgenticContext-aware policy evaluation is a calibration-active future surface — not part of the launch but enabled by being on the TA path.
- **MCP Gateway's identity-propagation roadmap on Federate-inbound.** UnifiedAuth and Midway A5 are both flagged "in progress" on the [MCP Gateway concepts](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/concepts/) page with ECD 2026 for Midway A5. If Federate-inbound + CloudAuth-outbound delegation lands within Skywalker's launch window, the QuickSuite path could move from "alias in arguments" to "alias in propagated claim," which would be a Section 09 calibration-surface event rather than an architecture-class change.
- **Skywalker MCP server registration date.** Determines whether the SigV4 grace window applies. The architecture proceeds as if it does not (Skywalker is treated as a new server) so the CloudAuth-inbound posture is the only valid path from day one.
- **AAA application names.** Operational. The architecture is buildable with any reasonable names.

## Section 09. QuickSuite Integration and MCP Consumption Model

Section 08 fixed the Slack surface as the client path where the team builds the agent behavior, the prompt layer, and the turn handling. This section moves to the other client surface and fixes the asymmetry directly rather than pretending the two integrations are the same shape. They are not.

QuickSuite is different for one simple reason: **it already handles MCP and already has its own agent runtime** — its own chat-agent surface, knowledge spaces, and intent-based routing across registered MCP connectors. The work for QuickSuite is not to build a second conversation layer or recreate prompting in a new place. It is to define how QuickSuite consumes Skywalker through MCP, what request shapes it uses, what response shapes come back, and where the boundary stays hard so the system does not drift into two different backends hiding behind one name.

The QuickSuite path is the cleanest expression of the architecture. A user interacts with their QuickSuite chat agent; the agent's intent routing decides Skywalker is the right tool; QuickSuite calls Skywalker through its registered MCP connector, routed through Amazon MCP Gateway with Federate-OAuth inbound auth; Skywalker performs scoped retrieval and returns a structured result; QuickSuite remains the conversational layer that decides how that result is presented. Slack needing more work on our side does not change this — it only makes the QuickSuite shape easier to forget unless written down. This section is intentionally thinner than Section 08, and the whole point is to make sure thin stays precise.

### 1. The consumption boundary

This section owns the QuickSuite-facing integration from the moment QuickSuite decides to invoke Skywalker through its registered MCP connector to the moment a structured backend result returns: the QuickSuite-side consumption model for the MCP contract, the MCP Gateway transport configuration on the Federate-inbound route, the Federate Service Profile that authenticates QuickSuite into the gateway, and the Bindle-based authorization controlling which Federate principals may invoke Skywalker's server.

It owns the explicit decision that **there is no wrapper Lambda between QuickSuite and Skywalker**. Earlier drafts imagined a Skywalker-owned plug-in wrapper under Bedrock AgentCore Gateway translating one task-oriented tool into the three core tools. That layer does not exist on the supported path. QuickSuite reads Skywalker's actual `tools/list` directly through the gateway and chooses among the three core tools based on what identity material its chat-agent integration is configured to supply ([API_08]; tenet 5).

It owns one structural consequence of the Federate-inbound route: **MCP Gateway does not propagate the originating end-user identity downstream on the Federate-inbound + CloudAuth-outbound combination.** The gateway's published delegated-identity patterns are all in-progress and none cover this combination; the statement is specific to QuickSuite's auth shape, not a general gateway property — the CloudAuth-inbound combination Slack and UAT use supports OBO + TransitiveAuth natively ([API_14]). Identity for retrieval scope on this path is therefore carried by the integration itself, as MCP tool arguments, and §3 fixes that as a real architectural decision rather than a footnote.

And it owns the candid statement of what that means, in the same register Section 06 uses for UKB's scope guarantee: **QuickSuite's identity trust is strictly weaker than the other production paths', not merely different.** D-39 fails the Slack and UAT paths closed on missing TransitiveAuth precisely because argument-supplied identity would let a caller inject any alias and defeat the verified-claims posture — and QuickSuite supplies the alias as an argument, by structural necessity, on one of two production clients in a system whose entire correctness model is identity scoping. Skywalker cannot verify the binding between the gateway-authenticated user session and the alias the integration asserts; it trusts QuickSuite's integration code to bind honestly. Three things bound the exposure without eliminating it: the Federate JWT means a real Midway-authenticated user session sits behind every request (the human is authenticated; the *binding* is what is asserted), the blast radius is QuickSuite-originated requests only, and every response's `scope_snapshot` plus `correlation_id` makes a mis-asserted identity auditable after the fact. The structural fix is the delegated-identity migration (§8 surface six), and until it ships this asymmetry is a known weaker guarantee, carried openly — not a footnote about channels.

It does not own QuickSuite's chat-agent runtime, behavioral prompt, conversation memory, rendering, or product behavior outside the tool call; PAPI, the entry contract, ingestion, UKB, reranking, or abstention (Sections 02–07); or QuickSuite's final answer phrasing — the Skywalker side stops at the structured result. And it owns one non-ownership boundary worth bolding: QuickSuite is a first-class integration surface but **not a privileged caller with a different backend**. The moment one client depends on a hidden path that bypasses MCP or normal validation, the backend has forked.

### 2. Inputs, outputs, and contracts

**The input** is a QuickSuite chat-agent decision to call Skywalker for one user request — a tool invocation QuickSuite already routed, not arbitrary conversation state or a raw transcript.

**Transport.** Amazon MCP Gateway on the Federate-inbound route: `https://api.mcp.asbx.aws.dev/federate/mcp/{registry}/{skywalker-server-id}`. The gateway validates the inbound Federate JWT directly — no AgentCore Gateway, no REQUEST interceptor Lambda. Bindle authorization (`canInvoke` on `MCPGateway::{skywalker-server-id}`) gates whether the QuickSuite Federate Service Profile's Amazon Teams group may invoke the server. On the outbound leg the gateway converts Federate-inbound to CloudAuth; Skywalker's MCP server is CloudAuth-protected. Gateway overhead is published at 50–150 ms with a 50 TPS per-client throttle ([API_08]) — both well inside budget.

**The request contract** is the three core tools fixed in Section 02: `skywalker.search.by_alias` (`{query_text, alias, correlation_id?}`), `skywalker.search.by_employee_id` (not an expected QuickSuite pattern at launch), and `skywalker.search.by_explicit_scope` (`{query_text, employee_id, country, level, role, correlation_id?}`, with `role` carrying the employee-class vocabulary per Section 02). No fourth task-oriented tool; no wrapper translating tool names. **Identity rides in `arguments`** — not in headers, not in propagated JWT claims. This differs materially from the v1 AZA wrapper design (signed identity headers) and from the AgentCore-interceptor draft (`__authContext` injection); neither is part of the current path. QuickSuite's `_meta.additionalProperties` conversation payload (`conversationHistory`, `attachments`, et al.) is not used: the current turn rides as `query_text`, QuickSuite keeps its history on its side, and Skywalker stays stateless.

The **identity-carriage mechanism inside QuickSuite is the largest open item on this path** (§9): the chat agent reading the alias from session metadata, a small QuickSuite-side alias-mapping data source, or per-session prompted scope are all shapes the architecture supports without modification — Skywalker validates arguments against the Section 02 schema and proceeds regardless of how QuickSuite obtained them.

**The output contract** is the same envelope every client receives — `result_kind`, `route`, `scope_snapshot`, `evidence[]` (ranked candidates with title, text, source URL, policy links, arm-local rank, rerank score), `abstain_reason`, `correlation_id` — flowing unchanged through the gateway. No wrapper reshapes it into a different `content[]` convention.

**The Sources-UI quirk, named explicitly.** QuickSuite's chat-agent "Sources" UI groups citations under the connector name rather than rendering per-document URLs from the payload — a documented current limitation. With no wrapper available to embed sources in a response text body, the workaround lives elsewhere: the **launch posture is chat-agent-side prompting** — the chat-agent author writes the instruction prompt to require explicit source listing in the agent's output text whenever it cites Skywalker evidence. The fallback, if production review shows prompting is insufficient, is Skywalker populating a `_sources` text field when the caller identifies as QuickSuite — which would introduce client-identity awareness on the Skywalker side that the architecture otherwise avoids. The trade-off is real and deliberately deferred until launch evidence justifies it (§8).

**The error model** is preserved across the gateway: JSON-RPC protocol errors for unknown tool, malformed arguments, Federate JWT validation failure, and Bindle authorization failure; tool-execution errors (`isError: true`) for PAPI-unresolvable identity, post-resolution scope failure, and total retrieval failure; abstention always a successful `result_kind: "ABSTAIN"` — never an error.

**Sync at the boundary.** Skywalker returns a single JSON-RPC result; the gateway returns a single sync HTTP response; QuickSuite consumes the complete envelope before composing its message. No SSE, no progressive tokens on this hop — the envelope is structured, not narrative, and the Federate-inbound transport exposes no streaming framing. Skywalker does not run the inline agent on this path (QuickSuite's runtime is the agent layer), so the streaming asymmetry across surfaces ([API_12]) simply does not apply here: whether QuickSuite streams its own generation to its own UI is QuickSuite's product behavior.

**One operational ceiling:** QuickSuite's MCP connector imposes a 299-second response timeout per invocation. Skywalker's 800–1000 ms p95 budget sits far below it, so it never binds at launch — recorded because if any future change pushed a request toward it, the correct behavior is cutting the call cleanly with a JSON-RPC error rather than letting QuickSuite's own timeout render ambiguously to the user.

### 3. Fixed decisions

**Decision 1 — Transport is MCP Gateway on the Federate-inbound route; no wrapper, no AgentCore Gateway, no interceptor.** Binds the registration, the auth chain, and the one-transport posture across all three clients (tenet 5). Reopens never; the wrapper alternative was rejected with reasons (§4).

**Decision 2 — QuickSuite reads the real `tools/list` and chooses among the three core tools directly.** Tool descriptions published by Skywalker are consumed verbatim by QuickSuite's intent routing — making description quality a backend responsibility (§8). Binds the tool-surface contract. Reopens never.

**Decision 3 — Identity is carried by the integration, in tool arguments.** Not propagated by the gateway (no published pattern on this auth combination); not symmetric with Slack/UAT, which carry the human identity via TransitiveAuth on the CloudAuth route. If the gateway ever publishes a delegated-identity pattern for Federate-inbound + CloudAuth-outbound, QuickSuite can move to the same OBO + TA shape — a calibration-active migration (§8), not an architecture change. Binds the chat-agent integration's responsibilities and §9's open mechanism. Reopens via that migration path.

**Decision 4 — QuickSuite remains the conversational layer.** Skywalker owes no QuickSuite-ready phrasing, session continuity, or follow-up interpretation. Binds the boundary. Reopens never.

**Decision 5 — Skywalker returns structured backend artifacts.** Rich enough to answer from; never final prose. Pinned in [API_01], identical across clients. Binds the envelope. Reopens never.

**Decision 6 — The MCP boundary is sync.** True symmetrically across all three paths at the boundary itself; streaming is a higher-layer property that exists only where Skywalker runs the agent (Slack, UAT). Binds the response handling. Reopens only if the gateway's transport grows streaming *and* a structured-envelope use case for it appears — neither is expected.

**Decision 7 — Same outcome classes as every client.** No QuickSuite-only answerability semantics, source preference, or abstention meaning. Binds backend uniformity. Reopens never.

**Decision 8 — The inbound authentication surface.** Federate OAuth (Authorization Code + PKCE) against a **Federate Prod Service Profile** created under the pre-approved "AWS QuickSuite Action Connectors" use case (no security ticket required). Integ profiles are explicitly not used: they expire after one month, and QuickSuite auto-deletes connectors backed by them every 24 hours. Binds the profile provisioning and the launch checklist (§9). Reopens never.

**Decision 9 — Bindle gates access, managed through one Teams group.** The gateway creates one Bindle resource per registered server; the Federate profile's Teams group holds `canInvoke`; user addition and removal is Teams-group membership, centralized. Binds access management. Reopens only if access needs become finer-grained than group membership (§8).

**Decision 10 — Connector registration mechanics.** Registered through the QuickSuite console (Integrations → Actions → MCP) by the QuickSuite-account administrator, carrying the Federate-route URL, Client ID and Secret, the Federate Prod authorize URL (`https://idp.federate.amazon.com/api/oauth2/v1/authorize`) and token URL (`https://idp.federate.amazon.com/api/oauth2/v2/token`), PKCE enabled, QuickSuite's region-specific OAuth callback registered in the profile's redirect URIs. Sharing controls open access via the same Teams group that holds Bindle permission, so the two access lists stay synchronized. Binds the onboarding runbook. Reopens never.

**Decision 11 — No QuickSuite-specific scope or trust logic in Skywalker.** Alias calls resolve PAPI; explicit-scope calls bypass it; exactly the Section 02 contract, with no QuickSuite-only short-circuit, header trust, or validation rule. Binds backend uniformity. Reopens never.

**Decision 12 — The Sources-UI workaround posture.** Launch is chat-agent-side prompting; the Skywalker-side `_sources` field is the calibration-active fallback with its client-identity cost named honestly (§2). Binds the chat-agent prompt requirements. Reopens via §8 surface five.

### 4. Alternatives considered

**A QuickSuite-specific direct integration bypassing the shared MCP contract.** Rejected — not only for reuse but for discipline: one hidden privileged entry path and the backend contract becomes a suggestion.

**Registering Skywalker as a QuickSuite Space/Knowledge Base.** Rejected: Spaces force a static-document model onto a retrieval-backed corpus and pre-index under the account identity rather than each user's — collapsing the identity-aware-scoping invariant into "every user gets the same surface" (violates tenet 3).

**The v2 AgentCore Gateway wrapper with a REQUEST interceptor.** Rejected: MCP Gateway is the paved path with materially simpler client setup; a wrapper adds a deployment surface and a translation seam the architecture no longer needs; and one gateway across all three paths gives one auth-onboarding story and one Bindle surface. The v2 design is preserved in git history; its AgentCore-specific gotchas (Allowed Audiences versus Allowed Clients, the API Gateway 29-second timeout) do not apply on this path.

**The POPSTAR/phonetool identity-resolution pattern** some QuickSuite-native agents use — the chat agent resolving identity before invoking the tool, then calling `by_explicit_scope` with a pre-resolved triple. Live, as one possible implementation of Decision 3's carriage requirement; the chat-agent author chooses.

**Service-IAM identity instead of end-user identity.** Rejected, same as on the v1 path: scope is part of correctness, answers change by user, and a service identity erases exactly what the architecture exists to enforce. The Federate JWT carries a per-user identity even though the gateway does not propagate it downstream.

**Skywalker producing QuickSuite-ready prose.** Rejected; it erases the fact that QuickSuite already has an agent runtime.

**Streaming the MCP envelope.** Rejected twice over: no time-to-first-token to optimize on a fixed-shape object, and the transport exposes no streaming framing. Neither reason constrains streaming above the boundary on the paths that have an agent to stream from.

### 5. Assumptions inherited from upstream

From Section 01: the system boundary and tenets. From Section 02: the three-tool contract, the per-route identity channels (arguments on this path), the employee-class scope vocabulary, and the error-model split. From Sections 04–07: routing, UKB, hybrid retrieval, reranking, and abstention arrive as stable backend truth — QuickSuite decides none of them. From Sections 05 and 08: the shared transport posture — **all three production paths ride Amazon MCP Gateway against one Skywalker server and one Bindle resource; Slack and UAT on the CloudAuth-inbound route with OBO + TransitiveAuth, QuickSuite on the Federate-inbound route with identity in arguments** — so access additions and revocations across all three paths are managed against the same bindle. Two further premises: QuickSuite users are already strongly authenticated through Midway by the time their JWT reaches the gateway, keeping this section from becoming an authentication design document; and the client asymmetry from Section 08 holds — the architectural work here concentrates on the MCP seam because QuickSuite already owns the agent.

### 6. End-to-end data flow

**Step one.** A user interacts with their QuickSuite chat agent; intent routing determines the request belongs on the Skywalker path and selects a tool from the real `tools/list` (typically `by_alias` at launch, per the identity-carriage decision).

**Step two.** QuickSuite checks for a valid Federate access token for the registered connector; if absent, it runs the OAuth Authorization Code + PKCE flow against the connector's Federate Prod profile. The user authenticates through Midway; Federate issues a JWT with the Midway login in the `sub` claim.

**Step three.** QuickSuite invokes `tools/call` against the Federate-route URL with `Authorization: Bearer <token>` and the JSON-RPC body carrying the tool name and arguments — including whatever identity material the integration supplies.

**Steps four and five.** The gateway validates the JWT against Federate's OIDC discovery document (signature, expiry, issuer, audience) and performs the Bindle check; failures reject at the gateway as JSON-RPC protocol errors before Skywalker sees anything.

**Step six.** The gateway forwards the authorized request on the CloudAuth-outbound leg, body unchanged — no interceptor, no reshaping, no synthetic identity headers. Skywalker reads identity from `arguments`.

**Step seven.** Skywalker validates against the Section 02 entry contract. Alias-shaped requests resolve country, level, and employee class through PAPI; explicit-scope requests use the supplied values. The canonical scoped request is constructed.

**Step eight.** The backend runtime executes per Sections 04 through 07 — gate, FAQ-only or dual-arm retrieval, normalization, reranking, abstain judgment — identically to every other client.

**Steps nine and ten.** One structured outcome returns as a single sync JSON-RPC result; the gateway passes it unchanged; the chat-agent runtime reads the envelope, composes the user-facing answer with citations drawn from `evidence[]`, and — per the launch Sources posture — lists source titles and URLs in its output text rather than relying on the Sources UI.

**Step eleven.** Multi-turn continuation stays on QuickSuite's side until the agent decides a new tool call is warranted. Skywalker does not become stateful.

The property that carries this flow: QuickSuite-specific behavior meaningfully exists in exactly two places — the decision to invoke the tool, and the use of the structured result afterward. The retrieval path in the middle is the same disciplined backend every client gets, behind the same gateway every client uses.

### 7. Failure behavior and abstain behavior

**Tool-selection failure is client-side.** The agent not calling Skywalker when it should, or calling on the wrong turn, is a QuickSuite orchestration problem — visible in the §8 routing surfaces, never disguised as backend failure.

**JWT validation failure is a hard gateway reject** — expired token, missing audience, signature mismatch — surfacing as a protocol error; the session re-authenticates against Federate before the next call.

**Bindle authorization failure is likewise a protocol error** at the gateway; the fix is Teams-group membership, an operational action.

**PAPI failure has the same shape as on every path**: client-level retries, fail closed, surfacing as `isError: true` with an identity-unresolvable message (Section 02 §7).

**Abstention is a successful result.** The chat-agent runtime must distinguish abstention from outage and render it as the evidence-grounded "not enough to answer for your situation" — the same rule Slack observes, preserved unchanged. Whether QuickSuite's prompt achieves that reliably is calibration, and the two abstain reasons give it the machine-readable basis.

**The 299-second ceiling** never binds at launch; the contingency posture (cut cleanly with a protocol error) is recorded in §2.

Non-goals: QuickSuite's prompt design, conversation memory, sharing administration, knowledge spaces, the gateway's own deployment, the QuickSuite-account connector administration, any QuickSuite-only retrieval path — and streaming on the QuickSuite user surface, which belongs to QuickSuite because Skywalker runs no agent there.

### 8. Calibration surfaces

**Surface one — the identity-carriage mechanism.** Open at launch (§9); calibrates once the chat-agent author commits and production load proves the choice in or out.

**Surface two — tool-selection reliability.** Both search tools are exposed; whether QuickSuite's intent routing picks correctly is empirical. Re-litigate on production routing data showing systematic wrong-tool selection.

**Surface three — tool description text.** QuickSuite reads Skywalker's published descriptions as-is, making description quality a backend concern: launch descriptions name in-scope query classes (per-diem, ride-home expensing, manager flight approval, home-internet eligibility, receipt submission, interview-meal expensing) and out-of-scope classes (time off → MyHR, payroll, AWS service docs). Re-litigate if review shows mis-routing in either direction.

**Surface four — sharing-control granularity.** One Teams group for both Federate sharing and Bindle permission. Re-litigate only if access needs outgrow group membership.

**Surface five — the Sources workaround.** Prompting at launch; the `_sources` envelope field as fallback, with its client-identity cost. Re-litigate on production review evidence that prompting is insufficient.

**Surface six — migration to delegated identity.** If the gateway ships TA (or successor) support for the Federate-inbound combination, moving identity from arguments to propagated claims simplifies the chat-agent author's job and makes the identity channel consistent across all three paths — a calibration-active migration when the platform allows it.

**Surface seven — production review feedback.** Findings about mishandled abstention, over-smoothed fallback responses, or dropped sources legitimately reopen the QuickSuite-facing guidance.

### 9. Open questions

Most open questions across this series share one precondition, stated in full in Section 10 §9: they are only answerable against real user data at meaningful volume — a few hundred actual users, arriving with the September production launch. Until then, launch postures stand, and pre-launch pressure to move them resolves as a recorded non-change.

**The identity-carriage mechanism** *(the largest open item on this path; an integration-time decision for the chat-agent author — the Skywalker contract is fixed under every candidate shape).* Session-metadata alias, a QuickSuite-side alias data source, or prompted scope.

**The Skywalker server identifier under MCP Gateway** *(disclaimer: operational; surfaces at onboarding).*

**The production launch checklist** *(disclaimer: execution items, not design questions).* Federate Prod profile creation under the pre-approved use case, ASR/AppSec and privacy review of the integration's identity handling, callback-URI registration, and Teams-group wiring across the profile and the bindle.

**MCP protocol revision** *(disclaimer: pinned at `2024-11-05` to match QuickSuite's connector; moves only as a coordinated update).*

**Citation rendering under QuickSuite's LLM path** *(disclaimer: empirical, lands during the chat-agent author's testing).* Whether prompt-required source listing compensates adequately for the Sources-UI grouping limitation — feeds surface five.

### Closing position

The QuickSuite integration is thinner than the Slack integration, and not conceptually lighter — it is the cleanest expression of the architecture because it keeps the boundary honest. QuickSuite's chat-agent runtime decides when Skywalker is the right tool and calls it through Amazon MCP Gateway's Federate-inbound route, authenticated by a Federate Prod Service Profile under the pre-approved QuickSuite use case. The gateway validates the JWT, authorizes against the Bindle resource, converts to CloudAuth outbound, and forwards the call unchanged — no wrapper, no AgentCore Gateway, no interceptor. QuickSuite reads the real `tools/list`, carries identity in tool arguments because the platform does not yet propagate it on this combination, and receives the same structured envelope as every client, sync and fully formed. Skywalker performs the same scoped retrieval it performs for everyone.

If this seam stays clean, Skywalker remains one retrieval backend serving multiple client surfaces on one transport with one access surface. If it blurs, the system forks. This section exists to keep it from blurring.

---

*Stale-source flags raised in this section, for propagation: prior Section 09 `citations[]` mention in the envelope description and "sibling-and-linked-parent expansion" in the flow (superseded per Sections 02 and 03); prior Section 09 §5 claims that UAT and Slack ride the SigV4-inbound route (stale leftovers — both are CloudAuth-inbound with OBO + TransitiveAuth per [API_14] and Sections 05/08); prior Section 09 residual "wrapper" phrasing on the Federate profile (the profile authenticates the connector; no wrapper exists); prior Section 09 manager-versus-IC scope framing (superseded by employee class, Section 01 Decision 8).*

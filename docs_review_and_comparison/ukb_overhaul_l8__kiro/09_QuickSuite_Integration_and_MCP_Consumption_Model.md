## Section 09. QuickSuite Integration and MCP Consumption Model

### 1. Tenets

This section treats QuickSuite as a first-class Skywalker client without allowing it to become a second Skywalker architecture. The tenets below are ordered. When they conflict, the earlier tenet wins.

1. We prefer one shared Skywalker MCP contract over a QuickSuite-specific service face when client convenience conflicts with backend consistency.
2. We prefer explicit scope carried in tool arguments over inferred delegated identity when the supported gateway route does not propagate the originating end-user identity to Skywalker.
3. We prefer QuickSuite-owned conversation and rendering over Skywalker-generated final prose when client autonomy conflicts with backend answer control.
4. We prefer structured synchronous backend results over streamed or conversational fragments when evidence, abstention, and citation handling must remain inspectable.
5. We prefer backend abstention over client-side smoothing when evidence is insufficient for the user's resolved scope.

These tenets intentionally make QuickSuite thinner than Slack. Slack is a client surface where the Skywalker team owns the agent behavior, prompt layer, and user-facing turn handling. QuickSuite already has a chat-agent runtime, knowledge spaces, conversation memory, and intent-based routing across registered MCP connectors. The architecture should connect that runtime to Skywalker's scoped retrieval backend, not duplicate or override the runtime.

### 2. Problem and intent

The QuickSuite integration problem is not to build another conversational agent. It is to define how QuickSuite consumes Skywalker through MCP, what identity material QuickSuite must supply, what structured result Skywalker returns, and where the client/backend boundary stays fixed.

The intent is that QuickSuite decides when Skywalker is the right tool, invokes Skywalker through Amazon MCP Gateway, supplies the current user scope through supported MCP tool arguments, receives one structured backend result, and renders the final user-facing answer itself. Skywalker remains responsible for scoped retrieval, backend routing, evidence packaging, reranking, answerability judgment, and abstention. QuickSuite remains responsible for tool selection, prompt behavior, conversation memory, follow-up interpretation, final wording, and citation rendering in the chat surface.

This division matters because QuickSuite is powerful enough to blur boundaries. If Skywalker starts producing QuickSuite-specific prose, QuickSuite-specific routing rules, or QuickSuite-only identity shortcuts, the system will fork. A fork would make correctness harder to reason about because identical employee-policy questions could behave differently depending on whether they entered through Slack, UAT, or QuickSuite. This section prevents that by making the MCP boundary explicit and by recording which responsibilities sit on each side.

### 3. Boundary and non-goals

The system described here is the QuickSuite-to-Skywalker consumption path through Amazon MCP Gateway. It begins when a QuickSuite chat agent decides to call one of Skywalker's registered MCP tools. It ends when QuickSuite receives Skywalker's complete JSON-RPC tool result and resumes control of user-facing conversation.

Skywalker owns the MCP tools, argument validation, scope construction, PAPI lookup on alias-shaped calls, backend retrieval orchestration, FAQ/UKB routing, hybrid retrieval behavior, reranking, evidence packaging, answerability judgment, abstain result construction, and correlation metadata. QuickSuite owns the chat-agent configuration, intent routing before the call, available identity material before the call, tool choice, final response composition, citation display, and any multi-turn continuation after the call.

The non-goals are load-bearing. This section does not define a QuickSuite-only backend, a wrapper Lambda, an AgentCore Gateway path, a REQUEST interceptor Lambda, an AppConfig CR equivalent, a fourth task-oriented MCP tool, a QuickSuite Space or Knowledge Base ingestion model, or a Skywalker-generated QuickSuite prose answer. It also does not move backend routing, reranking, or abstain judgment into QuickSuite. QuickSuite may decide whether to call Skywalker, but once it calls Skywalker it does not decide which retrieval arms run or whether the evidence is answerable.

### 4. Source-of-truth hierarchy

The global source-of-truth order for this section is fixed:

1. `IngestionCodeReference`.
2. Implemented code paths.
3. Architecture documents, including adopted API and integration contracts.
4. Design discussions.
5. Future proposals.

Within that global hierarchy, the QuickSuite-specific dependency order is:

1. **API_08**: [QuickSuite MCP Gateway Integration](done/API_08_QuickSuite_MCP_Gateway_Integration.md) is the grounding contract for the QuickSuite-to-MCP-Gateway path.
2. **BuilderHub MCP Gateway QuickSuite documentation**: [Integration with QuickSuite](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/howto-quicksuite-client/), [MCP Gateway concepts](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/concepts/), and [MCP Gateway User Guide](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/) define the supported transport, Federate-inbound route, and gateway behavior.
3. **Section 02 / API_01**: the Skywalker MCP tool contract defines the three core tools and the structured response envelope.
4. **Sections 04 through 07**: backend routing, FAQ/UKB retrieval behavior, reranking, evidence packaging, and abstain behavior are inherited backend facts.
5. **Section 08 and API_14**: Slack and UAT identity propagation use CloudAuth OBO plus TransitiveAuth where supported; that is intentionally not the QuickSuite launch posture because QuickSuite uses the Federate-inbound to CloudAuth-outbound gateway combination.
6. **API_12**: streaming applies above the MCP boundary on the Slack and UAT inline-agent paths; it does not change the QuickSuite MCP response contract.

If these sources conflict, the lower-numbered item in this list wins for the QuickSuite integration. Operational onboarding details can fill missing identifiers, such as the final MCP server ID, but they do not override the architecture-level contract.

### 5. Architecture facts and assumptions

The following are architecture facts for this section. They are not preferences.

QuickSuite calls Skywalker through **Amazon MCP Gateway** on the Federate-inbound route. The URL registered in QuickSuite has this shape:

`https://api.mcp.asbx.aws.dev/federate/mcp/{registry-id}/{skywalker-server-id}`

MCP Gateway validates the inbound Federate JWT, performs Bindle authorization against `MCPGateway::{skywalker-server-id}`, converts the authorized request to CloudAuth outbound, and forwards the request to Skywalker's MCP server. There is no AgentCore Gateway, no Skywalker-owned wrapper Lambda, no REQUEST interceptor Lambda, and no AppConfig CR equivalent on the supported QuickSuite path.

QuickSuite reads Skywalker's actual `tools/list` through MCP Gateway and invokes one of the three core tools:

- `skywalker.search.by_alias` with `{ query_text, alias, correlation_id? }`
- `skywalker.search.by_employee_id` with `{ query_text, employee_id, correlation_id? }`
- `skywalker.search.by_explicit_scope` with `{ query_text, employee_id, country, level, role, correlation_id? }`

There is no fourth QuickSuite-specific task tool, and no wrapper translates a single QuickSuite tool into the core Skywalker tools.

MCP Gateway does not propagate the originating Federate-authenticated end-user identity downstream to Skywalker on the Federate-inbound plus CloudAuth-outbound combination. The gateway's published delegated-identity patterns are not the launch mechanism for this path. Identity for retrieval scope is therefore carried in MCP tool arguments supplied by the QuickSuite integration, not read by Skywalker from Federate JWT claims, custom headers, or synthetic identity headers.

Skywalker returns the same structured backend envelope QuickSuite would receive as any other MCP client. The required fields include `result_kind`, `route`, `scope_snapshot`, `evidence[]`, `abstain_reason` when the backend abstains, and `correlation_id`. `result_kind` is either `ANSWERABLE` or `ABSTAIN`. `route` records the runtime path and which retrieval arms ran. `scope_snapshot` records the country, level, and role used for retrieval. `evidence[]` includes ranked candidates, titles, text, source URLs, policy links, arm-local rank, rerank score, and citation metadata when available.

The QuickSuite MCP boundary is synchronous. Skywalker returns one JSON-RPC result, MCP Gateway returns one HTTP response, and QuickSuite consumes the complete envelope before composing its own response. Skywalker does not stream partial MCP results to QuickSuite and does not run the inline agent on the QuickSuite path.

The assumptions below are not design decisions, but this section depends on them. Each assumption has an explicit consequence so the integration can fail in known places rather than silently changing architecture.

| Assumption | Consequence if false | Revisit target |
| --- | --- | --- |
| QuickSuite-authenticated users have already authenticated through Midway before Federate issues the access token used by the MCP connector. | Gateway admission may not represent the expected Amazon employee authentication posture, and the connector setup must be corrected before production launch. | QuickSuite connector configuration and Federate service profile onboarding. |
| The existing Skywalker scope tuple of country, level, and employee-class role is sufficient for the QuickSuite launch path. | Correctly authenticated users may still receive overbroad, underbroad, or abstained answers because scope lacks a needed dimension. | Section 02 tool schema and Sections 04-07 retrieval filters. |
| QuickSuite can select among tools exposed through `tools/list` and pass arguments from configured session or integration context. | QuickSuite cannot call the right Skywalker tool without either a client configuration change or an integration-local identity lookup. | QuickSuite chat-agent configuration and identity-carriage implementation. |
| QuickSuite prompting can require visible source titles and URLs when citing Skywalker evidence. | Users may see connector-level citations that hide the actual policy source, or answers that cite evidence without enough visible provenance. | QuickSuite prompt guidance first; Skywalker `_sources` extension only if prompt evidence fails. |
| Skywalker's normal latency remains far below QuickSuite's 299-second MCP connector timeout. | QuickSuite may surface an ambiguous timeout instead of a structured abstain or tool-execution failure. | Backend latency budget and fail-fast behavior before the connector timeout. |

### 6. Inputs, outputs, and contracts

QuickSuite's primary input to Skywalker is an MCP `tools/call` request sent through MCP Gateway with `Authorization: Bearer <federate-access-token>`. The method name must be one of the three Skywalker tools returned by `tools/list`. The arguments must include `query_text` and enough identity material for the selected tool. For `skywalker.search.by_alias`, QuickSuite supplies `alias`. For `skywalker.search.by_employee_id`, it supplies `employee_id`. For `skywalker.search.by_explicit_scope`, it supplies `employee_id`, `country`, `level`, and `role`. `correlation_id` is optional, but when supplied it must be propagated through the result so the client, gateway, and backend logs can be correlated.

Skywalker's normal output is a successful MCP tool result containing the structured backend envelope. The envelope is not final chat text. It is a backend artifact that QuickSuite must parse or pass to its chat-agent runtime for final answer composition. QuickSuite must preserve the semantic difference between an answerable result and an abstain result. It may change wording, but it must not convert `ABSTAIN` into a confident answer.

The contract between QuickSuite and MCP Gateway is that QuickSuite sends Federate-authenticated MCP requests to the registered Federate route. MCP Gateway is responsible for validating the Federate JWT, checking Bindle `canInvoke`, and forwarding authorized requests to Skywalker's CloudAuth-protected MCP server. A request rejected at this boundary is not a Skywalker retrieval failure because Skywalker never received it.

The contract between MCP Gateway and Skywalker is that the forwarded request body remains an MCP request using Skywalker's registered tool names and argument schema. Skywalker does not rely on gateway-propagated end-user identity for QuickSuite launch. Skywalker validates the tool name and arguments as it would for any MCP client.

The contract between Skywalker and QuickSuite is that Skywalker returns one complete synchronous result. If the backend can answer with sufficient scoped evidence, the result has `result_kind: "ANSWERABLE"` and evidence sufficient for citation. If the backend cannot answer with sufficient scoped evidence, the result has `result_kind: "ABSTAIN"` and `abstain_reason`. If execution fails before answerability can be evaluated, the result is an MCP error or an MCP tool result with `isError: true`, depending on the failure class defined by the tool contract.

The rendering contract is intentionally asymmetric. Skywalker provides evidence, source URLs, policy links, citation metadata when available, and abstain state. QuickSuite decides how to display them, but it is responsible for making cited sources visible enough that the user can understand where the answer came from. Because QuickSuite's current Sources UI groups citations under the MCP connector name, the launch posture is for the QuickSuite chat-agent prompt to include source titles and URLs in the visible answer text when it cites Skywalker evidence.

### 7. Fixed decisions

**Decision 1: QuickSuite consumes Skywalker through Amazon MCP Gateway.**  
The active architecture uses MCP Gateway on the Federate-inbound route. This is chosen because it is the supported QuickSuite-to-MCP path and because it lets Skywalker expose the same MCP server used by other clients. It binds connector onboarding, Federate service profile setup, Bindle authorization, and the absence of a Skywalker-owned wrapper. This decision reopens only if MCP Gateway cannot support the QuickSuite production connector requirement or if the platform replaces the supported QuickSuite MCP route.

**Decision 2: QuickSuite consumes the real Skywalker tool list.**  
QuickSuite sees and chooses among `skywalker.search.by_alias`, `skywalker.search.by_employee_id`, and `skywalker.search.by_explicit_scope` directly. This is chosen because tool selection belongs to the client surface and because a QuickSuite-only facade would hide the actual backend contract. It binds tool descriptions, prompt guidance, and client routing configuration. This decision reopens if production evidence shows that QuickSuite cannot reliably choose among the three tools after tool description and prompt calibration.

**Decision 3: Identity scope is supplied as MCP tool arguments at launch.**  
QuickSuite must put the user's alias, employee ID, or explicit `(employee_id, country, level, role)` scope into the request arguments before invoking Skywalker. This is chosen because the Federate-inbound plus CloudAuth-outbound gateway combination does not propagate originating end-user identity to Skywalker. It binds the launch identity-carriage work to QuickSuite configuration or an integration-local source, not to Skywalker header parsing. This decision reopens if MCP Gateway provides a supported delegated-identity pattern for this exact route and production integration evidence shows it is safer than argument-carried identity.

**Decision 4: QuickSuite owns conversation and final rendering.**  
Skywalker does not own QuickSuite prompt design, conversation memory, follow-up interpretation, session continuity, final wording, or chat citation UI. This is chosen because QuickSuite already provides the chat-agent runtime and because duplicating final-answer generation in Skywalker would create two user-facing agents. It binds Skywalker to returning structured backend artifacts rather than QuickSuite prose. This decision reopens only if QuickSuite's runtime cannot preserve answerability and citation obligations despite prompt and configuration changes.

**Decision 5: Skywalker returns the shared structured backend envelope.**  
The QuickSuite path receives the same envelope defined by the MCP contract, including `result_kind`, `route`, `scope_snapshot`, `evidence[]`, `abstain_reason` when applicable, and `correlation_id`. This is chosen because the backend result must remain comparable across clients and because evidence needs to be inspectable before QuickSuite renders it. It binds QuickSuite parsing and chat-agent consumption to the shared contract. This decision reopens if the MCP platform imposes a required response shape that cannot carry the existing envelope without loss.

**Decision 6: The QuickSuite MCP response is synchronous.**  
Skywalker returns one complete JSON-RPC result through MCP Gateway. This is chosen because the QuickSuite MCP connector consumes complete tool results and because the backend envelope is meaningful only as a complete evidence and abstain object. It binds the QuickSuite path away from inline-agent streaming. This decision reopens if the supported QuickSuite MCP transport adds a streaming contract that can preserve the full backend envelope and failure semantics.

**Decision 7: Backend answerability semantics are shared.**  
QuickSuite receives the same `ANSWERABLE`, `ABSTAIN`, and execution-error classes as other clients. This is chosen because answerability depends on retrieval evidence and scope, not on the client surface. It binds QuickSuite rendering to preserve abstention rather than treating it as a system failure or smoothing it into an answer. This decision reopens only if post-launch evidence shows a QuickSuite-specific user interaction pattern that requires an additional client-level explanation while preserving the backend `ABSTAIN` state.

**Decision 8: Federate OAuth and Bindle gate invocation.**  
Inbound auth uses Federate OAuth Authorization Code flow with PKCE against a Federate Prod Service Profile for the QuickSuite MCP connector, and QuickSuite access is granted through `canInvoke` on the MCP Gateway Bindle resource for the Skywalker server. This is chosen because it is the MCP Gateway admission model for the QuickSuite route. It binds onboarding to Federate and Bindle rather than service-IAM shortcuts. This decision reopens only if the MCP Gateway QuickSuite onboarding model changes.

**Decision 9: No QuickSuite-specific PAPI trust path exists.**  
If QuickSuite calls the alias tool, Skywalker resolves scope through its normal PAPI path. If QuickSuite calls the explicit-scope tool, Skywalker validates and uses the supplied scope per Section 02. This is chosen because correctness depends on the same scope construction rules across clients. It binds QuickSuite away from hidden identity headers or privileged scope overrides. This decision reopens only if Section 02 changes the global identity contract for all MCP clients.

**Decision 10: Source rendering starts as a QuickSuite chat-agent responsibility.**  
QuickSuite must render source titles and URLs visibly when it cites Skywalker evidence. This is chosen because QuickSuite owns the final response surface and because its current Sources UI groups citations under the MCP connector name. It binds the launch posture to prompt and rendering guidance rather than a Skywalker-specific response extension. This decision reopens if launch evidence shows that prompting cannot reliably preserve source visibility; the fallback under consideration is a Skywalker-side `_sources` extension.

### 8. Alternatives considered

A QuickSuite-only service face is rejected. It is attractive because it could hide tool selection and identity-carriage details from QuickSuite configuration. It is rejected because it would fork the backend contract, create a privileged hidden path, and violate the tenet that one shared MCP contract wins over client convenience.

Registering Skywalker as a QuickSuite Space or Knowledge Base is rejected. It is attractive because it matches an existing QuickSuite knowledge consumption pattern. It is rejected because Skywalker answers depend on employee-specific scope, backend abstention, and retrieval-time routing. A static-document model would collapse identity-aware retrieval into content ingestion and would make answer correctness depend on QuickSuite knowledge behavior rather than Skywalker's backend contract.

A Skywalker-owned Bedrock AgentCore Gateway wrapper is rejected. It is attractive because a wrapper could normalize identity and response formatting in one place. It is rejected because MCP Gateway is the supported QuickSuite-to-MCP path, and because adding a wrapper would introduce another deployment surface without changing Skywalker's actual retrieval obligations.

A service-IAM identity for all QuickSuite calls is rejected. It is attractive because service identity is simpler operationally than user-carried scope. It is rejected because the retrieval scope is user-specific. Treating all QuickSuite calls as a single service principal would make scoped answers depend on client-side convention rather than backend validation.

Skywalker-generated QuickSuite prose is rejected. It is attractive because it could enforce citation and abstain wording centrally. It is rejected because QuickSuite already owns the chat-agent runtime, memory, and final answer composition. Skywalker-generated prose would create two conversational layers and would make QuickSuite's agent less accountable for how it renders tool results.

Streaming the MCP envelope is rejected for the QuickSuite path. It is attractive because users may prefer progressive UI feedback. It is rejected because the QuickSuite MCP boundary is synchronous, the result envelope must be parsed as a complete object, and partial evidence before answerability is decided can mislead the client. Streaming above the MCP boundary remains a QuickSuite product concern.

Using gateway-propagated delegated identity at launch is rejected. It is attractive because it would avoid passing identity material in tool arguments. It is rejected for the launch path because the supported Federate-inbound plus CloudAuth-outbound combination does not propagate the originating end-user identity to Skywalker. If that platform capability changes, it belongs in calibration and then in a reopened identity decision.

### 9. End-to-end flow

A user begins in a QuickSuite chat agent. QuickSuite evaluates the turn using its own intent routing and decides whether Skywalker is the appropriate MCP connector. If the turn is not a Skywalker-scoped employee-policy question, QuickSuite should not call Skywalker.

When QuickSuite decides to call Skywalker, it reads or uses the tool list exposed through MCP Gateway. It chooses `skywalker.search.by_alias` when it has a reliable alias, `skywalker.search.by_employee_id` when employee ID is the available identity input, or `skywalker.search.by_explicit_scope` when it has the full scope tuple. QuickSuite sets `query_text` from the current user turn and supplies the identity fields required by the selected tool. If it has a correlation ID, it includes it; otherwise Skywalker can continue the request with backend-generated correlation.

QuickSuite sends an MCP `tools/call` request to the MCP Gateway Federate route with `Authorization: Bearer <federate-access-token>`. MCP Gateway validates the Federate JWT. Invalid, expired, or incorrectly scoped tokens are rejected before Skywalker receives the request. Gateway then checks Bindle `canInvoke` permission for the Skywalker MCP server resource. Authorization failure is also rejected before Skywalker receives the request.

For an authorized request, MCP Gateway forwards the MCP body to Skywalker's CloudAuth-protected MCP server. The request body is not reshaped by a wrapper or interceptor. Skywalker validates the tool name and argument schema. For alias-shaped calls, Skywalker resolves scope through the normal PAPI path. For explicit-scope calls, Skywalker validates and uses the supplied scope tuple. There is no QuickSuite-only identity shortcut.

Skywalker then executes the same backend behavior described in upstream sections: route selection, FAQ/UKB retrieval behavior, hybrid retrieval where applicable, reranking, evidence packaging, citation metadata construction, and answerability judgment. QuickSuite does not influence which retrieval arms run after the backend request is constructed.

Skywalker returns one structured answerable or abstain envelope as a synchronous JSON-RPC result. MCP Gateway returns that result to QuickSuite. QuickSuite's chat-agent runtime composes and renders the user-facing answer, including source titles and URLs when it cites Skywalker evidence. Any multi-turn continuation remains in QuickSuite until QuickSuite decides another Skywalker tool call is needed.

The invariant is that QuickSuite-specific behavior exists at the edges. Tool selection happens before the call. Answer composition happens after the result. Retrieval behavior in the middle remains the shared Skywalker backend.

### 10. Failure and abstain behavior

QuickSuite routing failure is a client orchestration failure. If the chat agent does not call Skywalker when it should, or calls Skywalker for a turn outside Skywalker's scope, the failure belongs to QuickSuite tool routing or prompt configuration. Skywalker may return a valid abstain for an out-of-scope request, but that does not prove the client made the right routing decision.

Federate JWT validation failure is a gateway protocol failure. MCP Gateway rejects the request before it reaches Skywalker. The client-visible behavior is a connector invocation failure, not a Skywalker `ABSTAIN`, because no retrieval was attempted.

Bindle authorization failure is also a gateway protocol failure. MCP Gateway rejects the request when the Federate principal is not authorized to invoke the Skywalker MCP server resource. The remediation is connector authorization, not backend retrieval tuning.

Malformed tool arguments are MCP contract failures. Skywalker rejects unknown tools, missing required arguments, invalid argument types, or invalid explicit-scope values according to the MCP tool contract. These failures should surface as JSON-RPC errors or tool errors rather than answerability decisions, because the backend did not receive a valid retrieval request.

PAPI failure on the alias path is a tool-execution failure. Skywalker cannot construct the scoped retrieval request when alias-to-scope resolution fails after configured retry behavior. The response should be a normal MCP tool result with `isError: true` as defined by the contract, not an `ABSTAIN`, because abstention is reserved for valid retrieval requests with insufficient evidence.

Backend abstention is not an error. `result_kind: "ABSTAIN"` is a successful structured result saying that Skywalker could not answer with sufficient evidence for the resolved scope. QuickSuite must render this as an evidence limitation, not as a connector crash and not as a confident answer. The visible answer should preserve the user's ability to distinguish "the system failed" from "the system looked and should not answer."

QuickSuite citation loss is a rendering failure. If Skywalker returns evidence with source titles and URLs but QuickSuite hides or collapses them under a generic connector label, the backend contract succeeded and the client consumption guidance failed. The first remediation is QuickSuite prompt and rendering calibration. A Skywalker-side `_sources` extension is a fallback only after launch evidence shows that prompt-based rendering is insufficient.

The QuickSuite MCP connector's 299-second timeout is a hard upper bound. It is not expected to bind under the current latency budget. If future backend changes approach that limit, Skywalker should fail cleanly before QuickSuite's timeout produces an ambiguous user-visible result.

### 11. Calibration surfaces

Identity carriage is calibration-active within a fixed architecture boundary. The architecture fixes that Skywalker receives identity scope through MCP tool arguments at launch. The exact QuickSuite-side mechanism remains open to implementation evidence: session alias, QuickSuite-side lookup, prompted scope, custom claims, or another integration-local source. The chosen mechanism must be judged by whether it reliably supplies the right scope without asking Skywalker to infer identity from unsupported gateway state.

Tool selection quality is calibration-active. QuickSuite must choose between the alias, employee-ID, and explicit-scope tools based on the identity material it has. Early testing should look for wrong-tool selection, missing required arguments, and cases where the chat agent chooses Skywalker for non-Skywalker tasks. Production review should use connector invocation logs and sampled transcripts rather than anecdotal demonstrations.

Tool description quality is calibration-active because QuickSuite reads Skywalker's actual `tools/list`. If the descriptions are too broad, QuickSuite may over-call Skywalker. If they are too narrow, QuickSuite may miss valid employee-policy questions. Description changes are allowed, but they must not introduce a fourth tool or change the backend contract without reopening the relevant fixed decision.

Citation rendering is calibration-active. The launch posture relies on QuickSuite prompt guidance to place source titles and URLs in visible answer text. The success standard is not whether a small test set looks acceptable; it is whether real usage shows cited answers remain traceable when reviewed across many users and policy topics.

Abstain rendering is calibration-active. QuickSuite must preserve `ABSTAIN` as an evidence limitation. The calibration question is the exact user-facing wording, not whether the backend should abstain. If QuickSuite repeatedly converts abstain results into generic connector errors or confident prose, the client consumption guidance must change before the backend answerability threshold is blamed.

Future delegated identity support is calibration-active only after the platform changes. If MCP Gateway later supports delegated identity for the Federate-inbound plus CloudAuth-outbound combination, the team can compare argument-carried identity against gateway-propagated identity. Until then, delegated identity is not a launch dependency.

### 12. Open questions and evidence standard

The open questions in this section are integration and calibration questions, not permission to change the backend architecture silently. Pre-launch testing can answer whether the connector works at all. It cannot prove the user-facing behavior is correct at scale. Decisions about systematic citation quality, abstain interpretation, and tool-routing accuracy require post-launch evidence across hundreds of users or an equivalently representative production sample. Small-sample demos can identify defects, but they should not be used to claim that the integration is calibrated.

1. Which QuickSuite-side identity-carriage mechanism will launch: session alias, QuickSuite-side lookup, prompted scope, custom claims, or another integration-local source?
2. What exact Skywalker MCP server identifier will be assigned by MCP Gateway onboarding?
3. Does QuickSuite reliably choose `skywalker.search.by_alias` when alias is available and `skywalker.search.by_explicit_scope` only when the full scope tuple is available?
4. Does the QuickSuite chat-agent prompt reliably include source titles and URLs in visible answer text when citing Skywalker evidence?
5. Does QuickSuite preserve `ABSTAIN` as an evidence limitation rather than rendering it as a system failure or smoothing it into an unsupported answer?
6. Does QuickSuite preserve policy links and citation metadata across multi-turn follow-ups, or does it detach later answers from the original evidence?
7. Should QuickSuite move to a future MCP Gateway delegated-identity pattern if one becomes available for Federate-inbound plus CloudAuth-outbound?

The evidence standard for questions 3 through 6 is post-launch review at hundreds-users scale or a representative production sample with real employee-policy questions, real identity material, and real citation rendering. The review should distinguish client routing failures, gateway failures, tool-contract failures, backend abstentions, and rendering failures. Without that separation, calibration will push on the wrong layer.

### 13. Closing position

QuickSuite is the cleanest client expression of the Skywalker architecture because QuickSuite already owns the agent surface. Skywalker's responsibility on this path is to expose the shared MCP contract through MCP Gateway, require retrieval identity in supported tool arguments at launch, perform the same scoped retrieval as every other client, and return the same structured synchronous backend envelope.

If this boundary holds, Skywalker remains one retrieval backend serving multiple client surfaces through one MCP contract. If QuickSuite-specific routing, reranking, identity trust, or final-answer generation leaks into Skywalker, the architecture has started to fork and the integration should be treated as a design regression rather than a client customization.

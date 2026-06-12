## Section 08. Slack Integration and Client Boundary

### 1. Tenets

We prefer backend truth over conversational fluency when they conflict. If Skywalker returns an abstain or failure-shaped outcome, Slack must preserve that outcome even when the Slack-side model can produce plausible prose.

We prefer verified identity over client-supplied convenience when they conflict. On the Slack CloudAuth plus TransitiveAuth path, the validated TA human alias is canonical; `arguments.alias` exists for MCP schema compatibility, not as the authority when TA succeeds.

We prefer Slack-owned conversation state over backend session memory when they conflict. Slack and the inline agent may use bounded conversation context to interpret the current turn, but Skywalker receives one scoped retrieval request at a time and does not own Slack thread state.

We prefer one shared Skywalker MCP contract over Slack-specific backend shortcuts when they conflict. Slack may have a richer client orchestration layer than QuickSuite, but it must still call the same Skywalker MCP server, use the same structured envelope, and respect the same answerable/abstain/failure split.

We prefer visible user-facing uncertainty over silent recovery when they conflict. Missing alias, invalid TA, gateway authorization failure, weak evidence, and model ambiguity must become distinct Slack outcomes rather than being repaired by guessing, widening scope, or answering from memory.

### 2. Problem and Architectural Intent

Slack is the first workplace chat surface where Skywalker has to feel like a product rather than a retrieval API. Users will ask questions through slash commands, direct messages, and channel mentions. Their questions will often be informal, abbreviated, and dependent on the local Slack thread. The architecture must let Slack interpret that conversational turn without letting Slack become a second retrieval backend or letting Skywalker become a Slack conversation manager.

The intent is therefore a deliberately split system. The Slack application owns Slack event intake, Slack identity resolution, bounded conversation context, Bedrock Inline Agent orchestration, return-control handling, Slack rendering, and the final user-visible distinction among grounded answer, abstain, and system failure. Skywalker owns scoped retrieval behind MCP: admission, identity/scope interpretation once the MCP call reaches the backend, PAPI-based scope resolution for the alias path, routing, evidence selection, reranking, structured response assembly, and abstain judgment.

This split matters because the most dangerous Slack failure is not a bad chat experience; it is a fluent answer that appears tailored to an employee but is grounded in the wrong scope or no defensible evidence. Slack is allowed to make the experience conversational. It is not allowed to make the evidence broader, the identity weaker, or the abstain less visible.

QuickSuite is the contrast case. QuickSuite brings its own chat-agent runtime and enters Skywalker through Amazon MCP Gateway on the Federate-inbound route. Slack does not bring that runtime, so the Skywalker Slack team must build the Slack bot, model layer, return-control loop, and Slack message surface. The clients share the Skywalker MCP server and Amazon MCP Gateway. They differ in inbound auth, identity propagation, and user-facing rendering.

### 3. Boundary and Non-Goals

The Slack boundary begins when Slack delivers a slash command, direct message event, or app mention to the Slack application. It ends when the Slack application writes the final Slack message or failure message. Inside that boundary live Slack signing verification or Socket Mode intake, event normalization, Slack-user-to-Amazon-alias resolution, inline-agent session selection, Bedrock Inline Agent invocation, return-control handling, MCP Gateway invocation, result reinjection into the inline agent, streaming buffer management, and Block Kit rendering.

The Skywalker boundary begins only when the Slack application calls `skywalker.search.by_alias` through Amazon MCP Gateway. Skywalker is not passed raw Slack payloads, Slack channel IDs as retrieval state, Slack thread history as backend memory, or Slack-ready prose instructions. Skywalker receives a single MCP request and returns a structured MCP result.

The boundary excludes retrieval correctness, PAPI behavior, controlled FAQ ingestion, UKB integration, route scoring, reranker thresholds, and backend abstain rules. Those are owned by Sections 02, 03, 04, 06, and 07. The boundary also excludes QuickSuite's runtime behavior and any permanent web-client strategy. Slack must not bypass Skywalker to query the controlled FAQ corpus directly, query UKB directly, synthesize `country`, `level`, or `role`, reimplement backend thresholding, or use prior Slack conversation state as a substitute for backend scope.

This section also does not define a new evaluation platform, a runtime human-review workflow, or a Slack-side authority to call explicit-scope search at launch. The employee-ID and explicit-scope MCP tools remain part of the broader Skywalker contract for other clients, but they are not Slack launch paths.

### 4. Source-of-Truth Hierarchy

When Slack-specific behavior conflicts across documents, use this order:

1. `IngestionCodeReference`.
2. Implemented code paths.
3. Architecture documents, including adopted API and integration contracts.
4. Design discussions.
5. Future proposals.

Within architecture documents, this section depends on API_01 and Section 02 for the MCP contract, API_14 for CloudAuth OBO and TransitiveAuth behavior, API_12 for Bedrock Inline Agent streaming and return-control behavior, and API_02 for Slack platform facts.

The hierarchy is intentionally asymmetric. If the Slack model can infer an answer but the Skywalker envelope says `ABSTAIN`, the envelope wins. If the Slack payload, model arguments, and TA claims disagree about the human alias, the validated TA claim wins on this path. If an implementation eventually differs from this prose, the implementation is the active runtime truth, and this section must be corrected rather than defended.

### 5. Facts, Assumptions, and Consequences

The launch facts are concrete. Slack supports three user entry surfaces: slash commands such as `/skywalker`, direct messages through `message.im` events from non-bot users, and channel mentions through `app_mention` events. The application normalizes all three into one internal turn shape before invoking the model. Output rendering may vary by Slack surface, but retrieval orchestration does not fork into three client products.

Slack requires fast acknowledgement for slash commands and Events API deliveries. In HTTP mode, request trust depends on verifying `X-Slack-Signature` with the signing secret and timestamp over the raw request body before the body is parsed. In Socket Mode, the app receives events over Slack's outbound WebSocket connection and uses the app-level token for that connection. The current framework direction is Bolt for Java, using `com.slack.api:bolt` with either `bolt-servlet` for HTTP mode or `bolt-socket-mode` for Socket Mode.

The Slack secret and scope facts are also fixed enough to build against. The secret set includes `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` for HTTP mode, and `SLACK_APP_TOKEN` for Socket Mode if that topology is selected. The OAuth scope set includes command receipt, app mention receipt, direct-message read/write capability, message posting, and user profile lookup sufficient to resolve Slack user identity to an Amazon alias.

The Slack application consumes Skywalker exclusively through Amazon MCP Gateway on the CloudAuth route:

```text
https://api.mcp.asbx.aws.dev/ca/mcp/{registry-id}/{skywalker-server-id}
```

The prior SigV4 inbound path at `/mcp/{registry-id}/{skywalker-server-id}` is not the Slack architecture. CloudAuth inbound with CloudAuth OBO and TA support is the current path captured by API_14. The Slack orchestrator is expected to be registered as a CloudAuth-modeled AAA application, such as `SkywalkerSlackOrchestrator` once onboarding finalizes. MCP Gateway invokes Skywalker on behalf of that orchestrator application identity, so Skywalker evaluates the orchestrator principal rather than the gateway principal.

TransitiveAuth carries the human end-user identity. The Slack orchestrator resolves the Slack user to an Amazon alias, initiates a TA token for that human plus an `AgenticContext`, and sends the token with the MCP call. Skywalker validates the TA token server-side and reads the human alias from TA claims. On the Slack path, TA is canonical. `arguments.alias` remains in the MCP request because the shared tool schema requires it for non-TA clients, but it is not the authority when TA is valid.

The Slack-side conversational model is Claude Sonnet 4.6. The exact model ID and regional availability remain open, but the model role is fixed: interpret the normalized Slack turn, decide whether to call Skywalker, and compose Slack-visible wording only from the structured backend result. The application invokes the model through Bedrock Inline Agents using `InvokeInlineAgent`, not the Converse API. The action group uses `RETURN_CONTROL` so Bedrock returns the retrieval request to the Slack application; the application performs the real MCP `tools/call` and feeds the structured result back through `inlineSessionState.returnControlInvocationResults`.

The MCP response is synchronous JSON-RPC. Streaming exists above the MCP boundary, between the inline agent and Slack. Every Slack-path `InvokeInlineAgent` call sets `streamingConfigurations: { streamFinalResponse: true }`. Final-response text streams as `chunk` events. `returnControl` is a discrete event, and the MCP call is a synchronous JSON-RPC request/response.

This section assumes Slack can resolve every participating Slack user to a stable Amazon alias before invoking Skywalker. If that assumption proves false, the consequence is not fallback to anonymous or unscoped retrieval; the Slack application must fail explicitly for that user until the resolver is corrected. Reopening the posture requires an alternate authoritative human identity source with documented coverage, freshness, and failure semantics.

This section assumes Slack does not have authoritative `country`, `level`, or `role` at launch. The consequence is that Slack uses `skywalker.search.by_alias`, and Skywalker resolves scope through PAPI. If Slack later gains a reliable source of the full scope tuple, the explicit-scope launch exclusion can be revisited, but only with evidence that the source is authoritative and conflict behavior is defined.

This section assumes the gateway, CloudAuth OBO, and TA setup described by API_14 are available to the Slack orchestrator before launch. If the setup is unavailable, the consequence is a launch-blocking integration failure, not a temporary downgrade to `arguments.alias` as canonical identity.

This section assumes the launch prompt can be treated as subsystem configuration while its wording is calibrated. If production behavior shows the model suppresses abstains, loses citations, or turns failures into generic apologies, the prompt changes; the backend contract does not.

### 6. Inputs, Outputs, and Contracts

The Slack turn input is the normalized object produced after Slack event verification and user resolution. It contains the raw user utterance, Slack surface type, Slack user ID, resolved Amazon alias, channel or thread reply target, response URL when present, and a bounded Slack-side conversation excerpt. Raw Slack payloads do not cross into Skywalker.

The prompt input to the inline agent includes the normalized turn, bounded conversation excerpt, identity context, tool description, stable behavioral instruction, and any returned Skywalker result. The prompt contract must teach four invariants: Skywalker is the source of scoped retrieval truth; backend abstention must be preserved; citations and policy links returned by Skywalker remain attached to answer claims; and transport, authorization, or backend failure is not permission to answer from model memory.

The Slack action-group contract exposes one launch function: `skywalker_search_by_alias`. It maps to MCP tool `skywalker.search.by_alias` and takes `query_text` and `alias`. Slack does not put explicit-scope search on the launch action group because Slack does not hold authoritative `country`, `level`, or `role`.

The MCP request contract is a JSON-RPC `tools/call` sent through Amazon MCP Gateway to the CloudAuth route. The request carries `arguments: { query_text, alias }`, CloudAuth credentials from the orchestrator AAA application, and a TA token carrying the human alias. Skywalker validates the request and TA token, treats the TA alias as canonical on this path, resolves scope through PAPI, and returns the shared structured envelope.

The MCP response contract has three meaningful backend outcome classes:

- `ANSWERABLE`: a valid scoped request reached retrieval, and evidence survived backend ranking and answerability judgment.
- `ABSTAIN`: a valid scoped request reached retrieval, but the backend judged the evidence insufficient for a defensible answer.
- System or tool failure: the call could not produce a trustworthy backend result because admission, identity, dependency, gateway, transport, or execution failed.

The Slack reply contract maps those backend outcomes into three user-visible shapes. A grounded answer is composed from the answerable evidence package with traceable citations. An abstain reply preserves Skywalker's non-answer judgment and avoids unsupported policy content. A system-failure reply says the integration could not obtain a trustworthy backend result. Slack owns whether the final message is ephemeral, in-channel, a direct message, or a threaded reply. Skywalker does not know or control that rendering.

### 7. Fixed Decisions

**Decision 1: Slack is a real client surface, not a thin transport.**  
The rationale is that Slack must normalize events, resolve user identity, manage thread context, run the inline agent, handle return control, and render Slack-specific messages. Treating Slack as a pass-through would push conversational ambiguity into Skywalker, which violates the backend boundary. This binds the Slack application architecture, prompt contract, streaming renderer, and failure taxonomy. Reopen only if another approved Slack runtime supplies the same normalized turn, return-control, identity, and rendering responsibilities.

**Decision 2: Slack launch supports slash commands, direct messages, and channel mentions through one normalized turn contract.**  
The rationale is that these are different Slack entry mechanics, not different retrieval products. A shared turn contract keeps model orchestration and MCP invocation consistent while allowing surface-specific rendering. This binds event handlers, prompt inputs, session selection, and metrics labels. Reopen only if production traffic across hundreds of users shows one surface needs materially different orchestration rather than different formatting.

**Decision 3: Slack invokes Skywalker only through Amazon MCP Gateway on `/ca/mcp/{registry-id}/{skywalker-server-id}`.**  
The rationale is that API_14 establishes CloudAuth inbound as the Slack path, with CloudAuth OBO and TA support. Direct backend calls or the prior SigV4 inbound path would bypass the intended authorization and identity propagation model. This binds onboarding, endpoint configuration, AAA application registration, Bindle invocation permission, and error handling. Reopen only if MCP Gateway deprecates this auth combination or publishes a replacement path with equivalent service and human identity propagation.

**Decision 4: CloudAuth OBO supplies the service identity and TA supplies the human identity.**  
The rationale is that the Slack orchestrator and the human invoker are different principals. CloudAuth OBO lets Skywalker evaluate the orchestrator application rather than MCP Gateway. TA lets Skywalker read the human alias from a validated token rather than trusting a model-supplied argument. This binds identity-source metadata, PAPI lookup input, auditability, and fail-closed behavior. Reopen only if TA cannot meet request isolation and reliability requirements in the deployed Java framework, or if a reviewed replacement delegated-human-identity channel exists.

**Decision 5: Slack launch uses `skywalker.search.by_alias`, not employee-ID or explicit-scope search.**  
The rationale is that Slack can resolve a human alias but does not hold authoritative launch scope. Alias search lets Skywalker use PAPI for `country`, `level`, and `role`, preserving the Section 02 scope correctness invariant. This binds the action-group function, tool description, prompt examples, and client-side validation. Reopen only if Slack gains an authoritative scope source and conflict semantics are documented.

**Decision 6: Missing or invalid TA on the Slack path fails closed.**  
The rationale is that accepting `arguments.alias` when TA is expected would erase the main benefit of the TA migration and allow the model or orchestrator payload to become the effective identity authority. This binds rollout readiness, integration tests, user-visible failure state, and support triage. Reopen only with explicit architecture approval for a degraded mode and evidence that it cannot broaden or mis-scope retrieval.

**Decision 7: Slack uses Bedrock Inline Agents with `RETURN_CONTROL`, not a direct model call that invokes MCP itself.**  
The rationale is that return control keeps the real MCP call in the Slack application, where CloudAuth credentials, TA initiation, gateway error handling, and result reinjection are owned. A model-mediated tool call would blur credential handling and make the integration harder to audit. This binds the prompt shape, action-group schema, two-turn invocation, and streaming consumer. Reopen only if Bedrock Inline Agents cannot support the latency or tool-control needs and an alternate orchestrator preserves the same control boundary.

**Decision 8: The Skywalker MCP boundary remains synchronous and structured; only final-response text streams above it.**  
The rationale is that Skywalker returns evidence, metadata, route state, and abstain reasons as one JSON object. Streaming partial JSON to the agent would not let the agent act earlier and would complicate a clean contract. Streaming the inline agent's final response improves perceived latency where text is actually generated. This binds API_12, Slack `chat.update` buffering, and MCP test expectations. Reopen only if the MCP protocol and gateway path add a useful structured streaming primitive and a consumer can use partial evidence safely.

**Decision 9: Slack renders answer, abstain, and system failure as structurally distinct outcomes.**  
The rationale is that users need to know whether Skywalker found grounded evidence, intentionally declined to answer, or could not complete the integration path. Collapsing those states into one apology would hide backend quality and make support triage worse. This binds Block Kit templates, prompt instruction, final-state metrics, and incident analysis. Reopen only if production evidence shows users understand a simpler presentation without confusing outage, abstain, and refusal.

**Decision 10: Slack does not ask Skywalker to maintain conversation state.**  
The rationale is that Slack context is client-specific and mutable, while Skywalker retrieval should be reproducible from a scoped request. Conversation history can help Slack formulate the current retrieval question; it must not become hidden backend memory. This binds session IDs, context-window handling, MCP request shape, and follow-up behavior. Reopen only if Skywalker adds a separately reviewed session contract with retention, replay, and correctness semantics.

### 8. Alternatives Considered

A thin Slack transport that sends raw Slack text directly to Skywalker is attractive because it reduces client code. It is rejected because Skywalker would have to interpret Slack threads, ask clarifications, render Slack prose, and own conversation state. That violates the boundary and weakens reproducibility.

A Slack-only backend endpoint is attractive because it could hide MCP and gateway details from the Slack team. It is rejected because it would fork the Skywalker contract and create a second place where identity, scope, abstain, and evidence behavior could drift.

Letting Slack call `skywalker.search.by_explicit_scope` is attractive because it would avoid a PAPI lookup after alias resolution. It is rejected for launch because Slack does not hold authoritative `country`, `level`, or `role`. Supplying guessed or derived scope would be worse than the latency cost of PAPI.

Trusting `arguments.alias` on Slack when TA is missing is attractive as an availability fallback. It is rejected because it turns a configuration or identity-propagation failure into a silent trust downgrade. The user experience is better served by a visible system failure than by a plausible answer for an unverified identity.

Using the Converse API with application-side function calling is attractive because it is a familiar LLM integration pattern. It is rejected because Bedrock Inline Agents with `RETURN_CONTROL` gives a cleaner control boundary for the MCP call while preserving inline-agent session behavior and final-response streaming.

Streaming the MCP response is attractive as a superficial consistency move because final Slack text streams. It is rejected because the MCP response is structured evidence, not generated text. The agent cannot safely use a half envelope, and the gateway path returns one sync JSON-RPC response.

Rendering backend abstain as a generic model refusal is attractive because it is simple. It is rejected because abstain is a successful backend outcome with different meaning from policy refusal, outage, or tool failure. The Slack UI must preserve that difference.

### 9. End-to-End Flow

Step one: Slack delivers a slash command, direct message event, or channel mention. In HTTP mode, the Slack application verifies the signing timestamp and signature over the raw request body before parsing. In Socket Mode, the application receives the event over the app WebSocket. The app acknowledges Slack within the platform deadline and creates or updates a placeholder message for longer work.

Step two: the Slack application normalizes the event into the shared turn object. Normalization records the surface type, user utterance, Slack user identity, reply target, response URL when present, thread context, and bounded conversation excerpt. The application resolves the Slack user to an Amazon alias. If alias resolution fails, the flow stops with a Slack integration failure and does not call Bedrock, MCP Gateway, or Skywalker.

Step three: the Slack application invokes the Bedrock Inline Agent with Claude Sonnet 4.6, the normalized turn, stable behavioral instruction, the action group containing `skywalker_search_by_alias`, a session ID derived from Slack context, and `streamingConfigurations.streamFinalResponse: true`. The model may ask a clarification question directly if the current turn cannot be responsibly converted into a retrieval request. That clarification streams back as chunks and no MCP call occurs.

Step four: when retrieval is needed, Bedrock emits a discrete `returnControl` event. The Slack application reads the requested `skywalker_search_by_alias` function and arguments, initiates a TA token for the resolved human alias plus `AgenticContext`, and builds the MCP `tools/call` request for `skywalker.search.by_alias`.

Step five: the Slack application sends the MCP request to Amazon MCP Gateway at the CloudAuth route with orchestrator CloudAuth credentials and the TA token. MCP Gateway validates CloudAuth, applies Bindle authorization for `canInvoke` on `MCPGateway::{skywalker-server-id}`, forwards to Skywalker using CloudAuth OBO, and preserves TA.

Step six: Skywalker validates the MCP request, validates TA, reads the human alias from TA claims, resolves scope through PAPI, performs routing, retrieval, evidence selection, reranking, and answerability judgment, then returns one synchronous JSON-RPC result. That result is structured as answerable, abstain, or failure-shaped output; it is not Slack-ready prose.

Step seven: the Slack application feeds the structured result back into the inline agent through `inlineSessionState.returnControlInvocationResults`. The second inline-agent turn streams the final grounded answer, abstain wording, or system-failure wording as `chunk` events. The behavioral prompt prevents the model from replacing backend evidence limits with unsupported policy content.

Step eight: the Slack application buffers chunks and updates Slack at the configured cadence. At launch, it posts an initial placeholder, captures the message `ts`, calls `chat.update` no more often than every 2.5 seconds while chunks arrive, and writes one final Block Kit message when generation completes. Slash commands are ephemeral by default and use `in_channel` only when the user explicitly asks for a public answer. Direct messages update or post in the IM channel. Channel mentions reply in thread using `thread_ts`.

### 10. Failure and Abstain Behavior

Slack intake failure is not backend retrieval failure. Invalid signatures, malformed Slack payloads, unsupported event types, missing reply targets, and acknowledgement failures are Slack integration failures. They do not imply anything about Skywalker's answerability.

Missing alias is not recoverable on the launch Slack path. The Slack application fails explicitly because a scoped backend request cannot be formed. It must not send an empty alias, reuse a prior alias from conversation state, or ask Skywalker to infer identity.

Missing or invalid TA is not recoverable at launch. The Slack path is expected to carry TA, and Skywalker treats a missing or malformed TA token as a hard failure on that path. The Slack response is a system-failure state, not an abstain and not a fallback answer.

CloudAuth authorization failure, AAA permission failure, Bindle `canInvoke` failure, TA validation failure, gateway 5xx, timeout, MCP transport failure, and Bedrock stream failure all produce system-failure states. The model cannot answer from memory in those cases, and any partial streamed text is replaced by a final failure message.

PAPI unresolved identity or incomplete scope after TA validation is also a system/tool failure from the Slack user's perspective. It means Skywalker could not form a trustworthy scoped request. Slack should make the failure visible without implying the backend searched and found no answer.

Backend `ABSTAIN` is not a system failure. It means identity and scope were valid, retrieval completed, and Skywalker judged the available evidence insufficient. Slack must render abstention as a grounded non-answer with a shape distinct from outage, permission failure, or generic refusal.

Multi-turn ambiguity remains a Slack-side responsibility. If the model cannot responsibly turn a follow-up into a current retrieval question, it asks a clarification rather than forcing Skywalker to retrieve against guessed intent. If the model already invoked Skywalker and the backend abstained, the model must preserve the abstain.

Weak, missing, or incomplete evidence remains weak, missing, or incomplete in Slack. The Slack layer may explain what Skywalker could not substantiate. It cannot repair source truth with unsupported model content, omit citations to make an answer seem cleaner, or cite Slack conversation text as if it were policy evidence.

### 11. Calibration Surfaces

The Slack prompt is calibration-active. The ownership boundary, use of Skywalker, and preservation of abstain/failure are fixed. The wording can change if production review shows the model overstates weak evidence, hides abstain, drops citations, mishandles tool failure, or asks unnecessary clarifications.

The bounded Slack conversation window is calibration-active. Conversation history stays in Slack, but the number of prior turns, thread messages, or excerpting rules may change if follow-ups are misread, stale context contaminates current questions, or latency increases without interpretation benefit.

Surface-specific rendering is calibration-active. Slash commands, direct messages, and channel mentions share orchestration, but their brevity, source placement, public/private defaults, and thread behavior may diverge based on observed usage and user confusion.

Abstain presentation is calibration-active. The backend abstain decision is fixed; Slack wording and visual treatment may change if users confuse abstain with outage, refusal, or an empty result.

The streaming update cadence is calibration-active. The launch cadence is 2.5 seconds. It can move through configuration, such as SSM, if Slack rate limits, visual flicker, or perceived latency require adjustment. A tighter cadence must not risk Slack API throttling for high-use channels.

The grounded-completeness versus conversational-brevity balance is calibration-active. Slack should be readable, but not at the cost of unsupported claims or lost citations.

Inline-agent trace collection, prompt version labels, and result classification metrics are calibration-active implementation surfaces. They may evolve to support debugging and post-launch analysis, but they do not change the core contract that answerable, abstain, and failure are distinct.

### 12. Open Questions and Evidence Standard

These questions do not block the launch architecture. They should be revisited after production behavior exists, especially once Slack has hundreds of active users or enough traffic to expose repeated failure and confusion patterns. The evidence standard is observed production data, integration test results, user-impact examples, and support or incident records. Preference, pre-launch speculation, or a small number of anecdotal demos is not enough.

HTTP mode versus Socket Mode remains open as a deployment-topology decision. The decision should use deployment constraints, connection reliability, signing/secret posture, streaming ergonomics, and operational evidence from pre-production traffic.

Whether an Amazon-internal Slack framework is required instead of Bolt for Java remains open. The decision should use constraints from the deployment environment and any internal platform requirements, not a preference for a wrapper.

The exact Claude Sonnet 4.6 model ID and regional availability remain open. The decision should use Bedrock availability, latency, quota, and integration test evidence for the Slack region.

The exact Slack-user-to-Amazon-alias resolver implementation remains open. The decision should use coverage, correctness, freshness, and failure-rate evidence. If email-derived alias resolution misses real users or produces ambiguous aliases, the architecture needs a stronger directory-backed resolver before broad launch.

Whether Slack should ever call the explicit scoped-metadata path remains open. Reopening requires a reliable Slack-side or adjacent source of `country`, `level`, and `role`, documented conflict semantics when that source disagrees with PAPI, and evidence across meaningful production usage that the added path improves latency or correctness without increasing mis-scope risk.

Whether the three Slack surfaces should diverge more strongly in response formatting remains open. Reopening requires observed user behavior showing that the shared rendering posture causes confusion, excess noise, or underuse on a specific surface.

Whether Slack should expose a secondary affordance for viewing supporting sources and policy links outside the main answer body remains open. Reopening requires evidence that citations in the default Block Kit context are either too noisy for common answers or too hidden for trust and audit needs.

Whether Helis or another AgenticContext-aware policy layer should become active for Slack remains open. Launch posture is to extract the TA alias and proceed with existing Skywalker scope behavior. Reopening requires a concrete policy need that cannot be satisfied by current identity and scope enforcement.

### 13. Closing Position

Slack is where Skywalker becomes conversational, but that is exactly why the backend boundary has to stay sharp. Slack interprets the turn, owns bounded conversation context, runs the inline agent, performs the return-control MCP call through Amazon MCP Gateway, and renders the final Slack message. Skywalker remains the scoped retrieval backend that returns structured answerable, abstain, or failure outcomes.

The design is buildable because every handoff has a source of truth and every non-happy path has a user-visible meaning. Conversation lives above MCP. Evidence judgment lives below it. The Slack model may make the answer easier to read, but it may not make the backend less honest.

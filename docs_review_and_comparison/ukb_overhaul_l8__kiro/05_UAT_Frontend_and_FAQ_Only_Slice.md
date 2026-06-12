## Section 05. UAT Frontend and FAQ-Only Slice

### 1. Tenets

The UAT slice exists to generate trustworthy launch evidence without pretending the full Skywalker production system is already present. Its tenets therefore resolve the trade-off between useful client exposure and premature architecture expansion.

1. We prefer scoped correctness over answer availability when they conflict.
2. We prefer a narrow FAQ-only UAT path over a thin imitation of production routing when they conflict.
3. We prefer explicit, server-established scope over browser-supplied or model-inferred scope when they conflict.
4. We prefer implementation-backed ingestion truth over stale abstraction when they conflict.
5. We prefer benchmark-gated calibration over pre-launch capacity commitments when they conflict.

These tenets are constraining. They explain why the UAT path fails closed when Midway scope claims are unavailable, why PAPI is not added as a rescue path, why UKB and routing-gate behavior are excluded from UAT evidence, why the query path reads the ingestion-owned SSM live-index pointer rather than an assumed AOSS alias, and why reranker GPU sizing is not fixed before measured data exists.

### 2. Problem and Architectural Intent

The first major client-visible Skywalker deliverable is a User Acceptance Testing slice due to clients on **June 30, 2026**. The deliverable exposes the FAQ retrieval arm through a Skywalker-owned web frontend inside Amazon's private network. It does not expose Slack, QuickSuite, the routing gate, UKB integration, or dual-arm convergence.

The architectural problem is to validate a real retrieval contract under real users while keeping the test surface honest about what it proves. UAT is not "production, but smaller." It is a bounded client path that proves whether Midway-authenticated users can reach a Skywalker-owned chat surface, whether the React frontend and inline-agent orchestrator can exercise the Bedrock `RETURN_CONTROL` pattern, whether the orchestrator can invoke Skywalker through Amazon MCP Gateway, and whether FAQ-only retrieval can return scoped evidence packages with citations or a clear abstain.

The intent is to make UAT useful engineering evidence without turning it into a shadow production design. UAT validates explicit-scope admission, FAQ evidence retrieval, citation rendering, abstain rendering, and gateway invocation. It does not validate production route selection, UKB candidate behavior, dual-arm ranking, Slack event handling, QuickSuite MCP consumption, or long-lived web-client strategy.

### 3. Boundary and Non-Goals

This section starts at the UAT browser surface and ends when the browser renders an answerable response, abstain response, identity-scope failure, or service-failure state. It includes the React single-page application, the server-side inline-agent orchestrator, Midway/Federate custom-claim consumption, Bedrock Inline Agent invocation, return-control handling, MCP Gateway invocation, FAQ-only Skywalker execution, and final streamed response delivery back to the browser.

The boundary excludes FAQ corpus construction, the MCP server's general identity contract, UKB retrieval, Slack integration, QuickSuite integration, production routing, production calibration policy, and source document authoring. Those adjacent systems still matter because UAT depends on them for contracts. Section 02 owns MCP entry semantics. Section 03 owns FAQ ingestion and live-index publication. Section 07 owns shared reranking and evidence-strength semantics. Section 08 owns Slack client behavior. Section 09 owns QuickSuite behavior.

The UAT non-goals are explicit:

- No routing-gate invocation.
- No UKB arm invocation.
- No dual-arm convergence.
- No Slack or QuickSuite client behavior.
- No PAPI call on the UAT request path.
- No browser-supplied retrieval scope.
- No new MCP tool or UAT-specific backend envelope.
- No corpus-management, admin, settings, or calibration UI.
- No permanent production web-client commitment.
- No AOSS alias assumption for the live FAQ index.
- No parent/child, linked-parent, or sibling-reassembly launch behavior.

### 4. Source-of-Truth Hierarchy

When sources disagree, implementers should use this hierarchy:

1. `IngestionCodeReference`.
2. Implemented code paths.
3. Architecture documents, including adopted API and integration contracts.
4. Design discussions.
5. Future proposals.

Within architecture documents, this section depends on Section 02 for MCP tool names and explicit-scope validation, Section 03 for FAQ ingestion truth, Section 07 for reranker semantics and no linked-parent or sibling reconstruction, Section 08 and API_12 for Bedrock Inline Agent return-control behavior, and Section 09, API_08, and API_14 for MCP Gateway route posture.

This hierarchy has consequences. If any older prose describes a stable AOSS alias for FAQ reads, that is not the UAT launch contract. UAT reads the ingestion-owned live pointer. If a proposal suggests PAPI fallback for UAT, Section 02 and this section reject it unless the explicit-scope decision is reopened. If future implementation changes the ingestion publication model, Section 03 and the implementation must change first; this section then follows.

### 5. Facts, Assumptions, and Consequences

The launch facts are fixed enough to build against:

| Fact | Source-backed behavior | Consequence |
| --- | --- | --- |
| UAT is due to clients on June 30, 2026. | The slice is the first major client-visible deliverable. | The launch path must stay narrow enough to finish and validate; production-only paths are excluded. |
| The client is a small React SPA on a Skywalker-owned private-network domain. | Users authenticate through Midway and submit one chat input. | The browser is a rendering and input surface, not an authority for retrieval scope or routing. |
| UAT uses a server-side inline-agent orchestrator. | The frontend posts the user turn to the orchestrator, which invokes Bedrock Inline Agents and performs return-control calls. | The orchestrator owns identity validation, scope extraction, MCP invocation, and SSE streaming back to the browser. |
| UAT does not call PAPI. | Midway/Federate custom claims provide `country`, `level`, and employee-class `role`; the orchestrator derives employee identity from the validated user. | Missing or malformed claims are launch-blocking integration failures, not triggers for hot-path PAPI fallback. |
| UAT calls `skywalker.search.by_explicit_scope`. | The inline-agent action group exposes `skywalker_search_by_explicit_scope`, mapped by the orchestrator to the MCP tool. | Skywalker receives complete explicit scope and validates it before FAQ retrieval. |
| UAT uses Amazon MCP Gateway on the CloudAuth route where applicable. | The orchestrator calls `https://api.mcp.asbx.aws.dev/ca/mcp/{registry-id}/{skywalker-server-id}` with CloudAuth credentials; MCP Gateway applies Bindle authorization and forwards with CloudAuth OBO; TA carries human identity when supported by the path. | Gateway, AAA application, Bindle, CloudAuth OBO, and TA failures are integration or service failures, not abstains. |
| UAT execution is FAQ-only. | The routing gate, UKB arm, and dual-arm convergence are not in the request graph. | UAT traffic cannot be used as evidence that routing or UKB production behavior is correct. |
| FAQ reads use current ingestion truth. | The active index is named by `/skywalker/ingestion/faq_evidence/live_index` over two physical indices, not by a stable AOSS alias. | The UAT query path must resolve the live index at runtime and must tolerate pointer-based promotion. |
| FAQ evidence is one-node-to-one-document at launch. | Section 03 and implementation map one COREx node to one `FragmentDocument`; no parent ID or child document contract exists. | UAT evidence packages cannot depend on parent/child joins, linked-parent expansion, or sibling reassembly. |
| Reranking is optional for UAT. | UAT may ship with or without Cohere Rerank 4 Pro. | `EVIDENCE_TOO_WEAK_AFTER_RERANK` is reachable only when the reranker is enabled; without it, weak-evidence abstain uses `NO_USABLE_EVIDENCE`. |
| Reranker GPU sizing is benchmark-gated. | Candidate SageMaker shapes are `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge`; final selection requires measured data and L6 engineering plus L6 finance approval. | This document cannot commit to a GPU shape or production-sized reranker capacity. |

The remaining assumptions are called out because they can be wrong under real UAT traffic:

| Assumption | Consequence if false | Revisit path |
| --- | --- | --- |
| The UAT domain remains private-network-only for the June 30 deliverable. | Exposure, authentication, and browser hosting assumptions would change. | Reopen the client-surface design before changing network exposure. |
| Federate can mint `country`, `level`, and `role` claims into the Midway token before launch. | The orchestrator cannot construct a trustworthy explicit-scope request, so UAT cannot safely retrieve. | Fix the identity integration or reopen the explicit-scope source decision with a documented alternate authority. |
| The orchestrator can initiate or propagate TA for the human invoker on the CloudAuth route. | Skywalker loses the intended human identity traceability channel for UAT. | Treat as gateway/auth integration failure unless API_14 posture changes and Section 02 reopens TA authority. |
| MCP Gateway registration and Bindle authorization are complete by launch. | UAT cannot call the Skywalker MCP server through the intended supported path. | Complete onboarding; do not introduce a direct MCP bypass without a new design decision. |
| The ingestion live-index pointer is readable from the UAT runtime environment. | FAQ retrieval cannot know which physical index is live. | Fix IAM/configuration for the pointer read; do not hardcode `faq_evidence_a` or `faq_evidence_b`. |
| One-node FAQ evidence is sufficient for UAT answer quality. | Users may see over-broad citations or misses caused by document granularity. | Reopen chunking only after post-launch analysis across hundreds of users shows node granularity is the dominant failure mode. |

### 6. Inputs, Outputs, and Contracts

The browser-to-orchestrator input is intentionally small: `query_text`, a chat-session identifier, and client request metadata needed for correlation. The browser does not send `country`, `level`, `role`, employee ID, route choice, or retrieval mode. Any scope value supplied by the browser must be ignored for retrieval.

The orchestrator's identity input is the validated Midway-authenticated server-side context. The orchestrator reads the human subject and the explicit-scope claims `country`, `level`, and `role`. It derives the retrieval `employee_id` from validated identity material available to the UAT integration. If any required identity or scope field is absent, empty, or non-canonicalizable, the orchestrator fails before Bedrock, MCP Gateway, or Skywalker is called.

The orchestrator-to-Bedrock contract uses `InvokeInlineAgent` with the UAT instruction, the chat `sessionId`, the user turn as `inputText`, and an action group containing one function: `skywalker_search_by_explicit_scope`. The action group uses `customControl: "RETURN_CONTROL"`. Final-response streaming is enabled so the user-facing response can stream back through the orchestrator.

The return-control contract is synchronous and discrete. When Bedrock emits a `returnControl` event for `skywalker_search_by_explicit_scope`, the orchestrator maps it to `skywalker.search.by_explicit_scope`, constructs the Section 02 JSON-RPC `tools/call`, fills `query_text`, `employee_id`, `country`, `level`, `role`, and `correlation_id`, initiates or propagates TA where supported, and calls MCP Gateway's CloudAuth route with the orchestrator AAA application's CloudAuth credentials.

The MCP Gateway contract is that the gateway validates inbound CloudAuth credentials, checks Bindle authorization for the orchestrator application to invoke the Skywalker server, forwards with CloudAuth OBO, and preserves delegated human identity material where API_14 supports it. Registry ID, server ID, AAA application name, and Bindle grant identifiers are onboarding details; they do not change the request shape.

The Skywalker tool contract is the Section 02 explicit-scope contract. Skywalker validates the JSON-RPC request, validates TA where required by the route, validates and canonicalizes explicit scope, does not call PAPI, and executes the UAT FAQ-only path. It resolves the active FAQ index from `/skywalker/ingestion/faq_evidence/live_index`, queries the physical index named by that pointer, applies scope filtering on `country`, `level`, and `role`, optionally reranks, and returns the shared structured envelope.

The Skywalker-to-orchestrator output is one synchronous JSON-RPC tool result. Successful backend results carry `result_kind` of `ANSWERABLE` or `ABSTAIN`, `route`, `scope_snapshot`, `evidence`, optional `abstain_reason`, and `correlation_id`. Tool-execution failures use the MCP error channel or tool-result error shape defined by Section 02/API_01. The MCP boundary does not stream final prose.

The orchestrator-to-browser output is Server-Sent Events for the final user-facing response, plus terminal state metadata sufficient for the React app to render one of four states: answerable, abstain, identity-scope failure, or service failure. Citations shown in the final answer must trace to FAQ evidence records returned by Skywalker. The inline agent is not allowed to answer from model memory when the backend did not return trustworthy evidence.

### 7. Fixed Decisions

| Decision | Rationale | Binds | Reopen criteria |
| --- | --- | --- | --- |
| UAT is FAQ-only. | The June 30 slice needs trustworthy evidence for one retrieval arm, not a partial simulation of production routing. | Request graph, evaluation interpretation, frontend states, and launch scope. | Reopen only if client commitment changes require UKB or routing evidence before June 30 and the dependent sections define buildable contracts. |
| Use a Skywalker-owned React SPA plus server-side inline-agent orchestrator. | React is sufficient for the private chat surface, while the orchestrator keeps identity, scope, gateway credentials, and return-control execution off the browser. | Client boundary, Midway validation, SSE streaming, and Bedrock/MCP invocation ownership. | Reopen if UAT must move to Slack, QuickSuite, or another hosted client before launch. |
| Scope comes from validated Midway/Federate claims, not the browser. | `country`, `level`, and `role` are correctness inputs, so they must come from a server-validated authority. | Browser payload, orchestrator admission, explicit-scope construction, and failure behavior. | Reopen only if the custom-claim integration cannot launch and another authoritative server-side scope source is approved. |
| UAT does not call PAPI on the request path. | Explicit-scope mode exists for callers that already hold authoritative scope; adding PAPI would add latency and a conflict policy the slice does not define. | Latency budget, identity failure semantics, Section 02 explicit-scope behavior, and UAT integration tests. | Reopen only with evidence that Midway claims are materially stale or unreliable and with a designed reconciliation rule for claim/PAPI disagreement. |
| The only UAT action-group function maps to `skywalker.search.by_explicit_scope`. | The inline agent should not choose among tools when the orchestrator already knows the only valid UAT entry mode. | Bedrock action schema, return-control mapping, and MCP request construction. | Reopen if UAT gains another authoritative identity mode or another backend capability in scope. |
| UAT invokes Skywalker through Amazon MCP Gateway's CloudAuth route where applicable. | The supported gateway path gives shared discovery, Bindle authorization, CloudAuth OBO, and TA posture instead of a UAT-only direct backend call. | Network route, AAA onboarding, Bindle grants, Skywalker auth expectations, and failure taxonomy. | Reopen only if MCP Gateway cannot support the UAT launch path and an approved alternate transport is documented. |
| UAT reads the ingestion-owned live index pointer. | Section 03 and implementation use two physical indices and an SSM live pointer because the launch model is not AOSS alias-based. | FAQ query configuration, rollback behavior, launch tests, and evidence provenance. | Reopen only after Section 03 and implementation move to a different publication contract. |
| UAT does not rely on parent/child or linked-parent evidence behavior. | Launch ingestion stores one node as one document, and Section 07 rejects hidden sibling reconstruction at rerank time. | Citation shape, evidence packaging, UI source rendering, and answer quality analysis. | Reopen only after production retrieval analysis shows one-node evidence is inadequate and ingestion/reranking contracts add richer structure. |
| Reranking may be absent from UAT. | FAQ-only UAT can still validate scoped retrieval, citations, and abstain UX without blocking launch on reranker readiness. | Abstain reason space, latency expectations, candidate ordering, and UAT evaluation labels. | Reopen if pre-launch evaluation shows hybrid-only ordering creates unacceptable false positives or misses for the UAT corpus. |
| Reranker GPU sizing is benchmark-gated. | Capacity choice without measured p50/p95 latency, throughput, utilization, and cost would be premature. | SageMaker instance selection, cost forecast, launch approval, and production capacity planning. | Reopen after benchmarks across `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` plus L6 engineering and L6 finance approval. |
| Abstain is rendered separately from service failure. | `ABSTAIN` means retrieval completed honestly and found insufficient evidence; service failure means the system could not execute the contract. | Frontend state model, user messaging, evaluation, and incident triage. | Reopen only if user research shows the distinction is misunderstood and a new rendering preserves backend semantics. |

### 8. Alternatives Considered

Using Slack or QuickSuite as the UAT surface is attractive because those are expected production client surfaces. It is rejected for this slice because UAT needs a controlled private web surface that can exercise explicit Midway claims and a Skywalker-owned orchestrator without waiting on Slack event handling or QuickSuite identity-carriage decisions.

Calling PAPI from the UAT orchestrator is attractive because PAPI is the normal authority for alias and employee-ID scope resolution. It is rejected because UAT's explicit-scope path deliberately validates the custom-claim integration. Adding PAPI would obscure which source wins on disagreement and would turn a missing-claim launch blocker into a hidden fallback.

Letting the browser send scope is attractive because it would simplify early demos and avoid Federate custom-claim dependency. It is rejected because the browser is not an authority for employee eligibility scope. A plausible answer for the wrong `country`, `level`, or `role` is worse than a visible identity-scope failure.

Calling the Skywalker MCP server directly from the orchestrator is attractive because it removes gateway onboarding risk. It is rejected because the supported architecture uses Amazon MCP Gateway for shared server discovery, Bindle authorization, CloudAuth OBO, and TA posture. A direct UAT path would create a second integration contract that production clients do not exercise.

Routing UAT through the full production decision flow is attractive because it would collect broader telemetry sooner. It is rejected because routing, UKB, and dual-arm convergence have their own unresolved calibration surfaces. Including them would make UAT failures ambiguous: a bad answer could be caused by FAQ retrieval, UKB retrieval, routing, fusion, reranking, or client rendering.

Assuming a stable AOSS alias for FAQ reads is attractive because aliases are familiar in search systems. It is rejected because the current ingestion implementation uses an SSM pointer over two physical indices. UAT must follow the implemented live-read contract rather than documenting an alias that launch code does not provide.

Requiring reranking for UAT is attractive because it may improve precision and align with the production target. It is deferred because UAT's core architectural evidence does not require reranking, and SageMaker GPU sizing must be based on measured latency, throughput, and cost rather than preference.

Adding parent/child reconstruction for UAT evidence is attractive because citations could become more granular. It is rejected because launch ingestion and reranking do not implement parent/child or linked-parent behavior. Adding it only for UAT would create source provenance and citation contracts the rest of the architecture does not own.

### 9. End-to-End Flow

First, a Midway-authenticated employee opens the private UAT domain and submits a question in the React chat input. The frontend sends the user text, chat-session identifier, and correlation metadata to the UAT orchestrator. It does not send retrieval scope or select a backend route.

Second, the orchestrator validates the Midway session and reads the server-side identity material. It extracts or derives the employee identity and reads `country`, `level`, and `role` from the validated custom claims. If the identity or any required scope field is missing, empty, or non-canonicalizable, the orchestrator returns an identity-scope failure to the browser and stops.

Third, the orchestrator calls Bedrock `InvokeInlineAgent` with Claude Sonnet 4.6, the UAT instruction, the chat `sessionId`, the user turn as `inputText`, final-response streaming enabled, and one return-control action-group function named `skywalker_search_by_explicit_scope`.

Fourth, when Bedrock emits a `returnControl` event, the orchestrator constructs the real backend call. It maps `skywalker_search_by_explicit_scope` to `skywalker.search.by_explicit_scope`, fills `query_text`, `employee_id`, `country`, `level`, `role`, and `correlation_id`, initiates or propagates TA for the human where supported, and sends the JSON-RPC `tools/call` to MCP Gateway's CloudAuth route using the orchestrator AAA application's CloudAuth credentials.

Fifth, MCP Gateway validates the inbound CloudAuth credentials, checks Bindle permission for the orchestrator application to invoke the Skywalker MCP server, forwards to Skywalker using CloudAuth OBO, and preserves human delegated identity material when present. Gateway rejection stops the request before Skywalker retrieval.

Sixth, Skywalker admits the explicit-scope call. It validates the JSON-RPC envelope, validates TA where required by the route, validates and canonicalizes explicit scope, records route and scope metadata, and proceeds directly to the UAT FAQ-only runtime path. It does not call PAPI, invoke the routing gate, or construct a UKB request.

Seventh, FAQ retrieval embeds the query with Cohere Embed v4 using `input_type: "search_query"`, resolves the active physical FAQ index by reading `/skywalker/ingestion/faq_evidence/live_index`, executes the FAQ hybrid search pipeline, and applies FAISS `efficient_filter` scope filtering on `country`, `level`, and `role`. Retrieved evidence documents are the one-node-to-one-document records from Section 03.

Eighth, optional reranking runs only if the UAT deployment includes the reranker. With a reranker, candidates are normalized and scored through Cohere Rerank 4 Pro according to Section 07. Without a reranker, the hybrid-fused FAQ order is the shortlist. In both cases, Skywalker packages selected evidence and source metadata into the shared response envelope.

Ninth, Skywalker returns one synchronous MCP result through MCP Gateway to the orchestrator. The result is either `ANSWERABLE`, `ABSTAIN`, or a tool/protocol failure. The orchestrator passes the structured tool result back into the inline agent through `inlineSessionState.returnControlInvocationResults`.

Tenth, the inline agent produces final user-facing prose only from the structured result. Bedrock streams final-response chunks to the orchestrator, and the orchestrator re-emits them to the React app over Server-Sent Events. When the stream terminates, the frontend applies the terminal state: answerable with citations, successful abstain, identity-scope failure, or service failure.

### 10. Failure and Abstain Behavior

Identity-scope failure occurs before Bedrock or Skywalker. Missing Midway session, expired session, missing `country`, missing `level`, missing `role`, empty employee identity, or non-canonicalizable claim values cause the orchestrator to fail closed. The frontend tells the user that Skywalker could not establish their eligibility scope. This is not an abstain because retrieval never had a valid scoped request.

Inline-agent invocation failure is a service failure. If `InvokeInlineAgent` cannot be called, times out before return control, or returns an unrecoverable agent-runtime error, the frontend renders service failure. The orchestrator must not synthesize an answer from the raw user question.

Unexpected return-control shape is a service failure. If the inline agent requests any function other than `skywalker_search_by_explicit_scope`, omits required parameters that the orchestrator cannot safely fill from server-side identity, or returns a malformed invocation, the orchestrator stops. It does not ask the model to choose another tool.

MCP Gateway authentication or authorization failure is a service failure. A CloudAuth rejection, Bindle denial, registry/server ID mismatch, gateway timeout, or CloudAuth OBO failure means the integration path is not available. The frontend renders service failure, not abstain.

TA failure on a route that requires TA is a service or integration failure. Skywalker must not downgrade to model-supplied or browser-supplied identity material to preserve the appearance of success.

Skywalker explicit-scope validation failure is an admission failure. If Skywalker rejects the supplied `employee_id`, `country`, `level`, or `role`, no retrieval occurs. The frontend renders this as identity-scope failure when the error is attributable to scope material and as service failure when the error is an internal contract mismatch.

FAQ live-index pointer failure is a service failure. If the query runtime cannot read `/skywalker/ingestion/faq_evidence/live_index`, or the pointer names an unavailable physical index, Skywalker cannot execute the FAQ contract. It must not guess a physical index.

FAQ retrieval returning no defensible evidence is a successful abstain. Without the reranker, the UAT abstain reason is `NO_USABLE_EVIDENCE`. With the reranker, `EVIDENCE_TOO_WEAK_AFTER_RERANK` may also be returned. The UI renders abstain as an honest successful outcome, distinct from timeouts, gateway failures, and identity failures.

Inline-agent prompt non-compliance is a calibration and evaluation signal. If the final prose claims facts not supported by Skywalker's evidence envelope, the response is defective even if the backend result was answerable. The prompt, guardrails, and evaluation set need adjustment; the architecture does not permit model memory to replace evidence.

### 11. Calibration Surfaces

The following surfaces may move with measured UAT evidence without changing the launch architecture:

- Inline-agent inference parameters such as `temperature`, `maxTokens`, and `idleSessionTTLInSeconds`.
- The exact UAT system instruction and return-control tool description.
- SSE chunk coalescing, typing indicator behavior, timeout copy, and browser terminal-state presentation.
- Citation rendering format: inline markers, footnoted source list, expandable source panel, or another format that keeps every claim traceable to evidence.
- Hybrid retrieval depths, candidate budgets, and scope-filter diagnostics inherited from the FAQ query path.
- Reranker presence for UAT, provided absence is represented honestly in evaluation and abstain reasons.
- Reranker candidate budget and evidence-strength thresholds when the reranker is present.
- MCP Gateway and Bedrock timeout values, retry posture, and user-facing failure copy.
- Identity-scope validation messages and integration readiness checks around Midway custom claims.

The following are not calibration surfaces in this document: adding PAPI to UAT, broadening retrieval without full scope, invoking UKB, invoking the routing gate, changing the ingestion live-index contract, using a stable AOSS alias without Section 03 changing first, adding parent/child evidence behavior, or selecting a SageMaker GPU shape without benchmark evidence and required L6 approvals.

Reranker GPU sizing remains a calibration surface with a higher evidence bar than ordinary prompt or UX tuning. The candidate shapes are `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge`. The decision requires measured p50 and p95 latency, throughput, cold-start or deployment behavior, utilization, and cost under representative UAT payloads. It also requires L6 engineering and L6 finance approval before the selected shape is treated as fixed.

### 12. Open Questions

Many open questions in this section cannot be resolved through offline experimentation alone. The UAT slice has not yet observed enough launch behavior, real user query patterns, identity-claim failures, frontend rendering confusion, or FAQ evidence gaps to close them honestly. Initial decisions should therefore optimize for observability, safe iteration, and low-cost reversibility. Final answers for reusable-client posture, latency tuning, reranker posture, and evidence granularity should emerge only after launch and after observing real usage, with structural changes requiring evidence across hundreds of active users.

The exact UAT domain hostname remains open as a launch detail. The evidence standard is successful Midway-authenticated access from the intended client population on the private network.

The exact MCP Gateway registry ID and Skywalker server ID remain open until onboarding completes. The evidence standard is an end-to-end gateway invocation from the UAT orchestrator through CloudAuth, Bindle authorization, CloudAuth OBO, and TA propagation where supported.

The June 30 reranker posture remains open. The evidence standard is whether implementation readiness, UAT-quality evaluation, latency, throughput, cost data, and required L6 approvals exist before launch. Without that evidence, UAT may ship without reranking and must label abstain reasons accordingly.

The exact canonical encodings for UAT `country`, `level`, and `role` remain open until integration tests pin them. The evidence standard is successful positive tests for representative employees plus negative tests proving missing, malformed, and non-canonicalizable values fail before retrieval.

Whether the React frontend and orchestrator should be reused after UAT remains open. The evidence standard is post-launch UAT feedback and maintenance data, not pre-launch preference. A long-lived web client requires a separate design if it becomes a product surface.

Whether one-node-to-one-document evidence remains sufficient after broader use remains open. The evidence standard is production or post-UAT analysis across hundreds of active users showing that citation granularity or retrieval misses are primarily caused by node-level document shape rather than query wording, scope filters, or reranking.

Whether explicit-scope trust should gain sampled PAPI comparison or delayed audit remains open. The evidence standard is post-launch mismatch, stale-claim, or support evidence showing Midway custom claims are materially wrong often enough to affect answer correctness. A reconciliation mechanism must define which source wins before it changes runtime behavior.

Whether UAT latency requires gateway, Bedrock, or reranker tuning remains open. The evidence standard is measured end-to-end latency broken down by orchestrator, Bedrock inline-agent, MCP Gateway, Skywalker retrieval, optional reranker, and streaming phases under real UAT traffic.

### 13. Closing Position

The UAT slice is deliberately narrow: a private React chat surface, a server-side inline-agent orchestrator, Midway/Federate explicit-scope claims, Bedrock Inline Agent return control, Amazon MCP Gateway invocation with CloudAuth OBO and TA posture where supported, FAQ-only retrieval, current SSM-pointer live-index resolution, evidence-backed citations, and distinct abstain rendering.

The slice intentionally excludes PAPI, routing, UKB, dual-arm convergence, Slack, QuickSuite, parent/child evidence behavior, linked-parent launch behavior, and permanent web-client strategy. That narrowness is the design, not a limitation to hide. It gives the team clean evidence about the FAQ arm and client-to-MCP invocation path while leaving production-only decisions to the sections that own them.

## Section 05. UAT Frontend and FAQ-Only Slice

Sections 01 through 04 fixed the production architecture: the system boundary, the entry contract and identity scoping, the controlled FAQ corpus, and the online query pipeline. This section steps deliberately to the side of that picture and defines the **first major project deliverable**: a User Acceptance Testing slice scoped to the FAQ arm only, exposed through a Skywalker-owned web frontend rather than through Slack or QuickSuite, due to clients on **June 30, 2026**.

The deliverable matters as architecture, not just program management, because what gets built for UAT is not "the production system, smaller." It is a deliberately narrowed slice that exercises specific contracts under real user traffic before the rest of the architecture exists. Writing UAT as production-in-miniature would force premature commitments to the routing gate, the UKB integration, and the rerank fleet that the deliverable does not need. Writing it as throwaway would lose the value of treating the FAQ arm as a real, contracted retrieval surface from day one. This section keeps both errors out.

The shape: a small React single-page application served from a Skywalker-owned domain inside Amazon's private network. A user authenticates through Midway and lands on a single chat input. The application calls a small inline-agent orchestrator that invokes a Bedrock Inline Agent running Claude Sonnet 4.6; the agent uses the return-control loop (shared with Section 08's Slack path) to call Skywalker's MCP surface through Amazon MCP Gateway on the CloudAuth-inbound route. The request hits the FAQ arm exclusively — no gate, no UKB, no dual-arm convergence — and the response renders as a grounded answer with citations or as a structurally distinct abstain message. The reranker is **optional for UAT**: the slice is buildable without it and ships with whichever posture the team lands before June 30.

One identity decision is flagged up front because it is the largest deviation from the production sections: **UAT does not call PAPI.** The Federate Service Profile backing the UAT domain's Midway authentication mints custom claims for `country`, `level`, and `role` — the `role` claim carrying the **employee class**, in the corpus vocabulary Section 02 fixed — directly into the Midway token, sourced from the same authoritative HR data PAPI exposes. The orchestrator reads those claims server-side and calls `skywalker.search.by_explicit_scope`. Section 02's rule that scope is part of correctness holds in full; this slice satisfies it by carrying scope in the token rather than resolving it per request.

### 1. The slice boundary

This section owns the UAT deliverable as a coherent, time-boxed slice: the React frontend, the Midway authentication path that surfaces the scope triple to the orchestrator, the orchestrator bridging the authenticated browser session to Bedrock Inline Agents, the return-control loop's UAT instantiation, the FAQ-only retrieval posture, the reranker-optional posture, and the three-way UI distinction between answerable, abstain, and failure.

It also owns the boundary separating the slice from the production architecture, so the slice does not silently absorb production assumptions. The routing gate (Section 04), the UKB arm (Section 06), QuickSuite (Section 09), and Slack (Section 08) are not on this path — scope decisions, not technical regressions. UAT validates the FAQ arm end to end against real users and client-side review of grounded answers; it is not a test of the two-arm design, the gate's calibration surfaces, or the QuickSuite consumption model. Saying that here keeps later sections from defending their existence against "but UAT didn't use it" — which is not the same statement as "UAT showed it was wrong."

It does not own the FAQ corpus (Section 03 — UAT consumes the published surface as-is), the canonical request shape (Section 02), the return-control loop's definition (Section 08 — UAT reuses the pattern with one transport and one frontend swapped underneath), the production reranker contract (Section 07 — UAT's optionality is a slice property, not an alternative design), or the calibration governance (Section 10 — UAT produces evidence Section 10 consumes; it does not run a parallel governance track).

### 2. Inputs, outputs, and contracts

The slice has four hops — browser to orchestrator, orchestrator to Bedrock, agent (via return control) to MCP Gateway, gateway to Skywalker — and each hop is a real contract.

**Browser to orchestrator.** The browser holds a Midway-authenticated session whose token carries, beyond the standard `sub` (alias) and group claims, the custom scope claims: `country`, `level`, and `role` (employee class). The frontend's contract to the orchestrator carries exactly one retrieval-relevant field — the user's `query_text` — plus a session identifier. Every field that matters to retrieval comes from the validated Midway identity, never from the request body. That is the architectural reason the frontend can stay simple: the browser cannot assert scope, so a tampered request body cannot de-scope retrieval.

**Orchestrator to Bedrock Inline Agents.** The orchestrator calls `InvokeInlineAgent` against `bedrock-agent-runtime` with the Claude Sonnet 4.6 model, a stable UAT behavioral instruction (Skywalker is the scoped-retrieval truth source; abstention is valid; sources must be preserved; ungrounded answers are forbidden), a `sessionId` derived from the chat session, and `inputText` carrying the user's turn. The action group declares one function — `skywalker_search_by_explicit_scope`, mirroring the MCP tool with underscores for dots — with `actionGroupExecutor: customControl: "RETURN_CONTROL"`, so the orchestrator performs the MCP call rather than Bedrock invoking a Lambda. Launch parameters: `temperature: 0`, `maxTokens: 1024`, `idleSessionTTLInSeconds: 900` ([API_05]). The single-tool action group is a deliberate UAT narrowing: the orchestrator always already holds the scope fields, so the alias tool has no role on this path.

**Return-control hop through MCP Gateway.** On a `returnControl` event, the orchestrator maps the function name to `skywalker.search.by_explicit_scope`, builds the `tools/call` request per [API_01], **initiates a TransitiveAuth token** for the human invoker (alias from the Midway `sub` claim plus an `AgenticContext` naming the agent involvement), and POSTs to `https://api.mcp.asbx.aws.dev/ca/mcp/{registry}/{skywalker-server-id}` with CloudAuth credentials minted by the orchestrator's AAA application, the TA token riding alongside via the CloudAuth MCP SDK. The gateway validates inbound CloudAuth, checks the orchestrator's `canInvoke` on Skywalker's Bindle resource, and forwards with **CloudAuth OBO** (Skywalker sees the orchestrator's AAA identity, not the gateway's) and the TA token preserved. Skywalker validates the TA token server-side and reads the human alias from TA claims. Gateway overhead is published at ~50–150 ms with a 50 TPS per-client throttle — both comfortably inside UAT's budget; OBO and TA validation add low tens of milliseconds ([API_14]). The SigV4-inbound route an earlier draft described is closed to new servers (April 24, 2026 cutoff); CloudAuth-inbound is the only available shape and also the one with native OBO + TA support.

**Inside Skywalker.** The explicit-scope request takes its canonical Section 02 form — scope supplied directly, PAPI not called — and proceeds straight to FAQ retrieval. No routing gate (`route.path` stamps `FAQ_ONLY` on every UAT response). No UKB arm — not "skipped at runtime"; not in the deployment. The FAQ retrieval runs unchanged from Section 04's definition: the query embeds once through `us.cohere.embed-v4:0` with `input_type: search_query`; the runtime resolves the live physical index through the SSM pointer `/skywalker/ingestion/faq_evidence/live_index`; the hybrid query (BM25 on `text` plus FAISS k-NN with the scope `efficient_filter`) runs through the `skywalker-faq-hybrid` pipeline; the scope filter applies the `(employee value OR everybody value)` semantics per dimension. Candidates are whole-node fragments (Section 03 Decision 5) — there is no sibling expansion, no parent reconstruction, no linked-Q&A citation chain on this or any path.

**Reranker optionality.** With the reranker present, the pool is scored through Cohere Rerank v4 on SageMaker exactly per Section 07 and the abstain floor applies. Without it, the hybrid-fused retrieval order is the shortlist, and only the `NO_USABLE_EVIDENCE` abstain branch is reachable — `EVIDENCE_TOO_WEAK_AFTER_RERANK` requires a rerank score to test against the floor. Either posture returns the same envelope shape.

**Output back to the chat UI.** The structured envelope ([API_01]: `result_kind`, `route`, `scope_snapshot`, `evidence[]`, `abstain_reason`, `correlation_id`) returns through the gateway as a single sync JSON-RPC result — the MCP boundary does not stream (Section 02 §2). The orchestrator feeds the serialized envelope back via a second `InvokeInlineAgent` on the same `sessionId` with `inlineSessionState.returnControlInvocationResults` populated and `streamingConfigurations: {streamFinalResponse: true}`. The agent's final text streams as `chunk` events; the orchestrator re-emits them to the React app over Server-Sent Events; the browser-native `EventSource` API renders progressively ([API_12]). The contract back to UAT remains a structured backend result, not final prose: the agent composes the message, and the frontend is not allowed to render evidence text directly without going through the agent. Streaming changes when characters appear; it does not change the rule that every claim in the rendered message traces to the evidence package.

### 3. Fixed decisions

**Decision 1 — UAT is a deliberately narrowed slice.** Gate, UKB, dual-arm, QuickSuite, and Slack are out of scope by intent. The slice exercises the FAQ arm end to end through a Skywalker-owned client so the retrieval contract and the citation/abstain UX are validated against real users before the rest of the architecture lands. Binds the deliverable's scope and the June 30 date. Reopens never — post-UAT expansion is a new deployment decision, not a UAT scope change.

**Decision 2 — Frontend hosting posture.** A static compiled React SPA behind Amazon's private network on a Skywalker-owned domain: one chat window, one input, no admin UI, no settings, no navigation. The minimalism is architectural — every additional surface is a place the slice would have to take a position the production architecture has not finalized. Binds the frontend's build scope. Reopens never within UAT.

**Decision 3 — Identity via custom Midway claims, no PAPI.** The Federate Service Profile mints `country`, `level`, and `role` (employee class) into the token; the orchestrator reads them server-side as authoritative. Satisfies scope-as-correctness (tenet 3) without putting the identity-resolution path inside a slice whose purpose is validating the FAQ arm. The production paths continue to call PAPI per Sections 02, 08, and 09 — this is the only path that bypasses it. Binds the Federate profile configuration and the claim vocabulary, which must carry the corpus's employee-class labels (Section 02's mapping contract applies to the claim values exactly as it applies to PAPI values — a claim minting `MANAGER` would reproduce the vocabulary mismatch this series already killed). Reopens if claim provisioning proves unreliable, in which direction the fallback is PAPI-on-the-UAT-path, not weakened scope.

**Decision 4 — Transport is Amazon MCP Gateway on the CloudAuth-inbound route.** The orchestrator registers as a CloudAuth-modeled AAA application (working name `SkywalkerUATOrchestrator`, finalized at onboarding) with `canInvoke` on Skywalker's MCP Bindle resource; CloudAuth OBO carries the orchestrator's identity; TransitiveAuth carries the human's (tenet 5; [API_14]). Same gateway product, same Skywalker server, same Bindle surface as the Slack and QuickSuite paths — one transport, one authorization surface, access managed against one bindle across all three clients. Binds the onboarding work and §6's flow. Reopens never; a per-client bypass is a rejected posture (§4).

**Decision 5 — The agent layer is a Bedrock Inline Agent with RETURN_CONTROL.** Claude Sonnet 4.6 via `InvokeInlineAgent`, single-function action group, the same mechanics Section 08 fixes for Slack. Reusing the pattern keeps UAT and Slack from drifting into incompatible agent-side contracts before Slack ships. Binds the orchestrator implementation. Reopens with Section 08's agent-layer decisions, not independently.

**Decision 6 — FAQ-only retrieval, by deployment.** `route.path: FAQ_ONLY` on every response; the dual-arm path does not exist on this request graph. Binds the deployment configuration. Reopens never within UAT — turning on the gate and UKB is the post-UAT deployment's job.

**Decision 7 — Reranker-optional for UAT.** Shippable with or without the SageMaker Rerank v4 endpoint; present means Section 07's contract applies in full, absent means hybrid order is the shortlist and the abstain vocabulary narrows to `NO_USABLE_EVIDENCE`. The optionality exists because the deliverable date is fixed and the rerank fleet is the heaviest infrastructure piece — made heavier by the pending instance bake-off (Section 01 §9) — and coupling UAT to it would put the date at risk for a validation gain UAT does not need. Binds the UAT environment definition and §7's abstain behavior. Reopens only as the program decision named in §8.

**Decision 8 — Citation discipline applies in full.** The final user-facing message must be constructible from the evidence package, with citations to the specific FAQ evidence (via each candidate's `source_url` and `policy_links`) surfaced in the rendered message. The visual treatment is not load-bearing; the grounding discipline is. UAT is not allowed to ship answers that look grounded but are not. Binds the agent instruction and the frontend's sources rendering. Reopens never. One dependency is named honestly, and reclassified: the implemented ingestion writes `source_url` and `policy_links` empty, and **this gates the deliverable** — a fixed citation requirement with an unwired data source is not a side task. Worse, the fix being mechanical is itself an unverified assumption: if CoreX's `applicable-policy` carries policy codes rather than links, citations require a code-to-URL resolution table that does not exist (Section 03 §9 carries the full statement and the inspect-now deciding evidence). This is the item most likely to slip June 30.

**Decision 9 — Abstain UX is structurally distinct from service failure.** On `result_kind: ABSTAIN`, the chat renders a clearly shaped "no grounded evidence for your specific situation" message, visually and copy-distinct from "the service failed." Same rule Section 08 applies to Slack. Binds the frontend's three-state rendering (§6 step nine). Reopens never.

**Decision 10 — UAT runs against production-shape FAQ data.** The CoreX → ingestion → AOSS path from Section 03 is the surface UAT consumes; real authored content under real per-user scope filtering is the entire point. Which `{stage}` UAT reads is operational, not load-bearing — but the data is authored content, never a fixture set. Binds the UAT environment to the ingestion pipeline's outputs. Reopens never.

### 4. Alternatives considered

**Production-shape miniature.** Gate present but loosely calibrated, UKB lightly used, dual-arm enabled. Rejected: UAT has no calibration evidence to set those values honestly, and shipping under-calibrated surfaces either freezes them on weak evidence or teaches the team to ignore them.

**UAT on Slack.** Rejected: Slack onboarding (event surfaces, Bolt handlers, the 3-second ack budget) is a real body of work UAT does not need in order to validate the FAQ arm. A hosted React frontend reaches user feedback faster on a cleaner contract.

**UAT on QuickSuite.** Rejected: Federate-OAuth auth, connector registration, and the Sources-UI quirk are all real and all irrelevant to FAQ-arm validation.

**Bypassing MCP Gateway.** Rejected for the same reason Sections 08 and 09 reject it: the gateway is the paved path (tenet 5), and a non-paved UAT shape adjacent to gateway-fronted production paths is drift by construction. If the gateway's support matrix ever blocks UAT, the answer is aligning Skywalker's auth posture with the gateway, not a per-client bypass.

**Hardcoded scope** (one fixed triple for every UAT request). Rejected: scope filtering would behave identically for every user and UAT's value as evidence about the FAQ arm under real per-user scope would collapse. Custom claims preserve per-user semantics without a PAPI call.

**Mandatory reranker.** Live, not fixed — the program decision in §8.

**Throwaway-prototype discipline** (citations optional, abstain merged with failure). Rejected: UAT is the first time real users see the system, and the citation and abstain rules are the strongest accuracy-preserving disciplines in the architecture. Shipping without them teaches users and the team that grounding is aspirational.

### 5. Assumptions inherited from upstream

From Section 01: the system boundary (the React app and orchestrator are clients of Skywalker, not parts of it) and the series tenets. From Section 02: the canonical request shape, the explicit-scope tool, scope-as-correctness (satisfied via token claims), and the employee-class vocabulary — the claims must carry it. From Section 03: the published FAQ surface in full — whole-node fragments, the SSM live pointer, the hybrid pipeline, the never-unscoped corpus guarantee — consumed as-is. From Section 04: the FAQ query construction, embedding discipline, and filter semantics; not the routing gate (FAQ-only by deployment, not per-request decision). From Section 07, conditionally: the reranker contract and the two-branch abstain rule when the reranker is present; `NO_USABLE_EVIDENCE` only when absent. From Section 08: the return-control loop pattern in full — UAT differs only in the declared function (explicit-scope, not alias) and the surface (React, not Slack). From Section 09's transport posture: one gateway, one server, one Bindle surface across all three client paths.

### 6. End-to-end data flow

**Step one — the user types a question.** A logged-in employee on the UAT domain (Midway-authenticated, private-network-only; the hostname is operational) types into the single chat input; the frontend posts `query_text` plus session ID to the orchestrator over the authenticated session.

**Step two — the orchestrator reads scope from the token.** It validates the Midway identity and reads `sub` (alias), `country`, `level`, and `role` (employee class). It does not call PAPI, does not synthesize scope, does not fall back to defaults. Any required claim missing or malformed stops the request here with a structurally distinct "we could not establish your identity scope" error — Section 02's fail-closed rule applied at the UAT orchestrator.

**Step three — the orchestrator invokes the inline agent.** `InvokeInlineAgent` with the model, instruction, session ID, and the user's turn; the action group declares `skywalker_search_by_explicit_scope` with RETURN_CONTROL. The agent returns either a clarification turn (no tool call — streamed directly to the user) or a `returnControl` event naming the function and arguments.

**Step four — return control becomes an MCP call through the gateway.** Function name maps to the MCP tool; the orchestrator initiates the TA token (human alias plus `AgenticContext`), POSTs to the CloudAuth route with CloudAuth credentials, and the gateway authorizes against the Bindle resource and forwards with OBO plus the preserved TA token.

**Step five — Skywalker runs the FAQ-only path.** Canonical request constructed from the explicit-scope arguments plus the TA-validated alias; query embedded once (`search_query`); live index resolved via the SSM pointer; hybrid query through `skywalker-faq-hybrid` with the per-dimension `(employee value OR everybody value)` scope filter. Whole-node fragment candidates return and normalize into the common envelope.

**Step six — optional reranking.** Present: Section 07's scoring and abstain floor apply. Absent: hybrid order is the shortlist. No post-rerank expansion exists in either posture — fragments are already whole.

**Step seven — the envelope returns.** `result_kind`, `route.path: FAQ_ONLY`, `scope_snapshot` echoing the claims-supplied triple, `evidence[]`, `abstain_reason` where applicable, `correlation_id` — back through the gateway as one sync result.

**Step eight — the orchestrator streams the agent's response.** Second `InvokeInlineAgent` on the same session with the serialized envelope in `returnControlInvocationResults` and `streamFinalResponse: true`; `chunk` events re-emit to the browser over SSE, with a terminating event when the stream ends ([API_12]).

**Step nine — the React app renders three distinguishable states.** Chunks append progressively with a typewriter cursor. On the terminating event: `ANSWERABLE` renders the grounded answer with citations as clickable source links; `ABSTAIN` applies the structurally distinct abstain styling; a true service failure (any hop errored, or the Bedrock stream died mid-generation) overwrites any partial text with a service-failure message distinct from both. Three states, three user experiences — users can always tell "no grounded evidence for you" from "the system broke."

### 7. Failure behavior and abstain behavior

UAT failure handling is more conservative than production, not less: it is the first surface real users see, and a confusing failure shape early teaches distrust.

**Identity failure at the orchestrator.** Missing/expired token or missing scope claims → identity-shaped error to the frontend, no Bedrock or Skywalker call. Distinct from both abstain and generic failure: the user is told the system could not even attempt retrieval for them.

**Gateway authorization failure.** 403 or rejection at the gateway → service failure. Likely causes are Bindle permission gaps or registration misconfiguration — operational, not user-facing; the frontend renders the failure state.

**Skywalker tool-execution error.** `isError: true` per the Section 02 error model, fed back to the agent with the flag preserved; the instruction directs the model to render a system-failure message, never to answer from memory. Frontend renders the failure state.

**Mid-stream failure.** `ModelStreamErrorException` or transport reset after partial text rendered → the terminating SSE event signals failure and the frontend replaces the partial text with the failure state ([API_12]).

**Prompt non-compliance.** The model answering from training instead of the evidence package is a calibration surface (the same one Section 08 tracks for Slack), visible against UAT traffic — not a per-request architectural failure.

**Abstain behavior** follows Section 07's contract scoped to the active posture: both branches reachable with the reranker, `NO_USABLE_EVIDENCE` alone without it. Abstain remains a successful result, never an error, and renders as Decision 9 requires.

Non-goals: UAT introduces no new MCP tool, no new abstain branch, no new envelope, no corpus or schema modifications, no parallel reranker fleet, and no gate-calibration evidence — the gate is not on this path; that evidence comes from the post-UAT deployment that turns on the gate and the UKB arm.

### 8. Calibration surfaces

**Surface one — inline-agent inference parameters** (`temperature: 0`, `maxTokens: 1024`, TTL 900 s). Shared launch defaults with the Slack path; UAT traffic is the first real exercise, and movement under UAT evidence is this surface's legitimate use.

**Surface two — the reranker posture.** Fixed as optional for the deliverable; the most likely surface to flip before June 30. The flip is a program decision (ship without versus delay to ship with) — outside Section 10's architecture governance, and named as such.

**Surface three — citation rendering.** The rule (present, traceable; abstain distinct) is fixed; the visual treatment (inline markers, footnote list, source cards) moves on UAT user feedback.

**Surface four — gateway latency.** Published 50–150 ms; UAT measurement is the first concrete evidence of where Skywalker lands in that band. Re-litigate only if measured gateway-attributable latency consistently exceeds the published bound — and the resolution is a conversation with the gateway team, not a bypass.

**Surface five — the custom-claims pattern's reach.** UAT establishes that token-carried scope is technically viable. If the production paths later want the same claims for the same reason, that is a Section 02 re-litigation (whether alias-resolution paths retire in favor of explicit scope everywhere) recorded in Section 10 — UAT does not pre-empt it.

Not a calibration surface: whether UAT grows into a long-lived production frontend. The production client surfaces are Slack and QuickSuite; a permanent web surface would be a new client-surface section, not a UAT scope expansion.

### 9. Open questions

Most open questions across this series share one precondition, stated in full in Section 10 §9: they are only answerable against real user data at meaningful volume — a few hundred actual users, arriving with the September production launch. Until then, launch postures stand, and pre-launch pressure to move them resolves as a recorded non-change.

**Skywalker's MCP Gateway server identifier** *(disclaimer: onboarding detail, not an architecture decision).* Surfaces when registration completes; the Bindle and registration shape are documented in the gateway user guide.

**Reranker presence at June 30** *(the program decision; gates nothing architecturally — both postures are fully specified).* Driven by what the team lands in time, including the pending instance bake-off upstream of any endpoint build-out.

**Claim minting verification under real traffic** *(disclaimer: shakedown item, not an open design).* Configuration is confirmed; verifying that `country`, `level`, and `role` arrive correctly — with `role` carrying corpus-vocabulary employee-class labels — under real Midway-authenticated traffic is part of UAT shakedown. Claims not arriving as expected is an integration failure to fix before UAT, not a re-litigation event.

**`source_url` / `policy_links` wiring — gates the deliverable** *(per Decision 8; full statement and the renderable-URL assumption in Section 03 §9).* Citations cannot render from empty fields, and whether the fix is wiring or net-new resolution-table design is unestablished until the CoreX fields are inspected.

**Post-UAT reuse** *(disclaimer: does not gate the deliverable; worth deciding deliberately rather than by inertia).* How much of the UAT build carries forward: the orchestrator's return-control loop is the same pattern Slack needs, so its agent-side code is a candidate for direct reuse; the React app is a candidate seed for any future dedicated web surface; the Federate custom-claims configuration is the proven prototype for token-carried scope. The deliberate decision is which of these become maintained assets versus archived references — made after UAT evidence lands, recorded in Section 10.

### Closing position

UAT is the first major deliverable, due June 30, 2026: a deliberately narrowed slice that exercises the FAQ arm end to end through a Skywalker-owned React frontend behind Amazon's private network. Midway authentication with custom Federate claims supplies `country`, `level`, and employee-class `role` directly, so PAPI is not on the request path while scope-as-correctness holds in full. A small orchestrator runs a Bedrock Inline Agent on Claude Sonnet 4.6 with the same return-control loop Slack will use, reaching Skywalker through MCP Gateway on CloudAuth with OBO and TransitiveAuth carrying service and human identity. The gate, UKB, dual-arm convergence, QuickSuite, and Slack are out of scope by intent. The reranker is optional and ships with whichever posture lands in time. The citation requirement and the structurally distinct abstain UX apply unchanged — and by the time UAT lands, the team has validated the FAQ retrieval contract, the citation and abstain UX, and the orchestrator-to-gateway-to-Skywalker invocation path under real users, without forcing the rest of the architecture to commit to calibration evidence it does not yet have.

---

*Stale-source flags raised in this section, for propagation: prior Section 05 references to the `faq_evidence_current` alias, the chunking pipeline, `linked_parent_ids` materialization, sibling-and-linked-parent expansion, and the `citations[]` chain envelope (all superseded per Sections 02 and 03); prior Section 05 `MANAGER`/`INDIVIDUAL_CONTRIBUTOR` claim vocabulary (superseded by employee class, Section 01 Decision 8); prior Section 05 fixed `ml.p5.4xlarge` reranker-fleet reference (instance pending the Section 01 §9 bake-off); [API_05]/[API_12] SigV4-auth references on the orchestrator-to-gateway hop (superseded by CloudAuth OBO + TransitiveAuth per [API_14]).*

## Section 08. Slack Integration and Client Boundary

Section 01 made Skywalker a retrieval backend behind MCP rather than a conversational agent. Section 02 fixed the entry contract and identity scoping. Sections 04 through 07 fixed the backend's runtime behavior: routing, UKB integration, common reranking, abstention. This section moves up one layer and defines the first production client surface Skywalker must live inside: a Slack bot employees invoke through slash commands, direct messages, and channel mentions.

Slack is not just another transport. It is where loosely phrased questions, conversational follow-ups, partial context, and ordinary workplace interaction arrive in one stream. Written as though Slack merely forwarded text in and echoed text out, this section would erase the part of the system that actually has to behave intelligently on the client side. So it does two things at once: defines the Slack bot as a concrete subsystem with its own boundaries, contracts, and failure semantics; and defines the line that must not blur — Skywalker owns scoped retrieval and evidence selection, the Slack application owns conversational handling, turn interpretation, and message construction. That division is not aesthetic. It keeps the retrieval backend deterministic and the Slack surface useful.

The system is intentionally asymmetric across client surfaces. QuickSuite brings its own chat-agent runtime and consumes Skywalker from that side (Section 09); in Slack, the team builds the bot, the model-layer prompt, and the interaction surface itself. This section has more surface area than Section 09 not because Slack is architecturally more important than retrieval, but because it owns more user-facing work. Both paths share one transport — Amazon MCP Gateway — and one Skywalker MCP server; they differ in inbound auth shape: CloudAuth from the Slack orchestrator's AAA application on `/ca/mcp/...`, with OBO carrying the orchestrator's identity and TransitiveAuth carrying the human's; Federate OAuth from QuickSuite on `/federate/mcp/...`, with identity in tool arguments.

### 1. The client boundary

This section owns the Slack-facing subsystem from the moment Slack delivers a user event to the moment the user receives a reply shaped from Skywalker output: event intake, surface normalization across the three supported surfaces, alias-aware request construction, the Slack-side model layer and its prompt, the return-control loop's Slack instantiation, and the translation of evidence or abstain packages into Slack-visible replies.

It owns the conversational boundary: the Slack application carries continuity, interprets follow-ups, and decides how one utterance becomes one or more tool calls — Skywalker is never extended into a multi-turn retrieval brain (Section 01 Decision 7). It owns the Slack-side identity handoff: the stable identifier on this path is the **Amazon alias**, resolved by the application from the Slack user identity (resolver implementation tracked in [API_02]'s open items; the architecturally important fact is that the alias is what crosses the MCP boundary, never raw Slack user IDs), then carried into Skywalker as TransitiveAuth claims initiated by the orchestrator. And it owns the distinction between a backend abstaining and a Slack conversation failing, between a retrieval failure and a user-visible reply, and the decision that the Slack application writes the user-facing answer rather than asking Skywalker for Slack-ready prose.

It does not own retrieval correctness — PAPI, the corpus, UKB, reranking, thresholds, and the abstain rules live below (Sections 02–07). It does not own the MCP contract beyond consuming it. And it does not own runtime human review: SME review is in scope once the system is live, but as a post-response evaluation discipline, not a request-time branch. A bug about a wrong answer from correct evidence files here; a bug about wrong evidence files below; a bug about a Slack event never arriving files against Slack platform configuration.

### 2. Inputs, outputs, and contracts

**The normalized turn.** Input arrives from three surfaces — slash command, DM, channel mention — and is normalized quickly into one internal turn object: raw message text, surface type, Slack user identity, resolved Amazon alias, the channel/thread return target, and the immediately relevant Slack-side conversation context. The application reasons over one turn representation, not over Slack's raw event diversity, and carries the subset of fields that determine who is speaking, what they asked, where the answer goes, and what prior context applies.

**The MCP contract, consumed.** Slack consumes Skywalker exclusively through MCP — never the corpus, UKB, or reranking directly. The default and only launch path is alias-based: Slack holds the alias, Skywalker resolves scope through PAPI. The employee-ID and explicit-scope tools remain in the core contract for other clients (UAT uses explicit-scope, Section 05); they are not on the Slack action group, because Slack does not hold authoritative `country`/`level`/employee-class values and exposing a tool the model cannot honestly populate invites fabricated scope.

**Two internal layers.** The event handler hands the normalized turn to the model layer; the model layer hands the narrower MCP tool call to Skywalker. Only the model layer bridges them — the event handler never constructs retrieval arguments from raw events, and Skywalker is never handed raw Slack structure. The turn object also has a prompt-facing contract: per turn, the model receives the current utterance, surface metadata, alias context, the permitted conversation excerpt, and the tool descriptions. The exact prompt text is not frozen; the fact that the prompt is part of the subsystem contract is. A Slack application whose model does not know the tool shapes, the importance of scoped correctness, and that abstention is valid is not operating inside this architecture even if the code runs.

**The response contract, consumed.** The application receives the structured envelope — `result_kind`, `route`, `scope_snapshot`, `evidence[]`, `abstain_reason`, `correlation_id` — never one raw block of prose it must judge for confidence. The model shapes wording; it is not free to invent what kind of backend result it received. Skywalker output is evidence-shaped input, not a final statement to echo: the Slack layer is an application composing the final answer from structured output, not a renderer over backend prose.

**The output** is a Slack reply event: an answer built from an evidence package, an explicit abstain message built from an abstain package, or a system-failure message when no trustworthy backend result was obtainable. Skywalker never needs to know whether the final message was a DM, an ephemeral slash-command response, or a threaded channel reply — that is precisely the variation this subsystem exists to own.

### 3. Fixed decisions

**Decision 1 — Slack is a real client surface, not a thin transport.** The team builds the prompting, interaction logic, and Slack-specific response behavior, because Slack is where conversational behavior has to exist. Binds this section's scope. Reopens never.

**Decision 2 — Three supported surfaces from the start.** Slash commands, DMs, channel mentions — all in scope, normalized early into one turn model. Binds the intake design. Reopens never; per-surface divergence is rendering, not orchestration (§8).

**Decision 3 — MCP-only consumption through Amazon MCP Gateway on CloudAuth.** The orchestrator (working name `SkywalkerSlackOrchestrator`) registers as a CloudAuth-modeled AAA application with `canInvoke` on Skywalker's Bindle resource; **CloudAuth OBO** carries the orchestrator's identity (Skywalker sees the orchestrator's principal, not the gateway's); **TransitiveAuth** carries the human's identity as a separate TA token the orchestrator initiates and Skywalker validates server-side ([API_14]; tenet 5). The SigV4-inbound route an earlier draft used is closed to new servers (April 24, 2026 cutoff); CloudAuth-inbound is the supported successor and the path with native OBO + TA. Binds the onboarding work, §6's flow, and the shared one-bindle authorization surface across all three client paths. Reopens never; bypassing the gateway is a rejected posture.

**Decision 4 — The Slack identity signal is the alias, carried via TA.** The resolver produces the alias before the MCP call leaves the orchestrator; the TA token is the canonical channel; `arguments.alias` is preserved for non-TA clients (QuickSuite) but is not this path's identity channel. Launch posture is **fail closed when TA is missing** — a TA failure surfaces as a system-failure error, never a silent fallback to argument-supplied identity. The action group declares one function, `skywalker_search_by_alias`. Binds the action-group shape and §7's failure rules. Reopens only if Slack ever gains an authoritative scope source (§9).

**Decision 5 — Multi-turn handling belongs to the Slack application.** The deterministic retrieval backend is the wrong place for conversational interpretation; the model layer already does that work well. Binds the conversational boundary. Reopens never (Section 01 Decision 7).

**Decision 6 — The model layer is Claude Sonnet 4.6 via Bedrock Inline Agents with RETURN_CONTROL.** `InvokeInlineAgent` with one action group (`skywalker_search_by_alias`, `customControl: "RETURN_CONTROL"` — the application performs the real MCP call rather than Bedrock invoking a Lambda; the Converse API is not used). `sessionId` derives from thread context (mentions), IM channel (DMs), or command-to-thread correlation; `idleSessionTTLInSeconds` starts at 900; inference starts at `temperature: 0`, `maxTokens: 1024` ([API_05]). Function names mirror MCP tool names with underscores for legibility against the tool registry. The decision also fixes **prompt ownership**: the stable behavioral prompt belongs to this subsystem and must teach five architectural facts — Skywalker is the source of scoped retrieval truth; backend abstention is preserved, never overwritten; sources and policy links stay attached; the model may not replace missing grounding with general fluency; and a `reranker_state: RERANKER_FAILURE_FALLBACK` result calls for more conservative composition, because the backend's abstain-on-weakness check could not run (Section 07 §7). Prompt text evolves; prompt ownership does not. Binds the orchestrator implementation, shared with UAT (Section 05). Reopens on model migration as a deliberate event.

**Decision 7 — Backend abstention is a valid outcome, rendered structurally distinct.** An abstain package is a grounded statement, not a failure; its Slack presentation must be distinguishable both from a service failure and from the application declining to engage. Users must be able to tell "Skywalker found no grounded answer for your specific situation" from "the bot is being cagey." Wording is a rendering concern; the distinction is architectural. Binds the reply construction. Reopens never (tenet 2).

**Decision 8 — Slack never asks Skywalker for conversation state.** History stays on the Slack side; the backend receives one request at a time. Binds statelessness. Reopens never.

**Decision 9 — The citation requirement: the strongest rule in this section.** On any answerable result, the final user-facing message must be constructible from the returned evidence package, and must surface citations — the candidates' `source_url` and `policy_links` — supporting its claims. Not a stylistic preference: the alternative, a strong general model producing confident, plausible, unsupported policy answers, is the single highest-risk failure mode against the accuracy target. Citation *format* varies by surface; citation *existence and traceability* is not negotiable. An implementation producing an uncited answer has violated this contract even when the wording happens to be correct. The model phrases the answer; it may not produce an ungrounded answer that looks grounded. Binds the agent instruction, the Block Kit defaults (§6), and — named honestly — the Section 03 §9 data-wiring task that populates `source_url`/`policy_links`, which this decision depends on. Reopens never.

**Decision 10 — The Slack latency target.** Under 4 seconds p95, user message receipt to final reply delivery. With the retrieval pipeline budgeted at 800–1000 ms (Section 01 Decision 9, revised June 10, 2026), client-side interpretation, the tool call, generation, and Slack transport share roughly 3 seconds — a real constraint on prompt length, tool-call chaining, token budget, and message construction, tightened by the retrieval envelope's raise and accepted because generation dominates either way. A sustained breach is an architecture-class event for this path. Binds prompt and rendering choices throughout. Reopens with Section 01 Decision 9.

**Streaming posture (fixed within Decision 6).** Every `InvokeInlineAgent` call carries `streamingConfigurations: {streamFinalResponse: true}` — the agent's final text arrives as `chunk` events rather than one consolidated block; the `returnControl` event itself stays sync and discrete; `applyGuardrailInterval` rests at its default (dormant — no Guardrail configured); streaming requires `bedrock:InvokeModelWithResponseStream` on the execution role. The MCP boundary stays sync regardless (structured envelope, no time-to-first-token to optimize); streaming is a rendering-layer property between the agent and Slack, delivered as rate-limited progressive `chat.update` calls ([API_12]).

### 4. Alternatives considered

**Slack as a thin forwarding wrapper.** Rejected: it forces Skywalker output to become user-facing prose, erases the evidence-versus-answer distinction, and leaves multi-turn interpretation homeless.

**Conversation state inside Skywalker.** Rejected; blurs the Section 01 boundary and makes a deterministic backend own what a conversational model does better.

**Both alias and explicit-scope tools on the Slack action group.** Rejected at launch: Slack holds no authoritative scope, so the explicit-scope tool would be one the model cannot honestly populate. It returns as an architecture question only if Slack gains a real scope source.

**Custom webhook service instead of an established framework.** Live only in the abstract; the realistic open choice is Bolt for Java versus any internally mandated framework (§9). The architectural point is that the subsystem lives comfortably in the program's Java-heavy environment, not the library name.

**Answering from model intuition on backend failure or abstention.** Rejected — it defeats the entire point of a scoped retrieval backend with explicit abstention. Phrasing is the model's; confidence theater is not.

**Three separate per-surface orchestration paths.** Rejected as architecture; normalize early, vary rendering only where Slack forces it.

**Sync single-message reply (the original posture).** Reversed in favor of streaming, with the reasoning pinned in [API_12]: total latency is unchanged, but time-to-first-token improves materially, and at a 4-second p95 target where model generation dominates, perceived latency is real value on a chat surface. Costs accepted: a more elaborate failure surface (mid-stream errors need a final-state overwrite) and a coarser cadence than SSE (Slack's `chat.update` rate limit caps frequency). The same rate-limited pattern is production-validated by other internal Bedrock-agent Slack bots.

### 5. Assumptions inherited from upstream

From Section 01: the boundary, tenets, latency budget, statelessness. From Section 02: the three-tool contract, the per-route identity channels (TA on this path), the error-model split the prompt depends on, and the employee-class scope vocabulary — resolved by PAPI on Skywalker's side; Slack never sees or synthesizes it. From Sections 03/04/06/07: the corpus, routing, UKB, and reranking/abstain layers as fixed subsystems below — the Slack layer never picks arms, compares scores, or invents abstain rules. Two principles bear specifically here: context-free answers are wrong-shaped (tenet 3), and Slack is the easiest surface on which to drift into casual genericity — tone may be conversational, identity-specific correctness may not; and structured abstention is healthy behavior the integration treats as normal (tenet 2).

### 6. End-to-end data flow

**Step one — event intake.** A slash command, DM, or channel mention arrives and is normalized into the turn object. Platform mechanics ([API_02], [API_03]): slash commands POST to the command's Request URL; mentions arrive as `app_mention` events and DMs as `message.im` events via the Events API; the initial `url_verification` challenge is echoed on endpoint configuration. Every inbound HTTP request is verified against the signing secret (HMAC-SHA256 over body plus timestamp, raw body read before JSON parsing — parsing first invalidates the check); stale or non-matching requests are rejected. Bot scopes: `commands`, `app_mentions:read`, `im:history`, `im:read`, `im:write`, `chat:write`, `users:read`, `users:read.email` (the last two supporting alias resolution through profile email). Secrets: `SLACK_BOT_TOKEN` (`xoxb-`), `SLACK_SIGNING_SECRET` (HTTP mode), `SLACK_APP_TOKEN` (`xapp-`, Socket Mode). HTTP mode versus Socket Mode remains a deployment decision (§9) — Socket Mode removes the public inbound endpoint, which matters for an internal-only service, and is the likely choice. The framework is Bolt for Java (`com.slack.api:bolt`, plus `bolt-servlet` or `bolt-socket-mode`): `app.command("/skywalker", ...)`, `app.event(AppMentionEvent.class, ...)`, `app.message(...)` filtered to IMs. Slack's 3-second ack rule is load-bearing for Decision 10: Bolt acks immediately, the user sees a working indicator, and retrieval work happens after ack on a background executor, with the grounded answer edited in when it lands.

**Step two — turn interpretation.** The model layer reads the current turn with the relevant Slack-side context. Follow-ups, references to earlier turns, and conversational framing are resolved here into a coherent current question — the backend is never asked to reconstruct conversation from fragments. Prompt assembly is layered: the stable behavioral instruction (Decision 6's four architectural facts) plus a per-turn layer carrying only what changed.

**Step three — tool selection.** Fixed: the action group declares only `skywalker_search_by_alias`, so the agent has no other tool to choose. Skywalker resolves country, level, and employee class from the alias through PAPI on its own side.

**Step four — the return-control loop's MCP call.** On the `returnControl` event, the application maps the function name to `skywalker.search.by_alias`, builds the `tools/call` request ([API_01]) with `arguments: {query_text, alias}` (the argument preserved for contract compatibility; not this path's canonical identity channel), initiates the TA token from the resolved alias plus `AgenticContext`, and POSTs to the gateway's CloudAuth route. The gateway validates inbound CloudAuth, authorizes against the Bindle resource, and forwards with OBO and the TA token preserved. Skywalker validates the TA token, reads the alias from TA claims, and proceeds through the Section 02 entry contract.

**Step five — Skywalker executes.** PAPI resolves scope; the gate routes; arms retrieve; candidates normalize and rerank; an answerable or abstain envelope returns — as a single sync JSON-RPC result through the gateway. Streaming lives one layer up.

**Step six — answer construction.** Strict internal order: read `result_kind` first, then the surviving evidence and link metadata, then compose — forcing backend-result-first, wording-second, the only reliable way to keep a strong model from sounding more certain than the package warrants. Answerable: the reply is written from the package with traceable citations per Decision 9; no claim may exceed the returned evidence. Abstain: an abstain-shaped reply preserving the backend's judgment, structurally distinct per Decision 7.

**Step seven — progressive delivery.** On receipt the bot posts a placeholder via `chat.postMessage` and captures its `ts`. The second `InvokeInlineAgent` (same `sessionId`, `returnControlInvocationResults` populated, streaming on) generates the final message; Bolt buffers `chunk` events and calls `chat.update` against the `ts` at a 2.5-second cadence (SSM-tunable, comfortably under Slack's ~1/sec/channel `chat.update` limit, avoiding flicker). The terminating event triggers the final `chat.update`: a complete Block Kit message — a `section` block with the answer body, a `context` block listing source titles and URLs drawn from the evidence candidates' `title` and `source_url` fields — satisfying Decision 9 without per-surface hand-rolled citation logic. Per-surface transport: slash commands use `response_url` (ephemeral by default, `in_channel` on explicit request, `chat.postMessage` past the 30-minute TTL); DMs use `chat.postMessage` to the IM channel; mentions thread under the trigger via `thread_ts`. On mid-stream error, the same final-state-write primitive lands a structurally distinct error message over any partial text. Per-surface rendering may differ — a DM tolerates a fuller body, a channel mention wants a tighter reply, a slash command suits the cleanest structure — and those are rendering decisions that correctly live here, after grounding, never before.

**Step eight — post-response capture.** For later SME review, the subsystem preserves a trace bundle close to runtime truth: the normalized request, the Skywalker route outcome, answer-or-abstain, and the final message the user saw — enough to distinguish retrieval problems from interpretation problems from phrasing problems. Not part of the online path; part of why the integration preserves backend traceability rather than flattening everything into one ephemeral message.

Each layer keeps one honest job: Slack handles interaction; Skywalker handles retrieval; the application translates between turns and tool calls; the backend translates between scoped requests and evidence judgments. Neither impersonates the other.

### 7. Failure behavior and abstain behavior

**Event-handling failure is not retrieval failure.** Verification, normalization, or routing failures fail at the Slack boundary with an integration-shaped message — never disguised as an evidence problem.

**Missing alias stops the request.** Alias is the identity signal; the subsystem fails explicitly rather than sending an under-scoped request and hoping the backend infers what was missing.

**TA failure is a system failure.** Per Decision 4's fail-closed posture: a missing or invalid TA token surfaces as a service failure, with initiator setup an explicit pre-launch responsibility — never a silent downgrade to argument-supplied identity.

**Backend-call failure is not permission to answer from intuition.** Gateway 403, AAA gaps, TA validation failure, 5xx, timeout — the return-control result carries the explicit failure indicator, and the prompt directs the model to render a system-failure message rather than answer from memory. The mid-stream variant overwrites partial text with the failure state.

**Backend abstention is not a Slack failure.** An abstain package converts to a user-facing non-answer that preserves the backend's judgment (Decisions 7 and 9 interact: abstain messages carry no fabricated citations because there is nothing to cite).

**Conversational ambiguity stays client-side.** When a turn is underspecified for a safe backend call, the correct shape is one more clarification turn — not silent tool use on guessed intent or scope. The backend is doing its job by insisting on questions specific enough to retrieve against responsibly.

Non-goals: Slack does not become a second retrieval system, bypass Skywalker, host PAPI logic, own thresholds or confidence scoring, or become a persistent backend session manager. It does not repair source truth — weak sources, missing links, or abstentions are presented well, never patched over by widening beyond the returned evidence. Honest translation, not cosmetic rescue. Evaluation-harness design stays out of scope (traceability is preserved for it); Slack-specific deep auth design stays out of scope (the gateway and TA own the auth path).

### 8. Calibration surfaces

**Surface one — the behavioral prompt.** Fixed that the model treats Skywalker output as grounded input; the wording that achieves it reliably is empirical. Re-litigate on repeated overstating of weak output, suppressed abstentions, or incoherent source use.

**Surface two — conversation-context depth.** Multi-turn lives client-side; how much history helps before stale turns degrade interpretation is empirical. Re-litigate on frequently misread follow-ups or history-induced drift.

**Surface three — the alias resolver.** The alias-crosses-the-boundary rule is fixed; the resolver implementation is tracked in [API_02]. Re-litigate if resolution proves unreliable or the resolver becomes a meaningful operational dependency of its own.

**Surface four — per-surface response shaping.** One orchestration, varied rendering. Re-litigate if a surface clearly needs different brevity, formatting, or source presentation to stay usable.

**Surface five — abstain presentation.** The backend decides abstention; the presentation is calibrated. Re-litigate if users systematically misread abstentions as crashes, refusals, or empty answers.

**Surface six — SME review feedback.** Repeated review findings legitimately reopen prompt behavior, tool-use guidance, and response shaping — that is the review loop working.

**Surface seven — grounded completeness versus conversational brevity.** Slack is a workplace surface; answers must be readable without trading away grounding. Re-litigate if reviewers find answers technically grounded but uselessly compressed, or pleasant but loose against what the backend supported.

**Surface eight — the streaming cadence** (2.5 s, SSM-tunable). Moves freely within Slack's rate limit against flicker and perceived-latency evidence.

### 9. Open questions

Most open questions across this series share one precondition, stated in full in Section 10 §9: they are only answerable against real user data at meaningful volume — a few hundred actual users, arriving with the September production launch. Until then, launch postures stand, and pre-launch pressure to move them resolves as a recorded non-change.

**HTTP mode versus Socket Mode** *(disclaimer: deployment decision, not architecture; the contract is identical either way).* Socket Mode's elimination of the public inbound endpoint makes it the likely choice for an internal-only service; confirmation awaits deployment-environment constraints.

**Framework mandate** *(disclaimer: working assumption is Bolt for Java; does not gate design).* Whether an Amazon-internal Slack framework is required instead.

**The Slack-user-to-alias resolver** *(the one Slack-side dependency that may need its own internal documentation before coding starts; tracked in [API_02]).* Most likely an internal directory service keyed by Slack user ID or profile email.

**Claude Sonnet 4.6 model ID and regional availability** *(disclaimer: operational pin, shared with Sections 05 and [API_05]).*

**Explicit-scope capability for Slack** *(disclaimer: not a launch question).* If the integration ever gains an authoritative scope source, adding the explicit-scope tool is a Decision 4 architecture question, not a calibration tweak.

**Per-surface formatting divergence and a dedicated sources affordance** *(disclaimer: UX questions; the citation rule holds under any treatment).* Whether surfaces should diverge further, and whether sources deserve an affordance outside the main answer body.

**The reviewer-facing trace bundle's exact shape** *(disclaimer: must exist per §6 step eight; field-level definition lands with the production review program, Section 10).*

### Closing position

The Slack subsystem is the first place Skywalker has to feel like a product instead of a backend — which is exactly why the line between retrieval and conversation must be sharper here than anywhere else. Skywalker remains the scoped retrieval backend behind MCP, reached through MCP Gateway on CloudAuth with OBO and TransitiveAuth carrying service and human identity. The Slack application remains the conversational layer: it normalizes three surfaces into one turn, interprets follow-ups, drives the inline-agent return-control loop with a single alias-based tool, composes grounded replies with non-negotiable citations, renders abstention as a structurally distinct healthy outcome, and streams progressively against a 4-second p95 budget. Multi-turn handling lives with the application; scoped retrieval and abstain judgment live with Skywalker. That split makes the backend deterministic, keeps the Slack surface useful, and gives the architecture one honest place for each kind of work.

---

*Stale-source flags raised in this section, for propagation: prior Section 08 `citations[]` reference in the Block Kit rendering (superseded — citations render from candidate `title`/`source_url`/`policy_links` per Section 02's envelope); prior Section 08 manager-versus-IC scope framing (superseded by employee class, Section 01 Decision 8); prior Section 08 SigV4-inbound transport (already superseded in the prior draft, re-confirmed per [API_14]); [API_05]'s SigV4 auth description for the orchestrator-to-gateway hop (superseded by CloudAuth OBO + TA).*

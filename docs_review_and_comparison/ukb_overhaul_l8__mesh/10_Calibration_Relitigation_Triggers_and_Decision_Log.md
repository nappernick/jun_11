## Section 10. Calibration, Re-litigation Triggers, and Decision Log

The earlier sections fixed the architecture itself. They defined what Skywalker is, what it owns, where it stops, how scope is resolved, how the controlled Top 50 FAQ corpus is ingested and indexed, how the online path routes requests, how UKB participates as the general arm, how both arms converge into one common reranking surface, and how the Slack and QuickSuite clients consume the backend. That body of work is now large enough that the final section cannot be a loose appendix about future tuning. It has to be the operating discipline that keeps the architecture coherent once implementation, production review, and real traffic begin to apply pressure.

This section exists because the project now contains two different classes of uncertainty and they cannot be handled the same way. Some parts of the design are fixed enough that changing them would alter subsystem boundaries, invalidate earlier sections, force coordinated contract changes, or materially change what Skywalker is. Those are architecture decisions. Other parts are intentionally empirical. They are thresholds, candidate budgets, fallback postures, publication details, and client-consumption refinements that can only be chosen honestly once the system sees judged examples, subject-matter review, and production behavior. Those are calibration surfaces. If both classes are handled with the same loose “we can revisit later” language, the document stops being a source of truth and becomes a collection of temporary opinions.

The purpose of this section is therefore not to reopen the whole system. It is to define how the system changes without drifting. That requires three things. First, the architecture needs a decision log that survives implementation pressure. Second, calibration surfaces need explicit baseline postures rather than hand-waving. Third, re-litigation needs triggers. The team should not have to guess whether a pressure point justifies a small calibration change, a disciplined non-change, or a real architectural reopening.

This section is therefore a real architecture section. It does not describe another runtime subsystem. It describes the rules by which the runtime subsystems remain authoritative.

### 1. What this section owns

This section owns the change-control discipline for Project Skywalker after Sections 01 through 09 have established the baseline architecture. In practical terms, that means it owns the difference between configuration-class change and architecture-class change, the record format for major decisions, the calibration surfaces already visible in the design, the classes of evidence that are allowed to pressure those decisions, and the review posture the team should apply once implementation and production feedback begin to accumulate.

It also owns the principle that architecture does not remain healthy by being rigid and does not remain healthy by being fluid. Both extremes fail. A document that refuses to revisit anything becomes ceremonial. A document that casually revisits everything becomes decorative. The role of this section is to keep the architecture hard where contracts and subsystem boundaries matter and deliberately empirical where real evidence is the only honest basis for choosing a number, a threshold, or a fallback rule.

This section owns the decision log as a program artifact rather than as a writing artifact. The log is not here merely to make the series feel complete. It exists because a system of this shape now has enough load-bearing seams that later implementation work will otherwise be forced to operate against half-remembered reasoning. The PAPI short-circuit path, the FAQ interception threshold, the daily polling baseline, the full-rebuild posture for the controlled corpus, the common reranking surface, the abstain package, the UKB normalization seam, and the client asymmetry between Slack and QuickSuite are all decisions that can be eroded accidentally if they are not recorded with both rationale and explicit triggers for reopening.

This section also owns the architecture-level meaning of re-litigation. Re-litigation does not mean that any engineer can reopen any subsystem whenever local implementation becomes inconvenient. It means a previously fixed decision is being deliberately reconsidered because one of three things has happened. Either the assumptions beneath the decision have changed, the runtime evidence has become strong enough to show that the decision is underperforming, or the surrounding system has evolved enough that the old decision is no longer compatible with the rest of the architecture. Re-litigation is therefore a controlled technical event, not a synonym for second-guessing.

This section does not own the subsystem details already fixed elsewhere. It does not re-specify the MCP contract from Section 02, the controlled FAQ-arm ingestion and storage design from Section 03, the online routing flow from Section 04, the UKB integration boundary from Section 06, the reranking and abstain contract from Section 07, or the client-specific responsibilities from Sections 08 and 09. It also does not own staffing, delivery sequencing, or project management. It is a technical-governance section, not a planning section.

A second non-ownership boundary matters as well. This section does not create permission for “experiments” that quietly bypass the architecture. If an experiment changes an MCP contract, index compatibility assumption, runtime route shape, or backend response package, that is not a harmless local test. It is an architecture event, and this section exists precisely so those events remain visible.

### 2. Inputs, outputs, and contracts

The first input to this section is the architecture already fixed by the earlier sections. That matters because the decision log is not supposed to invent a new system from scratch. It is supposed to preserve and govern the one that the earlier sections already established.

The second input is evidence. In this section, evidence is a narrower and more disciplined concept than “something someone noticed.” Acceptable evidence classes are representative judged queries, subject-matter review outcomes, structured production observations, measured latency or reliability behavior, corpus observations that falsify an earlier assumption, client-integration findings from Slack or QuickSuite that expose a contract mismatch, and implementation discoveries that prove a current design is no longer buildable as specified. Anecdotal discomfort can start a conversation. It is not enough by itself to reopen a major decision.

The third input is the set of calibration surfaces already visible across the series. By the time the reader reaches this section, the architecture already has known empirical surfaces: FAQ routing thresholds, abstain rules, reranker candidate budgets, final shortlist size, direct-scope usage conditions, rebuild publication behavior, client-side consumption of abstain packages, and several fallback-route postures. Those surfaces enter this section so they can be tuned under one discipline rather than being adjusted independently by whichever subsystem exposes the nearest configuration file.

The first output of this section is an explicit decision record. Every architecture-class decision should be representable as one durable record with a stable identifier, a statement of the decision itself, the sections it binds, the rationale for choosing it, the baseline currently in force, the class of evidence that would justify reopening it, and the current status. Status is not decorative. At minimum the log should support fixed, calibration-active, and open states. Fixed means the decision is current architectural truth and cannot be changed casually. Calibration-active means the architectural shape is fixed but the exact setting is intentionally empirical. Open means the architecture has deliberately not chosen yet.

The second output is a re-litigation packet. That packet is what the team creates when evidence is strong enough to challenge a current baseline. It should contain the decision identifier being challenged, the trigger that fired, the evidence attached to that trigger, the scope of impacted sections, whether the change is calibration-class or architecture-class, and the candidate replacement baseline. Without that packet, a “we should revisit this” discussion is just noise.

The third output is a recorded non-change. This is the governance-layer equivalent of backend abstention. If the evidence is not strong enough, or if the issue is local implementation discomfort rather than a design failure, the correct output of this section is not an improvised change. It is a documented decision to preserve the current baseline, record the pressure, and define what additional evidence would justify reopening it later.

The contract this section establishes is therefore simple to state. A pressure enters. It is bound to a known decision or calibration surface. The evidence is classified. The result is one of three legitimate outcomes: reaffirm the current baseline, tune a declared empirical surface, or reopen an architecture decision explicitly.

### 3. Fixed decisions

The first fixed decision is the system boundary itself. Skywalker is a retrieval backend behind an MCP boundary. It is not the conversational layer and it is not the final response-rendering layer. That boundary is fixed because multiple later sections already depend on it.

The second fixed decision is that identity-aware scoping is part of answer correctness rather than optional personalization. Location, level, and manager versus individual contributor are not decorative metadata. They are part of whether the answer is shaped correctly for the person asking.

The third fixed decision is that PAPI sits before retrieval unless the caller already provides authoritative scope through the explicit short-circuit path. Scope is retrieval infrastructure. It is not a late-stage hint.

The fourth fixed decision is that the backend supports three MCP entry modes overall: alias-based entry, employee-ID entry, and explicit-scope entry that bypasses PAPI. Different clients use those modes differently, but the shared boundary is now fixed.

The fifth fixed decision is the two-arm retrieval architecture itself. Skywalker continues to use a controlled Top 50 FAQ arm and a UKB-backed general arm rather than collapsing into one pure controlled corpus or one pure UKB wrapper.

The sixth fixed decision is that the Top 50 controlled arm remains retrieval-backed rather than cache-backed. The corpus is small, the rebuild posture is simple, and the system is intentionally control-oriented, but it is still an evidence-backed retrieval subsystem rather than a hardwired answer table pretending to be one.

The seventh fixed decision is that the controlled FAQ corpus is polled on a schedule and rebuilt cleanly on detected change. The current baseline is daily polling and full rebuild on source change because the corpus is tiny enough that simplicity is more valuable than elaborate partial-update machinery.

The eighth fixed decision is the runtime routing posture. A strong FAQ match stays on the FAQ-only path. A weaker match or a non-match widens the request into the dual-arm path where both the controlled FAQ arm and the UKB arm contribute candidates.

The ninth fixed decision is that both arms converge into one common reranking surface. Skywalker does not compare arm-local scores directly and does not ask the later client or agent to decide which arm should be trusted.

The tenth fixed decision is that abstention is a valid backend outcome. The system is allowed to decide that the evidence package it produced is too weak, too thin, too ambiguous, or too thinly supported by the surviving route to support an answer-shaped handoff.

The eleventh fixed decision is that multi-turn handling and conversation state live with the agent layers above Skywalker rather than inside the deterministic retrieval backend itself.

The twelfth fixed decision is that Slack and QuickSuite are intentionally asymmetric clients. Slack is the surface where the team is building more of the agent behavior. QuickSuite is the thinner MCP-consuming surface that remains conversational on QuickSuite's chat-agent runtime side.

The thirteenth fixed decision is that human subject-matter review is part of the early production calibration loop. A separate formal evaluation harness is not in scope for this series, but truth review is.

These decisions are fixed because changing them would invalidate earlier sections and require explicit rewrite work. That is exactly why the decision log exists.

### 4. Alternatives considered or still live

The first alternative is ad hoc tuning without a formal decision log. That model is attractive because it appears fast. Thresholds move whenever someone notices a problem, route behavior changes when local implementation gets uncomfortable, and the series is left behind as a static artifact that nobody expects to stay synchronized. This alternative is rejected. It is the fastest way to lose the value of the series as an engineering source of truth.

The second alternative is the opposite extreme: treat every choice made in Sections 01 through 09 as effectively permanent until some later rewrite replaces the whole architecture. This alternative is also rejected. Several parts of the design are intentionally empirical and cannot be finalized honestly before the system sees judged traffic and subject-matter review.

The third alternative is blanket periodic re-litigation, where every major decision is reopened on a fixed cadence regardless of whether its assumptions have broken or its triggers have fired. This alternative remains a live risk because teams often drift into it. Architecturally it is the wrong posture. It spends decision energy indiscriminately, blurs the difference between stable and unstable surfaces, and makes the system feel permanently provisional even where it should be firm.

The fourth alternative is trigger-based re-litigation. This is the posture the series adopts. Decisions remain in force until one of their explicit triggers fires. Those triggers can be quantitative, observational, integration-driven, or corpus-driven, but they must be named. This alternative is accepted because it preserves discipline without pretending the design is finished forever.

The fifth alternative is to keep one global log with no distinction between architecture and calibration. That option remains live only if the log is strongly typed. Without type information, “decision changed” is too vague to be useful in a system this shape.

The sixth alternative is to record only changes and never record explicit non-changes. That alternative is rejected because it deletes the rationale for why tempting changes were not taken and creates the false impression that the architecture simply failed to evolve.

The seventh alternative is to treat implementation inconvenience as sufficient evidence to reopen architecture. That alternative is rejected in principle. Implementation difficulty becomes architectural evidence only when it proves the design cannot be built as specified or reveals a hidden cost that materially changes the architecture’s operating assumptions. Mere inconvenience is not enough.

### 5. Assumptions inherited from upstream sections

This section inherits the full architecture established in Sections 01 through 09: the system boundary (Skywalker is a retrieval backend behind MCP, not a full conversational stack), the MCP contract and identity-scoping structure (alias path, employee-ID path, explicit-scope path, with the short-circuit available only when the caller already has authoritative values), the controlled FAQ-arm ingestion and storage model (daily polling, full rebuild on change, a single AOSS vector-collection evidence index using FAISS, two-step chunking over CoreX fragments), the static S3 variant set used by the routing gate (manually maintained, no pipeline), the online routing model (hybrid gate combining in-memory cosine similarity with a contingent Cohere Rerank v4 cross-encoder on a dedicated SageMaker endpoint; strong gate signal stays FAQ-only, weak signal widens into dual-arm; the FAQ arm itself runs hybrid BM25+FAISS retrieval fused by an OpenSearch search pipeline; both arms feed the common scoring layer), the UKB integration boundary (a dedicated seam rather than ad hoc downstream calls), the common reranking, candidate unification, and abstain design from Section 07 (routing threshold and abstain rule are already distinct decisions), and the client posture from Sections 08 and 09 (Slack and QuickSuite are both first-class MCP clients of Skywalker through Amazon MCP Gateway — Slack and the UAT inline-agent orchestrator on the **CloudAuth-inbound route** at `/ca/mcp/{registry}/{server}` with each orchestrator registered as a CloudAuth-modeled AAA application and **CloudAuth OBO + TransitiveAuth** carrying both service identity and human identity to Skywalker (D-38, API_14), QuickSuite on the Federate-OAuth-inbound route with a Federate Prod Service Profile and identity carried as MCP tool arguments because the Federate-inbound + CloudAuth-outbound combination has no published delegated-identity pattern (D-19); the gateway endpoint itself is a public DNS name with auth enforced at the gateway, and the architecture inherits a public-with-auth posture from the gateway's natural shape (D-40, API_13)).

Future pressure points will try to reopen these boundaries indirectly, especially through client-surface needs. The inherited assumption is that those pressures must be evaluated against the fixed architecture rather than allowed to dissolve it. This section also inherits the series-wide purpose: these documents are specific enough that later engineering and backlog work can be derived from them, which is why the distinction between fixed decisions, calibration surfaces, and re-litigation triggers matters so much here.

### 6. End-to-end data flow for this section

The data flow owned by this section is not a user request path. It is a design-governance path. It begins when one of the accepted evidence classes creates pressure against a current decision. That pressure can come from judged-query review, subject-matter feedback, production telemetry, client-integration findings, corpus observations, or implementation discoveries.

The first step is evidence capture. The observation has to be written in a form that makes the challenged decision identifiable. “Routing feels off” is not enough. “The strong-match FAQ-only threshold is producing too many FAQ-only routes that later abstain under review” is the right shape because it names the subsystem and the suspected failure pattern.

The second step is classification. The pressure is classified against an existing decision record or, if necessary, against a newly named decision that should have been recorded earlier. At this point the team determines whether the issue is calibration-class, architecture-class, or not yet strong enough to justify either.

The third step is evidence binding. The observation is attached to the decision record with its evidence class and with the explicit trigger it is attempting to satisfy. This is the step that prevents the program from turning every intuition into a redesign proposal.

The fourth step is re-litigation triage. If the trigger has not been met, the process abstains from change. The non-change is recorded, the current baseline remains in force, and the record specifies what additional evidence would be needed later. If the trigger has been met and the issue is calibration-class, the proposal moves forward as a controlled tuning change. If the trigger has been met and the issue is architecture-class, the process opens a wider re-litigation packet that names the impacted sections and contracts.

The fifth step is decision issuance. For calibration-class changes, that means selecting a new baseline value or rule, updating the log, and marking the affected subsystem sections as having a revised calibration baseline. For architecture-class changes, it means updating the decision record, rewriting the affected sections, and treating the change as a first-order architecture event rather than as a hidden patch.

The sixth step is publication. The decision log becomes the visible record of the new truth, and any implementation or backlog consequences flow from it. If the decision changed but the series and later implementation artifacts did not, then the decision was never really published.

The seventh step is stabilization. Once a decision has been recalibrated or re-litigated, the new baseline again becomes the active truth until a future trigger is met. This closes the loop and prevents permanent open-ended debate.

The important property of this flow is that it produces three valid outcomes rather than one. It can produce a change, it can produce a documented non-change, or it can produce an explicit open question that still lacks enough evidence to move in either direction. That is deliberate. The section needs to support abstention at the governance layer just as the runtime supports abstention at the evidence layer.

### 7. Failure behavior, abstain behavior, and non-goals that matter here

The first failure mode this section is meant to prevent is silent drift. That happens when a threshold, filter, route, or response behavior changes in a way that materially alters the system but is never named as either calibration or redesign. The whole point of the section is to make that pattern harder.

The second failure mode is false tuning. This is what happens when a contract-breaking change is labeled calibration because the word calibration sounds smaller and safer. Expanding the scope tuple without revisiting client contracts is not tuning. Moving conversational behavior into the backend is not tuning. Treating UKB-only fallback as a harmless route optimization would not be tuning. This section is supposed to catch those category errors.

The third failure mode is false finality. This is the opposite mistake. It occurs when every uncertain surface is frozen as though it were settled architecture and later engineers are made to feel they are violating doctrine merely by asking whether a threshold or shortlist size is still serving the system. The goal of this section is not to make the architecture rigid everywhere. It is to make it explicit where rigidity belongs and where it does not.

The fourth failure mode is governance theater. That is the condition where the program appears to have a decision discipline but never actually records non-changes, never updates baselines, and never ties the log back to implementation work. In that failure mode the document becomes performative rather than useful.

The abstain behavior for this section should be understood literally. When the evidence attached to a proposed change is not yet strong enough, the governance layer should abstain from changing the baseline. It should record the concern, preserve the current decision, and define what would justify reopening it later. This is not indecision. It is disciplined non-change.

The non-goals here matter because without them the section could metastasize into a vague management chapter that claims authority over everything. It does not own staffing, backlog priority, review-board process, runtime monitoring, delivery schedule, or client product strategy. It does not replace future quality tooling. It does not turn every unresolved issue into an open architecture crisis. And it does not promise that every threshold will be perfectly calibrated before launch.

A second non-goal is equally important. This section is not a substitute for judgment. A decision log and a set of triggers help teams reason better, but they do not remove the need for engineers and reviewers to think.

### 8. Calibration surfaces and what would cause re-litigation

The first calibration surface is the Top 50 strong-match routing threshold. The architecture is fixed that a sufficiently strong Top 50 FAQ match stays on the FAQ-only route, while a weaker result widens into the dual-arm path. What remains calibratable is the numeric threshold itself and any language-specific adjustments once judged traffic exists. Re-litigation is justified if production review or judged-query analysis shows that the threshold is sending too many borderline requests down the FAQ-only path or, in the opposite direction, failing to capture questions that should have stayed there.

The second calibration surface is the answerable-versus-abstain rule. The architecture is fixed that answerability is not the same as routing and that abstention is a valid backend outcome. What remains calibratable is the exact composite rule by which reranked evidence strength, degradation state, and shortlist shape become answerable versus abstain. Re-litigation is justified when review data shows that the backend is answering too aggressively on weak evidence or abstaining too aggressively on clearly supportable requests.

The third calibration surface is the variant set itself. The system depends on a static list of canonical Top 50 FAQ question phrasings for the routing gate's cosine first pass. The architecture is fixed that this is a real owned artifact in S3 (API_11) rather than a hidden side file, and that it is manually maintained rather than pipelined. What remains calibratable is the breadth, shape, and maintenance discipline of that variant list. Re-litigation is justified if recurring misses cluster around a specific subject area within the Top 50, revealing that the problem is not the routing thresholds alone but the representational coverage of the variant list.

The fourth calibration surface is the daily polling cadence for the controlled FAQ corpus. The architecture is fixed that the team chose schedule-driven simplicity rather than webhook complexity and that the corpus is small enough to justify it. What remains calibratable is whether daily is actually the right cadence. Re-litigation is justified if content freshness expectations, change frequency, or review findings show that the daily schedule is either unnecessarily stale or unnecessarily frequent for the practical update pattern.

The fifth calibration surface is the full-rebuild posture for the controlled corpus. The architecture is fixed that the corpus is small enough for full rebuild to be the preferred simplicity baseline. Re-litigation is justified only if the corpus stops being small in the way originally assumed or if publication behavior reveals that rebuild-time visibility, rebuild duration, or operational coupling now creates problems that the original design did not have to account for.

The sixth calibration surface is the defensive chunking assumption over CoreX fragments. The architecture is fixed that chunking remains in place because fragment size cannot be guaranteed. What remains calibratable is the degree to which this defense is actually needed. Re-litigation is justified if log evidence shows that fragments are almost never producing more than one chunk or, in the opposite direction, that they are frequently producing many chunks and therefore falsifying the assumption that the upstream fragment model is already close to answer-sized.

The seventh calibration surface is the size of the reranker candidate pool and the final evidence shortlist. The architecture is fixed that retrieval should over-fetch and that the common reranking layer should narrow that pool into a bounded evidence package. What remains calibratable is how wide the upstream candidate set needs to be and how narrow the final package should be. Re-litigation is justified if later review shows that the reranker is routinely starved of useful diversity or that the final shortlist is too small or too diffuse for downstream answer quality.

The eighth calibration surface is UKB result normalization. The architecture is fixed that UKB remains a black-box general arm and that its outputs must be normalized into Skywalker’s common candidate schema. What remains calibratable is how much of UKB’s returned structure should survive into candidate text and provenance metadata, and how much should be stripped before common reranking. Re-litigation is justified if later evidence shows that useful UKB context is being lost or that too much UKB-specific noise is polluting the common scoring surface.

The ninth calibration surface is usage of the explicit-scope MCP path by clients that already hold authoritative scope. The architecture is fixed that the path exists and that clients may use it deliberately. What remains calibratable is whether that path should remain exceptional or become common for one or both clients. Re-litigation is justified if integration work shows that a client routinely possesses authoritative scope and is paying unnecessary PAPI cost and failure exposure on most requests.

The tenth calibration surface is single-arm fallback route behavior. The architecture is fixed that the backend distinguishes normal answerable, fallback-route answerable, and abstain outcomes. What remains calibratable is how conservative the fallback path should be in practice. Re-litigation is justified if the fallback path proves either too timid to be useful or too permissive to be trustworthy.

The eleventh calibration surface is client-side consumption of backend abstention. The architecture is fixed that Skywalker returns structured abstain results and that Slack and QuickSuite should preserve the distinction between abstention and outage. What remains calibratable is the exact client behavior and presentation pattern above that boundary. Re-litigation is justified if subject-matter review shows that the clients are systematically oversmoothing, misframing, or effectively hiding backend abstention.

The twelfth calibration surface is the decision log itself. This may sound circular, but it is necessary. The current baseline is that the log records fixed decisions, calibration-active decisions, explicit non-changes, and open questions. Re-litigation is justified if the log becomes too light to be useful, too heavy to maintain, or too detached from actual implementation work to serve as a real control artifact.

### 9. Open questions, if any

The first open question is the exact operational home of the decision log. The architecture is fixed that the log must exist as a durable engineering artifact. What remains open is whether the authoritative representation should live primarily inside this document series, in a repository artifact, or in a synchronized dual form.

The second open question is the exact review cadence for production-era re-litigation. Trigger-based review is the adopted posture, but the project still needs an implementation-level answer for how often accumulated triggers and non-change records are formally reviewed.

The third open question is the minimum evidence bar for architecture-class change versus calibration-class change in mixed cases. Some pressures will begin as threshold complaints and later turn out to be contract complaints. The section defines the distinction, but the exact governance process for mixed cases is still open.

The fourth open question is how tightly subject-matter review should be wired into the log once the system is in production. Human-in-the-loop review is already in scope. The remaining question is how that review should be translated into decision-record updates in a disciplined, low-friction way.

The fifth open question is whether the team eventually needs a formally separated “proposed but not accepted” registry beyond the normal non-change records. The current design assumes that explicit abstained changes recorded against decision IDs may be enough. That may prove sufficient. It remains open.

### 10. Initial decision register

The initial decision register should capture the decisions that later change requests are most likely to pressure. It does not need to restate every sentence of every section. It needs to preserve the architectural commitments that implementation and later production pressure are most likely to distort.

**D-01. Skywalker is a retrieval backend behind MCP.**  
**Status:** Fixed.  
**Decision:** Skywalker stops at scoped retrieval, evidence selection, and backend answerability. It is not the conversational layer.  
**Rationale:** Multiple later sections depend on this boundary, especially the Slack and QuickSuite integration sections.  
**Trigger to reopen:** Re-litigation would be justified only if the product deliberately moved conversational state or response rendering into Skywalker itself.

**D-02. Identity-aware scoping is part of correctness.**  
**Status:** Fixed.  
**Decision:** Location, level, and manager-versus-IC are part of whether the answer is shaped correctly.  
**Rationale:** Context-free answers are wrong-shaped in this domain, not merely less personalized.  
**Trigger to reopen:** Re-litigation would be justified only if repeated review showed that the active scope tuple is insufficient for correctness.

**D-03. PAPI runs before retrieval unless authoritative scope is already supplied.**  
**Status:** Fixed.  
**Decision:** Scope is resolved before search through PAPI or provided directly by a trusted caller through the explicit-scope path.  
**Rationale:** Scope is retrieval infrastructure, not post-processing.  
**Trigger to reopen:** Re-litigation would be justified only if clients routinely hold authoritative scope already and the default path becomes structurally wasteful.

**D-04. Three MCP entry modes exist.**  
**Status:** Fixed.  
**Decision:** The shared backend contract supports alias-based entry, employee-ID entry, and explicit-scope entry.  
**Rationale:** Different clients naturally possess different identity material, and the system has already been designed around that fact.  
**Trigger to reopen:** Re-litigation would be justified only if a new client or new trust model required a fundamentally different entry shape.

**D-05. Skywalker uses two retrieval arms.**  
**Status:** Fixed.  
**Decision:** The architecture combines a controlled Top 50 FAQ arm with a UKB-backed general arm.  
**Rationale:** Control matters most where answer quality is judged most tightly, while UKB broadens general coverage.  
**Trigger to reopen:** Re-litigation would be justified only if one arm stopped justifying its cost or if the dual-arm model itself ceased to fit the domain.

**D-06. The controlled FAQ arm is retrieval-backed, not cache-backed.**  
**Status:** Fixed.  
**Decision:** The owned FAQ subsystem remains a tiny retrieval corpus rather than a table of hardwired final answers.  
**Rationale:** The system is designed around controlled evidence and controlled indexing, not semantic dispatch onto static answers.  
**Trigger to reopen:** Re-litigation would be justified only if the Top 50 space no longer behaved like a retrieval problem at all.

**D-07. The controlled FAQ corpus is polled daily and rebuilt cleanly on change.**  
**Status:** Fixed baseline.  
**Decision:** Source change is detected through polling, and changes trigger a clean rebuild of the tiny controlled corpus.  
**Rationale:** The corpus is small enough that simplicity is worth more than elaborate incremental mutation.  
**Trigger to reopen:** Re-litigation would be justified only if the corpus stopped being small or if publication behavior made clean rebuild materially problematic.

**D-08. Strong FAQ match stays FAQ-only; otherwise both arms run.**  
**Status:** Fixed.  
**Decision:** A strong Top 50 match remains on the FAQ-only route. A weaker result widens into the dual-arm route.  
**Rationale:** This captures controlled questions efficiently while still letting broader evidence compete when the FAQ signal is not strong enough.  
**Trigger to reopen:** Re-litigation would be justified only if the route model itself changed enough that one of those branches no longer existed independently.

**D-09. Both arms converge into one common reranking surface.**  
**Status:** Fixed.  
**Decision:** FAQ-arm and UKB-arm candidates are normalized and scored together on one common surface rather than compared by arm-local scores.  
**Rationale:** Arm-local scores are not comparable. Common reranking is what makes the two-arm model coherent.  
**Trigger to reopen:** Re-litigation would be justified only if the common candidate surface or common reranking layer proved structurally insufficient.

**D-10. Abstention is a valid backend outcome.**  
**Status:** Fixed.  
**Decision:** Skywalker may return a structured abstain package when evidence is too weak, too thin, too ambiguous, or too thinly supported by the surviving route.  
**Rationale:** Backend honesty requires a non-answer outcome.  
**Trigger to reopen:** Re-litigation would be justified only if product requirements deliberately forced the backend to answer even when evidence was weak.

**D-11. Multi-turn handling lives with client agents.**  
**Status:** Fixed.  
**Decision:** Conversation memory and multi-turn interpretation belong to Slack- and QuickSuite-side agents, not inside Skywalker.  
**Rationale:** Skywalker is a deterministic retrieval backend. Slack and QuickSuite are the surfaces that interpret and continue conversation.  
**Trigger to reopen:** Re-litigation would be justified only if the product deliberately moved conversation-state ownership into Skywalker.

**D-12. Both client surfaces are alias-first; identity channel differs by auth combination.**  
**Status:** Fixed.  
**Decision:** Slack and QuickSuite both enter Skywalker through the alias-based MCP path because both surfaces naturally hold the alias rather than a pre-resolved scope tuple. The employee-ID and explicit-scope paths remain in the core MCP contract for any future caller that holds those richer identifiers but are not the default for either launch client. **The channel through which the alias arrives at Skywalker differs by auth combination.** On the Slack and UAT paths (CloudAuth-inbound at the gateway, see D-38), the orchestrator initiates a TransitiveAuth token carrying the alias, and Skywalker reads the alias from the validated TA claims server-side. On the QuickSuite path (Federate-inbound at the gateway, D-19), MCP Gateway has no published delegated-identity pattern for the Federate-inbound + CloudAuth-outbound combination, so the chat-agent integration supplies the alias as `arguments.alias` instead. The MCP tool's `arguments.alias` field stays in the contract for QuickSuite and any future non-TA caller; it is preserved as a fallback for Slack and UAT only when TA propagation fails (treated as a system-failure error rather than silent degradation, per D-39).  
**Rationale:** Slack supplies the user's Slack ID, which the orchestrator resolves to the Amazon alias. QuickSuite supplies a Federate JWT whose `sub` claim is the user's Midway login. Both surfaces therefore enter through the same alias mode, and PAPI's `peopleSearchV3` resolves the scope triple identically on Skywalker's side. The channel asymmetry exists because the Slack and UAT orchestrators speak CloudAuth (where MCP Gateway supports OBO + TA natively) while QuickSuite speaks Federate OAuth (where the gateway does not yet propagate identity).  
**Trigger to reopen:** One of the client integrations fundamentally changes the identity material it carries naturally (for example, QuickSuite adds scope claims to its Federate JWT that Skywalker could trust as authoritative); or MCP Gateway publishes a delegated-identity pattern for the Federate-inbound + CloudAuth-outbound combination (at which point QuickSuite could move to the TA channel as a calibration event, see API_14).

**D-13. Human subject-matter review is part of the launch calibration loop.**  
**Status:** Fixed.  
**Decision:** Human-in-the-loop review is part of real production calibration even though a separate formal evaluation harness is out of scope for this series.  
**Rationale:** The project still needs disciplined truth review.  
**Trigger to reopen:** Re-litigation would be justified only if production validation shifted to a different formal quality regime.

**D-14. Embedding model is Cohere Embed v4.**  
**Status:** Fixed.  
**Decision:** The owned FAQ evidence corpus, the live query at runtime, and the static variant set held in S3 for the routing gate are all embedded under Cohere Embed v4. A single live query embedding serves both the evidence retrieval surface and the in-memory cosine comparison against variants.  
**Rationale:** Shared embedding contract prevents vector-space drift between the routing-gate first pass and the evidence surface.  
**Trigger to reopen:** A deliberate migration to a different embedding model, which forces a coordinated regeneration of the variant S3 artifact, a coordinated rebuild of the evidence index, and a coordinated code change in the runtime — and is therefore an architecture-class event.

**D-15. Evidence reranker is Cohere Rerank 4 Pro on SageMaker.**  
**Status:** Fixed.  
**Decision:** The common scoring surface uses Cohere Rerank 4 Pro on SageMaker `ml.p5.4xlarge`. Detail in D-25.  
**Rationale:** The two-arm convergence model depends on one common reranker, and its 32K-token context window directly enables the 1000-token chunk ceiling and the 20-candidate pool size. See D-25 for hosting and D-32 for the separate gate-stage-2 reranker.  
**Trigger to reopen:** A capability gap, operational change, or deliberate reranker migration.

**D-16. System-level latency budgets.**  
**Status:** Fixed.  
**Decision:** The Skywalker retrieval pipeline targets 250 to 450 milliseconds p95 end-to-end within the backend (reranker ≈250–350 ms, everything else ≈100 ms). The Slack surface targets under 4 seconds p95 from user message receipt to final Slack reply.  
**Rationale:** Users abandon slow conversational surfaces, and concurrency, timeouts, and generation token budgets are all shaped by these numbers. The retrieval budget reflects the H100-hosted reranker's p95 ceiling on 20K-token payloads.  
**Trigger to reopen:** A sustained production breach that cannot be resolved inside the existing subsystem boundaries, or a change in surrounding product requirements.

**D-17. Slack citation requirement.**  
**Status:** Fixed.  
**Decision:** On any answerable backend result, the Slack application's final user-facing message must include traceable citations to the evidence package returned by Skywalker.  
**Rationale:** The 95 percent accuracy target on Top 50 cannot survive a client layer that freelances confident, unsupported answers. Citation is the integrity seam of the user-facing surface.  
**Trigger to reopen:** A deliberate product change that replaces citation with a different verifiable grounding discipline. Operational inconvenience is not sufficient.

**D-18. Slack orchestration shape.**  
**Status:** Fixed.  
**Decision:** The Slack application orchestrates turns using Amazon Bedrock Inline Agents (`InvokeInlineAgent`) with a single action group whose executor is `customControl: RETURN_CONTROL`. The return-control handler performs a real MCP `tools/call` against Skywalker's core MCP surface. The Bedrock Converse API is not used on this path.  
**Rationale:** Inline agents give us agent behavior (tool choice, clarification turns, grounded composition) without coupling to Lambda-backed action groups. RETURN_CONTROL keeps tool execution in the Slack application's JVM where it can speak directly to Skywalker over MCP, preserving decision three in Section 08.  
**Trigger to reopen:** Bedrock deprecates inline agents, or inline agents' preview status is unacceptable for the launch timeline, or measured latency shows the inline-agent loop cannot fit the 4-second p95 budget.

**D-19. QuickSuite via Amazon MCP Gateway on the Federate-inbound route.**  
**Status:** Fixed.  
**Decision:** QuickSuite consumes Skywalker through **Amazon MCP Gateway** on its Federate-OAuth-inbound route at `https://api.mcp.asbx.aws.dev/federate/mcp/{registry-id}/{skywalker-server-id}`. There is no wrapper Lambda, no AgentCore Gateway, no REQUEST interceptor, and no AppConfig CR equivalent on this path. QuickSuite reads Skywalker's actual `tools/list` directly through the gateway and chooses among the three core MCP tools (`skywalker.search.by_alias`, `skywalker.search.by_employee_id`, `skywalker.search.by_explicit_scope`) based on what identity material the QuickSuite chat-agent integration is configured to supply. Inbound auth is Federate OAuth (Authorization Code with PKCE) against a Federate Prod Service Profile created using the "AWS QuickSuite Action Connectors" pre-approved use case. MCP Gateway validates the JWT, performs Bindle-based authorization on `MCPGateway::{skywalker-server-id}`, and converts Federate inbound to CloudAuth outbound on the gateway-to-Skywalker leg. **Identity is carried by the integration as MCP tool arguments**, not propagated by the gateway, because MCP Gateway's published delegated-identity patterns (TransitiveAuth, FAS, UnifiedAuth, Midway A5) are all flagged "in progress" on the BuilderHub MCP Gateway concepts page for the Federate-inbound + CloudAuth-outbound combination. This is the same gateway product Slack (D-17/D-18) and the UAT inline-agent orchestrator (Section 05) use; the three production paths share one transport, one MCP server, and one Bindle authorization surface.  
**Rationale:** BuilderHub's [Integration with QuickSuite](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/howto-quicksuite-client/) guide documents this as the supported QuickSuite-to-internal-MCP path. Operating all three production paths on MCP Gateway gives the Skywalker MCP server one auth-onboarding story and one Bindle-permission surface rather than three. Removing the wrapper Lambda eliminates a deployment surface and a translation seam the architecture does not need now that QuickSuite can read Skywalker's actual `tools/list` directly. The earlier-draft v2 architecture (a Skywalker-owned plug-in wrapper hosted under Bedrock AgentCore Gateway with a REQUEST interceptor Lambda extracting identity claims from the JWT) is preserved in git history but is not the active architecture.  
**Trigger to reopen:** MCP Gateway's published delegated-identity patterns (TransitiveAuth, FAS, UnifiedAuth, Midway A5) ship for the Federate-inbound + CloudAuth-outbound combination, at which point identity carriage could move from arguments to header-level propagation; the QuickSuite team materially changes its supported integration model; a third external client with conventions that MCP Gateway does not natively support emerges and forces the wrapper-Lambda decision back open.

**D-20. MCP protocol version at launch.**  
**Status:** Fixed.  
**Decision:** Skywalker targets MCP protocol revision `2024-11-05`. This matches the version current QuickSuite MCP-connector implementations support today and that Amazon MCP Gateway carries through transparently.  
**Rationale:** Launching on the version every production client and the gateway already support keeps the launch surface coherent. Advancing requires a coordinated update across MCP Gateway, the Skywalker MCP server, and any client expectations.  
**Trigger to reopen:** QuickSuite advances its supported MCP revision and the upgrade buys real capability, at which point Skywalker can advance deliberately.

**D-21. Embedding model and vector dimension.**  
**Status:** Fixed.  
**Decision:** The owned FAQ arm embeds the evidence corpus using Amazon Bedrock's Cohere Embed v4 (`cohere.embed-v4:0`) at dimension 1024. The Amazon OpenSearch Serverless (AOSS) vector collection stores these vectors using the **FAISS** engine with `cosinesimil` space and HNSW indexing (`m: 24`, `ef_construction: 128`). The static variant set in S3 (API_11) is also pre-embedded under Cohere Embed v4 at dimension 1024 so the live query vector can be compared against both surfaces without drift.  
**Rationale:** AOSS vector collections support FAISS as their k-NN substrate (Lucene/nmslib is not an AOSS option), and FAISS HNSW with `efficient_filter` clauses gives correct scope pre-filtering inside the k-NN query. Sharing one embedding contract across the evidence index, the variant S3 artifact, and the query side lets the runtime compute the query vector once and reuse it across the routing gate's cosine step and the FAQ-evidence k-NN leg.  
**Trigger to reopen:** Cohere issues a successor model whose migration value exceeds the coordinated rebuild cost (evidence index + variant S3 artifact + runtime), or AOSS exposes a different supported vector engine whose properties materially outperform FAISS for our workload.

**D-22. Ingestion state storage.**  
**Status:** Fixed.  
**Decision:** The daily ingestion job persists exactly one piece of state between runs per corpus: the CoreX snapshot marker from the last successful publish, held in an AWS Systems Manager Parameter Store parameter at `/skywalker/ingestion/{corpus_id}/last_snapshot_marker`. There is no external state table, no publish-status record, and no revision counter. Every other piece of "state" is derivable from the AOSS collection itself: the live version is whatever `faq_<corpus>_current` aliases, previous versions are whichever versioned indexes still exist under the retention window, and publish status is implicit — alias points at the new version or it doesn't.  
**Rationale:** State management is a source of complexity and bugs. A tiny corpus published by a single daily writer does not need transactional multi-row state. The high-water mark is the only thing that cannot be reconstructed from the collection, so it is the only thing we persist elsewhere. SSM Parameter Store is effectively free at this scale and requires no schema, no capacity planning, and no separate deployment.  
**Trigger to reopen:** Ingestion becomes multi-writer or multi-tenant, rollback frequency grows high enough that a two-step operator action is untenable, or the need for durable publish history (beyond what AOSS indexes and CloudWatch logs provide) becomes a real operational requirement.

**D-23. AOSS alias swap for publication atomicity.**  
**Status:** Fixed.  
**Decision:** Live retrieval reads through an AOSS index alias (`faq_evidence_current`) inside the `skywalker-faq-{stage}` vector collection. The ingestion job builds `faq_evidence_v<N+1>` in full, validates it, and atomically moves the alias from `v<N>` to `v<N+1>` via the OpenSearch `_aliases` API (which AOSS supports on vector collections) before updating the SSM high-water-mark parameter.  
**Rationale:** The alias swap is the single atomicity primitive. Ordering the alias swap before the SSM parameter update means a failure between them produces at worst a redundant rebuild on the next run, never a divergent live state. Partial state never becomes the live truth, per Section 03 §3 decision nine.  
**Trigger to reopen:** The corpus grows beyond a scale where whole-index rebuild is operationally acceptable.

**D-24. UKB invocation shape.**  
**Status:** Fixed.  
**Decision:** Skywalker calls UKB through its `retrieve` MCP tool over the stage-specific `iam/v1/mcp` endpoint, authenticating via AWS IAM SigV4 after assuming a `kbs-mcp-role_{stage}_{client_id}` cross-account role. The request carries the resolved employee as `targetUser`, leaves `additionalFilters` empty at launch, and accepts the `content[]` response with `type: "resource"` as the general-arm evidence.  
**Rationale:** Matches UKB's documented contract and preserves the Section 06 black-box boundary — Skywalker does not attempt to shape UKB's internals.  
**Trigger to reopen:** UKB deprecates v1, changes the authentication model, or exposes materially different contract shapes that affect the Section 06 seam.

**D-25. Reranker model and hosting.**  
**Status:** Fixed.  
**Decision:** The common reranking surface is Cohere Rerank 4 Pro (`rerank-v4.0-pro`), self-hosted on Amazon SageMaker using `ml.p5.4xlarge` (H100) real-time endpoints. Production runs a 2-instance always-on fleet for HA; beta runs 1 instance. Subscribed via AWS Marketplace listing prodview-du2svpomxs5vw.  
**Rationale:** The 32K-token context window removes the chunk-size pressure Rerank 3.5's 4096-token window imposed on FAQ and UKB candidates. The H100 instance is what makes sub-300 ms p50 latency feasible on 20K-token payloads; A10G SKUs (`ml.g5.2xlarge`) push p50 to 600–900 ms and break the Skywalker p95 budget. SageMaker hosting keeps the reranker in our VPC with no egress to Cohere's backend, matching the internal-network posture the rest of the system assumes.  
**Trigger to reopen:** Cohere deprecates Rerank 4 Pro, or a successor model's measured quality materially outperforms it at comparable cost, or AWS Marketplace changes the supported instance types in a way that affects our deployment. Cost alone is not a trigger; SageMaker was chosen with full knowledge of the fixed-capacity cost model and will not be revisited on per-call economics.

**D-26. Chunker architecture.**  
**Status:** Fixed.  
**Decision:** Ingestion chunks source content in two discrete, independently-testable steps. Step 1: `HierarchicalChunker` splits the normalized CoreX snapshot on structural hierarchy (headings, sections, coherent blocks) and assigns a `parent_id` UUID to each unit, with no regard for token count. Step 2: `SemanticChildSplitter` consumes each hierarchical unit and produces children respecting a 1000-token hard ceiling, preferring paragraph and sentence boundaries and only forcing mid-content cuts when the ceiling requires it. Each child carries `parent_id`, `chunk_id`, `child_order`, `child_count`, `split_type`, and the child's own text and embedding. Parents are not stored as separate documents.  
**Rationale:** Two narrow components are easier to reason about, test, and evolve than one monolithic chunker that mixes structural and sizing concerns. The 1000-token ceiling matches the reranker's practical budget (20 candidates × ~1000 tokens comfortably fit the 32K context window with room for the query). Semantic splitting inside the ceiling preserves answer coherence better than naive token-count splitting. No parent duplication keeps storage minimal and eliminates the sync-drift risk between parent and child copies.  
**Trigger to reopen:** Empirical evidence that 1000 tokens is systematically wrong (children routinely too noisy at that size, or parents too fragmented for good answers), or the semantic splitter's output quality breaks down on a material subset of CoreX content, or the chunker's two-step composition creates a measurable operational cost that a unified chunker would avoid.

**D-27. Post-rerank sibling and linked-parent expansion.**  
**Status:** Fixed.  
**Decision:** After reranking returns the top N child chunks, Skywalker recovers parent context by issuing one AOSS `terms` filter query against the union of two sets of parent_ids: the winners' own `parent_id` values, and the parent_ids in each winner's `linked_parent_ids` array (the precomputed depth-2 chain materialized at ingest from the COREx custom "next" field per D-35). The query carries no scope filter — sibling expansion within an anchor's own parent inherits scope from the original retrieval, and linked-parent expansion is curatorial (per D-35). Client-side, the system groups returned children by `parent_id`, sorts each group by `child_order`, and concatenates the `text` fields using a separator derived from `split_type` (`"\n\n"` on `paragraph_boundary`, empty string on `size_forced`). For each anchor, the rendered evidence text is the anchor's reconstructed parent text followed by each linked Q&A's reconstructed parent text in chain order, separated by `"\n\n"`, with one Unicode superscript citation marker (`¹`, `²`, `³`) at the end of each segment; the anchor's MCP envelope record carries a parallel `citations[]` field whose entries match the markers and resolve to `{marker, source_id, title, source_url, policy_links}` per segment (per D-37). If a parent's returned sibling count is less than its recorded `child_count`, the reconstruction utility returns the present subset in order, logs a structured warning, and emits a CloudWatch metric. If a linked Q&A returns no chunks at all (depublish race, missing children), the linked segment is dropped from concatenation and `LinkedItemSuppressed` fires; the answer still goes to the user. `LinkedItemSuppressed` does not fire for scope mismatch — author-asserted contextual relationships override the scope filter on the expansion query specifically.  
**Rationale:** Rerank on small chunks keeps the cross-encoder signal-to-noise high. Answer with parent-sized context keeps the downstream agent grounded. Doing both without duplicating storage is the reason children carry `parent_id` and the reconstruction happens at query time. The same query also handles linked-parent expansion because chains are materialized at ingest into a per-chunk `linked_parent_ids` field — runtime stays at one terms query whose list is just longer, not a graph walk. Best-effort degrade on missing siblings or unresolved linked Q&As is the right failure mode because a storage-level inconsistency is a maintenance concern, not a user-facing one.  
**Trigger to reopen:** Sibling-or-linked-parent queries become a measurable latency regression (current estimate: <10 ms against a tiny corpus), or missing-sibling/missing-link events become frequent enough to mask real quality issues in reviewer trace data, or the carve-out from scope filtering on the expansion query (per D-35) starts producing user-visible answers actively wrong for the requester's context.

**D-28. SageMaker HA posture.**  
**Status:** Fixed.  
**Decision:** Production reranker capacity is two always-on `ml.p5.4xlarge` SageMaker endpoints across different AZs. Beta runs one always-on endpoint. Auto-scaling is not used because SageMaker cold start (~3–5 minutes) is longer than any burst window we expect. Endpoint rollovers use SageMaker deployment guardrails with blue/green traffic shifting; production rollover that drops below one healthy endpoint is an incident.  
**Rationale:** Skywalker's projected peak QPS (~2.3 at 50K searches/day) is well within a single instance's capacity ceiling, so a second instance exists for availability rather than throughput. Fixed fleet sizing is simpler and more predictable than reactive auto-scaling that cannot react quickly enough anyway.  
**Trigger to reopen:** Sustained QPS above ~8–10 (roughly half the H100 single-instance estimated ceiling), or a cheaper HA strategy becomes available on the SageMaker side.

**D-29. Variant set as a static S3 artifact.**  
**Status:** Fixed.  
**Decision:** The list of canonical Top 50 FAQ question phrasings used by the routing gate lives as a pre-embedded JSON file in S3 (API_11). No ingestion pipeline, no AOSS index, no SSM manifest for variants. The file is maintained by hand when the team decides variants should change, and loaded into memory at service boot. Hard-fail on boot if the file is missing or malformed.  
**Rationale:** The variant list changes on a human cadence, not a content-system cadence. Building a pipeline for a tiny static artifact is the wrong complexity trade. Pre-embedding at authoring time means zero embedding calls at service boot and zero drift risk between the variant text and its vector. Hard-fail rather than fallback because silently starting with an empty variant list would route every query to dual-arm — a hidden quality regression.  
**Trigger to reopen:** The variant list starts changing often enough that manual S3 updates become operationally painful, or the team wants a programmatic authoring flow that justifies a small pipeline.

**D-30. Hybrid routing gate.**  
**Status:** Fixed.  
**Decision:** The routing gate is a two-stage hybrid. Stage 1 is an in-memory cosine similarity comparison between the query embedding (Cohere Embed v4, `search_query`) and the pre-embedded variant set from S3. Stage 2 is a Cohere Rerank v4 cross-encoder call against the variants on a dedicated SageMaker endpoint (see D-32), invoked **only** when the top stage-1 cosine score lands in the ambiguity band between configurable low and high thresholds. On confident cosine results, stage 2 is skipped entirely. On transport failure or timeout of stage 2, the gate falls through to dual-arm without treating the failure as an error.  
**Rationale:** Cosine is cheap and fast but known to miss paraphrase, negation, and entity substitution. Cross-encoder rerank is accurate but adds 100–200 ms per query and we don't want to pay that on every request. Shape B (contingent rerank in the ambiguity band) pays the cross-encoder cost only where it helps most. The gate's failure mode is designed to be benign — a widening to dual-arm, not a system-level error.  
**Trigger to reopen:** Measured gate accuracy shows that Shape A (always rerank) would materially improve quality at acceptable latency cost, or that Shape B's contingent rerank rarely changes the stage-1 decision and is therefore not earning its latency.

**D-31. Control-plane values in SSM Parameter Store.**  
**Status:** Fixed.  
**Decision:** Every tunable runtime threshold, timeout, candidate budget, and shortlist size lives in AWS Systems Manager Parameter Store under `/skywalker/runtime/`. The service reads these parameters with a periodic refresh loop (launch default: 60-second polling) and emits a CloudWatch metric on each read so dashboards and audits can see which values were in effect at any given time. Architecture-class values (embedding dimension, HNSW `m` and `ef_construction`, model IDs, instance types, SageMaker endpoint ARNs) are **not** in SSM — they require code or deployment changes and are governed by the decision register rather than by runtime config.  
**Rationale:** Tuning without a redeploy is essential for calibration surfaces like the routing gate thresholds, the abstain floor, UKB timeout, and per-arm candidate budgets. Keeping them all under one SSM prefix (`/skywalker/runtime/`) with a standard read/refresh pattern makes operator mental-models simple and makes control-plane changes auditable. Keeping architecture-class values out of SSM prevents accidental runtime changes to things that require coordinated rebuilds.  
**Trigger to reopen:** SSM Parameter Store throughput limits become a real constraint (unlikely at Skywalker scale), or the team wants a richer config surface (AppConfig, typed configuration service, feature flagging) for a reason that SSM cannot meet.

The authoritative list of control-plane parameters at launch is in API_07 §"Control plane." Summary:

- `/skywalker/runtime/gate/cosine_low_threshold` — 0.30
- `/skywalker/runtime/gate/cosine_high_threshold` — 0.80
- `/skywalker/runtime/gate/rerank_floor` — 0.50
- `/skywalker/runtime/gate/rerank_timeout_ms` — 200
- `/skywalker/runtime/abstain/floor` — 0.30
- `/skywalker/runtime/retrieval/ukb_timeout_ms` — 300
- `/skywalker/runtime/retrieval/per_arm_candidate_budget` — 10
- `/skywalker/runtime/retrieval/shortlist_size` — 5
- `/skywalker/runtime/retrieval/knn_overretrieve_k` — 40
- `/skywalker/runtime/retrieval/hybrid_bm25_weight` — 0.30
- `/skywalker/runtime/retrieval/ef_search` — 100
- `/skywalker/runtime/rerank/evidence_timeout_ms` — 350

Every launch default above is a starting point derived from first-order estimates, not a commitment. All are expected to move against judged-traffic calibration.

**D-32. Routing gate Stage 2 reranker is Cohere Rerank v4 on dedicated SageMaker endpoint.**  
**Status:** Fixed.  
**Decision:** The routing gate's Stage 2 cross-encoder is Cohere Rerank v4 (`rerank-v4.0`), self-hosted on a SageMaker real-time endpoint named `skywalker-gate-rerank-v4-{stage}`, separate from the evidence-reranker endpoint defined in D-25. Production runs two `ml.g5.2xlarge` (A10G) endpoints across availability zones for HA; beta runs one. The wire shape is the standard Cohere Rerank JSON payload with `top_n: 1` over the 50 short variant texts. Authentication is SigV4 against SageMaker Runtime; the wrapper's IAM execution role is scoped to the gate endpoint ARNs only.  
**Rationale:** Bedrock-hosted Cohere Rerank 3.5 was an option but was rejected for two reasons. First, the team standardizes on SageMaker hosting for cross-encoder workloads (D-25 already does this for the evidence reranker), so two-endpoint single-substrate posture keeps the operational story unified. Second, Cohere Rerank v4 is the current Cohere generation and is what the evidence reranker also uses; using one model family across the two reranker workloads avoids version skew. The gate endpoint runs on A10G rather than the evidence reranker's H100 because Stage 2 fires on ~20% of requests with a much smaller payload (50 short variant texts, `top_n: 1`), and A10G hits the gate's 200 ms timeout comfortably at that payload size.  
**Trigger to reopen:** Measured gate Stage 2 latency on A10G consistently breaches 200 ms p95, or Cohere deprecates Rerank v4, or operational findings show that consolidating the gate and evidence rerankers onto one endpoint would not increase failure-domain risk.

**D-33. Hybrid retrieval (BM25 + FAISS cosine) on the FAQ arm.**  
**Status:** Fixed.  
**Decision:** The FAQ evidence call uses a hybrid query against AOSS that combines a `match` clause on `text` and `title` (BM25 lexical leg) with a `knn` clause on `embedding` (FAISS cosine vector leg), fused through the `skywalker-faq-hybrid` search pipeline using `min_max` normalization and `arithmetic_mean` combination. The vector leg includes a FAISS `efficient_filter` clause pre-filtering on `country`, `level`, and `role`. Per-leg weights are SSM-tunable at `/skywalker/runtime/retrieval/hybrid_bm25_weight` (launch 0.30, implying vector weight 0.70).  
**Rationale:** Vector-only retrieval is fragile on identifier-shaped tokens (policy codes, vendor names, currency abbreviations) that BM25 catches reliably. BM25-only retrieval misses paraphrase and conceptual matches that vector similarity catches reliably. Top 50 FAQ traffic includes both shapes, so retrieving with both legs and fusing on a normalized score is the design. Hybrid is architecture-class, not a calibration surface — the architecture commits to running both legs on every FAQ-evidence call. What is calibratable is the per-leg weight.  
**Trigger to reopen:** Judged-traffic evidence shows that one leg consistently dominates the other across the entire weight range and the simpler-leg-only path performs equivalently, or AOSS deprecates the search-pipeline normalization processor and the fusion has to move to client-side.

**D-34. Storage substrate is Amazon OpenSearch Serverless vector collection with FAISS engine.**  
**Status:** Fixed.  
**Decision:** The owned FAQ evidence corpus lives in an Amazon OpenSearch Serverless (AOSS) vector search collection named `skywalker-faq-{stage}`. The `knn_vector` field uses the FAISS engine with `cosinesimil` space and HNSW indexing (`m: 24`, `ef_construction: 128`). AOSS data-access policies grant `aoss:*` permissions on the collection's index resource pattern; SigV4 signs requests with `service: "aoss"`. Network access is gated by an AOSS network policy that places the collection behind a VPC endpoint at launch.  
**Rationale:** AOSS vector collections support FAISS as their k-NN substrate; Lucene/nmslib is not an AOSS engine option. The serverless model removes capacity planning and cluster-management toil for a deliberately tiny corpus. FAISS HNSW with `efficient_filter` gives correct in-query scope filtering, which is what the architecture requires to keep retrieval scope-aware.  
**Trigger to reopen:** AOSS introduces a different supported vector engine whose properties materially outperform FAISS for our workload, or AOSS pricing/operational characteristics shift in ways that make a self-managed OpenSearch domain or a different vector store worth the migration cost.

**D-35. Cross-Q&A linked-item expansion.**  
**Status:** Fixed.  
**Decision:** Authors curate contextual relationships between FAQ Q&As as a directed, single-valued, depth-2-bounded chain. Each FAQ Q&A carries one custom metadata field on its COREx content model whose value is the COREx nodeId of the next linked Q&A in the chain (or empty if the Q&A is a chain tail). The Skywalker ingestion pipeline reads this field, walks at most two hops per Q&A, breaks cycles deterministically (stops on first revisit, emits `FAQLinkCycleBroken`), treats unresolved nodeIds as absent (`FAQLinkUnresolved`), and stamps the resulting ordered list of linked parent_ids onto every child chunk under the new unindexed `linked_parent_ids` field on the AOSS evidence index. At query time, post-rerank expansion (per D-27) issues one `terms` query against the union of anchor parent_ids and linked parent_ids, with no scope filter on the expansion query — author-asserted contextual relationships override `country`/`level`/`role` filtering for the expansion query specifically. Linked items never enter the rerank pool (per D-36); they ride along anchors that have already won rerank. Each rendered segment carries a Unicode superscript citation marker and the MCP envelope grows a parallel `citations[]` field per evidence record (per D-37).  
**Rationale:** The single-pointer linked-list shape is the minimum viable curatorial primitive; depth-2 keeps both the ingest cost and the runtime fan-out trivially bounded; materialization at ingest keeps query-time cost at one terms query whose list grows by at most a few entries per shortlist anchor. Curatorial linkage is conceptually distinct from scope-applicability — when an author asserts "B is contextually relevant alongside A," that assertion stands regardless of B's `country`/`level`/`role` tags — so the expansion query carries no scope filter. This is a deliberate carve-out from the spirit of D-02 for author-curated links only; the original retrieval query continues to apply the full scope filter unchanged.  
**Trigger to reopen:** Single-pointer linked-list semantics consistently cannot represent the link patterns authors are authoring (e.g., consistent need for fan-out from one Q&A to several siblings); the depth-2 bound is consistently too tight or too loose under judged traffic; or — most importantly — real evidence that authors are linking content across scope boundaries in ways that produce user-visible answers actively wrong for the requester's context, at which point the carve-out itself is what gets re-litigated and a scope-aware expansion filter would be reintroduced.

**D-36. Linked items do not enter the rerank pool.**  
**Status:** Fixed.  
**Decision:** Linked Q&As surfaced through the D-35 chain expansion are not added to the reranker's input candidate pool. The reranker scores the original retrieval pool only. Linked items appear in the final evidence package because their anchor won, not because they themselves were independently scored.  
**Rationale:** Section 07 §3 decisions four and five fix that source identity does not influence rerank scoring. Independent rerank scoring of linked-item chunks would either require source-aware text rendering (forbidden by decision four) or would let the rerank pool size fluctuate with link-graph density (breaking C-13's calibration semantics). Author-curated linkage is a separate signal from cross-encoder relevance; mixing them at the rerank surface confuses both.  
**Trigger to reopen:** Judged-traffic evidence shows that independent rerank scoring of linked-item chunks would meaningfully outperform the always-include rule, and that the rendering and pool-size concerns can be solved without re-introducing source-arm bias into the rerank surface.

**D-37. Citation contract for linked-item evidence.**  
**Status:** Fixed.  
**Decision:** When an answerable FAQ candidate carries linked items, the rendered evidence text concatenates the anchor's reconstructed parent text with each linked Q&A's reconstructed parent text in chain order, separated by `"\n\n"`, with each segment suffixed by a Unicode superscript citation marker (`¹` for the anchor, `²` for the first linked Q&A, `³` for the second). The MCP `evidence[]` record gains a parallel `citations[]` array — one entry per rendered segment in marker order — each carrying `marker`, `source_id`, `title`, `source_url`, `policy_links`. The candidate's top-level `source_id`, `title`, `source_url`, and `policy_links` continue to refer to the anchor's own Q&A. The `citations[]` field is absent on UKB candidates and on FAQ candidates whose anchor has no linked items. Per Section 02 §2.  
**Rationale:** Preserves D-17's citation requirement at per-segment granularity without splitting the rendered text into per-segment objects (which would have been a heavier envelope change). Inline Unicode superscript markers survive any reasonable downstream rendering pipeline (Slack, QuickSuite, plain text) without requiring markdown footnote syntax. The parallel `citations[]` array is purely additive — agents that don't know about it ignore it without breaking; agents that do know about it can resolve markers cleanly.  
**Trigger to reopen:** Downstream agents (Slack composer, QuickSuite chat-agent runtime) cannot consume the inline-marker plus parallel-array shape and need a fundamentally different envelope, or the depth bound rises above 9 segments and double-digit markers force a different inline encoding.

**D-38. Slack and UAT use CloudAuth inbound on `/ca/mcp/` with CloudAuth OBO + TransitiveAuth.**  
**Status:** Fixed.  
**Decision:** The Slack inline-agent orchestrator and the UAT inline-agent orchestrator each register as a CloudAuth-modeled AAA application in ServiceLens (suggested names `SkywalkerSlackOrchestrator` and `SkywalkerUATOrchestrator`, finalized at onboarding) and reach Skywalker through MCP Gateway's CloudAuth-inbound route at `https://api.mcp.asbx.aws.dev/ca/mcp/{registry-id}/{skywalker-server-id}`. Each orchestrator establishes AAA relationships to MCP Gateway's `InvokeMcp` endpoint and to Skywalker's MCP server resource per the OBO Decision Guide's "OBO Required (Shared / Cross-Boundary)" path. Bindle-based authorization grants each orchestrator's AAA application principal `canInvoke` on `MCPGateway::{skywalker-server-id}`. **CloudAuth OBO is on by default on this combination** — MCP Gateway invokes Skywalker on behalf of the calling orchestrator's AAA application identity, so Skywalker sees the orchestrator's principal rather than the gateway's. **TransitiveAuth additionally carries the human end-user's identity** as a separate TA token alongside CloudAuth: the orchestrator initiates the TA token from the resolved alias plus an `AgenticContext`, and Skywalker validates the TA token server-side and reads the human alias from the TA claims. The grounding contract is API_14.  
**Rationale:** Per the [BuilderHub MCP server vendor guide](https://docs.hub.amazon.dev/docs/mcp-gateway/user-guide/howto-vendor/), AWSAuth-inbound for CloudAuth-protected services was withdrawn for new MCP servers after April 24, 2026, and the grace window for existing servers closed May 15, 2026. CloudAuth-inbound is the BuilderHub-supported successor and is the path with native OBO + TA support per the [UnifiedAuth MCP Server Builder Guidance](https://w.amazon.com/bin/view/Dev.CDO/UnifiedAuth/Agentic/MCP/Overview/). Adopting OBO + TA together (rather than OBO alone) is the right structural improvement: identity arrives at Skywalker through verified auth claims rather than through MCP tool arguments the orchestrator chose to inject. The earlier-draft SigV4-inbound approach is preserved in git history but is not the active architecture.  
**Trigger to reopen:** MCP Gateway changes its supported auth shapes for CloudAuth-protected backends (forcing a different inbound route); UnifiedAuth retires the OBO + TA pair (forcing a different identity-propagation mechanism); or the orchestrator pattern itself changes (forcing an AAA-application-model rethink).

**D-39. Skywalker fails closed when TransitiveAuth is missing on the Slack and UAT paths.**  
**Status:** Fixed.  
**Decision:** When a request arrives on the CloudAuth-inbound route from the Slack or UAT orchestrator and the TA token is missing or fails validation, Skywalker returns a system-failure error (JSON-RPC protocol error) rather than falling back to `arguments.alias` or proceeding without a verified human identity. Argument-supplied alias is preserved as a contract shape (so QuickSuite and any future non-TA caller still works on the alias-based MCP tool) but is **not** an automatic fallback path on the CloudAuth-inbound route.  
**Rationale:** Allowing silent fallback to `arguments.alias` on the TA-expected paths would erode the migration's main benefit — the orchestrator could inject any alias it chose, defeating the verified-claims posture. Failing closed forces orchestrator misconfigurations to surface immediately rather than silently degrading retrieval scoping. Pre-launch testing in beta and gamma is the right place to catch initiator/validator setup issues; the production posture is hard failure on missing TA.  
**Trigger to reopen:** Operational evidence shows that TA propagation is unreliable enough (e.g., transient validator outages on Skywalker's side, TA initiator token-refresh races on the orchestrator side) that fail-closed produces user-visible outages disproportionate to the security benefit. At that point the alternatives are (a) hardening the TA infrastructure to remove the unreliability, (b) adding a narrowly-scoped audit-only fallback that proceeds with `arguments.alias` while emitting a critical-severity alarm, or (c) re-litigating the closed-loop posture entirely. The launch posture is (a)-by-default: harden rather than soften.

**D-40. Skywalker MCP server is hosted on MCP Gateway; gateway is the only internet-facing surface.**  
**Status:** Fixed.  
**Decision:** Skywalker's MCP server is registered with Amazon MCP Gateway and is reachable only through the gateway's authenticated termination at `api.mcp.asbx.aws.dev`. The Coral service backing the gateway is not directly addressable on its own public DNS. The gateway's endpoint is a public DNS name with auth enforced at the gateway (CloudAuth, Federate OAuth, or SigV4 depending on the inbound route), and the architecture inherits the gateway's natural public-with-auth posture. The closed set of supported auth shapes (none currently issued to callers external to Amazon's auth platforms) keeps the authorization boundary against external callers; admitting external callers would be a re-litigation event, not a calibration adjustment. Grounding contract is API_13.  
**Rationale:** MCP Gateway is the BuilderHub-paved-path for Coral/Smithy-modeled internal MCP servers and provides exactly the public-endpoint-with-auth pattern the [BuilderHub Web service Golden Path](https://docs.hub.amazon.dev/docs/golden-path/web-service-ec2/recommendation/application-infrastructure/networking/prod-corp-to-aws-vpc-connectivity/) recommends as the default for cross-fabric Amazon-internal connectivity. Building a parallel direct-public-ALB on top of the gateway would duplicate the auth surface for no architectural benefit. The gateway's network coverage (CORP all regions, PROD/Native AWS in PDX/IAD/DUB/NRT) is sufficient for every production caller without supplementary connectivity bridges (PrivateLink, SuperStar/Allegiance, Tardigrade ProdLink/CorpLink).  
**Trigger to reopen:** A future caller cannot use any of the gateway's supported auth shapes; a future product surface is genuinely external to Amazon and requires admission with AppSec review and data-classification rework; or MCP Gateway is deprecated in favor of AgentCore Gateway per the [ASBX AIM and MCP Gateway Roadmap](https://w.amazon.com/bin/view/BuilderTools/GenAIDevX/Roadmap/) and the posture has to be confirmed on the new substrate.

### 11. Initial calibration register

The calibration register is the counterpart to the decision register. It captures the places where the architecture is intentionally open because the right value should come from evidence later rather than from premature certainty now.

**C-01. FAQ-only routing threshold.**  
**Current posture:** Architecture fixed, numeric threshold open.  
**Evidence needed for adjustment:** Variant-set score distributions, judged examples, and production review outcomes.  
**Escalates to re-litigation when:** The route shape itself appears wrong rather than the number inside it.

**C-02. Answerable-versus-abstain rule.**  
**Current posture:** Two-branch composite fixed at the structural level in Section 07. Abstain when no candidates survive reranking (`NO_USABLE_EVIDENCE`) or when the top reranked `relevance_score` falls below a configured absolute floor (`EVIDENCE_TOO_WEAK_AFTER_RERANK`). Target operational abstain rate at launch is 5 to 15 percent on Top 50 traffic. The absolute floor value is calibration-active.  
**Evidence needed for adjustment:** Judged evidence quality, subject-matter review outcomes, and measured abstain rates against the 5-to-15 percent target band.  
**Escalates to re-litigation when:** The common answerability model itself appears structurally insufficient, or when the target band cannot be held without abandoning grounding discipline.

**C-03. Reranker candidate-pool size.**  
**Current posture:** Open.  
**Evidence needed for adjustment:** Retrieval diversity and shortlist quality under judged traffic.  
**Escalates to re-litigation when:** Common reranking appears unable to reconcile the route at all.

**C-04. Final evidence shortlist size.**  
**Current posture:** Open.  
**Evidence needed for adjustment:** Downstream grounding sufficiency and client usefulness.  
**Escalates to re-litigation when:** The evidence-packaging model itself appears wrong.

**C-05. FAQ variant-bank quality.**  
**Current posture:** Open and maintainable.  
**Evidence needed for adjustment:** Classification misses, false positives, and subject-matter review.  
**Escalates to re-litigation when:** The Top 50 question set no longer fits the domain.

**C-06. UKB candidate normalization detail.**  
**Current posture:** Open within a fixed normalization requirement.  
**Evidence needed for adjustment:** UKB-result quality, provenance gaps, and reranker behavior.  
**Escalates to re-litigation when:** UKB can no longer be normalized honestly into the common surface.

**C-07. Publication mechanics for rebuilt controlled corpus.**  
**Current posture:** Open within the fixed full-rebuild doctrine.  
**Evidence needed for adjustment:** Rebuild consistency, visibility correctness, and operational simplicity.  
**Escalates to re-litigation when:** Corpus size or publication-state pressure breaks the simplicity-first assumption.

**C-08. Client use of explicit-scope entry.**  
**Current posture:** Open within a fixed shared contract.  
**Evidence needed for adjustment:** Integration maturity and authoritative-scope availability.  
**Escalates to re-litigation when:** The explicit-scope path proves structurally unsafe or unnecessary.

**C-09. Abstain reason vocabulary.**  
**Current posture:** Pinned to two values at launch: `NO_USABLE_EVIDENCE` and `EVIDENCE_TOO_WEAK_AFTER_RERANK`. Client-consumption clarity and reviewer usefulness at these two values remain to be measured.  
**Evidence needed for adjustment:** Reviewer trace data showing that QuickSuite's chat-agent runtime and the Slack application cannot produce meaningfully different user-facing messages from the two classes, or that a third class would actually help diagnosis.  
**Escalates to re-litigation when:** Clients need fundamentally different backend result classes, supported by specific reviewer-trace evidence rather than theoretical fine-graining.

**C-10. Production review feedback cadence.**  
**Current posture:** Open.  
**Evidence needed for adjustment:** Subject-matter throughput and operational learning needs.  
**Escalates to re-litigation when:** The governance model cannot absorb review feedback coherently.

**C-11. QuickSuite wrapper hosting and Federate profile.**  
**Current posture:** Launch posture is AgentCore Gateway with Federate Prod inbound auth and a REQUEST interceptor Lambda for JWT-claim extraction. AgentCore Runtime remains a viable alternative.  
**Evidence needed for adjustment:** Measured Gateway request/response shaping behavior on Skywalker's response envelope, operational posture of Gateway versus Runtime in the target account, and the team's preference for deployment surface.  
**Escalates to re-litigation when:** The chosen hosting model imposes a real constraint on QuickSuite-facing behavior, such as Gateway's interceptor pattern proving too brittle to JWT claim variations, or Runtime's operational profile becoming materially better suited to Skywalker's traffic shape.

**C-12. Reranker absolute floor.**  
**Current posture:** Launch default 0.30. Calibration-active.  
**Evidence needed for adjustment:** Judged-traffic abstain rates against the 5-to-15 percent target band, subject-matter review outcomes, and reranker score distributions observed in production.  
**Escalates to re-litigation when:** The floor-based rule stops being able to express the distinction between answerable and abstain cleanly at any threshold value, indicating the signal itself is too noisy to act on.

**C-13. Per-arm candidate budget.**  
**Current posture:** Launch default 10 candidates per arm. Calibration-active.  
**Evidence needed for adjustment:** Reranker input-token headroom on real candidate text, observed shortlist quality, and whether the reranker is starved or flooded.  
**Escalates to re-litigation when:** The budget has to change significantly in either direction to avoid either over-retrieval cost or under-retrieval starvation.

**C-14. UKB per-request timeout.**  
**Current posture:** Launch default 300 ms. Calibration-active, held in SSM at `/skywalker/runtime/retrieval/ukb_timeout_ms`.  
**Evidence needed for adjustment:** Measured UKB latency p50/p95/p99 at target volume, rate of single-arm fallback events attributable to UKB timeout.  
**Escalates to re-litigation when:** The timeout causes frequent single-arm fallback in ways that degrade overall answerability, or UKB is consistently fast enough that a tighter budget is safe.

**C-15. Routing-gate cosine-band thresholds.**  
**Current posture:** Launch defaults `cosine_low_threshold = 0.30`, `cosine_high_threshold = 0.80`. Both are calibration-active control-plane values held in SSM under `/skywalker/runtime/gate/`.  
**Evidence needed for adjustment:** Gate-decision telemetry (how often each band fires, what the stage-2 verdict was on ambiguity-band requests), judged examples of near-miss FAQ-like and non-FAQ-like queries, abstain rates correlated with routing decisions.  
**Escalates to re-litigation when:** The ambiguity band is either so wide that stage 2 fires on nearly every request (cosine is too weak) or so narrow that stage 2 almost never fires (cosine is doing all the work, making stage 2 performative).

**C-16. Routing-gate rerank floor.**  
**Current posture:** Launch default `rerank_floor = 0.50`. Calibration-active control-plane value held in SSM at `/skywalker/runtime/gate/rerank_floor`.  
**Evidence needed for adjustment:** Stage-2 verdict distribution, judged examples where the gate over-routed to FAQ-only or under-routed to dual-arm, subject-matter review outcomes.  
**Escalates to re-litigation when:** No value of the floor produces a routing distribution that matches subject-matter expectations — at which point the stage-2 model itself (Cohere Rerank v4 on the gate's SageMaker endpoint) might be the wrong choice or the wrong instance size.

**C-17. Routing-gate rerank timeout.**  
**Current posture:** Launch default 200 ms. Calibration-active control-plane value held in SSM at `/skywalker/runtime/gate/rerank_timeout_ms`.  
**Evidence needed for adjustment:** Measured Cohere Rerank v4 latency p50/p95/p99 on the gate's `ml.g5.2xlarge` endpoint at target volume against a 50-variant payload.  
**Escalates to re-litigation when:** Cohere Rerank v4 on the gate endpoint consistently runs hotter than 200 ms at production volume (the timeout must grow or stage 2 must be skipped more aggressively, or the gate endpoint must move to a faster instance), or consistently runs much cooler (the timeout can tighten to catch tail latency sooner).

**C-18. Hybrid retrieval per-leg weights.**  
**Current posture:** Launch default `hybrid_bm25_weight = 0.30` (implying vector weight 0.70). Calibration-active control-plane value held in SSM at `/skywalker/runtime/retrieval/hybrid_bm25_weight`.  
**Evidence needed for adjustment:** Judged-traffic evidence on whether identifier-shaped queries (policy codes, vendor names, currency abbreviations) are systematically underranked, or whether paraphrase-shaped queries are systematically underranked. Per-query telemetry on which leg carried the winning candidate's score.  
**Escalates to re-litigation when:** No value of the weight produces acceptable ranking on both query shapes — at which point the fusion strategy itself (`min_max` normalization with `arithmetic_mean` combination) may need to change to a different combination technique, which is a search-pipeline rebuild rather than a calibration update.

**C-19. AOSS scope-filter selectivity.**  
**Current posture:** Launch default `knn_overretrieve_k = 40` against a `size = 20` result on FAQ-only routes. Calibration-active control-plane value held in SSM at `/skywalker/runtime/retrieval/knn_overretrieve_k`.  
**Evidence needed for adjustment:** Measured how often the FAISS pre-filter on `country`/`level`/`role` reduces the candidate population enough that 40-over-retrieval is insufficient, observed in cases where fewer than `size` candidates survive the filter.  
**Escalates to re-litigation when:** Over-retrieval has to grow significantly to keep the result set populated, indicating that scope-filter selectivity is biting harder than the corpus distribution suggested.

**C-22. Cross-Q&A link unresolved-edge tolerance.**  
**Current posture:** Launch default `linked_unresolved_tolerance_pct = 0.05`. Calibration-active. Held in the ingestion-job configuration (the value gates whether the build fails when the cumulative fraction of unresolved "next" edges across all FAQ Q&As exceeds the threshold).  
**Evidence needed for adjustment:** Observed unresolved-edge rates across daily ingestion runs, correlated with whether the unresolved edges are systematic authoring drift (real signal) or transient depublish-races (cosmetic noise).  
**Escalates to re-litigation when:** No tolerance value produces both a build-success rate the team is willing to accept and a corpus-completeness guarantee that prevents silently shipping a build with materially incomplete chains. At that point either the COREx authoring discipline needs tightening or the ingestion-job needs a different validation signal than per-edge resolution.

**C-23. Per-linked-segment text-token cap.**  
**Current posture:** Launch default `linked_text_token_cap = 8000`. Calibration-active control-plane value held in SSM at `/skywalker/runtime/retrieval/linked_text_token_cap`. Truncation occurs at the nearest paragraph boundary; an inline `[truncated]` marker is appended; `LinkedSegmentTruncated` is emitted; the segment's citation marker remains attached.  
**Evidence needed for adjustment:** Observed truncation rates and downstream agent context-window pressure when chains include long linked Q&As. Reviewer feedback on whether truncated segments lost answer-relevant content.  
**Escalates to re-litigation when:** The default systematically truncates linked segments that turn out to be load-bearing to the answer (cap is too small), or it never truncates and is therefore not earning its existence as a knob (cap is too large or the problem it was meant to solve does not exist at the corpus scale we operate at).

### Closing position

The rest of the Skywalker series defines the system. This final section defines how the system stays itself while still learning.

That is the real job here. A retrieval system with two arms, explicit identity shaping, a controlled owned corpus, a black-box general integration, one common reranking surface, structured abstention, and two different client surfaces cannot be kept coherent by folklore. It needs a written discipline for what is fixed, what is calibratable, what evidence is allowed to reopen a decision, and what the team should do when the evidence is not yet strong enough.

The answer is not to freeze the architecture and not to dissolve it. The answer is to keep a durable decision log, treat re-litigation as a controlled technical event, and let the governance layer abstain from change when the record is not yet strong enough. That is the posture that preserves both honesty and momentum.

## Section 10. Calibration, Re-litigation Triggers, and Decision Log

The earlier sections fixed the architecture itself: what Skywalker is, what it owns, where it stops, how scope is resolved, how the controlled FAQ corpus is ingested and published, how the online path routes, how UKB participates, how both arms converge on one reranking surface, and how the clients consume the backend. That body of work is now large enough that this final section cannot be a loose appendix about future tuning. It is the operating discipline that keeps the architecture coherent once implementation, production review, and real traffic apply pressure.

The project contains two classes of uncertainty that cannot be handled the same way. Some parts of the design are fixed enough that changing them would alter subsystem boundaries, invalidate earlier sections, or force coordinated contract changes — architecture decisions. Other parts are intentionally empirical — thresholds, candidate budgets, fallback postures, client-consumption refinements that can only be chosen honestly against judged examples and production behavior — calibration surfaces. Handled with the same loose "we can revisit later" language, the document stops being a source of truth and becomes a collection of temporary opinions.

This section therefore defines how the system changes without drifting: a decision log that survives implementation pressure, explicit baseline postures for calibration surfaces, and named triggers for re-litigation. It is a real architecture section — it describes no runtime subsystem; it describes the rules by which the runtime subsystems remain authoritative. The register below also demonstrates the discipline working: the June 10, 2026 regrounding of this series against the implemented ingestion code and executive direction produced revisions, supersessions, and retirements, and every one of them is recorded as a visible log event rather than silently rewritten.

### 1. The governance boundary

This section owns the change-control discipline after Sections 01 through 09 establish the baseline: the difference between configuration-class and architecture-class change, the record format for major decisions, the calibration surfaces visible across the design, the evidence classes allowed to pressure decisions, and the review posture once production feedback accumulates.

It owns the principle that architecture stays healthy neither by rigidity nor by fluidity — a document that refuses to revisit anything becomes ceremonial; one that casually revisits everything becomes decorative. It owns the decision log as a program artifact: the PAPI short-circuit, the routing gate, the rebuild posture, the common reranking surface, the abstain contract, the UKB seam, and the client asymmetry are all decisions that erode accidentally when not recorded with rationale and reopening triggers. And it owns the architecture-level meaning of re-litigation: a previously fixed decision being deliberately reconsidered because its assumptions changed, runtime evidence shows it underperforming, or the surrounding system evolved past it. A controlled technical event — not a synonym for second-guessing, and not permission for "experiments" that quietly bypass the architecture: an experiment that changes an MCP contract, an index compatibility assumption, a route shape, or the response package is an architecture event, and this section exists so those events stay visible.

It does not own the subsystem details fixed elsewhere, staffing, delivery sequencing, or project management. It is technical governance, not planning.

### 2. Inputs, outputs, and contracts

**Inputs.** The fixed architecture (the log preserves and governs, never reinvents). Evidence — a disciplined concept here: representative judged queries, subject-matter review outcomes, structured production observations, measured latency or reliability behavior, corpus observations that falsify an assumption, client-integration findings exposing contract mismatches, and implementation discoveries proving a design unbuildable as specified. The June 2026 code regrounding is the first major instance of that last class, and the register reflects it. Anecdotal discomfort starts conversations; it does not reopen decisions. And the calibration surfaces already visible across the series, entering here so they tune under one discipline rather than through whichever subsystem exposes the nearest configuration file.

**Outputs.** First, the decision record: stable identifier, the decision, what it binds, rationale, current baseline, the evidence class that would reopen it, and status — `Fixed` (current truth, not casually changeable), `Calibration-active` (shape fixed, value empirical), `Open` (deliberately undecided), and two statuses this revision adds: `Superseded` (replaced by a named successor; retained for history) and `Retired` (the machinery the decision governed was removed). Second, the re-litigation packet: the challenged decision ID, the trigger that fired, the attached evidence, impacted sections, calibration-class versus architecture-class, and the candidate replacement. Third, the recorded non-change — the governance equivalent of backend abstention: when evidence is insufficient, the correct output is a documented decision to preserve the baseline, record the pressure, and define what further evidence would justify reopening.

The contract: a pressure enters, binds to a known decision or surface, its evidence is classified, and exactly one of three legitimate outcomes follows — reaffirm, tune a declared surface, or reopen explicitly.

### 3. Fixed decisions

The decisions fixed at the series level, stated once and detailed in the register: Skywalker is a retrieval backend behind MCP, not the conversational layer. Identity-aware scoping is part of correctness — **country, job level, and employee class** (the third dimension's vocabulary was corrected from manager-versus-IC to employee class on June 10, 2026, against the production corpus; D-02). PAPI sits before retrieval unless the caller supplies authoritative scope. Three MCP entry modes exist. Two retrieval arms — controlled FAQ plus UKB general. The controlled arm is retrieval-backed, never cache-backed. The corpus is polled daily and rebuilt all-or-nothing on change, published by an atomic pointer flip. Strong FAQ matches stay FAQ-only; everything else widens dual-arm. Both arms converge on one common reranking surface — **all reranking in Skywalker, gate stage included, runs Cohere Rerank v4 on SageMaker** (June 10, 2026 direction; D-25/D-32). Abstention is a valid backend outcome with exactly two reasons. Multi-turn handling lives with client agents. Slack and QuickSuite are intentionally asymmetric clients on one shared transport. Human subject-matter review is part of the launch calibration loop. Hybrid retrieval (BM25 + k-NN) ships on the FAQ arm at launch. These are fixed because changing them invalidates earlier sections and requires explicit rewrite work — which is exactly why the log exists.

### 4. Alternatives considered

**Ad hoc tuning without a formal log.** Rejected: the fastest way to lose the series as a source of truth.

**Everything permanent until a wholesale rewrite.** Rejected: several surfaces are intentionally empirical and cannot be finalized honestly before judged traffic exists.

**Blanket periodic re-litigation.** A live risk teams drift into; architecturally wrong — it spends decision energy indiscriminately and makes the system feel permanently provisional where it should be firm.

**Trigger-based re-litigation.** Adopted. Decisions stand until a named trigger fires; triggers may be quantitative, observational, integration-driven, or corpus-driven, but they must be named.

**One untyped global log.** Live only if strongly typed; without status and class information, "decision changed" is too vague to govern anything.

**Recording only changes, never non-changes.** Rejected: it deletes the rationale for tempting changes not taken and falsely implies the architecture simply failed to evolve.

**Implementation inconvenience as sufficient evidence.** Rejected in principle. Difficulty becomes architectural evidence only when it proves the design unbuildable as specified or reveals a hidden cost that changes operating assumptions — which is precisely what the ingestion implementation did for the alias-swap and chunking decisions, and why those entries read `Superseded` rather than quietly edited.

### 5. Assumptions inherited from upstream

This section inherits the full architecture as revised: the system boundary and tenets (Section 01); the entry contract with the employee-class scope vocabulary and per-route identity channels (Section 02); the implemented ingestion design — daily polling, whole-node fragments, two physical indices with an SSM live pointer, the all-or-nothing flip gate, never-unscoped publication (Section 03); the static S3 variant set ([API_11]); the online routing model — hybrid gate with in-memory cosine plus a contingent SageMaker-hosted Rerank v4 cross-encoder, hybrid BM25+FAISS retrieval on the FAQ arm from launch (Section 04); the UKB seam (Section 06); common reranking with the two-branch abstain rule (Section 07); and the client posture (Sections 05, 08, 09) — all three production paths on Amazon MCP Gateway: Slack and UAT on CloudAuth-inbound with OBO + TransitiveAuth (D-38), QuickSuite on Federate-inbound with argument-carried identity (D-19), the gateway's public-with-auth posture inherited deliberately (D-40).

Future pressures will try to reopen these boundaries indirectly, especially through client-surface needs; the inherited assumption is that they are evaluated against the fixed architecture rather than allowed to dissolve it.

### 6. End-to-end data flow

The flow this section owns is a design-governance path, not a request path. **Evidence capture**: the observation is written so the challenged decision is identifiable — "routing feels off" is not enough; "the FAQ-only threshold is producing routes that later abstain under review" names the subsystem and failure pattern. **Classification**: bound to an existing record (or a newly named one that should have existed), and sorted calibration-class, architecture-class, or not yet strong enough. **Evidence binding**: attached with its class against the trigger it claims to satisfy — the step that stops every intuition from becoming a redesign proposal. **Triage**: trigger unmet → recorded non-change, baseline stands, the missing evidence is named; trigger met and calibration-class → controlled tuning change; trigger met and architecture-class → a re-litigation packet naming impacted sections and contracts. **Issuance**: calibration changes update the baseline and the log; architecture changes update the record, rewrite affected sections, and land as first-order events. **Publication**: the log is the visible truth, and implementation consequences flow from it — a decision that changed without the series and backlog changing was never really published. **Stabilization**: the new baseline becomes active truth until a future trigger fires.

The flow's important property is three valid outcomes, not one: a change, a documented non-change, or an explicit open question still lacking evidence. The governance layer supports abstention exactly as the runtime does.

### 7. Failure behavior and abstain behavior

**Silent drift** — a threshold, route, or response behavior changing materially without being named calibration or redesign — is the first failure mode this section exists to prevent. **False tuning** — a contract-breaking change labeled calibration because the word sounds smaller: expanding the scope tuple without revisiting client contracts is not tuning; moving conversational behavior into the backend is not tuning. **False finality** — freezing every uncertain surface as doctrine until engineers feel they are violating the architecture by asking whether a shortlist size still serves it. **Governance theater** — a log that never records non-changes, never updates baselines, and never ties back to implementation, performing discipline rather than exercising it.

Abstain behavior here is literal: when evidence is insufficient, the governance layer abstains from change, records the concern, preserves the baseline, and defines what would reopen it. Disciplined non-change, not indecision.

Non-goals: staffing, backlog priority, review-board process, runtime monitoring, delivery schedule, client product strategy, future quality tooling, and the pretense that every threshold will be perfectly calibrated at launch. And this section is not a substitute for judgment — logs and triggers help teams reason; they do not remove the need to think.

### 8. Calibration surfaces

The major calibration surfaces, each detailed in the calibration register (§11): the FAQ-only routing thresholds (C-01, C-15, C-16, C-17); the answerable-versus-abstain rule and floor (C-02, C-12); the variant set's representational coverage (C-05); the ingestion cadence and the deliberately loose launch alarm thresholds (C-20); the reranker pool and shortlist sizes (C-03, C-04, C-13); UKB normalization detail and timeout (C-06, C-14); explicit-scope usage by clients (C-08); single-arm fallback conservatism (C-07's successor posture); client-side abstain consumption (C-09, C-11); hybrid retrieval weights and scope-filter over-retrieval (C-18, C-19); and the decision log itself (C-10) — which may sound circular but is necessary: the log re-litigates if it becomes too light to be useful, too heavy to maintain, or too detached from implementation to govern anything.

### 9. Open questions

One precondition frames everything below and most of the calibration register besides: **the majority of these questions are only answerable against real user data at meaningful volume — at minimum a few hundred actual users, which arrives with the September production launch, not before.** UAT's June cohort produces directional evidence on a narrow FAQ-only slice (citation UX, abstain rendering, prompt compliance); it does not produce calibration-grade distributions for routing thresholds, abstain floors, candidate budgets, or hybrid weights. Until September-scale traffic exists, launch defaults stand, and pre-launch pressure to move them is guesswork wearing evidence's clothes — the correct governance response is the recorded non-change.

The precondition itself gets the same scrutiny as any other load-bearing assumption, because "September solves it" can do as much unexamined work as any premise in this series. A few hundred users is not a few hundred samples *per decision*: roughly 20% of traffic reaches the gate's ambiguity band, abstentions target a 5–15% band, and the thin slices those rates produce may be dozens of examples per month against a dozen threshold decisions — enough to detect gross miscalibration, not enough to move a threshold with confidence. Three mitigations are named now rather than discovered in October. First, shadow-routing (D-45) manufactures comparative evidence from live traffic in exactly the thinnest region, and its sample rate (C-26) can be raised when a specific surface needs evidence faster than organic volume supplies it. Second, judged-query augmentation — SME-constructed query sets run against the live system — is the accepted instrument for surfaces where production traffic is structurally sparse. Third, the calibration windows extend rather than the evidence bar lowering: if September data proves too sparse for a given threshold, the recorded outcome is "insufficient evidence, window extended," not a tuned-anyway value. September is the earliest the questions become answerable, not a guarantee that they all will be.

**The reranker instance bake-off** *(the one question gating a build; pending executive approval — D-41; also the one question below that does not wait for September — its evidence is a benchmark, not user traffic).* Everything else in this section's registers is governed evidence-collection; this is a decision awaiting authorization to collect its evidence.

**Prod-region consistency** *(disclaimer: does not gate current work; must close before the prod stage lands — D-42's residual).*

**The decision log's operational home** *(disclaimer: the log must exist; where its authoritative representation lives — this series, a repository artifact, or synchronized dual form — is open).*

**Review cadence for accumulated triggers and non-changes** *(disclaimer: trigger-based posture is adopted; the formal review rhythm is an implementation-level answer).* 

**The evidence bar for mixed cases** *(disclaimer: the calibration/architecture distinction is defined; the governance process for pressures that start as threshold complaints and become contract complaints is open).*

**Wiring SME review into the log** *(disclaimer: review is in scope; the disciplined, low-friction translation of review findings into record updates is open).*

**A separate proposed-but-not-accepted registry** *(disclaimer: the assumption is that non-change records against decision IDs suffice; open until proven otherwise).*

### 10. Decision register

Register discipline: identifiers are stable and never reused. Entries revised on June 10, 2026 carry the revision inline; superseded and retired entries remain in the register with pointers, because a log that silently rewrites its history is not a log.

One axis was added in this revision because its absence was itself a small overclaim: **implementation status, orthogonal to decision status.** A "Fixed" decision about an unbuilt subsystem is fixed-as-intent; a "Fixed" decision citing deployed line numbers is fixed-as-observed — and uniform prose that does not distinguish them masks the implemented/unimplemented split behind one confident register. Three values: **Verified in code** (the implementation exists and was read), **Specified — not built** (the contract is complete; nothing runs), **Partial** (one side implemented, the other specified). Boundary and governance decisions that are not independently buildable carry "—". At this writing the verified cluster is the ingestion subsystem; nearly everything runtime- and client-side is specified-not-built, and the register now says so per entry.

**D-01. Skywalker is a retrieval backend behind MCP.**
**Status:** Fixed.
**Implementation:** —
**Decision:** Skywalker stops at scoped retrieval, evidence selection, and backend answerability. It is not the conversational layer.
**Rationale:** Multiple later sections depend on this boundary, especially the client integrations.
**Trigger to reopen:** Only a deliberate product decision moving conversational state or response rendering into Skywalker.

**D-02. Identity-aware scoping is part of correctness.** *(Revised June 10, 2026.)*
**Status:** Fixed.
**Implementation:** Partial — corpus-side vocabulary verified in code; the [API_01] contract revision and PAPI mapping are specified, not built.
**Decision:** The scoping dimensions are **country/geography, job level, and employee class**. Scope values are arrays; the corpus's applies-to-everybody values (`"Global"`, `"All Job Levels"`, `"All Employee Classes"`) are real data, and the per-dimension filter contract is `(employee value OR everybody value)`.
**Revision record:** The third dimension was originally specified as manager-versus-individual-contributor. The production corpus (`CorpusSchema.java`, 56-record CoreX export) scopes on `system_employee-class` with a vocabulary the manager/IC enum cannot express; a manager/IC filter would never match employee-class-scoped documents. Evidence class: implementation discovery falsifying a design assumption. The correction is closed, not open — [API_01]'s `role` enum is flagged stale and Section 02 owns the contract revision and the PAPI mapping.
**Trigger to reopen:** Repeated review showing the three-dimension tuple is insufficient for correctness (a fourth dimension), not vocabulary preference.

**D-03. PAPI runs before retrieval unless authoritative scope is supplied.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** Scope resolves through PAPI or arrives directly via the explicit-scope path; retrieval never starts unscoped.
**Trigger to reopen:** Clients routinely holding authoritative scope, making the default path structurally wasteful (C-08).

**D-04. Three MCP entry modes.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** Alias, employee-ID, and explicit-scope entry on the shared contract.
**Trigger to reopen:** A new client or trust model requiring a fundamentally different entry shape.

**D-05. Two retrieval arms.**
**Status:** Fixed.
**Implementation:** —
**Decision:** Controlled Top 50 FAQ arm plus UKB general arm.
**Trigger to reopen:** One arm ceasing to justify its cost, or the dual-arm model ceasing to fit the domain.

**D-06. The controlled arm is retrieval-backed, not cache-backed.**
**Status:** Fixed.
**Implementation:** —
**Trigger to reopen:** The Top 50 space no longer behaving like a retrieval problem at all.

**D-07. Daily polling, all-or-nothing rebuild, verified flip gate.** *(Revised June 10, 2026 — extended with implemented mechanics.)*
**Status:** Fixed baseline.
**Implementation:** Verified in code.
**Decision:** EventBridge daily cron (08:00 UTC launch default) triggers the Poller; the high-water mark is the single most-recent CoreX `lastModifiedDate`; on change, the entire corpus rebuilds into the idle physical index; promotion requires 100% node success (after per-node retries with full jitter) **and** read-back-verified queryability of the full expected count; partial or unverifiable builds never promote, the live corpus keeps serving, and the marker does not advance (`RebuildCoordinator.java`).
**Rationale:** The corpus is small enough that simplicity beats incremental machinery (tenet 4), and the flip gate makes "the runtime only ever sees a complete corpus" structural rather than aspirational.
**Trigger to reopen:** Rebuild duration approaching the Poller's 15-minute Lambda ceiling — duration, not document count.

**D-08. Strong FAQ match stays FAQ-only; otherwise both arms run.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Trigger to reopen:** The route model changing enough that one branch no longer exists independently (the always-query-both alternative remains tracked in Section 04 §4).

**D-09. One common reranking surface.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** FAQ and UKB candidates normalize and score together; arm-local scores are never compared.
**Trigger to reopen:** The common candidate surface or reranking layer proving structurally insufficient.

**D-10. Abstention is a valid backend outcome.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** Two reasons at launch: `NO_USABLE_EVIDENCE`, `EVIDENCE_TOO_WEAK_AFTER_RERANK`. Earlier four-reason drafts dropped the branches requiring semantic judgment Skywalker cannot compute or conflating arm identity with answerability.
**Trigger to reopen:** Product requirements forcing answers on weak evidence (rejected posture), or a new abstain category with a measurable signal computable without an LLM verifier (Section 07 Decision 11's bar).

**D-11. Multi-turn handling lives with client agents.**
**Status:** Fixed.
**Implementation:** —
**Trigger to reopen:** A deliberate product move of conversation-state ownership into Skywalker.

**D-12. Both launch clients are alias-first; the identity channel differs by auth combination.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** Slack and QuickSuite enter through the alias path. On the CloudAuth-inbound paths (Slack, UAT — D-38), the orchestrator initiates a TransitiveAuth token and Skywalker reads the alias from validated TA claims; on the Federate-inbound path (QuickSuite — D-19), the integration supplies `arguments.alias` because the gateway publishes no delegated-identity pattern for that combination. `arguments.alias` stays in the contract for non-TA callers; it is not a silent fallback on TA paths (D-39). Stated plainly: argument-supplied identity is a **strictly weaker trust guarantee** — the mode D-39 fails closed against — and QuickSuite operates in it by structural necessity; Section 09 §1 carries the full candor statement and what bounds the exposure.
**Trigger to reopen:** A client's natural identity material changing fundamentally, or the gateway publishing TA support for the Federate-inbound combination (at which point QuickSuite's migration is calibration-class — C-11).

**D-13. Human SME review is part of the launch calibration loop.**
**Status:** Fixed.
**Implementation:** —
**Trigger to reopen:** Production validation shifting to a different formal quality regime.

**D-14. One embedding model across evidence, query, and variants.**
**Status:** Fixed.
**Implementation:** Partial — ingest-side embedding verified in code; the runtime query path is specified, not built.
**Decision:** Cohere Embed v4, dimension 1024, strict input-type discipline (`search_document` at ingest and variant authoring, `search_query` live). One query embedding serves both the gate's cosine stage and the FAQ k-NN leg.
**Trigger to reopen:** A deliberate migration forcing coordinated regeneration of the variant artifact, evidence rebuild, and runtime change — architecture-class by definition.

**D-15. The evidence reranker is Cohere Rerank v4.0 Pro on SageMaker.** *(Revised June 10, 2026.)*
**Status:** Fixed (model and hosting pattern); instance type Open under D-41.
**Implementation:** Specified — not built.
**Decision:** The common scoring surface is Rerank v4.0 Pro self-hosted on SageMaker; the 32K-token context window comfortably holds 20 whole-node candidates plus the query. Hosting detail in D-25; the prior rationale's "1000-token chunk ceiling" framing died with the chunk model (D-26).
**Trigger to reopen:** Capability gap, deprecation, or deliberate migration.

**D-16. System-level latency budgets.** *(Revised June 10, 2026.)*
**Status:** Fixed.
**Implementation:** — (targets).
**Decision:** Retrieval pipeline **800–1000 ms p95**, raised from 250–450 ms at program direction; Slack end-to-end under 4 s p95 unchanged, so the client share tightens to ~3 s. The raise makes the budget a real envelope (worst-case component stacking ≈ 950 ms now sits inside it) and gives the evidence-rerank timeout (350 ms launch default) explicit headroom to grow toward 600–700 ms — which materially widens the D-41 instance field and shifts the bake-off from feasibility to cost-versus-quality.
**Revision record:** The 250–450 ms target was tighter than its own component timeouts could guarantee, and it priced the cheaper rerank instances out before any benchmark ran. Evidence class: program direction plus the stacking arithmetic the original entry already admitted.
**Trigger to reopen:** A sustained production breach unresolvable inside existing subsystem boundaries, or changed product requirements.

**D-17. Slack citation requirement.**
**Status:** Fixed.
**Implementation:** Specified — not built, and gated by the citation data gap recorded below.
**Decision:** Answerable results render with traceable citations to the returned evidence package (candidate `source_url` and `policy_links`).
**Dependency recorded — gates the June 30 deliverable:** the implemented ingestion writes `source_url`/`policy_links` empty, and whether the fix is wiring or net-new design is unestablished: if CoreX's `applicable-policy` carries policy codes rather than links, citations require a code-to-URL resolution table that does not exist. The earlier "data-wiring, does not gate the build" framing blurred the build/deliverable distinction in exactly the way this section warns against; Section 03 §9 carries the corrected statement and the inspect-the-fields-now deciding evidence. This is the open item most likely to slip June 30.
**Trigger to reopen:** A deliberate product replacement of citation with a different verifiable grounding discipline. Operational inconvenience is not sufficient.

**D-18. Slack orchestration via Bedrock Inline Agents with RETURN_CONTROL.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** `InvokeInlineAgent`, single action group (`skywalker_search_by_alias`), return-control handler performs the real MCP call; streaming on (`streamFinalResponse: true`) with rate-limited `chat.update` rendering; Converse API not used.
**Trigger to reopen:** Inline-agent deprecation, preview status blocking launch, or measured latency breaking the 4-second budget.

**D-19. QuickSuite via MCP Gateway, Federate-inbound, no wrapper.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** Federate OAuth (Auth Code + PKCE) against a Prod Service Profile under the pre-approved QuickSuite use case; Bindle authorization; Federate-inbound converts to CloudAuth-outbound; QuickSuite reads the real `tools/list`; identity rides in tool arguments. No wrapper Lambda, no AgentCore Gateway, no interceptor; the v2 wrapper design is preserved in git history only.
**Trigger to reopen:** Delegated identity shipping for this combination (→ C-11 migration); QuickSuite materially changing its integration model; a future client whose conventions force the wrapper question back open.

**D-20. MCP protocol revision `2024-11-05` at launch.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Trigger to reopen:** QuickSuite advancing its supported revision with real capability gains; movement is a coordinated update.

**D-21. Embedding invocation and vector substrate.** *(Revised June 10, 2026.)*
**Status:** Fixed.
**Implementation:** Partial — ingest side verified in code; runtime invocation specified, not built.
**Decision:** Embedding calls go through the **`us.cohere.embed-v4:0` cross-region inference profile** — the bare model ID is not invokable on Bedrock on-demand (HTTP 400, pinned empirically; IAM grants cover both the profile ARN and the underlying foundation-model ARNs, `serviceStack.ts:53-58,187-196`). Vectors store in AOSS under FAISS HNSW (`cosinesimil`, `m: 24`, `ef_construction: 128`, dimension 1024).
**Trigger to reopen:** A successor model whose migration value exceeds the coordinated rebuild cost, or a materially better AOSS engine option.

**D-22. Ingestion state: two SSM parameters.** *(Revised June 10, 2026.)*
**Status:** Fixed.
**Implementation:** Verified in code.
**Decision:** Exactly two pieces of state persist: the high-water marker (`/skywalker/ingestion/faq_evidence/last_snapshot_marker`) and the **live-index pointer** (`/skywalker/ingestion/faq_evidence/live_index`). The pointer exists because AOSS offers no alias to read (D-41a); everything else derives from the collection. No state table, no publish-status record, no revision counter — one scheduled writer needs none of it.
**Revision record:** The original entry held one parameter with the alias as the implicit live-pointer; the implementation added the pointer when the alias became unavailable. [API_07] documents the marker correctly and is flagged stale on the pointer.
**Trigger to reopen:** Multi-writer ingestion, untenable rollback frequency, or a real durable-publish-history requirement.

**D-23. AOSS alias swap for publication atomicity.**
**Status:** **Superseded by D-41a (June 10, 2026).**
**Supersession record:** AOSS Serverless does not support index aliases — the platform constraint was discovered at implementation. The alias-swap design (versioned `faq_evidence_v<N>` indexes, `faq_evidence_current` alias, retention-window GC) was replaced by the two-physical-index pointer flip. The ordering rationale (publication primitive first, marker second, so a failure between them yields a benign redundant rebuild rather than divergent live state) survives intact in the successor. [API_06]/[API_07] flagged stale.

**D-24. UKB invocation shape.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** The `retrieve` tool over stage-specific `iam/v1/mcp` endpoints, SigV4 after assuming the UKB-issued cross-account role, `targetUser` populated for native personalization, `additionalFilters` empty at launch, `content[]` resources as general-arm evidence. The scope guarantee on this arm is acknowledged as approximate (Section 06 §2) — UKB's personalization attributes correlate with, but are not, the scoping triple.
**Trigger to reopen:** UKB deprecating v1, changing auth, or changing contract shapes that affect the Section 06 seam.

**D-25. Evidence-reranker hosting.** *(Revised June 10, 2026.)*
**Status:** Fixed (hosting pattern); instance type Open under D-41.
**Implementation:** Specified — not built.
**Decision:** Two distinct always-on production endpoints across AZs with client-side round-robin (independent failure domains, independent rolling updates), one beta endpoint, no auto-scaling (cold start exceeds burst windows), VPC-isolated with network isolation on, Marketplace subscription `prodview-du2svpomxs5vw` at the flat $3.50/host-hour fee, blue-green `UpdateEndpoint` rollouts against immutable configs.
**Revision record:** The original entry fixed `ml.p5.4xlarge` in us-east-1, asserting A10G SKUs "push p50 to 600–900 ms and break the budget." That assertion is unbenchmarked; the Marketplace package supports `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge`, and the selection is now the D-41 bake-off. The region claim is superseded by D-42.
**Trigger to reopen (model/hosting):** Deprecation, a measurably superior successor, or Marketplace instance-support changes. Cost alone is not a trigger.

**D-26. Two-step chunker architecture.**
**Status:** **Superseded by D-43 (June 10, 2026).**
**Supersession record:** The `HierarchicalChunker`/`SemanticChildSplitter` design (parent/child structure, 1000-token ceiling, `child_order`/`split_type` reconstruction metadata) was never implemented and is unnecessary at the corpus's actual shape: FAQ nodes are answer-sized, the embedding window (128K) dwarfs any node, and 20 whole-node candidates fit the reranker's 32K window. Evidence class: implementation reality plus corpus measurement. The chunking design is preserved in git history; D-43 carries the concrete reopen trigger.

**D-27. Post-rerank sibling and linked-parent expansion.**
**Status:** **Retired (June 10, 2026).**
**Retirement record:** With whole-node fragments (D-43) there are no siblings to reconstruct and no chunk reassembly; with the linked-chain machinery retired (D-35) there are no linked parents to expand. The "rerank small, answer big" pattern is unnecessary when retrieval units are already answer-sized. The expansion query, reconstruction utilities, suppression metrics, and the scope-filter carve-out are all gone with it.

**D-28. SageMaker HA posture.**
**Status:** Fixed (shape); instance type Open under D-41.
**Implementation:** Specified — not built.
**Decision:** Two always-on endpoints across AZs in production, one in beta; fixed fleet rather than auto-scaling; capacity is for availability, not throughput (per-instance QPS far exceeds projected peak); a rollover dropping below one healthy endpoint is an incident.
**Trigger to reopen:** Sustained QPS approaching half a single instance's ceiling, or a cheaper HA strategy on the SageMaker side.

**D-29. Variant set as a static S3 artifact.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** Pre-embedded JSON in S3 ([API_11]), manually maintained, loaded at boot, hard-fail on missing/malformed (silent empty-set startup would route everything dual-arm — a hidden quality regression).
**Trigger to reopen:** Variant change cadence making manual updates painful, or a justified programmatic authoring flow.

**D-30. Hybrid routing gate.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** Stage 1 in-memory cosine against the variant set; stage 2 a contingent cross-encoder check fired only in the ambiguity band (~20% of traffic); stage-2 failure widens to dual-arm benignly. Stage 2's model is fixed by D-32.
**Trigger to reopen:** Evidence that always-rerank (Shape A) materially improves quality at acceptable latency, or that the contingent stage rarely changes stage-1 decisions and is not earning its latency.

**D-31. Control-plane values in SSM.** *(Revised June 10, 2026 — parameter list updated.)*
**Status:** Fixed.
**Implementation:** Partial — the ingestion parameters exist; the runtime refresh loop is specified, not built.
**Decision:** Tunable runtime values live under `/skywalker/runtime/` with periodic refresh and per-read metrics; architecture-class values (dimensions, HNSW build parameters, model IDs, instance types, endpoint ARNs) are deliberately not in SSM — they require coordinated changes and are governed by this register. Launch inventory:

- `/skywalker/runtime/gate/cosine_low_threshold` — 0.30
- `/skywalker/runtime/gate/cosine_high_threshold` — 0.80
- `/skywalker/runtime/gate/rerank_floor` — 0.50
- `/skywalker/runtime/gate/rerank_timeout_ms` — 300 *(standardized from the prior draft's 200; final value calibrates against the D-41 endpoint shape — C-17)*
- `/skywalker/runtime/abstain/floor` — 0.30
- `/skywalker/runtime/retrieval/ukb_timeout_ms` — 300
- `/skywalker/runtime/retrieval/per_arm_candidate_budget` — 10
- `/skywalker/runtime/retrieval/shortlist_size` — 5
- `/skywalker/runtime/retrieval/knn_overretrieve_k` — 40
- `/skywalker/runtime/retrieval/hybrid_bm25_weight` — 0.30
- `/skywalker/runtime/retrieval/ef_search` — 100
- `/skywalker/runtime/rerank/evidence_timeout_ms` — 350

*(The prior list's `/skywalker/runtime/retrieval/linked_text_token_cap` is removed with D-27's retirement.)* Every default is a first-order starting point expected to move against judged traffic.
**Trigger to reopen:** SSM throughput limits binding (unlikely at this scale), or a justified richer config surface.

**D-32. Gate stage-2 reranker is Cohere Rerank v4 on SageMaker.** *(Revised June 10, 2026.)*
**Status:** Fixed (model family and substrate); endpoint shape Open under D-41.
**Implementation:** Specified — not built.
**Decision:** All reranking in Skywalker runs the Cohere Rerank v4 family on SageMaker — the gate's cross-encoder stage included. The wire shape is the standard Cohere Rerank payload with `top_n: 1` over the 50 variant texts. Whether the gate runs a dedicated lighter endpoint (the leading candidate, for independent failure domains and right-sizing against a far lighter payload) or shares the evidence fleet — and which v4 family variant it runs — is folded into the D-41 bake-off.
**Revision record:** [API_10] Part B specified Bedrock-hosted Rerank 3.5 for the gate; executive direction (June 10, 2026: "we will use SageMaker endpoints running v4") supersedes it. One model family, one hosting pattern, one operational surface. [API_10] Part B flagged stale.
**Trigger to reopen:** Measured stage-2 latency breaching its timeout on the chosen endpoint shape, model deprecation, or consolidation evidence per the bake-off.

**D-33. Hybrid retrieval (BM25 + FAISS k-NN) on the FAQ arm, from launch.** *(Revised June 10, 2026.)*
**Status:** Fixed.
**Implementation:** Partial — pipeline creation verified in ingestion code; query-side fusion specified, not built.
**Decision:** Every FAQ-evidence call runs both legs, fused through the `skywalker-faq-hybrid` pipeline (`min_max` + `arithmetic_mean`; created best-effort by ingestion, `OpenSearchIndexManager.java:154-208`), with the scope `efficient_filter` on the vector leg. The BM25 leg scores `text` only — the implemented mapping carries no analyzed `title` field. Weights SSM-tunable (C-18). Hybrid-at-launch was confirmed as a decision, not an open item (June 10, 2026).
**Trigger to reopen:** One leg contributing nothing across the full weight range, or AOSS deprecating the normalization processor (forcing client-side fusion).

**D-34. Storage substrate: AOSS vector collection, FAISS, public-with-IAM.** *(Revised June 10, 2026.)*
**Status:** Fixed.
**Implementation:** Verified in code.
**Decision:** Collection `skywalker-faq-{stage}` (VECTORSEARCH), FAISS HNSW per D-21, AWS-owned-key encryption, standby replicas in prod, **public-endpoint-with-IAM-auth network policy** (`AllowFromPublic: true` with IAM as the enforced boundary — the standard posture for internal AOSS collections, `openSearchStack.ts:47-60`), cross-account read-only query role limited to read actions.
**Revision record:** The original entry claimed a VPC-endpoint network policy; the deployed policy is public-with-IAM. Code wins; the entry is corrected with the deployed posture.
**Trigger to reopen:** A materially better AOSS engine option, or pricing/operational shifts making a different store worth the migration.

**D-35. Cross-Q&A linked-item expansion.**
**Status:** **Retired (June 10, 2026).**
**Retirement record:** The depth-2 chain (custom "next" field, ingest materialization, cycle-breaking, unresolved-edge tolerance, the scope-filter carve-out) was never implemented; the index's `followup_fragment_ids` field is written empty and reserved (`FragmentProcessor.java:112-125`). If author-curated linking returns, it re-enters as a contract revision against that reserved field, recorded here — with the scope-carve-out question re-litigated on its own merits at that time. C-22 retires with it.

**D-36. Linked items do not enter the rerank pool.**
**Status:** **Retired (June 10, 2026)** — moot with D-35; its principle (source identity does not influence rerank scoring) survives independently in Section 07 Decisions 4 and 5.

**D-37. Citation contract for linked-item evidence.**
**Status:** **Retired (June 10, 2026)** — the `citations[]` envelope field, superscript markers, and segment concatenation are gone with the chain machinery; the envelope's citation surface is the candidates' `source_url` and `policy_links` (D-17). C-23 retires with it.

**D-38. Slack and UAT on CloudAuth-inbound with OBO + TransitiveAuth.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** Each orchestrator registers as a CloudAuth-modeled AAA application with `canInvoke` on the Bindle resource; OBO carries the orchestrator's identity; TA carries the human's, validated server-side. The SigV4-inbound route closed to new servers April 24, 2026; CloudAuth-inbound is the supported successor and the path with native OBO + TA ([API_14]).
**Trigger to reopen:** Gateway auth-shape changes, UnifiedAuth retiring the OBO + TA pair, or the orchestrator pattern changing.

**D-39. Fail closed on missing TransitiveAuth.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** A missing or invalid TA token on the CloudAuth paths is a protocol error — never a silent fallback to `arguments.alias`, which would let an orchestrator inject any alias and defeat the verified-claims posture. Pre-launch beta/gamma testing catches initiator/validator setup issues; production posture is hard failure.
**Trigger to reopen:** TA propagation proving unreliable enough that fail-closed produces outages disproportionate to the security benefit — resolved by hardening first (the default), an audit-only alarmed fallback second, and re-litigating the posture only after both.

**D-40. The gateway is the only internet-facing surface.**
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** Skywalker's MCP server registers with MCP Gateway and is reachable only through the gateway's authenticated termination at `api.mcp.asbx.aws.dev`; the architecture inherits the gateway's public-with-auth posture; the closed auth-shape set keeps the boundary against non-Amazon callers; admitting an external caller is a re-litigation event with AppSec review, never a calibration adjustment ([API_13]).
**Trigger to reopen:** A caller unable to use any supported auth shape; a genuinely external product surface; or gateway deprecation forcing posture confirmation on a successor substrate.

**D-41. Reranker instance selection — pending executive approval.** *(New, June 10, 2026.)*
**Status:** **Open. This is the register's one build-gating entry.**
**Decision pending:** A cost-versus-latency bake-off across `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` (the Marketplace-supported real-time set; flat $3.50/host-hour software fee; HA-pair all-in roughly $6.6K–$19K/month across the range). The same benchmark answers what timeout each type can hold on real ~20K-token payloads — public p50 estimates are H100-class and no per-instance benchmark exists. The revised D-16 budget gives the rerank timeout room to grow toward 600–700 ms, which makes the cheaper types realistic candidates and turns this primarily into a cost-versus-quality decision rather than a feasibility test. The benchmark also settles the gate's endpoint shape (dedicated versus shared, and the gate's v4 variant) per D-32.
**Binds:** D-15, D-25, D-28, D-32, D-16's budget verification, and the final cost envelope.
**Resolution path:** Executive approval → bake-off → instance pinned here as a Fixed revision, with measured latency distributions attached as the evidence record.

**D-41a. Publication atomicity: two physical indices plus an SSM live pointer.** *(New, June 10, 2026 — successor to D-23.)*
**Status:** Fixed.
**Implementation:** Verified in code.
**Decision:** `faq_evidence_a`/`faq_evidence_b` with `/skywalker/ingestion/faq_evidence/live_index` naming the live one; `beginRebuild()` recreates the idle index empty (removed content disappears; mapping changes deploy naturally; no orphans, no GC); promotion is one atomic pointer write, refused for any unknown index name; promote-then-marker ordering preserved from D-23's rationale. Rollback is the operator flipping the pointer back to the previous physical index, which still holds the prior complete build.
**Trigger to reopen:** The platform constraint lifting *and* a positive reason to prefer aliases emerging — the pointer model works regardless, so there is no standing pressure.

**D-42. Reranker region topology: no global fleet.** *(New, June 10, 2026 — researched and answered.)*
**Status:** Fixed (the topology question); the residual prod-region pick is Open.
**Implementation:** —
**Decision:** One HA pair, co-regional with the prod query service. The rerank hop is service-to-service — user geography never touches it, and no amount of rerank replication improves the front legs; SageMaker endpoints are strictly regional with no anycast option, so latency-motivated regions multiply cost for no benefit; per-instance throughput dwarfs internal traffic, so the fleet is sized by availability.
**Residual (Open):** Prod-region consistency — `ATESkywalkerQueryCDK` deploys alpha/beta to us-west-2 and gamma to us-east-1 (Allegiance prod placeholder also us-east-1), while UKB v1 prod is us-west-2-only and the implemented AOSS/ingestion stack is us-west-2; a us-east-1 prod query service pays ~60–75 ms cross-region on every UKB call and evidence read, eroding exactly the headroom a cheaper rerank instance needs. One region must be picked for the whole prod data plane; UKB's constraint makes us-west-2 the gravity well. Deciding evidence: UKB's region roadmap plus the prod account/region assignment.
**Trigger to reopen (topology):** The query service itself going multi-region active-active — a far larger architectural decision that nothing in the current design calls for.

**D-43. Whole-node fragments: one CoreX node, one document.** *(New, June 10, 2026 — successor to D-26.)*
**Status:** Fixed.
**Implementation:** Verified in code.
**Decision:** Each node's full extracted text (PlateJS walked depth-first, links preserved inline as `"text (url)"`) embeds as one fragment; no parent/child structure, no reconstruction metadata; `followup_fragment_ids` reserved and written empty; scope mapped from real corpus vocabulary with unscoped nodes skipped, never backfilled (`FragmentProcessor.java:79-92`).
**Trigger to reopen:** Node length outgrowing embedding or reranker budgets — the concrete trigger D-26's machinery waits behind, in git history.

**D-44. PAPI scope cache.** *(New, June 10, 2026 — reverses Section 02's original no-cache Decision 6.)*
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** An in-process, TTL-bounded cache of the post-mapping scope triple keyed by lookup identity (alias or person ID). TTL launch default 24 hours, SSM-held (`/skywalker/runtime/papi/cache_ttl_seconds`); no negative caching — failures never cache and fail-closed is unchanged on every miss; LRU-bounded; per-JVM in-memory only, never persisted. A hit skips PAPI entirely.
**Rationale:** Multi-turn conversations re-resolve an identical, months-stable triple every turn — wasted dependency load and repeated failure exposure for zero correctness gain. The named cost is the staleness window: an employee promoted or relocated inside the TTL is served prior scope — bounded, previous-correct-scope wrongness rather than the generic-scope mode tenet 3 forbids, diagnosable from the response because `scope_snapshot` always echoes the values used.
**Trigger to reopen:** A stale-scope incident traced to the cache (TTL moves down or invalidation machinery enters), or evidence the window should move materially (C-25).

**D-45. Shadow-routing on FAQ-only traffic.** *(New, June 10, 2026 — promoted from an open question to a commitment.)*
**Status:** Fixed.
**Implementation:** Specified — not built.
**Decision:** A sampled fraction of FAQ-only requests (`/skywalker/runtime/gate/shadow_sample_pct`, launch 0.10) asynchronously fires the UKB arm plus a shadow rerank of the combined pool after the user response has returned — off the latency path — logging whether a UKB candidate would have outscored the served FAQ winner.
**Rationale:** The FAQ-only short-circuit can answer worse than dual-arm would have, invisibly: rescue fires only on empty-or-unusable, not mediocre-but-passing, so a variant-similar-but-corpus-poor query answers from weak FAQ evidence above the abstain floor while UKB's better answer is never consulted. The system can see FAQ-only abstentions; without shadow data it cannot see FAQ-only answers that were worse than the road not taken — which leaves the gate threshold calibration (C-01/C-15/C-16) optimizing against half the failure surface. A quality cost you cannot measure is not a cost you have accepted. Shadow data also partially de-risks the September sample-size question (§9): it manufactures comparative evidence from live traffic in exactly the region — the ambiguity band and the FAQ-only margin — where organic volume will be thinnest.
**Trigger to reopen:** Shadow evidence of consistently safe short-circuits (sample down, C-26) or consistently worse ones (gate thresholds move, or always-query-both re-litigates per D-08/D-30).

### 11. Calibration register

Counterpart to the decision register: where the architecture is intentionally open because the right value comes from evidence. Identifiers stable; C-20 and C-21 were never assigned (historical numbering gap, preserved); C-22 and C-23 are retired with D-35/D-37.

**C-01. FAQ-only routing threshold.** Architecture fixed, numerics open. **Evidence:** variant-score distributions, judged examples, production review — and, indispensably, D-45 shadow data: without it this surface sees FAQ-only abstentions but not FAQ-only answers that were worse than dual-arm would have produced, and tunes against half the failure surface. **Escalates when:** the route shape itself appears wrong rather than the number inside it.

**C-02. Answerable-versus-abstain rule.** Two-branch composite structurally fixed (D-10); the floor calibrates (C-12); target abstain band 5–15% on Top 50 traffic. **Evidence:** judged evidence quality, SME review, measured abstain rates against the band. **Escalates when:** the answerability model is structurally insufficient, or the band cannot hold without abandoning grounding discipline.

**C-03. Reranker pool size.** Launch 20 (10 per arm dual-arm). **Evidence:** retrieval diversity and shortlist quality under judged traffic. **Escalates when:** common reranking cannot reconcile the route at all.

**C-04. Shortlist size.** Launch 5. **Evidence:** downstream grounding sufficiency and client usefulness. **Escalates when:** the evidence-packaging model itself appears wrong.

**C-05. Variant-set coverage.** Open and maintainable. **Evidence:** classification misses clustering by subject area, false positives, SME review. **Escalates when:** the Top 50 set no longer fits the domain.

**C-06. UKB normalization detail.** Open within the fixed requirement; the approximate scope alignment (D-24) is this surface's sharpest edge. **Evidence:** UKB result quality, provenance gaps, wrong-scope candidates surviving reranking. **Escalates when:** UKB can no longer be normalized honestly — or its personalization diverges from the scoping triple badly enough that explicit `additionalFilters` must carry scope (Section 06 surface three).

**C-07. Publication mechanics.** Open within the fixed all-or-nothing doctrine: work-item size (50), per-node retry budget (4 attempts, jittered), read-back budget (~100 s against AOSS refresh behavior). **Evidence:** rebuild outcomes, throttle behavior, AOSS refresh characteristics at real volume. **Escalates when:** corpus or platform pressure breaks the simplicity-first assumption (→ D-07's duration trigger).

**C-08. Client use of explicit-scope entry.** Open within the fixed contract; UAT uses it by design (Section 05); the custom-Midway-claims pattern UAT proves is the candidate mechanism if production clients ever move. **Evidence:** integration maturity, authoritative-scope availability, PAPI cost and failure exposure on default paths. **Escalates when:** the path proves structurally unsafe or unnecessary — or so clearly superior that retiring alias resolution becomes a Section 02 re-litigation.

**C-09. Abstain reason vocabulary.** Pinned at two values. **Evidence:** reviewer traces showing clients cannot produce distinguishable messages from the two classes, or that a third class would genuinely aid diagnosis. **Escalates when:** clients need fundamentally different result classes — supported by trace evidence and a computable signal, never theoretical fine-graining.

**C-10. The decision log itself.** **Evidence:** maintenance cost, usage in implementation work. **Escalates when:** too light to be useful, too heavy to maintain, or too detached from implementation to govern.

**C-11. QuickSuite consumption refinements.** *(Replaced June 10, 2026 — the prior entry described the dead AgentCore wrapper hosting question.)* Two active sub-surfaces: the **identity-carriage mechanism** (open at launch; session-metadata alias, QuickSuite-side alias source, or prompted scope — calibrates once the chat-agent author commits and production proves the choice) and the **Sources-UI workaround** (launch is chat-agent-side prompting; fallback is the `_sources` envelope field with its client-identity cost). **Escalates when:** prompting proves insufficient under production review (→ the `_sources` trade-off lands), or the gateway ships delegated identity for the Federate combination (→ the D-12/D-19 migration).

**C-12. The abstain floor.** Launch 0.30 (`/skywalker/runtime/abstain/floor`). **Evidence:** abstain rates against the band, SME review, production score distributions. **Escalates when:** no floor value expresses answerable-versus-abstain cleanly — the signal itself too noisy to act on.

**C-13. Per-arm candidate budget.** Launch 10. **Evidence:** reranker token headroom on real candidates, shortlist quality, starvation versus flooding. **Escalates when:** the budget must move drastically in either direction.

**C-14. UKB timeout.** Launch 300 ms. **Evidence:** measured UKB latency at volume, fallback rates attributable to timeout. **Escalates when:** the timeout drives answerability-degrading fallback frequency, or UKB is consistently fast enough to tighten.

**C-15. Gate cosine-band thresholds.** Launch 0.30 / 0.80. **Evidence:** band-fire telemetry, stage-2 verdicts on ambiguity traffic, judged near-miss examples, abstain rates correlated with routing, and D-45 shadow comparisons (the only signal that catches answered-worse-than-dual-arm). **Escalates when:** the band is so wide stage 2 fires on nearly everything (cosine too weak) or so narrow it never fires (stage 2 performative).

**C-16. Gate rerank floor.** Launch 0.50. **Evidence:** stage-2 verdict distribution, over/under-routing examples, SME review, and D-45 shadow comparisons on ambiguity-band traffic — the band is where shadow evidence is most decisive and organic volume thinnest. **Escalates when:** no floor value matches subject-matter expectations — at which point the stage-2 model or endpoint shape (D-41) is the question.

**C-17. Gate rerank timeout.** Launch 300 ms (standardized; the prior draft's 200 ms was sized against a fixed dedicated-g5 assumption that now lives inside D-41). The revised 800–1000 ms pipeline budget (D-16) gives this knob real room in both directions. **Evidence:** measured stage-2 latency on the bake-off's chosen endpoint shape. **Escalates when:** the chosen shape consistently runs hotter (grow, skip more aggressively, or resize) or much cooler (tighten).

**C-18. Hybrid per-leg weights.** Launch `bm25 = 0.30`. **Evidence:** judged misses on identifier-shaped versus paraphrase-shaped queries; per-query telemetry on which leg carried the winner. **Escalates when:** no weight serves both query shapes — at which point the fusion technique itself changes, a pipeline rebuild rather than a calibration update.

**C-19. Scope-filter over-retrieval.** Launch `k = 40` against `size = 20`. **Evidence:** how often the scope pre-filter (including the everybody-value terms) thins the population below `size`. **Escalates when:** over-retrieval must grow significantly, indicating filter selectivity bites harder than the corpus distribution suggested.

**C-22 / C-23.** **Retired (June 10, 2026)** with D-35/D-37 — the unresolved-edge tolerance and linked-segment token cap governed machinery that no longer exists.

**C-24. Ingestion cadence and alarm posture.** *(New, June 10, 2026.)* Launch: daily 08:00 UTC; deliberately loose alarm thresholds (error count, error rate, throttle rate, duration — documented as provisional in `monitoringStack.ts` because a new daily batch has no baseline and per-item failure tolerance means isolated errors must not page). **Evidence:** the CoreX corpus's real update timing; run-history baselines. **Escalates when:** one-day staleness becomes operationally unacceptable (→ D-07), or sustained decline-to-promote patterns show the staleness signal needs first-class alarming rather than threshold tuning.

**C-25. PAPI scope-cache TTL.** *(New, June 10, 2026; D-44.)* Launch 86400 s (`/skywalker/runtime/papi/cache_ttl_seconds`). The knob trades dependency load against the staleness window. **Evidence:** PAPI call-rate reduction, tail-latency contribution, and any stale-scope incident. **Escalates when:** staleness incidents recur at any TTL the load case can tolerate — at which point event-driven invalidation (an HR-change signal) re-litigates the TTL-only model.

**C-26. Shadow-routing sample rate.** *(New, June 10, 2026; D-45.)* Launch 0.10 (`/skywalker/runtime/gate/shadow_sample_pct`). **Evidence:** shadow-comparison volume per calibration surface versus UKB/rerank spend on shadow traffic. **Escalates when:** no rate simultaneously yields usable evidence and tolerable spend — unlikely, and recorded for completeness.

**C-27. Embedding-drift canary tolerance.** *(New, June 10, 2026; Section 04 §2.)* Launch 0.01 cosine drift (`/skywalker/runtime/gate/canary_drift_tolerance`), checked at boot by embedding a known variant text live and comparing against its stored vector — the behavioral check the header's string self-report cannot provide against Cohere point updates. **Evidence:** observed drift distribution across boots and embed-model releases. **Escalates when:** drift alarms fire without a corresponding model change (instrument problem) or a real model update lands (→ D-14's coordinated-migration trigger, not a tolerance tweak).

### Closing position

The rest of the series defines the system; this section defines how the system stays itself while still learning. A retrieval system with two arms, explicit identity shaping, an owned corpus published by verified atomic flips, a black-box general integration, one common reranking surface, structured abstention, and three client paths on one transport cannot be kept coherent by folklore. It needs a written discipline for what is fixed, what is calibratable, what evidence reopens a decision, and what to do when the evidence is not yet strong enough.

The June 2026 regrounding is the proof of concept: implementation reality and program direction pressured the register, and the register absorbed it the way it is designed to — revisions recorded, supersessions pointed at successors, retirements named, one gating open question (D-41) isolated and awaiting its evidence, one residual topology question (D-42) bound to the decision that will close it — and, in the same day's second pass, D-16 revised (the 800–1000 ms budget), D-44 added (the PAPI scope cache, reversing a recorded no-cache posture), D-45 added (shadow-routing, promoted from open question to commitment), D-17's deliverable-gating dependency stated without euphemism, and an implementation-status axis added so fixed-as-intent and fixed-as-observed stop reading identically. Not frozen, not dissolved: a durable log, re-litigation as a controlled technical event, and a governance layer that abstains from change when the record is not strong enough. That is the posture that preserves both honesty and momentum.

---

*Stale-source flags consolidated from this revision, for propagation: [API_01] `role` enum (D-02); [API_06] alias swap, chunk schema, sibling expansion, lucene engine (D-23/D-26/D-27); [API_07] alias-as-primitive and missing live pointer (D-22/D-41a); [API_10] Part B Bedrock gate model and Part A fixed instance/region posture (D-32/D-41/D-42); prior Section 10 entries D-23, D-26, D-27, D-35, D-36, D-37 and surfaces C-11 (wrapper form), C-22, C-23 (superseded or retired as recorded above).*

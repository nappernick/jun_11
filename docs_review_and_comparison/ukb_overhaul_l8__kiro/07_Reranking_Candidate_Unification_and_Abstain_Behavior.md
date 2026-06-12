## Section 07. Reranking, Candidate Unification, and Abstain Behavior

### 1. Tenets

Skywalker prefers evidence comparability over arm-local confidence when they conflict. FAQ hybrid score, FAISS similarity, BM25 contribution, and UKB's native ordering are useful inside the systems that produced them, but they are not a shared confidence language. The architecture therefore converges candidates into one envelope and judges their evidence text on one reranking surface.

Skywalker prefers backend honesty over always-answer behavior when evidence is weak. Abstention is a valid backend outcome that tells the caller the evidence package is not strong enough. It is not a user-facing refusal, an empty-list accident, or a downstream-agent decision disguised as retrieval success.

Skywalker prefers provenance-preserving unification over source-prestige scoring when they conflict. Source arm, native rank, source URL, policy metadata, route state, and scope stay attached to the candidate envelope, but they are not rendered into the reranker text surface. The reranker scores evidence content, not source labels.

Skywalker prefers no evidence over fabricated evidence. The reranking layer must not invent missing source details, citations, policy links, answer text, parent context, child chunks, linked-parent context, or support that was not present in the candidate envelope.

Skywalker prefers benchmark-gated capacity decisions over premature instance selection. Reranker fleet shape, endpoint count, timeout budget, and production instance type are selected only after measured results across `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge`, followed by L6 engineering and L6 finance approval.

### 2. Problem and Intent

Section 04 defines how the online runtime decides whether a request stays in the controlled FAQ arm or widens into the UKB general arm. Section 06 defines UKB as a black-box retrieval arm whose internal ranking is not controlled by Skywalker. This section defines the layer that must sit after those retrieval decisions: the common evidence scoring and answerability boundary.

The problem is that a two-arm retrieval architecture is unsafe if each arm carries its own confidence language all the way to the downstream agent. The FAQ arm can return controlled FAQ evidence ranked by the AOSS hybrid path. The UKB arm can return general-domain evidence ranked by UKB's opaque native system. Those rankings are not commensurable. If Skywalker directly compared them, it would silently convert implementation artifacts into an architecture-level source preference.

The intent is to make one backend layer responsible for evidence convergence, evidence ordering, and answerability. Both retrieval arms produce candidates. Candidates enter one common envelope. One reranker scores the evidence surfaces that are safe to compare. The backend then returns either an answerable ranked evidence package or a structured abstain package. Because Skywalker serves agents through MCP and does not render final conversational prose, this section returns evidence and answerability state, not final answers.

### 3. Boundary and Non-Goals

This section begins after online orchestration has produced a route record and a normalized candidate pool. It ends when Skywalker has returned either an answerable evidence package or a structured abstain package to the caller. The layer owns the convergence contract, reranker invocation contract, answerability decision, abstain reason, and preservation of provenance needed by downstream agents.

This section does not own routing policy. It does not decide whether a request should be FAQ-only, dual-arm, or degraded single-arm. It consumes that route record from Section 04.

This section does not own UKB retrieval semantics. UKB remains an opaque upstream arm. Skywalker preserves UKB provenance but does not reinterpret UKB's internal ranking as calibrated confidence.

This section does not own PAPI resolution, country or level scoping, employee-class role interpretation, or user identity resolution. It consumes the scoped request snapshot established earlier and keeps that scope attached to the evidence package.

This section does not own final answer generation, user-facing refusal language, multi-turn clarification, or escalation prose. Those behaviors belong above Skywalker. Skywalker returns answerability state and evidence that downstream agents can ground on.

This section does not perform parent/child expansion, sibling reconstruction, or linked-parent expansion at launch. FAQ candidates are returned as evidence records as indexed and retrieved. If future ingestion introduces chunk families or linked-parent structures, using that additional context is a separate architecture decision with its own evidence requirement.

### 4. Facts, Assumptions, and Consequences

The following facts are fixed by upstream architecture or by user-approved implementation truth.

- FAQ candidates are controlled-corpus evidence records produced by the FAQ retrieval arm. On FAQ-only requests, they are produced by the AOSS hybrid path before this layer receives them.
- UKB candidates are general-arm evidence records from a black-box retrieval system. Skywalker does not know or control UKB's internal scoring model.
- Native retrieval scores are not directly comparable across arms. They may be retained for provenance and debugging, but not used as a shared confidence metric.
- Abstain is a backend outcome returned by Skywalker when the evidence package is not answerable.
- The reranker scores evidence text and useful title context only. It must not fabricate evidence, citations, source metadata, policy links, or answer text.
- The launch implementation does not perform parent/child expansion, sibling reconstruction, or linked-parent expansion.
- Production GPU sizing is benchmark-gated across `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge`, then approval-gated by L6 engineering and L6 finance.

This section assumes the scoped request snapshot is already correct when it arrives. If that assumption proves false, reranking may faithfully rank evidence under the wrong country, level, or role scope, and the fix belongs in the scoping layer before this section.

This section assumes the common reranker is available often enough to be part of the normal request path. If that assumption proves false in beta or launch traffic, the fallback posture must be reopened because retrieval-order packages are not equivalent to reranked evidence.

This section assumes candidate text payloads are complete enough for standalone evidence scoring. If judged traffic shows that selected FAQ records are too coarse or too narrow, future ingestion may need chunk-family or linked-context work; launch behavior still must not invent that context at runtime.

This section assumes the launch abstain floor and shortlist size are calibration defaults, not permanent correctness claims. If judged traffic shows under-abstention, over-abstention, or unstable downstream behavior, Section 10 calibration work must adjust those surfaces with measured evidence.

### 5. Source-of-Truth Hierarchy

The source-of-truth hierarchy for this layer is about contract authority, not source prestige. It does not say FAQ evidence always beats UKB evidence, and it does not allow UKB evidence to override scoped request constraints.

The global source-of-truth hierarchy is:

1. `IngestionCodeReference`.
2. Implemented code paths.
3. Architecture documents, including adopted API and integration contracts.
4. Design discussions.
5. Future proposals.

The scoped request snapshot and route record are authoritative for what Skywalker attempted and which country, level, and role context governed retrieval. If the route record says the request ran FAQ-only, dual-arm, or degraded single-arm, that state remains visible in the final package.

The common candidate envelope is authoritative for what evidence may be scored and returned. Evidence outside the envelope is not inferred later. Missing title, missing URL, missing citation metadata, or missing policy metadata remain missing unless already present in source metadata.

The source documents and source metadata are authoritative for evidence text, source identifiers, source URLs, policy links, and citation metadata. The reranking layer may select and order candidates; it may not author new source facts.

The common reranker output is authoritative for relative ordering inside the candidate set it was shown. Its `relevance_score` is not a probability of correctness and must not be renamed or interpreted as calibrated confidence.

The answerability rule is authoritative for whether the backend returns an answerable evidence package or a structured abstain package. Routing confidence, source arm, and native scores do not bypass the answerability rule.

### 6. Inputs, Outputs, and Contracts

The input to this layer is a normalized candidate pool plus route record from online orchestration. Every candidate must carry a common runtime envelope with a stable runtime candidate identifier, source-arm marker, stable source identifier where one exists, rerankable evidence text, source title where available, source URL where available, policy-link metadata where available, arm-local rank or score metadata for provenance only, scoped request snapshot, and source metadata for FAQ candidates where present.

The reranker document string is intentionally narrower than the candidate envelope. It may contain useful title context and the candidate evidence text. It must not contain source-arm labels, source prestige cues, native rank, native score, route notes, source URL, administrative fields, or policy metadata. Those fields remain attached for provenance and output construction, but they are excluded from the scored text so the reranker judges evidence content rather than metadata status.

The normal output is an answerable evidence decision package. It contains the final ordered shortlist, route metadata, scoped request snapshot, source and policy metadata, citation metadata where available, reranker scores as relative ordering signals, and a positive answerability state.

The non-answerable output is a structured abstain package. It contains route metadata, scoped request snapshot, candidate provenance for the evidence examined when present, and exactly one launch abstain reason. Abstention must not be represented as a bare empty list because no evidence, weak evidence, and degraded-route evidence are materially different states for callers and for measurement.

The failure contract is conservative. Reranker failure is not retrieval failure, but reranker failure removes the normal common scoring surface. If a fallback returns retrieval-order evidence, the package must be explicitly marked with `reranker_state: RERANKER_FAILURE_FALLBACK` and must not claim normal reranked answerability. If the fallback cannot satisfy the conservative answerability rule, it returns structured abstain.

### 7. Fixed Decisions

**Decision 1: Both arms converge onto one common candidate envelope.** The rationale is that candidate unification is the only safe way to preserve heterogeneous provenance while allowing downstream scoring to operate on one contract. This binds the orchestration output, reranker input builder, citation builder, and abstain package schema. This decision reopens only if Skywalker removes the dual-arm architecture or replaces both retrieval arms with a single retrieval system that emits one calibrated evidence contract.

**Decision 2: Skywalker does not directly compare native arm scores.** FAQ hybrid scores, FAISS similarity, BM25 contribution, and UKB native ordering are retained as provenance only. The rationale is that direct comparison would create a hidden confidence model with no calibration basis. This binds answerability logic, tie handling, fallback behavior, and downstream package semantics. This decision reopens only if both arms produce a shared, validated calibration signal measured against judged traffic.

**Decision 3: FAQ-only routing does not bypass reranking.** FAQ-only means UKB did not need to run; it does not mean FAQ output is self-certifying. The rationale is that answerability and routing are separate judgments. This binds the request lifecycle for controlled-corpus requests and prevents shortcut paths that skip the common scoring surface. This decision reopens only if FAQ retrieval produces a separately validated answerability signal that is proven equivalent or superior to reranking on judged FAQ traffic.

**Decision 4: The reranker scores evidence surfaces, not answer surfaces.** The reranker input is title plus evidence text where useful, and excludes source prestige, arm identity, native rank, native score, URL, route notes, and administrative metadata. The rationale is that the reranker must judge whether the candidate text answers the query, not whether the source looks important. This binds the input renderer, logging redaction of scored payloads, reranker evaluation, and future source-aware tie-breaking work. This decision reopens only if judged near-tie traffic proves that a constrained metadata feature improves outcomes without turning source identity into a broad source prior.

**Decision 5: Abstain is a first-class backend state.** Skywalker may return a structured non-answerable package when evidence is absent or too weak. The rationale is that downstream agents should not have to infer answerability from an empty list or a low score. This binds MCP response semantics, measurement, caller handling, and launch dashboards. This decision reopens only if a higher layer becomes solely responsible for answerability and accepts an explicit contract to consume raw evidence without backend answerability state.

**Decision 6: Launch abstain taxonomy has exactly two normal reasons.** `NO_USABLE_EVIDENCE` applies when the reranker returns an empty shortlist or orchestration never produced enough usable candidates to rerank. `EVIDENCE_TOO_WEAK_AFTER_RERANK` applies when the top reranked candidate's `relevance_score` falls below the configured absolute floor, with launch default `0.30`. The rationale is that these reasons are computable without an LLM-side verifier and distinguish absence from weakness. This binds dashboards, caller behavior, and post-launch calibration. This decision reopens if hundreds-user traffic shows the two reasons are not behaviorally distinct or misses a measurable, backend-computable abstain condition.

**Decision 7: Launch does not include top-two disagreement or single-arm thinness abstain branches.** The top-two branch is excluded because Skywalker cannot determine whether two close candidates conflict or support the same answer without a verifier it does not run. The single-arm thinness branch is excluded because it would conflate arm identity with answerability. This binds answerability implementation and prevents hidden source-prior behavior. This decision reopens only if Skywalker adds a verifier or judged traffic identifies a backend-computable signal that predicts bad answers better than the current absolute floor.

**Decision 8: Launch does not reconstruct siblings, parent chunks, child chunks, or linked-parent context after reranking.** The rationale is that evidence returned to the caller must be evidence actually selected from the candidate envelope, not context assembled after scoring. This binds output construction, citation construction, and FAQ ingestion expectations. This decision reopens only if judged traffic shows one-record evidence is insufficient and a new ingestion or retrieval design explicitly supplies related context as first-class candidates or first-class metadata.

**Decision 9: Reranker production sizing is benchmark-gated and approval-gated.** The benchmark matrix must include `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge`, and must measure latency, throughput, error behavior, GPU utilization, GPU memory utilization, and cost under representative Skywalker candidate payloads. The selected production shape requires L6 engineering approval and L6 finance approval. This binds endpoint creation, launch capacity, timeout budget, and cost posture. This decision reopens only if the reranker vendor contract, SageMaker runtime constraint, or launch traffic model materially changes.

### 8. Reranker Service and Invocation Contract

The common scoring model is Cohere Rerank 4 Pro (`rerank-v4.0-pro`) served through a SageMaker real-time endpoint contract as described by API_10. The Java invocation path uses AWS SDK for Java v2 `software.amazon.awssdk:sagemakerruntime` and `InvokeEndpointRequest` against SageMaker Runtime. Endpoint names and timeout values come from runtime configuration rather than source-code constants.

The on-the-wire request body is a standard Cohere Rerank JSON payload containing `model: "rerank-v4.0-pro"`, `query` with the user's query text, `documents` with candidate document strings in common-pool order, `top_n` with launch default `5`, `max_tokens_per_doc` with launch default `4096`, and `api_version: 2`.

The response returns `results[]` entries with `index` and `relevance_score`. The `index` maps back to the original common-pool candidate envelope. The `relevance_score` is used for ordering and for the absolute abstain floor. It is not exposed or interpreted as a probability of correctness.

The Java client holds a long-lived `SageMakerRuntimeClient` per JVM, uses the configured reranker timeout budget from `/skywalker/runtime/rerank/evidence_timeout_ms`, and applies at most one short retry for transient transport failure. Failures beyond the configured retry enter the reranker-failure path instead of silently returning unmarked retrieval-order evidence.

SageMaker instance type, endpoint count, high-availability shape, auto-scaling posture, and timeout budget are not fixed production recommendations in this section. They are benchmark-gated calibration surfaces. Until benchmark evidence and approvals exist, the only fixed requirements are one common reranker service, observable failure behavior, and a conservative fallback or abstain path.

### 9. End-to-End Flow

The flow begins when online orchestration passes the normalized candidate pool and route record to this layer. The route record identifies FAQ-only, normal dual-arm, or degraded single-arm operation. The route record is carried forward; it does not by itself decide answerability.

The runtime validates that each candidate has the minimum envelope fields needed for reranking and output construction. A candidate with no rerankable evidence text cannot enter the reranker document list. A candidate that cannot be tied to source arm, expected source identifier, and scoped request context cannot enter an answerable shortlist.

The runtime renders reranker documents from useful title context and evidence text. It keeps source-arm markers, native rank, native score, route state, source URL, policy metadata, and citation metadata attached to the envelope but outside the scored text.

The common reranker scores the candidate universe it was shown and returns ordered result entries. This is the first point where FAQ evidence and UKB evidence are judged on one common surface.

The runtime maps each reranker result back to its candidate envelope, forms a bounded final shortlist, and preserves the original route and scope metadata. For FAQ candidates, the runtime returns the indexed evidence record text, source metadata, source URL, and policy-link metadata already present on the candidate. It does not issue a second reconstruction query, fetch sibling records, append child chunks, or expand linked-parent context at launch.

The runtime then applies the answerability rule. Empty or absent usable evidence returns `NO_USABLE_EVIDENCE`. A non-empty shortlist whose top `relevance_score` is below the configured floor returns `EVIDENCE_TOO_WEAK_AFTER_RERANK`. Otherwise, the package is answerable and is returned as ranked evidence with positive answerability state.

The final response is therefore one of two backend outcomes: a ranked evidence package that downstream agents can ground on, or a structured abstain package that explains why Skywalker is not returning answerable evidence for the request.

### 10. Failure and Abstain Behavior

An empty candidate pool after convergence is an immediate abstain with `NO_USABLE_EVIDENCE`. The backend has no evidence package to pass upward, and downstream agents should not be asked to infer that state from missing records.

A candidate pool with unusable evidence text is treated as no usable evidence for those candidates. If no candidates remain after validation, the backend returns `NO_USABLE_EVIDENCE`.

A non-empty reranked shortlist is not automatically answerable. If the top reranked candidate falls below the configured absolute floor, the backend returns `EVIDENCE_TOO_WEAK_AFTER_RERANK`. The launch floor is `0.30`, calibration-active per Section 10 C-07.

Reranker timeout, transport failure, malformed response, or model-side failure enters the reranker-failure path. If the runtime returns retrieval-order evidence, the package must be marked `RERANKER_FAILURE_FALLBACK`, must preserve route and provenance, and must use a stricter conservative answerability rule than normal reranked operation. If that stricter rule cannot be satisfied, the backend abstains.

Cross-arm disagreement is not a failure by itself. It becomes actionable only when represented by a backend-computable signal that is part of the answerability rule. At launch, Skywalker does not have an LLM-side verifier and therefore does not add a cross-arm-disagreement abstain reason.

Missing provenance is a structural defect. A candidate that cannot be tied to source arm, expected source identifier, and request scope cannot enter the answerable shortlist because the downstream agent could not ground or audit the evidence.

The target launch abstain rate for Top 50 traffic is 5 to 15 percent. Material deviation is a recalibration trigger. It is not permission to silently lower the floor, silently force answers, or reinterpret reranker scores as confidence.

### 11. Alternatives Considered

Skipping the common reranker and relying on arm-local ranking is rejected. It is attractive because it would reduce latency and remove a runtime dependency. It is rejected because it would force the backend or downstream agent to compare incommensurable FAQ and UKB ranking signals.

Using separate rerankers per arm is rejected as the baseline. It is attractive because each arm could be tuned against its own corpus shape. It is rejected because the system would still need a meta-comparison layer above the two rerankers, recreating the same unsolved comparability problem.

Adding a hard FAQ-over-UKB source prior is rejected. It is attractive because FAQ is the controlled corpus and should often be preferred for policy-specific answers. It is rejected as a fixed rule because it would pollute the common scoring surface with source prestige and would make dual-arm retrieval less useful precisely when UKB contains better evidence.

Using top reranker score as the entire answerability model is rejected. It is attractive because it is simple and measurable. It is rejected because answerability must also preserve route state, empty evidence, malformed candidates, fallback state, and provenance defects.

Forbidding abstention is rejected. It is attractive because it simplifies caller behavior and keeps the user-facing path answer-shaped. It is rejected because it would force downstream agents to answer from weak or absent evidence, violating the backend honesty tenet.

Returning retrieval-order evidence on every reranker failure is deferred rather than fixed as permanent doctrine. It is attractive because it preserves partial availability. It is not fixed because retrieval order is not equivalent to reranked order, and observed fallback quality may require hard abstain instead.

Adding parent/child reconstruction or linked-parent expansion after reranking is rejected for launch. It is attractive because richer context can improve final answer quality. It is rejected at this layer because it would append evidence that was not part of the scored candidate surface and could create citation or grounding drift.

### 12. Calibration Surfaces

The candidate-pool size entering rerank is calibration-active. Launch default is 20 candidates: 10 per arm on dual-arm requests, 20 from the surviving arm on single-arm fallback, and 20 FAQ candidates on FAQ-only requests.

The final evidence shortlist size is calibration-active. Launch default is 5, aligned with `/skywalker/runtime/retrieval/shortlist_size`.

The absolute abstain floor is calibration-active. Launch default is `0.30`. Movement requires judged-traffic analysis and subject-matter review rather than anecdotal pressure from individual failures.

The reranker-failure fallback posture is calibration-active. Observed fallback quality determines whether marked retrieval-order packages remain acceptable or whether reranker failure should become hard abstain.

Source diversity inside the final shortlist is calibration-active. No hard per-source or per-arm cap is fixed at launch.

Source-aware tie-breaking is calibration-active but disabled at launch. It may be considered only if measured near-tie behavior consistently produces the wrong winner and a constrained tie-break rule improves outcomes without polluting reranker input text with source prestige.

Candidate text rendering is calibration-active. Title plus body is the launch direction. Body-only or richer rendering requires evaluation evidence.

The SageMaker instance type, endpoint count, high-availability shape, auto-scaling posture, and timeout budget are benchmark-gated calibration surfaces. They cannot be finalized without measured results across `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge`, followed by L6 engineering and L6 finance approval.

### 13. Open Questions and Evidence Standard

Open questions in this section are post-launch or hundreds-users questions unless pre-launch benchmarks or integration tests show that a fixed decision is not implementable. The evidence standard for changing launch behavior is judged traffic, beta or launch usage from hundreds of users, benchmark data under representative payloads, or a reproducible production defect with clear blast radius. Individual anecdotes can start an investigation but do not by themselves reopen architecture decisions.

The Marketplace Model Package ARN remains open until subscription acceptance exposes the deployable ARN for the selected Cohere Rerank 4 Pro package. The decision is closed only when the ARN is deployable in the target account and region.

The final SageMaker production instance type, endpoint count, and high-availability topology remain open until benchmark evidence compares `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` under representative Skywalker payloads and receives L6 engineering plus L6 finance approval.

Whether real p95 latency stays inside the configured reranker timeout remains open until benchmark data and beta traffic are observed. If latency misses the budget, the timeout, candidate-pool size, endpoint shape, or fallback posture must be recalibrated with measured evidence.

Whether reranker failure should return a marked retrieval-order package or hard abstain remains open until operational traffic shows fallback quality. The evidence standard is judged fallback packages, downstream-agent behavior, and user-visible quality impact.

Whether the two abstain reasons produce distinguishable downstream behavior in QuickSuite chat-agent runtime and Slack remains open. If callers treat them identically or need a third backend-computable category, the taxonomy can be revised through Section 10 C-09.

Whether the system eventually needs a source-diversity rule or source-aware tie-break rule remains open until judged traffic shows systematic shortlist domination or near-tie misranking.

Whether unresolved cross-arm disagreement deserves a future explicit abstain category remains open. Any new category must be backed by a measurable signal Skywalker can compute without an LLM-side verifier, or by a separately approved verifier design.

Whether future ingestion should add parent/child chunks, sibling reconstruction, or linked-parent expansion remains open until judged traffic from hundreds of users shows that one-record evidence is too coarse for answer quality. Until that evidence exists, launch behavior remains no parent/child expansion and no linked-parent expansion.

### 14. Closing Position

The two-arm architecture works only if heterogeneous retrieval converges into one honest scoring and answerability layer. This section defines that layer as a contract: normalized candidates enter one envelope, evidence text is scored on one common surface, provenance remains attached but outside the scored text, and the backend returns either ranked evidence or structured abstain.

That boundary keeps the system buildable. Retrieval arms can remain different. UKB can remain opaque. FAQ can remain controlled. Downstream agents can remain responsible for final wording. Skywalker still has one place where evidence is judged together and one place where the backend is allowed to say the current evidence is not good enough.

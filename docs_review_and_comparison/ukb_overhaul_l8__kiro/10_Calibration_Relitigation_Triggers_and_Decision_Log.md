## Section 10. Calibration, Re-litigation Triggers, and Decision Log

## 1. Tenets

This section governs how Skywalker changes after the launch architecture has been written down. Its tenets are ordered. When two good instincts conflict, the earlier tenet wins.

1. We prefer source-backed truth over document continuity when they conflict.
2. We prefer explicit re-litigation over silent architectural drift when a fixed decision is challenged.
3. We prefer calibration through judged evidence over pre-launch certainty when the value is empirical.
4. We prefer narrow, trigger-based change control over standing process ceremony when the architecture is not under pressure.
5. We prefer recorded non-changes over untraceable repeated debate when evidence is not strong enough to reopen a decision.

These tenets intentionally separate architecture governance from runtime flexibility. Skywalker should be easy to tune where the design says tuning is allowed, and difficult to mutate where other contracts depend on the decision staying true.

## 2. Problem and architectural intent

Sections 01 through 09 define the launch architecture for Skywalker: a scoped retrieval backend behind MCP, a controlled Top 50 FAQ arm, a UKB-backed general arm, a shared candidate and reranking surface, structured abstention, and Slack, UAT, and QuickSuite clients that reach the backend through Amazon MCP Gateway. Once implementation pressure and production traffic arrive, the risk is not that every value will change. The risk is that the team will treat every change the same way.

Skywalker has two different classes of uncertainty. Fixed decisions define subsystem boundaries, source-of-truth behavior, identity contracts, client integration shape, or answerability semantics. Calibration surfaces define values that are expected to move after judged examples, benchmark data, reviewer feedback, and production behavior. Treating a fixed decision as a runtime knob causes drift. Treating a threshold as an architecture debate prevents the system from learning.

The architectural intent of this section is to make change disciplined without making it theatrical. It records what is fixed, what is empirical, what remains open, and what evidence would justify reopening a decision. It also defines the change-control contract: the inputs a proposed change must provide, the outputs the review can produce, the flow from trigger to decision record, and the failure behavior when evidence is incomplete or contradictory.

This is not a runtime request path. The boundary, contracts, flow, and failure behavior in this section apply to architecture change control. They govern how the architecture responds to new evidence after launch, not how a user query flows through retrieval.

## 3. Change-control boundary, contracts, and failure behavior

The change-control process receives evidence about the architecture and produces one of four outcomes: reaffirm, calibrate, re-litigate, or hold open. It does not replace implementation ownership, code review, launch readiness review, benchmark execution, or client-specific product decision-making. It binds those activities only when they would change a fixed architecture decision, move a declared calibration surface, or close an open decision.

The input contract for a change-control review is a change packet. A valid packet names the affected decision, calibration surface, or open decision; describes the trigger; cites the observed evidence; identifies the affected sections and downstream contracts; classifies the proposed change as calibration-class or architecture-class; and states the non-change rationale if the proposed outcome is reaffirmation. A packet that cannot cite observed evidence is not rejected because it is unimportant. It is held open because the process cannot safely distinguish a real architectural signal from implementation friction, anecdote, or preference.

The output contract is one recorded outcome:

- **Reaffirm:** The current decision remains fixed, and the record explains why the evidence did not justify change.
- **Calibrate:** A declared tunable value, threshold, timeout, weight, budget, cadence, or vocabulary changes without changing the architecture boundary.
- **Re-litigate:** A fixed decision or contract is reopened because the evidence challenges the design itself.
- **Hold open:** The issue is real enough to track but lacks the evidence required to change or close the decision.

The failure behavior is intentionally conservative. If evidence is missing, the output is hold open. If implementation truth conflicts with this document, the source-of-truth hierarchy in Section 4 decides the immediate truth and the document must be corrected. If a proposed change bypasses a fixed contract by calling it calibration, the packet fails closed into re-litigation. If a production incident requires emergency mitigation, the mitigation may proceed through the owning operational path, but any durable architecture change still needs a later decision record. If reviewers disagree about classification, the default is the smaller reversible action only when it stays inside an already declared calibration surface; otherwise the decision remains fixed until re-litigated.

## 4. Source-of-truth hierarchy

When this section conflicts with another artifact, use this hierarchy:

1. `IngestionCodeReference`.
2. Implemented code paths.
3. Architecture documents, including adopted API and integration contracts.
4. Design discussions.
5. Future proposals.

The most important consequence is ingestion truth. The launch ingestion implementation is not the older alias-swap, parent-child chunking, or linked-parent materialization design. The current source-backed launch facts are:

- Live FAQ index resolution uses two physical AOSS indices, `<base>_a` and `<base>_b`, plus an SSM pointer at `/skywalker/ingestion/faq_evidence/live_index`.
- The high-water marker is stored at `/skywalker/ingestion/faq_evidence/last_snapshot_marker`.
- One COREx node maps to one `FragmentDocument` and one AOSS index document.
- The implementation does not split one fetched node into a parent/child chunk hierarchy at launch.
- The implementation does not rely on a stable AOSS alias for live reads.
- Rebuilds are full rebuilds, not incremental mutation.
- Promotion is all-or-nothing. Incomplete, skipped, or unverifiable builds do not become live.
- Text is extracted from COREx RTE_V2 / PlateJS content.
- Scope uses real COREx values: geography maps to `country`, job level maps to `level`, and employee class maps to `role`.
- Nodes with missing text or missing required scope are skipped rather than force-filled.
- Vector embeddings use Bedrock Cohere Embed v4 at 1024 dimensions.
- The AOSS index uses FAISS HNSW cosine and the `skywalker-faq-hybrid` search pipeline for BM25 plus kNN hybrid retrieval.

Any later decision record that contradicts those points is stale unless the implementation changes first and the source-of-truth hierarchy is updated. The document is allowed to lag briefly during implementation, but it is not allowed to overrule implemented source-backed behavior by assertion.

## 5. Facts versus assumptions

### Fixed facts

- Skywalker is a retrieval backend behind MCP. It is not the conversational layer and not the final response-rendering layer.
- Identity-aware scoping is part of answer correctness.
- PAPI runs before retrieval unless a trusted caller supplies authoritative explicit scope.
- The MCP contract supports alias-based entry, employee-ID entry, and explicit-scope entry.
- Slack, UAT, and QuickSuite consume Skywalker through Amazon MCP Gateway.
- Slack and UAT use the CloudAuth-inbound route with CloudAuth OBO plus TransitiveAuth.
- QuickSuite uses the Federate-OAuth-inbound route and carries human identity as MCP tool arguments unless the gateway later supports delegated identity for that combination.
- The Top 50 FAQ arm and UKB general arm remain separate retrieval arms at launch.
- The FAQ arm uses hybrid BM25 plus FAISS vector retrieval as fixed launch architecture.
- Both arms normalize into a common candidate schema before common scoring.
- Abstention is a valid backend outcome.
- Multi-turn handling belongs above Skywalker in the agent/client layer.
- Human subject-matter review is part of launch calibration.

### Assumptions to validate

- Daily polling remains sufficient for Top 50 freshness.
- One COREx node per indexed document remains sufficient for initial answer quality.
- The FAQ variant set remains small and human-maintained enough to live as a static S3 artifact.
- Launch default thresholds and budgets are reasonable enough to begin review, even though they are not final.
- UKB response shape remains stable enough to normalize without making Skywalker responsible for UKB internals.
- SageMaker GPU instance sizing can be selected from measured benchmark data rather than from pre-launch estimates.

Assumptions are not facts. They can support launch posture, but they do not become fixed architecture until confirmed by implementation, judged traffic, benchmark results, or explicit decision.

## 6. Alternatives considered for change-control posture

The selected posture is trigger-based architecture governance with explicit calibration surfaces. It is intentionally narrower than a standing architecture board and stricter than informal team judgment.

### Alternative A. Informal change ownership by implementers

This is attractive because it is fast. The engineer closest to the code can tune thresholds, reshape ingestion, or adjust client behavior as soon as the need appears. That speed is useful for ordinary implementation work, but it is dangerous for Skywalker because several decisions are load-bearing across documents, clients, identity propagation, ingestion safety, and answerability semantics. Informal ownership would blur the line between a value that should move and a boundary that other systems rely on. This alternative is rejected because it violates the tenet that fixed decisions should not drift silently.

### Alternative B. Standing architecture review for every change

This is attractive because it maximizes central visibility. Every meaningful change would be reviewed in one place, producing a complete paper trail. The cost is that calibration would become too slow and too expensive. FAQ routing thresholds, hybrid weights, reranker timeouts, abstain floors, and validation windows need empirical adjustment as evidence arrives. Requiring full architecture review for every movement would turn expected tuning into process debt. This alternative is rejected because it treats calibration surfaces as if they were fixed architecture.

### Alternative C. Trigger-based governance with declared calibration surfaces

This is the selected approach. Fixed decisions are recorded with rationale, binds, and reopen criteria. Calibration surfaces are listed separately and may move when their evidence standard is met. Open decisions remain explicitly unresolved until post-launch evidence is strong enough to close them. The tradeoff is that the process depends on honest classification. That tradeoff is acceptable because misclassification has a defined failure behavior: if a proposed change alters a boundary, contract, source-of-truth behavior, or answerability semantics, it is architecture-class and must be re-litigated.

### Alternative D. Freeze all architecture until a formal post-launch review

This is attractive because it protects launch scope and prevents churn. It is rejected because it would force the team to ignore meaningful evidence that appears during UAT, benchmark runs, or early production. The launch architecture should be stable, not brittle. Evidence that a calibration value is wrong should not wait for a calendar ceremony, and evidence that a fixed decision is wrong should not be hidden until after user impact compounds.

## 7. Change-control flow

The flow begins when a trigger appears: a benchmark result, a judged answer-quality miss, a client integration conflict, an implementation contradiction, a dependency change, a production behavior pattern, or a reviewer escalation. The first step is classification. The owner identifies whether the pressure is on a fixed decision, a calibration surface, or an open decision. If the pressure is on an unlisted area, the owner must classify whether the proposed change would alter a boundary, contract, source-of-truth behavior, answerability behavior, or client integration shape.

The second step is source validation. If the issue involves ingestion, the implementation and `IngestionCodeReference` are checked before the architecture narrative is treated as current truth. If the issue involves an API, MCP, identity, or client contract, Sections 01 through 09 and the adopted integration contracts are checked before discussion notes or deferred proposals are used. This avoids re-litigating stale designs that have already been superseded by code or adopted contracts.

The third step is evidence review. Calibration changes require evidence appropriate to the surface: judged examples, benchmark data, latency distributions, route distributions, reviewer traces, skip causes, or production observations. Fixed decision changes require stronger evidence because they invalidate other work. A single anecdotal miss can create a tracked concern, but it does not reopen an architectural boundary by itself.

The fourth step is recording the outcome. A reaffirmed decision records why no change was made. A calibration records the old value, new value, evidence, blast radius, and rollback condition. A re-litigation records the decision being reopened, the replacement baseline under consideration, and the sections or contracts that must change if the new decision is accepted. A held-open item records the missing evidence and the next observation that would make the issue decidable.

The final step is propagation. If the outcome changes a fixed decision, every dependent section and implementation contract named in the decision's binds must be updated. If the outcome is calibration-only, the runtime configuration or operating value can change without rewriting fixed architecture, but the decision log must still preserve why the value moved.

## 8. Fixed decision register

### D-01. Skywalker is a retrieval backend behind MCP

**Decision:** Skywalker owns scoped retrieval, candidate normalization, common scoring, and backend answerability. Conversation state and final response rendering stay above it.

**Rationale:** Keeping Skywalker behind MCP as a retrieval backend gives Slack, UAT, and QuickSuite a shared evidence and answerability contract without forcing the backend to own client-specific conversation state.

**Binds:** MCP tool shape, client integration boundaries, abstention semantics, multi-turn ownership, and final response rendering.

**Reopen criteria:** Product direction deliberately moves conversation memory, answer drafting, or response rendering into Skywalker.

### D-02. Identity-aware scoping is correctness, not personalization

**Decision:** `country`, `level`, and `role` affect answer eligibility and evidence retrieval.

**Rationale:** Skywalker answers policy-like employment questions where the correct evidence can differ by geography, job level, and employee class. Treating scope as optional personalization would permit answers from the wrong eligibility universe.

**Binds:** PAPI resolution, explicit-scope entry, FAQ index filtering, UKB normalization, reviewer judgment, and abstention behavior.

**Reopen criteria:** Subject-matter review shows that the scope tuple is structurally insufficient, structurally wrong, or materially incomplete for the domain.

### D-03. PAPI precedes retrieval unless explicit authoritative scope is supplied

**Decision:** Alias and employee-ID entry resolve scope before search. Explicit-scope entry bypasses PAPI only when the caller already holds trusted scope values.

**Rationale:** Retrieval must run against the user's eligibility context. PAPI provides that context for identity-based entry, while explicit-scope entry exists for trusted callers that already have authoritative values.

**Binds:** Alias entry, employee-ID entry, explicit-scope entry, identity error handling, and retrieval filter construction.

**Reopen criteria:** A launch or post-launch client consistently holds authoritative scope and PAPI becomes structurally wasteful or unreliable for that path.

### D-04. Three MCP entry modes remain in the backend contract

**Decision:** The backend supports alias, employee ID, and explicit scope. Clients can use different modes without changing the retrieval backend.

**Rationale:** The three modes cover current client identity shapes while preserving one backend contract. Removing a mode would push identity assumptions into clients or require different backend implementations.

**Binds:** MCP API contract, caller onboarding, PAPI dependency behavior, test coverage, and client migration paths.

**Reopen criteria:** A new client, gateway capability, or trust model requires a materially different identity entry shape.

### D-05. Two retrieval arms are launch architecture

**Decision:** Skywalker launches with a controlled FAQ arm and a UKB-backed general arm.

**Rationale:** The controlled FAQ arm gives high-confidence behavior for the Top 50 launch scope. The UKB arm preserves broader coverage without making the controlled corpus pretend to cover everything.

**Binds:** Routing gate, candidate normalization, common reranking, answerability rules, calibration surfaces, and reviewer workflows.

**Reopen criteria:** Judged traffic shows one arm no longer justifies its role, or the dual-arm model creates answer-quality failures that cannot be corrected through routing and calibration.

### D-06. Hybrid retrieval is fixed for the FAQ arm

**Decision:** Every normal FAQ evidence retrieval uses both BM25 lexical retrieval over `text` and `title` and FAISS cosine vector retrieval over `embedding`, fused through the `skywalker-faq-hybrid` search pipeline.

**Rationale:** The FAQ corpus contains both exact-token signals and paraphrased user language. Hybrid retrieval protects identifier-shaped queries and semantic variants without forcing either retrieval leg to carry the full workload alone.

**Binds:** AOSS index mapping, `skywalker-faq-hybrid` pipeline, retrieval telemetry, calibration of BM25/vector weights, and judged FAQ evidence review.

**Reopen criteria:** Judged traffic shows one leg contributes no useful signal across the calibrated range, or AOSS search-pipeline behavior changes enough to force a new fusion design.

### D-07. Controlled FAQ ingestion follows the implemented two-index SSM-pointer model

**Decision:** The launch ingestion path uses two physical AOSS indices, `<base>_a` and `<base>_b`, plus the SSM live-index pointer at `/skywalker/ingestion/faq_evidence/live_index` and the SSM high-water marker at `/skywalker/ingestion/faq_evidence/last_snapshot_marker`. It does not use a stable AOSS alias for live reads.

**Rationale:** This matches implemented source-backed ingestion behavior. The architecture must follow the code-backed publication model rather than preserve an older alias-swap proposal.

**Binds:** Live index resolution, rebuild publication, rollback reasoning, ingestion validation, runbooks that inspect live state, and any document that describes FAQ evidence reads.

**Reopen criteria:** `IngestionCodeReference` is deliberately changed to a different publication mechanism, or production evidence shows the two-index pointer model cannot preserve publish safety.

### D-08. Controlled FAQ ingestion is full rebuild and all-or-nothing promotion

**Decision:** Source changes trigger a full rebuild of the idle physical index. The index becomes live only after a complete, validated build. Builds with skipped, incomplete, or unverifiable records do not promote.

**Rationale:** The launch corpus is small enough that full rebuild is simpler and safer than incremental mutation. All-or-nothing promotion prevents partial evidence sets from becoming the live answer source.

**Binds:** Daily polling, validation gates, skipped-node handling, SSM pointer updates, freshness expectations, and failure behavior for ingestion runs.

**Reopen criteria:** Corpus size, rebuild duration, or content-update frequency makes full rebuild operationally unworkable.

### D-09. One COREx node maps to one FAQ evidence document at launch

**Decision:** The implemented launch path maps each eligible COREx node to one `FragmentDocument` and one AOSS document. Parent/child chunking, sibling reconstruction, and linked-parent chains are not launch ingestion facts unless implemented later.

**Rationale:** The source-backed launch path is simpler than the older chunking proposal and avoids inventing document relationships the implementation does not materialize.

**Binds:** Evidence provenance, index document shape, retrieval granularity, reviewer traces, and any future chunking proposal.

**Reopen criteria:** Review shows one-document-per-node is too coarse or too noisy for answer quality, and the ingestion implementation is changed to support chunked evidence.

### D-10. Common candidate surface precedes common scoring

**Decision:** FAQ and UKB candidates normalize into one candidate schema before common scoring. The system does not compare arm-local scores directly.

**Rationale:** Local scores from different retrieval arms are not directly comparable. A common candidate surface lets the reranker and answerability logic reason over evidence rather than over arm-specific scoring internals.

**Binds:** UKB normalization, FAQ candidate shaping, common reranking, answerability rules, logging, and reviewer trace format.

**Reopen criteria:** UKB or FAQ evidence cannot be represented honestly in the shared schema.

### D-11. Abstention is a backend outcome

**Decision:** Skywalker may return structured abstain results when evidence is absent, weak, ambiguous, degraded, or outside scope.

**Rationale:** Backend honesty is safer than forcing a response when grounding is insufficient. Clients can render the abstain differently, but they must receive a meaningful backend outcome.

**Binds:** Answerability scoring, client response handling, reviewer judgments, calibration of abstain floor, and reason vocabulary.

**Reopen criteria:** Product requirements explicitly require the backend to answer even when grounding is insufficient.

### D-12. Slack and QuickSuite are asymmetric clients

**Decision:** Slack carries richer agent orchestration in the Slack-side application. QuickSuite remains a thinner MCP-consuming surface through its chat-agent runtime. Both consume Skywalker; neither changes Skywalker into a conversational state store.

**Rationale:** The clients differ in orchestration and identity propagation shape, but those differences should not leak into Skywalker's retrieval ownership.

**Binds:** Client integration boundaries, state ownership, MCP Gateway usage, identity propagation, and final response rendering.

**Reopen criteria:** Either client changes its integration model enough to move state ownership or identity propagation boundaries.

### D-13. MCP Gateway is the shared transport surface

**Decision:** Slack, UAT, and QuickSuite use Amazon MCP Gateway as the authenticated gateway to Skywalker. The Coral service behind the gateway is not the direct client-facing surface.

**Rationale:** A shared gateway keeps authentication and caller integration consistent across launch clients while preserving Skywalker as the backend retrieval service.

**Binds:** Client onboarding, auth route selection, tool exposure, direct service access assumptions, and integration testing.

**Reopen criteria:** MCP Gateway deprecates the needed route, a new caller cannot use any supported auth shape, or the product deliberately admits an external caller class.

### D-14. Embedding contract is Cohere Embed v4 at 1024 dimensions

**Decision:** FAQ evidence and the static variant set use Bedrock Cohere Embed v4 at 1024 dimensions.

**Rationale:** Evidence vectors and variant vectors must share a stable embedding contract for retrieval and routing calibration to mean anything.

**Binds:** AOSS vector dimension, embedding generation, static variant artifact, rebuild requirements, and benchmark comparability.

**Reopen criteria:** A successor embedding model or vector-store capability justifies a coordinated rebuild of the evidence index and variant artifact.

### D-15. SageMaker reranker model family is fixed; GPU sizing is benchmark-gated

**Decision:** The common evidence reranker uses Cohere Rerank v4 family on SageMaker. The final SageMaker instance type is not fixed by this document. `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` must all be treated as benchmark candidates, not preselected recommendations.

**Rationale:** The model family is part of the launch scoring design, but instance sizing is an empirical capacity and cost decision. Preselecting an instance before same-payload benchmarking would create false certainty.

**Binds:** Evidence reranker deployment, benchmark plan, latency and throughput targets, cost model, and finance/engineering approval.

**Reopen criteria:** Benchmarks show no candidate can satisfy quality, latency, and cost constraints, or the selected model/hosting substrate becomes unavailable.

### D-16. Routing gate cross-encoder sizing is benchmark-gated

**Decision:** The routing gate remains two-stage: in-memory cosine over the S3 variant set, with contingent Cohere Rerank v4 cross-encoder evaluation only in the ambiguity band. The dedicated gate endpoint instance type is not fixed by this document. `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` are benchmark candidates.

**Rationale:** The two-stage gate limits expensive cross-encoder work to ambiguous cases. The instance class must be selected by measured benefit, latency, throughput, and cost rather than by expectation.

**Binds:** FAQ routing, ambiguity-band calibration, gate benchmark plan, SageMaker endpoint planning, and production cost approval.

**Reopen criteria:** The two-stage gate rarely improves the cosine decision, adds unacceptable latency, or cannot be sized economically after benchmarking.

### D-17. Runtime tunables live in SSM Parameter Store

**Decision:** Tunable thresholds, budgets, timeouts, weights, and shortlist sizes live under `/skywalker/runtime/` and refresh without redeploy. Architecture-class values do not become casual runtime knobs.

**Rationale:** Calibration requires runtime adjustability, but only for declared empirical values. Keeping tunables in SSM separates controlled calibration from code deployment and prevents fixed decisions from being disguised as configuration.

**Binds:** Calibration rollout, auditability of runtime values, service refresh behavior, rollback of bad tuning, and classification of future configuration proposals.

**Reopen criteria:** SSM cannot support the operational or audit requirements and a typed configuration service becomes necessary.

## 9. Calibration surfaces

Calibration surfaces are intentionally empirical. They can move after evidence review without rewriting the architecture. A calibration record must state the previous value, new value, evidence, expected effect, rollback condition, and whether any fixed decision remains untouched.

### C-01. FAQ routing thresholds

**Values:** Cosine low/high thresholds and the stage-2 rerank floor.

**Evidence standard:** Gate telemetry, judged FAQ/non-FAQ examples, subject-matter review, and route-to-abstain correlation.

**Architecture boundary:** The routing gate remains two-stage unless D-16 is reopened.

**Re-litigation trigger:** No threshold range produces a sensible route distribution.

### C-02. FAQ variant-bank coverage

**Values:** Variant phrasing breadth, variant maintenance discipline, and update timing.

**Evidence standard:** Clustered misses, false positives, and reviewer-tagged phrasing gaps.

**Architecture boundary:** The launch variant set remains a static S3 artifact unless evidence challenges that storage or maintenance model.

**Re-litigation trigger:** The Top 50 space no longer behaves like a small controlled variant set.

### C-03. Hybrid retrieval weights

**Values:** BM25/vector weights in the `skywalker-faq-hybrid` pipeline.

**Evidence standard:** Identifier-shaped query performance, paraphrase query performance, winning-leg telemetry, and judged retrieval relevance.

**Architecture boundary:** Hybrid retrieval itself is fixed by D-06. Calibration may change weights, not remove a retrieval leg.

**Re-litigation trigger:** No weight combination preserves both exact-token and semantic retrieval quality.

### C-04. Retrieval depth and over-retrieval

**Values:** kNN over-retrieve depth, returned size, per-arm candidate budget, and `ef_search`.

**Evidence standard:** Scope-filter selectivity, candidate diversity, reranker starvation or flooding, and latency distributions.

**Architecture boundary:** Candidate depth may move; the common candidate surface remains fixed by D-10.

**Re-litigation trigger:** Depth must grow so much that the FAQ retrieval architecture or index design becomes the problem.

### C-05. Evidence reranker timeout and candidate budget

**Values:** Evidence reranker timeout, candidate-pool size, and final shortlist size.

**Evidence standard:** Benchmark results, production latency, downstream grounding quality, and reviewer feedback.

**Architecture boundary:** The common reranking layer remains part of launch architecture unless D-15 or D-10 is reopened.

**Re-litigation trigger:** The common reranking layer cannot produce stable answerability at any reasonable budget.

### C-06. SageMaker GPU instance selection

**Values:** `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` are benchmark candidates for the evidence reranker and gate reranker.

**Evidence standard:** Same-model same-payload benchmark data, p50/p95/p99 latency, throughput under expected concurrency, timeout rate, quality parity, cost at expected traffic, failover posture, availability behavior, and operational complexity.

**Decision gate:** Production use of `ml.g5.xlarge`, `ml.g5.2xlarge`, or `ml.p5.4xlarge` requires measured benchmark evidence plus explicit L6 engineering and L6 finance approval. This document makes no final instance recommendation.

**Re-litigation trigger:** Benchmarks invalidate SageMaker hosting or show that model choice, payload shape, or route shape must change.

### C-07. Abstain floor and reason vocabulary

**Values:** Absolute score floor, answerability rule details, and abstain reason set.

**Evidence standard:** Judged answerability, abstain rates, reviewer trace quality, and client handling.

**Architecture boundary:** Abstention remains a backend outcome unless D-11 is reopened.

**Re-litigation trigger:** The answerable-versus-abstain distinction cannot be expressed by the current scoring signal.

### C-08. UKB timeout and fallback behavior

**Values:** UKB per-request timeout, single-arm fallback handling, and degraded-result labeling.

**Evidence standard:** UKB p50/p95/p99 latency, timeout frequency, fallback answer quality, and user-visible degradation.

**Architecture boundary:** UKB remains the general arm unless D-05 is reopened.

**Re-litigation trigger:** UKB latency or contract behavior makes the general arm unreliable as launch architecture.

### C-09. UKB normalization detail

**Values:** Which UKB fields survive into candidate text, provenance metadata, and downstream evidence.

**Evidence standard:** Reranker behavior, missing provenance, noisy candidate text, and reviewer diagnosis.

**Architecture boundary:** UKB must normalize into the common candidate surface unless D-10 is reopened.

**Re-litigation trigger:** UKB output cannot be normalized honestly into the common candidate surface.

### C-10. Daily polling cadence

**Values:** Polling interval and marker mismatch handling.

**Evidence standard:** COREx change frequency, freshness complaints, rebuild duration, and failed-run rate.

**Architecture boundary:** Full rebuild and all-or-nothing promotion remain fixed unless D-08 is reopened.

**Re-litigation trigger:** Daily polling is too stale or too costly for real content operations.

### C-11. Ingestion validation gates

**Values:** Read-back wait window, expected-count validation, retry strategy, and skip handling thresholds.

**Evidence standard:** Daily run outcomes, skip causes, validation failures, and source-system instability.

**Architecture boundary:** Validation can be calibrated, but skipped, incomplete, or unverifiable builds do not promote unless D-08 is reopened.

**Re-litigation trigger:** Strict non-promotion on skipped nodes blocks launch freshness more often than it protects correctness.

### C-12. Client consumption of abstention

**Values:** Slack and QuickSuite user-facing treatment of abstain versus outage versus degraded fallback.

**Evidence standard:** Reviewer traces and user feedback showing whether clients preserve backend meaning.

**Architecture boundary:** Clients may render abstain differently, but the backend result class remains meaningful unless D-11 or D-13 is reopened.

**Re-litigation trigger:** Clients need backend result classes that the MCP envelope cannot express additively.

## 10. Open decisions

These items are intentionally not fixed at launch. They should be resolved only after post-launch evidence, including behavior across hundreds of active users, unless a pre-launch implementation blocker forces an earlier decision. Open decisions are not calibration surfaces. A calibration value can move inside an existing design; an open decision remains unresolved because the design does not yet have enough evidence to close it.

### O-01. Long-term decision-log home

**Open decision:** Whether the durable decision log should live primarily in this document series, in a repository artifact, or in a synchronized pair.

**Evidence needed:** Maintenance friction, review workflow, and whether implementation changes reliably update the authoritative record after launch.

**Closure standard:** Close after the team observes real decision updates across hundreds of active users or multiple post-launch change packets, unless repository workflow blocks launch traceability earlier.

### O-02. Formal review cadence

**Open decision:** Trigger-based review is fixed, but the cadence for reviewing accumulated triggers and non-change records remains open.

**Evidence needed:** Production issue volume, subject-matter reviewer throughput, number of unresolved calibration changes, and the rate at which held-open items become stale.

**Closure standard:** Close after post-launch operations show whether trigger-only review leaves too much unresolved work across hundreds of active users.

### O-03. One-document-per-node sufficiency

**Open decision:** Whether the current one-COREx-node-to-one-index-document model remains enough as real traffic broadens.

**Evidence needed:** Judged cases where correct answers require finer-grained chunking, parent/child reconstruction, sibling context, or linked-parent evidence.

**Closure standard:** Close only after judged traffic across hundreds of active users shows the current granularity is either sufficient or repeatedly harmful.

### O-04. Additional indexed metadata

**Open decision:** Whether more COREx metadata fields should be indexed beyond the current query needs.

**Evidence needed:** Repeated misses or incorrect inclusions that cannot be solved by the current `country`, `level`, and `role` filter set.

**Closure standard:** Close only after post-launch evidence shows a repeated metadata-driven failure mode that calibration cannot correct.

### O-05. Future delegated identity for QuickSuite

**Open decision:** Whether QuickSuite moves from argument-carried identity to a gateway-propagated delegated identity mechanism if MCP Gateway supports Federate-inbound plus CloudAuth-outbound delegated identity.

**Evidence needed:** Published gateway support, integration cost, security review of the new path, and observed friction in the launch identity model.

**Closure standard:** Close after the gateway capability is real and post-launch QuickSuite usage shows whether the current argument-carried model is materially limiting.

### O-06. Production SageMaker instance choice

**Open decision:** The final production instance type for the evidence reranker and gate reranker.

**Evidence needed:** Benchmarks across `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge`, using the same model family and payload classes, plus L6 engineering and L6 finance approval for any production selection.

**Closure standard:** Close only after benchmark evidence is reviewed. This document does not recommend a final instance type.

## 11. Re-litigation triggers

Architecture re-litigation is justified when one of these happens:

- Source-backed implementation truth contradicts the written architecture.
- A fixed client, identity, MCP, ingestion, retrieval, or answerability contract is no longer buildable as specified.
- Judged traffic shows a fixed architectural boundary is causing repeated wrong answers.
- Calibration cannot produce acceptable behavior within the declared surface.
- A dependency deprecates or materially changes a required contract.
- Production behavior across meaningful traffic invalidates a launch assumption.
- Cost or capacity cannot be corrected through benchmark-gated sizing and approved calibration.
- A proposed configuration change would alter a boundary, source-of-truth behavior, or failure contract.
- Post-launch behavior across hundreds of active users provides enough evidence to close an open decision.

Architecture re-litigation is not justified by:

- Preference for a cleaner abstraction without evidence.
- Local implementation inconvenience.
- A single anecdotal miss.
- A desire to tune a value that already has a calibration surface.
- A proposal that bypasses the source-of-truth hierarchy.
- A benchmark that is not comparable across the same model, payload class, and traffic assumption.
- A request to make the document match an older proposal that implementation has superseded.

## 12. Closing position

The launch architecture is stable where contracts and subsystem boundaries matter, and deliberately empirical where only traffic, benchmarks, and review can choose the right value. That distinction is the core governance decision.

The fixed decisions keep Skywalker coherent. The calibration surfaces let it learn. The open decisions stay open until post-launch evidence, including behavior across hundreds of active users, justifies closing them. Re-litigation is not a failure of the document; silent drift is. This section exists so that when Skywalker changes, the team can tell whether it is tuning the system, reopening the architecture, or simply recording that the evidence was not strong enough to change anything.

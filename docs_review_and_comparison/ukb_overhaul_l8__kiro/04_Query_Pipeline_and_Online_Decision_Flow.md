## Section 04. Query Pipeline and Online Decision Flow

### 1. Tenets

We prefer scoped correctness over broad retrieval when they conflict. A request that lacks a resolved country, level, and employee-class role is not broadened inside this layer; it has already failed the admission contract upstream.

We prefer controlled FAQ precision over unnecessary dual-arm cost when the request is strongly FAQ-shaped. The Top 50 FAQ arm exists because those questions are valuable enough to be owned, measured, and routed deliberately.

We prefer widening over silent under-retrieval when routing evidence is ambiguous or degraded. If the gate cannot confidently prove that FAQ-only is sufficient, the runtime widens to dual-arm retrieval and records why.

We prefer provenance-preserving convergence over fake score comparability. FAQ hybrid scores, vector similarities, BM25 contributions, and UKB ordering stay arm-local metadata until the common reranker judges normalized candidate text.

We prefer benchmark-gated capacity decisions over preselected GPU sizing. SageMaker instance type, endpoint count, and HA posture remain unresolved until measured evidence across the approved benchmark matrix supports an L6 engineering and L6 finance decision.

### 2. Problem and Architectural Intent

Sections 01 through 03 establish Skywalker's backend boundary, MCP entry contract, identity scoping, and controlled Top 50 FAQ corpus. This section defines the online runtime path that turns one valid scoped request into one normalized candidate package for the common reranking and answerability layer in Section 07.

The problem is not "search for an answer." Skywalker launches with two retrieval arms that have different ownership, failure modes, and score semantics. The controlled FAQ arm is owned by Skywalker and exists so high-value recurring questions are not delegated entirely to a black-box surface. The UKB arm is broad, external, MCP-backed coverage whose internal retrieval behavior is not owned by Skywalker.

The architectural intent is to make those two arms behave like one coherent backend retrieval system without pretending they are the same system. The query pipeline decides whether the request is FAQ-shaped enough for FAQ-only retrieval, when to widen into dual-arm retrieval, how to invoke each arm, how to normalize results without comparing native scores directly, and how to preserve route provenance for reranking, evaluation, and human review.

This layer is deliberately boring in the places where hidden creativity would create risk. It does not rewrite the user query, infer missing scope, author final responses, repair PAPI identity, rank final evidence, or manage conversation state. It receives a scoped request, routes it, retrieves candidate evidence, normalizes the results, and hands a route-aware candidate package forward.

### 3. Boundary and Non-Goals

This section is the online retrieval orchestration layer behind MCP. It starts after the canonical scoped request exists and ends before common evidence reranking and answerability. It owns route choice, retrieval-arm invocation, candidate normalization, route metadata, and degradation behavior for retrieval-stage failures.

It is not the MCP admission layer. Section 02 owns tool names, identity input forms, PAPI resolution, explicit-scope validation, and the rule that missing scope fails before retrieval.

It is not FAQ ingestion. Section 03 owns COREx enumeration, evidence document construction, embedding at ingestion time, physical index rebuilds, validation, promotion, and the SSM live-index pointer update.

It is not UKB. Section 06 owns the external UKB integration contract. This section treats UKB as a black-box retrieval arm and records what it returned or failed to return.

It is not final scoring or abstain judgment. Section 07 owns common reranking, final shortlist construction, and answerability. This section can return empty evidence, thin evidence, or failed retrieval state to that layer, but it does not decide final user-facing prose.

It is not parent/child reconstruction, sibling expansion, or linked-parent launch behavior. The current launch candidate pool uses the evidence documents as returned by retrieval. If future ingestion introduces parent/child chunks or linked FAQ chains, using that structure online requires a separate architecture decision.

It is not a standalone operational or compliance design. Runtime observability and access controls are referenced only where they bind this online retrieval contract.

### 4. Source-of-Truth Hierarchy

When this document conflicts with implementation-grounded material, the following hierarchy applies:

1. `IngestionCodeReference`.
2. Implemented code paths.
3. Architecture documents, including adopted API and integration contracts.
4. Design discussions.
5. Future proposals.

Within architecture documents, this section depends on Section 01 for the system boundary, Section 02 for MCP entry and identity scoping, Section 03 for controlled FAQ ingestion and live evidence publication, Section 07 for common candidate unification and reranking, and Section 10 for calibration and re-litigation posture.

The most important implementation-backed correction is the FAQ live read surface. AOSS Serverless does not supply the live indirection assumed by older architecture drafts for this path. The controlled FAQ evidence surface is published through two physical indices, `faq_evidence_a` and `faq_evidence_b`, plus the SSM live-index pointer `/skywalker/ingestion/faq_evidence/live_index`. Query-time FAQ retrieval reads that pointer and queries the selected physical index. Any text that says query-time retrieval uses a stable AOSS alias or named live target is stale unless implementation changes first.

The second important correction is launch evidence granularity. One eligible COREx node maps to one FAQ evidence document in the current implementation. The online path must not assume parent/child chunk hierarchies, sibling reconstruction, or linked-parent chains at launch.

The third important correction is GPU sizing. Neither this section nor a research note may be read as approving a production SageMaker shape. The only fixed posture is benchmark-gated selection across `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge`, followed by L6 engineering and L6 finance approval.

### 5. Facts, Assumptions, and Consequences

The canonical scoped request is a fact inherited from Section 02. It contains original query text, traceable caller identity, resolved country, resolved level, resolved employee-class role, and route metadata explaining how scope was obtained. This layer uses that scope as binding input. If scope is absent or invalid, the request should not reach this layer.

The controlled FAQ variant set is a fact inherited from API_11 and the current architecture. The artifact lives at `s3://skywalker-config-{stage}/variants/top_50.json`, one object per stage. It contains a header and `variants[]`. The launch header includes `schema_version` `1`, `embedding_model` `cohere.embed-v4:0`, `embedding_dimension` `1024`, `embedding_input_type` `search_document`, and informational `generated_at`. Each variant has stable `id`, `text`, and a precomputed 1024-float embedding.

At service boot, the runtime reads the variant artifact with `s3:GetObject`, validates schema version and embedding dimension, stores the variants in memory as `(id, text, embedding)` tuples, and emits `skywalker.variants.count` and `skywalker.variants.schema_version`. If the file is missing, malformed, empty, or dimensionally invalid, the service refuses to start. The consequence of starting with an empty variant set would be silent route drift away from the intended gate behavior, so there is no empty-set fallback.

The variant set is manually maintained, not produced by FAQ ingestion. A small update job or CLI embeds changed variant text with Cohere Embed v4 using `input_type: "search_document"`, writes a new `top_50.json`, and uploads it to S3. S3 versioning provides recovery. Running service instances pick up the file on next boot or rolling restart; launch does not require hot reload.

The controlled FAQ evidence surface is an AOSS-backed retrieval asset created by Section 03. The live physical index is selected by `/skywalker/ingestion/faq_evidence/live_index`; ingestion rebuilds the idle physical index and promotes it by flipping that SSM pointer. The consequence is that every FAQ retrieval call has an explicit runtime dependency on SSM pointer readability and pointer validity.

The UKB arm is an external MCP retrieval surface. It accepts a query and target-user context and returns UKB-side candidates in its own shape. Skywalker cannot assume UKB exposes the same fields, score semantics, freshness markers, or explanation detail as the FAQ arm. The consequence is that candidate normalization must tolerate missing arm-native metadata while preserving whatever provenance UKB does provide.

This section assumes the launch scoping triple of country, level, and employee-class role is sufficient for retrieval filtering. If judged traffic shows that correct answers require additional scope dimensions, the change must reopen the admission contract, FAQ index schema, UKB invocation shape, and candidate envelope together.

This section assumes 50 variants with 1024-dimensional vectors are small enough for in-memory cosine comparison on every request. If the variant bank grows enough to create measurable latency or memory pressure, the gate implementation may need a different local index structure, but that would not automatically change the two-stage routing decision.

This section assumes the routing-gate ambiguity band is a minority path. If production traffic places a large share of requests in the ambiguity band, the cost and latency posture of the gate reranker must be re-benchmarked and the thresholds may need recalibration.

This section assumes dual-arm retrieval is available often enough to be the normal widening path. If UKB availability or latency makes dual-arm retrieval unreliable, the failure behavior in this document still protects correctness, but launch quality would depend more heavily on FAQ coverage and would require re-litigation of the branch policy.

### 6. Inputs, Outputs, and Contracts

The primary input is the canonical scoped request from the orchestration layer. The minimum runtime fields are `query_text`, `correlation_id`, scoped identity fields, `country`, `level`, `role`, upstream scope-source metadata, and caller trace metadata. This layer must not mutate those fields into broader or guessed scope.

The runtime configuration inputs are SSM values for gate thresholds, rerank timeout, retrieval budgets, UKB timeout, hybrid weight, and FAQ over-retrieval depth. These values are calibration-active. Their names are part of the runtime contract because they allow calibration without a new architecture decision.

The static routing input is the S3 Top 50 variant artifact. Its boot-time validation contract is strict because an invalid variant file changes route behavior for every request.

The FAQ arm input is the scoped request plus the query embedding and the SSM-selected physical AOSS index name. The FAQ query must apply the `skywalker-faq-hybrid` search pipeline through the `search_pipeline` query parameter and must scope-filter on `country`, `level`, and `role`.

The UKB arm input is an MCP `tools/call` to `retrieve` with the user query, max results, and target-user context per API_09. The launch posture is no extra UKB `additionalFilters` unless API_09 and Section 06 are explicitly revised.

The normal output is one retrieval result package. It contains a route record, one normalized candidate pool, candidate provenance, arm status, arm counts, and a reranker handoff contract. It is not a final answer.

The route record must state the chosen path, cosine result, top variant id, whether stage 2 was skipped or invoked, stage-2 score or failure when applicable, arm failures or timeouts, fallback behavior, candidate counts by arm, and whether the pool is complete, thin, empty, or failed.

Every normalized candidate must carry a stable runtime candidate identifier, source arm, arm-local identifier where available, text payload suitable for reranking, source title where available, source URL where available, policy-link or source-link metadata where available, arm-local rank or ordering information, arm-native score metadata where available, publication or request trace where available, and the scoped request snapshot.

Missing arm-native fields remain absent or explicitly null. The runtime must not fabricate UKB score semantics, FAQ source details, policy links, or scope metadata to make the two arms look symmetrical.

The failure output is structured retrieval failure or empty-evidence state for the later abstain path. This section distinguishes transport failure, timeout, malformed arm response, empty evidence, thin convergence, and complete convergence because Section 07 and later client surfaces need those states to behave differently.

### 7. Fixed Decisions

#### D-04-01. The routing gate runs before retrieval-arm selection.

**Decision:** The runtime embeds the query, runs the gate, and then chooses FAQ-only or dual-arm retrieval. It does not call both arms first and retrospectively label the query.

**Rationale:** The Top 50 FAQ arm is valuable because strong FAQ-shaped requests can be handled with a controlled, owned path. Always calling both arms would erase that cost and precision advantage while still requiring route explanation later.

**Binds:** Route metadata, per-arm budgets, UKB cost, latency posture, and evaluation metrics all depend on route choice being explicit before retrieval.

**Reopen criteria:** Post-launch judged traffic from hundreds of real users shows that pre-retrieval route choice creates unacceptable misses that always-on dual-arm retrieval would materially reduce without unacceptable latency, cost, or explanation loss.

#### D-04-02. The launch gate is two-stage: cosine first, contingent rerank second.

**Decision:** In-memory cosine similarity over the S3 variant set runs first. Cohere Rerank v4 runs only when cosine lands inside the ambiguity band.

**Rationale:** Cosine is cheap and deterministic enough for obvious matches and obvious non-matches. A cross-encoder is more expensive but useful for paraphrase, negation, and compound queries where cosine alone is not trustworthy.

**Binds:** Variant artifact format, boot-time variant loading, gate-reranker endpoint contract, route metadata, and the SSM threshold set.

**Reopen criteria:** Evaluation shows that cosine-only, rerank-only, or always-dual routing produces materially better quality or simpler operations after measuring latency, cost, and misroute rate on real traffic.

#### D-04-03. Threshold values are calibration surfaces, not architecture.

**Decision:** `cosine_high_threshold`, `cosine_low_threshold`, `rerank_floor`, and gate timeout have launch defaults but are not fixed decisions.

**Rationale:** The architecture-class choice is the two-stage gate. The exact cut points must move with judged traffic, variant quality, and latency observations.

**Binds:** SSM configuration names, evaluation dashboards, and re-litigation rules in Section 10.

**Reopen criteria:** No re-litigation is required to move threshold values within the declared gate shape. Re-litigation is required only if threshold tuning is used to effectively eliminate a route or bypass a gate stage.

#### D-04-04. The query is embedded once per request with Cohere Embed v4.

**Decision:** The runtime calls Cohere Embed v4 using `input_type: "search_query"` once per request. The resulting 1024-dimensional vector is reused for cosine gating and the FAQ vector retrieval leg.

**Rationale:** Reusing one query embedding avoids needless latency and keeps the gate and FAQ vector leg aligned to the same embedding model and input contract. UKB remains separate because Skywalker does not own UKB internals.

**Binds:** Bedrock embedding dependency, FAQ vector query shape, variant embedding compatibility, and request latency budget.

**Reopen criteria:** The embedding model changes, FAQ and gate require incompatible input contracts, or benchmark evidence shows separate embeddings are necessary for quality.

#### D-04-05. The gate performs no query rewriting.

**Decision:** The gate decides where the request goes. It does not rewrite, expand, summarize, classify into hidden intents, or alter the user query before retrieval.

**Rationale:** Query rewriting would introduce another source of correctness risk and make route provenance harder to explain. Launch quality is better served by preserving the user query and widening when unsure.

**Binds:** FAQ retrieval query body, UKB invocation, route explanation, and downstream auditability.

**Reopen criteria:** Judged traffic shows a repeated, measurable failure mode that cannot be solved by variant coverage, threshold calibration, or reranker behavior, and a proposed rewrite contract can be tested without corrupting provenance.

#### D-04-06. Route outcomes are FAQ-only and dual-arm.

**Decision:** Strong FAQ-shaped requests invoke only the controlled FAQ evidence arm. Weak or ambiguous requests invoke the controlled FAQ evidence arm and UKB concurrently. There is no launch route that invokes UKB only by design.

**Rationale:** The FAQ arm is controlled, scoped, and intentionally small. Even broad requests may benefit from FAQ evidence, while UKB-only would remove the owned source from consideration before common reranking.

**Binds:** Candidate budgets, concurrency model, route metadata, and evaluation buckets.

**Reopen criteria:** Production evidence shows that FAQ retrieval materially harms broad-query quality or latency on a defined request class and that a UKB-only route can be recognized without reducing answer correctness.

#### D-04-07. FAQ retrieval uses hybrid BM25 plus FAISS vector search at launch.

**Decision:** Every normal FAQ evidence call runs both BM25 lexical retrieval over `text` and `title` and FAISS cosine vector retrieval over `embedding`, fused through the AOSS `skywalker-faq-hybrid` search pipeline using `min_max` normalization and `arithmetic_mean` combination.

**Rationale:** The FAQ corpus contains policy terms, names, and user paraphrases. Lexical and semantic retrieval cover different miss modes, and launch should not choose one as the only recall mechanism.

**Binds:** Section 03 index mapping, AOSS search pipeline existence, query body shape, FAQ candidate provenance, and retrieval evaluation.

**Reopen criteria:** Judged FAQ traffic shows one leg consistently harms quality or cost enough to justify removing it, or AOSS support constraints make the hybrid pipeline unavailable.

#### D-04-08. FAQ live reads use the SSM pointer and two physical indices.

**Decision:** Query-time FAQ retrieval reads `/skywalker/ingestion/faq_evidence/live_index` and queries the named physical index, expected to be `faq_evidence_a` or `faq_evidence_b`.

**Rationale:** This matches the implemented publication model in Section 03 and avoids assuming alias behavior that the launch path does not use.

**Binds:** FAQ query client, ingestion promotion contract, failure behavior when SSM is unreadable, and rollback model.

**Reopen criteria:** Implementation moves to a different proven live-read indirection and Section 03 is updated as the source of truth.

#### D-04-09. Native arm scores are not compared directly.

**Decision:** FAQ scores and UKB scores remain provenance. Candidate convergence produces one common envelope for the reranker rather than an arithmetic merge of arm-native scores.

**Rationale:** FAQ hybrid scores and UKB ordering are not calibrated to the same meaning. Direct comparison would create a hidden source preference and make quality debugging misleading.

**Binds:** Candidate schema, Section 07 reranker contract, evaluation analysis, and downstream explanations.

**Reopen criteria:** A future shared scoring system is introduced and validated across both arms with evidence that the scores have a comparable interpretation.

#### D-04-10. The online path remains stateless.

**Decision:** Retrieval is computed from the current scoped request, current configuration, current variant artifact, current FAQ live index, and current UKB response. Session memory is not part of retrieval behavior.

**Rationale:** Slack, QuickSuite, or downstream agents may own conversational memory. Mixing session state into this layer would blur ownership and make reproducibility worse.

**Binds:** MCP contract, cache strategy, route metadata, and reproducible evaluation.

**Reopen criteria:** A future client contract requires explicit multi-turn retrieval state and the ownership boundary is revised across client, MCP, and backend layers.

#### D-04-11. Parent/child and linked-parent behavior are not launch behavior.

**Decision:** The launch online path does not assume sibling expansion, parent reconstruction, linked-parent chains, or post-retrieval FAQ graph traversal.

**Rationale:** Current ingestion maps one eligible COREx node to one evidence document. Hidden reconstruction in the online path would depend on structure that is not part of the implemented launch corpus.

**Binds:** FAQ candidate normalization, Section 07 reranking input, citation construction, and evaluation of evidence granularity.

**Reopen criteria:** Ingestion implements parent/child or linked context as a real source-backed structure and judged traffic shows that using it improves answer quality enough to justify a new online contract.

#### D-04-12. SageMaker sizing is benchmark-gated.

**Decision:** The gate reranker uses a dedicated SageMaker real-time endpoint contract for Cohere Rerank v4, separate from the evidence reranker in Section 07, but production instance type, endpoint count, and HA posture are not selected here.

**Rationale:** The gate payload shape is different from evidence reranking: ambiguity-band traffic, `top_n: 1`, and 50 short variant documents. Sizing must be measured against that shape, not inherited from another endpoint or guessed from instance class names.

**Binds:** Benchmark plan, cost review, launch readiness gates, and any deployment document that creates the endpoint.

**Reopen criteria:** Benchmarking across `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` produces measured latency, throughput, utilization, failure behavior, and cost evidence, followed by L6 engineering and L6 finance approval of the selected production posture.

### 8. Alternatives Considered

Always querying both FAQ and UKB is attractive because it removes route-choice mistakes and gives the common reranker the broadest available evidence. It is rejected as the launch default because it spends UKB latency and cost on requests that are clearly inside the controlled FAQ set, weakens the purpose of the owned Top 50 path, and turns routing into after-the-fact explanation instead of an explicit runtime decision.

FAQ-only routing for all Top 50-like traffic without a dual-arm ambiguity path is attractive because it is simple and cheap. It is rejected because cosine alone cannot reliably detect paraphrase, negation, and mixed-intent questions. The architecture needs a widening path when the request is not confidently FAQ-shaped.

Cosine-only routing is attractive because it avoids a SageMaker gate-reranker dependency. It is rejected for launch because the expected hard cases are exactly the cases where vector similarity is a weak proxy for answer intent. The cross-encoder stage is limited to the ambiguity band to keep the dependency bounded.

Always invoking the gate reranker is attractive because it makes routing quality less dependent on cosine thresholds. It is rejected because the Top 50 variant set gives a cheap first pass, and obvious high or low cosine cases should not pay the cross-encoder latency and cost tax.

Using Bedrock-hosted reranking for the gate is attractive because it could reduce endpoint ownership. It is not the fixed launch path in this section. The launch path uses Cohere Rerank v4 through the SageMaker endpoint contract, with sizing benchmark-gated and approvals required before production selection.

Directly merging FAQ and UKB scores is attractive because it appears to simplify convergence. It is rejected because it would compare values that do not share semantics. The common reranker is the first shared judgment surface.

Using only vector retrieval for FAQ is attractive because the corpus is small and semantically embedded. It is rejected because policy language and FAQ phrasing include exact terms, acronyms, titles, and names that lexical retrieval can preserve better than dense retrieval alone.

Using only BM25 retrieval for FAQ is attractive because it is explainable and cheap. It is rejected because users often ask paraphrased questions that need semantic recall before reranking.

Resolving the FAQ live index through an AOSS alias is attractive because aliases are a familiar indirection pattern. It is rejected for the implemented launch path because Section 03 uses two physical indices plus the SSM live-index pointer, and query-time code must follow the implementation-backed publication contract.

Adding parent/child reconstruction or linked-parent expansion at launch is attractive because richer context can improve downstream answers. It is rejected because the implemented launch corpus does not publish that structure. Adding it later requires source-backed ingestion changes and judged evidence that one-document-per-node is insufficient.

Preselecting `ml.p5.4xlarge` or another GPU instance type is attractive because it would make deployment planning feel complete. It is rejected because endpoint capacity is a measured decision. The approved benchmark matrix is `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge`, and final selection requires L6 engineering and L6 finance approval.

### 9. End-to-End Flow

The online flow begins when the orchestration layer receives a canonical scoped request. The runtime builds a request context containing query text, scoped identity fields, upstream route metadata, correlation id, and handles to the currently published retrieval surfaces.

The runtime calls Cohere Embed v4 once with `input_type: "search_query"` and stores the 1024-dimensional query vector in the request context. If embedding fails, the runtime cannot run the gate or FAQ vector leg. That is a retrieval-stage failure, not an opportunity to degrade into unscoped lexical-only behavior.

Gate stage 1 computes cosine similarity between the query vector and every in-memory variant embedding. The runtime records the top score, top variant id, and whether the variant artifact was the expected schema version.

The gate reads SSM-controlled calibration values:

- `/skywalker/runtime/gate/cosine_high_threshold`, launch default `0.80`
- `/skywalker/runtime/gate/cosine_low_threshold`, launch default `0.30`
- `/skywalker/runtime/gate/rerank_floor`, launch default `0.50`
- `/skywalker/runtime/gate/rerank_timeout_ms`, launch default `200`

If `top_cosine > cosine_high_threshold`, the route is FAQ-only and stage 2 is skipped. If `top_cosine < cosine_low_threshold`, the route is dual-arm and stage 2 is skipped. Otherwise, the runtime invokes gate stage 2: Cohere Rerank v4 on SageMaker against the 50 variant texts with `top_n: 1`. If the top `relevance_score > rerank_floor`, the route is FAQ-only; otherwise the route is dual-arm.

The stage-2 request body uses `model: "rerank-v4.0"`, `query` set to the user query text, `documents` set to the stable-order variant texts, `top_n: 1`, `max_tokens_per_doc: 4096`, and `api_version: 2`. The response `results[]` supplies the winning document `index` and `relevance_score` in `[0, 1]`.

The gate-reranker SageMaker endpoint is a separate real-time endpoint named `skywalker-gate-rerank-v4-{stage}`. It is invoked with SigV4 using the query service's IAM execution role scoped to the gate endpoint ARNs. The Java SDK artifact is `software.amazon.awssdk:sagemakerruntime`. Endpoint sizing must be selected only after benchmarking `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` against the launch traffic shape: ambiguity-band traffic, `top_n: 1`, and 50 short variant documents. Production sizing, endpoint count, and HA require L6 engineering and L6 finance approval.

If stage 2 times out, returns a transport failure, returns malformed output, or cannot be authorized, the gate widens to dual-arm and records the event. A gate stage-2 failure does not stop retrieval because the conservative routing outcome is to gather more evidence, not less.

On the FAQ-only path, the runtime invokes the controlled FAQ evidence arm. On the dual-arm path, it invokes the controlled FAQ evidence arm and UKB concurrently. Concurrency is used only after route choice; it is not a substitute for route choice.

The FAQ evidence invocation first resolves the live physical AOSS index by reading `/skywalker/ingestion/faq_evidence/live_index`. The value is expected to name one of the two physical indices, `faq_evidence_a` or `faq_evidence_b`. The query is sent to that physical index with the `skywalker-faq-hybrid` search pipeline applied through the `search_pipeline` query parameter.

The FAQ query body uses the AOSS `hybrid` query type. The lexical leg is a `match` clause on `text` and `title`, with `title` receiving a smaller boost. The vector leg is a `knn` clause on `embedding` using the request's 1024-float query vector, launch `k: 40`, and a FAISS `efficient_filter` that pre-filters on `country`, `level`, and `role`.

The hybrid pipeline normalizes both leg scores with `min_max` and combines them with `arithmetic_mean`. Launch weights are `[0.3, 0.7]` for BM25 and vector respectively. The BM25 weight is SSM-tunable at `/skywalker/runtime/retrieval/hybrid_bm25_weight`; the vector weight is derived as the complement. The launch over-retrieval depth `k: 40` is calibration-active at `/skywalker/runtime/retrieval/knn_overretrieve_k`.

The launch candidate budget is 20 FAQ candidates on FAQ-only routes and approximately 10 FAQ candidates plus 10 UKB candidates on dual-arm routes. Per-arm candidate budget is calibration-active at `/skywalker/runtime/retrieval/per_arm_candidate_budget`, launch default `10`.

The UKB invocation is an MCP `tools/call` to `retrieve` with `query`, `maxResults = UKB_CANDIDATE_BUDGET`, and `targetUser` populated from resolved identity per API_09. Launch UKB timeout is `300 ms`, held at `/skywalker/runtime/retrieval/ukb_timeout_ms`.

After arm-native candidate reception, the runtime normalizes each candidate into the common envelope. It preserves source arm, arm-local rank, arm-local identifier, available source metadata, available source URL or policy links, native score metadata where provided, and scope snapshot. It also preserves arm-level counts so a thin dual-arm result is distinguishable from complete convergence.

The reranker handoff contains one 20-candidate pool when enough candidates exist. FAQ-only normally contributes 20 FAQ evidence documents. Dual-arm normally contributes roughly 10 FAQ evidence documents and 10 UKB passages. Single-arm fallback uses the surviving arm's candidates. Launch does not assume sibling expansion, parent reconstruction, or linked-parent chains after this handoff.

Before leaving this layer, the runtime finalizes route metadata: cosine result, stage-2 invocation status, stage-2 score or failure, chosen route, arm statuses, candidate counts by arm, fallback status, and whether the pool is complete, thin, empty, or failed.

### 10. Failure and Abstain Behavior

Embedding failure stops normal retrieval because the same query vector is required for both the gate and FAQ vector retrieval. The runtime returns a structured retrieval-stage failure for downstream handling rather than silently switching to an unscoped or lexical-only path.

Variant artifact failure at boot prevents service start. Variant artifact failure is not handled per request because a running process should already have validated the artifact. This makes route corruption visible as deployment or startup failure rather than silent production drift.

SSM threshold read failure should use the last known valid in-process value when one exists and mark configuration state in route metadata. If no valid value exists at startup for required gate thresholds, startup should fail. Calibration values are allowed to move, but undefined routing behavior is not allowed.

Gate stage-2 failure widens to dual-arm and records the widening. The gate is a routing aid, not a hard prerequisite for retrieval.

If a request is routed FAQ-only but the FAQ evidence call fails or returns no usable candidates, the runtime widens to UKB rather than returning no evidence solely because the preferred arm failed. The route record must show that this was an FAQ-only route with fallback widening, not a normal dual-arm route.

If dual-arm retrieval is selected and one arm fails while the other succeeds, the request continues with the surviving arm and is recorded as a single-arm fallback. The surviving arm's candidates are not relabeled as complete dual-arm evidence.

If both arms fail, or if the surviving arm returns no usable candidates, the runtime returns a structured retrieval failure or empty-evidence condition to the later abstain path. It does not fabricate a candidate pool, repeat stale evidence, or ask the downstream agent to guess.

A successful arm call that returns no usable candidates is a retrieval miss, not an outage. The route metadata distinguishes transport failure, timeout, malformed response, empty evidence, thin convergence, and complete convergence.

This section preserves abstain as a possible downstream outcome. A coherent route can still produce evidence too weak to answer. Section 07 owns the final abstain decision, but this section must supply enough route and candidate-quality metadata for that decision to be explainable.

### 11. Calibration Surfaces

Calibration surfaces are runtime-tunable or evidence-sensitive values. They do not change the architecture-class decisions above.

The cosine-high threshold at `/skywalker/runtime/gate/cosine_high_threshold`, launch default `0.80`, controls how confidently cosine can route directly to FAQ-only.

The cosine-low threshold at `/skywalker/runtime/gate/cosine_low_threshold`, launch default `0.30`, controls how confidently cosine can route directly to dual-arm.

The Rerank v4 floor at `/skywalker/runtime/gate/rerank_floor`, launch default `0.50`, controls how strict the stage-2 cross-encoder must be before FAQ-only routing.

The gate-reranker timeout at `/skywalker/runtime/gate/rerank_timeout_ms`, launch default `200`, controls when stage 2 widens to dual-arm because the second opinion did not arrive in time.

The per-arm candidate budget at `/skywalker/runtime/retrieval/per_arm_candidate_budget`, launch default `10`, controls how much each arm contributes before common reranking on dual-arm routes.

The FAQ-only candidate budget is calibration-active but must remain compatible with the Section 07 reranker pool size. Launch posture is 20 FAQ candidates when enough candidates exist.

The UKB timeout at `/skywalker/runtime/retrieval/ukb_timeout_ms`, launch default `300`, controls how long the runtime waits for the black-box arm before preserving the surviving path.

The hybrid BM25 weight at `/skywalker/runtime/retrieval/hybrid_bm25_weight`, launch default `0.30`, controls the lexical/vector blend in FAQ retrieval. The vector weight is the complement, launch `0.70`.

The FAQ vector over-retrieval depth at `/skywalker/runtime/retrieval/knn_overretrieve_k`, launch default `40`, controls vector leg depth before pipeline fusion and result shaping.

The variant bank is calibration-sensitive. Adding, removing, or rephrasing variant texts can improve gate behavior without changing the two-stage gate architecture, provided the artifact schema and embedding contract remain stable.

The candidate envelope is a calibration-sensitive contract surface only through explicit revision. If evaluation shows the reranker needs additional arm-native metadata promoted into scored text or structured features, that change must be recorded as a contract revision rather than ad hoc field leakage.

The branch policy is calibration-sensitive after launch. Runtime evidence may show that FAQ-only is too narrow, too permissive, or not valuable enough relative to always querying both arms. Until measured evidence justifies re-litigation, the launch architecture remains FAQ-only for strong FAQ-shaped requests and dual-arm for weak or ambiguous requests.

The SageMaker endpoint shape for the gate reranker is benchmark-gated. The calibration surface includes instance type, endpoint count, HA posture, timeout budget, and cost acceptance, but movement requires the benchmark and approval process described in D-04-12.

### 12. Open Questions and Evidence Standard

Open questions in this section are not launch blockers unless integration tests or pre-launch benchmarks show that a fixed decision above is not implementable. They should be answered with post-launch evidence, including judged traffic, failure analysis, cost data, latency measurements, and behavior from hundreds of real users rather than pre-launch preference.

Whether Skywalker ever populates UKB `additionalFilters` remains open. The launch posture is no additional UKB filters beyond `query`, `maxResults`, and `targetUser`. Closing this question requires evidence that UKB-side filters improve answer quality or latency without creating mismatched scope semantics.

Whether additional arm-level fields should be promoted from provenance into the reranker's scored surface remains open. Closing this question requires judged cases showing that the current title-plus-evidence text loses information necessary for the common reranker.

Whether the variant bank has enough paraphrase coverage remains open. Closing this question requires measured misroute analysis over real traffic, not a pre-launch list review alone.

Whether FAQ-only routing remains valuable at scale remains open. Closing this question requires comparing quality, latency, UKB dependency rate, and cost across hundreds of real user requests where strong FAQ-shaped traffic is separable from broad or mixed-intent traffic.

Whether the current launch candidate budget is sufficient remains open. Closing this question requires reranker input analysis showing when correct evidence is absent from the 20-candidate pool and whether misses come from FAQ recall, UKB recall, route choice, or budget truncation.

Whether one COREx node per FAQ evidence document remains sufficient remains open. Closing this question requires judged answer-quality failures showing that parent/child chunks, sibling reconstruction, or linked FAQ context would have changed the result.

Whether the final gate-reranker SageMaker instance type, endpoint count, and HA topology are acceptable remains open until benchmarks compare `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` under representative gate payloads and receive L6 engineering plus L6 finance approval.

Whether optional shadow-routing on FAQ-only requests is worth adding remains open. If adopted later, it must be scoped as instrumentation and must not quietly change user-facing route behavior.

### Closing Position

The online query path makes Skywalker's two-arm retrieval architecture operational. One scoped request enters. The runtime embeds the query once. A fixed two-stage routing gate uses cosine first and Cohere Rerank v4 only on ambiguity. Strong FAQ-shaped requests take the FAQ-only path; weak or ambiguous requests take the dual-arm path. The controlled FAQ arm always uses hybrid BM25 plus FAISS vector retrieval against the live physical AOSS index selected by the SSM pointer. UKB runs concurrently when the route widens. The runtime normalizes all candidates into one provenance-rich envelope and hands that unified package to the common reranker.

That boundary keeps route choice, retrieval concurrency, convergence, and degradation behavior in this section while leaving identity resolution, FAQ ingestion, UKB internals, common scoring, downstream conversation handling, and final answer rendering in their own layers.

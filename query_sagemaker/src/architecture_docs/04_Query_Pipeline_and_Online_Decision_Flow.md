## Section 04. Query Pipeline and Online Decision Flow

Section 01 fixed the system boundary. Section 02 fixed the entry contract and the scoping path. Section 03 fixed the controlled Top 50 FAQ corpus as a real owned subsystem rather than as a sidecar convenience. This section now defines the online runtime path that sits on top of those decisions.

That runtime path is where the architecture stops being descriptive and starts being operational. One scoped request arrives. The system decides whether the request is close enough to the Top 50 FAQ question set to deserve an FAQ-first route. It decides whether one arm or both arms should be invoked. It keeps the two retrieval arms from becoming two incompatible mini-systems. It preserves enough provenance that later reranking and downstream agents can still understand where each candidate came from. And it does all of that without drifting into the work that belongs elsewhere, namely UKB's own internal retrieval behavior, the reranker's internal scoring logic, or conversational state management in Slack and QuickSuite.

The point of this section is therefore not merely to describe "what happens when a query comes in." The point is to define the orchestration layer that makes the two-arm design legible under real runtime conditions. A controlled FAQ arm and a black-box UKB arm are only a coherent architecture if there is a disciplined online path above them that knows when to prefer one, when to invoke both, how to converge their outputs, and how to fail without quietly inventing confidence the system did not earn.

### 1. What this section owns

This section owns the request-time orchestration layer that begins after the canonical scoped request has already been constructed and ends when the system has produced one normalized candidate set ready for the common reranking surface. In practical terms, that means it owns the routing gate that decides between FAQ-only and dual-arm retrieval, the branch policy behind that decision, the concurrency model for arm invocation, the normalization of arm-native retrieval outputs into one common candidate envelope, the runtime route metadata that records what happened on this request, and the handoff contract into the reranking stage.

It also owns the statelessness of the retrieval backend at runtime. The online path is allowed to know the current request, the current scoped identity, and the currently published retrieval surfaces. It is not allowed to accumulate conversational memory, invent per-session retrieval context, or mutate retrieval behavior based on unbounded prior turns. That boundary is not cosmetic. It preserves the discipline already established earlier in the series that Skywalker is the retrieval backend behind MCP, not the conversational layer that sits above it.

This section does not own the MCP entry contract itself. Section 02 already fixed how a request is admitted and scoped. It does not own the controlled FAQ evidence corpus. Section 03 already fixed that. It does not own the static variant set used by the routing gate; that artifact lives in S3 and is documented in [API_11](done/API_11_S3_Variant_Set.md). It does not own the internal behavior of UKB. The UKB arm remains a black box behind its own MCP surface. It does not own the internals of the reranker, the final answerability threshold, or downstream response rendering inside Slack or QuickSuite. Those concerns will be treated later and deserve their own boundaries.

The runtime orchestration layer is therefore narrower than "the whole query path" and more important than that phrasing usually implies. It is the place where Skywalker turns one valid scoped request into one justified runtime route, one or two retrieval calls, and one common candidate pool whose later scoring can be reasoned about. If that orchestration is muddy, the two-arm design becomes difficult to evaluate because every later success or failure gets blamed on the wrong layer.

### 2. Inputs, outputs, and contracts

The primary input to this section is the canonical scoped request emitted by Section 02. By the time that request reaches this runtime layer, the system already knows the original query text, the stable caller identity used for traceability, the resolved country, the resolved level, the resolved role dimension that currently distinguishes manager from individual contributor, and the route metadata that explains how that scope was obtained. This section is not allowed to revisit whether that scoping data was necessary. It inherits that decision. It is allowed only to use it correctly.

The second input is the static variant set loaded into memory at service boot from S3. That artifact is a small list of canonical Top 50 FAQ question phrasings with pre-computed Cohere Embed v4 embeddings. The routing gate uses this set purely for similarity comparison against the live query. There is no hierarchy, no family grouping, no mapping from the variant back to any specific FAQ document. A strong similarity against a variant is simply evidence that the query is shaped like a Top 50 question; it does not claim the query is "about" any particular one.

The artifact lives at `s3://skywalker-config-{stage}/variants/top_50.json` — one object per stage (`beta`, `gamma`, `prod`), same file shape across stages, with stage-specific values during staging rollouts. The file is a JSON object with a small header and a `variants[]` array. The header fields are `schema_version` (integer, currently `1`; the service refuses to load files whose `schema_version` it does not know about), `embedding_model` (the exact Bedrock model ID used to generate the embeddings — `cohere.embed-v4:0` at launch, which must match the model the query-time embedding call uses or cosine similarities are meaningless), `embedding_dimension` (`1024`, which must match the evidence index's `knn_vector` dimension), `embedding_input_type` (`"search_document"` — the variants are being matched against, so they are embedded as documents, while the live query is embedded with `"search_query"` per Cohere Embed v4's asymmetric conventions), and `generated_at` (an ISO-8601 UTC timestamp, informational only, not used for staleness checks). Each entry in `variants[]` carries `id` (a stable identifier used in logs and metrics when the gate wants to record which variant matched), `text` (the canonical phrasing), and `embedding` (the 1024-float pre-computed vector).

On service boot, the runtime reads the artifact via `s3:GetObject`, validates `schema_version` and `embedding_dimension` against expectations, holds the `variants[]` array in memory as a list of `(id, text, embedding)` tuples, and exposes two metrics: `skywalker.variants.count` reporting the number of variants loaded and `skywalker.variants.schema_version` reporting the loaded schema version. **If the file is missing, malformed, or fails validation, the service refuses to start.** There is no fallback because a service without a usable variant set cannot make the routing decision correctly, and silently starting with an empty variant set would route every request to dual-arm — a hidden quality regression rather than a visible outage.

The variant set is a manually maintained control-plane artifact, not a pipeline output. When the team decides to change variants, a small one-off job or CLI reads the new list, calls Cohere Embed v4 with `input_type: "search_document"` on each variant text, produces a new `top_50.json`, and uploads it to S3. S3 versioning is enabled so prior versions are recoverable. Running service instances pick up the new file on their next boot; a rolling restart across the fleet propagates the change. There is no hot reload — a deliberate simplicity choice given how rarely variants should change. The service execution role carries `s3:GetObject` and `s3:GetObjectVersion` scoped to `arn:aws:s3:::skywalker-config-{stage}/variants/*` and nothing else; write access is held only by the manual update path's separate role.

The third input is the published controlled FAQ evidence surface, created by Section 03. That surface accepts a scoped retrieval request and returns FAQ-arm candidates as child chunks in the common runtime schema defined by API_06.

The fourth input is the UKB general arm. Unlike the FAQ surface, this is not an owned storage layer. It is an external MCP-backed retrieval surface that accepts the runtime query and returns UKB-side candidates in its own shape. The orchestration layer cannot assume that UKB returns the same fields, the same score semantics, or the same explanation detail as the owned FAQ arm. That is one of the reasons the common candidate envelope exists at all.

The output of this section is not the final answer. It is a runtime retrieval result package. That package has four parts.

The first part is the route record. This states whether the request took the FAQ-only path or the dual-arm path, whether any arm timed out or failed, and what single-arm fallback behaviors were applied if the preferred route could not be completed as designed.

The second part is the normalized candidate pool. Every candidate in that pool must be convertible into one common schema regardless of whether it originated in the controlled FAQ arm or in UKB. The reranker cannot operate coherently if one candidate arrives as a rich section-level retrieval object with explicit source metadata and another arrives as a loosely shaped UKB snippet whose provenance is only implicit in free text.

The third part is the per-candidate provenance record. This preserves where the candidate came from, which arm produced it, what its arm-local identity was, what the arm-local ranking metadata looked like, and what publication version or request trace should later be visible when the system is reviewed by a human. The important point is not that downstream stages will compare raw arm-local scores directly. They should not. The important point is that later stages and later reviewers still need to know what the system actually did.

The fourth part is the reranker handoff contract. This is the point where the orchestration layer stops deciding which retrieval surfaces to invoke and starts giving the common scoring surface a coherent set of things to judge. Section 07 will own the reranker's behavior in detail. This section owns the fact that the reranker receives one ordered package of normalized candidates plus route metadata rather than two unrelated stacks of arm-specific output.

A practical common candidate contract needs, at minimum, a stable runtime candidate identifier, source arm, original source identifier, text payload suitable for later scoring, source title where available, source URL where available, arm-local rank or ordering information, any policy-link or source-link metadata already attached to the candidate, and a scope snapshot that makes the candidate legible under audit. The contract does not require the two arms to expose identical upstream internals. It does require the orchestration layer to normalize the pieces that the later pipeline actually needs.

A second contract point matters because this orchestration layer is sitting between systems that were not built together. The arm-specific contracts are allowed to be different at the edges, but they are not allowed to stay different past convergence. The FAQ arm may naturally return chunk-oriented evidence records carrying the source metadata that Skywalker itself indexed. UKB may naturally return a result object that is closer to a retrieved passage with service-level metadata attached. The common candidate envelope is the place where the runtime makes those surfaces comparable in structure even though they are not comparable in native score. If that normalization is underspecified, later reranking and later human review both become guesswork.

The contract with later stages also needs to preserve absence honestly. A candidate field that does not exist for one arm should remain absent or explicitly null in the normalized object rather than being replaced with fabricated structure for the sake of symmetry. The runtime's job is to normalize what is real, not to beautify away asymmetry. That matters because one of the main reasons to preserve provenance is to let the system improve over time. A normalized object that hides which fields were truly present and which were synthesized for convenience will make later diagnosis materially harder.

### 3. Fixed decisions

The first fixed decision is sequencing. The routing gate runs before arm selection. The online path does not first call both arms and then retroactively decide whether the query "really was" FAQ-shaped. The gate is a routing mechanism, not a reporting mechanism.

The second fixed decision is the **hybrid gate architecture**. The gate has two stages.

- **Stage 1 — cosine similarity** between the query embedding and each variant embedding in the in-memory set (API_11). The top similarity score is the gate's first signal.
- **Stage 2 — Cohere Rerank v4 cross-encoder on SageMaker** (API_10 Part B). Invoked only when the top cosine score lands in an ambiguity band — neither confidently high nor confidently low. On clear cosine signals, stage 2 is skipped entirely. This keeps average latency low while buying cross-encoder accuracy on exactly the cases where bi-encoder cosine is least trustworthy (paraphrase, negation, entity substitution, compound queries).

All three threshold values that govern this gate — the cosine-low threshold, the cosine-high threshold, and the Rerank v4 floor — are **control-plane values held in SSM Parameter Store** (API_07). They are explicitly designed to be tuned without a redeploy. Launch defaults:

- `/skywalker/runtime/gate/cosine_low_threshold` — `0.30`
- `/skywalker/runtime/gate/cosine_high_threshold` — `0.80`
- `/skywalker/runtime/gate/rerank_floor` — `0.50`

Routing rules:

- `top_cosine > cosine_high_threshold` → FAQ-only. Stage 2 skipped.
- `top_cosine < cosine_low_threshold` → dual-arm. Stage 2 skipped.
- Otherwise → invoke Rerank 3.5 against the variant set. If top `relevance_score > rerank_floor` → FAQ-only; else → dual-arm.

**These three thresholds are the most calibration-active values in the system.** Any documentation or code referencing them must make their tunability and SSM-backed control-plane nature clear; they are not hard-coded constants and treating them as such is a bug.

The third fixed decision is concurrency. When the runtime takes the dual-arm path, it invokes the FAQ evidence arm and the UKB arm in parallel. The architecture does not treat those calls as a serialized chain. That matters because the dual-arm path is part of the intended design, not a slow fallback that the rest of the runtime should be embarrassed about.

The fourth fixed decision is that reranking is the common scoring surface. The orchestration layer preserves arm provenance and arm-local ranking metadata for traceability, but it does not try to decide between FAQ-arm and UKB-arm results by comparing their native scores directly. Native scores are not assumed to be commensurate across arms. The system resolves that by converging candidates into one common reranking surface rather than by pretending heterogeneous retrieval scores mean the same thing.

The fifth fixed decision is that PAPI does not reappear inside this layer. Scope is already resolved upstream. The runtime uses that scope to shape retrieval. It does not re-open identity resolution in the middle of the online path and it does not allow retrieval to become weakly scoped because one arm would prefer a looser request.

The sixth fixed decision is that the online path remains stateless. The retrieval backend does not attempt to preserve or interpret multi-turn state. It receives one request, routes one request, retrieves evidence for one request, and hands the resulting evidence package to later stages. Slack and QuickSuite may be intelligent enough to manage prior turns, but the retrieval backend does not simulate that intelligence by quietly building request history into retrieval logic.

The seventh fixed decision is that the routing variant set and the FAQ evidence corpus are independent artifacts with different storage and different change cadences. Variants live in S3 and are manually maintained (API_11). Evidence lives in AOSS and is rebuilt daily from CoreX (Section 03). They are not coupled at the publication level and do not need to be.

The eighth fixed decision is that the runtime preserves route metadata as a first-class artifact. Later sections may turn that metadata into trace output, quality instrumentation, or user-facing diagnostics. What matters here is that the orchestration layer records whether the request was FAQ-only, dual-arm, single-arm fallback, or fully failed, and whether the gate reached stage 2 or exited at stage 1, because otherwise the system will not be able to explain itself under review.

The ninth fixed decision narrows the implementation posture. The query is embedded once per request using Cohere Embed v4 with `input_type: "search_query"`. That single vector is reused for both the cosine step of the routing gate (against the in-memory variant embeddings) and for the FAQ-evidence retrieval hybrid query's `knn` leg (against AOSS). UKB does not share this embedding — it is a black box that runs its own internal embedding.

The tenth fixed decision is that the gate performs no query rewriting. The query text handed to retrieval is unchanged. The routing gate decides where to send it, not what it says. If later work ever introduces a stage that does rewrite the query text before retrieval, that should become an explicit architectural decision rather than a quiet embellishment inside the orchestration layer.

The eleventh fixed decision is that the FAQ arm uses **hybrid retrieval** — BM25 lexical scoring on `text` and `title` combined with FAISS cosine vector similarity on `embedding`, fused by an AOSS search pipeline (`min_max` normalization, `arithmetic_mean` combination). Hybrid is architecture-class, not a calibration surface — the architecture commits to running both legs on every FAQ-evidence call. What is calibratable is the per-leg weight (launch `[0.3, 0.7]`, SSM-tunable). The reasoning: vector-only retrieval is fragile on identifier-shaped tokens (policy codes, vendor names, currency abbreviations) that BM25 catches reliably, while BM25-only retrieval misses paraphrase and conceptual matches that vector similarity catches reliably. Top 50 FAQ traffic includes both shapes, so retrieving with both legs is the design.

### 4. Alternatives considered or still live

The first alternative was a single-stage gate using cosine similarity only. That design is simpler but meaningfully less accurate on paraphrase and negation — exactly the queries where a Top 50 FAQ question is most often disguised. The hybrid gate pays a small contingent latency cost on the ambiguity band to recover that accuracy. Rejected in favor of the hybrid.

The second alternative was always running the Rerank v4 cross-encoder, not just on ambiguous cosine scores. That adds 100–200 ms to every query, including the ~80% that cosine already classifies confidently. Rejected because the return on latency spend is smallest on the confident end of the cosine distribution.

The third alternative was a single-arm gate: strong FAQ-like → FAQ-only, non-FAQ-like → UKB-only. Rejected because it throws away the controlled FAQ corpus exactly on the near-miss cases where it most helps to have it competing in the reranker.

The fourth alternative was always querying both arms and treating the routing gate as diagnostic only. That remains a live alternative — it would simplify routing and remove one branch from the online path. It is not the current decision. The current design preserves an FAQ-only short path for requests that are strongly aligned with the Top 50 question shape. Re-litigation would be justified only if runtime evidence later shows that the short path buys little beyond implementation complexity.

The fifth alternative was deciding between arms by native retrieval score rather than by a common reranking surface. Rejected because it places too much meaning on scores that originate in fundamentally different systems.

The sixth alternative was letting Skywalker itself become responsible for conversational carry-forward, follow-up interpretation, or session-aware retrieval. Rejected because it collapses the backend boundary fixed in Section 01.

One more alternative remains live but not adopted. If UKB eventually proves both stable and high quality on the exact query shape that the controlled arm exists to own, the rationale for FAQ-only routing weakens and the architecture could shift toward wider dual-arm behavior by default. That is not the current posture, but it is the correct condition under which this part of the design would deserve another look.

### 5. Assumptions inherited from upstream sections

This section inherits the system boundary from Section 01 (Skywalker is a retrieval backend behind MCP whose output feeds downstream agents, not the final conversational policy engine) and the entry contract from Section 02 (requests reaching this layer are already valid, already carry the scoping triple, and the runtime is not allowed to degrade into best-effort retrieval with weak or missing scope). It also inherits the controlled FAQ subsystem from Section 03: the evidence surface exists as a published retrieval-backed asset, and this section is not allowed to reinterpret the FAQ arm as a static lookup just because the corpus is small.

Two further premises apply. The asymmetry that justifies the architecture — UKB as broad black-box coverage, the controlled FAQ arm as the owned path where the highest-value question set must not be delegated — is preserved even after both arms converge on the reranker. And the scoping triple (country, level, and manager-versus-IC role) is sufficient for runtime routing and retrieval today; this section does not invent additional scoping dimensions.

### 6. End-to-end data flow for this section

The online flow begins when the orchestration layer receives the canonical scoped request. At that moment the runtime packages the query text, the scoped identity fields, and the route metadata from the entry contract into one request context that every downstream step can reference without reinterpreting the boundary contract. This request context becomes the working runtime object for the rest of the online path.

The first substantive action is **query embedding**. The runtime calls Cohere Embed v4 once with `input_type: "search_query"` against the query text. The resulting 1024-dimensional vector is held in the request context and reused for both the gate's cosine step and the FAQ-evidence retrieval.

The second action is the **routing gate, stage 1 — cosine against the in-memory variant set**. The runtime computes cosine similarity between the query vector and each of the pre-embedded variants (50 vectors, loaded at service boot from S3 per API_11). It takes the top similarity score and records which variant produced it for later logging.

The third action is **the gate decision**, driven by the control-plane thresholds from SSM:

- If `top_cosine > cosine_high_threshold` (launch default 0.80): the gate decides FAQ-only immediately and skips stage 2.
- If `top_cosine < cosine_low_threshold` (launch default 0.30): the gate decides dual-arm immediately and skips stage 2.
- Otherwise: the gate invokes **stage 2** — a Cohere Rerank v4 cross-encoder call on SageMaker against the variant set, returning the top `relevance_score`. If that score exceeds the `rerank_floor` (launch default 0.50), the gate decides FAQ-only; otherwise dual-arm. Every outcome of stage 2 is logged with the score, the top variant, and the resulting route.

The stage-2 call uses a **separate** SageMaker real-time endpoint serving Cohere Rerank v4 (`rerank-v4.0`) named `skywalker-gate-rerank-v4-{stage}`, distinct from the evidence reranker endpoints in Section 07. The gate endpoint is sized for the gate's traffic shape — Stage 2 fires on roughly 20% of requests, with `top_n: 1` over 50 short variant texts — which is a fundamentally lighter payload than the evidence reranker's 20 candidates × ~1000 tokens. Launch posture is one always-on `ml.g5.2xlarge` (A10G) per stage rather than the H100 the evidence reranker requires, because at this payload size A10G hits well inside the gate timeout. Production runs two endpoints across availability zones for HA; beta runs one. Authentication is SigV4 against the SageMaker Runtime service using the query service's IAM execution role, scoped to `sagemaker:InvokeEndpoint` on the gate endpoint ARNs only. The AWS SDK for Java v2 artifact is `software.amazon.awssdk:sagemakerruntime`, and the request body is the standard Cohere Rerank JSON payload: `model: "rerank-v4.0"`, `query` set to the user query text, `documents` set to the array of 50 variant texts in stable order, `top_n: 1` (we only need the top score), `max_tokens_per_doc: 4096`, `api_version: 2`. The response carries `results[]` with `index` (position in the input documents array, used to recover which variant won) and `relevance_score` in `[0, 1]`. The call is bounded by the SSM-controlled `/skywalker/runtime/gate/rerank_timeout_ms` (launch default 200 ms, tighter than the evidence reranker's 350 ms because the payload is materially smaller). Two distinct endpoints — gate and evidence — give independent failure domains and independent rolling updates; co-locating them on one endpoint is rejected because gate traffic is bursty and high-frequency while evidence reranking is bounded by the 4-second p95 Slack budget.

Why a separate endpoint at all rather than reusing the evidence reranker's H100 fleet for gate traffic too: the gate runs 5x as often (every dual-arm-eligible request hits cosine, ~20% reach Stage 2) and its payload is small enough that paying the H100 instance-hour cost for it is wasteful. The evidence reranker fleet stays right-sized for evidence traffic; the gate fleet stays right-sized for gate traffic; failures on either do not cascade into the other.

If stage 2 times out or transport-fails, the gate falls through to dual-arm and records the failure as a route-widening event (not an error). A gate failure never stops the request.

The fourth action is **arm invocation**. On the FAQ-only path, the runtime invokes the controlled FAQ evidence surface. On the dual-arm path, it invokes the FAQ evidence surface and the UKB surface concurrently. The route decision changes which arms are invoked, not how scope works. Scope remains part of retrieval correctness on every path.

The FAQ evidence invocation is a **hybrid query** posted to AOSS against the `faq_evidence_current` alias, with the `skywalker-faq-hybrid` search pipeline applied at the `search_pipeline` query parameter. The query body uses the `hybrid` query type that combines two leg clauses scored independently and fused by the pipeline's `normalization-processor`:

- A `match` clause on `text` (and on `title` with a smaller boost) — this is the BM25 lexical leg. It scores chunks whose tokenized content contains the query terms regardless of semantic similarity, which is what catches policy codes, vendor names, currency abbreviations, and other identifier-shaped tokens that vector similarity famously misses.
- A `knn` clause on `embedding` carrying the query embedding (1024 floats), `k: 40`, and a FAISS `efficient_filter` clause that pre-filters on `country`, `level`, and `role` so only in-scope children compete in the vector leg.

The pipeline normalizes both leg scores via `min_max` to `[0, 1]` and combines them via `arithmetic_mean` with weights set on the query (`[bm25_weight, vector_weight]`, launch defaults `[0.3, 0.7]`, SSM-tunable via `/skywalker/runtime/retrieval/hybrid_bm25_weight`). Higher `bm25_weight` favors keyword matches; higher `vector_weight` favors semantic matches. `0.3 / 0.7` at launch reflects that the corpus is small and conceptually scoped, so semantic recall does most of the work, but BM25 catches what semantic similarity blurs. Launch budget: 20 candidates returned on the FAQ-only route, ~10 candidates on dual-arm (leaving ~10 for UKB). Over-retrieval factor `k: 40` accommodates the scope-filter's effect on population size on the vector leg.

A vector-only fallback is supported but not on the active path: if the search pipeline is unavailable for any reason, the runtime can issue a plain `knn`-only query to the same alias as a degraded mode. The unified hybrid path is the default; degradation is recorded in route metadata as `RETRIEVAL_VECTOR_ONLY_FALLBACK`.

The UKB invocation is an MCP `tools/call` to `retrieve` with `maxResults = UKB_CANDIDATE_BUDGET` and `targetUser` populated from the resolved identity (API_09). Both calls run with their own timeouts; UKB's is bounded at launch by 300 ms to fit inside the p95 budget, and an exceeded budget counts as arm failure for the single-arm fallback rule.

The fifth action is arm-native candidate reception. At this point the runtime has either one candidate set from the controlled FAQ arm or two candidate sets, one from the controlled FAQ arm and one from UKB. The sets are not assumed to be shape-compatible.

The sixth action is candidate normalization. Every arm-native candidate is transformed into the common candidate envelope described earlier in this section. The orchestration layer preserves the source arm, the arm-local rank, the arm-local identifier, and any useful upstream metadata, but it also normalizes the fields the reranker and the later response path will actually need: candidate text, source title, source URL, policy-link or source-link metadata when available, and a stable runtime candidate identity.

The seventh action is candidate shaping for the reranker handoff. This section does not own the reranker's internal behavior, but it does own the fact that the reranker receives one unified pool rather than two stacks. If route metadata indicates FAQ-only retrieval, the handoff contains 20 FAQ-arm child chunks. If route metadata indicates dual-arm retrieval, the handoff contains roughly 10 FAQ-arm children plus 10 UKB-arm passages, converging into one 20-candidate pool for reranking. If the route is a single-arm fallback, the surviving arm supplies 20 candidates. The later reranking section will define scoring and answerability in detail. This section defines the fact of convergence.

Post-rerank sibling expansion is the subject of Section 07's data flow, not this section's. The orchestration layer hands a 20-candidate pool to Section 07 and receives back either an answerable evidence package (with parents already reconstructed from child siblings via the sibling query described in API_06) or a structured abstain package. This section does not see the parent reconstruction; it sees only the inputs and the final handoff.

One more runtime discipline belongs here because it affects later evaluation directly. The common pool should preserve enough structure to distinguish complete convergence from thin convergence. A dual-arm request that returns eight FAQ candidates and one UKB candidate is not the same runtime condition as a dual-arm request that returns four strong candidates from each arm, even if both routes technically succeeded. The orchestration layer therefore has to preserve arm-level counts and route completeness alongside the normalized pool rather than collapsing every successful retrieval event into the same generic state.

The eighth action is route-record finalization. Before the request leaves this layer, the runtime records the gate decision (cosine-only confident / stage-2 invoked / stage-2 widened / stage-2 failed), whether the request was FAQ-only or dual-arm, whether one arm had to be dropped because of timeout or failure, and whether the common candidate pool is complete or a single-arm fallback.

The result of the section's data flow is therefore one routed retrieval package ready for common reranking.

### 7. Failure behavior, abstain behavior, and non-goals that matter here

The first failure posture is gate failure tolerance. If the routing gate's stage 2 call (Cohere Rerank v4 on SageMaker) fails or times out, the runtime does not turn that into a hard no-answer event. The gate falls through to dual-arm and records the widening. The gate is a routing aid, not a prerequisite for retrieval. Cosine stage 1 failure (the in-memory comparison) is essentially impossible in practice — it is a pure in-process vector math operation — but would also widen to dual-arm if it ever occurred.

The second failure posture is FAQ-arm rescue behavior. If a request was routed FAQ-only because the gate was confident but the FAQ evidence retrieval call fails or returns nothing usable, the runtime widens to the UKB arm rather than returning no evidence purely because the preferred controlled path was unavailable.

The third failure posture is one-arm survival on the dual-arm path. If the runtime has already chosen dual-arm retrieval and one arm fails while the other succeeds, the online path continues with the surviving arm and records the request as a single-arm fallback.

The fourth failure posture is total retrieval failure. If both arms fail, or if the surviving arm returns no usable candidates, the online path cannot create a meaningful candidate pool and returns control to the later abstain or failure path rather than pretending that an empty pool is a thinly grounded answer opportunity.

The distinction between arm failure and empty evidence is important. A call can succeed technically and still return no usable candidates. That should be reported as a retrieval miss on that arm, not as an outage. The runtime then decides whether the other arm produced enough evidence to continue, and later stages decide whether the surviving pool is strong enough to answer.

Abstain behavior begins to matter here even though this section does not own the final abstain threshold. The orchestration layer must preserve abstention as a first-class possible outcome for the later pipeline. A coherent runtime route can still produce a candidate pool too weak to justify an answer, and this section makes that possible by preserving route quality metadata rather than flattening every request into a false success state.

The non-goals for this section are just as important as the failure rules. This section does not own PAPI retries or identity repair. It does not own UKB's internal search algorithm, FAQ corpus publication, the reranker's scoring semantics, or agent-side prompt construction. It does not attempt to salvage missing scope by broadening filters. It does not keep session memory. It does not decide what wording the final answer should use. And it does not perform hidden query rewriting.

### 8. Calibration surfaces and what would cause re-litigation

**All three routing-gate thresholds are control-plane values tuned via SSM Parameter Store without redeploy.** This is the single most important point about calibration in this section.

The first calibration surface is the **cosine-high threshold** (`/skywalker/runtime/gate/cosine_high_threshold`, launch default 0.80). Lowering it routes more requests directly to FAQ-only based on cosine alone, trading off cross-encoder second-opinion for latency savings. Raising it routes more requests into stage 2 for a second look, trading off latency for accuracy on high-cosine-but-still-ambiguous cases.

The second calibration surface is the **cosine-low threshold** (`/skywalker/runtime/gate/cosine_low_threshold`, launch default 0.30). Raising it routes more requests directly to dual-arm based on cosine alone. Lowering it routes more low-cosine requests into stage 2 to catch false negatives from cosine.

The third calibration surface is the **Rerank v4 floor** (`/skywalker/runtime/gate/rerank_floor`, launch default 0.50). Raising it makes stage 2 stricter (more dual-arm). Lowering it makes stage 2 more permissive (more FAQ-only).

The fourth calibration surface is the **gate-reranker timeout** (`/skywalker/runtime/gate/rerank_timeout_ms`, launch default 200 ms). If Cohere Rerank v4 latency on the gate's `ml.g5.2xlarge` endpoint in production consistently runs higher than expected, this timeout grows; if it runs well below, the timeout tightens to catch tail latency sooner.

The fifth calibration surface is the branch policy itself. Re-litigation would be justified if empirical evidence shows that always querying both arms materially improves quality without meaningfully complicating runtime behavior, or conversely that the FAQ-only path is clearly preserving quality and reducing noise exactly as intended.

The sixth calibration surface is per-arm candidate budget. The orchestration layer decides how many FAQ-arm candidates and how many UKB candidates are allowed into the common pool before reranking. Held in SSM at `/skywalker/runtime/retrieval/per_arm_candidate_budget` (launch default 10). Re-litigation is justified if one arm consistently floods the handoff and starves the other.

The seventh calibration surface is UKB timeout posture. Held in SSM at `/skywalker/runtime/retrieval/ukb_timeout_ms` (launch default 300 ms). Re-litigation is justified if UKB latency regularly drags dual-arm performance past acceptable bounds.

The eighth calibration surface is the common candidate envelope itself. Re-litigation would be justified if later evaluation shows that important arm-specific distinctions are getting lost in normalization, or that the reranker benefits from a richer representation than the current common contract provides.

The ninth calibration surface is the **hybrid retrieval weight** (`/skywalker/runtime/retrieval/hybrid_bm25_weight`, launch default 0.30 — implying a vector weight of 0.70). Raising it favors lexical matches more strongly; lowering it favors semantic matches more strongly. Re-litigation is justified if judged-traffic evidence shows the system systematically misranking on identifier-heavy or paraphrase-heavy queries that the current weight blend handles poorly. Tuning is in-place via SSM and does not require an index rebuild because both legs are scored at query time.

The ninth calibration surface is route-state instrumentation itself. The architecture keeps detailed route and degradation metadata because the system will need to know whether weak answers are coming from bad routing, thin evidence, one-arm outages, or later reranking behavior. The default assumption is that a two-arm runtime needs honest route-state visibility.

### 9. Open questions, if any

The UKB invocation contract is pinned in API_09 (tool name `retrieve`, required headers, arguments `query` / `maxResults` / `targetUser`, response is `content[]` of `type: "resource"`). What remains open is whether Skywalker ever populates `additionalFilters` for UKB (launch posture: no) and the final `client_id` issued during onboarding.

The common candidate schema is pinned in API_01 and API_06. What remains open is whether later evaluation shows that specific arm-level fields need to be promoted from provenance metadata into the reranker's scored text surface (Section 07 §8 calibration surface seven).

The four gate-control thresholds (two cosine, one rerank floor, one rerank timeout) are calibration-active with launch defaults 0.30 / 0.80 / 0.50 / 200 ms. The real values come from measured traffic once the system is live. These are explicitly tunable without redeploy.

The arm-level candidate budget and the UKB timeout are also calibration-active through SSM (launch defaults 10 per arm and 300 ms respectively).

Whether the system should preserve any optional shadow-routing instrumentation on FAQ-only requests for evaluation purposes remains a future quality-measurement aid rather than a fixed part of the current path.

### Closing position

The online query path is where Skywalker's two-arm architecture either becomes a coherent runtime system or remains two adjacent ideas. The design defined here makes it coherent. One scoped request enters. The runtime embeds the query once. The hybrid routing gate — cosine-first, cross-encoder only on ambiguity — decides between FAQ-only and dual-arm retrieval using SSM-controlled thresholds operators can tune without a redeploy. A strong cosine signal routes to FAQ-only directly. A weak cosine signal routes to dual-arm directly. Everything in between gets a second opinion from Cohere Rerank v4 on a dedicated SageMaker endpoint against the variant set. The controlled FAQ arm uses hybrid (BM25 + FAISS cosine) retrieval against AOSS so identifier-shaped and paraphrase-shaped queries both land cleanly. The black-box UKB arm runs concurrently when the route widens. Both arms are then normalized into one common candidate surface with route provenance preserved, and that unified runtime package moves forward to the reranking stage.

That is the correct boundary for this section. It owns orchestration, route choice, concurrency, convergence, and degradation behavior. It does not claim the internals of UKB, the internals of reranking, or the downstream conversational layer. It keeps the runtime path sharp enough that later failures can be diagnosed honestly instead of being hidden inside one vague phrase like "the system searched and answered."

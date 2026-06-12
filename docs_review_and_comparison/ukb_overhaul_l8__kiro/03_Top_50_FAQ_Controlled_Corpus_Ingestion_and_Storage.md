## Section 03. Top 50 FAQ Controlled Corpus, Ingestion, and Storage

## 1. Tenets

This ingestion path exists to make the Top 50 FAQ evidence corpus dependable enough for the online decision flow to trust it. The design therefore prefers conservative publication over freshness when those goals conflict.

1. We prefer controlled accuracy over broad coverage when they conflict.
2. We prefer a complete previous corpus over a partial fresh corpus when a rebuild cannot prove completeness.
3. We prefer simple full-corpus rebuilds over incremental mutation while the owned corpus remains small enough to rebuild within the Poller/Processor envelope.
4. We prefer implementation-backed contracts over architecture prose when the two disagree.
5. We prefer calibration at retrieval parameters over relitigating the launch architecture after traffic begins.

These tenets are intentionally constraining. They explain why the ingestion system can decline to publish a newer source state, why a single skipped node blocks promotion, and why hybrid retrieval is treated as a launch architecture decision rather than a tuning experiment.

## 2. Problem and Intent

The Top 50 FAQ arm needs an evidence corpus that is owned, reproducible, scoped, and publish-safe. The online query path can only make honest answer-vs-abstain decisions if the stored evidence has known provenance, known scope, stable embedding shape, and a live-read surface that never points at a half-built index.

This section defines the controlled ingestion and storage architecture for that corpus. It covers how FAQ evidence is enumerated from COREx, transformed into OpenSearch Serverless documents, embedded with Bedrock Cohere Embed v4, written into a rebuild target, and promoted to live only after the rebuild proves complete. It also records which choices are fixed for launch and which surfaces remain empirical calibration.

This is not a general article ingestion platform. It is not a universal COREx mirror. It is not a parent/child chunking system. The launch implementation maps one eligible COREx node to one `FragmentDocument` and one OpenSearch document. There is no linked-parent launch behavior, no sibling reassembly contract, and no query-time dependency on parent or child document joins.

## 3. Boundary and Non-Goals

This system begins when the Poller Lambda enumerates COREx content nodes for the configured domain owner and ends when a verified rebuild target is promoted through the SSM live-index pointer. The live query path consumes the active physical OpenSearch Serverless index named by `/skywalker/ingestion/faq_evidence/live_index`; it does not query a symbolic AOSS alias.

The boundary includes COREx enumeration, full node fetch, text extraction, scope validation, source metadata preservation, document embedding, document write, read-back validation, live pointer promotion, and high-water marker advancement.

The boundary excludes answer generation, reranking, abstain policy, Slack or QuickSuite client behavior, COREx authoring workflows, and any non-FAQ UKB retrieval corpus. Those adjacent systems rely on this section for a complete, scope-filterable FAQ evidence store, but they own their own online decision behavior.

Launch non-goals are explicit:

- No incremental per-node mutation in the live index.
- No AOSS alias-based publication model.
- No parent/child document layout.
- No linked-parent or sibling-reassembly launch behavior.
- No synthetic scope backfill when COREx scope is missing.
- No promotion of partial, skipped, or unverifiable rebuilds.
- No replacement of hybrid retrieval with sparse-only or vector-only retrieval at launch.

## 4. Source-of-Truth Hierarchy

The source-of-truth hierarchy for this section is:

1. `IngestionCodeReference`.
2. Implemented code paths.
3. Architecture documents, including adopted API and integration contracts.
4. Design discussions.
5. Future proposals.

If prose conflicts with the implemented behavior, engineers should execute against the implemented behavior and update the prose. The concrete source-backed facts used here come from `RebuildCoordinator`, `OpenSearchIndexManager`, `OpenSearchFragmentWriter`, `FragmentProcessor`, `BedrockEmbeddingClient`, `CoreXSearchEnumerator`, `CoreXContentFetcher`, `SsmLiveIndexStore`, `SsmSnapshotMarkerStore`, `Poller`, `Processor`, and the CDK service/OpenSearch stacks.

## 5. Facts, Assumptions, and Consequences

The launch design depends on a small set of implementation facts:

| Fact | Source-backed behavior | Consequence |
| --- | --- | --- |
| Publication uses two physical AOSS indices. | `OpenSearchIndexManager` owns `<base>_a` and `<base>_b`; with `baseName=faq_evidence`, the physical indices are `faq_evidence_a` and `faq_evidence_b`. | Rebuilds happen against the idle physical index. The live index is not mutated during the rebuild. |
| Live selection is an SSM pointer. | `SsmLiveIndexStore` reads and writes `/skywalker/ingestion/faq_evidence/live_index`. | Query services must resolve the live index by reading SSM, not by assuming an AOSS alias or static index name. |
| Freshness detection is one high-water marker. | `SsmSnapshotMarkerStore` stores `/skywalker/ingestion/faq_evidence/last_snapshot_marker`; `CoreXSearchEnumerator` computes max `lastModifiedDate`. | Any source change that advances the max marker triggers a full rebuild. There is no per-node checkpoint table. |
| Rebuilds are full rebuilds. | `beginRebuild()` drops and recreates the idle index empty before work dispatch. | Removed source content disappears only when the next complete rebuild promotes. |
| Promotion is all-or-nothing. | `RebuildCoordinator` refuses promotion when any node is skipped, when indexed count is below expected, or when read-back count does not reach expected. | A single skipped node preserves the previous live corpus instead of publishing a partial new one. |
| One source node becomes one document. | `FragmentProcessor` writes one `FragmentDocument` for each eligible node; tests assert one document per node. | The index has no chunk hierarchy, no parent ID contract, and no linked-parent runtime behavior at launch. |
| Embeddings are Cohere Embed v4 document embeddings. | `BedrockEmbeddingClient` invokes Bedrock with `input_type=search_document`, `output_dimension=1024`, and validates returned dimension. | The OpenSearch mapping and runtime query embeddings must stay dimension-compatible. |
| Vector search is FAISS HNSW cosine. | `OpenSearchIndexManager` and CDK mapping define `knn_vector` dimension `1024`, engine `faiss`, name `hnsw`, `space_type=cosinesimil`, `m=24`, `ef_construction=128`. | Changing model dimension or distance function is an index contract change, not a query-only tuning change. |
| Hybrid retrieval is fixed for launch. | `OpenSearchIndexManager` ensures the `skywalker-faq-hybrid` search pipeline using min-max normalization and arithmetic-mean combination. | Sparse and vector retrieval both remain part of launch candidate generation; depths and weights are calibration surfaces. |

The remaining assumptions are deliberately called out because they can be wrong in production:

| Assumption | Consequence if false | Revisit path |
| --- | --- | --- |
| The Top 50 FAQ corpus remains small enough for full rebuilds within Lambda timeouts and dependency throttle envelopes. | Daily or manual rebuilds may fail to promote often enough to maintain freshness. | Reopen full rebuild vs incremental mutation after observing post-launch rebuild duration, dependency throttling, and promotion failure rate across hundreds of active users. |
| COREx `lastModifiedDate` advances for every source change that should affect FAQ answers. | The marker may remain unchanged while meaningful content changes, leaving the live corpus stale. | Add a stronger snapshot marker only after source-side evidence shows missed updates under real authoring traffic. |
| One COREx node carries a coherent FAQ evidence unit. | Retrieval may return over-broad evidence or miss sub-question intent inside large nodes. | Reopen chunking only after production miss analysis shows node-level granularity is the dominant failure mode. |
| Required scope exists in COREx metadata/geography for launch FAQ content. | Valid source content can be skipped, preventing promotion. | Fix source data first; consider new scope contract only if hundreds-user launch evidence shows systemic source gaps that cannot be corrected upstream. |

## 6. Inputs, Outputs, and Contracts

The Poller input is ignored at launch. The effective inputs are environment and service state:

- `COREX_DOMAIN_OWNER_ID` selects the COREx owner whose content is enumerated.
- `COREX_HOST`, `COREX_ROLE_ARN`, `COREX_ROLE_SESSION_PREFIX`, and `COREX_SECRET_NAME` configure COREx access.
- `SSM_SNAPSHOT_MARKER` names `/skywalker/ingestion/faq_evidence/last_snapshot_marker`.
- `SSM_LIVE_INDEX` names `/skywalker/ingestion/faq_evidence/live_index`.
- `OPENSEARCH_ENDPOINT` names the AOSS collection endpoint.
- `PROCESSOR_FUNCTION_NAME` names the Processor Lambda.

COREx enumeration uses `searchContent` on `/search/graphql`, filtered by domain owner, paginated in pages of 50, and accepts only rows with UUID-shaped `nodeId` and nonblank `lastModifiedDate`. The marker is the maximum `lastModifiedDate` across accepted rows.

Full source materialization uses `getContentNodes(input: GetContentNodesInput!)` on `/infoarch/getContentNodes/graphql`, passing one node ID per call with `returnFieldVersions: true` and `returnTaxonomyValues: "LABEL"`. The fetcher reads the returned `nodes[0]` and parses `content` and `metadata` JSON strings into structured fields.

The Processor output contract is a `WorkItemResponse` containing the run ID, work item index, fragment count, and skipped node IDs. A content-shaped failure such as blank text or missing required scope returns zero fragments and marks the node skipped. Transport or dependency failures are retried per node; once retries are exhausted, the node is skipped. The Processor continues attempting the rest of the work item so the coordinator can make one complete promotion decision.

The OpenSearch document contract is the `FragmentDocument` shape:

- `fragment_id`: keyword, the COREx node ID in the launch one-node-one-document model.
- `source_id`: keyword, the COREx node ID.
- `text`: text, the extracted body and BM25 field.
- `source_url`: keyword, not indexed.
- `policy_links`: keyword array, not indexed.
- `country`: keyword array of real COREx values.
- `level`: keyword array of real COREx values.
- `role`: keyword array of real COREx values.
- `corpus_version`: keyword, the run snapshot marker.
- `followup_fragment_ids`: keyword array, not indexed, empty at launch.
- `content_type`: keyword, resolved from versioned COREx metadata and promoted for filtering.
- `source_metadata`: `flat_object`, preserved COREx metadata.
- `embedding`: `knn_vector`, dimension 1024, FAISS HNSW cosine.

The publication contract is that the query path reads only the physical index named by `/skywalker/ingestion/faq_evidence/live_index`. A rebuild target does not become query-visible until the pointer is flipped. The marker at `/skywalker/ingestion/faq_evidence/last_snapshot_marker` is written only after successful promotion.

## 7. Fixed Decisions

| Decision | Rationale | Binds | Reopen criteria |
| --- | --- | --- | --- |
| Use two physical indices plus `/skywalker/ingestion/faq_evidence/live_index`. | AOSS Serverless does not provide the alias behavior this design needs, while an SSM pointer gives zero-downtime switching with a small owned corpus. | Query services, rebuild target selection, rollback posture, and promotion semantics. | Reopen only if AOSS Serverless supports the needed alias semantics and migration reduces operational risk without weakening all-or-nothing publication. |
| Use `/skywalker/ingestion/faq_evidence/last_snapshot_marker` as the single high-water marker. | One max `lastModifiedDate` is simple, auditable, and enough to decide whether the corpus needs rebuilding. | Poller no-op behavior, corpus version stamping, and rebuild cadence. | Reopen if production authoring evidence shows marker misses or if source changes require finer-grained freshness guarantees. |
| Rebuild the full corpus instead of mutating the live corpus incrementally. | Full rebuilds make deletion handling and mapping updates simple, and they avoid accumulating partial state in the live index. | Index recreation, document ID non-requirement, promotion gate, and failure behavior. | Reopen if rebuild duration, dependency quotas, or corpus size prevent reliable promotion under launch traffic. |
| Promote only after a complete, non-skipping, read-back-verified rebuild. | A partial corpus is more dangerous than a stale complete corpus for answer-vs-abstain correctness. | Processor skip semantics, read-back polling, marker write ordering, and user-visible freshness. | Reopen only with evidence that strict blocking harms users more than partial publication, and with a replacement contract that preserves answer integrity. |
| Map one COREx node to one `FragmentDocument` and one index document. | The launch corpus is controlled FAQ evidence, and node-level storage avoids chunking complexity before traffic proves it is needed. | Index schema, source ID semantics, read-back expected count, and no parent/child runtime behavior. | Reopen after production retrieval analysis shows node granularity is the primary recall or precision failure across hundreds of active users. |
| Store full COREx metadata as `flat_object` and promote only needed filter fields. | `flat_object` preserves provenance without mapping explosion from versioned custom keys; `content_type` is indexed because the query path needs exact filtering. | Index mapping, future filter expansion, and metadata contract. | Reopen when a specific metadata field becomes a measured query-time filter requirement. |
| Use Cohere Embed v4 1024-dimensional document embeddings with FAISS HNSW cosine. | The embedding client and index mapping agree on a fixed vector shape and distance function. | Bedrock invocation, OpenSearch mapping, query embedding compatibility, and rebuild requirement for model changes. | Reopen only as a coordinated embedding/index migration with measured quality or cost evidence. |
| Launch with hybrid retrieval through `skywalker-faq-hybrid`. | FAQ evidence benefits from exact-term and semantic matching; launch should not force sparse-only or vector-only behavior before calibration data exists. | Query pipeline architecture, candidate generation, downstream reranking input, and evaluation. | Reopen the existence of hybrid retrieval only after post-launch evidence shows one retrieval leg is consistently harmful. Weights, depths, `k`, and size are calibration, not architecture reopeners. |

## 8. Alternatives Considered

The main alternative to the two-index SSM pointer model was an AOSS alias-based promotion model. It is attractive because many search systems use aliases to switch read traffic between built indices. It is rejected for launch because the implementation and source comments treat AOSS Serverless aliases as unavailable for this need. The SSM pointer is simpler, explicit, and already wired into both rebuild target selection and query-side live index resolution.

Incremental mutation was considered instead of full rebuild. It is attractive because it can reduce work for large corpora and improve freshness after small source changes. It is rejected for launch because it creates deletion, idempotency, and partial-failure complexity that the Top 50 FAQ corpus does not yet require. Full rebuild also makes the AOSS-assigned document ID constraint harmless because the target index is recreated empty.

Partial promotion with skipped nodes was considered as a way to increase freshness. It is rejected because it violates the controlled-corpus tenet. A skipped node can represent a data-quality issue, a dependency fault, or a systemic failure. Promoting around it would make the live corpus incomplete while making the high-water marker appear current.

Parent/child chunking was considered for richer document structure. It is attractive for large articles where sub-sections need independent retrieval and reassembly. It is rejected for launch because the implemented FAQ source unit is one COREx node, the evidence shape has no parent ID, and `followup_fragment_ids` is explicitly empty until later work lands. Adding parent/child behavior now would create a contract the runtime does not implement.

Sparse-only and vector-only retrieval were considered as simpler query architectures. Sparse-only is attractive because it is explainable and cheap; vector-only is attractive because it handles paraphrase and wording drift. Both are rejected for launch because the fixed architecture is hybrid retrieval: candidate generation uses both lexical and semantic evidence, and calibration tunes how they are combined.

## 9. End-to-End Flow

The scheduled or manually invoked Poller starts a rebuild run and reads the previous high-water marker from `/skywalker/ingestion/faq_evidence/last_snapshot_marker`. It then enumerates COREx nodes through `searchContent`, filtered by domain owner. Invalid enumeration rows are skipped during enumeration if the node ID is not UUID-shaped or the row has no usable `lastModifiedDate`.

If enumeration returns zero accepted nodes, the run exits as a no-op and leaves the live corpus intact. If the computed max `lastModifiedDate` equals the stored marker, the run also exits as a no-op. In both cases, no target index is recreated and no live pointer changes.

When the marker has advanced or no prior marker exists, the coordinator asks `OpenSearchIndexManager` for the rebuild target. The manager reads `/skywalker/ingestion/faq_evidence/live_index`, chooses the non-live physical index, deletes it if present, recreates it with the current FAISS/vector/BM25 mapping, and best-effort ensures the `skywalker-faq-hybrid` search pipeline. On first run, when the pointer is absent, the target is `faq_evidence_a`.

The coordinator groups node IDs into work items and synchronously dispatches them to the Processor Lambda. For each node, the Processor fetches full COREx content through `getContentNodes`, extracts PlateJS body text, maps scope from real COREx values, validates that `country`, `level`, and `role` are all nonempty, embeds the text with Bedrock Cohere Embed v4 as `search_document`, and writes one document to the rebuild target through SigV4-signed AOSS data-plane calls.

The coordinator aggregates work item responses. If any node was skipped or if the fragment count is below the enumerated node count, promotion is refused. If work completion is nominal, the coordinator polls the rebuild target count until every expected document is queryable or the read-back budget expires. Only after read-back reaches expected count does the coordinator promote the target by writing `/skywalker/ingestion/faq_evidence/live_index`.

After the live pointer flip succeeds, the coordinator writes the new high-water marker to `/skywalker/ingestion/faq_evidence/last_snapshot_marker`. The order matters: if the marker were advanced before the live pointer, a failed promotion could make the next run incorrectly believe the source state was already published.

## 10. Failure Behavior

The failure posture is fail-closed for publication and fail-stale for readers. A failed rebuild does not clear or mutate the currently live index. Users continue querying the last complete promoted corpus.

If COREx enumeration fails, the run fails before selecting or promoting a target. If COREx returns zero accepted nodes, the run treats that as unsafe to publish and exits no-op. If an individual COREx node has blank extracted text or missing required scope, the Processor records a skip and continues, but the coordinator refuses promotion because the rebuild is incomplete.

If Bedrock embedding or AOSS write calls fail for a node, the Processor retries that node with exponential backoff and jitter. After retries are exhausted, the node is skipped. The remaining nodes are still attempted so the run produces a complete accounting, but a nonempty skip list prevents promotion.

If target index recreation fails, the run does not dispatch work and the live pointer remains unchanged. If hybrid pipeline creation fails, ingestion continues because the pipeline is a search-time object; however, launch readiness still requires the `skywalker-faq-hybrid` pipeline to exist for the online path.

If read-back count never reaches the expected document count, promotion is refused. This protects against AOSS refresh races and silent write visibility failures. If live pointer promotion succeeds but marker write fails, users are still correct because the live pointer names the newly built corpus; the next run may rebuild again because the marker was not advanced.

## 11. Calibration Surfaces

Hybrid retrieval itself is fixed for launch. The calibration surfaces are the empirical settings around that architecture:

- BM25 and vector candidate depths.
- Query-time `k`, request `size`, and any `ef_search` equivalent exposed by the search path.
- Hybrid combination weights or equivalent normalization/combination settings.
- Reranker candidate budget consumed downstream.
- Poll cadence where stages wire an EventBridge schedule.
- Processor work item size.
- Per-node retry count, base backoff, and max backoff.
- Read-back polling attempts and interval.
- Scope-specific quality thresholds in downstream evaluation.

Changing these values should be treated as calibration when it does not change the launch contract: COREx source, one node to one document, full rebuild, all-or-nothing promotion, two physical indices, SSM live pointer, Cohere Embed v4 1024-dimensional vectors, FAISS HNSW cosine, and hybrid retrieval.

## 12. Open Questions

Many open questions in this section cannot be resolved through offline experimentation alone. The ingestion and retrieval system has not yet observed enough production behavior, real user query patterns, source-authoring failure modes, content distribution, or query diversity to close them honestly. Initial decisions should therefore optimize for observability, safe iteration, and low-cost reversibility. Final answers for several ingestion, retrieval-depth, and metadata questions should emerge only after launch and after observing usage across hundreds of active users.

1. Does the Top 50 FAQ corpus remain small enough that full rebuilds continue to promote reliably as content and traffic grow?
2. Does one COREx node remain the right retrieval unit, or do production misses show a need for chunking?
3. Are the current scope fields sufficient for real user identity and eligibility patterns?
4. Does the high-water marker catch every source edit, publish, retract, and metadata change that matters to answers?
5. Which hybrid retrieval depths and weights maximize answer accuracy without increasing false positives?
6. Should additional COREx metadata fields become first-class indexed filters?

The evidence standard for reopening launch decisions is intentionally high. A change should be based on post-launch observations across hundreds of active users, including query logs or evaluation traces, skipped-node and promotion-failure history, source authoring examples, and measured answer quality impact. A single anecdote can start an investigation, but it should not by itself overturn the rebuild, storage, or hybrid retrieval architecture.

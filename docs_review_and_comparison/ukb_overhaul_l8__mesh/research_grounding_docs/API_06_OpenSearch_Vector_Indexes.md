# API Contract 06. OpenSearch Vector Index (FAQ evidence)

Covers the single OpenSearch vector index that holds FAQ evidence children, the child-chunk schema that supports post-rerank sibling expansion, and the scope-filter fields on that index. Pairs with API_04 (embedding model), API_10 (reranker), API_11 (static variant set held in S3, not in OpenSearch), and Sections 03, 04, 06.

## What the architecture has already fixed

- Section 03 §3 decision seven: evidence lives in a single OpenSearch vector index. The variant set is **not** an OpenSearch surface — it is a static S3 artifact maintained manually and loaded into memory at service boot. See API_11.
- Section 03 §3 decision nine: publication safety — a rebuild that has not completed cannot become visible. Rebuild-and-republish is the mutation model.
- Section 03 §3 decision eight: the evidence children and the query are embedded under the same Cohere Embed v4 contract at dimension 1024 (API_04). The same query vector is reused against the in-memory variant embeddings held by the routing gate (see API_11 and Section 04).
- Section 03 decision on chunking (D-26): hierarchical structural chunking feeds a semantic child splitter with a 1000-token hard ceiling. Parents are not stored; children carry `parent_id`, `child_order`, `child_count`, and `split_type` as retrievable-but-unindexed metadata. Post-rerank sibling expansion reconstructs the parent at query time.
- Section 04 decision on scope filtering: country, level, and role are filterable fields on the single evidence index. Pre-filter applied alongside the k-NN query so only in-scope candidates reach the reranker.

## What OpenSearch gives us

From the [OpenSearch k-NN methods and engines docs](https://docs.opensearch.org/latest/mappings/supported-field-types/knn-methods-engines/):

- `knn_vector` field type with configurable `method`, `engine`, `space_type`.
- Engines: `lucene` (native, HNSW only, efficient pre-filter), `faiss` (HNSW and IVF with quantization). Lucene is the default for Skywalker because efficient filtering against the scope fields matters more than raw vector throughput at this corpus scale.
- Lucene supports per-field vector dimensions up to 1024 without special configuration, which matches Skywalker's D-21 embedding-dimension decision.
- Cosine similarity (`cosinesimil`) is the space type for text embeddings.

Individual field mappings can be declared `"index": false` to store the field in `_source` without adding it to the inverted index. Reads via `_source` still return the field; filters and sorts that reference it will fail. This is how child-chunk ordering metadata is stored.

Content from external sources has been rephrased for compliance with licensing restrictions.

## The evidence index — `faq_evidence_v<N>`

One document per child chunk. Every child from the same hierarchical parent shares a `parent_id`.

```json
PUT /faq_evidence_v1
{
  "settings": {
    "index": {
      "knn": true,
      "knn.algo_param.ef_search": 100
    }
  },
  "mappings": {
    "properties": {
      "chunk_id":       { "type": "keyword" },
      "parent_id":      { "type": "keyword" },
      "child_order":    { "type": "integer", "index": false },
      "child_count":    { "type": "integer", "index": false },
      "split_type":     { "type": "keyword", "index": false },
      "source_id":      { "type": "keyword" },
      "title":          { "type": "text" },
      "text":           { "type": "text" },
      "source_url":     { "type": "keyword" },
      "policy_links":   { "type": "keyword" },
      "country":        { "type": "keyword" },
      "level":          { "type": "keyword" },
      "role":           { "type": "keyword" },
      "corpus_version": { "type": "keyword" },
      "linked_parent_ids": { "type": "keyword", "index": false },
      "embedding": {
        "type": "knn_vector",
        "dimension": 1024,
        "method": {
          "name": "hnsw",
          "space_type": "cosinesimil",
          "engine": "lucene",
          "parameters": {
            "ef_construction": 128,
            "m": 24
          }
        }
      }
    }
  }
}
```

Field notes:

- **`chunk_id`** — unique child identifier. Used as `candidate_id` in the common envelope and echoed in MCP responses.
- **`parent_id`** — shared across all children of one hierarchical unit. Filterable at query time for the sibling expansion.
- **`child_order` / `child_count` / `split_type`** — retrievable from `_source` only. Never filtered or sorted against. Keeping them unindexed saves inverted-index space and avoids accidental dependency on these fields in queries.
- **`split_type`** — enum: `paragraph_boundary`, `size_forced`, `other`. Drives the concatenation separator during sibling reconstruction.
- **`country`, `level`, `role`** — scope filter fields. Arrays of keyword values, since a chunk may apply to multiple countries or both roles. Pre-filter at query time.
- **`corpus_version`** — tracks which publication this child belongs to. Always matches the corpus version carried on the SSM high-water marker (API_07) at the moment the index became live.
- **`linked_parent_ids`** — ordered keyword array, retrievable from `_source` only. Holds the parent_ids of the linked Q&As that ride along with this chunk's own parent at evidence-assembly time, in linked-list (chain) order. Empty array when the chunk's parent has no outgoing link. Author-asserted contextual relationships; never filtered or sorted against. The chain is materialized at ingest from a single-valued custom metadata field on the FAQ content model in COREx (the "next" pointer per Q&A) and bounded at depth 2.

No separate parent index. Parents are reconstructed from children at query time (see "Post-rerank sibling expansion" below). No variants index — the variant set does not live in OpenSearch.

### HNSW parameters are architecture-class, not control-plane

`ef_construction`, `m`, `dimension`, `space_type`, and `engine` are fixed at index-creation time. Changing any of them requires a full index rebuild. These are not tunable without a republish. Values that change without a rebuild — per-query `ef_search`, per-query `k`, per-query `size` — are different and may become control-plane later.

## Runtime queries

### Evidence retrieval with scope pre-filter (against `faq_evidence_current`)

```json
POST /faq_evidence_current/_search
{
  "size": 20,
  "_source": [
    "chunk_id", "parent_id", "child_order", "child_count", "split_type",
    "source_id", "title", "text", "source_url", "policy_links",
    "country", "level", "role"
  ],
  "query": {
    "knn": {
      "embedding": {
        "vector": [...],
        "k": 40,
        "filter": {
          "bool": {
            "must": [
              { "term": { "country": "US" } },
              { "term": { "level": "L5" } },
              { "term": { "role": "INDIVIDUAL_CONTRIBUTOR" } }
            ]
          }
        }
      }
    }
  }
}
```

The `filter` inside the k-NN query is applied **pre-filter** on the Lucene engine, so only in-scope candidates enter the vector search. `k: 40` over-retrieves so the pre-filter still yields 20 final candidates even when the scope cuts the population; tune the over-retrieval factor against measured filter selectivity during Phase 2.

### Post-rerank sibling and linked-parent expansion (against `faq_evidence_current`)

After reranking returns the top `N` children, the runtime collects two sets of parent_ids from the winners: each winner's own `parent_id`, and the parent_ids in each winner's `linked_parent_ids` array (the precomputed depth-2 chain of author-asserted contextually-linked Q&As). The union of those two sets — deduplicated — is one terms list:

```json
POST /faq_evidence_current/_search
{
  "size": 200,
  "_source": [
    "chunk_id", "parent_id", "child_order", "child_count", "split_type",
    "source_id", "title", "text", "source_url", "policy_links"
  ],
  "query": {
    "terms": {
      "parent_id": [
        "<anchor_parent_1>", "<anchor_parent_2>",
        "<linked_parent_a>", "<linked_parent_b>"
      ]
    }
  }
}
```

The query carries no scope filter. Sibling expansion within an anchor's own parent inherits scope from the original retrieval (the anchor passed scope-filtered retrieval to win rerank in the first place). Linked-parent expansion is curatorial: an author asserting "B is contextually relevant alongside A" is the link, and that assertion stands regardless of B's own `country`/`level`/`role` tags. Treating both expansion behaviors with one unfiltered terms query keeps the runtime path simple and matches the architectural decision to honor curatorial linkage as an exception to scope filtering for post-rerank expansion only.

Client-side: group results by `parent_id`, sort each group by `child_order`, concatenate `text` fields using a separator derived from `split_type` (`"\n\n"` on `paragraph_boundary`, empty string on `size_forced`, configurable on `other`). For each anchor in the shortlist, the final rendered `text` is the anchor's reconstructed parent text followed by each linked Q&A's reconstructed parent text in chain order, separated by `"\n\n"`, with one Unicode superscript citation marker (`¹`, `²`, `³`) at the end of each segment. The anchor's MCP envelope record carries a parallel `citations[]` field whose entries match the markers and resolve to `{marker, source_id, title, source_url, policy_links}` for each segment.

**Integrity handling on missing siblings or linked Q&As.** If a parent's returned children count is less than `child_count`, the reconstruction utility returns what is present (sorted by `child_order`), logs a structured warning, and emits a CloudWatch metric. If a linked Q&A returns no chunks at all (depublish race, missing children), the linked segment is dropped from concatenation and `LinkedItemSuppressed` fires. The answer still goes out with the surviving fragment text. This is best-effort by design; the alternative (abstaining because of a storage-level inconsistency) would fail the user for a condition they cannot do anything about.

## Publication discipline

Rebuild-and-republish using an OpenSearch index alias:

- `faq_evidence_current` → alias → `faq_evidence_v<N>`

The ingestion job builds `faq_evidence_v<N+1>` in full, validates it, and atomically moves the alias via the `_aliases` API. That one alias-swap call is the only atomic publication primitive Skywalker uses for the evidence corpus; the SSM high-water marker (API_07) is updated as a separate call after the swap.

**Publication state is minimal and external.** The alias-swap atomicity lives in OpenSearch. The single piece of state that persists between ingestion runs — the CoreX snapshot high-water mark — lives in AWS Systems Manager Parameter Store (API_07). "Which `corpus_version` is live" is not stored anywhere: it is whatever `faq_evidence_current` points at, discoverable via `GET /_cat/aliases`.

Old index versions are retained for the rollback window (default: keep the last three versions). The ingestion job performs garbage collection as a final step each run, deleting indexes older than the retention window.

## What we still need to decide

1. **Cluster shape.** Amazon OpenSearch Service managed domain vs. OpenSearch Serverless. Managed Service is the defensible baseline for the corpus scale; Serverless is attractive if autoscaling and no-capacity-planning outweigh the cost multiplier.
2. **Over-retrieval factor `k` against the scope pre-filter.** Launch default is `k: 40` for a `size: 20` result. If measured filter selectivity shows we consistently over-retrieve (returning much more than 20 in-scope candidates) or under-retrieve (returning fewer than 20), the factor tunes.
3. **HNSW parameters.** `m=24`, `ef_construction=128`, `ef_search=100` are reasonable defaults for a tiny corpus. Retune against measured retrieval quality during Phase 2. Note that `m` and `ef_construction` are rebuild-required; `ef_search` is per-query.
4. **Auth.** SigV4 on Amazon OpenSearch Service; username/password on self-hosted. Choice follows the cluster-shape decision.

## Sections of the architecture this binds

- Section 03 §3 decisions four, seven, eight, nine (mutation posture, single storage topology, embedding contract, publication safety), plus D-26 (chunker architecture) and D-35 (linked-parent chain materialization at ingest).
- Section 04 §2, §3 decision nine, scope-field filtering decision.
- Section 07 §3 decisions four, five, eleven (reranker text surface, abstain rule), plus the linked-item expansion contract.
- API_07 (SSM high-water mark and alias-swap ordering).
- API_10 (reranker candidate pool size and sibling-expansion handoff).
- API_11 (static variant set held in S3, loaded into memory at service boot — not stored in OpenSearch).

## Outstanding unknowns

- Cluster shape (managed vs. Serverless) and endpoint topology.
- Final HNSW parameters.
- Final `k` over-retrieval factor against measured scope-filter selectivity.
- Final index-retention window (launch default is three versions; revisit if rollback needs change).

/**
 * Canonical OpenSearch mapping for the Skywalker FAQ evidence index.
 *
 * One COREx fragment becomes one document. Scope filtering happens on the keyword
 * fields `country`, `level`, and `role`; every fragment carries either specific values
 * or the per-axis sentinel ("Global" / "All Job Levels" / "All Employee Classes"), and
 * the query-side filter matches a requester's specific value OR the sentinel.
 *
 * `source_metadata` is a `flat_object` preserving the full raw COREx metadata for every
 * record (the backup of all metadata fields), so nothing is lost even though only the
 * three scope axes are promoted to first-class filterable fields.
 *
 * Notes:
 * - `line_of_business` is intentionally NOT a field here. LOB is not a scope axis for
 *   Skywalker; the raw COREx `system_line-of-business` value still survives inside
 *   `source_metadata` for audit, but it is not promoted or filtered on.
 * - `policy_links` is a first-class (non-indexed, retrievable) keyword field carrying the
 *   fragment's policy URLs.
 * - HNSW/engine/dimension/space are fixed at index-creation time (architecture-class).
 */
export const FAQ_EVIDENCE_INDEX_NAME = 'faq_evidence';

export const FAQ_EVIDENCE_INDEX_BODY = {
  settings: { index: { knn: true } },
  mappings: {
    properties: {
      // Vector (kNN leg of hybrid retrieval).
      embedding: {
        type: 'knn_vector',
        dimension: 1024,
        method: {
          engine: 'faiss',
          name: 'hnsw',
          space_type: 'cosinesimil',
          parameters: { m: 24, ef_construction: 128 },
        },
      },

      // Identity / structural.
      fragment_id: { type: 'keyword' },
      source_id: { type: 'keyword' },
      followup_fragment_ids: { type: 'keyword', index: false },

      // Retrieval content.
      title: { type: 'text' },
      text: { type: 'text' },

      // Scope axes (keyword, array-valued; filtered with sentinel-OR at query time).
      country: { type: 'keyword' },
      level: { type: 'keyword' },
      role: { type: 'keyword' },

      // Source / provenance.
      source_url: { type: 'keyword', index: false },
      policy_links: { type: 'keyword', index: false },
      content_type: { type: 'keyword' },
      corpus_version: { type: 'keyword' },

      // Preserved backup of the full raw COREx metadata for every record.
      source_metadata: { type: 'flat_object' },
    },
  },
} as const;

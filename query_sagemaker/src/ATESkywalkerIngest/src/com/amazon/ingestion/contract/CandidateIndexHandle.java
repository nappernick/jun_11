package com.amazon.ingestion.contract;

/**
 * Identifies the versioned OpenSearch index being built during a rebuild run.
 *
 * Pairs with the live alias (faq_evidence_current) that the Poller atomically swaps to
 * point at the candidate index on successful completion.
 *
 * @param indexName      versioned name, e.g. faq_evidence_v42.
 * @param version        the integer N in faq_evidence_v&lt;N&gt;.
 * @param snapshotMarker the CoreX snapshot marker that triggered this rebuild;
 *                       written to SSM on successful alias swap.
 */
public record CandidateIndexHandle(String indexName, int version, String snapshotMarker) {

    public CandidateIndexHandle {
        if (indexName == null || indexName.isBlank()) {
            throw new IllegalArgumentException("indexName must be non-blank");
        }
        if (version < 1) {
            throw new IllegalArgumentException("version must be >= 1");
        }
        if (snapshotMarker == null || snapshotMarker.isBlank()) {
            throw new IllegalArgumentException("snapshotMarker must be non-blank");
        }
    }
}

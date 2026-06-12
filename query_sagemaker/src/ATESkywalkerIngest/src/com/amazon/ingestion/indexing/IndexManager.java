package com.amazon.ingestion.indexing;

import com.amazon.ingestion.contract.CandidateIndexHandle;

/**
 * OpenSearch index lifecycle operations owned by the Poller.
 *
 * The Poller is the single coordinator for index creation, validation, the atomic alias
 * swap, and garbage collection. Processors never touch these operations; they only write
 * documents into an already-created candidate index.
 */
public interface IndexManager {

    /**
     * Create a fresh candidate index with the full FAQ evidence mapping (API_06).
     *
     * @param snapshotMarker the CoreX snapshot marker driving this rebuild;
     *                       recorded on the returned handle for later SSM persistence.
     * @return handle identifying the new candidate index.
     */
    CandidateIndexHandle createCandidateIndex(String snapshotMarker);

    /**
     * Run post-build validation on the candidate index (doc count, dimension spot-check).
     * Throws if the index is not fit to publish.
     *
     * @param candidate the index to validate.
     */
    void validate(CandidateIndexHandle candidate);

    /**
     * Atomically move the faq_evidence_current alias to point at the candidate index.
     * This is the publication primitive.
     *
     * @param candidate the index to become live.
     */
    void swapAlias(CandidateIndexHandle candidate);

    /**
     * Delete the candidate index after a failed rebuild so it does not accumulate as an orphan.
     *
     * @param candidate the index to drop.
     */
    void deleteOrphan(CandidateIndexHandle candidate);

    /**
     * Drop versioned indexes older than the retention window (launch default: keep last 3).
     */
    void garbageCollectOldVersions();
}

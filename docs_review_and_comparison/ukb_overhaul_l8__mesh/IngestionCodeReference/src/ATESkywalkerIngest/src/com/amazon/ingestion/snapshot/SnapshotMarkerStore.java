package com.amazon.ingestion.snapshot;

import java.util.Optional;

/**
 * SSM-backed persistence of the single high-water mark (R5).
 *
 * Exactly one parameter per corpus: /skywalker/ingestion/faq_evidence/last_snapshot_marker,
 * holding the most-recent lastModifiedDate ingested. Absence of the parameter (first run)
 * surfaces as Optional.empty.
 *
 * The coordinator writes the new marker only after the rebuild work completes. A write
 * failure produces a benign rebuild on the next run, not a correctness hazard.
 */
public interface SnapshotMarkerStore {

    /**
     * Read the last-successful marker.
     *
     * @return the marker written by the previous run, or empty on first run.
     */
    Optional<String> read();

    /**
     * Persist the marker after the rebuild work for this run completes.
     *
     * @param marker the most-recent lastModifiedDate just ingested.
     */
    void write(String marker);
}

package com.amazon.ingestion.snapshot;

import java.util.Optional;

/**
 * SSM-backed persistence of the last-successful CoreX snapshot marker.
 *
 * Exactly one parameter per corpus: /skywalker/ingestion/faq_evidence/last_snapshot_marker.
 * Absence of the parameter (first run) surfaces as Optional.empty.
 *
 * Write ordering is fixed by API_07: the Poller writes the new marker only after a
 * successful alias swap. A write failure after a successful alias swap produces a
 * benign rebuild on the next run, not a correctness hazard.
 */
public interface SnapshotMarkerStore {

    /**
     * Read the last-successful marker.
     *
     * @return the marker written by the previous run, or empty on first run.
     */
    Optional<String> read();

    /**
     * Persist the marker after a successful alias swap.
     *
     * @param marker the CoreX snapshot marker that just became live.
     */
    void write(String marker);
}

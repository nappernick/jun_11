package com.amazon.ingestion.heartbeat;

/**
 * Records the timestamp of every Poller run, whether or not content changed.
 *
 * Separate from SnapshotMarkerStore on purpose. The heartbeat advances on every run
 * as an observability signal; the content-update watermark only advances on a
 * successful rebuild-and-publish.
 */
public interface PollHeartbeat {

    /**
     * Write the current timestamp to the last-poll-timestamp parameter.
     *
     * @param isoTimestamp ISO-8601 UTC timestamp of the current run.
     */
    void record(String isoTimestamp);
}

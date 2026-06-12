package com.amazon.ingestion.pipeline;

import com.amazon.ingestion.contract.CandidateIndexHandle;
import com.amazon.ingestion.contract.WorkItemResponse;
import com.amazon.ingestion.dispatch.WorkItemDispatcher;
import com.amazon.ingestion.enumeration.CoreXEnumerator;
import com.amazon.ingestion.enumeration.EnumeratedNode;
import com.amazon.ingestion.heartbeat.PollHeartbeat;
import com.amazon.ingestion.indexing.IndexManager;
import com.amazon.ingestion.snapshot.SnapshotMarkerStore;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

/**
 * Orchestrates one rebuild-and-republish run for the FAQ evidence corpus.
 *
 * Sequence (API_07, Section 03):
 * - Record a poll heartbeat regardless of whether content changed.
 * - Read the last-successful snapshot marker from SSM.
 * - Enumerate the current FAQ corpus from COREx and compute its marker.
 * - If markers match, exit no-op.
 * - Otherwise: create candidate index, fan out to Processor.
 * - On any work item failure, abort: delete the orphan candidate, leave SSM marker unchanged.
 * - On full success: validate, swap alias, write SSM marker, GC old versions.
 *
 * Alias swap precedes the SSM write. A failure in between produces a benign rebuild next run.
 * GC failure after the SSM write is logged but does not fail the run - the publish has
 * already succeeded and orphans get cleaned up on the next run.
 */
public final class RebuildCoordinator {

    private static final Logger LOGGER = LogManager.getLogger(RebuildCoordinator.class);

    private static final int DEFAULT_WORK_ITEM_SIZE = 50;

    private final SnapshotMarkerStore markerStore;
    private final PollHeartbeat heartbeat;
    private final CoreXEnumerator coreX;
    private final IndexManager indexManager;
    private final WorkItemDispatcher dispatcher;
    private final int workItemSize;

    public RebuildCoordinator(
            SnapshotMarkerStore markerStore,
            PollHeartbeat heartbeat,
            CoreXEnumerator coreX,
            IndexManager indexManager,
            WorkItemDispatcher dispatcher) {
        this(markerStore, heartbeat, coreX, indexManager, dispatcher, DEFAULT_WORK_ITEM_SIZE);
    }

    public RebuildCoordinator(
            SnapshotMarkerStore markerStore,
            PollHeartbeat heartbeat,
            CoreXEnumerator coreX,
            IndexManager indexManager,
            WorkItemDispatcher dispatcher,
            int workItemSize) {
        this.markerStore = markerStore;
        this.heartbeat = heartbeat;
        this.coreX = coreX;
        this.indexManager = indexManager;
        this.dispatcher = dispatcher;
        this.workItemSize = workItemSize;
    }

    public RunOutcome run() {
        String runId = UUID.randomUUID().toString();
        LOGGER.info("Rebuild run starting runId={}", runId);

        heartbeat.record(Instant.now().toString());

        Optional<String> lastMarker = markerStore.read();

        List<EnumeratedNode> nodes = coreX.enumerate();
        if (nodes.isEmpty()) {
            LOGGER.warn("CoreX returned zero FAQ nodes; refusing to publish an empty corpus runId={}", runId);
            return RunOutcome.aborted(runId, null, "empty node enumeration");
        }

        String currentMarker = coreX.computeMarker(nodes);

        if (lastMarker.isPresent() && lastMarker.get().equals(currentMarker)) {
            LOGGER.info("Snapshot marker unchanged ({}), skipping rebuild runId={}", currentMarker, runId);
            return RunOutcome.noOp(runId, currentMarker);
        }

        LOGGER.info(
                "Snapshot changed last={} current={}, beginning rebuild runId={} nodes={}",
                lastMarker.orElse("<none>"),
                currentMarker,
                runId,
                nodes.size());

        List<String> nodeIds = nodes.stream().map(EnumeratedNode::nodeId).toList();

        CandidateIndexHandle candidate = indexManager.createCandidateIndex(currentMarker);
        LOGGER.info("Created candidate index {} runId={}", candidate.indexName(), runId);

        List<WorkItemResponse> responses;
        try {
            responses = dispatcher.dispatch(runId, candidate.indexName(), nodeIds, workItemSize);
        } catch (RuntimeException e) {
            LOGGER.error("Dispatcher threw, aborting rebuild runId={}", runId, e);
            indexManager.deleteOrphan(candidate);
            return RunOutcome.aborted(runId, currentMarker, "dispatcher failure: " + e.getMessage());
        }

        List<WorkItemResponse> failures = responses.stream()
                .filter(r -> r.status() == WorkItemResponse.Status.FAILURE)
                .toList();
        if (!failures.isEmpty()) {
            LOGGER.error(
                    "{}/{} work items failed, aborting rebuild runId={}",
                    failures.size(),
                    responses.size(),
                    runId);
            indexManager.deleteOrphan(candidate);
            return RunOutcome.aborted(runId, currentMarker, failures.size() + " work item failure(s)");
        }

        long totalChildren = responses.stream().mapToLong(WorkItemResponse::childrenIndexed).sum();
        LOGGER.info(
                "All {} work items succeeded, {} children indexed, validating runId={}",
                responses.size(),
                totalChildren,
                runId);

        indexManager.validate(candidate);
        indexManager.swapAlias(candidate);
        LOGGER.info("Alias swapped to {} runId={}", candidate.indexName(), runId);

        markerStore.write(currentMarker);
        LOGGER.info("Snapshot marker written runId={}", runId);

        // GC is non-critical cleanup. The publish has already succeeded (alias swapped,
        // marker written); a GC failure must not surface as a run failure to the caller.
        // Orphaned old index versions will be picked up by the next run's GC pass.
        try {
            indexManager.garbageCollectOldVersions();
        } catch (RuntimeException e) {
            LOGGER.warn(
                    "Garbage collection of old index versions failed; publish succeeded, "
                            + "GC will retry next run runId={}",
                    runId,
                    e);
        }

        return RunOutcome.published(runId, currentMarker, candidate, totalChildren);
    }

    /**
     * Outcome of a single rebuild run, surfaced back to the Poller handler for
     * logging and metric emission.
     *
     * @param kind             which terminal branch the run took.
     * @param runId            correlation ID.
     * @param snapshotMarker   marker observed at run start. Null if enumeration failed
     *                         before a marker could be computed.
     * @param candidateIndex   the published index, only set when kind is PUBLISHED.
     * @param childrenIndexed  total children written, only non-zero when kind is PUBLISHED.
     * @param reason           abort reason, only set when kind is ABORTED.
     */
    public record RunOutcome(
            Kind kind,
            String runId,
            String snapshotMarker,
            CandidateIndexHandle candidateIndex,
            long childrenIndexed,
            String reason) {

        /** Terminal branches a run can take. */
        public enum Kind {
            /** Snapshot marker unchanged; no rebuild performed. */
            NO_OP,
            /** Candidate index built and alias swapped. */
            PUBLISHED,
            /** Rebuild started but did not complete; alias and SSM marker unchanged. */
            ABORTED
        }

        /**
         * Build a no-op outcome.
         *
         * @param runId correlation ID.
         * @param marker marker observed at run start.
         * @return NO_OP outcome.
         */
        public static RunOutcome noOp(String runId, String marker) {
            return new RunOutcome(Kind.NO_OP, runId, marker, null, 0L, null);
        }

        /**
         * Build a published outcome.
         *
         * @param runId correlation ID.
         * @param marker marker observed at run start.
         * @param candidate the index that became live.
         * @param children total children written.
         * @return PUBLISHED outcome.
         */
        public static RunOutcome published(
                String runId, String marker, CandidateIndexHandle candidate, long children) {
            return new RunOutcome(Kind.PUBLISHED, runId, marker, candidate, children, null);
        }

        /**
         * Build an aborted outcome.
         *
         * @param runId correlation ID.
         * @param marker marker observed at run start, or null if enumeration failed first.
         * @param reason human-readable abort reason.
         * @return ABORTED outcome.
         */
        public static RunOutcome aborted(String runId, String marker, String reason) {
            return new RunOutcome(Kind.ABORTED, runId, marker, null, 0L, reason);
        }
    }
}

package com.amazon.ingestion.pipeline;

import com.amazon.ingestion.contract.WorkItemResponse;
import com.amazon.ingestion.dispatch.WorkItemDispatcher;
import com.amazon.ingestion.enumeration.CoreXEnumerator;
import com.amazon.ingestion.enumeration.EnumeratedNode;
import com.amazon.ingestion.indexing.IndexManager;
import com.amazon.ingestion.snapshot.SnapshotMarkerStore;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

/**
 * Orchestrates one rebuild-and-republish run for the FAQ evidence corpus (R5, T14).
 *
 * Sequence (zero-downtime, two physical indices + an SSM live pointer):
 * <ol>
 *   <li>Read the high-water mark (most-recent lastModifiedDate) from SSM.</li>
 *   <li>Enumerate the current corpus from COREx and compute its max lastModifiedDate.</li>
 *   <li>If the marker is unchanged, exit no-op.</li>
 *   <li>Otherwise {@code beginRebuild()} — (re)create the IDLE index empty — and dispatch every
 *       node to the Processor to write into it. The live index keeps serving throughout.</li>
 *   <li>Flip gate: promote only when the rebuild is 100% complete — every enumerated node was
 *       rebuilt successfully (no skips/failures after the Processor's per-node retries) AND all
 *       of those documents are queryable. If the build is partial or unverifiable, do NOT
 *       promote — the previous live index keeps serving and the marker is not advanced.</li>
 *   <li>Atomically {@code promote()} the new index (flip the SSM pointer), then write the
 *       marker.</li>
 * </ol>
 *
 * All-or-nothing (R5/R10): every change rewrites the entire corpus from the ground up, so a
 * rebuild is only published when it is complete. A node that cannot be rebuilt — even after
 * the Processor's per-node retries — leaves the run unpromoted rather than shipping a partial
 * corpus. Zero-downtime is structural: a partial or failed rebuild simply never gets promoted,
 * so readers always see a complete corpus (the old one until the new one is proven complete).
 * The empty-enumeration guard (COREx returned zero nodes) likewise leaves the live corpus
 * intact.
 */
public final class RebuildCoordinator {

    private static final Logger LOGGER = LogManager.getLogger(RebuildCoordinator.class);

    private static final int DEFAULT_WORK_ITEM_SIZE = 50;

    // Flip-gate read-back polling: AOSS Serverless refreshes ~every 10s in steady state, but a
    // freshly-created index has a cold first refresh measured at ~64s before written docs become
    // countable. Poll up to ~100s before deciding the build is empty.
    private static final int READBACK_MAX_ATTEMPTS = 50;
    private static final long READBACK_POLL_MILLIS = 2000L;

    private final SnapshotMarkerStore markerStore;
    private final CoreXEnumerator coreX;
    private final IndexManager indexManager;
    private final WorkItemDispatcher dispatcher;
    private final int workItemSize;

    public RebuildCoordinator(
            SnapshotMarkerStore markerStore,
            CoreXEnumerator coreX,
            IndexManager indexManager,
            WorkItemDispatcher dispatcher) {
        this(markerStore, coreX, indexManager, dispatcher, DEFAULT_WORK_ITEM_SIZE);
    }

    public RebuildCoordinator(
            SnapshotMarkerStore markerStore,
            CoreXEnumerator coreX,
            IndexManager indexManager,
            WorkItemDispatcher dispatcher,
            int workItemSize) {
        this.markerStore = markerStore;
        this.coreX = coreX;
        this.indexManager = indexManager;
        this.dispatcher = dispatcher;
        this.workItemSize = workItemSize;
    }

    public RunOutcome run() {
        String runId = UUID.randomUUID().toString();
        LOGGER.info("Rebuild run starting runId={}", runId);

        Optional<String> lastMarker = markerStore.read();

        List<EnumeratedNode> nodes = coreX.enumerate();
        if (nodes.isEmpty()) {
            LOGGER.warn(
                    "COREx returned zero nodes; leaving live corpus intact (no flip) runId={}", runId);
            return RunOutcome.noOp(runId, lastMarker.orElse(""));
        }

        String currentMarker = coreX.computeMarker(nodes);

        if (lastMarker.isPresent() && lastMarker.get().equals(currentMarker)) {
            LOGGER.info("High-water mark unchanged ({}), skipping rebuild runId={}", currentMarker, runId);
            return RunOutcome.noOp(runId, currentMarker);
        }

        LOGGER.info(
                "High-water mark advanced last={} current={}, rebuilding runId={} nodes={}",
                lastMarker.orElse("<none>"),
                currentMarker,
                runId,
                nodes.size());

        // Zero-downtime (T14): build into the IDLE index; the live index keeps serving.
        String target = indexManager.beginRebuild();
        List<String> nodeIds = nodes.stream().map(EnumeratedNode::nodeId).toList();
        LOGGER.info("Rebuild node set runId={} target={} nodeIds={}", runId, target, nodeIds);

        List<WorkItemResponse> responses = dispatcher.dispatch(
                runId, target, currentMarker, nodeIds, workItemSize);

        long expected = nodeIds.size();
        long totalFragments = responses.stream().mapToLong(WorkItemResponse::fragmentsIndexed).sum();
        long totalSkipped = responses.stream().mapToLong(r -> r.skippedNodeIds().size()).sum();
        LOGGER.info(
                "Rebuild work complete runId={} target={} expectedNodes={} fragmentsIndexed={} nodesSkipped={}",
                runId,
                target,
                expected,
                totalFragments,
                totalSkipped);

        // FLIP GATE (R10): a rebuild is all-or-nothing. Every change rewrites the entire corpus
        // from the ground up, so the freshly-built index only replaces the live one when EVERY
        // node was rebuilt successfully — 100% completion, not "at least one document." Any
        // skipped or unprocessed node (after the Processor's per-node retries) means the build is
        // partial, so we decline to promote and leave the current live corpus serving. This is
        // what stops a systemic failure (auth/connection/throttle) from promoting a near-empty or
        // fragmentary index over a complete one.
        if (totalSkipped > 0 || totalFragments < expected) {
            LOGGER.warn(
                    "Refusing to promote target={} runId={}: incomplete rebuild (expectedNodes={} "
                            + "fragmentsIndexed={} nodesSkipped={}). Live index unchanged; marker NOT "
                            + "advanced. A rebuild requires every node to succeed.",
                    target, runId, expected, totalFragments, totalSkipped);
            return RunOutcome.noOp(runId, lastMarker.orElse(""));
        }

        // Completeness is confirmed at the work layer above; now confirm every document is
        // actually queryable before flipping. AOSS Serverless has a ~10s refresh interval (a
        // freshly-created index's first refresh is ~64s), so just-written docs are not
        // immediately counted — poll until the full expected count is searchable so the gate
        // reflects the real build, not a refresh race.
        long built = waitForBuiltDocs(target, expected, runId);
        if (built < expected) {
            LOGGER.warn(
                    "Refusing to promote target={} runId={}: read-back count={} did not reach "
                            + "expected={} within the wait budget. Live index unchanged; marker NOT advanced.",
                    target, runId, built, expected);
            return RunOutcome.noOp(runId, lastMarker.orElse(""));
        }

        // Atomic promote: flip the live pointer to the freshly-built index (zero downtime).
        indexManager.promote(target);

        // Marker write follows a successful promote so a multi-update day is captured in one pass.
        markerStore.write(currentMarker);
        LOGGER.info("High-water mark written ({}) runId={} live={}", currentMarker, runId, target);

        // Post-promote confirmation (soft): the now-live index is queryable.
        LOGGER.info("Promoted live index {} holds {} document(s) runId={}", target, built, runId);

        return RunOutcome.rebuilt(runId, currentMarker, totalFragments, totalSkipped);
    }

    /**
     * Poll the freshly-built target's document count until it reaches the expected node count,
     * tolerating AOSS Serverless's ~10s refresh interval (just-written docs are not immediately
     * searchable). Returns the first count that meets or exceeds {@code expected}, or the last
     * observed count (likely below {@code expected}) if it never gets there within the budget —
     * in which case the flip gate declines to promote.
     *
     * @param indexName the freshly-built target index.
     * @param expected  the number of documents a complete build must hold (one per node).
     * @param runId     correlation id for logging.
     * @return a count {@code >= expected}, or the last observed (lower) count if never reached.
     */
    private long waitForBuiltDocs(String indexName, long expected, String runId) {
        long last = 0L;
        for (int attempt = 0; attempt < READBACK_MAX_ATTEMPTS; attempt++) {
            last = indexManager.readBackCount(indexName);
            if (last >= expected) {
                if (attempt > 0) {
                    LOGGER.info(
                            "Read-back for target={} reached {} doc(s) (expected {}) after {} attempt(s) runId={}",
                            indexName, last, expected, attempt + 1, runId);
                }
                return last;
            }
            try {
                Thread.sleep(READBACK_POLL_MILLIS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
        }
        return last;
    }

    /**
     * Outcome of a single rebuild run, surfaced back to the Poller handler for logging.
     *
     * @param kind             which terminal branch the run took.
     * @param runId            correlation ID.
     * @param snapshotMarker   high-water mark observed/written this run.
     * @param fragmentsIndexed total fragments written (non-zero only when REBUILT).
     * @param nodesSkipped     total nodes skipped (REBUILT only).
     */
    public record RunOutcome(
            Kind kind,
            String runId,
            String snapshotMarker,
            long fragmentsIndexed,
            long nodesSkipped) {

        /** Terminal branches a run can take. */
        public enum Kind {
            /** High-water mark unchanged (or empty enumeration); no rebuild performed. */
            NO_OP,
            /** Index cleared and rebuilt; marker advanced. */
            REBUILT
        }

        /**
         * Build a no-op outcome.
         *
         * @param runId  correlation ID.
         * @param marker marker observed at run start.
         * @return NO_OP outcome.
         */
        public static RunOutcome noOp(String runId, String marker) {
            return new RunOutcome(Kind.NO_OP, runId, marker, 0L, 0L);
        }

        /**
         * Build a rebuilt outcome.
         *
         * @param runId     correlation ID.
         * @param marker    marker written this run.
         * @param fragments total fragments written.
         * @param skipped   total nodes skipped.
         * @return REBUILT outcome.
         */
        public static RunOutcome rebuilt(String runId, String marker, long fragments, long skipped) {
            return new RunOutcome(Kind.REBUILT, runId, marker, fragments, skipped);
        }
    }
}

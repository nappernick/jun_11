package com.amazon.ingestion.pipeline;

import com.amazon.ingestion.contract.WorkItemResponse;
import com.amazon.ingestion.dispatch.WorkItemDispatcher;
import com.amazon.ingestion.enumeration.CoreXEnumerator;
import com.amazon.ingestion.enumeration.EnumeratedNode;
import com.amazon.ingestion.indexing.IndexManager;
import com.amazon.ingestion.snapshot.SnapshotMarkerStore;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Zero-downtime rebuild behavior (T14): build into the idle index, gate the flip on a
 * non-empty verified build, and only then promote + advance the marker.
 */
public class RebuildCoordinatorTest {

    @Test
    public void rebuildBuildsIdleThenPromotesAndWritesMarker() {
        FakeIndexManager index = new FakeIndexManager("faq_evidence_a", "faq_evidence_b", null);
        FakeMarkerStore marker = new FakeMarkerStore(null);
        FakeEnumerator corex = new FakeEnumerator(
                List.of(new EnumeratedNode("n1", "2026-05-01T00:00:00Z"),
                        new EnumeratedNode("n2", "2026-05-22T00:00:00Z")),
                "2026-05-22T00:00:00Z");
        FakeDispatcher dispatcher = new FakeDispatcher(2L); // build writes 2 docs

        RebuildCoordinator.RunOutcome outcome =
                new RebuildCoordinator(marker, corex, index, dispatcher).run();

        assertEquals(RebuildCoordinator.RunOutcome.Kind.REBUILT, outcome.kind());
        // First run with no live pointer builds into _a and promotes it.
        assertEquals("faq_evidence_a", index.promoted);
        assertEquals("faq_evidence_a", dispatcher.dispatchedIndex);
        assertEquals("2026-05-22T00:00:00Z", marker.written);
    }

    @Test
    public void secondRebuildTargetsTheOtherIndex() {
        // Live is _a; the next rebuild must build into _b and promote it.
        FakeIndexManager index = new FakeIndexManager("faq_evidence_a", "faq_evidence_b", "faq_evidence_a");
        FakeMarkerStore marker = new FakeMarkerStore("2026-05-01T00:00:00Z");
        FakeEnumerator corex = new FakeEnumerator(
                List.of(new EnumeratedNode("n1", "2026-06-01T00:00:00Z")), "2026-06-01T00:00:00Z");
        FakeDispatcher dispatcher = new FakeDispatcher(1L);

        new RebuildCoordinator(marker, corex, index, dispatcher).run();

        assertEquals("faq_evidence_b", dispatcher.dispatchedIndex);
        assertEquals("faq_evidence_b", index.promoted);
    }

    @Test
    public void emptyBuildIsNotPromotedAndMarkerNotAdvanced() {
        // FLIP GATE: a build that wrote zero documents must NOT replace the live corpus.
        FakeIndexManager index = new FakeIndexManager("faq_evidence_a", "faq_evidence_b", "faq_evidence_a");
        index.readBackOverride = 0L; // freshly-built target is empty
        FakeMarkerStore marker = new FakeMarkerStore("2026-05-01T00:00:00Z");
        FakeEnumerator corex = new FakeEnumerator(
                List.of(new EnumeratedNode("n1", "2026-06-01T00:00:00Z")), "2026-06-01T00:00:00Z");
        FakeDispatcher dispatcher = new FakeDispatcher(0L); // every node skipped

        RebuildCoordinator.RunOutcome outcome =
                new RebuildCoordinator(marker, corex, index, dispatcher).run();

        assertEquals(RebuildCoordinator.RunOutcome.Kind.NO_OP, outcome.kind());
        assertNull(index.promoted, "must not promote an empty build");
        assertNull(marker.written, "must not advance marker when not promoting");
    }

    @Test
    public void partialBuildWithSkippedNodesIsNotPromoted() {
        // 100% GATE: if any node is skipped (even after the Processor's per-node retries) the
        // rebuild is incomplete and must NOT replace the live corpus.
        FakeIndexManager index = new FakeIndexManager("faq_evidence_a", "faq_evidence_b", "faq_evidence_a");
        FakeMarkerStore marker = new FakeMarkerStore("2026-05-01T00:00:00Z");
        FakeEnumerator corex = new FakeEnumerator(
                List.of(new EnumeratedNode("n1", "2026-06-01T00:00:00Z"),
                        new EnumeratedNode("n2", "2026-06-02T00:00:00Z")),
                "2026-06-02T00:00:00Z");
        // One of the two nodes was skipped after retries; only one fragment written.
        FakeDispatcher dispatcher = new FakeDispatcher(1L, List.of("n2"));

        RebuildCoordinator.RunOutcome outcome =
                new RebuildCoordinator(marker, corex, index, dispatcher).run();

        assertEquals(RebuildCoordinator.RunOutcome.Kind.NO_OP, outcome.kind());
        assertNull(index.promoted, "must not promote a partial build");
        assertNull(marker.written, "must not advance marker for a partial build");
    }

    @Test
    public void unchangedMarkerIsNoOpWithoutBuilding() {
        FakeIndexManager index = new FakeIndexManager("faq_evidence_a", "faq_evidence_b", "faq_evidence_a");
        FakeMarkerStore marker = new FakeMarkerStore("2026-05-22T00:00:00Z");
        FakeEnumerator corex = new FakeEnumerator(
                List.of(new EnumeratedNode("n1", "2026-05-22T00:00:00Z")), "2026-05-22T00:00:00Z");
        FakeDispatcher dispatcher = new FakeDispatcher(1L);

        RebuildCoordinator.RunOutcome outcome =
                new RebuildCoordinator(marker, corex, index, dispatcher).run();

        assertEquals(RebuildCoordinator.RunOutcome.Kind.NO_OP, outcome.kind());
        assertFalse(index.beganRebuild, "unchanged marker must not begin a rebuild");
        assertNull(index.promoted);
        assertNull(marker.written);
    }

    @Test
    public void emptyEnumerationLeavesLiveIntact() {
        FakeIndexManager index = new FakeIndexManager("faq_evidence_a", "faq_evidence_b", "faq_evidence_a");
        FakeMarkerStore marker = new FakeMarkerStore("2026-05-22T00:00:00Z");
        FakeEnumerator corex = new FakeEnumerator(List.of(), "");
        FakeDispatcher dispatcher = new FakeDispatcher(1L);

        RebuildCoordinator.RunOutcome outcome =
                new RebuildCoordinator(marker, corex, index, dispatcher).run();

        assertEquals(RebuildCoordinator.RunOutcome.Kind.NO_OP, outcome.kind());
        assertFalse(index.beganRebuild);
        assertNull(index.promoted);
    }

    // --- Fakes ---

    private static final class FakeIndexManager implements IndexManager {
        private final String idleFirst;
        private final String idleSecond;
        private String live;
        boolean beganRebuild;
        String promoted;
        Long readBackOverride;

        FakeIndexManager(String a, String b, String live) {
            this.idleFirst = a;
            this.idleSecond = b;
            this.live = live;
        }

        @Override
        public String beginRebuild() {
            beganRebuild = true;
            return idleFirst.equals(live) ? idleSecond : idleFirst;
        }

        @Override
        public void promote(String indexName) {
            promoted = indexName;
            live = indexName;
        }

        @Override
        public Optional<String> liveIndexName() {
            return Optional.ofNullable(live);
        }

        @Override
        public long readBackCount(String indexName) {
            return readBackOverride != null ? readBackOverride : 5L;
        }
    }

    private static final class FakeMarkerStore implements SnapshotMarkerStore {
        private final String existing;
        String written;

        FakeMarkerStore(String existing) {
            this.existing = existing;
        }

        @Override
        public Optional<String> read() {
            return Optional.ofNullable(existing);
        }

        @Override
        public void write(String marker) {
            written = marker;
        }
    }

    private static final class FakeEnumerator implements CoreXEnumerator {
        private final List<EnumeratedNode> nodes;
        private final String marker;

        FakeEnumerator(List<EnumeratedNode> nodes, String marker) {
            this.nodes = nodes;
            this.marker = marker;
        }

        @Override
        public List<EnumeratedNode> enumerate() {
            return nodes;
        }

        @Override
        public String computeMarker(List<EnumeratedNode> n) {
            return marker;
        }
    }

    private static final class FakeDispatcher implements WorkItemDispatcher {
        private final long fragmentsPerCall;
        private final List<String> skippedNodeIds;
        String dispatchedIndex;

        FakeDispatcher(long fragmentsPerCall) {
            this(fragmentsPerCall, List.of());
        }

        FakeDispatcher(long fragmentsPerCall, List<String> skippedNodeIds) {
            this.fragmentsPerCall = fragmentsPerCall;
            this.skippedNodeIds = List.copyOf(skippedNodeIds);
        }

        @Override
        public List<WorkItemResponse> dispatch(
                String runId, String indexName, String corpusVersion, List<String> nodeIds, int groupSize) {
            this.dispatchedIndex = indexName;
            List<WorkItemResponse> out = new ArrayList<>();
            out.add(WorkItemResponse.completed(runId, 0, fragmentsPerCall, skippedNodeIds));
            return out;
        }
    }
}

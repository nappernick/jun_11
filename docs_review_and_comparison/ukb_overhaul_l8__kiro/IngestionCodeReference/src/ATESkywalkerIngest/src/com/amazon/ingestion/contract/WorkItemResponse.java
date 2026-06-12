package com.amazon.ingestion.contract;

import java.util.List;

/**
 * Result the Processor returns to the Poller for a single work item.
 *
 * Failure-tolerant by design (R2): a work item never "fails the run." It reports how many
 * fragments it wrote and which nodes it skipped (empty text, fetch/embed/write fault). The
 * Poller aggregates counts and logs skips; it does not abort on any of them.
 *
 * A work item whose Processor invocation could not be completed at all (Lambda transport or
 * function error) is represented with {@link #invocationFailed}; that too is logged and the
 * run continues — there are no hard stops.
 *
 * @param runId           echoes WorkItemRequest.runId.
 * @param workItemIndex   echoes WorkItemRequest.workItemIndex.
 * @param fragmentsIndexed count of fragment documents successfully written to OpenSearch.
 * @param skippedNodeIds  node IDs that were skipped (per-node); empty when none were skipped.
 * @param note            human-readable detail (skip summary or invocation error); may be null.
 */
public record WorkItemResponse(
        String runId,
        int workItemIndex,
        long fragmentsIndexed,
        List<String> skippedNodeIds,
        String note) {

    public WorkItemResponse {
        if (runId == null || runId.isBlank()) {
            throw new IllegalArgumentException("runId must be non-blank");
        }
        skippedNodeIds = skippedNodeIds == null ? List.of() : List.copyOf(skippedNodeIds);
    }

    /**
     * Build a completed-work-item response.
     *
     * @param runId            echoes WorkItemRequest.runId.
     * @param workItemIndex    echoes WorkItemRequest.workItemIndex.
     * @param fragmentsIndexed count of fragment documents written to OpenSearch.
     * @param skippedNodeIds   node IDs skipped during processing; may be empty.
     * @return a completed response.
     */
    public static WorkItemResponse completed(
            String runId, int workItemIndex, long fragmentsIndexed, List<String> skippedNodeIds) {
        String note = skippedNodeIds == null || skippedNodeIds.isEmpty()
                ? null
                : "skipped " + skippedNodeIds.size() + " node(s)";
        return new WorkItemResponse(runId, workItemIndex, fragmentsIndexed, skippedNodeIds, note);
    }

    /**
     * Build a response for a work item whose Processor invocation could not be completed.
     * Logged by the Poller; does not abort the run.
     *
     * @param runId         echoes WorkItemRequest.runId.
     * @param workItemIndex echoes WorkItemRequest.workItemIndex.
     * @param nodeIds       node IDs that went unprocessed because the invocation failed.
     * @param errorMessage  human-readable invocation-failure detail.
     * @return a response recording the unprocessed nodes as skipped.
     */
    public static WorkItemResponse invocationFailed(
            String runId, int workItemIndex, List<String> nodeIds, String errorMessage) {
        return new WorkItemResponse(runId, workItemIndex, 0L, nodeIds, errorMessage);
    }
}

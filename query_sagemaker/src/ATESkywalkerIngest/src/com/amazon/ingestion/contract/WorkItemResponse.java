package com.amazon.ingestion.contract;

import java.util.List;

/**
 * Result the Processor returns to the Poller for a single work item.
 *
 * A work item either succeeds as a whole or fails as a whole. Per-document failures
 * escalate the whole work item to FAILURE per Section 03 §7 conservatism — any work
 * item failure aborts the run.
 *
 * @param runId           echoes WorkItemRequest.runId.
 * @param workItemIndex   echoes WorkItemRequest.workItemIndex.
 * @param status          overall work item outcome.
 * @param childrenIndexed count of child chunks successfully written to OpenSearch.
 * @param failedNodeIds   node IDs whose processing failed; empty on success.
 * @param errorMessage    human-readable failure summary; null on success.
 */
public record WorkItemResponse(
        String runId,
        int workItemIndex,
        Status status,
        long childrenIndexed,
        List<String> failedNodeIds,
        String errorMessage) {

    /** Overall outcome of one Processor invocation. */
    public enum Status {
        /** All node IDs processed; children written to the candidate index. */
        SUCCESS,
        /** One or more node IDs failed; the run as a whole must abort. */
        FAILURE
    }

    public WorkItemResponse {
        if (runId == null || runId.isBlank()) {
            throw new IllegalArgumentException("runId must be non-blank");
        }
        if (status == null) {
            throw new IllegalArgumentException("status must not be null");
        }
        failedNodeIds = failedNodeIds == null ? List.of() : List.copyOf(failedNodeIds);
    }

    /**
     * Build a successful response.
     *
     * @param runId           echoes WorkItemRequest.runId.
     * @param workItemIndex   echoes WorkItemRequest.workItemIndex.
     * @param childrenIndexed count of child chunks written to OpenSearch.
     * @return a SUCCESS response.
     */
    public static WorkItemResponse success(String runId, int workItemIndex, long childrenIndexed) {
        return new WorkItemResponse(runId, workItemIndex, Status.SUCCESS, childrenIndexed, List.of(), null);
    }

    /**
     * Build a failure response.
     *
     * @param runId         echoes WorkItemRequest.runId.
     * @param workItemIndex echoes WorkItemRequest.workItemIndex.
     * @param failedNodeIds node IDs whose processing failed; must be non-empty.
     * @param errorMessage  human-readable failure summary.
     * @return a FAILURE response.
     */
    public static WorkItemResponse failure(
            String runId, int workItemIndex, List<String> failedNodeIds, String errorMessage) {
        return new WorkItemResponse(runId, workItemIndex, Status.FAILURE, 0L, failedNodeIds, errorMessage);
    }
}

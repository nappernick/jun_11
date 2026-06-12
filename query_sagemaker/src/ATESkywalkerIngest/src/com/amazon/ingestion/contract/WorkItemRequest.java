package com.amazon.ingestion.contract;

import java.util.List;

/**
 * Payload the Poller sends to each Processor invocation.
 *
 * Carries node IDs only — never document bodies. The Processor performs its own CoreX
 * fetch so we do not move content bytes across the Lambda boundary.
 *
 * CoreX's getContentNode GraphQL call takes one node ID at a time. A work item is a
 * grouping of node IDs that one Processor invocation is responsible for; the Processor
 * loops and makes one CoreX call per ID. The grouping controls parallelism at the Poller
 * boundary, not at the CoreX boundary.
 *
 * @param runId              correlation ID for the whole rebuild run, shared by all work items.
 * @param workItemIndex      zero-based index of this work item within the run.
 * @param workItemCount      total work items dispatched in this run.
 * @param candidateIndexName OpenSearch index the Processor must write into
 *                           (e.g. faq_evidence_v42). Created by the Poller before fan-out.
 * @param nodeIds            CoreX node IDs this work item is responsible for.
 */
public record WorkItemRequest(
        String runId,
        int workItemIndex,
        int workItemCount,
        String candidateIndexName,
        List<String> nodeIds) {

    public WorkItemRequest {
        if (runId == null || runId.isBlank()) {
            throw new IllegalArgumentException("runId must be non-blank");
        }
        if (candidateIndexName == null || candidateIndexName.isBlank()) {
            throw new IllegalArgumentException("candidateIndexName must be non-blank");
        }
        if (nodeIds == null || nodeIds.isEmpty()) {
            throw new IllegalArgumentException("nodeIds must be non-empty");
        }
        nodeIds = List.copyOf(nodeIds);
    }
}

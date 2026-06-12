package com.amazon.ingestion.contract;

import java.util.List;

/**
 * Payload the Poller sends to each Processor invocation.
 *
 * Carries node IDs only — never document bodies. The Processor performs its own COREx
 * fetch so we do not move content bytes across the Lambda boundary.
 *
 * COREx's getContentNode GraphQL call takes one node ID at a time. A work item is a
 * grouping of node IDs that one Processor invocation is responsible for; the Processor
 * loops and makes one COREx call per ID, processing each item independently and skipping
 * any that fail (R2). The grouping controls parallelism at the Poller boundary.
 *
 * @param runId         correlation ID for the whole rebuild run, shared by all work items.
 * @param workItemIndex zero-based index of this work item within the run.
 * @param workItemCount total work items dispatched in this run.
 * @param indexName     OpenSearch index the Processor must write into (the fixed
 *                      {@code faq_evidence} index; overwrite-in-place, no versioning).
 * @param corpusVersion the run's snapshot date marker, stamped on every fragment written.
 * @param source        optional content-source hint: {@code "corex"} (default/absent, the
 *                      production path) or {@code "exemplar"} (serve bundled real-prod records
 *                      for a local write proof, since COREx has no write API). The Poller never
 *                      sets this, so scheduled production runs always use COREx.
 * @param nodeIds       COREx node IDs this work item is responsible for.
 */
public record WorkItemRequest(
        String runId,
        int workItemIndex,
        int workItemCount,
        String indexName,
        String corpusVersion,
        String source,
        List<String> nodeIds) {

    public WorkItemRequest {
        if (runId == null || runId.isBlank()) {
            throw new IllegalArgumentException("runId must be non-blank");
        }
        if (indexName == null || indexName.isBlank()) {
            throw new IllegalArgumentException("indexName must be non-blank");
        }
        if (nodeIds == null || nodeIds.isEmpty()) {
            throw new IllegalArgumentException("nodeIds must be non-empty");
        }
        corpusVersion = corpusVersion == null ? "" : corpusVersion;
        source = source == null || source.isBlank() ? "corex" : source;
        nodeIds = List.copyOf(nodeIds);
    }
}

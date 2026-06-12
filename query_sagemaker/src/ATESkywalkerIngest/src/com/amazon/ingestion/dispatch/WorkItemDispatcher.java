package com.amazon.ingestion.dispatch;

import com.amazon.ingestion.contract.WorkItemResponse;
import com.amazon.ingestion.contract.WorkItemRequest;

import java.util.ArrayList;
import java.util.List;

/**
 * Fan-out surface that invokes the Processor Lambda for each work item and collects results.
 *
 * Launch implementation is synchronous parallel InvokeFunction calls. The Poller blocks
 * until all work items complete or one fails hard enough to abort the run.
 */
public interface WorkItemDispatcher {

    /**
     * Group node IDs into work items, invoke the Processor for each, and return all responses.
     *
     * @param runId          correlation ID for this rebuild run.
     * @param candidateIndex candidate index name the Processor must write into.
     * @param nodeIds        full set of node IDs to dispatch across work items.
     * @param groupSize      max node IDs per work item.
     * @return one response per work item; ordering is not guaranteed.
     */
    List<WorkItemResponse> dispatch(
            String runId, String candidateIndex, List<String> nodeIds, int groupSize);

    /**
     * Default grouping: contiguous slices of size groupSize.
     *
     * @param runId          correlation ID for this rebuild run.
     * @param candidateIndex candidate index name the Processor must write into.
     * @param nodeIds        full set of node IDs to partition.
     * @param groupSize      max node IDs per work item.
     * @return work item requests covering every node ID exactly once.
     */
    default List<WorkItemRequest> groupIntoWorkItems(
            String runId, String candidateIndex, List<String> nodeIds, int groupSize) {
        if (groupSize < 1) {
            throw new IllegalArgumentException("groupSize must be >= 1");
        }
        int total = nodeIds.size();
        int count = (total + groupSize - 1) / groupSize;
        List<WorkItemRequest> items = new ArrayList<>(count);
        for (int i = 0; i < count; i++) {
            int from = i * groupSize;
            int to = Math.min(from + groupSize, total);
            items.add(new WorkItemRequest(runId, i, count, candidateIndex, nodeIds.subList(from, to)));
        }
        return List.copyOf(items);
    }
}

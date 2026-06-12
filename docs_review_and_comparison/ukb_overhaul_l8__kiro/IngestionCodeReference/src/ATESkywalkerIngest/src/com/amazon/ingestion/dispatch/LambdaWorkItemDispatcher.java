package com.amazon.ingestion.dispatch;

import com.amazon.ingestion.contract.WorkItemRequest;
import com.amazon.ingestion.contract.WorkItemResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.services.lambda.LambdaClient;
import software.amazon.awssdk.services.lambda.model.InvocationType;
import software.amazon.awssdk.services.lambda.model.InvokeRequest;

import java.util.ArrayList;
import java.util.List;

/**
 * Synchronously invokes the Processor Lambda for each grouped work item.
 */
public final class LambdaWorkItemDispatcher implements WorkItemDispatcher {

    private static final Logger LOGGER = LogManager.getLogger(LambdaWorkItemDispatcher.class);
    private static final ObjectMapper JSON = new ObjectMapper();

    private final LambdaClient lambda;
    private final String processorFunctionName;

    public LambdaWorkItemDispatcher(LambdaClient lambda, String processorFunctionName) {
        this.lambda = lambda;
        if (processorFunctionName == null || processorFunctionName.isBlank()) {
            throw new IllegalArgumentException("processorFunctionName must be non-blank");
        }
        this.processorFunctionName = processorFunctionName;
    }

    @Override
    public List<WorkItemResponse> dispatch(
            String runId, String indexName, String corpusVersion, List<String> nodeIds, int groupSize) {
        List<WorkItemRequest> items = groupIntoWorkItems(runId, indexName, corpusVersion, nodeIds, groupSize);
        List<WorkItemResponse> responses = new ArrayList<>(items.size());
        for (WorkItemRequest item : items) {
            responses.add(invoke(item));
        }
        return List.copyOf(responses);
    }

    private WorkItemResponse invoke(WorkItemRequest item) {
        try {
            byte[] payload = JSON.writeValueAsBytes(item);
            LOGGER.info(
                    "Invoking Processor {} for runId={} workItem={}/{} nodes={}",
                    processorFunctionName,
                    item.runId(),
                    item.workItemIndex(),
                    item.workItemCount(),
                    item.nodeIds().size());

            var response = lambda.invoke(InvokeRequest.builder()
                    .functionName(processorFunctionName)
                    .invocationType(InvocationType.REQUEST_RESPONSE)
                    .payload(SdkBytes.fromByteArray(payload))
                    .build());

            if (response.functionError() != null && !response.functionError().isBlank()) {
                LOGGER.warn(
                        "Processor reported functionError for runId={} workItem={}: {} (run continues, R2)",
                        item.runId(),
                        item.workItemIndex(),
                        response.functionError());
                return WorkItemResponse.invocationFailed(
                        item.runId(),
                        item.workItemIndex(),
                        item.nodeIds(),
                        "Processor function error: " + response.functionError());
            }
            return JSON.readValue(response.payload().asByteArray(), WorkItemResponse.class);
        } catch (Exception e) {
            LOGGER.warn(
                    "Processor invocation failed for runId={} workItem={}: {} (run continues, R2)",
                    item.runId(),
                    item.workItemIndex(),
                    e.getMessage());
            return WorkItemResponse.invocationFailed(
                    item.runId(),
                    item.workItemIndex(),
                    item.nodeIds(),
                    "Processor invocation failed: " + e.getMessage());
        }
    }
}

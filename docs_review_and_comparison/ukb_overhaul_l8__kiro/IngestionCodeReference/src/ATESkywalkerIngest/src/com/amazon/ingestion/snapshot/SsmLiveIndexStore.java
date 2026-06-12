package com.amazon.ingestion.snapshot;

import software.amazon.awssdk.services.ssm.SsmClient;
import software.amazon.awssdk.services.ssm.model.GetParameterRequest;
import software.amazon.awssdk.services.ssm.model.ParameterNotFoundException;
import software.amazon.awssdk.services.ssm.model.ParameterType;
import software.amazon.awssdk.services.ssm.model.PutParameterRequest;

import java.util.Optional;

/**
 * SSM-backed {@link LiveIndexStore}: one parameter holding the name of the currently-live FAQ
 * evidence index (T14), e.g. {@code /skywalker/ingestion/faq_evidence/live_index}.
 *
 * This is the alias substitute for zero-downtime rebuilds (AOSS Serverless does not support
 * index aliases). The rebuild writes into the idle index, then {@link #write(String)} flips
 * this pointer atomically. The query side reads it to resolve which physical index to query.
 *
 * Absent on first run; {@link #read()} returns empty and the coordinator bootstraps by picking
 * a default physical index.
 */
public final class SsmLiveIndexStore implements LiveIndexStore {

    private final SsmClient ssm;
    private final String parameterName;

    public SsmLiveIndexStore(SsmClient ssm, String parameterName) {
        this.ssm = ssm;
        this.parameterName = parameterName;
    }

    @Override
    public Optional<String> read() {
        try {
            String value = ssm.getParameter(GetParameterRequest.builder()
                    .name(parameterName)
                    .build())
                    .parameter()
                    .value();
            return Optional.ofNullable(value).filter(v -> !v.isBlank());
        } catch (ParameterNotFoundException e) {
            return Optional.empty();
        }
    }

    @Override
    public void write(String indexName) {
        ssm.putParameter(PutParameterRequest.builder()
                .name(parameterName)
                .value(indexName)
                .type(ParameterType.STRING)
                .overwrite(true)
                .build());
    }
}

package com.amazon.ingestion.snapshot;

import software.amazon.awssdk.services.ssm.SsmClient;
import software.amazon.awssdk.services.ssm.model.GetParameterRequest;
import software.amazon.awssdk.services.ssm.model.ParameterNotFoundException;
import software.amazon.awssdk.services.ssm.model.ParameterType;
import software.amazon.awssdk.services.ssm.model.PutParameterRequest;

import java.util.Optional;

/**
 * SSM-backed store for the last-content-update-timestamp watermark.
 *
 * The parameter is absent on first run. Read returns empty in that case and the
 * coordinator treats it as "no prior snapshot, rebuild from scratch."
 *
 * Write failures propagate as RuntimeException. The coordinator orders the write
 * after a successful alias swap; if the write fails the next run will observe
 * a CoreX marker mismatch and rebuild harmlessly.
 */
public final class SsmSnapshotMarkerStore implements SnapshotMarkerStore {

    private final SsmClient ssm;
    private final String parameterName;

    public SsmSnapshotMarkerStore(SsmClient ssm, String parameterName) {
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
            return Optional.ofNullable(value);
        } catch (ParameterNotFoundException e) {
            return Optional.empty();
        }
    }

    @Override
    public void write(String marker) {
        ssm.putParameter(PutParameterRequest.builder()
                .name(parameterName)
                .value(marker)
                .type(ParameterType.STRING)
                .overwrite(true)
                .build());
    }
}

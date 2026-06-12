package com.amazon.ingestion.heartbeat;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import software.amazon.awssdk.services.ssm.SsmClient;
import software.amazon.awssdk.services.ssm.model.ParameterType;
import software.amazon.awssdk.services.ssm.model.PutParameterRequest;

/**
 * SSM-backed heartbeat. Writes on every run.
 *
 * Parameter name comes from the environment: SSM_LAST_POLL_TIMESTAMP.
 * A write failure is logged but does not abort the run — the heartbeat is informational.
 */
public final class SsmPollHeartbeat implements PollHeartbeat {

    private static final Logger LOGGER = LogManager.getLogger(SsmPollHeartbeat.class);

    private final SsmClient ssm;
    private final String parameterName;

    public SsmPollHeartbeat(SsmClient ssm, String parameterName) {
        this.ssm = ssm;
        this.parameterName = parameterName;
    }

    @Override
    public void record(String isoTimestamp) {
        try {
            ssm.putParameter(PutParameterRequest.builder()
                    .name(parameterName)
                    .value(isoTimestamp)
                    .type(ParameterType.STRING)
                    .overwrite(true)
                    .build());
            LOGGER.debug("Heartbeat recorded {}={}", parameterName, isoTimestamp);
        } catch (RuntimeException e) {
            LOGGER.warn("Failed to write heartbeat to {}: {}", parameterName, e.getMessage());
        }
    }
}

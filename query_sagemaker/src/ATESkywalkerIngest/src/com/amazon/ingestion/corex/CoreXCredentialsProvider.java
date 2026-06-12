package com.amazon.ingestion.corex;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import software.amazon.awssdk.auth.credentials.AwsSessionCredentials;
import software.amazon.awssdk.services.sts.StsClient;
import software.amazon.awssdk.services.sts.model.AssumeRoleRequest;
import software.amazon.awssdk.services.sts.model.AssumeRoleResponse;

import java.time.Duration;
import java.time.Instant;

/**
 * Manages the cross-account AssumeRole cycle for COREx API access.
 *
 * Caches the assumed credentials and returns them until they are within a 30-second
 * refresh buffer of expiration, then assumes the role again. Thread-safe via a
 * synchronized refresh block; contention is expected to be negligible because a
 * Lambda instance is single-threaded during a given invocation.
 */
public final class CoreXCredentialsProvider {

    private static final Logger LOGGER = LogManager.getLogger(CoreXCredentialsProvider.class);

    private static final Duration REFRESH_BUFFER = Duration.ofSeconds(30);

    private final StsClient sts;
    private final String roleArn;
    private final String roleSessionName;
    private final CoreXSecretReader secretReader;

    private volatile AwsSessionCredentials cachedCredentials;
    private volatile Instant cachedExpiration = Instant.EPOCH;

    public CoreXCredentialsProvider(
            StsClient sts,
            String roleArn,
            String roleSessionName,
            CoreXSecretReader secretReader) {
        this.sts = sts;
        this.roleArn = roleArn;
        this.roleSessionName = roleSessionName;
        this.secretReader = secretReader;
    }

    /**
     * Return valid temporary credentials for COREx.
     *
     * @return session credentials from the most recent successful AssumeRole call.
     */
    public AwsSessionCredentials credentials() {
        if (!isFresh()) {
            refresh();
        }
        return cachedCredentials;
    }

    private synchronized void refresh() {
        if (isFresh()) {
            return;
        }
        CoreXSecret secret = secretReader.read();
        AssumeRoleResponse response = sts.assumeRole(AssumeRoleRequest.builder()
                .roleArn(roleArn)
                .roleSessionName(roleSessionName)
                .externalId(secret.externalId())
                .build());
        this.cachedCredentials = AwsSessionCredentials.create(
                response.credentials().accessKeyId(),
                response.credentials().secretAccessKey(),
                response.credentials().sessionToken());
        this.cachedExpiration = response.credentials().expiration();
        LOGGER.info(
                "Assumed COREx role {} as session {}, credentials expire at {}",
                roleArn,
                roleSessionName,
                cachedExpiration);
    }

    private boolean isFresh() {
        return cachedCredentials != null
                && Instant.now().plus(REFRESH_BUFFER).isBefore(cachedExpiration);
    }
}

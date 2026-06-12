package com.amazon.ingestion.corex;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import software.amazon.awssdk.services.secretsmanager.SecretsManagerClient;
import software.amazon.awssdk.services.secretsmanager.model.GetSecretValueRequest;

/**
 * Reads the COREx secret from Secrets Manager.
 *
 * The secret name is environment-specific and injected at construction time
 * (env var COREX_SECRET_NAME in the Lambda config). One read per Lambda cold start
 * is fine; the returned value is stable until the operator rotates it.
 */
public final class CoreXSecretReader {

    private static final ObjectMapper JSON = new ObjectMapper();

    private final SecretsManagerClient secretsManager;
    private final String secretName;

    public CoreXSecretReader(SecretsManagerClient secretsManager, String secretName) {
        this.secretsManager = secretsManager;
        this.secretName = secretName;
    }

    /**
     * Read the current value of the secret.
     *
     * @return parsed apiKey and externalId.
     * @throws RuntimeException if the secret is missing, empty, or does not contain the expected fields.
     */
    public CoreXSecret read() {
        String payload = secretsManager.getSecretValue(GetSecretValueRequest.builder()
                .secretId(secretName)
                .build())
                .secretString();
        if (payload == null || payload.isBlank()) {
            throw new IllegalStateException("COREx secret " + secretName + " is empty");
        }
        try {
            JsonNode root = JSON.readTree(payload);
            JsonNode apiKey = root.get("ApiKey");
            JsonNode externalId = root.get("ExternalId");
            if (apiKey == null || externalId == null) {
                throw new IllegalStateException(
                        "COREx secret " + secretName + " is missing ApiKey or ExternalId");
            }
            return new CoreXSecret(apiKey.asText(), externalId.asText());
        } catch (RuntimeException e) {
            throw e;
        } catch (Exception e) {
            throw new IllegalStateException("Failed to parse COREx secret " + secretName, e);
        }
    }
}

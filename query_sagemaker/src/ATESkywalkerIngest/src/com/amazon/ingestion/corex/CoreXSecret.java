package com.amazon.ingestion.corex;

/**
 * COREx authentication material retrieved from Secrets Manager.
 *
 * The secret stored at ATESkywalkerIngest/&lt;stage&gt;/corex has this shape:
 * { "ApiKey": "...", "ExternalId": "..." }.
 *
 * @param apiKey     API key sent in the x-api-key header on every COREx request.
 * @param externalId external ID required when assuming the COREx cross-account role.
 */
public record CoreXSecret(String apiKey, String externalId) {

    public CoreXSecret {
        if (apiKey == null || apiKey.isBlank()) {
            throw new IllegalArgumentException("apiKey must be non-blank");
        }
        if (externalId == null || externalId.isBlank()) {
            throw new IllegalArgumentException("externalId must be non-blank");
        }
    }
}

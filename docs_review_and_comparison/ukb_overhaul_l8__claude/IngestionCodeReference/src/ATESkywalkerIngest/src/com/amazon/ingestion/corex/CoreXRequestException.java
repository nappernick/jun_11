package com.amazon.ingestion.corex;

/**
 * Thrown when a COREx request fails at the transport or protocol level.
 *
 * GraphQL-level errors (payload.status != SUCCESS) are surfaced as normal return values
 * by the adapters that call CoreXGraphQLClient and are not wrapped in this exception.
 */
public class CoreXRequestException extends RuntimeException {

    public CoreXRequestException(String message) {
        super(message);
    }

    public CoreXRequestException(String message, Throwable cause) {
        super(message, cause);
    }
}

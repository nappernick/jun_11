package com.amazon.ateskywalkerquery.rerank;

/** Thrown when a Bedrock rerank call fails. */
public class RerankException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public RerankException(String message) {
        super(message);
    }

    public RerankException(String message, Throwable cause) {
        super(message, cause);
    }
}

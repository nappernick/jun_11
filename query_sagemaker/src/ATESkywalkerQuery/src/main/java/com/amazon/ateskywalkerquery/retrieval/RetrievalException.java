package com.amazon.ateskywalkerquery.retrieval;

/** Thrown when hybrid retrieval against AOSS fails. */
public class RetrievalException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public RetrievalException(String message) {
        super(message);
    }

    public RetrievalException(String message, Throwable cause) {
        super(message, cause);
    }
}

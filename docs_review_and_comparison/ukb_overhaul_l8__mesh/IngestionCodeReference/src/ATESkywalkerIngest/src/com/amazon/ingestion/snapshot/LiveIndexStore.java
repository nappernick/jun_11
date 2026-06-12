package com.amazon.ingestion.snapshot;

import java.util.Optional;

/**
 * Persists the name of the currently-live (queryable) FAQ evidence index (T14).
 *
 * Zero-downtime rebuilds use two physical indices and this single pointer: the rebuild writes
 * into the idle index, then atomically flips this pointer to it. Readers (the query service)
 * resolve the live index name from this pointer rather than hard-coding one, so a rebuild never
 * exposes a partial/empty index. AOSS Serverless does not support index aliases (confirmed:
 * {@code PUT /index/_alias} returns 403 and no {@code aoss:*} permission maps to alias actions),
 * so this SSM pointer is the alias substitute.
 */
public interface LiveIndexStore {

    /**
     * Read the current live index name.
     *
     * @return the live index name, or empty if the pointer has never been set (first run).
     */
    Optional<String> read();

    /**
     * Atomically set the live index name (the rebuild's promote/flip step).
     *
     * @param indexName the freshly-built index to promote to live.
     */
    void write(String indexName);
}

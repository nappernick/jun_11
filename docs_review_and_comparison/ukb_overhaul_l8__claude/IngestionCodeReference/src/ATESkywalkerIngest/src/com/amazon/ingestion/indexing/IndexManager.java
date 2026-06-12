package com.amazon.ingestion.indexing;

import java.util.Optional;

/**
 * OpenSearch index lifecycle for the FAQ evidence corpus, zero-downtime (T14).
 *
 * <h2>Two physical indices + an SSM pointer (no aliases)</h2>
 * AOSS Serverless does not support index aliases (confirmed: {@code PUT /index/_alias} → 403,
 * and no {@code aoss:*} permission maps to alias actions), so the classic build-new →
 * alias-flip → drop-old pattern is unavailable. Instead we keep two physical indices
 * ({@code <base>_a} / {@code <base>_b}) and a single SSM pointer naming the live one. A rebuild
 * always writes into the <b>idle</b> index, validates it, then atomically flips the pointer.
 * The live index serves queries uninterrupted for the whole rebuild — zero downtime — and a
 * failed/empty build never replaces a good corpus because the flip is gated on a non-empty,
 * verified target.
 *
 * <p>The Poller (rebuild side) drives these; the query service reads the same pointer to learn
 * which physical index to query. Processors write documents into the index name handed to them.
 */
public interface IndexManager {

    /**
     * Begin a rebuild: resolve the idle (non-live) physical index, (re)create it empty with the
     * correct FAISS mapping, ensure the hybrid search pipeline exists (best-effort, R8), and
     * return the idle index name to write the rebuild into. Does not touch the live index.
     *
     * @return the target (idle) index name to write this rebuild into.
     */
    String beginRebuild();

    /**
     * Atomically promote a freshly-built index to live by flipping the SSM pointer. After this,
     * readers resolve to the new index. The previously-live index is left in place to become
     * the idle target of the next rebuild.
     *
     * @param indexName the index to promote (must be the target from {@link #beginRebuild()}).
     */
    void promote(String indexName);

    /**
     * The currently-live index name, resolved from the pointer.
     *
     * @return the live index name, or empty before the first successful rebuild.
     */
    Optional<String> liveIndexName();

    /**
     * Read-back canary (R6/R10): count documents visible in a specific index. Used on the
     * freshly-built target before promotion (the flip gate) and for post-promote confirmation.
     * Soft — returns -1 on failure rather than throwing.
     *
     * @param indexName the index to count.
     * @return the document count, or -1 if it could not be obtained.
     */
    long readBackCount(String indexName);
}

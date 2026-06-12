package com.amazon.ateskywalkerquery.retrieval;

import java.util.List;

/**
 * A scoped hybrid-retrieval request: query text plus its embedding and the scope filter.
 *
 * @param queryText raw user query (BM25 leg)
 * @param embedding query embedding (kNN leg)
 * @param country scope filter value
 * @param level scope filter value
 * @param role scope filter value
 * @param size number of candidates to retrieve
 */
public record RetrievalRequest(
    String queryText, List<Float> embedding, String country, String level, String role, int size) {}

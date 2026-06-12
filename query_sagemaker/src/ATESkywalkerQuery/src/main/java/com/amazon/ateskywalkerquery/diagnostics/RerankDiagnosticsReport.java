package com.amazon.ateskywalkerquery.diagnostics;

/**
 * Immutable snapshot of reranker context-window usage for a single query-document batch.
 *
 * @param queryTokens estimated token count for the query
 * @param docCount number of documents in the batch
 * @param totalDocTokens sum of estimated tokens across all documents
 * @param maxDocTokens largest single-document token estimate (0 if empty)
 * @param avgDocTokens mean document token estimate (0.0 if empty)
 * @param maxPairTokens largest query+document pair token count
 * @param overflowCount number of documents whose pair exceeds the context window
 * @param estPayloadBytes estimated UTF-8 byte payload size
 */
public record RerankDiagnosticsReport(
    int queryTokens,
    int docCount,
    int totalDocTokens,
    int maxDocTokens,
    double avgDocTokens,
    int maxPairTokens,
    int overflowCount,
    long estPayloadBytes) {}

package com.amazon.ateskywalkerquery.diagnostics;

import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * Standalone analyzer that estimates reranker context-window usage without requiring configuration
 * or dependency injection.
 */
public final class RerankDiagnostics {

    private RerankDiagnostics() {}

    /**
     * Analyze a query and its candidate documents against a reranker context window.
     *
     * @param query the query text (null treated as empty)
     * @param documents list of document texts (null list treated as empty; null elements as empty)
     * @param contextWindowTokens maximum tokens the reranker context window supports
     * @param charsPerToken characters-per-token estimate used for token approximation
     * @return a diagnostic report summarizing context-window usage
     * @throws IllegalArgumentException if charsPerToken is zero or negative
     */
    public static RerankDiagnosticsReport analyze(
        String query, List<String> documents, int contextWindowTokens, double charsPerToken) {
        if (charsPerToken <= 0) {
            throw new IllegalArgumentException("charsPerToken must be positive, got: " + charsPerToken);
        }

        List<String> docs = documents == null ? List.of() : documents;
        String q = query == null ? "" : query;

        int queryTokens = estTokens(q, charsPerToken);
        int docCount = docs.size();
        int totalDocTokens = 0;
        int maxDocTokens = 0;
        int maxPairTokens = queryTokens;
        int overflowCount = 0;
        long estPayloadBytes = q.getBytes(StandardCharsets.UTF_8).length;

        for (String doc : docs) {
            String d = doc == null ? "" : doc;
            int docTokens = estTokens(d, charsPerToken);
            totalDocTokens += docTokens;
            if (docTokens > maxDocTokens) {
                maxDocTokens = docTokens;
            }
            int pairTokens = queryTokens + docTokens;
            if (pairTokens > maxPairTokens) {
                maxPairTokens = pairTokens;
            }
            if (pairTokens > contextWindowTokens) {
                overflowCount++;
            }
            estPayloadBytes += d.getBytes(StandardCharsets.UTF_8).length;
        }

        double avgDocTokens = docCount == 0 ? 0.0 : totalDocTokens / (double) docCount;

        return new RerankDiagnosticsReport(
            queryTokens,
            docCount,
            totalDocTokens,
            maxDocTokens,
            avgDocTokens,
            maxPairTokens,
            overflowCount,
            estPayloadBytes);
    }

    private static int estTokens(String text, double charsPerToken) {
        if (text.isEmpty()) {
            return 0;
        }
        return (int) Math.ceil(text.length() / charsPerToken);
    }
}

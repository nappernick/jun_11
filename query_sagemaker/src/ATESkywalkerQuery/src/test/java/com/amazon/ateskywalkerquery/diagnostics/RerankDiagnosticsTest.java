package com.amazon.ateskywalkerquery.diagnostics;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RerankDiagnosticsTest {

    @Test
    void shortQueryShortDocsNoOverflow() {
        String query = "hello world"; // 11 chars -> ceil(11/4.0) = 3 tokens
        List<String> docs = List.of("doc one", "doc two ab"); // 7 -> 2 tokens, 10 -> 3 tokens

        RerankDiagnosticsReport r = RerankDiagnostics.analyze(query, docs, 4096, 4.0);

        assertEquals(3, r.queryTokens());
        assertEquals(2, r.docCount());
        assertEquals(5, r.totalDocTokens()); // 2 + 3
        assertEquals(3, r.maxDocTokens());
        assertEquals(2.5, r.avgDocTokens());
        assertEquals(6, r.maxPairTokens()); // 3 + 3
        assertEquals(0, r.overflowCount());
        assertEquals(28L, r.estPayloadBytes()); // 11 + 7 + 10
    }

    @Test
    void longDocForcesOverflow() {
        String query = "q"; // 1 char -> ceil(1/4.0) = 1 token
        // Need query+doc > 4096 tokens at 4.0 chars/token -> doc > 4095*4 = 16380 chars
        String longDoc = "x".repeat(16384);

        RerankDiagnosticsReport r = RerankDiagnostics.analyze(query, List.of(longDoc), 4096, 4.0);

        assertTrue(r.overflowCount() >= 1);
        assertTrue(r.maxPairTokens() > 4096);
    }

    @Test
    void emptyDocumentList() {
        String query = "test"; // 4 chars -> ceil(4/4.0) = 1 token

        RerankDiagnosticsReport r = RerankDiagnostics.analyze(query, List.of(), 4096, 4.0);

        assertEquals(0, r.docCount());
        assertEquals(0.0, r.avgDocTokens());
        assertEquals(1, r.maxPairTokens()); // queryTokens only
    }

    @Test
    void charsPerTokenZeroThrows() {
        assertThrows(IllegalArgumentException.class, () -> RerankDiagnostics.analyze("q", List.of(), 4096, 0.0));
    }

    @Test
    void charsPerTokenNegativeThrows() {
        assertThrows(IllegalArgumentException.class, () -> RerankDiagnostics.analyze("q", List.of(), 4096, -1.0));
    }
}

package com.amazon.ingestion.corex;

import com.amazon.ingestion.processor.ScopeMapper;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

public class LocalExemplarContentFetcherTest {

    @Test
    public void loadsBundledRealProdRecordsAndMapsScope() {
        LocalExemplarContentFetcher fetcher = new LocalExemplarContentFetcher();
        List<String> ids = fetcher.nodeIds();
        assertFalse(ids.isEmpty(), "exemplar resource should contain records");

        ScopeMapper scope = new ScopeMapper();
        CoreXTextExtractor extractor = new CoreXTextExtractor();

        for (String id : ids) {
            CoreXContentNode node = fetcher.fetch(id);
            // Real text extracts to non-empty plain text (markdown stripped).
            assertFalse(extractor.extract(node).isBlank(), "extracted text for " + id);
            // All three scope dimensions resolve to non-empty real values (R10).
            assertFalse(scope.country(node).isEmpty(), "country for " + id);
            assertFalse(scope.level(node).isEmpty(), "level for " + id);
            assertFalse(scope.role(node).isEmpty(), "role for " + id);
        }
    }

    @Test
    public void firstExemplarIsGlobalAllAll() {
        LocalExemplarContentFetcher fetcher = new LocalExemplarContentFetcher();
        ScopeMapper scope = new ScopeMapper();
        CoreXContentNode node = fetcher.fetch(fetcher.nodeIds().get(0));
        assertEquals(List.of("Global"), scope.country(node));
        assertTrue(scope.level(node).contains("All Job Levels"));
        assertTrue(scope.role(node).contains("All Employee Classes"));
    }
}

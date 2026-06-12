package com.amazon.ingestion.schema;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

public class CorpusSchemaTest {

    @Test
    public void splitScopeParsesCommaJoinedRealValues() {
        // Real COREx prod shape: comma-joined strings.
        assertEquals(List.of("Global"), CorpusSchema.splitScope("Global"));
        assertEquals(
                List.of("L0", "L1", "L2", "L12"),
                CorpusSchema.splitScope("L0,L1,L2,L12"));
        assertEquals(
                List.of("China", "India", "Japan"),
                CorpusSchema.splitScope("China,India,Japan"));
    }

    @Test
    public void splitScopeStripsAemBlankPlaceholderAndWhitespace() {
        // "All Job Levels,No Value (Blank in AEM)" is real noise observed in the data.
        assertEquals(
                List.of("All Job Levels"),
                CorpusSchema.splitScope("All Job Levels,No Value (Blank in AEM)"));
        assertEquals(List.of("A", "B"), CorpusSchema.splitScope(" A , B , "));
    }

    @Test
    public void splitScopeOnBlankIsEmpty() {
        assertTrue(CorpusSchema.splitScope(null).isEmpty());
        assertTrue(CorpusSchema.splitScope("").isEmpty());
        assertTrue(CorpusSchema.splitScope("   ").isEmpty());
        assertTrue(CorpusSchema.splitScope(CorpusSchema.BLANK_IN_AEM).isEmpty());
    }

    @Test
    public void knownValueSetsContainEverybodyMarkers() {
        assertTrue(CorpusSchema.KNOWN_JOB_LEVELS.contains(CorpusSchema.JOB_LEVEL_ALL));
        assertTrue(CorpusSchema.KNOWN_JOB_LEVELS.contains("L12"));
        assertTrue(CorpusSchema.KNOWN_EMPLOYEE_CLASSES.contains(CorpusSchema.EMPLOYEE_CLASS_ALL));
    }
}

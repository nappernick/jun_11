package com.amazon.ingestion.schema;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

public class CorpusSchemaVersionTest {

    @Test
    public void baseKeyStripsTrailingVersion() {
        assertEquals("content-type", CorpusSchema.baseKey("content-type-16"));
        assertEquals("applicable-policy", CorpusSchema.baseKey("applicable-policy-0"));
        assertEquals("entitlement", CorpusSchema.baseKey("entitlement-1"));
        // Unversioned system_* keys are unchanged (the internal hyphen is not a version).
        assertEquals("system_job-level", CorpusSchema.baseKey("system_job-level"));
        assertEquals("system_employee-class", CorpusSchema.baseKey("system_employee-class"));
    }

    @Test
    public void keyVersionReadsTheSuffix() {
        assertEquals(16, CorpusSchema.keyVersion("content-type-16"));
        assertEquals(0, CorpusSchema.keyVersion("applicable-policy-0"));
        assertEquals(-1, CorpusSchema.keyVersion("system_job-level"));
    }

    @Test
    public void resolveLatestVersionsTakesHighestPerBase() {
        // content-type appears as 13/15/16 -> 16 wins; others pass through.
        Map<String, String> latest = CorpusSchema.resolveLatestVersions(List.of(
                "content-type-13", "content-type-15", "content-type-16",
                "applicable-policy-0", "system_job-level", "entitlement-1"));
        assertEquals("content-type-16", latest.get("content-type"));
        assertEquals("applicable-policy-0", latest.get("applicable-policy"));
        assertEquals("entitlement-1", latest.get("entitlement"));
        assertEquals("system_job-level", latest.get("system_job-level"));
    }

    @Test
    public void resolveLatestVersionsHandlesOutOfOrderInput() {
        Map<String, String> latest = CorpusSchema.resolveLatestVersions(List.of(
                "content-type-16", "content-type-2", "content-type-9"));
        assertEquals("content-type-16", latest.get("content-type"));
    }
}

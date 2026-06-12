package com.amazon.ingestion.processor;

import com.amazon.ingestion.corex.CoreXContentNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

public class MetadataAssemblerTest {

    private static final ObjectMapper JSON = new ObjectMapper();

    private static CoreXContentNode node() {
        ObjectNode md = JSON.createObjectNode();
        md.set("system_job-level", JSON.createArrayNode().add("All Job Levels"));
        md.set("system_employee-class", JSON.createArrayNode().add("All Employee Classes"));
        // versioned content-type: 13 and 16 present -> 16 wins, value "Skywalker FAQ"
        md.put("content-type-13", "reference");
        md.set("content-type-16", JSON.createArrayNode().add("Skywalker FAQ"));
        md.set("applicable-policy-0", JSON.createArrayNode().add("[Policy][https://x]"));

        ObjectNode raw = JSON.createObjectNode();
        raw.put("nodeId", "n1");
        raw.put("title", "Why book in the program?");
        raw.put("status", "DRAFT");
        raw.set("geography", JSON.createArrayNode().add("Global"));
        raw.put("content", "BODY TEXT SHOULD NOT APPEAR IN METADATA");
        raw.set("metadata", md); // raw metadata blob should be merged, not nested

        return new CoreXContentNode("n1", "0.13.0", "DRAFT",
                List.of("Global"), List.of("Travel"), md,
                JSON.getNodeFactory().textNode("BODY TEXT"), "owner", "", true, raw);
    }

    @Test
    public void assemblePreservesTopLevelFieldsExceptBodyAndRawMetadata() {
        ObjectNode out = new MetadataAssembler().assemble(node());
        assertEquals("Why book in the program?", out.path("title").asText());
        assertEquals("DRAFT", out.path("status").asText());
        assertEquals("Global", out.path("geography").path(0).asText());
        // body content is embedded separately, never copied into metadata
        assertFalse(out.has("content"), "body content must not be in metadata");
        // the raw nested 'metadata' blob is merged in, not stored as a nested string
        assertFalse(out.path("metadata").isObject() && out.path("metadata").has("system_job-level"),
                "raw metadata blob should be flattened in, not nested");
    }

    @Test
    public void assembleResolvesVersionedCustomFieldsToHighestAndBaseKeys() {
        ObjectNode out = new MetadataAssembler().assemble(node());
        // base-keyed, highest version wins
        assertTrue(out.has("content-type"), "versioned field stored under base name");
        assertFalse(out.has("content-type-16"), "raw versioned key not stored");
        assertEquals("Skywalker FAQ", out.path("content-type").path(0).asText());
        assertTrue(out.has("applicable-policy"));
        // system_* passthrough
        assertEquals("All Job Levels", out.path("system_job-level").path(0).asText());
    }

    @Test
    public void resolveContentTypeTakesHighestVersionValue() {
        assertEquals("Skywalker FAQ", MetadataAssembler.resolveContentType(node()));
    }
}

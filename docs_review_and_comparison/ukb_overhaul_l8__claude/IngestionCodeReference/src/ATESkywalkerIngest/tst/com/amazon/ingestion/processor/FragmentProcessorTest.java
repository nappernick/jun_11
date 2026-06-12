package com.amazon.ingestion.processor;

import com.amazon.ingestion.corex.ContentNodeFetcher;
import com.amazon.ingestion.corex.CoreXContentNode;
import com.amazon.ingestion.corex.CoreXTextExtractor;
import com.amazon.ingestion.embedding.EmbeddingClient;
import com.amazon.ingestion.indexing.FragmentDocument;
import com.amazon.ingestion.indexing.FragmentIndexWriter;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

public class FragmentProcessorTest {

    private static final ObjectMapper JSON = new ObjectMapper();

    @Test
    public void processFetchesExtractsEmbedsAndWritesOneFragmentDocument() {
        CapturingWriter writer = new CapturingWriter();
        FragmentProcessor processor = new FragmentProcessor(
                fullyScopedNode(),
                new CoreXTextExtractor(),
                new FixedEmbeddingClient(),
                writer);

        long indexed = processor.process("faq_evidence", "node-1", "2026-05-29T00:00:00Z");

        assertEquals(1L, indexed);
        assertEquals("faq_evidence", writer.indexName);
        assertEquals("node-1", writer.document.fragmentId());
        assertEquals("node-1", writer.document.sourceId());
        assertEquals("Question Answer", writer.document.text());
        // Real COREx values from metadata/geography; "Global" is the genuine everybody value.
        assertEquals(List.of("Global"), writer.document.country());
        assertEquals(List.of("All Job Levels"), writer.document.level());
        assertEquals(List.of("All Employee Classes"), writer.document.role());
        assertEquals("2026-05-29T00:00:00Z", writer.document.corpusVersion());
        assertEquals(List.of(0.25d, 0.75d), writer.document.embedding());
        // Promoted indexed field (T18): content_type resolved from the versioned metadata key.
        assertEquals("Skywalker FAQ", writer.document.contentType());
    }

    @Test
    public void blankTextNodeIsSkippedNotWritten() {
        CapturingWriter writer = new CapturingWriter();
        ContentNodeFetcher blankNode = nodeId -> new CoreXContentNode(
                nodeId, "1", "PUBLISHED",
                List.of("Global"), List.of(),
                scopeMetadata(), JSON.createObjectNode(),
                "owner", "managedBy", true, null);
        FragmentProcessor processor = new FragmentProcessor(
                blankNode, new CoreXTextExtractor(), new FixedEmbeddingClient(), writer);

        long indexed = processor.process("faq_evidence", "node-1", "v");

        assertEquals(0L, indexed);
        assertNull(writer.document);
    }

    @Test
    public void nodeMissingScopeIsSkipped() {
        // R10: a node with no scope on a dimension is not published.
        CapturingWriter writer = new CapturingWriter();
        ContentNodeFetcher noScope = nodeId -> {
            try {
                return new CoreXContentNode(
                        nodeId, "1", "PUBLISHED",
                        List.of(), List.of(),               // empty geography
                        JSON.createObjectNode(),            // empty metadata -> no level/role
                        JSON.readTree(rteContent()),
                        "owner", "managedBy", true, null);
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        };
        FragmentProcessor processor = new FragmentProcessor(
                noScope, new CoreXTextExtractor(), new FixedEmbeddingClient(), writer);

        long indexed = processor.process("faq_evidence", "node-1", "v");

        assertEquals(0L, indexed);
        assertNull(writer.document);
    }

    private static ContentNodeFetcher fullyScopedNode() {
        return nodeId -> {
            try {
                return new CoreXContentNode(
                        nodeId,
                        "1.2.3",
                        "PUBLISHED",
                        List.of("Global"),
                        List.of("Travel Booking"),
                        scopeMetadata(),
                        JSON.readTree(rteContent()),
                        "amzn1.abacus.team.looo53floubmzytmswva",
                        "AMAZON_TRAVEL_EVENTS_EXPENSE",
                        true,
                        null);
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        };
    }

    /**
     * Metadata in the real prod shape: system_* keys with array values, plus a versioned
     * content-type-N field. Exercises the version-resolution + array-scope paths.
     */
    private static ObjectNode scopeMetadata() {
        ObjectNode md = JSON.createObjectNode();
        md.set("system_job-level", JSON.createArrayNode().add("All Job Levels"));
        md.set("system_employee-class", JSON.createArrayNode().add("All Employee Classes"));
        md.put("content-type-16", "Skywalker FAQ");
        return md;
    }

    private static String rteContent() throws Exception {
        String rte = "[{\"children\":[{\"text\":\"Question\"},{\"text\":\"Answer\"}]}]";
        return "{\"system_rte_v2\":{\"type\":\"RTE_V2\",\"content\":"
                + JSON.writeValueAsString(rte)
                + "}}";
    }

    private static final class FixedEmbeddingClient implements EmbeddingClient {
        @Override
        public List<Double> embedDocument(String text) {
            assertEquals("Question Answer", text);
            return List.of(0.25d, 0.75d);
        }
    }

    private static final class CapturingWriter implements FragmentIndexWriter {
        private String indexName;
        private FragmentDocument document;

        @Override
        public void write(String indexName, FragmentDocument document) {
            this.indexName = indexName;
            this.document = document;
        }
    }
}

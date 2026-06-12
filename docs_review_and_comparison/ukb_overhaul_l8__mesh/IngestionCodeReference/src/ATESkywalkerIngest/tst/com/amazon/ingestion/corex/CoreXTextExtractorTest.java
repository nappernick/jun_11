package com.amazon.ingestion.corex;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.Test;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Extractor tests grounded in the <b>real</b> COREx PlateJS body captured live from
 * getContentNodes (node 38b86658-…, "Business Travel Visas - EMEA", 2026-06-03). The fixture
 * {@code /corex/platejs-real-blocks.json} is the actual PlateJS block array COREx returned.
 */
public class CoreXTextExtractorTest {

    private static final ObjectMapper JSON = new ObjectMapper();

    /** Wrap a PlateJS block array (as a JSON string) in the real RTE_V2 content envelope. */
    private static CoreXContentNode nodeWithPlateJs(String plateArrayJson) throws Exception {
        String contentJson = "{\"system_rte_v2\":{\"type\":\"RTE_V2\",\"content\":"
                + JSON.writeValueAsString(plateArrayJson)
                + "}}";
        return new CoreXContentNode(
                "node-1", "1.0.0", "PUBLISHED",
                List.of("Global"), List.of("Travel"),
                JSON.createObjectNode(),
                JSON.readTree(contentJson),
                "owner", "managedBy", true, null);
    }

    private static String loadRealPlateJsArray() throws Exception {
        try (InputStream in = CoreXTextExtractorTest.class.getResourceAsStream(
                "/resources/corex/platejs-real-blocks.json")) {
            assertNotNull(in, "real PlateJS fixture must be on the test classpath");
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    @Test
    public void extractsCleanProseFromRealPlateJsBody() throws Exception {
        String realArray = loadRealPlateJsArray();
        CoreXContentNode node = nodeWithPlateJs(realArray);

        String text = new CoreXTextExtractor().extract(node);

        // Real headings, body, and list/link text all come through as readable prose.
        assertTrue(text.contains("Business Travel Visas - EMEA"), "h1 title");
        assertTrue(text.contains("Overview"), "h2 heading");
        assertTrue(text.contains("business visa"), "body prose");
        assertTrue(text.contains("International Short-Term Travel Checklist"), "link text");

        // No PlateJS/JSON structural tokens leak into the embedded text.
        assertFalse(text.contains("\"type\""));
        assertFalse(text.contains("children"));
        assertFalse(text.contains("RTE_V2"));
        assertFalse(text.contains("system_rte_v2"));

        // Non-breaking spaces are normalized away.
        assertFalse(text.contains("\u00a0"));
    }

    @Test
    public void preservesInTextLinkUrls() throws Exception {
        // A paragraph with an inline link: both the link text and the URL must survive, so the
        // target is embedded and BM25-searchable (FAQ answers are often "go to this page").
        String plate = "[{\"type\":\"p\",\"children\":["
                + "{\"text\":\"See \"},"
                + "{\"type\":\"a\",\"url\":\"https://w.amazon.com/bin/view/GlobalTravel/\","
                + "\"children\":[{\"text\":\"Global Travel\"}]},"
                + "{\"text\":\" for details.\"}"
                + "]}]";
        CoreXContentNode node = nodeWithPlateJs(plate);

        String text = new CoreXTextExtractor().extract(node);

        assertTrue(text.contains("Global Travel"), "link text kept");
        assertTrue(text.contains("https://w.amazon.com/bin/view/GlobalTravel/"), "link URL kept inline");
        assertTrue(text.contains("for details."), "surrounding prose kept");
    }

    @Test
    public void joinsBlocksSoHeadingAndBodyDoNotRunTogether() throws Exception {
        String plate = "[{\"type\":\"h2\",\"children\":[{\"text\":\"Heading\"}]},"
                + "{\"type\":\"p\",\"children\":[{\"text\":\"Body text.\"}]}]";
        CoreXContentNode node = nodeWithPlateJs(plate);

        String text = new CoreXTextExtractor().extract(node);

        assertEquals("Heading Body text.", text);
    }

    @Test
    public void boldAndOtherInlineMarksDoNotAlterText() throws Exception {
        String plate = "[{\"type\":\"p\",\"children\":["
                + "{\"text\":\"This is \"},"
                + "{\"text\":\"important\",\"bold\":true},"
                + "{\"text\":\" today.\"}"
                + "]}]";
        CoreXContentNode node = nodeWithPlateJs(plate);

        String text = new CoreXTextExtractor().extract(node);

        assertEquals("This is important today.", text);
    }

    @Test
    public void blankWhenContentHasNoText() throws Exception {
        CoreXContentNode node = new CoreXContentNode(
                "node-1", "1.0.0", "PUBLISHED",
                List.of(), List.of(),
                JSON.createObjectNode(),
                JSON.readTree("{}"),
                "owner", "managedBy", true, null);

        // Empty content yields blank text (no title fallback), which the processor treats as a
        // skip rather than backfilling a non-existent value.
        assertTrue(new CoreXTextExtractor().extract(node).isBlank());
    }

    @Test
    public void bareStringBodyReturnedAsNormalizedText() throws Exception {
        ObjectNode unused = JSON.createObjectNode();
        CoreXContentNode node = new CoreXContentNode(
                "node-str", "1.0.0", "PUBLISHED",
                List.of(), List.of(),
                unused,
                JSON.getNodeFactory().textNode("Plain   body\u00a0text"),
                "owner", "", true, null);

        assertEquals("Plain body text", new CoreXTextExtractor().extract(node));
    }
}

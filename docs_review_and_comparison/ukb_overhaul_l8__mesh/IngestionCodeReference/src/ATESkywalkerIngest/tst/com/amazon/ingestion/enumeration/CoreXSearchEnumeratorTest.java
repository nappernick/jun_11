package com.amazon.ingestion.enumeration;

import com.amazon.ingestion.corex.CoreXGraphQLClient;
import com.amazon.ingestion.corex.CoreXRequestException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

public class CoreXSearchEnumeratorTest {

    private static final ObjectMapper JSON = new ObjectMapper();

    @Test
    public void enumerateUsesDomainOwnerGlobalStateFilter() throws Exception {
        CapturingClient client = new CapturingClient();
        CoreXSearchEnumerator enumerator = new CoreXSearchEnumerator(
                client,
                "amzn1.abacus.team.looo53floubmzytmswva");

        List<EnumeratedNode> nodes = enumerator.enumerate();

        assertEquals(List.of(new EnumeratedNode("eb558531-15e7-4030-bed0-72eaa07b6f9c",
                "2026-05-29T00:00:00Z")), nodes);
        JsonNode filter = JSON.readTree(client.lastBody)
                .path("variables")
                .path("search")
                .path("filters")
                .path(0);
        assertEquals("domainOwner", filter.path("id").asText());
        assertEquals("GLOBALSTATE", filter.path("group").asText());
        assertEquals("STRING", filter.path("columnType").asText());
        assertEquals("amzn1.abacus.team.looo53floubmzytmswva", filter.path("value").path(0).asText());
        assertFalse(client.lastBody.contains("COREX_FAQ_TOPIC_ID"));
        assertFalse(client.lastBody.contains("\"topics\""));
    }

    @Test
    public void computeMarkerReturnsMostRecentLastModifiedDate() {
        CoreXSearchEnumerator enumerator = new CoreXSearchEnumerator(new CapturingClient(), "owner");

        String marker = enumerator.computeMarker(List.of(
                new EnumeratedNode("b", "2026-05-29T00:00:00Z"),
                new EnumeratedNode("a", "2026-05-30T12:00:00Z"),
                new EnumeratedNode("c", "2026-05-28T00:00:00Z")));

        assertEquals("2026-05-30T12:00:00Z", marker);
    }

    @Test
    public void computeMarkerIsOrderIndependent() {
        CoreXSearchEnumerator enumerator = new CoreXSearchEnumerator(new CapturingClient(), "owner");

        String first = enumerator.computeMarker(List.of(
                new EnumeratedNode("b", "2026-05-29T00:00:00Z"),
                new EnumeratedNode("a", "2026-05-30T00:00:00Z")));
        String second = enumerator.computeMarker(List.of(
                new EnumeratedNode("a", "2026-05-30T00:00:00Z"),
                new EnumeratedNode("b", "2026-05-29T00:00:00Z")));

        assertEquals(first, second);
        assertEquals("2026-05-30T00:00:00Z", first);
    }

    private static final class CapturingClient extends CoreXGraphQLClient {
        private String lastBody;

        private CapturingClient() {
            super(null, null, "example.com");
        }

        @Override
        public JsonNode post(String path, String graphqlBody) {
            if (!"/search/graphql".equals(path)) {
                throw new CoreXRequestException("Unexpected path " + path);
            }
            lastBody = graphqlBody;
            try {
                return JSON.readTree("{"
                        + "\"data\":{\"searchContent\":{\"status\":\"SUCCESS\","
                        + "\"payload\":{\"data\":[{\"nodeId\":\"eb558531-15e7-4030-bed0-72eaa07b6f9c\","
                        + "\"lastModifiedDate\":\"2026-05-29T00:00:00Z\"}],"
                        + "\"pagination\":{\"limit\":50,\"pageNumber\":1,\"total\":1}}}}}");
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        }
    }
}

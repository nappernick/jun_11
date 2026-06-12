package com.amazon.ateskywalkerquery.retrieval;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class AossHybridRetrievalClientTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void buildQueryBodyHasHybridLegsAndSentinelScopeFilter() throws Exception {
        RetrievalRequest req =
            new RetrievalRequest("hotel booking", List.of(0.1f, 0.2f, 0.3f), "USA", "L5", "F - Regular Full Time", 20);
        JsonNode root = MAPPER.readTree(AossHybridRetrievalClient.buildQueryBody(req));

        assertEquals(20, root.path("size").asInt());
        JsonNode legs = root.path("query").path("hybrid").path("queries");
        assertEquals(2, legs.size());
        assertEquals(
            "hotel booking",
            legs.get(0)
                .path("bool")
                .path("must")
                .get(0)
                .path("match")
                .path("text")
                .asText());
        assertEquals(3, legs.get(1).path("knn").path("embedding").path("vector").size());
        assertEquals(20, legs.get(1).path("knn").path("embedding").path("k").asInt());
        // Scope filter matches the requester's specific value OR the per-axis sentinel.
        String filterJson = legs.get(0).path("bool").path("filter").toString();
        assertTrue(filterJson.contains("USA") && filterJson.contains("Global"));
        assertTrue(filterJson.contains("L5") && filterJson.contains("All Job Levels"));
        assertTrue(filterJson.contains("F - Regular Full Time") && filterJson.contains("All Employee Classes"));
    }

    @Test
    void scopeFilterEmitsSentinelOnceWhenRequesterValueIsSentinel() throws Exception {
        RetrievalRequest req =
            new RetrievalRequest("q", List.of(0.1f), "Global", "All Job Levels", "All Employee Classes", 5);
        JsonNode legs = MAPPER.readTree(AossHybridRetrievalClient.buildQueryBody(req))
            .path("query")
            .path("hybrid")
            .path("queries");
        JsonNode countryTerms =
            legs.get(0).path("bool").path("filter").get(0).path("terms").path("country");
        assertEquals(1, countryTerms.size());
        assertEquals("Global", countryTerms.get(0).asText());
    }

    @Test
    void parseResponseReadsTopLevelUrlAndPolicyLinks() {
        // source_url and policy_links are TOP-LEVEL _source fields; title is in source_metadata.
        String json = "{\"hits\":{\"hits\":[{\"_score\":1.23,\"_source\":{"
            + "\"source_id\":\"abc\",\"text\":\"hello\",\"source_url\":\"http://x\","
            + "\"policy_links\":[\"p1\",\"p2\"],\"source_metadata\":{\"title\":\"T\"}}}]}}";
        List<RetrievedHit> hits = AossHybridRetrievalClient.parseResponse(json);
        assertEquals(1, hits.size());
        RetrievedHit hit = hits.get(0);
        assertEquals("abc", hit.sourceId());
        assertEquals("T", hit.title());
        assertEquals("hello", hit.text());
        assertEquals(1.23, hit.score(), 1e-9);
        assertEquals("http://x", hit.sourceUrl());
        assertEquals(List.of("p1", "p2"), hit.policyLinks());
    }
}

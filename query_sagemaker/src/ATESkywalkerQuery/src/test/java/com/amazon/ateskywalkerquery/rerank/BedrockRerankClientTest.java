package com.amazon.ateskywalkerquery.rerank;

import com.amazon.ateskywalkerquery.EvidenceCandidate;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class BedrockRerankClientTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void buildBodyMatchesRerankApiShape() throws Exception {
        JsonNode root = MAPPER.readTree(BedrockRerankClient.buildBody("q", List.of("d0", "d1"), "arn:model", 5));
        JsonNode query = root.path("queries").get(0);
        assertEquals("q", query.path("textQuery").path("text").asText());
        assertEquals("TEXT", query.path("type").asText());
        assertEquals(2, root.path("sources").size());
        JsonNode doc0 = root.path("sources").get(0).path("inlineDocumentSource");
        assertEquals("d0", doc0.path("textDocument").path("text").asText());
        assertEquals("INLINE", root.path("sources").get(0).path("type").asText());
        JsonNode config = root.path("rerankingConfiguration");
        assertEquals("BEDROCK_RERANKING_MODEL", config.path("type").asText());
        JsonNode bedrock = config.path("bedrockRerankingConfiguration");
        assertEquals(5, bedrock.path("numberOfResults").asInt());
        assertEquals(
            "arn:model", bedrock.path("modelConfiguration").path("modelArn").asText());
    }

    @Test
    void parseResultsReadsIndexAndScore() {
        List<RerankHit> hits = BedrockRerankClient.parseResults(
            "{\"results\":[{\"index\":1,\"relevanceScore\":0.9},{\"index\":0,\"relevanceScore\":0.5}]}");
        assertEquals(2, hits.size());
        assertEquals(1, hits.get(0).index());
        assertEquals(0.9, hits.get(0).relevanceScore(), 1e-9);
    }

    @Test
    void mapResultsReordersAndSetsScores() {
        EvidenceCandidate c0 = new EvidenceCandidate();
        c0.setSourceId("s0");
        EvidenceCandidate c1 = new EvidenceCandidate();
        c1.setSourceId("s1");
        List<EvidenceCandidate> ranked =
            BedrockRerankClient.mapResults(List.of(c0, c1), List.of(new RerankHit(1, 0.9), new RerankHit(0, 0.5)));
        assertEquals(2, ranked.size());
        assertEquals("s1", ranked.get(0).getSourceId());
        assertEquals(0.9, ranked.get(0).getRerankScore(), 1e-9);
        assertEquals("s0", ranked.get(1).getSourceId());
    }
}

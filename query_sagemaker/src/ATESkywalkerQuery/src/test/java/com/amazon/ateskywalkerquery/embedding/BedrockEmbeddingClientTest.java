package com.amazon.ateskywalkerquery.embedding;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class BedrockEmbeddingClientTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void buildBodyHasCohereEmbedFields() throws Exception {
        JsonNode root = MAPPER.readTree(BedrockEmbeddingClient.buildBody("hotel booking"));
        assertEquals("hotel booking", root.path("texts").get(0).asText());
        assertEquals("search_query", root.path("input_type").asText());
        assertEquals("float", root.path("embedding_types").get(0).asText());
    }

    @Test
    void parseResponseExtractsFirstFloatVector() {
        String json = "{\"embeddings\":{\"float\":[[0.1,0.2,0.3]]}}";
        List<Float> vector = BedrockEmbeddingClient.parseResponse(json);
        assertEquals(3, vector.size());
        assertEquals(0.1f, vector.get(0), 1e-6f);
        assertEquals(0.3f, vector.get(2), 1e-6f);
    }
}

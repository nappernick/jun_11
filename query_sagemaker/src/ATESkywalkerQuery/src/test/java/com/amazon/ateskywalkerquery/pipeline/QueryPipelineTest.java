package com.amazon.ateskywalkerquery.pipeline;

import com.amazon.ateskywalkerquery.Level;
import com.amazon.ateskywalkerquery.Role;
import com.amazon.ateskywalkerquery.SearchByExplicitScopeInput;
import com.amazon.ateskywalkerquery.SearchResult;
import com.amazon.ateskywalkerquery.embedding.FakeEmbeddingClient;
import com.amazon.ateskywalkerquery.rerank.FakeRerankClient;
import com.amazon.ateskywalkerquery.retrieval.FakeHybridRetrievalClient;
import com.amazon.ateskywalkerquery.retrieval.RetrievedHit;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

class QueryPipelineTest {

    @Test
    void runsEmbedRetrieveNormalizeRerankAndBuildsResult() {
        FakeEmbeddingClient embed = new FakeEmbeddingClient();
        embed.setVector(List.of(0.1f, 0.2f));
        FakeHybridRetrievalClient retrieve = new FakeHybridRetrievalClient();
        retrieve.setHits(List.of(
            new RetrievedHit("s1", "T1", "text one", 1.5, "http://a", List.of("p1")),
            new RetrievedHit("s2", "T2", "text two", 1.2, null, List.of())));
        FakeRerankClient rerank = new FakeRerankClient();

        QueryPipeline pipeline = new QueryPipeline(embed, retrieve, rerank, 20, 10);
        SearchByExplicitScopeInput input = new SearchByExplicitScopeInput();
        input.setQueryText("hotel booking");
        input.setEmployeeId("123");
        input.setCountry("US");
        input.setLevel(Level.L5);
        input.setRole(Role.F_REGULAR_FULL_TIME);

        SearchResult result = pipeline.execute(input);

        assertEquals("hotel booking", embed.lastText());
        assertEquals("hotel booking", rerank.lastQuery());
        assertNotNull(result.getCorrelationId());
        assertEquals("ANSWERABLE", result.getResultKind());
        assertEquals("FAQ_ONLY", result.getRoute().getPath());
        assertEquals(List.of("FAQ"), result.getRoute().getSurvivingArms());
        assertEquals("US", result.getScopeSnapshot().getCountry());
        assertEquals(Role.F_REGULAR_FULL_TIME, result.getScopeSnapshot().getRole());
        // fake rerank reverses order, so s2 ranks first
        assertEquals(2, result.getEvidence().size());
        assertEquals("s2", result.getEvidence().get(0).getSourceId());
        assertNotNull(result.getEvidence().get(0).getRerankScore());
        // retrieval saw the scope-filtered request with the embedding
        assertEquals(2, retrieve.lastRequest().embedding().size());
        assertEquals("US", retrieve.lastRequest().country());
    }
}

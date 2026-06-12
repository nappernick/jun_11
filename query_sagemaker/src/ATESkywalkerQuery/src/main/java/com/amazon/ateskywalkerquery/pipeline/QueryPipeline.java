package com.amazon.ateskywalkerquery.pipeline;

import com.amazon.ateskywalkerquery.EvidenceCandidate;
import com.amazon.ateskywalkerquery.RouteInfo;
import com.amazon.ateskywalkerquery.ScopeSnapshot;
import com.amazon.ateskywalkerquery.SearchByExplicitScopeInput;
import com.amazon.ateskywalkerquery.SearchResult;
import com.amazon.ateskywalkerquery.embedding.EmbeddingClient;
import com.amazon.ateskywalkerquery.normalize.CandidateNormalizer;
import com.amazon.ateskywalkerquery.rerank.RerankClient;
import com.amazon.ateskywalkerquery.retrieval.HybridRetrievalClient;
import com.amazon.ateskywalkerquery.retrieval.RetrievalRequest;
import com.amazon.ateskywalkerquery.retrieval.RetrievedHit;

import java.util.List;
import java.util.UUID;

/**
 * FAQ-only query pipeline through the reranker: embed the query, hybrid-retrieve scoped FAQ
 * evidence, normalize into candidates, and rerank. No routing gate and no UKB arm this pass;
 * the route is recorded as a static FAQ marker.
 */
public class QueryPipeline {
    private static final String FAQ_ARM = "FAQ";

    private final EmbeddingClient embeddingClient;
    private final HybridRetrievalClient retrievalClient;
    private final RerankClient rerankClient;
    private final int candidateBudget;
    private final int topN;

    /**
     * @param embeddingClient query embedding client
     * @param retrievalClient hybrid retrieval client
     * @param rerankClient evidence rerank client
     * @param candidateBudget number of candidates to retrieve before reranking
     * @param topN number of reranked candidates to return
     */
    public QueryPipeline(
        EmbeddingClient embeddingClient,
        HybridRetrievalClient retrievalClient,
        RerankClient rerankClient,
        int candidateBudget,
        int topN) {
        this.embeddingClient = embeddingClient;
        this.retrievalClient = retrievalClient;
        this.rerankClient = rerankClient;
        this.candidateBudget = candidateBudget;
        this.topN = topN;
    }

    /**
     * Runs the pipeline for an explicit-scope request.
     *
     * @param input the explicit-scope search input
     * @return the search result with reranked FAQ evidence
     */
    public SearchResult execute(SearchByExplicitScopeInput input) {
        String role = roleValue(input);
        List<Float> embedding = embeddingClient.embed(input.getQueryText());
        RetrievalRequest request = new RetrievalRequest(
            input.getQueryText(), embedding, input.getCountry(), input.getLevel(), role, candidateBudget);
        List<RetrievedHit> hits = retrievalClient.retrieve(request);
        List<EvidenceCandidate> candidates = CandidateNormalizer.normalize(hits, FAQ_ARM);
        List<EvidenceCandidate> reranked = rerankClient.rerank(input.getQueryText(), candidates, topN);

        SearchResult result = new SearchResult();
        result.setCorrelationId(UUID.randomUUID().toString());
        result.setResultKind("ANSWERABLE");

        ScopeSnapshot scope = new ScopeSnapshot();
        scope.setCountry(input.getCountry());
        scope.setLevel(input.getLevel());
        scope.setRole(role);
        result.setScopeSnapshot(scope);

        RouteInfo route = new RouteInfo();
        route.setPath("FAQ_ONLY");
        route.setSurvivingArms(List.of(FAQ_ARM));
        route.setRerankerState("NORMAL");
        result.setRoute(route);

        result.setEvidence(reranked);
        return result;
    }

    private static String roleValue(SearchByExplicitScopeInput input) {
        Object role = input.getRole();
        return role == null ? null : role.toString();
    }
}

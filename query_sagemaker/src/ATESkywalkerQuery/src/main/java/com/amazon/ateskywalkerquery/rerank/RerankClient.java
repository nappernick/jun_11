package com.amazon.ateskywalkerquery.rerank;

import com.amazon.ateskywalkerquery.EvidenceCandidate;

import java.util.List;

/** Reranks evidence candidates by relevance to the query. */
public interface RerankClient {

    /**
     * Reranks candidates and returns the top results with rerank scores set.
     *
     * @param query query text
     * @param candidates candidates to rerank (arm-local order)
     * @param topN maximum number of results to return
     * @return reranked candidates, best-first, truncated to topN
     */
    List<EvidenceCandidate> rerank(String query, List<EvidenceCandidate> candidates, int topN);
}

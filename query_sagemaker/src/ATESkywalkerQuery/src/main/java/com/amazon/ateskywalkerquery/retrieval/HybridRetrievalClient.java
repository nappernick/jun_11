package com.amazon.ateskywalkerquery.retrieval;

import java.util.List;

/** Hybrid (BM25 + kNN) retrieval against the FAQ evidence index. */
public interface HybridRetrievalClient {

    /**
     * Retrieves scope-filtered hybrid hits for the request.
     *
     * @param request the scoped retrieval request
     * @return hits ordered by fused score (best first)
     */
    List<RetrievedHit> retrieve(RetrievalRequest request);
}

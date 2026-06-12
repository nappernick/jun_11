package com.amazon.ateskywalkerquery.retrieval;

import java.util.ArrayList;
import java.util.List;

/** In-memory {@link HybridRetrievalClient} for tests: returns canned hits, records the request. */
public class FakeHybridRetrievalClient implements HybridRetrievalClient {
    private List<RetrievedHit> hits = new ArrayList<>();
    private RetrievalRequest lastRequest;

    public void setHits(List<RetrievedHit> hits) {
        this.hits = new ArrayList<>(hits);
    }

    public RetrievalRequest lastRequest() {
        return lastRequest;
    }

    @Override
    public List<RetrievedHit> retrieve(RetrievalRequest request) {
        this.lastRequest = request;
        return new ArrayList<>(hits);
    }
}

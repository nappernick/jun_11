package com.amazon.ateskywalkerquery.rerank;

import com.amazon.ateskywalkerquery.EvidenceCandidate;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** In-memory {@link RerankClient} for tests: reverses order with descending scores, records query. */
public class FakeRerankClient implements RerankClient {
    private String lastQuery;

    public String lastQuery() {
        return lastQuery;
    }

    @Override
    public List<EvidenceCandidate> rerank(String query, List<EvidenceCandidate> candidates, int topN) {
        this.lastQuery = query;
        List<EvidenceCandidate> reversed = new ArrayList<>(candidates);
        Collections.reverse(reversed);
        List<EvidenceCandidate> out = new ArrayList<>();
        double score = 1.0;
        for (EvidenceCandidate candidate : reversed) {
            if (out.size() >= topN) {
                break;
            }
            candidate.setRerankScore(score);
            score -= 0.1;
            out.add(candidate);
        }
        return out;
    }
}

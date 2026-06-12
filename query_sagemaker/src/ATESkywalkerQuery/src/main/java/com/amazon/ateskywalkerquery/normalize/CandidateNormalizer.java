package com.amazon.ateskywalkerquery.normalize;

import com.amazon.ateskywalkerquery.EvidenceCandidate;
import com.amazon.ateskywalkerquery.retrieval.RetrievedHit;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/** Maps AOSS hits into the {@link EvidenceCandidate} envelope the reranker consumes. */
public final class CandidateNormalizer {

    private CandidateNormalizer() {}

    /**
     * Normalizes hits (best-first) into evidence candidates, assigning 1-based arm-local ranks.
     * Rerank score is left unset; it is populated after reranking.
     *
     * @param hits retrieval hits in arm-local order
     * @param sourceArm retrieval arm label (e.g. FAQ)
     * @return evidence candidates in the same order
     */
    public static List<EvidenceCandidate> normalize(List<RetrievedHit> hits, String sourceArm) {
        List<EvidenceCandidate> candidates = new ArrayList<>();
        int rank = 1;
        for (RetrievedHit hit : hits) {
            EvidenceCandidate candidate = new EvidenceCandidate();
            candidate.setCandidateId(UUID.randomUUID().toString());
            candidate.setSourceArm(sourceArm);
            candidate.setSourceId(hit.sourceId());
            candidate.setTitle(hit.title());
            candidate.setText(hit.text());
            candidate.setSourceUrl(hit.sourceUrl());
            candidate.setPolicyLinks(hit.policyLinks());
            candidate.setArmLocalRank(rank);
            candidates.add(candidate);
            rank++;
        }
        return candidates;
    }
}

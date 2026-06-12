package com.amazon.ateskywalkerquery.normalize;

import com.amazon.ateskywalkerquery.EvidenceCandidate;
import com.amazon.ateskywalkerquery.retrieval.RetrievedHit;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

class CandidateNormalizerTest {

    @Test
    void mapsHitsToCandidatesWithArmLocalRanks() {
        List<RetrievedHit> hits = List.of(
            new RetrievedHit("s1", "T1", "text one", 1.5, "http://a", List.of("p1")),
            new RetrievedHit("s2", "T2", "text two", 1.2, null, List.of()));

        List<EvidenceCandidate> candidates = CandidateNormalizer.normalize(hits, "FAQ");

        assertEquals(2, candidates.size());
        EvidenceCandidate first = candidates.get(0);
        assertNotNull(first.getCandidateId());
        assertEquals("FAQ", first.getSourceArm());
        assertEquals("s1", first.getSourceId());
        assertEquals("T1", first.getTitle());
        assertEquals("text one", first.getText());
        assertEquals("http://a", first.getSourceUrl());
        assertEquals(List.of("p1"), first.getPolicyLinks());
        assertEquals(Integer.valueOf(1), first.getArmLocalRank());
        assertNull(first.getRerankScore());
        assertEquals(Integer.valueOf(2), candidates.get(1).getArmLocalRank());
    }

    @Test
    void emptyHitsYieldEmptyCandidates() {
        assertEquals(0, CandidateNormalizer.normalize(List.of(), "FAQ").size());
    }
}

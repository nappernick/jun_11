package com.amazon.ateskywalkerquery.retrieval;

import java.util.List;

/**
 * A single hit returned by AOSS, before normalization into the evidence envelope.
 *
 * @param sourceId dedup/source identifier
 * @param title document title (from source_metadata)
 * @param text fragment text
 * @param score fused hybrid score
 * @param sourceUrl canonical source URL, if present
 * @param policyLinks associated policy links, if present
 */
public record RetrievedHit(
    String sourceId, String title, String text, double score, String sourceUrl, List<String> policyLinks) {}

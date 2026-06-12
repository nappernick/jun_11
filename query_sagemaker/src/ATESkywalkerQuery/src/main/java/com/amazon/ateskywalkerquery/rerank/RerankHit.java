package com.amazon.ateskywalkerquery.rerank;

/**
 * A single rerank result returned by the model.
 *
 * @param index index into the submitted candidate list
 * @param relevanceScore model relevance score
 */
public record RerankHit(int index, double relevanceScore) {}

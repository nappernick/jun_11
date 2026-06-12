package com.amazon.ateskywalkerquery.embedding;

import java.util.List;

/** Produces a query embedding vector for hybrid retrieval. */
public interface EmbeddingClient {

    /**
     * Embeds the query text into a dense vector.
     *
     * @param text query text
     * @return embedding vector
     */
    List<Float> embed(String text);
}

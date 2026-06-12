package com.amazon.ingestion.embedding;

import java.util.List;

/**
 * Embeds text with Cohere Embed v4.
 *
 * <h2>input_type contract (cross-team, load-bearing for recall)</h2>
 * Cohere Embed v4 produces <em>asymmetric</em> embeddings: the {@code input_type} must match
 * the role of the text. This ingestion path embeds stored documents and therefore MUST use
 * {@link #INPUT_TYPE_DOCUMENT} ({@value #INPUT_TYPE_DOCUMENT}). The runtime query path, which
 * is a separate service, MUST embed user queries with {@link #INPUT_TYPE_QUERY}
 * ({@value #INPUT_TYPE_QUERY}). Mismatching the two (e.g. embedding queries as documents)
 * does not error — it <b>silently degrades recall</b>, because the query and document vectors
 * land in mismatched regions of the space. Both sides must also agree on the output dimension
 * ({@code 1024}) and the model/inference-profile id.
 *
 * <p>This interface exposes the document side only; the constants are published here so the
 * query side can reference the same contract rather than re-deriving the string.
 */
public interface EmbeddingClient {

    /** {@code input_type} for embedding stored documents (this ingestion path). */
    String INPUT_TYPE_DOCUMENT = "search_document";

    /** {@code input_type} the runtime query path MUST use for user queries. */
    String INPUT_TYPE_QUERY = "search_query";

    /**
     * Embed a stored document's text. Uses {@link #INPUT_TYPE_DOCUMENT}.
     *
     * @param text the document text to embed (non-blank).
     * @return the embedding vector.
     */
    List<Double> embedDocument(String text);
}

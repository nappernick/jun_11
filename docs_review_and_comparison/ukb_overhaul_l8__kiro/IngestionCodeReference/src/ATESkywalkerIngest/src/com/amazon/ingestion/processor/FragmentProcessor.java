package com.amazon.ingestion.processor;

import com.amazon.ingestion.corex.CoreXContentNode;
import com.amazon.ingestion.corex.CoreXTextExtractor;
import com.amazon.ingestion.corex.ContentNodeFetcher;
import com.amazon.ingestion.embedding.EmbeddingClient;
import com.amazon.ingestion.indexing.FragmentDocument;
import com.amazon.ingestion.indexing.FragmentIndexWriter;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.List;

/**
 * Processes one COREx node into one OpenSearch fragment document.
 *
 * Posture (R2/R10): item-by-item and failure-tolerant. A node that cannot become a valid,
 * embeddable fragment is skipped with a structured log line and contributes zero fragments;
 * it never aborts the run. The only thing that reaches the index is a fragment with non-empty
 * text and a valid embedding — junk that would corrupt the runtime's answer-vs-abstain
 * decision is dropped here.
 *
 * {@link #process} returns the number of fragments written (1 on success, 0 on a skip), and
 * does not throw for content-shaped problems (blank text). It propagates only genuine
 * infrastructure faults (fetch/embed/write transport failures) so the caller can log-and-skip
 * them per node; the caller never lets one node end the run.
 */
public final class FragmentProcessor {

    private static final Logger LOGGER = LogManager.getLogger(FragmentProcessor.class);

    private final ContentNodeFetcher fetcher;
    private final CoreXTextExtractor textExtractor;
    private final EmbeddingClient embeddingClient;
    private final FragmentIndexWriter indexWriter;
    private final ScopeMapper scopeMapper;
    private final MetadataAssembler metadataAssembler;

    public FragmentProcessor(
            ContentNodeFetcher fetcher,
            CoreXTextExtractor textExtractor,
            EmbeddingClient embeddingClient,
            FragmentIndexWriter indexWriter) {
        this(fetcher, textExtractor, embeddingClient, indexWriter, new ScopeMapper());
    }

    public FragmentProcessor(
            ContentNodeFetcher fetcher,
            CoreXTextExtractor textExtractor,
            EmbeddingClient embeddingClient,
            FragmentIndexWriter indexWriter,
            ScopeMapper scopeMapper) {
        this.fetcher = fetcher;
        this.textExtractor = textExtractor;
        this.embeddingClient = embeddingClient;
        this.indexWriter = indexWriter;
        this.scopeMapper = scopeMapper;
        this.metadataAssembler = new MetadataAssembler();
    }

    /**
     * Fetch, extract, embed, and write one node as a fragment document.
     *
     * @param indexName the index to write into.
     * @param nodeId    the COREx node to process.
     * @param corpusVersion the run's snapshot date marker, stamped on the document.
     * @return 1 if a fragment was written, 0 if the node was skipped for empty text.
     */
    public long process(String indexName, String nodeId, String corpusVersion) {
        CoreXContentNode node = fetcher.fetch(nodeId);
        String text = textExtractor.extract(node);
        if (text.isBlank()) {
            LOGGER.warn(
                    "Skipping nodeId={}: no embeddable text extracted (skip-and-continue, R2)",
                    nodeId);
            return 0L;
        }

        // R10: never publish unscoped evidence. Each scope dimension must carry at least one
        // real COREx value ("Global" / "All Job Levels" / "All Employee Classes" all count as
        // real "applies to everybody" values). An empty dimension is a data problem on that
        // node — skip and log, do not backfill a synthetic value.
        List<String> country = scopeMapper.country(node);
        List<String> level = scopeMapper.level(node);
        List<String> role = scopeMapper.role(node);
        if (country.isEmpty() || level.isEmpty() || role.isEmpty()) {
            LOGGER.warn(
                    "Skipping nodeId={}: missing scope (country={} level={} role={}); "
                            + "R10 — not publishing unscoped evidence",
                    nodeId, country, level, role);
            return 0L;
        }

        List<Double> embedding = embeddingClient.embedDocument(text);
        indexWriter.write(indexName, documentFor(node, text, country, level, role, embedding, corpusVersion));
        return 1L;
    }

    private FragmentDocument documentFor(
            CoreXContentNode node,
            String text,
            List<String> country,
            List<String> level,
            List<String> role,
            List<Double> embedding,
            String corpusVersion) {
        com.fasterxml.jackson.databind.node.ObjectNode metadata = metadataAssembler.assemble(node);
        // Promote one field to an indexed filter (T18): content_type, the resolved scalar value
        // of the versioned COREx custom key (content-type-NN, highest version wins). It also
        // remains in source_metadata; here we surface it as a first-class indexed field.
        String contentType = MetadataAssembler.resolveContentType(node);
        return new FragmentDocument(
                node.nodeId(),
                node.nodeId(),
                text,
                "",
                List.of(),
                country,
                level,
                role,
                corpusVersion,
                List.of(),
                embedding,
                metadata,
                contentType);
    }
}

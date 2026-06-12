package com.amazon.ingestion.lambda.processor;

import com.amazon.ingestion.contract.WorkItemRequest;
import com.amazon.ingestion.contract.WorkItemResponse;
import com.amazon.ingestion.corex.ContentNodeFetcher;
import com.amazon.ingestion.corex.CoreXContentFetcher;
import com.amazon.ingestion.corex.CoreXCredentialsProvider;
import com.amazon.ingestion.corex.CoreXGraphQLClient;
import com.amazon.ingestion.corex.CoreXSecretReader;
import com.amazon.ingestion.corex.CoreXTextExtractor;
import com.amazon.ingestion.corex.LocalExemplarContentFetcher;
import com.amazon.ingestion.embedding.BedrockEmbeddingClient;
import com.amazon.ingestion.indexing.OpenSearchFragmentWriter;
import com.amazon.ingestion.processor.FragmentProcessor;
import com.amazonaws.services.lambda.runtime.Context;
import com.amazonaws.services.lambda.runtime.RequestHandler;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import software.amazon.awssdk.services.secretsmanager.SecretsManagerClient;
import software.amazon.awssdk.services.sts.StsClient;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Function;

public class Processor implements RequestHandler<Map<String, Object>, WorkItemResponse> {

    private static final Logger LOGGER = LogManager.getLogger(Processor.class);
    private static final ObjectMapper JSON = new ObjectMapper();

    // Per-node retry (R2/R10): the rebuild is all-or-nothing — every node must be successfully
    // rebuilt or the coordinator declines to promote (see RebuildCoordinator flip gate). A node
    // therefore only lands in the skipped list after these attempts are exhausted, so a
    // transient fault (Bedrock/AOSS throttling — HTTP 429 surfaces as a RuntimeException here
    // because the embed/write clients use a hand-rolled HTTP path, not the SDK retry layer — or
    // a brief network blip) does not permanently drop a node and block every future promote.
    // Exponential backoff with full jitter (AWS-recommended) spreads retries across the many
    // work items that throttle together.
    private static final int MAX_PROCESS_ATTEMPTS = 4;
    private static final long BASE_BACKOFF_MILLIS = 500L;
    private static final long MAX_BACKOFF_MILLIS = 8_000L;

    private static final String ENV_COREX_ROLE_ARN = "COREX_ROLE_ARN";
    private static final String ENV_COREX_SESSION = "COREX_ROLE_SESSION_PREFIX";
    private static final String ENV_COREX_SECRET = "COREX_SECRET_NAME";
    private static final String ENV_COREX_HOST = "COREX_HOST";
    private static final String ENV_BEDROCK_REGION = "BEDROCK_REGION";
    private static final String ENV_BEDROCK_MODEL_ID = "BEDROCK_MODEL_ID";
    private static final String ENV_OPENSEARCH_ENDPOINT = "OPENSEARCH_ENDPOINT";
    private static final String DEFAULT_REGION = "us-west-2";

    private static final String SOURCE_EXEMPLAR = "exemplar";

    /**
     * Resolves a FragmentProcessor for a given source key, cached across invocations (Lambda
     * reuses the instance between warm invokes). In tests this is replaced with a fixed map.
     */
    private final Function<String, FragmentProcessor> processorForSource;
    private final Map<String, FragmentProcessor> cache = new ConcurrentHashMap<>();

    public Processor() {
        this(Processor::buildProcessorForSource);
    }

    Processor(Function<String, FragmentProcessor> processorForSource) {
        this.processorForSource = processorForSource;
    }

    /** Test/injection constructor pinning a single processor for the default COREx source. */
    Processor(FragmentProcessor fixed) {
        this(source -> fixed);
    }

    @Override
    public WorkItemResponse handleRequest(Map<String, Object> event, Context context) {
        WorkItemRequest request = JSON.convertValue(event, WorkItemRequest.class);
        LOGGER.info(
                "Processor invoked runId={} workItem={}/{} source={} nodes={}",
                request.runId(),
                request.workItemIndex(),
                request.workItemCount(),
                request.source(),
                request.nodeIds().size());

        FragmentProcessor processor = cache.computeIfAbsent(request.source(), processorForSource);

        List<String> skipped = new ArrayList<>();
        long indexed = 0L;
        for (String nodeId : request.nodeIds()) {
            try {
                long written = processWithRetry(
                        processor, request.indexName(), nodeId, request.corpusVersion());
                if (written == 0L) {
                    skipped.add(nodeId);
                } else {
                    indexed += written;
                }
            } catch (RuntimeException e) {
                // Only after retries are exhausted (R2): log and skip. The run continues so the
                // remaining nodes are still attempted, but a non-empty skip list makes the
                // coordinator decline to promote — a partial corpus never replaces the live one.
                LOGGER.warn(
                        "Skipping nodeId={} after {} attempt(s); rebuild will not promote unless every "
                                + "node succeeds: {}",
                        nodeId, MAX_PROCESS_ATTEMPTS, e.getMessage(), e);
                skipped.add(nodeId);
            }
        }

        LOGGER.info(
                "Processor work item complete runId={} workItem={} indexed={} skipped={}",
                request.runId(),
                request.workItemIndex(),
                indexed,
                skipped.size());
        return WorkItemResponse.completed(request.runId(), request.workItemIndex(), indexed, skipped);
    }

    /**
     * Process one node, retrying transient failures with exponential backoff + full jitter.
     * Content-shaped non-results (blank text, missing scope) return 0 without throwing and are
     * NOT retried here — retrying would not change a data-quality outcome; they surface as a
     * skip to the caller. Only thrown {@link RuntimeException}s (fetch/embed/write transport
     * faults, including throttling) are retried. Rethrows the last failure once attempts are
     * exhausted so the caller records the node as skipped.
     *
     * @param processor     the source-specific FragmentProcessor.
     * @param indexName     the rebuild target index.
     * @param nodeId        the COREx node to process.
     * @param corpusVersion the run's snapshot marker.
     * @return fragments written (1 on success, 0 on a content skip).
     */
    private long processWithRetry(
            FragmentProcessor processor, String indexName, String nodeId, String corpusVersion) {
        RuntimeException last = null;
        for (int attempt = 1; attempt <= MAX_PROCESS_ATTEMPTS; attempt++) {
            try {
                return processor.process(indexName, nodeId, corpusVersion);
            } catch (RuntimeException e) {
                last = e;
                if (attempt < MAX_PROCESS_ATTEMPTS) {
                    long backoff = backoffMillis(attempt);
                    LOGGER.warn(
                            "Transient failure on nodeId={} (attempt {}/{}); retrying after {}ms: {}",
                            nodeId, attempt, MAX_PROCESS_ATTEMPTS, backoff, e.getMessage());
                    sleep(backoff);
                }
            }
        }
        throw last;
    }

    /**
     * Exponential backoff with full jitter: a random delay in {@code [0, min(cap, base*2^(n-1))]}.
     * Full jitter (rather than fixed exponential) de-synchronizes the many work items that hit a
     * Bedrock/AOSS throttle at the same instant, which is the recommended AWS backoff strategy.
     */
    private static long backoffMillis(int attempt) {
        long ceiling = Math.min(MAX_BACKOFF_MILLIS, BASE_BACKOFF_MILLIS * (1L << (attempt - 1)));
        return ThreadLocalRandom.current().nextLong(ceiling + 1);
    }

    private static void sleep(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Interrupted during retry backoff", e);
        }
    }

    /**
     * Build a FragmentProcessor for the requested content source. The embed + write tail is
     * identical; only the content fetcher differs.
     *
     * <ul>
     *   <li>{@code corex} (default, production): live COREx getContentNode.</li>
     *   <li>{@code exemplar} (dev/proof, request-scoped): bundled real-prod records, so the
     *       full extract → embed → AOSS write path can be proven without a COREx write API.
     *       Selected per invoke via {@code WorkItemRequest.source}; the Poller never sets it,
     *       so scheduled production runs are always COREx.</li>
     * </ul>
     *
     * @param source the content-source key.
     * @return a FragmentProcessor wired to that source.
     */
    private static FragmentProcessor buildProcessorForSource(String source) {
        String region = envOrDefault(ENV_BEDROCK_REGION, DEFAULT_REGION);
        ContentNodeFetcher fetcher;
        if (SOURCE_EXEMPLAR.equalsIgnoreCase(source)) {
            LOGGER.warn("source=exemplar — serving bundled real-prod exemplar records, not live COREx");
            fetcher = new LocalExemplarContentFetcher();
        } else {
            fetcher = buildCoreXFetcher();
        }
        return new FragmentProcessor(
                fetcher,
                new CoreXTextExtractor(),
                new BedrockEmbeddingClient(region, env(ENV_BEDROCK_MODEL_ID)),
                new OpenSearchFragmentWriter(env(ENV_OPENSEARCH_ENDPOINT), region));
    }

    private static ContentNodeFetcher buildCoreXFetcher() {
        SecretsManagerClient sm = SecretsManagerClient.create();
        StsClient sts = StsClient.create();
        CoreXSecretReader secretReader = new CoreXSecretReader(sm, env(ENV_COREX_SECRET));
        CoreXCredentialsProvider credentials = new CoreXCredentialsProvider(
                sts,
                env(ENV_COREX_ROLE_ARN),
                env(ENV_COREX_SESSION),
                secretReader);
        CoreXGraphQLClient graphql = new CoreXGraphQLClient(credentials, secretReader, env(ENV_COREX_HOST));
        return new CoreXContentFetcher(graphql);
    }

    private static String env(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("Required environment variable missing: " + name);
        }
        return value;
    }

    private static String envOrDefault(String name, String defaultValue) {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? defaultValue : value;
    }
}

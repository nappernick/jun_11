package com.amazon.ateskywalkerquery.rerank;

import com.amazon.ateskywalkerquery.EvidenceCandidate;
import com.amazon.ateskywalkerquery.aws.SigV4Signer;
import com.amazon.ateskywalkerquery.diagnostics.RerankDiagnostics;
import com.amazon.ateskywalkerquery.diagnostics.RerankDiagnosticsReport;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Reranks evidence via the Amazon Bedrock {@code Rerank} API (on-demand, e.g.
 * {@code cohere.rerank-v3-5}). The model ARN is injected, so the same client serves either
 * the Bedrock Rerank 3.5 path or a SageMaker-fronted v4 model ARN (config-selected upstream).
 * Each call is optionally wrapped with context-window diagnostics. SigV4-signed for
 * {@code bedrock} against {@code bedrock-agent-runtime}.
 */
public class BedrockRerankClient implements RerankClient {
    private static final Logger LOG = LogManager.getLogger(BedrockRerankClient.class);
    private static final String SERVICE = "bedrock";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int HTTP_OK = 200;
    private static final int MAX_ATTEMPTS = 2;

    private final String modelArn;
    private final String region;
    private final AwsCredentialsProvider credentialsProvider;
    private final boolean diagnosticsEnabled;
    private final int contextWindowTokens;
    private final double charsPerToken;
    private final HttpClient httpClient;
    private final Duration requestTimeout;

    /**
     * @param modelArn rerank model ARN (e.g. cohere.rerank-v3-5 foundation-model ARN)
     * @param region AWS region
     * @param credentialsProvider credentials used to sign requests
     * @param diagnosticsEnabled whether to compute + log context-window diagnostics per call
     * @param contextWindowTokens context window to evaluate overflow against (4096 for 3.5)
     * @param charsPerToken token-estimate heuristic (chars per token)
     */
    public BedrockRerankClient(
        String modelArn,
        String region,
        AwsCredentialsProvider credentialsProvider,
        boolean diagnosticsEnabled,
        int contextWindowTokens,
        double charsPerToken) {
        this.modelArn = modelArn;
        this.region = region;
        this.credentialsProvider = credentialsProvider;
        this.diagnosticsEnabled = diagnosticsEnabled;
        this.contextWindowTokens = contextWindowTokens;
        this.charsPerToken = charsPerToken;
        this.requestTimeout = Duration.ofSeconds(10);
        this.httpClient =
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    }

    @Override
    public List<EvidenceCandidate> rerank(String query, List<EvidenceCandidate> candidates, int topN) {
        List<String> documents = new ArrayList<>();
        for (EvidenceCandidate candidate : candidates) {
            documents.add(candidate.getText());
        }
        if (diagnosticsEnabled) {
            logDiagnostics(query, documents);
        }
        String body = buildBody(query, documents, modelArn, Math.min(topN, candidates.size()));
        URI uri = URI.create("https://bedrock-agent-runtime." + region + ".amazonaws.com/rerank");

        RerankException last = null;
        for (int attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
            try {
                Map<String, String> headers = SigV4Signer.sign(
                    "POST",
                    uri,
                    Map.of(),
                    body,
                    new SigV4Signer.AwsServiceTarget(SERVICE, region),
                    credentialsProvider.resolveCredentials(),
                    Instant.now());
                HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(uri)
                    .timeout(requestTimeout)
                    .POST(HttpRequest.BodyPublishers.ofString(body));
                headers.forEach(builder::header);
                HttpResponse<String> response = httpClient.send(builder.build(), HttpResponse.BodyHandlers.ofString());
                if (response.statusCode() != HTTP_OK) {
                    throw new RerankException(
                        "Bedrock rerank returned HTTP " + response.statusCode() + ": " + response.body());
                }
                return mapResults(candidates, parseResults(response.body()));
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RerankException("Bedrock rerank interrupted", e);
            } catch (RerankException e) {
                last = e;
            } catch (Exception e) {
                last = new RerankException("Bedrock rerank failed", e);
            }
        }
        throw last;
    }

    private void logDiagnostics(String query, List<String> documents) {
        RerankDiagnosticsReport report =
            RerankDiagnostics.analyze(query, documents, contextWindowTokens, charsPerToken);
        LOG.info(
            "rerank-diagnostics window={} queryTokens={} docCount={} maxDocTokens={} "
                + "maxPairTokens={} overflowCount={} totalDocTokens={} estPayloadBytes={}",
            contextWindowTokens,
            report.queryTokens(),
            report.docCount(),
            report.maxDocTokens(),
            report.maxPairTokens(),
            report.overflowCount(),
            report.totalDocTokens(),
            report.estPayloadBytes());
    }

    static String buildBody(String query, List<String> documents, String modelArn, int numberOfResults) {
        ObjectNode root = MAPPER.createObjectNode();
        ObjectNode queryNode = MAPPER.createObjectNode();
        queryNode.put("type", "TEXT");
        queryNode.putObject("textQuery").put("text", query);
        root.putArray("queries").add(queryNode);

        ArrayNode sources = root.putArray("sources");
        for (String document : documents) {
            ObjectNode source = MAPPER.createObjectNode();
            source.put("type", "INLINE");
            ObjectNode inline = source.putObject("inlineDocumentSource");
            inline.put("type", "TEXT");
            inline.putObject("textDocument").put("text", document == null ? "" : document);
            sources.add(source);
        }

        ObjectNode config = root.putObject("rerankingConfiguration");
        config.put("type", "BEDROCK_RERANKING_MODEL");
        ObjectNode bedrockConfig = config.putObject("bedrockRerankingConfiguration");
        bedrockConfig.put("numberOfResults", numberOfResults);
        bedrockConfig.putObject("modelConfiguration").put("modelArn", modelArn);
        return root.toString();
    }

    static List<RerankHit> parseResults(String json) {
        List<RerankHit> hits = new ArrayList<>();
        try {
            for (JsonNode result : MAPPER.readTree(json).path("results")) {
                hits.add(new RerankHit(
                    result.path("index").asInt(), result.path("relevanceScore").asDouble()));
            }
        } catch (Exception e) {
            throw new RerankException("Failed to parse Bedrock rerank response", e);
        }
        return hits;
    }

    static List<EvidenceCandidate> mapResults(List<EvidenceCandidate> candidates, List<RerankHit> hits) {
        List<EvidenceCandidate> ranked = new ArrayList<>();
        for (RerankHit hit : hits) {
            if (hit.index() < 0 || hit.index() >= candidates.size()) {
                continue;
            }
            EvidenceCandidate candidate = candidates.get(hit.index());
            candidate.setRerankScore(hit.relevanceScore());
            ranked.add(candidate);
        }
        return ranked;
    }
}

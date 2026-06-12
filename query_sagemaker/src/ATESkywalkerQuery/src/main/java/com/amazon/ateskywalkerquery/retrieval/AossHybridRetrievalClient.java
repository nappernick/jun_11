package com.amazon.ateskywalkerquery.retrieval;

import com.amazon.ateskywalkerquery.aws.SigV4Signer;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
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
 * Retrieves FAQ evidence from Amazon OpenSearch Serverless using a single hybrid
 * (BM25 {@code match} + {@code knn}) query fused by the {@code skywalker-faq-hybrid}
 * search pipeline, scope-filtered on country/level/role. Requests are SigV4-signed for
 * the {@code aoss} service (including the {@code x-amz-content-sha256} header AOSS requires).
 */
public class AossHybridRetrievalClient implements HybridRetrievalClient {
    private static final String SERVICE = "aoss";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int HTTP_OK = 200;

    private final String endpoint;
    private final String index;
    private final String searchPipeline;
    private final String region;
    private final AwsCredentialsProvider credentialsProvider;
    private final HttpClient httpClient;
    private final Duration requestTimeout;

    /**
     * @param endpoint AOSS collection endpoint (https://host)
     * @param index index name or alias to search
     * @param searchPipeline hybrid search pipeline name
     * @param region AWS region
     * @param credentialsProvider credentials used to sign requests (may be an assume-role provider)
     */
    public AossHybridRetrievalClient(
        String endpoint,
        String index,
        String searchPipeline,
        String region,
        AwsCredentialsProvider credentialsProvider) {
        this.endpoint = endpoint;
        this.index = index;
        this.searchPipeline = searchPipeline;
        this.region = region;
        this.credentialsProvider = credentialsProvider;
        this.requestTimeout = Duration.ofSeconds(5);
        this.httpClient =
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    }

    @Override
    public List<RetrievedHit> retrieve(RetrievalRequest request) {
        String body = buildQueryBody(request);
        URI signUri = URI.create(endpoint + "/" + index + "/_search");
        Map<String, String> query = Map.of("search_pipeline", searchPipeline);
        try {
            Map<String, String> headers = SigV4Signer.sign(
                "POST",
                signUri,
                query,
                body,
                new SigV4Signer.AwsServiceTarget(SERVICE, region),
                credentialsProvider.resolveCredentials(),
                Instant.now());
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(signUri + "?search_pipeline=" + searchPipeline))
                .timeout(requestTimeout)
                .POST(HttpRequest.BodyPublishers.ofString(body));
            headers.forEach(builder::header);
            HttpResponse<String> response = httpClient.send(builder.build(), HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != HTTP_OK) {
                throw new RetrievalException("AOSS returned HTTP " + response.statusCode() + ": " + response.body());
            }
            return parseResponse(response.body());
        } catch (RetrievalException e) {
            throw e;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RetrievalException("AOSS hybrid retrieval interrupted", e);
        } catch (Exception e) {
            throw new RetrievalException("AOSS hybrid retrieval failed", e);
        }
    }

    /** Per-axis "applies to everyone" sentinel values; scope filter matches specific OR sentinel. */
    private static final String COUNTRY_SENTINEL = "Global";

    private static final String LEVEL_SENTINEL = "All Job Levels";
    private static final String ROLE_SENTINEL = "All Employee Classes";

    static String buildQueryBody(RetrievalRequest req) {
        ObjectNode root = MAPPER.createObjectNode();
        root.put("size", req.size());
        ArrayNode source = root.putArray("_source");
        source.add("source_id");
        source.add("title");
        source.add("text");
        source.add("source_url");
        source.add("policy_links");

        ObjectNode matchLeg = MAPPER.createObjectNode();
        ObjectNode bool = matchLeg.putObject("bool");
        ArrayNode must = bool.putArray("must");
        must.add(
            MAPPER.createObjectNode().set("match", MAPPER.createObjectNode().put("text", req.queryText())));
        bool.set("filter", scopeFilter(req));

        ObjectNode knnLeg = MAPPER.createObjectNode();
        ObjectNode embedding = knnLeg.putObject("knn").putObject("embedding");
        ArrayNode vector = embedding.putArray("vector");
        for (Float f : req.embedding()) {
            vector.add(f);
        }
        embedding.put("k", req.size());
        embedding.putObject("filter").putObject("bool").set("filter", scopeFilter(req));

        ArrayNode queries = root.putObject("query").putObject("hybrid").putArray("queries");
        queries.add(matchLeg);
        queries.add(knnLeg);
        return root.toString();
    }

    private static ArrayNode scopeFilter(RetrievalRequest req) {
        ArrayNode scope = MAPPER.createArrayNode();
        scope.add(scopedTerms("country", req.country(), COUNTRY_SENTINEL));
        scope.add(scopedTerms("level", req.level(), LEVEL_SENTINEL));
        scope.add(scopedTerms("role", req.role(), ROLE_SENTINEL));
        return scope;
    }

    /**
     * A {@code terms} clause matching the requester's specific value OR the axis sentinel,
     * so fragments tagged "applies to everyone" (e.g. country "Global") are always in scope.
     * When the requester value is null/blank or already the sentinel, only the sentinel is matched.
     */
    private static ObjectNode scopedTerms(String field, String value, String sentinel) {
        ObjectNode clause = MAPPER.createObjectNode();
        ArrayNode values = clause.putObject("terms").putArray(field);
        if (value != null && !value.isBlank() && !value.equals(sentinel)) {
            values.add(value);
        }
        values.add(sentinel);
        return clause;
    }

    static List<RetrievedHit> parseResponse(String json) {
        List<RetrievedHit> hits = new ArrayList<>();
        try {
            JsonNode root = MAPPER.readTree(json);
            for (JsonNode hit : root.path("hits").path("hits")) {
                JsonNode src = hit.path("_source");
                JsonNode meta = src.path("source_metadata");
                // source_url and policy_links are top-level _source fields; title lives in
                // source_metadata. Fall back to source_metadata for url/links defensively.
                List<String> policyLinks = new ArrayList<>();
                JsonNode links = src.has("policy_links") ? src.path("policy_links") : meta.path("policy_links");
                for (JsonNode link : links) {
                    policyLinks.add(link.asText());
                }
                String sourceUrl = src.has("source_url")
                    ? src.path("source_url").asText(null)
                    : meta.path("source_url").asText(null);
                hits.add(new RetrievedHit(
                    src.path("source_id").asText(null),
                    meta.path("title").asText(null),
                    src.path("text").asText(null),
                    hit.path("_score").asDouble(0.0),
                    sourceUrl,
                    policyLinks));
            }
        } catch (Exception e) {
            throw new RetrievalException("Failed to parse AOSS response", e);
        }
        return hits;
    }
}

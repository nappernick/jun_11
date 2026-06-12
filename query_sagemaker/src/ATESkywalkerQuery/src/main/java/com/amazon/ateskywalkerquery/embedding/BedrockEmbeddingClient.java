package com.amazon.ateskywalkerquery.embedding;

import com.amazon.ateskywalkerquery.aws.SigV4Signer;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
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
 * Embeds query text via Amazon Bedrock {@code InvokeModel} against Cohere Embed v4
 * ({@code cohere.embed-v4:0}, {@code input_type=search_query}, float embeddings) — the
 * same model that built the stored index, so query vectors share its vector space.
 * Requests are SigV4-signed for the {@code bedrock} service.
 */
public class BedrockEmbeddingClient implements EmbeddingClient {
    private static final String SERVICE = "bedrock";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int HTTP_OK = 200;

    private final String modelId;
    private final String region;
    private final AwsCredentialsProvider credentialsProvider;
    private final HttpClient httpClient;
    private final Duration requestTimeout;

    /**
     * @param modelId Bedrock model id (e.g. {@code cohere.embed-v4:0})
     * @param region AWS region
     * @param credentialsProvider credentials used to sign requests
     */
    public BedrockEmbeddingClient(String modelId, String region, AwsCredentialsProvider credentialsProvider) {
        this.modelId = modelId;
        this.region = region;
        this.credentialsProvider = credentialsProvider;
        this.requestTimeout = Duration.ofSeconds(5);
        this.httpClient =
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    }

    @Override
    public List<Float> embed(String text) {
        String body = buildBody(text);
        URI uri = URI.create("https://bedrock-runtime." + region + ".amazonaws.com/model/" + modelId + "/invoke");
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
                throw new EmbeddingException("Bedrock returned HTTP " + response.statusCode() + ": " + response.body());
            }
            return parseResponse(response.body());
        } catch (EmbeddingException e) {
            throw e;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new EmbeddingException("Bedrock embedding interrupted", e);
        } catch (Exception e) {
            throw new EmbeddingException("Bedrock embedding failed", e);
        }
    }

    static String buildBody(String text) {
        ObjectNode root = MAPPER.createObjectNode();
        root.putArray("texts").add(text);
        root.put("input_type", "search_query");
        root.putArray("embedding_types").add("float");
        return root.toString();
    }

    static List<Float> parseResponse(String json) {
        List<Float> embedding = new ArrayList<>();
        try {
            JsonNode floats = MAPPER.readTree(json).path("embeddings").path("float");
            if (floats.isArray() && !floats.isEmpty()) {
                for (JsonNode value : floats.get(0)) {
                    embedding.add((float) value.asDouble());
                }
            }
        } catch (Exception e) {
            throw new EmbeddingException("Failed to parse Bedrock embedding response", e);
        }
        return embedding;
    }
}

package com.amazon.ingestion.embedding;

import com.amazon.ingestion.corex.CoreXRequestException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import software.amazon.awssdk.auth.credentials.AwsCredentials;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.http.ContentStreamProvider;
import software.amazon.awssdk.http.SdkHttpFullRequest;
import software.amazon.awssdk.http.SdkHttpMethod;
import software.amazon.awssdk.http.auth.aws.signer.AwsV4HttpSigner;
import software.amazon.awssdk.http.auth.spi.signer.SignedRequest;
import software.amazon.awssdk.regions.Region;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Optional;

/**
 * Embeds COREx fragment text through Bedrock Cohere Embed v4.
 */
public final class BedrockEmbeddingClient implements EmbeddingClient {

    /** Embedding size expected by the OpenSearch vector mapping. */
    public static final int OUTPUT_DIMENSION = 1024;

    private static final String SIGNING_SERVICE = "bedrock";
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(30);
    private static final ObjectMapper JSON = new ObjectMapper();

    private final HttpClient http;
    private final AwsV4HttpSigner signer;
    private final AwsCredentialsProvider credentials;
    private final Region region;
    private final String modelId;
    private final String host;
    private final String path;

    public BedrockEmbeddingClient(String region, String modelId) {
        this(DefaultCredentialsProvider.create(), Region.of(region), modelId);
    }

    BedrockEmbeddingClient(AwsCredentialsProvider credentials, Region region, String modelId) {
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
        this.signer = AwsV4HttpSigner.create();
        this.credentials = credentials;
        this.region = region;
        this.modelId = modelId;
        this.host = "bedrock-runtime." + region.id() + ".amazonaws.com";
        this.path = "/model/" + encodePathSegment(modelId) + "/invoke";
    }

    @Override
    public List<Double> embedDocument(String text) {
        if (text == null || text.isBlank()) {
            throw new IllegalArgumentException("text must be non-blank");
        }

        ObjectNode body = JSON.createObjectNode();
        // Ingest side embeds documents: input_type MUST be search_document (see EmbeddingClient
        // contract). The runtime query path uses search_query; mismatching silently hurts recall.
        body.put("input_type", INPUT_TYPE_DOCUMENT);
        body.putArray("texts").add(text);
        body.putArray("embedding_types").add("float");
        body.put("output_dimension", OUTPUT_DIMENSION);
        body.put("truncate", "RIGHT");

        try {
            JsonNode response = post(JSON.writeValueAsString(body));
            JsonNode vector = response.path("embeddings").path("float").path(0);
            if (!vector.isArray()) {
                vector = response.path("embeddings").path(0);
            }
            if (!vector.isArray()) {
                throw new CoreXRequestException("Bedrock response did not contain a float embedding: " + response);
            }

            List<Double> out = new ArrayList<>(vector.size());
            for (JsonNode value : vector) {
                out.add(value.asDouble());
            }
            if (out.size() != OUTPUT_DIMENSION) {
                throw new CoreXRequestException(
                        "Bedrock returned embedding dimension " + out.size() + ", expected " + OUTPUT_DIMENSION);
            }
            return List.copyOf(out);
        } catch (CoreXRequestException e) {
            throw e;
        } catch (Exception e) {
            throw new CoreXRequestException("Failed to embed document with model " + modelId, e);
        }
    }

    private JsonNode post(String body) throws Exception {
        byte[] bodyBytes = body.getBytes(StandardCharsets.UTF_8);
        AwsCredentials creds = credentials.resolveCredentials();

        SdkHttpFullRequest unsigned = SdkHttpFullRequest.builder()
                .method(SdkHttpMethod.POST)
                .protocol("https")
                .host(host)
                .encodedPath(path)
                .putHeader("Content-Type", "application/json")
                .putHeader("Accept", "application/json")
                .contentStreamProvider(ContentStreamProvider.fromUtf8String(body))
                .build();

        SignedRequest signed = signer.sign(r -> r
                .identity(creds)
                .request(unsigned)
                .payload(ContentStreamProvider.fromUtf8String(body))
                .putProperty(AwsV4HttpSigner.SERVICE_SIGNING_NAME, SIGNING_SERVICE)
                .putProperty(AwsV4HttpSigner.REGION_NAME, region.id()));

        HttpRequest.Builder request = HttpRequest.newBuilder()
                .uri(URI.create("https://" + host + path))
                .timeout(REQUEST_TIMEOUT)
                .POST(HttpRequest.BodyPublishers.ofByteArray(bodyBytes));
        signed.request().headers().forEach((name, values) -> {
            if (!isRestrictedHeader(name)) {
                values.forEach(value -> request.header(name, value));
            }
        });

        HttpResponse<byte[]> response = http.send(request.build(), HttpResponse.BodyHandlers.ofByteArray());
        String responseBody = Optional.ofNullable(response.body())
                .map(b -> new String(b, StandardCharsets.UTF_8))
                .orElse("");
        if (response.statusCode() / 100 != 2) {
            throw new CoreXRequestException("Bedrock returned HTTP " + response.statusCode() + ": " + responseBody);
        }
        return JSON.readTree(responseBody);
    }

    private static String encodePathSegment(String raw) {
        return URLEncoder.encode(raw, StandardCharsets.UTF_8).replace("+", "%20");
    }

    private static boolean isRestrictedHeader(String name) {
        String lower = name.toLowerCase(Locale.ROOT);
        return lower.equals("content-length")
                || lower.equals("host")
                || lower.equals("connection")
                || lower.equals("upgrade")
                || lower.equals("expect");
    }
}

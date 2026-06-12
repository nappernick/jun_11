package com.amazon.ingestion.indexing;

import com.amazon.ingestion.corex.CoreXRequestException;
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
import java.util.Locale;
import java.util.Optional;

/**
 * Writes fragment documents to OpenSearch Serverless with SigV4 service aoss.
 */
public final class OpenSearchFragmentWriter implements FragmentIndexWriter {

    private static final String SIGNING_SERVICE = "aoss";
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(30);
    private static final ObjectMapper JSON = new ObjectMapper();

    private final HttpClient http;
    private final AwsV4HttpSigner signer;
    private final AwsCredentialsProvider credentials;
    private final Region region;
    private final URI endpoint;

    public OpenSearchFragmentWriter(String endpoint, String region) {
        this(DefaultCredentialsProvider.create(), URI.create(endpoint), Region.of(region));
    }

    OpenSearchFragmentWriter(AwsCredentialsProvider credentials, URI endpoint, Region region) {
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
        this.signer = AwsV4HttpSigner.create();
        this.credentials = credentials;
        this.endpoint = endpoint;
        this.region = region;
    }

    @Override
    public void write(String indexName, FragmentDocument document) {
        try {
            ObjectNode body = JSON.createObjectNode();
            body.put("fragment_id", document.fragmentId());
            body.put("source_id", document.sourceId());
            body.put("text", document.text());
            body.put("source_url", document.sourceUrl());
            body.set("policy_links", JSON.valueToTree(document.policyLinks()));
            body.set("country", JSON.valueToTree(document.country()));
            body.set("level", JSON.valueToTree(document.level()));
            body.set("role", JSON.valueToTree(document.role()));
            body.put("corpus_version", document.corpusVersion());
            body.set("followup_fragment_ids", JSON.valueToTree(document.followupFragmentIds()));
            // Promoted indexed filter field (T18): also present in source_metadata, but indexed
            // here as a first-class keyword field for fast query-time filtering.
            body.put("content_type", document.contentType());
            body.set("embedding", JSON.valueToTree(document.embedding()));
            // Full preserved COREx metadata (flat_object): all top-level fields + all
            // version-resolved custom fields. Only the body text is embedded; this is for
            // provenance and future filters.
            if (document.sourceMetadata() != null && !document.sourceMetadata().isNull()) {
                body.set("source_metadata", document.sourceMetadata());
            }

            // AOSS Serverless does NOT support client-specified document IDs on writes
            // ("Document ID is not supported in create/index operation request"). So we POST
            // to /_doc with no id and let AOSS assign one. Idempotency-by-fragment_id is not
            // needed because the rebuild clears the index first (overwrite-in-place, R5); the
            // fragment_id is retained as a document FIELD for filtering and read-back, not as
            // the _id. (Pinned empirically against the alpha collection.)
            String path = "/" + encode(indexName) + "/_doc";
            post(path, JSON.writeValueAsString(body));
        } catch (CoreXRequestException e) {
            throw e;
        } catch (Exception e) {
            throw new CoreXRequestException("Failed to write fragment " + document.fragmentId(), e);
        }
    }

    private void post(String path, String body) throws Exception {
        byte[] bodyBytes = body.getBytes(StandardCharsets.UTF_8);
        AwsCredentials creds = credentials.resolveCredentials();
        String host = endpoint.getHost();

        SdkHttpFullRequest unsigned = SdkHttpFullRequest.builder()
                .method(SdkHttpMethod.POST)
                .protocol("https")
                .host(host)
                .encodedPath(path)
                .putHeader("Content-Type", "application/json")
                .contentStreamProvider(ContentStreamProvider.fromUtf8String(body))
                .build();

        SignedRequest signed = signer.sign(r -> r
                .identity(creds)
                .request(unsigned)
                .payload(ContentStreamProvider.fromUtf8String(body))
                .putProperty(AwsV4HttpSigner.SERVICE_SIGNING_NAME, SIGNING_SERVICE)
                .putProperty(AwsV4HttpSigner.REGION_NAME, region.id()));

        HttpRequest.Builder request = HttpRequest.newBuilder()
                .uri(endpoint.resolve(path))
                .timeout(REQUEST_TIMEOUT)
                .POST(HttpRequest.BodyPublishers.ofByteArray(bodyBytes));
        signed.request().headers().forEach((name, values) -> {
            if (!isRestrictedHeader(name)) {
                values.forEach(value -> request.header(name, value));
            }
        });

        HttpResponse<byte[]> response = http.send(request.build(), HttpResponse.BodyHandlers.ofByteArray());
        if (response.statusCode() / 100 != 2) {
            String responseBody = Optional.ofNullable(response.body())
                    .map(b -> new String(b, StandardCharsets.UTF_8))
                    .orElse("");
            throw new CoreXRequestException(
                    "OpenSearch returned HTTP " + response.statusCode() + ": " + responseBody);
        }
    }

    private static String encode(String raw) {
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

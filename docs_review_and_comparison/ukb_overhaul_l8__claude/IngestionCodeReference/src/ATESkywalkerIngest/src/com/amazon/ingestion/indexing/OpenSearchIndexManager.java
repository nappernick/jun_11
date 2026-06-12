package com.amazon.ingestion.indexing;

import com.amazon.ingestion.corex.CoreXRequestException;
import com.amazon.ingestion.snapshot.LiveIndexStore;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
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
import java.util.Locale;
import java.util.Optional;
import java.time.Duration;

/**
 * Zero-downtime, two-physical-index manager for the FAQ evidence corpus (T14).
 *
 * <p>Owns two physical indices, {@code <base>_a} and {@code <base>_b}, plus an SSM
 * {@link LiveIndexStore} pointer naming the live one. {@link #beginRebuild()} (re)creates the
 * idle index empty and returns it; {@link #promote(String)} flips the pointer to it after the
 * rebuild verifies a non-empty build. The live index is never touched during a rebuild, so
 * queries see no downtime. Aliases are not used (AOSS Serverless does not support them).
 *
 * <p>Data-plane requests are SigV4-signed for service {@code aoss}.
 */
public final class OpenSearchIndexManager implements IndexManager {

    private static final Logger LOGGER = LogManager.getLogger(OpenSearchIndexManager.class);
    private static final String SIGNING_SERVICE = "aoss";
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(30);

    /** Name of the hybrid search pipeline (R8). */
    public static final String HYBRID_PIPELINE = "skywalker-faq-hybrid";

    /** Embedding dimension; must match the Cohere Embed v4 output and the writer. */
    private static final int EMBEDDING_DIM = 1024;

    private final HttpClient http;
    private final AwsV4HttpSigner signer;
    private final AwsCredentialsProvider credentials;
    private final URI endpoint;
    private final Region region;
    private final String baseName;
    private final String indexA;
    private final String indexB;
    private final LiveIndexStore liveIndexStore;

    public OpenSearchIndexManager(
            String endpoint, String region, String baseName, LiveIndexStore liveIndexStore) {
        this(DefaultCredentialsProvider.create(), URI.create(endpoint), Region.of(region), baseName, liveIndexStore);
    }

    OpenSearchIndexManager(
            AwsCredentialsProvider credentials,
            URI endpoint,
            Region region,
            String baseName,
            LiveIndexStore liveIndexStore) {
        if (baseName == null || baseName.isBlank()) {
            throw new IllegalArgumentException("baseName must be non-blank");
        }
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
        this.signer = AwsV4HttpSigner.create();
        this.credentials = credentials;
        this.endpoint = endpoint;
        this.region = region;
        this.baseName = baseName;
        this.indexA = baseName + "_a";
        this.indexB = baseName + "_b";
        this.liveIndexStore = liveIndexStore;
    }

    @Override
    public Optional<String> liveIndexName() {
        return liveIndexStore.read();
    }

    @Override
    public String beginRebuild() {
        // Pick the idle index = the one that is NOT currently live. First run (no pointer) →
        // build into _a. Then (re)create it empty with the FAISS mapping so removed content
        // disappears and the mapping is current; ensure the hybrid pipeline (best-effort).
        String live = liveIndexStore.read().orElse(null);
        String target = indexA.equals(live) ? indexB : indexA;
        LOGGER.info("Rebuild target (idle) index={} (live={})", target, live == null ? "<none>" : live);

        recreateEmpty(target);
        ensureHybridPipelineBestEffort();
        return target;
    }

    @Override
    public void promote(String indexName) {
        if (!indexA.equals(indexName) && !indexB.equals(indexName)) {
            throw new IllegalArgumentException(
                    "Refusing to promote unknown index " + indexName + " (expected " + indexA + " or " + indexB + ")");
        }
        liveIndexStore.write(indexName);
        LOGGER.info("Promoted index {} to live (pointer flipped)", indexName);
    }

    @Override
    public long readBackCount(String indexName) {
        try {
            Response resp = send(SdkHttpMethod.GET, "/" + encode(indexName) + "/_count", null);
            if (resp.status() / 100 != 2) {
                LOGGER.warn("Read-back canary: _count {} returned HTTP {} {}", indexName, resp.status(), resp.body());
                return -1L;
            }
            int idx = resp.body().indexOf("\"count\"");
            if (idx < 0) {
                return -1L;
            }
            String tail = resp.body().substring(idx + 7).replaceAll("[^0-9]", " ").trim();
            return tail.isEmpty() ? -1L : Long.parseLong(tail.split("\\s+")[0]);
        } catch (RuntimeException e) {
            LOGGER.warn("Read-back canary failed for {} (soft, non-fatal): {}", indexName, e.toString());
            return -1L;
        }
    }

    /** Drop (if present) and recreate the target index empty with the current FAISS mapping. */
    private void recreateEmpty(String indexName) {
        Response delete = send(SdkHttpMethod.DELETE, "/" + encode(indexName), null);
        if (delete.status() / 100 != 2 && delete.status() != 404) {
            throw new CoreXRequestException(
                    "Failed to drop index " + indexName + ": HTTP " + delete.status() + " " + delete.body());
        }
        Response create = send(SdkHttpMethod.PUT, "/" + encode(indexName), indexBody());
        if (create.status() / 100 != 2) {
            if (create.body() != null && create.body().contains("resource_already_exists")) {
                // Lost a race to a concurrent creator; the mapping is fixed, so this is benign.
                LOGGER.info("Index {} created concurrently; continuing", indexName);
                return;
            }
            throw new CoreXRequestException(
                    "Failed to create index " + indexName + ": HTTP " + create.status() + " " + create.body());
        }
        LOGGER.info("Recreated empty rebuild target index {}", indexName);
    }

    private void ensureHybridPipelineBestEffort() {
        try {
            Response put = send(
                    SdkHttpMethod.PUT, "/_search/pipeline/" + encode(HYBRID_PIPELINE), hybridPipelineBody());
            if (put.status() / 100 == 2) {
                LOGGER.info("Hybrid search pipeline {} ensured", HYBRID_PIPELINE);
            } else {
                LOGGER.warn(
                        "Hybrid search pipeline {} not created (HTTP {} {}); ingestion continues. "
                                + "If 403, grant aoss:CreateCollectionItems/UpdateCollectionItems on the "
                                + "collection in the data access policy. Search-time only; not on the write path.",
                        HYBRID_PIPELINE,
                        put.status(),
                        put.body());
            }
        } catch (RuntimeException e) {
            LOGGER.warn(
                    "Hybrid search pipeline {} ensure failed; ingestion continues (search-time only)",
                    HYBRID_PIPELINE,
                    e);
        }
    }

    private static String indexBody() {
        return "{"
                + "\"settings\":{\"index\":{\"knn\":true}},"
                + "\"mappings\":{\"properties\":{"
                + "\"embedding\":{\"type\":\"knn_vector\",\"dimension\":" + EMBEDDING_DIM + ","
                + "\"method\":{\"engine\":\"faiss\",\"name\":\"hnsw\",\"space_type\":\"cosinesimil\","
                + "\"parameters\":{\"m\":24,\"ef_construction\":128}}},"
                + "\"fragment_id\":{\"type\":\"keyword\"},"
                + "\"source_id\":{\"type\":\"keyword\"},"
                + "\"text\":{\"type\":\"text\"},"
                + "\"source_url\":{\"type\":\"keyword\",\"index\":false},"
                + "\"policy_links\":{\"type\":\"keyword\",\"index\":false},"
                + "\"country\":{\"type\":\"keyword\"},"
                + "\"level\":{\"type\":\"keyword\"},"
                + "\"role\":{\"type\":\"keyword\"},"
                + "\"corpus_version\":{\"type\":\"keyword\"},"
                + "\"followup_fragment_ids\":{\"type\":\"keyword\",\"index\":false},"
                + "\"content_type\":{\"type\":\"keyword\"},"
                + "\"source_metadata\":{\"type\":\"flat_object\"}"
                + "}}}";
    }

    private static String hybridPipelineBody() {
        return "{"
                + "\"description\":\"Skywalker FAQ hybrid search: min_max normalization, "
                + "arithmetic_mean combination of BM25 and k-NN legs.\","
                + "\"phase_results_processors\":[{"
                + "\"normalization-processor\":{"
                + "\"normalization\":{\"technique\":\"min_max\"},"
                + "\"combination\":{\"technique\":\"arithmetic_mean\"}"
                + "}}]}";
    }

    private Response send(SdkHttpMethod method, String path, String body) {
        try {
            AwsCredentials creds = credentials.resolveCredentials();
            SdkHttpFullRequest.Builder unsigned = SdkHttpFullRequest.builder()
                    .method(method)
                    .protocol(endpoint.getScheme())
                    .host(endpoint.getHost())
                    .encodedPath(path);
            if (body != null) {
                unsigned.putHeader("Content-Type", "application/json")
                        .contentStreamProvider(ContentStreamProvider.fromUtf8String(body));
            }

            SignedRequest signed = signer.sign(r -> {
                r.identity(creds)
                        .request(unsigned.build())
                        .putProperty(AwsV4HttpSigner.SERVICE_SIGNING_NAME, SIGNING_SERVICE)
                        .putProperty(AwsV4HttpSigner.REGION_NAME, region.id());
                if (body != null) {
                    r.payload(ContentStreamProvider.fromUtf8String(body));
                }
            });

            HttpRequest.Builder request = HttpRequest.newBuilder()
                    .uri(endpoint.resolve(path))
                    .timeout(REQUEST_TIMEOUT)
                    .method(
                            method.name(),
                            body == null
                                    ? HttpRequest.BodyPublishers.noBody()
                                    : HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
            signed.request().headers().forEach((name, values) -> {
                if (!isRestrictedHeader(name)) {
                    values.forEach(value -> request.header(name, value));
                }
            });

            HttpResponse<byte[]> response = http.send(request.build(), HttpResponse.BodyHandlers.ofByteArray());
            String responseBody = Optional.ofNullable(response.body())
                    .map(b -> new String(b, StandardCharsets.UTF_8))
                    .orElse("");
            return new Response(response.statusCode(), responseBody);
        } catch (CoreXRequestException e) {
            throw e;
        } catch (Exception e) {
            throw new CoreXRequestException("OpenSearch request failed for " + method + " " + path, e);
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

    /** Minimal HTTP response holder. */
    private record Response(int status, String body) {
    }
}

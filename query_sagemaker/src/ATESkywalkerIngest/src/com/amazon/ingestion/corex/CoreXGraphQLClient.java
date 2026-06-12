package com.amazon.ingestion.corex;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import software.amazon.awssdk.auth.credentials.AwsSessionCredentials;
import software.amazon.awssdk.http.ContentStreamProvider;
import software.amazon.awssdk.http.SdkHttpFullRequest;
import software.amazon.awssdk.http.SdkHttpMethod;
import software.amazon.awssdk.http.auth.aws.signer.AwsV4HttpSigner;
import software.amazon.awssdk.http.auth.spi.signer.SignedRequest;
import software.amazon.awssdk.regions.Region;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Optional;

/**
 * Sends SigV4-signed GraphQL POST requests to COREx.
 *
 * One instance per Lambda cold start. Holds an HttpClient and an AwsV4HttpSigner,
 * both safe to reuse across invocations. Credentials are pulled fresh from the
 * CoreXCredentialsProvider on every call so expiration is handled centrally.
 *
 * Callers hand in the full GraphQL request body (already JSON-serialized) and the
 * path (e.g. /search/graphql or /infoarch/graphql). The client returns the raw
 * response body as a parsed JsonNode; callers extract what they need.
 */
public final class CoreXGraphQLClient {

    private static final Logger LOGGER = LogManager.getLogger(CoreXGraphQLClient.class);

    private static final Region SIGNING_REGION = Region.US_WEST_2;
    private static final String SIGNING_SERVICE = "execute-api";
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(10);

    private static final ObjectMapper JSON = new ObjectMapper();

    private final HttpClient http;
    private final AwsV4HttpSigner signer;
    private final CoreXCredentialsProvider credentials;
    private final CoreXSecretReader secretReader;
    private final String host;

    /**
     * Create a COREx GraphQL client.
     *
     * @param credentials  source of AssumeRole-cached session credentials.
     * @param secretReader source of the x-api-key header value.
     * @param host         stage-specific hostname, e.g. corex-api.beta.corex.pxt.amazon.dev.
     */
    public CoreXGraphQLClient(
            CoreXCredentialsProvider credentials,
            CoreXSecretReader secretReader,
            String host) {
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .build();
        this.signer = AwsV4HttpSigner.create();
        this.credentials = credentials;
        this.secretReader = secretReader;
        this.host = host;
    }

    /**
     * POST a GraphQL request to the given path.
     *
     * @param path       request path, e.g. /search/graphql.
     * @param graphqlBody the request body as JSON already serialized.
     * @return the parsed response body.
     * @throws CoreXRequestException on transport failure, non-2xx response, or unparseable body.
     */
    public JsonNode post(String path, String graphqlBody) {
        byte[] bodyBytes = graphqlBody.getBytes(StandardCharsets.UTF_8);
        CoreXSecret secret = secretReader.read();
        AwsSessionCredentials sessionCreds = credentials.credentials();

        SdkHttpFullRequest unsigned = SdkHttpFullRequest.builder()
                .method(SdkHttpMethod.POST)
                .protocol("https")
                .host(host)
                .encodedPath(path)
                .putHeader("Content-Type", "application/json")
                .putHeader("x-api-key", secret.apiKey())
                .putHeader("host", host)
                .contentStreamProvider(ContentStreamProvider.fromUtf8String(graphqlBody))
                .build();

        SignedRequest signed = signer.sign(r -> r
                .identity(sessionCreds)
                .request(unsigned)
                .payload(ContentStreamProvider.fromUtf8String(graphqlBody))
                .putProperty(AwsV4HttpSigner.SERVICE_SIGNING_NAME, SIGNING_SERVICE)
                .putProperty(AwsV4HttpSigner.REGION_NAME, SIGNING_REGION.id()));

        HttpRequest.Builder httpBuilder = HttpRequest.newBuilder()
                .uri(URI.create("https://" + host + path))
                .timeout(REQUEST_TIMEOUT)
                .POST(HttpRequest.BodyPublishers.ofByteArray(bodyBytes));

        signed.request().headers().forEach((name, values) -> {
            if (isRestrictedHeader(name)) {
                return;
            }
            for (String value : values) {
                httpBuilder.header(name, value);
            }
        });

        HttpRequest req = httpBuilder.build();
        HttpResponse<byte[]> resp;
        try {
            resp = http.send(req, HttpResponse.BodyHandlers.ofByteArray());
        } catch (Exception e) {
            throw new CoreXRequestException("Transport failure calling COREx " + path, e);
        }

        if (resp.statusCode() / 100 != 2) {
            String preview = Optional.ofNullable(resp.body())
                    .map(b -> new String(b, StandardCharsets.UTF_8))
                    .orElse("");
            throw new CoreXRequestException(
                    "COREx " + path + " returned HTTP " + resp.statusCode() + ": " + preview);
        }

        try {
            return JSON.readTree(resp.body());
        } catch (Exception e) {
            throw new CoreXRequestException("Failed to parse COREx response from " + path, e);
        }
    }

    // The JDK HttpClient forbids callers from setting certain headers that it manages itself.
    // The signed request contains them as part of the signature, so we re-emit the signature-bearing
    // ones but drop the transport-managed ones to avoid an IllegalArgumentException.
    private static boolean isRestrictedHeader(String name) {
        String lower = name.toLowerCase();
        return lower.equals("content-length")
                || lower.equals("host")
                || lower.equals("connection")
                || lower.equals("upgrade")
                || lower.equals("expect");
    }
}

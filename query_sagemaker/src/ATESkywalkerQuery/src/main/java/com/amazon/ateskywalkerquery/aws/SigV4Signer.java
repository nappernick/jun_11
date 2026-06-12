package com.amazon.ateskywalkerquery.aws;

import software.amazon.awssdk.auth.credentials.AwsCredentials;
import software.amazon.awssdk.auth.credentials.AwsSessionCredentials;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.Map;
import java.util.TreeMap;

/**
 * Minimal AWS SigV4 request signer producing the headers required to call AWS HTTP
 * APIs (aoss, bedrock, sagemaker) directly via {@code java.net.http}. JDK-crypto only.
 */
public final class SigV4Signer {
    private static final String ALGORITHM = "AWS4-HMAC-SHA256";
    private static final DateTimeFormatter AMZ_DATE =
        DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmss'Z'").withZone(ZoneOffset.UTC);
    private static final DateTimeFormatter DATE_STAMP =
        DateTimeFormatter.ofPattern("yyyyMMdd").withZone(ZoneOffset.UTC);
    private static final String CONTENT_TYPE = "application/json";

    private SigV4Signer() {}

    /** Identifies the AWS service and region a request is signed for. */
    public record AwsServiceTarget(String service, String region) {}

    /**
     * Signs a request and returns the headers to attach (Authorization, X-Amz-*).
     *
     * @param method HTTP method
     * @param uri request URI (host + path)
     * @param query query parameters (may be empty)
     * @param body request body
     * @param target AWS service + region the request is signed for
     * @param creds resolved AWS credentials
     * @param when signing timestamp
     * @return headers to set on the outgoing request
     */
    public static Map<String, String> sign(
        String method,
        URI uri,
        Map<String, String> query,
        String body,
        AwsServiceTarget target,
        AwsCredentials creds,
        Instant when) {
        String service = target.service();
        String region = target.region();
        String amzDate = AMZ_DATE.format(when);
        String dateStamp = DATE_STAMP.format(when);
        String payloadHash = hex(sha256(body));
        String token = (creds instanceof AwsSessionCredentials s) ? s.sessionToken() : null;

        TreeMap<String, String> signed = new TreeMap<>();
        signed.put("content-type", CONTENT_TYPE);
        signed.put("host", uri.getHost());
        signed.put("x-amz-content-sha256", payloadHash);
        signed.put("x-amz-date", amzDate);
        if (token != null) {
            signed.put("x-amz-security-token", token);
        }

        StringBuilder canonicalHeaders = new StringBuilder();
        StringBuilder signedHeaders = new StringBuilder();
        for (Map.Entry<String, String> e : signed.entrySet()) {
            canonicalHeaders
                .append(e.getKey())
                .append(':')
                .append(e.getValue().trim())
                .append('\n');
            if (signedHeaders.length() > 0) {
                signedHeaders.append(';');
            }
            signedHeaders.append(e.getKey());
        }

        String canonicalRequest = method
            + '\n'
            + canonicalPath(uri.getPath())
            + '\n'
            + canonicalQuery(query)
            + '\n'
            + canonicalHeaders
            + '\n'
            + signedHeaders
            + '\n'
            + payloadHash;
        String scope = dateStamp + '/' + region + '/' + service + "/aws4_request";
        String stringToSign = ALGORITHM + '\n' + amzDate + '\n' + scope + '\n' + hex(sha256(canonicalRequest));
        String signature = hex(hmac(signingKey(creds.secretAccessKey(), dateStamp, region, service), stringToSign));

        String authorization = ALGORITHM + " Credential=" + creds.accessKeyId() + '/' + scope + ", SignedHeaders="
            + signedHeaders + ", Signature=" + signature;

        TreeMap<String, String> out = new TreeMap<>();
        out.put("Authorization", authorization);
        out.put("X-Amz-Date", amzDate);
        out.put("X-Amz-Content-Sha256", payloadHash);
        out.put("Content-Type", CONTENT_TYPE);
        if (token != null) {
            out.put("X-Amz-Security-Token", token);
        }
        return out;
    }

    static byte[] signingKey(String secret, String dateStamp, String region, String service) {
        byte[] key = ("AWS4" + secret).getBytes(StandardCharsets.UTF_8);
        key = hmac(key, dateStamp);
        key = hmac(key, region);
        key = hmac(key, service);
        return hmac(key, "aws4_request");
    }

    private static String canonicalPath(String path) {
        return (path == null || path.isEmpty()) ? "/" : path;
    }

    private static String canonicalQuery(Map<String, String> query) {
        if (query == null || query.isEmpty()) {
            return "";
        }
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, String> e : new TreeMap<>(query).entrySet()) {
            if (sb.length() > 0) {
                sb.append('&');
            }
            sb.append(uriEncode(e.getKey())).append('=').append(uriEncode(e.getValue()));
        }
        return sb.toString();
    }

    private static String uriEncode(String s) {
        StringBuilder sb = new StringBuilder();
        for (byte b : s.getBytes(StandardCharsets.UTF_8)) {
            char c = (char) (b & 0xff);
            boolean unreserved = (c >= 'A' && c <= 'Z')
                || (c >= 'a' && c <= 'z')
                || (c >= '0' && c <= '9')
                || c == '-'
                || c == '_'
                || c == '.'
                || c == '~';
            if (unreserved) {
                sb.append(c);
            } else {
                sb.append('%').append(String.format("%02X", b & 0xff));
            }
        }
        return sb.toString();
    }

    private static byte[] sha256(String s) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static byte[] hmac(byte[] key, String data) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            return mac.doFinal(data.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static String hex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            sb.append(String.format("%02x", b & 0xff));
        }
        return sb.toString();
    }
}

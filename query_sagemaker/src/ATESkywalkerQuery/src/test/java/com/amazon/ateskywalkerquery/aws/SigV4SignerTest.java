package com.amazon.ateskywalkerquery.aws;

import org.junit.jupiter.api.Test;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;

import java.net.URI;
import java.time.Instant;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

class SigV4SignerTest {

    /** AWS-documented "deriving the signing key" example vector. */
    @Test
    void signingKeyMatchesAwsExampleVector() {
        byte[] key = SigV4Signer.signingKey("wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20120215", "us-east-1", "iam");
        StringBuilder hex = new StringBuilder();
        for (byte b : key) {
            hex.append(String.format("%02x", b & 0xff));
        }
        assertEquals("f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d", hex.toString());
    }

    @Test
    void signProducesWellFormedAuthorizationHeader() {
        Map<String, String> headers = SigV4Signer.sign(
            "POST",
            URI.create("https://abc.us-west-2.aoss.amazonaws.com/idx/_search"),
            Map.of("search_pipeline", "skywalker-faq-hybrid"),
            "{}",
            new SigV4Signer.AwsServiceTarget("aoss", "us-west-2"),
            AwsBasicCredentials.create("AKIDEXAMPLE", "secret"),
            Instant.parse("2026-06-03T00:00:00Z"));
        String auth = headers.get("Authorization");
        org.junit.jupiter.api.Assertions.assertTrue(
            auth.startsWith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260603/us-west-2/aoss/aws4_request"));
        org.junit.jupiter.api.Assertions.assertTrue(
            auth.contains("SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date"));
        assertEquals(headers.get("X-Amz-Date"), "20260603T000000Z");
    }
}

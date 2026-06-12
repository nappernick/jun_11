package com.amazon.ingestion.enumeration;

import com.amazon.ingestion.corex.CoreXGraphQLClient;
import com.amazon.ingestion.corex.CoreXRequestException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * SearchContent-backed CoreXEnumerator.
 *
 * Filters by the configured FAQ topic UUID and by status=PUBLISHED so drafts never
 * enter the corpus. Paginates through results in pages of 50 (COREx's cap).
 *
 * The snapshot marker is the hex sha-256 of a sorted-by-nodeId concatenation of
 * "nodeId|lastModifiedDate" lines. Any publish, retraction, or modification shifts
 * the hash.
 */
public final class CoreXSearchEnumerator implements CoreXEnumerator {

    private static final Logger LOGGER = LogManager.getLogger(CoreXSearchEnumerator.class);

    private static final int PAGE_SIZE = 50;
    private static final String SEARCH_PATH = "/search/graphql";
    private static final String SEARCH_QUERY =
            "query searchContent($search: SearchInput!) {"
                    + " searchContent(search: $search) {"
                    + " status statusCode errorMessage"
                    + " payload { data { nodeId lastModifiedDate } pagination { limit pageNumber total } }"
                    + " } }";

    private static final ObjectMapper JSON = new ObjectMapper();

    private final CoreXGraphQLClient client;
    private final String faqTopicId;

    public CoreXSearchEnumerator(CoreXGraphQLClient client, String faqTopicId) {
        this.client = client;
        if (faqTopicId == null || faqTopicId.isBlank()) {
            throw new IllegalArgumentException(
                    "faqTopicId must be non-blank; populate COREX_FAQ_TOPIC_ID env var");
        }
        this.faqTopicId = faqTopicId;
    }

    @Override
    public List<EnumeratedNode> enumerate() {
        List<EnumeratedNode> out = new ArrayList<>();
        int pageNumber = 1;
        while (true) {
            JsonNode payload = callSearch(pageNumber);
            ArrayNode data = (ArrayNode) payload.path("data");
            for (JsonNode row : data) {
                String nodeId = row.path("nodeId").asText(null);
                String lastModified = row.path("lastModifiedDate").asText(null);
                if (nodeId == null || lastModified == null) {
                    LOGGER.warn("Skipping malformed enumeration row: {}", row);
                    continue;
                }
                out.add(new EnumeratedNode(nodeId, lastModified));
            }
            int total = payload.path("pagination").path("total").asInt(0);
            if (pageNumber * PAGE_SIZE >= total) {
                break;
            }
            pageNumber++;
        }
        LOGGER.info("Enumerated {} FAQ nodes from COREx", out.size());
        return List.copyOf(out);
    }

    @Override
    public String computeMarker(List<EnumeratedNode> nodes) {
        List<EnumeratedNode> sorted = new ArrayList<>(nodes);
        sorted.sort(Comparator.comparing(EnumeratedNode::nodeId));
        try {
            MessageDigest sha = MessageDigest.getInstance("SHA-256");
            for (EnumeratedNode n : sorted) {
                sha.update(n.nodeId().getBytes(StandardCharsets.UTF_8));
                sha.update((byte) '|');
                sha.update(n.lastModifiedDate().getBytes(StandardCharsets.UTF_8));
                sha.update((byte) '\n');
            }
            byte[] digest = sha.digest();
            StringBuilder sb = new StringBuilder("sha256:");
            for (byte b : digest) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 not available", e);
        }
    }

    private JsonNode callSearch(int pageNumber) {
        ObjectNode variables = JSON.createObjectNode();
        ObjectNode search = variables.putObject("search");
        search.put("query", "");
        ArrayNode filters = search.putArray("filters");

        ObjectNode topicFilter = filters.addObject();
        topicFilter.put("id", "topics");
        topicFilter.putArray("value").add(faqTopicId);
        topicFilter.put("columnType", "ARRAY");
        topicFilter.put("group", "SYSTEMFIELDS");

        ObjectNode statusFilter = filters.addObject();
        statusFilter.put("id", "status");
        statusFilter.putArray("value").add("PUBLISHED");
        statusFilter.put("columnType", "STRING");
        statusFilter.put("group", "SYSTEMFIELDS");

        ObjectNode pagination = search.putObject("pagination");
        pagination.put("limit", PAGE_SIZE);
        pagination.put("pageNumber", pageNumber);

        search.putNull("sorting");

        ObjectNode body = JSON.createObjectNode();
        body.put("query", SEARCH_QUERY);
        body.set("variables", variables);

        try {
            JsonNode response = client.post(SEARCH_PATH, JSON.writeValueAsString(body));
            JsonNode root = response.path("data").path("searchContent");
            String status = root.path("status").asText();
            if (!"SUCCESS".equals(status)) {
                String err = root.path("errorMessage").asText("");
                throw new CoreXRequestException(
                        "SearchContent returned status=" + status + " error=" + err);
            }
            return root.path("payload");
        } catch (CoreXRequestException e) {
            throw e;
        } catch (Exception e) {
            throw new CoreXRequestException("Failed to build or parse SearchContent", e);
        }
    }
}

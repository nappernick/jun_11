package com.amazon.ingestion.enumeration;

import com.amazon.ingestion.corex.CoreXGraphQLClient;
import com.amazon.ingestion.corex.CoreXRequestException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * SearchContent-backed CoreXEnumerator.
 *
 * Filters by the configured COREx domain owner Bindle ID. Paginates through
 * results in pages of 50 (COREx's cap).
 *
 * The snapshot marker is the single most-recent {@code lastModifiedDate} across the corpus
 * (R5): one date in one place. Any publish, retraction, or modification advances some node's
 * lastModifiedDate and therefore the max, which triggers a full rebuild.
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

    /**
     * Canonical UUID shape (8-4-4-4-12 hex). COREx node IDs are always UUIDs, so the enumerator
     * accepts a row only when its {@code nodeId} matches this exactly. This is a Sec-team guard:
     * if COREx were compromised or a response tampered with, a non-UUID nodeId (path or GraphQL
     * injection, control characters, an otherwise-shaped identifier) is precisely the value we
     * must not propagate into downstream fetch/index calls — reject it rather than trust it.
     */
    private static final java.util.regex.Pattern UUID_PATTERN = java.util.regex.Pattern.compile(
            "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$");

    private final CoreXGraphQLClient client;
    private final String domainOwnerId;

    public CoreXSearchEnumerator(CoreXGraphQLClient client, String domainOwnerId) {
        this.client = client;
        if (domainOwnerId == null || domainOwnerId.isBlank()) {
            throw new IllegalArgumentException(
                    "domainOwnerId must be non-blank; populate COREX_DOMAIN_OWNER_ID env var");
        }
        this.domainOwnerId = domainOwnerId;
    }

    @Override
    public List<EnumeratedNode> enumerate() {
        List<EnumeratedNode> out = new ArrayList<>();
        int pageNumber = 1;
        while (true) {
            JsonNode payload = callSearch(pageNumber);
            ArrayNode data = (ArrayNode) payload.path("data");
            for (JsonNode row : data) {
                String nodeId = trimToNull(row.path("nodeId").asText(null));
                String lastModified = trimToNull(row.path("lastModifiedDate").asText(null));
                // Security validation (Sec team): accept a row ONLY when nodeId is a well-formed
                // UUID. COREx node IDs are always UUIDs, so anything else is untrusted — a sign
                // of a compromised or tampered response — and must not flow into downstream
                // fetch/index calls. Reject and log rather than enumerate it.
                if (nodeId == null || !UUID_PATTERN.matcher(nodeId).matches()) {
                    LOGGER.warn(
                            "Skipping enumeration row with missing or non-UUID nodeId (rejected as "
                                    + "untrusted; COREx node IDs must be UUIDs): {}",
                            row);
                    continue;
                }
                // A node must also carry a non-blank lastModifiedDate; without it we cannot date
                // the node and it would corrupt the high-water marker.
                if (lastModified == null) {
                    LOGGER.warn(
                            "Skipping enumeration row with missing/blank lastModifiedDate: {}", row);
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
        LOGGER.info("Enumerated {} COREx nodes for domainOwner={}", out.size(), domainOwnerId);
        return List.copyOf(out);
    }

    @Override
    public String computeMarker(List<EnumeratedNode> nodes) {
        // The high-water mark is the single most-recent lastModifiedDate across the corpus
        // (R5). Any changed fragment advances some node's lastModifiedDate, which advances
        // the max, which triggers a full rebuild. ISO-8601 timestamps compare correctly as
        // strings, so a lexical max is also the chronological max.
        return nodes.stream()
                .map(EnumeratedNode::lastModifiedDate)
                .filter(d -> d != null && !d.isBlank())
                .max(Comparator.naturalOrder())
                .orElse("");
    }

    /** Trim a possibly-null string, returning null when it is null or blank after trimming. */
    private static String trimToNull(String value) {
        if (value == null) {
            return null;
        }
        String trimmed = value.trim();
        return trimmed.isEmpty() ? null : trimmed;
    }

    private JsonNode callSearch(int pageNumber) {
        ObjectNode variables = JSON.createObjectNode();
        ObjectNode search = variables.putObject("search");
        search.put("query", "");
        ArrayNode filters = search.putArray("filters");

        ObjectNode ownerFilter = filters.addObject();
        ownerFilter.put("id", "domainOwner");
        ownerFilter.putArray("value").add(domainOwnerId);
        ownerFilter.put("columnType", "STRING");
        ownerFilter.put("group", "GLOBALSTATE");

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

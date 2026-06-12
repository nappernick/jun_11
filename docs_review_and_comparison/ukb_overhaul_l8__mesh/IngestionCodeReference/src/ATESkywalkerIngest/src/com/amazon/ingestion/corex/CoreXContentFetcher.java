package com.amazon.ingestion.corex;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.util.ArrayList;
import java.util.List;

/**
 * Fetches full COREx node bodies through the {@code getContentNodes} (plural) endpoint.
 *
 * <h2>Endpoint (proven against live COREx, 2026-06-03)</h2>
 * The singular {@code getContentNode} at {@code /infoarch/graphql} returns {@code {"data":null}}
 * for valid node IDs — it does not serve bodies on this stage. The working call is the
 * <b>plural</b> {@code getContentNodes(input: GetContentNodesInput!)} at
 * {@code /infoarch/getContentNodes/graphql}, which returns
 * {@code data.getContentNodes.payload.nodes[]}. We pass exactly one nodeId per call (the
 * pipeline is one-node-at-a-time, R: one node = one fragment) and read {@code nodes[0]}.
 *
 * <h2>Field shape</h2>
 * Each node carries {@code nodeId, title, content, version, status, language, geography,
 * topics, metadata, lastModifiedDate, isEmbeddable} plus {@code globalState} (domainOwner /
 * managedBy). {@code content} and {@code metadata} are JSON <em>strings</em>: {@code content}
 * is the PlateJS RTE_V2 envelope (see {@link CoreXTextExtractor}); {@code metadata} carries the
 * versioned custom fields the scope mapper/assembler need. Beta metadata is sparse/unrepresentative
 * — beta proves the mechanism, prod defines the values (see CorpusSchema).
 */
public final class CoreXContentFetcher implements ContentNodeFetcher {

    private static final String GET_CONTENT_NODES_PATH = "/infoarch/getContentNodes/graphql";
    private static final String GET_CONTENT_NODES_QUERY =
            "query GetContentNodes($input: GetContentNodesInput!) {"
                    + " getContentNodes(input: $input) {"
                    + " status statusCode errorMessage"
                    + " payload {"
                    + " nodes {"
                    + " nodeId title content version status language geography topics metadata"
                    + " lastModifiedDate isEmbeddable globalState { domainOwner managedBy }"
                    + " }"
                    + " unprocessedNodes { nodeId }"
                    + " }"
                    + " }"
                    + "}";

    private static final ObjectMapper JSON = new ObjectMapper();

    private final CoreXGraphQLClient client;

    public CoreXContentFetcher(CoreXGraphQLClient client) {
        this.client = client;
    }

    @Override
    public CoreXContentNode fetch(String nodeId) {
        // input: { nodes: [{ nodeId }], returnFieldVersions: true, returnTaxonomyValues: LABEL }
        ObjectNode input = JSON.createObjectNode();
        input.putArray("nodes").addObject().put("nodeId", nodeId);
        input.put("returnFieldVersions", true);
        input.put("returnTaxonomyValues", "LABEL");

        ObjectNode variables = JSON.createObjectNode();
        variables.set("input", input);

        ObjectNode body = JSON.createObjectNode();
        body.put("query", GET_CONTENT_NODES_QUERY);
        body.set("variables", variables);

        try {
            JsonNode response = client.post(GET_CONTENT_NODES_PATH, JSON.writeValueAsString(body));

            JsonNode errors = response.path("errors");
            if (errors.isArray() && !errors.isEmpty()) {
                throw new CoreXRequestException("getContentNodes returned GraphQL errors: " + errors);
            }

            JsonNode root = response.path("data").path("getContentNodes");
            String status = root.path("status").asText("");
            if (!status.isEmpty() && !"SUCCESS".equals(status)) {
                throw new CoreXRequestException(
                        "getContentNodes returned status=" + status
                                + " error=" + root.path("errorMessage").asText(""));
            }

            JsonNode nodes = root.path("payload").path("nodes");
            if (!nodes.isArray() || nodes.isEmpty()) {
                throw new CoreXRequestException("getContentNodes returned no node for nodeId=" + nodeId);
            }
            JsonNode node = nodes.get(0);

            JsonNode metadata = parseJsonString(node.path("metadata"));
            JsonNode bodyContent = parseJsonString(node.path("content"));
            JsonNode globalState = node.path("globalState");
            return new CoreXContentNode(
                    node.path("nodeId").asText(nodeId),
                    node.path("version").asText(""),
                    node.path("status").asText(""),
                    stringList(node.path("geography")),
                    stringList(node.path("topics")),
                    metadata,
                    bodyContent,
                    globalState.path("domainOwner").asText(""),
                    globalState.path("managedBy").asText(""),
                    node.path("isEmbeddable").asBoolean(true),
                    node);
        } catch (CoreXRequestException e) {
            throw e;
        } catch (Exception e) {
            throw new CoreXRequestException("Failed to fetch COREx node " + nodeId, e);
        }
    }

    private static JsonNode parseJsonString(JsonNode node) throws Exception {
        if (node == null || node.isMissingNode() || node.isNull()) {
            return JSON.createObjectNode();
        }
        if (node.isTextual()) {
            String raw = node.asText();
            if (raw == null || raw.isBlank()) {
                return JSON.createObjectNode();
            }
            return JSON.readTree(raw);
        }
        return node;
    }

    private static List<String> stringList(JsonNode node) {
        if (!node.isArray()) {
            return List.of();
        }
        List<String> out = new ArrayList<>();
        for (JsonNode item : node) {
            String value = item.asText("");
            if (!value.isBlank()) {
                out.add(value);
            }
        }
        return List.copyOf(out);
    }
}

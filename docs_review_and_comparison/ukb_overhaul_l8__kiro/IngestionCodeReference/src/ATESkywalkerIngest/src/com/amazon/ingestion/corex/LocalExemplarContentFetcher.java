package com.amazon.ingestion.corex;

import com.amazon.ingestion.schema.CorpusSchema;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * A {@link ContentNodeFetcher} backed by a bundled exemplar of <b>real COREx prod</b> records
 * (src/exemplar/skywalker-faq-exemplar.jsonl), not the live COREx API.
 *
 * Why this exists: COREx has no write API, and beta data is unrepresentative of prod, so the
 * only way to prove the downstream write path (scope mapping → embed → AOSS write → read-back)
 * on data we know is correct is to feed real prod records through the real pipeline. This is
 * the "exemplar JSON we keep locally" — real text and real scope values; the only approximated
 * pieces are our custom field names (e.g. contentType), which a standard-fields export could
 * not include.
 *
 * Selection is env-gated in the Processor (default is the live COREx fetcher); this is a
 * dev/proof path, never the production default.
 *
 * Each exemplar record carries the prod fields the pipeline needs:
 * {@code nodeId, title, geography, jobLevel, employeeClass, topics, markdown, ...}. We map
 * {@code markdown} into the node's content as a textual node (the extractor detects markdown),
 * and pack {@code jobLevel}/{@code employeeClass} into metadata as COREx returns them
 * (comma-joined strings), so {@link com.amazon.ingestion.processor.ScopeMapper} reads them
 * exactly as it would from the live API.
 */
public final class LocalExemplarContentFetcher implements ContentNodeFetcher {

    private static final Logger LOGGER = LogManager.getLogger(LocalExemplarContentFetcher.class);
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String RESOURCE = "/exemplar/skywalker-faq-exemplar.jsonl";

    private final Map<String, CoreXContentNode> byNodeId = new LinkedHashMap<>();

    public LocalExemplarContentFetcher() {
        this(RESOURCE);
    }

    LocalExemplarContentFetcher(String resourcePath) {
        load(resourcePath);
        LOGGER.info("Loaded {} exemplar prod records from {}", byNodeId.size(), resourcePath);
    }

    private void load(String resourcePath) {
        try (InputStream in = LocalExemplarContentFetcher.class.getResourceAsStream(resourcePath)) {
            if (in == null) {
                throw new IllegalStateException("Exemplar resource not found on classpath: " + resourcePath);
            }
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    if (line.isBlank()) {
                        continue;
                    }
                    JsonNode r = JSON.readTree(line);
                    CoreXContentNode node = toNode(r);
                    byNodeId.put(node.nodeId(), node);
                }
            }
        } catch (Exception e) {
            throw new IllegalStateException("Failed to load exemplar resource " + resourcePath, e);
        }
    }

    private static CoreXContentNode toNode(JsonNode r) throws Exception {
        String nodeId = r.path("nodeId").asText();

        // The exemplar carries the REAL prod metadata JSON-string (system_* arrays, versioned
        // custom keys). Parse it exactly as CoreXContentFetcher would parse a live node.
        JsonNode metadata = parseMaybeJsonString(r.get("metadata"));

        // geography is a real top-level array; topics likewise.
        java.util.List<String> geography = stringList(r.get("geography"));
        java.util.List<String> topics = stringList(r.get("topics"));

        // Content body is the prod markdown as a textual node; the extractor detects markdown.
        JsonNode content = JSON.getNodeFactory().textNode(r.path("markdown").asText(""));

        // Build a raw top-level envelope so MetadataAssembler preserves all top-level fields,
        // mirroring what searchContent/getContentNode return (minus the body content).
        ObjectNode raw = JSON.createObjectNode();
        raw.put("nodeId", nodeId);
        raw.put("version", r.path("version").asText(""));
        raw.put("status", r.path("status").asText("DRAFT"));
        raw.put("title", r.path("title").asText(""));
        raw.set("geography", JSON.valueToTree(geography));
        raw.set("topics", JSON.valueToTree(topics));
        if (r.has("lastModifiedDate")) {
            raw.set("lastModifiedDate", r.get("lastModifiedDate"));
        }

        return new CoreXContentNode(
                nodeId,
                r.path("version").asText(""),
                r.path("status").asText("DRAFT"),
                geography,
                topics,
                metadata,
                content,
                CorpusSchema.DOMAIN_OWNER,
                "",
                true,
                raw);
    }

    private static JsonNode parseMaybeJsonString(JsonNode node) throws Exception {
        if (node == null || node.isNull()) {
            return JSON.createObjectNode();
        }
        if (node.isTextual()) {
            String raw = node.asText();
            return raw.isBlank() ? JSON.createObjectNode() : JSON.readTree(raw);
        }
        return node;
    }

    private static java.util.List<String> stringList(JsonNode node) {
        if (node == null || !node.isArray()) {
            return java.util.List.of();
        }
        java.util.List<String> out = new java.util.ArrayList<>();
        for (JsonNode item : node) {
            if (item != null && item.isValueNode() && !item.asText().isBlank()) {
                out.add(item.asText());
            }
        }
        return out;
    }

    @Override
    public CoreXContentNode fetch(String nodeId) {
        CoreXContentNode node = byNodeId.get(nodeId);
        if (node == null) {
            throw new CoreXRequestException("No exemplar record for nodeId=" + nodeId);
        }
        return node;
    }

    /**
     * The node IDs available in the exemplar, in file order. Used by the Processor's exemplar
     * mode to know what it can serve and by tests.
     *
     * @return ordered exemplar node IDs.
     */
    public java.util.List<String> nodeIds() {
        return java.util.List.copyOf(byNodeId.keySet());
    }
}

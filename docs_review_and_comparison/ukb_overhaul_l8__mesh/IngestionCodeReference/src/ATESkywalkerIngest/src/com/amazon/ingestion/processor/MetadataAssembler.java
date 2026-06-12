package com.amazon.ingestion.processor;

import com.amazon.ingestion.corex.CoreXContentNode;
import com.amazon.ingestion.schema.CorpusSchema;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.util.Map;

/**
 * Assembles the full {@code source_metadata} blob stored on each fragment document.
 *
 * Goal (per direction): the embedding input is solo body text; <b>everything else</b> about a
 * node is preserved as metadata so nothing is lost and new filters can be added later without
 * re-ingesting.
 *
 * What goes in:
 * <ul>
 *   <li><b>All top-level API fields</b> from the node's raw object — except the body
 *       {@code content} (embedded separately, not duplicated into metadata) and the raw
 *       {@code metadata} blob (parsed and merged in below rather than nested as a string).</li>
 *   <li><b>All custom metadata fields, version-resolved:</b> versioned families
 *       ({@code base-N}) collapse to the highest N (newest), keyed by base name
 *       (e.g. {@code content-type-16} → {@code content-type}); unversioned {@code system_*}
 *       keys pass through as-is. See {@link CorpusSchema#resolveLatestVersions}.</li>
 *   <li><b>A resolved {@code content_type}</b> scalar: the value of the highest
 *       {@code content-type-N}. (Whenever that value is "Skywalker FAQ" the node belongs to
 *       this corpus.)</li>
 * </ul>
 *
 * The result is stored as a single {@code flat_object} field in OpenSearch, so the numbered
 * keys never cause dynamic-mapping explosion.
 */
public final class MetadataAssembler {

    private static final ObjectMapper JSON = new ObjectMapper();

    /** Body field name on the raw top-level object; excluded from metadata (it is embedded). */
    private static final String RAW_CONTENT_FIELD = "content";
    /** Raw metadata-blob field on the top-level object; parsed and merged, not nested raw. */
    private static final String RAW_METADATA_FIELD = "metadata";

    /**
     * Build the source_metadata object for a node.
     *
     * @param node the COREx node (uses {@code raw} when present, else the typed fields).
     * @return an ObjectNode holding all preserved metadata, plus a resolved content_type.
     */
    public ObjectNode assemble(CoreXContentNode node) {
        ObjectNode out = JSON.createObjectNode();

        // 1) All top-level fields (except body content and the raw metadata blob).
        JsonNode raw = node.raw();
        if (raw != null && raw.isObject()) {
            raw.fields().forEachRemaining(e -> {
                String name = e.getKey();
                if (name.equals(RAW_CONTENT_FIELD) || name.equals(RAW_METADATA_FIELD)) {
                    return;
                }
                out.set(name, e.getValue());
            });
        } else {
            // No raw envelope (e.g. unit-test nodes): fall back to the typed top-level fields.
            out.put("nodeId", node.nodeId());
            out.put("version", node.version());
            out.put("status", node.status());
            out.set("geography", JSON.valueToTree(node.geography()));
            out.set("topics", JSON.valueToTree(node.topics()));
        }

        // 2) Custom metadata, version-resolved (highest version per base name).
        JsonNode metadata = node.metadata();
        if (metadata != null && metadata.isObject()) {
            java.util.List<String> keys = new java.util.ArrayList<>();
            metadata.fieldNames().forEachRemaining(keys::add);
            Map<String, String> latest = CorpusSchema.resolveLatestVersions(keys);
            for (Map.Entry<String, String> e : latest.entrySet()) {
                // Store under the base name so downstream is version-agnostic.
                out.set(e.getKey(), metadata.get(e.getValue()));
            }
        }

        // 3) Resolved content_type scalar (first value of the highest content-type-N).
        String contentType = resolveContentType(node);
        if (contentType != null) {
            out.put("content_type", contentType);
        }

        return out;
    }

    /**
     * Resolve the node's content type: the (first) value of the highest-versioned
     * {@code content-type-N} metadata field, version-agnostic.
     *
     * @param node the COREx node.
     * @return the content-type value, or null when absent.
     */
    public static String resolveContentType(CoreXContentNode node) {
        JsonNode metadata = node.metadata();
        if (metadata == null || !metadata.isObject()) {
            return null;
        }
        java.util.List<String> keys = new java.util.ArrayList<>();
        metadata.fieldNames().forEachRemaining(keys::add);
        Map<String, String> latest = CorpusSchema.resolveLatestVersions(keys);
        String rawKey = latest.get(CorpusSchema.CONTENT_TYPE_BASE);
        if (rawKey == null) {
            return null;
        }
        return firstValue(metadata.get(rawKey));
    }

    private static String firstValue(JsonNode field) {
        if (field == null || field.isNull()) {
            return null;
        }
        if (field.isArray()) {
            for (JsonNode item : field) {
                if (item != null && item.isValueNode() && !item.asText().isBlank()) {
                    return item.asText().trim();
                }
            }
            return null;
        }
        String v = field.asText("");
        return v.isBlank() ? null : v.trim();
    }
}

package com.amazon.ingestion.corex;

import com.fasterxml.jackson.databind.JsonNode;

import java.util.List;

/**
 * COREx content node returned by getContentNode / searchContent.
 *
 * The typed fields ({@code geography}, {@code metadata}, ...) drive scope mapping; {@code raw}
 * carries the full top-level API object so the metadata assembler can preserve every field
 * (R: all top-level fields + all custom metadata become document metadata). The body
 * {@code content} is embedded as solo text and is the only thing that goes into the vector.
 */
public record CoreXContentNode(
        String nodeId,
        String version,
        String status,
        List<String> geography,
        List<String> topics,
        JsonNode metadata,
        JsonNode content,
        String domainOwner,
        String managedBy,
        boolean embeddable,
        JsonNode raw) {

    public CoreXContentNode {
        if (nodeId == null || nodeId.isBlank()) {
            throw new IllegalArgumentException("nodeId must be non-blank");
        }
        geography = geography == null ? List.of() : List.copyOf(geography);
        topics = topics == null ? List.of() : List.copyOf(topics);
    }
}

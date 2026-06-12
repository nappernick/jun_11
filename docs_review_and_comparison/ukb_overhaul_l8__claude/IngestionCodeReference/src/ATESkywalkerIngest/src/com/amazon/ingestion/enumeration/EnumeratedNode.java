package com.amazon.ingestion.enumeration;

/**
 * One row of the Poller's phase-1 COREx enumeration.
 *
 * Carries the information needed to decide whether the corpus has changed since the
 * last successful rebuild, without fetching the content body itself.
 *
 * @param nodeId           COREx node UUID.
 * @param lastModifiedDate ISO-8601 timestamp from COREx indicating when the fragment
 *                         was last modified. Used for snapshot-marker computation.
 */
public record EnumeratedNode(String nodeId, String lastModifiedDate) {

    public EnumeratedNode {
        if (nodeId == null || nodeId.isBlank()) {
            throw new IllegalArgumentException("nodeId must be non-blank");
        }
        if (lastModifiedDate == null || lastModifiedDate.isBlank()) {
            throw new IllegalArgumentException("lastModifiedDate must be non-blank");
        }
    }
}

package com.amazon.ingestion.enumeration;

import java.util.List;

/**
 * COREx phase-1 surface: enumerate node IDs and compute the current snapshot marker.
 *
 * Never fetches document bodies. Body-fetch is a Processor concern (phase 2).
 */
public interface CoreXEnumerator {

    /**
     * Enumerate all FAQ fragments currently owned in COREx.
     *
     * @return one EnumeratedNode per fragment, covering every node in the
     *         configured COREx domain owner. Order is not guaranteed.
     */
    List<EnumeratedNode> enumerate();

    /**
     * Compute the high-water mark from the current enumeration: the single most-recent
     * {@code lastModifiedDate} across all nodes (R5). It advances whenever any fragment is
     * published, modified, or retracted, since that shifts some node's lastModifiedDate.
     *
     * @param nodes output of enumerate().
     * @return the max ISO-8601 lastModifiedDate, suitable for SSM persistence; empty string
     *         when the enumeration carries no dates.
     */
    String computeMarker(List<EnumeratedNode> nodes);
}

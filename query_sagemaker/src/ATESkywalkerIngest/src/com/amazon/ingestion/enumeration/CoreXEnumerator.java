package com.amazon.ingestion.enumeration;

import java.util.List;

/**
 * COREx phase-1 surface: enumerate node IDs and compute the current snapshot marker.
 *
 * Never fetches document bodies. Body-fetch is a Processor concern (phase 2).
 */
public interface CoreXEnumerator {

    /**
     * Enumerate all FAQ fragments currently published in COREx.
     *
     * @return one EnumeratedNode per fragment, covering every PUBLISHED node in the
     *         configured FAQ topic. Order is not guaranteed.
     */
    List<EnumeratedNode> enumerate();

    /**
     * Compute a stable marker from the current enumeration that advances whenever the
     * corpus changes. Implementations derive this from the (nodeId, lastModifiedDate)
     * pairs so any publish or retraction shifts the marker.
     *
     * @param nodes output of enumerate().
     * @return opaque marker suitable for SSM persistence.
     */
    String computeMarker(List<EnumeratedNode> nodes);
}

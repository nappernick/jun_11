package com.amazon.ingestion.indexing;

import com.fasterxml.jackson.databind.JsonNode;

import java.util.List;

/**
 * One FAQ evidence fragment, mapped one-to-one to an OpenSearch document.
 *
 * Field shape follows requirements R3/R7/R10:
 * - No {@code title}: these are question/answer-pair fragments; a title is meaningless.
 * - No containment fields ({@code chunk_id}, {@code parent_id}, ...): the chunk-and-reassemble
 *   system is retired. The only cross-Q&amp;A relationship is succession, carried by
 *   {@code followupFragmentIds}, which stays empty until R11 lands.
 * - Scope dimensions {@code country}/{@code level}/{@code role} are keyword arrays holding the
 *   real COREx values ("Global" / "All Job Levels" / "All Employee Classes" are the genuine
 *   "applies to everybody" values, carried in the data — there is no synthetic catch-all
 *   token). Per R10 the processor must not construct a document with an empty scope
 *   dimension; that enforcement lives in FragmentProcessor (skip-and-log), so by the time a
 *   document is built the scope arrays are non-empty.
 * - {@code sourceMetadata} preserves the full COREx metadata (all top-level fields + all
 *   version-resolved custom fields). Only the body text is embedded; this blob is stored as a
 *   {@code flat_object} so the numbered custom keys never explode the index mapping.
 *
 * @param fragmentId          COREx fragment identifier (or nodeId when the node is one fragment).
 * @param sourceId            COREx nodeId; defaults to fragmentId when absent.
 * @param text                fragment body, stored verbatim; participates in BM25.
 * @param sourceUrl           canonical source URL, may be empty.
 * @param policyLinks         associated policy links, may be empty.
 * @param country             geography scope (real COREx values, e.g. ["Global"]).
 * @param level               jobLevel scope (real COREx values, e.g. ["All Job Levels"]).
 * @param role                employeeClass scope (real COREx values).
 * @param corpusVersion       the run's snapshot date marker.
 * @param followupFragmentIds ordered succession pointers; empty until R11.
 * @param embedding           1024-dim Cohere Embed v4 vector.
 * @param sourceMetadata      full preserved COREx metadata (flat_object); may be null/empty.
 * @param contentType         promoted indexed filter field (T18): the resolved content-type
 *                            scalar (e.g. "Skywalker FAQ"), taken from the versioned COREx
 *                            custom key (content-type-NN, highest version wins); a keyword for
 *                            exact-match filtering.
 */
public record FragmentDocument(
        String fragmentId,
        String sourceId,
        String text,
        String sourceUrl,
        List<String> policyLinks,
        List<String> country,
        List<String> level,
        List<String> role,
        String corpusVersion,
        List<String> followupFragmentIds,
        List<Double> embedding,
        JsonNode sourceMetadata,
        String contentType) {

    public FragmentDocument {
        if (fragmentId == null || fragmentId.isBlank()) {
            throw new IllegalArgumentException("fragmentId must be non-blank");
        }
        if (text == null || text.isBlank()) {
            throw new IllegalArgumentException("text must be non-blank");
        }
        sourceId = sourceId == null || sourceId.isBlank() ? fragmentId : sourceId;
        sourceUrl = sourceUrl == null ? "" : sourceUrl;
        policyLinks = policyLinks == null ? List.of() : List.copyOf(policyLinks);
        country = country == null ? List.of() : List.copyOf(country);
        level = level == null ? List.of() : List.copyOf(level);
        role = role == null ? List.of() : List.copyOf(role);
        corpusVersion = corpusVersion == null ? "" : corpusVersion;
        followupFragmentIds = followupFragmentIds == null ? List.of() : List.copyOf(followupFragmentIds);
        embedding = embedding == null ? List.of() : List.copyOf(embedding);
        contentType = contentType == null ? "" : contentType;
    }
}

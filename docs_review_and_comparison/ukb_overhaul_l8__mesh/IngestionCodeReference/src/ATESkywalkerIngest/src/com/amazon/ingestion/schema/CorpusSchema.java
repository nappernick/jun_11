package com.amazon.ingestion.schema;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Persistent, real-data-grounded schema for the Skywalker FAQ corpus.
 *
 * This type crystallizes what we learned empirically from a real COREx prod export of the
 * current top-50 FAQ set (src/source_with_content.jsonl, 56 records). It records, per field:
 * the COREx field name, and the known/acceptable value set where one exists. We keep it in
 * code (and mirror the movable parts to SSM) so we never have to re-derive this by hand.
 *
 * <h2>Two enumeration fixtures (what to pull)</h2>
 * Lookup is owner-first, then sub-type:
 * <ol>
 *   <li>{@link #DOMAIN_OWNER} — the owning team (COREx {@code domainOwner}).</li>
 *   <li>{@link #CONTENT_TYPE} — the FAQ sub-type (COREx custom field {@code contentType},
 *       value/label "Skywalker FAQ"). NOTE: {@code contentType} is a *custom* COREx field; a
 *       standard-fields-only export shows it blank, but the live getContentNode API returns
 *       it. Treated as ground truth.</li>
 * </ol>
 * Both are effectively fixtures (rarely change) but are stored in SSM so they can move
 * without a redeploy; these constants are the defaults.
 *
 * <h2>Metadata model (real prod API shape)</h2>
 * The body we embed is <b>solo text content only</b>. Everything else about a node is
 * preserved as metadata on the document (a {@code flat_object} {@code source_metadata}), so
 * we never lose provenance and can add filters later without a schema migration:
 * <ul>
 *   <li><b>All top-level API fields</b> (title, geography, topics, status, version,
 *       lastModifiedDate, globalState, ...) are treated as metadata — except the body
 *       {@code content} (embedded separately) and the raw {@code metadata} blob (parsed and
 *       merged in).</li>
 *   <li><b>Custom metadata fields are versioned by a numeric suffix</b> ({@code -N}, e.g.
 *       {@code content-type-16}, {@code applicable-policy-0}, {@code entitlement-1}). We key
 *       by the <b>base name</b> (suffix stripped) and take the <b>highest version</b> present
 *       — the newest the content team published. We assume they will not ship arbitrary
 *       breaking changes under a higher number; if a value is malformed for its purpose we
 *       validate before trusting it (e.g. content-type only counts when its value is the
 *       expected label).</li>
 *   <li><b>{@code system_*} fields are not versioned</b> (no numeric suffix); kept as-is.</li>
 * </ul>
 * Scope fields (the three we filter on) come back as JSON <b>arrays</b> inside the metadata
 * blob (e.g. {@code system_job-level: ["All Job Levels"]}); geography/topics/title are
 * top-level. There is no synthetic catch-all token — "applies to everybody" is the real value
 * the data carries ({@link #GEOGRAPHY_GLOBAL} / {@link #JOB_LEVEL_ALL} /
 * {@link #EMPLOYEE_CLASS_ALL}). The AEM placeholder {@link #BLANK_IN_AEM} is stripped.
 *
 * <h2>Required-ness</h2>
 * The three scope dimensions (geography, system_job-level, system_employee-class) are treated
 * as required; a node missing one is skipped-and-logged (R2/R10), not fatal. Measured across
 * the real prod owned set, all three are present on ~100% of nodes, so this is a
 * malformed-node safety net, not a content filter.
 */
public final class CorpusSchema {

    private CorpusSchema() {
    }

    // --- Enumeration fixtures (defaults; SSM-overridable) ---

    /** Owning team (COREx {@code domainOwner}). Skywalker FAQ corpus owner. */
    public static final String DOMAIN_OWNER = "amzn1.abacus.team.looo53floubmzytmswva";

    /**
     * Base name of the custom content-type field. Real prod stores it as a versioned key
     * inside the metadata blob ({@code content-type-NN}, e.g. content-type-16). We resolve by
     * base name + highest version. A node's content type is the value of the highest
     * {@code content-type-N}; whenever that value is {@link #CONTENT_TYPE} ("Skywalker FAQ")
     * the node belongs to this corpus.
     */
    public static final String CONTENT_TYPE_BASE = "content-type";
    public static final String CONTENT_TYPE = "Skywalker FAQ";

    // --- Scope dimension field names (REAL prod COREx metadata keys) ---
    //
    // Pinned from the live prod searchContent API (not the flattened export): scope fields
    // live inside the metadata JSON string under system_* keys, with ARRAY values
    // (e.g. system_job-level = ["All Job Levels"]). geography is a top-level array.

    public static final String FIELD_GEOGRAPHY = "geography";
    /** Real prod metadata key for employee level (export flattened this to "jobLevel"). */
    public static final String FIELD_JOB_LEVEL = "system_job-level";
    /** Real prod metadata key for manager/IC employee class (export flattened to "employeeClass"). */
    public static final String FIELD_EMPLOYEE_CLASS = "system_employee-class";

    // --- "Applies to everybody" values carried in the data ---

    public static final String GEOGRAPHY_GLOBAL = "Global";
    public static final String JOB_LEVEL_ALL = "All Job Levels";
    public static final String EMPLOYEE_CLASS_ALL = "All Employee Classes";

    /** Authoring placeholder appended by AEM; stripped from scope arrays at ingest. */
    public static final String BLANK_IN_AEM = "No Value (Blank in AEM)";

    /**
     * Known jobLevel values observed in real data: the "everybody" marker plus L0..L12.
     * Used to validate that a returned value is a member of the known set (R2/R7).
     */
    public static final Set<String> KNOWN_JOB_LEVELS = Set.of(
            JOB_LEVEL_ALL,
            "L0", "L1", "L2", "L3", "L4", "L5", "L6",
            "L7", "L8", "L9", "L10", "L11", "L12");

    /**
     * Known employeeClass labels observed in real data. Not exhaustive of the COREx
     * taxonomy, but the members seen on the FAQ corpus; extend as new values appear.
     */
    public static final Set<String> KNOWN_EMPLOYEE_CLASSES = Set.of(
            EMPLOYEE_CLASS_ALL,
            "B - Fixed Term Contractor - EU",
            "C - Third Party Consultant",
            "F - Regular Full Time",
            "G - Agency or Temp",
            "H - Regular Part Time - 20 + Hours",
            "I - Intern",
            "J - JV or RC Worker",
            "M - Internal Staffing Solutions",
            "N - Trainee European",
            "P - Apprentice",
            "Q - Field Regular Part Time 20-29",
            "R - Regular Reduced Time",
            "S", "T", "V", "W", "X");

    /**
     * Split a COREx comma-joined scope string into clean values, stripping blanks and the
     * AEM authoring placeholder. Returns an empty list when nothing real remains.
     *
     * @param raw the raw COREx field value (comma-joined), may be null/blank.
     * @return cleaned, de-duplicated scope values in input order.
     */
    public static List<String> splitScope(String raw) {
        if (raw == null || raw.isBlank()) {
            return List.of();
        }
        return cleanScopeValues(java.util.Arrays.stream(raw.split(","))
                .map(String::trim)
                .toList());
    }

    /**
     * Clean already-separated scope values (the real prod shape: metadata scope fields are
     * JSON arrays, e.g. {@code system_job-level: ["All Job Levels"]}). Strips blanks and the
     * AEM authoring placeholder, trims, de-duplicates, preserves order. Does NOT re-split on
     * commas, so a value that legitimately contains a comma is preserved intact.
     *
     * @param values raw scope values (e.g. from a JSON array), may be null/empty.
     * @return cleaned, de-duplicated scope values in input order.
     */
    public static List<String> cleanScopeValues(List<String> values) {
        if (values == null || values.isEmpty()) {
            return List.of();
        }
        return values.stream()
                .filter(v -> v != null)
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .filter(s -> !s.equals(BLANK_IN_AEM))
                .distinct()
                .toList();
    }

    /** Matches a versioned custom metadata key: base name + a trailing {@code -<digits>}. */
    private static final Pattern VERSIONED_KEY = Pattern.compile("^(.*)-(\\d+)$");

    /**
     * Base name of a (possibly versioned) metadata key. {@code content-type-16} → {@code
     * content-type}; {@code system_job-level} (no numeric suffix) → unchanged.
     *
     * @param key a raw metadata key.
     * @return the base name with any trailing {@code -<digits>} removed.
     */
    public static String baseKey(String key) {
        Matcher m = VERSIONED_KEY.matcher(key);
        return m.matches() ? m.group(1) : key;
    }

    /**
     * Numeric version of a versioned key, or -1 when the key has no {@code -<digits>} suffix.
     *
     * @param key a raw metadata key.
     * @return the version number, or -1 if unversioned.
     */
    public static int keyVersion(String key) {
        Matcher m = VERSIONED_KEY.matcher(key);
        return m.matches() ? Integer.parseInt(m.group(2)) : -1;
    }

    /**
     * Collapse a set of raw metadata keys to highest-version-wins, keyed by base name.
     *
     * Versioned fields ({@code -N}) keep only the entry with the greatest N — the newest the
     * content team published — on the assumption that a higher number is a newer,
     * non-breaking version. Unversioned keys (e.g. {@code system_*}) pass through as-is. The
     * returned map preserves first-seen order and maps base name → the winning raw key.
     *
     * @param rawKeys the metadata keys as COREx returned them.
     * @return base name → winning raw key (highest version for versioned families).
     */
    public static Map<String, String> resolveLatestVersions(Iterable<String> rawKeys) {
        Map<String, String> winner = new LinkedHashMap<>();
        Map<String, Integer> bestVersion = new java.util.HashMap<>();
        for (String raw : rawKeys) {
            String base = baseKey(raw);
            int ver = keyVersion(raw);
            Integer best = bestVersion.get(base);
            if (best == null || ver > best) {
                bestVersion.put(base, ver);
                winner.put(base, raw);
            }
        }
        return winner;
    }
}

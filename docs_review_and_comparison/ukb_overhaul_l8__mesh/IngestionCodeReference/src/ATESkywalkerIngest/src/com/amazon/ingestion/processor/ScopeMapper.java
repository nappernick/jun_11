package com.amazon.ingestion.processor;

import com.amazon.ingestion.corex.CoreXContentNode;
import com.amazon.ingestion.schema.CorpusSchema;
import com.fasterxml.jackson.databind.JsonNode;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Maps a COREx node onto the three scope dimensions (R7), grounded in the real COREx prod
 * field shape (see {@link CorpusSchema}). Lookup is owner+sub-type first (enumeration); these
 * three filters scope within that.
 *
 * <ul>
 *   <li><b>country</b> ← {@code geography}. Real values include "Global" (everybody) or
 *       country labels. Comes through the fetcher as a parsed array, but COREx also returns
 *       comma-joined strings, so both are normalized via {@link CorpusSchema#splitScope}.</li>
 *   <li><b>level</b> ← {@code jobLevel} metadata field. "All Job Levels" (everybody) or
 *       L0..L12.</li>
 *   <li><b>role</b> ← {@code employeeClass} metadata field (readable manager/IC label set;
 *       the built-in is {@code system_employee-class}). "All Employee Classes" (everybody)
 *       or specific classes.</li>
 * </ul>
 *
 * There is no synthetic catch-all token: "applies to everybody" is the real value the data
 * carries ("Global" / "All Job Levels" / "All Employee Classes"). The AEM authoring
 * placeholder "No Value (Blank in AEM)" is stripped. The three scope fields are required
 * (R10): a node missing all real values for a dimension yields an empty list, which the
 * processor treats as a skip-worthy data problem rather than silently publishing unscoped
 * evidence.
 */
public final class ScopeMapper {

    private static final Logger LOGGER = LogManager.getLogger(ScopeMapper.class);

    /**
     * Map the geography/country dimension from the node's geography values.
     *
     * @param node the COREx node to map.
     * @return cleaned country/geography scope values (e.g. ["Global"] or country labels).
     */
    public List<String> country(CoreXContentNode node) {
        Set<String> out = new LinkedHashSet<>();
        // geography arrives as a parsed array from the fetcher; each element may itself be a
        // comma-joined string in COREx, so normalize through splitScope.
        for (String g : node.geography()) {
            out.addAll(CorpusSchema.splitScope(g));
        }
        return new ArrayList<>(out);
    }

    /**
     * Map the employee-level dimension from the system_job-level metadata field.
     *
     * @param node the COREx node to map.
     * @return cleaned jobLevel scope values (e.g. ["All Job Levels"] or ["L4","L5"]).
     */
    public List<String> level(CoreXContentNode node) {
        List<String> values = CorpusSchema.cleanScopeValues(metadataValues(node, CorpusSchema.FIELD_JOB_LEVEL));
        if (values.isEmpty()) {
            LOGGER.info("No {} scope found for nodeId={}", CorpusSchema.FIELD_JOB_LEVEL, node.nodeId());
        }
        return values;
    }

    /**
     * Map the manager/IC dimension from the system_employee-class metadata field.
     *
     * @param node the COREx node to map.
     * @return cleaned employeeClass scope values (e.g. ["All Employee Classes"]).
     */
    public List<String> role(CoreXContentNode node) {
        List<String> values = CorpusSchema.cleanScopeValues(metadataValues(node, CorpusSchema.FIELD_EMPLOYEE_CLASS));
        if (values.isEmpty()) {
            LOGGER.info("No {} scope found for nodeId={}", CorpusSchema.FIELD_EMPLOYEE_CLASS, node.nodeId());
        }
        return values;
    }

    /**
     * Read a metadata field as a list of values. The real prod shape stores scope fields as
     * JSON arrays (e.g. {@code system_job-level: ["All Job Levels"]}); a scalar string (or a
     * comma-joined string) is also tolerated for robustness.
     *
     * @param node the COREx node.
     * @param key  the metadata field name.
     * @return the values as a list, empty when absent.
     */
    private static List<String> metadataValues(CoreXContentNode node, String key) {
        JsonNode metadata = node.metadata();
        if (metadata == null || !metadata.has(key)) {
            return List.of();
        }
        JsonNode field = metadata.get(key);
        if (field == null || field.isNull()) {
            return List.of();
        }
        if (field.isArray()) {
            List<String> parts = new ArrayList<>();
            for (JsonNode item : field) {
                if (item != null && item.isValueNode() && !item.asText().isBlank()) {
                    parts.add(item.asText());
                }
            }
            return parts;
        }
        // Scalar (or comma-joined) string fallback: split on commas.
        return CorpusSchema.splitScope(field.asText(""));
    }
}

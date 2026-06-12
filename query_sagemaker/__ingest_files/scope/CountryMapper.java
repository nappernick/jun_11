package com.amazon.ingestion.scope;

import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.InputStream;
import java.util.Map;

/**
 * Maps COREx {@code geography} LABEL values to the canonical country representation stored
 * on each evidence record under the index field {@code country}.
 *
 * <p>Known country names are mapped to their ISO 3166-1 alpha-3 code (e.g.
 * {@code "United States" -> "USA"}). Values that are not countries — the {@code "Global"}
 * sentinel and regional rollups like {@code "LATAM"}, {@code "North America"},
 * {@code "Middle East"}, {@code "Africa"} — are intentionally passed through unchanged, so
 * {@code "Global"} (the "applies everywhere" sentinel) and any non-country scoping value
 * survive into the index for the query-side scope filter to match.
 *
 * <p>The canonical name-to-ISO3 table is loaded from the {@code /country_iso_alpha3.json}
 * classpath resource.
 */
public final class CountryMapper {
    /** The sentinel meaning "applies to every country". Preserved verbatim, never mapped. */
    public static final String SENTINEL = "Global";

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Map<String, String> NAME_TO_ISO3 = load();

    private CountryMapper() {}

    @SuppressWarnings("unchecked")
    private static Map<String, String> load() {
        try (InputStream is = CountryMapper.class.getResourceAsStream("/country_iso_alpha3.json")) {
            if (is == null) {
                throw new IllegalStateException("country_iso_alpha3.json not found on classpath");
            }
            return MAPPER.readValue(is, Map.class);
        } catch (IOException e) {
            throw new IllegalStateException("Failed to load country_iso_alpha3.json", e);
        }
    }

    /**
     * Map a single COREx geography label to its stored {@code country} value.
     *
     * @param geographyLabel a COREx geography LABEL (e.g. "United States", "Global", "LATAM")
     * @return the ISO-3 code for a known country; otherwise the input unchanged (sentinel,
     *     regional rollup, or any value not in the country table). Null/blank input returns null.
     */
    public static String toCountry(String geographyLabel) {
        if (geographyLabel == null || geographyLabel.isBlank()) {
            return null;
        }
        return NAME_TO_ISO3.getOrDefault(geographyLabel, geographyLabel);
    }

    /** Whether a stored country value is the "applies everywhere" sentinel. */
    public static boolean isSentinel(String country) {
        return SENTINEL.equals(country);
    }

    /** Whether a COREx geography label is a recognized country (has an ISO-3 mapping). */
    public static boolean isKnownCountry(String geographyLabel) {
        return NAME_TO_ISO3.containsKey(geographyLabel);
    }
}

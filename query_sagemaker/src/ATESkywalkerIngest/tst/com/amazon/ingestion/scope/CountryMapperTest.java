package com.amazon.ingestion.scope;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CountryMapperTest {

    @Test
    void mapsKnownCountriesToIso3() {
        assertEquals("USA", CountryMapper.toCountry("United States"));
        assertEquals("GBR", CountryMapper.toCountry("United Kingdom"));
        assertEquals("TUR", CountryMapper.toCountry("Türkiye"));
        assertEquals("VNM", CountryMapper.toCountry("Viet Nam"));
    }

    @Test
    void preservesGlobalSentinelVerbatim() {
        assertEquals("Global", CountryMapper.toCountry("Global"));
        assertTrue(CountryMapper.isSentinel("Global"));
        assertFalse(CountryMapper.isSentinel("USA"));
    }

    @Test
    void preservesRegionalRolloupsVerbatim() {
        assertEquals("LATAM", CountryMapper.toCountry("LATAM"));
        assertEquals("North America", CountryMapper.toCountry("North America"));
        assertEquals("Middle East", CountryMapper.toCountry("Middle East"));
        assertEquals("Africa", CountryMapper.toCountry("Africa"));
    }

    @Test
    void knownCountryDetection() {
        assertTrue(CountryMapper.isKnownCountry("United States"));
        assertFalse(CountryMapper.isKnownCountry("Global"));
        assertFalse(CountryMapper.isKnownCountry("LATAM"));
    }

    @Test
    void handlesNullAndBlank() {
        assertNull(CountryMapper.toCountry(null));
        assertNull(CountryMapper.toCountry("   "));
    }
}

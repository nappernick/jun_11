package com.amazon.ingestion.scope;

import java.util.Map;
import java.util.Optional;
import java.util.function.Function;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Canonical job-level scope values, sourced from the COREx {@code system_job-level}
 * taxonomy. Stored on each evidence record under the index field {@code level}; the raw
 * COREx label is also preserved in the record's {@code source_metadata} backup.
 *
 * <p>{@link #ALL_JOB_LEVELS} is the sentinel meaning "applies to every level"; scope
 * filtering matches a requester's specific level OR this sentinel.
 */
public enum Level {
    ALL_JOB_LEVELS("All Job Levels"),
    L0("L0"),
    L1("L1"),
    L2("L2"),
    L3("L3"),
    L4("L4"),
    L5("L5"),
    L6("L6"),
    L7("L7"),
    L8("L8"),
    L9("L9"),
    L10("L10"),
    L11("L11"),
    L12("L12"),
    L99("L99");

    /** The sentinel value meaning "applies to all job levels". */
    public static final Level SENTINEL = ALL_JOB_LEVELS;

    private static final Map<String, Level> BY_LABEL =
        Stream.of(values()).collect(Collectors.toMap(Level::label, Function.identity()));

    private final String label;

    Level(String label) {
        this.label = label;
    }

    /** The exact COREx taxonomy label for this level. */
    public String label() {
        return label;
    }

    /** Whether this is the "applies to all" sentinel. */
    public boolean isSentinel() {
        return this == SENTINEL;
    }

    /** Resolve a COREx label to its {@link Level}, if recognized. */
    public static Optional<Level> fromLabel(String label) {
        return Optional.ofNullable(BY_LABEL.get(label));
    }
}

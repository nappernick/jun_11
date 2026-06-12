package com.amazon.ingestion.scope;

import java.util.Map;
import java.util.Optional;
import java.util.function.Function;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Canonical role scope values, sourced from the COREx {@code system_employee-class}
 * taxonomy. ("Employee class" is COREx's name; Skywalker calls this axis {@code role}.)
 * Stored on each evidence record under the index field {@code role}; the raw COREx label
 * is also preserved in the record's {@code source_metadata} backup.
 *
 * <p>{@link #ALL_EMPLOYEE_CLASSES} is the sentinel meaning "applies to every role"; scope
 * filtering matches a requester's specific role OR this sentinel.
 *
 * <p>Note: this is the lettered employment-class taxonomy, NOT a manager-vs-IC
 * distinction. The {@code label()} values are the exact COREx strings, leading letter
 * prefix included.
 */
public enum Role {
    ALL_EMPLOYEE_CLASSES("All Employee Classes"),
    B_FIXED_TERM_CONTRACTOR_EU("B - Fixed Term Contractor - EU"),
    C_THIRD_PARTY_CONSULTANT("C - Third Party Consultant"),
    F_REGULAR_FULL_TIME("F - Regular Full Time"),
    G_AGENCY_OR_TEMP("G - Agency or Temp"),
    H_REGULAR_PART_TIME_20_PLUS_HOURS("H - Regular Part Time - 20 + Hours"),
    I_INTERN("I - Intern"),
    J_JV_OR_RC_WORKER("J - JV or RC Worker"),
    M_INTERNAL_STAFFING_SOLUTIONS("M - Internal Staffing Solutions"),
    N_TRAINEE_EUROPEAN("N - Trainee European"),
    P_APPRENTICE("P - Apprentice"),
    Q_FIELD_REGULAR_PART_TIME_20_29("Q - Field Regular Part Time 20-29"),
    R_REGULAR_REDUCED_TIME_30_PLUS_HRS("R - Regular Reduced Time 30 + Hrs"),
    S_SEASONAL_SHORT_TERM("S - Seasonal/Short-Term"),
    T_ONSITE_VENDOR("T - Onsite Vendor"),
    V_OFFSITE_VENDOR("V - Offsite Vendor"),
    W_3P_ONSITE_WORKER("W - 3P Onsite Worker"),
    X_REGULAR_FLEX_TIME_UNDER_20_HRS("X - Regular Flex Time - < 20 Hrs");

    /** The sentinel value meaning "applies to all employee classes". */
    public static final Role SENTINEL = ALL_EMPLOYEE_CLASSES;

    private static final Map<String, Role> BY_LABEL =
        Stream.of(values()).collect(Collectors.toMap(Role::label, Function.identity()));

    private final String label;

    Role(String label) {
        this.label = label;
    }

    /** The exact COREx taxonomy label for this role. */
    public String label() {
        return label;
    }

    /** Whether this is the "applies to all" sentinel. */
    public boolean isSentinel() {
        return this == SENTINEL;
    }

    /** Resolve a COREx label to its {@link Role}, if recognized. */
    public static Optional<Role> fromLabel(String label) {
        return Optional.ofNullable(BY_LABEL.get(label));
    }
}

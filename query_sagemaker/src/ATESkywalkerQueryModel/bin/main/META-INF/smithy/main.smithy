$version: "2"

namespace com.amazon.ateskywalkerquery

use amazon.coral#java
use smithy.protocols#rpcv2Cbor

@rpcv2Cbor
service ATESkywalkerQuery {
    version: "2022-07-26"
    operations: [
        CreateBeer
        GetAllBeers
        GetCorpus
        GetCorpus2
        SearchByExplicitScope
    ]
}

/// Create a new beer
operation CreateBeer {
    input: Beer
    output: Beer
    errors: [
        DependencyException
    ]
}

/// Get a list of all of the beers in the system
operation GetAllBeers {
    input: Unit
    output: BeerList
    errors: [
        DependencyException
    ]
}

structure Beer {
    beerId: beerId

    @required
    name: name

    description: description

    beerCompanyName: beerCompanyName

    beerTypeName: beerTypeName
}

structure BeerList {
    beers: beerListDefinition
}

/// Get the full text corpus
operation GetCorpus {
    input: Unit
    output: CorpusResponse
}

/// Get the second text corpus
operation GetCorpus2 {
    input: Unit
    output: CorpusResponse
}

structure CorpusResponse {
    @required
    content: String
}

/// This exception is thrown on a database (or other dependency) error
@error("client")
structure DependencyException {
    message: errorMessage
}

list beerListDefinition {
    member: Beer
}

@length(min: 1, max: 255)
@pattern("^[\\S\\s]+$")
string beerCompanyName

@java("java.lang.Long")
long beerId

@length(min: 1, max: 255)
@pattern("^[\\S\\s]+$")
string beerTypeName

@length(min: 0, max: 1000)
@pattern("^[\\S\\s]*$")
string description

string errorMessage

@length(min: 1, max: 255)
@pattern("^[\\S\\s]+$")
string name

/// Search by explicit scope - bypasses PAPI resolution
operation SearchByExplicitScope {
    input: SearchByExplicitScopeInput
    output: SearchResult
    errors: [
        DependencyException
    ]
}

structure SearchByExplicitScopeInput {
    @required
    queryText: String

    @required
    employeeId: String

    @required
    country: String

    @required
    level: Level

    @required
    role: Role
}

/// Job-level scope, from the COREx system_job-level taxonomy.
/// ALL_JOB_LEVELS is the sentinel meaning "applies to every level".
enum Level {
    ALL_JOB_LEVELS = "All Job Levels"
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"
    L6 = "L6"
    L7 = "L7"
    L8 = "L8"
    L9 = "L9"
    L10 = "L10"
    L11 = "L11"
    L12 = "L12"
    L99 = "L99"
}

/// Role scope, from the COREx system_employee-class taxonomy (COREx calls this
/// "employee class"; Skywalker calls the axis "role"). This is the lettered
/// employment-class taxonomy, NOT a manager-vs-IC distinction.
/// ALL_EMPLOYEE_CLASSES is the sentinel meaning "applies to every role".
enum Role {
    ALL_EMPLOYEE_CLASSES = "All Employee Classes"
    B_FIXED_TERM_CONTRACTOR_EU = "B - Fixed Term Contractor - EU"
    C_THIRD_PARTY_CONSULTANT = "C - Third Party Consultant"
    F_REGULAR_FULL_TIME = "F - Regular Full Time"
    G_AGENCY_OR_TEMP = "G - Agency or Temp"
    H_REGULAR_PART_TIME_20_PLUS_HOURS = "H - Regular Part Time - 20 + Hours"
    I_INTERN = "I - Intern"
    J_JV_OR_RC_WORKER = "J - JV or RC Worker"
    M_INTERNAL_STAFFING_SOLUTIONS = "M - Internal Staffing Solutions"
    N_TRAINEE_EUROPEAN = "N - Trainee European"
    P_APPRENTICE = "P - Apprentice"
    Q_FIELD_REGULAR_PART_TIME_20_29 = "Q - Field Regular Part Time 20-29"
    R_REGULAR_REDUCED_TIME_30_PLUS_HRS = "R - Regular Reduced Time 30 + Hrs"
    S_SEASONAL_SHORT_TERM = "S - Seasonal/Short-Term"
    T_ONSITE_VENDOR = "T - Onsite Vendor"
    V_OFFSITE_VENDOR = "V - Offsite Vendor"
    W_3P_ONSITE_WORKER = "W - 3P Onsite Worker"
    X_REGULAR_FLEX_TIME_UNDER_20_HRS = "X - Regular Flex Time - < 20 Hrs"
}

structure SearchResult {
    @required
    resultKind: ResultKind

    route: RouteInfo

    scopeSnapshot: ScopeSnapshot

    evidence: EvidenceList

    abstainReason: String

    correlationId: String
}

enum ResultKind {
    ANSWERABLE
    ABSTAIN
}

structure RouteInfo {
    path: String
    survivingArms: StringList
    rerankerState: String
}

structure ScopeSnapshot {
    country: String
    level: String
    role: String
}

structure EvidenceCandidate {
    candidateId: String
    sourceArm: String
    sourceId: String
    title: String
    text: String
    sourceUrl: String
    policyLinks: StringList
    armLocalRank: Integer
    rerankScore: Double
}

list EvidenceList {
    member: EvidenceCandidate
}

list StringList {
    member: String
}

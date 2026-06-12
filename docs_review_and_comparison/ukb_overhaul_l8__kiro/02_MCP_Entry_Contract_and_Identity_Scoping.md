## Section 02. MCP Entry Contract and Identity Scoping

### 1. Tenets

We prefer scoped correctness over broader retrieval when they conflict. A request whose employee scope cannot be established is not eligible for candidate generation, even if an unscoped search might return plausible text.

We prefer one canonical internal request over caller-specific execution paths when they conflict. Alias, employee-id, and explicit-scope entry modes exist at the MCP boundary, but retrieval receives one normalized scoped request shape.

We prefer verified identity channels over argument convenience when they conflict. On CloudAuth plus TransitiveAuth paths, the validated TA human identity is canonical. On Federate-inbound paths without delegated human identity propagation, identity remains an MCP tool argument by contract.

We prefer fail-closed admission over late repair when they conflict. Missing identity, unresolved PAPI scope, malformed explicit scope, or identity-channel mismatch stops at the boundary; retrieval does not widen, guess, or post-filter its way back to correctness.

We prefer stable client contracts over hidden client-specific shortcuts when they conflict. Slack, UAT, QuickSuite, and future clients all see the same MCP tools and response envelope even though their identity transport differs.

### 2. Problem and Architectural Intent

Skywalker's entry boundary has one hard job: turn an external MCP tool call into a scoped retrieval request that downstream systems can trust. The boundary is not a search implementation detail. It is the point where employee identity, scope, client route, and request shape become either valid enough to run retrieval or invalid enough to reject.

The architectural intent is to keep retrieval deterministic across client surfaces. Slack may arrive through a Slack-side inline-agent orchestrator, UAT through a Skywalker-owned web orchestrator, and QuickSuite through its own chat-agent runtime. Those clients differ in how they authenticate, how they carry human identity, and how they render the final answer. They must not differ in what Skywalker considers a valid retrieval request. After admission, every successful call has a non-empty query, a stable employee identity for traceability, canonical `country`, `level`, and employee-class `role`, route metadata, and a correlation identifier.

This section also prevents an easy future mistake: treating scope as a hint. In Skywalker, scope is part of the correctness contract. The FAQ index filters on it, UKB requests are issued for the target user, route metadata records it, and the response envelope exposes it as `scope_snapshot`. If scope is missing or untrustworthy, the system does not have a lesser-quality answer; it has no safe retrieval request.

### 3. Boundary and Non-Goals

This boundary starts when an authenticated client issues MCP `tools/list` or `tools/call` for the Skywalker server through Amazon MCP Gateway. It ends when Skywalker has either rejected the call at admission or produced the canonical scoped request consumed by routing, retrieval, reranking, and evidence packaging.

The boundary includes MCP tool names, JSON-RPC request admission, tool argument validation, identity-channel selection by route, PAPI resolution when scope is not supplied, explicit-scope validation when scope is supplied, canonical scope normalization, correlation and route metadata, and the response envelope distinction between answerable retrieval, abstention, and tool-execution failure.

The boundary does not include Slack event handling, QuickSuite intent routing, UAT browser state, final user-facing prose, conversation memory, retrieval ranking, FAQ ingestion, UKB internals, reranker scoring, or client-specific citation rendering. Those systems depend on this contract, but they do not change it.

The boundary is intentionally not an authorization design chapter. Auth and identity facts appear here only where they change the request contract: Slack and UAT use the CloudAuth-inbound route with CloudAuth OBO plus TransitiveAuth; QuickSuite uses the Federate-inbound route and carries identity in tool arguments because delegated human identity propagation is not the launch posture for that route. Details such as AAA application names, Bindle onboarding tickets, registry IDs, and server IDs are operational facts, not architecture-defining contract shape.

### 4. Source-of-Truth Hierarchy

For the MCP entry contract, use this order when sources disagree:

1. `IngestionCodeReference`.
2. Implemented code paths.
3. Architecture documents, including adopted API and integration contracts.
4. Design discussions.
5. Future proposals.

Within architecture documents, this section depends on API_01 for MCP tool protocol shape, response envelope, and error split; API_14 for CloudAuth OBO plus TransitiveAuth behavior on Slack and UAT paths; Section 09 and API_08 for QuickSuite's Federate-inbound MCP Gateway behavior; and Section 05 and Section 08 for UAT explicit-scope usage and Slack alias usage.

This hierarchy has consequences. If a future client document says it can omit scope, this section and API_01 win. If future gateway documentation adds delegated identity propagation to the Federate-inbound combination, Section 09 can reopen the QuickSuite identity-carriage decision without changing the three-tool Skywalker contract. If runtime implementation later differs from this prose, the implementation is the active source of truth and this document must be corrected rather than defended.

### 5. Facts, Assumptions, and Consequences

The launch facts are concrete. Skywalker is an MCP server reached through Amazon MCP Gateway. The launch MCP protocol target is the revision supported by QuickSuite through the gateway. Skywalker exposes three search tools: `skywalker.search.by_alias`, `skywalker.search.by_employee_id`, and `skywalker.search.by_explicit_scope`. All three return the same structured backend envelope rather than final conversational prose.

Scope is mandatory before retrieval. Alias and employee-id modes do not carry the full retrieval scope, so Skywalker resolves `country`, `level`, and `role` through PAPI before routing. Explicit-scope mode carries `employee_id`, `country`, `level`, and `role` in the request and does not call PAPI on the hot path after validation. `ABSTAIN` is a successful retrieval outcome, not a transport error and not an admission failure.

Client identity channels differ by route. Slack and UAT call the CloudAuth-inbound gateway route and propagate human identity through TransitiveAuth; on those paths, a valid TA token is the canonical human identity source. QuickSuite calls the Federate-inbound gateway route; at launch, Skywalker does not receive delegated human identity from the Federate JWT, so QuickSuite must supply identity through MCP tool arguments. The MCP input schemas retain alias and explicit-scope fields because QuickSuite and future non-TA clients need them.

This section assumes PAPI remains the authoritative launch source for scope resolution when the request supplies only alias or employee ID. If PAPI cannot resolve the employee, the consequence is an admission or tool-execution failure rather than a broader retrieval. Reopening that posture would require evidence that a replacement authoritative identity source exists and can supply country, level, and role with equal or better freshness and correctness.

This section assumes explicit-scope callers that are allowed to use `skywalker.search.by_explicit_scope` already hold authoritative scope for the human request. UAT satisfies that assumption through server-side Midway/Federate claims read by the orchestrator; QuickSuite may satisfy it only if its integration can supply the full tuple. If production evidence shows explicit-scope fields are stale, inconsistent, or client-guessed, the consequence is not a silent PAPI fallback. The reopen path is a design change for reconciliation, secondary validation, or removal of explicit-scope authority for that caller class.

This section assumes route metadata plus a scope snapshot are sufficient for downstream observability at the MCP boundary. If incident review shows the envelope cannot distinguish identity source, route, or scope derivation clearly enough, the consequence is a non-breaking metadata extension to the envelope rather than a new MCP tool.

### 6. Inputs, Outputs, and Contracts

The MCP server exposes three entry tools because they represent three materially different identity inputs, not because retrieval has three modes.

`skywalker.search.by_alias` accepts `query_text`, `alias`, and optional `correlation_id`. On Slack's CloudAuth plus TA path, `arguments.alias` is retained for schema compatibility, but the validated TA human alias is canonical when present and valid. On QuickSuite and future non-TA callers, `arguments.alias` is the identity input. Skywalker resolves the alias through PAPI before retrieval.

`skywalker.search.by_employee_id` accepts `query_text`, `employee_id`, and optional `correlation_id`. Skywalker resolves the employee ID through PAPI before retrieval. This path exists for callers whose reliable identity handle is employee ID rather than alias.

`skywalker.search.by_explicit_scope` accepts `query_text`, `employee_id`, `country`, `level`, `role`, and optional `correlation_id`. The request already carries the retrieval scope. Skywalker validates required fields and canonicalizes values, then proceeds without a PAPI lookup on the hot path. UAT uses this path because its server-side orchestrator reads scope claims from the validated Midway identity and calls the MCP server with the canonical tuple.

All three successful entry modes produce one internal request:

- `query_text`, a non-empty user question.
- `identity`, including `employee_id` when known and `alias` when available or derived.
- `scope`, containing canonical `country`, canonical `level`, and canonical `role`.
- `entry_mode`, one of alias, employee-id, or explicit-scope.
- `identity_source`, such as TA-human-claim, MCP alias argument, MCP employee-id argument, or explicit-scope argument.
- `route_metadata`, including client route class and gateway path class where available.
- `correlation_id`, supplied by the caller or generated at admission.

The output contract is a single structured MCP result for all tools. A successful backend result has `result_kind` of `ANSWERABLE` or `ABSTAIN`, route metadata, `scope_snapshot`, `evidence`, optional `abstain_reason`, and `correlation_id`. Evidence is populated for answerable responses. Abstain responses carry an abstain reason such as no usable evidence or evidence too weak after reranking; they may retain limited evidence for auditability when the downstream contract permits it.

Contract failures split into two classes. JSON-RPC or gateway-level failures cover unknown tools, malformed arguments, gateway authentication or authorization rejection, and missing or invalid TA when a route requires TA. Tool-execution errors use the MCP tool-result error channel for failures after the tool call is admitted but before trustworthy evidence can be produced, such as unresolved PAPI identity or non-convertible scope after resolution. Backend abstention is neither class.

### 7. Fixed Decisions

**Decision 1: Skywalker exposes three search tools, not one overloaded search tool.**  
The rationale is that alias, employee-id, and explicit-scope requests have different trust and resolution requirements. Separate tools make those differences visible in `tools/list` and simplify schema validation. This binds API_01, Slack action-group mapping, UAT explicit-scope mapping, and QuickSuite tool selection. Reopen only if production client integration shows the three-tool surface causes systematic wrong-tool selection that cannot be corrected with tool descriptions.

**Decision 2: Scope must be complete before retrieval starts.**  
The rationale is that country, level, and role are correctness inputs, not ranking hints. This binds FAQ filtering, UKB target-user construction, route metadata, response `scope_snapshot`, and abstain interpretation. Reopen only if the retrieval architecture itself changes to support a formally reviewed unscoped or partially scoped mode with separate answerability semantics.

**Decision 3: PAPI resolves scope for alias and employee-id modes.**  
The rationale is that these modes identify the human but do not carry the full scope tuple. PAPI is the launch authority for the missing employee attributes. This binds latency budgets, failure behavior, and any caller that chooses alias or employee-id mode. Reopen only if another authoritative employee-scope source is available with documented freshness, coverage, and failure semantics.

**Decision 4: Explicit-scope mode does not call PAPI on the hot path.**  
The rationale is that explicit-scope exists for clients that already possess authoritative scope, and adding PAPI would make the path slower while blurring which source wins on disagreement. This binds UAT's Midway-claim path and any QuickSuite launch path that supplies full scope. Reopen only if post-launch evidence shows supplied explicit scope is materially stale, malformed, or client-guessed at a rate high enough to affect answer correctness.

**Decision 5: CloudAuth plus TA paths use TA human identity as canonical.**  
The rationale is that Slack and UAT have a supported delegated human-identity channel, so Skywalker should not trust a model- or orchestrator-supplied alias over a validated TA claim. This binds Slack's alias path and UAT's explicit-scope path for identity traceability. Reopen only if TA support is unavailable in the deployed framework or if validated production failures show TA cannot meet request isolation and reliability requirements.

**Decision 6: Federate-inbound QuickSuite uses argument-carried identity at launch.**  
The rationale is that the Federate-inbound plus CloudAuth-outbound gateway combination does not provide the launch delegated human identity channel Skywalker needs. This binds QuickSuite's use of alias or explicit-scope arguments and prevents Skywalker from inferring user identity from Federate JWT claims. Reopen if MCP Gateway adds a supported delegated identity pattern for this combination and QuickSuite can exercise it end-to-end.

**Decision 7: Admission fails closed on missing or invalid identity/scope.**  
The rationale is that the worst failure is a plausible answer for the wrong employee population. This binds caller UX: identity failures are service or integration failures, not abstains. Reopen only with an approved alternate mode that can prove safe degradation without broadening employee eligibility.

**Decision 8: The response envelope is shared across clients and remains structured.**  
The rationale is that Skywalker is the retrieval backend, while Slack, UAT, and QuickSuite own final user-facing rendering. This binds client prompt design, citation rendering, and the distinction between backend abstain and service failure. Reopen only if a future client cannot consume structured MCP output and the team accepts the cost of a separate client adapter above the MCP boundary.

### 8. Alternatives Considered

A single `skywalker.search` tool with a tagged identity payload is attractive because it makes the tool list shorter. It is rejected because the schema would hide trust boundaries inside optional fields and make it easier for clients to send partial identity material that looks valid until runtime.

PAPI for every request, including explicit-scope mode, is attractive because it centralizes scope authority. It is rejected for launch because explicit-scope exists precisely for clients that already have authoritative scope, and dual-source reconciliation would require a separate conflict policy. Without that policy, PAPI-on-explicit-scope would create ambiguity rather than safety.

Trusting `arguments.alias` on Slack and UAT even when TA exists is attractive because it keeps all clients visually identical in the tool payload. It is rejected because TA is the stronger identity channel on those routes. Keeping the weaker channel canonical would preserve accidental model/orchestrator authority where a verified human claim is available.

Inferring QuickSuite identity from Federate JWT claims is attractive because it would reduce the burden on QuickSuite prompts and tool arguments. It is rejected at launch because the active Federate-inbound gateway path does not provide the delegated human identity contract Skywalker needs downstream.

Falling back to unscoped retrieval when PAPI or explicit scope fails is attractive as a user-experience rescue. It is rejected because it violates the core correctness invariant. The system should be visibly unavailable rather than silently wrong for a user's country, level, or role.

A client-specific wrapper tool for QuickSuite or Slack is attractive because it could hide MCP details from each client. It is rejected because it would fork the contract, obscure route behavior, and create a hidden second boundary where identity and scope bugs could accumulate.

### 9. End-to-End Flow

On `tools/list`, Skywalker advertises the three search tools with their input schemas and one shared output schema. Clients choose the tool whose identity material they can supply. The tool description must make this selection explicit enough that QuickSuite and model-driven clients do not treat explicit scope as optional.

On an alias call from Slack, the Slack application resolves the Slack user to an Amazon alias, initiates a TransitiveAuth token for that human, and calls `skywalker.search.by_alias` through the CloudAuth-inbound MCP Gateway route. MCP Gateway performs CloudAuth authorization and forwards to Skywalker with CloudAuth OBO and the TA token. Skywalker validates the request schema and TA token, treats the TA alias as canonical, resolves scope through PAPI, normalizes the scoped request, and hands it to routing and retrieval.

On an explicit-scope call from UAT, the UAT orchestrator validates the Midway-authenticated user, reads `employee_id` or `sub`, `country`, `level`, and `role` from server-side identity claims, initiates TA for the human, and calls `skywalker.search.by_explicit_scope` through the same CloudAuth-inbound route. Skywalker validates TA for identity traceability, validates and canonicalizes the supplied scope, does not call PAPI, and hands the canonical request to the UAT FAQ-only runtime path.

On a QuickSuite call, QuickSuite's chat-agent runtime chooses among the advertised tools and calls through the Federate-inbound MCP Gateway route. The request body carries the identity material: alias, employee ID, or explicit scope. MCP Gateway validates the Federate token and Bindle permission, then forwards to Skywalker. Skywalker validates arguments, resolves scope through PAPI for alias or employee-id mode, or validates supplied explicit scope for explicit-scope mode. It does not infer the human from Federate JWT claims at launch.

After canonical request construction, this section stops owning the request. Section 04 owns routing and retrieval flow, Section 07 owns common reranking and abstain behavior, and the client sections own final rendering. The response returns through the same MCP Gateway path as one synchronous JSON-RPC result.

### 10. Failure Behavior

Malformed JSON-RPC, unknown tool names, schema-invalid arguments, and gateway authentication or authorization failures fail before retrieval. The caller receives a protocol-level failure and no `scope_snapshot` because no valid scoped request existed.

Missing TA on Slack or UAT fails closed. Skywalker does not downgrade to `arguments.alias` as canonical on those paths because that would erase the identity-channel distinction the architecture intentionally introduced. The client should render this as a system or integration failure, not as an abstain.

PAPI unresolved identity, PAPI returning incomplete required scope, or a non-convertible PAPI value fails before retrieval. The failure is attributable to identity/scope resolution, not to answerability. The system does not broaden the query, omit filters, or use stale scope from a previous request.

Explicit-scope validation failure fails before retrieval. Missing `country`, `level`, or `role`, unknown role values, non-canonicalizable level values, and empty employee identity are contract failures. A caller that cannot provide the full tuple must choose alias or employee-id mode instead of sending partial explicit scope.

Route metadata or correlation metadata failure must not hide the primary result when the scoped request and retrieval are otherwise valid. Missing caller-supplied `correlation_id` can be repaired by generation at admission; missing scope cannot.

Backend `ABSTAIN` is a normal successful result. It means Skywalker had a valid scoped request and completed retrieval but found no defensible evidence for the user's scoped situation. Clients must render abstain separately from identity failure, gateway failure, timeout, and tool-execution error.

### 11. Calibration Surfaces

Tool descriptions are calibration-active. The three-tool decision is fixed, but wording in `tools/list` may change if model-driven clients choose explicit-scope without full scope, choose employee-id when alias is available, or suppress abstain semantics.

Canonical value encodings for `country`, `level`, and `role` are calibration-active until implementation pins the exact accepted values. The contract requires canonicalization and rejection of non-convertible values; the exact country code format, level string format, and role enum spelling can be finalized with integration tests.

Error taxonomy detail is calibration-active. The architecture fixes the distinction between protocol failure, tool-execution failure, answerable result, and abstain. The exact message strings and subcodes can evolve as clients prove what they need for clear rendering and support triage.

Identity-source metadata is calibration-active. The architecture requires enough route and scope data to debug identity behavior. If post-launch incidents need finer distinctions, the envelope can add non-breaking metadata such as `identity_source` or `scope_source` without changing the entry tools.

Explicit-scope reconciliation is calibration-active but not enabled by default. Production evidence may justify adding delayed audit, sampled PAPI comparison, or caller-specific validation for explicit-scope paths. Such mechanisms must not silently change the request's authority order without reopening Decision 4.

### 12. Open Questions

Many open questions in this section cannot be resolved through offline experimentation alone. The entry contract has not yet observed enough production behavior, real user query patterns, client tool-selection failures, identity-data gaps, or scope-diversity examples to close every question honestly. Initial decisions should therefore optimize for observability, safe iteration, and low-cost reversibility. Final answers for several calibration and contract questions should emerge only after launch and after observing usage across hundreds of active users, unless a pre-launch implementation blocker proves that a fixed decision is not buildable.

The final canonical encodings for country, level, and role remain open until implementation and client integration tests pin the accepted values. The evidence standard is successful end-to-end calls from every launch client plus negative tests proving malformed values fail before retrieval.

Whether explicit-scope trust remains direct for all future caller classes remains open. The decision should be revisited only after launch traffic shows behavior across hundreds of active users or after a concrete new caller proposes to supply scope from a different authority. The evidence must include mismatch rates, stale-claim examples, and user-impact analysis, not pre-launch preference.

Whether QuickSuite can eventually move from argument-carried identity to a delegated identity channel remains open. The evidence standard is a supported MCP Gateway/Federate delegated-identity pattern plus a QuickSuite integration test proving Skywalker receives a validated human identity without a wrapper.

Whether the shared envelope needs additional identity observability fields remains open. The evidence standard is incident, support, or audit review showing that `route`, `scope_snapshot`, and `correlation_id` are insufficient to explain real production behavior.

Whether employee-id mode remains useful after real client adoption remains open. It should not be removed based on small-sample launch preference; removal requires production evidence that no supported client uses it and that keeping it increases misrouting or support burden.

### 13. Closing Position

The MCP entry contract is deliberately strict because Skywalker's downstream intelligence is only as correct as the scope it receives. Three external entry modes give clients the right way to provide identity. One canonical internal request gives retrieval one way to reason. PAPI resolves missing scope, explicit-scope callers supply complete scope, TA-backed clients use verified human identity, and Federate-backed QuickSuite carries identity in arguments until the gateway offers a better delegated path.

The contract is buildable because every branch has a defined authority, a defined output, and a defined failure behavior. It is also intentionally humble: value encodings, tool descriptions, error detail, and observability fields can move with evidence. The invariant that cannot move casually is scoped admission before retrieval.

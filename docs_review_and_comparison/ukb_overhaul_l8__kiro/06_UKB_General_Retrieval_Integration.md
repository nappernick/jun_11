## Section 06. UKB General Retrieval Integration

### 1. Tenets

Skywalker prefers controlled accuracy over broad coverage when the two conflict. The controlled Top 50 FAQ path remains the highest-control retrieval surface, and UKB is added to broaden evidence coverage without weakening that controlled path.

Skywalker prefers honest black-box integration over invented comparability. When UKB does not expose internal ranker semantics, stable scores, filters, or pagination tokens, the adapter preserves what UKB returned and leaves missing fields missing rather than fabricating native behavior.

Skywalker prefers one common evidence pipeline over arm-specific answer paths. UKB results become normalized candidates for convergence, reranking, answerability, and final response construction; they do not become terminal answers.

Skywalker prefers native UKB personalization before explicit Skywalker-side filtering at launch. `targetUser` is the launch mechanism for UKB personalization, and `additionalFilters` remains empty until observed evidence shows that explicit filters are needed.

Skywalker prefers classified degradation over hidden fallback. A UKB miss, UKB service failure, malformed partial response, and UKB-not-called state are different facts, and the adapter must preserve those distinctions for routing, diagnosis, and calibration.

### 2. Problem And Intent

Skywalker needs retrieval coverage for travel, events, and expense questions that are not cleanly captured by the controlled Top 50 FAQ path. The architecture solves that by integrating UKB as the general retrieval arm while preserving a clear boundary between Skywalker's owned runtime and UKB's black-box retrieval system.

The problem is not how UKB retrieves internally. Skywalker does not own UKB indexing, chunking, ranking, personalization internals, corpus composition, or native confidence behavior. The problem is how Skywalker should call UKB, classify the outcome, normalize the returned evidence, and pass that evidence back into the common runtime pipeline without pretending that the general arm has the same observability or score semantics as the controlled FAQ arm.

The intent is a narrow adapter boundary. Route policy decides whether UKB participates. The adapter invokes UKB through the MCP `retrieve` tool, supplies the resolved user context required for UKB native personalization, receives evidence-bearing resources, converts usable resources into the shared candidate envelope, and reports the arm outcome. After that point, Section 04 owns arm convergence and one-arm survival, Section 07 owns common reranking, and later answer construction decides whether the full system can answer.

### 3. Boundary And Non-Goals

This integration is the Skywalker boundary around UKB access. It is responsible for constructing the UKB MCP request from an already-scoped Skywalker request, enforcing the launch contract for `targetUser` and `additionalFilters`, signing and sending the request to the stage-specific UKB endpoint, validating the MCP tool result shape, classifying the UKB arm outcome, and normalizing valid resources into common runtime candidates.

It is not a UKB ranking specification. Skywalker does not define how UKB indexes documents, selects chunks, ranks resources, personalizes internally, computes confidence, or decides which source metadata to return. Bugs in those behaviors route to UKB unless the Skywalker adapter sent the wrong request, dropped valid returned evidence, or misclassified the service outcome.

It is not a route-policy engine. The adapter does not decide that a request is dual-arm, FAQ-only, general-only, or rescue-path eligible. It runs only because upstream route state selected UKB participation.

It is not a final answer path. UKB returns evidence candidates, not a Skywalker answer. UKB evidence must pass through the same convergence, reranking, answerability, and abstention machinery as other evidence.

It is not a broad enterprise search escape hatch. The product boundary remains travel, events, and expense. UKB may have access to a broader knowledge universe, but Skywalker should not use this integration to answer arbitrary internal search questions outside the product scope.

It is not a launch-time explicit filtering system. `additionalFilters` is intentionally empty at launch. Explicit filters are a calibration surface, not a hidden requirement for the first release.

### 4. Source-of-Truth Hierarchy

This section is downstream of the earlier architecture decisions and upstream of reranking and answer construction. When documents appear to overlap, the binding hierarchy is:

1. `IngestionCodeReference`.
2. Implemented code paths.
3. Architecture documents, including adopted API and integration contracts.
4. Design discussions.
5. Future proposals.

Within that global hierarchy, the UKB-specific dependency order is:

1. Section 01 defines Skywalker's product and system boundary.
2. Section 02 defines canonical request scoping, including resolved user identity and the location, level, and role dimensions.
3. Section 03 defines the controlled Top 50 FAQ retrieval arm.
4. Section 04 defines route policy, arm participation, one-arm survival, and convergence into the shared reranking surface.
5. API_09 and the UKB onboarding contract define the concrete UKB MCP request, response, endpoint, authentication, and role shape.
6. This section defines the Skywalker adapter contract for invoking UKB and normalizing UKB evidence.
7. Section 07 owns common reranking after arm convergence.

The consequence of this hierarchy is that this section cannot reinterpret route policy, user scoping, UKB's concrete API contract, or downstream reranking ownership. If the UKB onboarding contract changes the wire shape, API_09 and the adapter contract must be updated. If route participation changes, Section 04 must be updated. If cross-arm comparability changes, Section 07 must be updated.

### 5. Known Facts

UKB is reached through MCP. The tool is `retrieve`, invoked through the standard MCP `tools/call` method over Streamable HTTP.

The V1 endpoint pattern is:

```text
https://api.us-west-2.{stage}.knowledge.pxt.amazon.dev/iam/v1/mcp
```

The supported stage values are `alpha`, `beta`, `gamma`, `preprod`, and `prod`. V1 is in `us-west-2`. Gamma does not carry production content. PreProd is the pre-production staging environment with a production content mirror.

The request body contains `arguments` with `query`, `maxResults`, `targetUser`, and `additionalFilters`. `query` is the user's canonical query text. `maxResults` is the per-arm candidate budget. `targetUser` is a two-field object containing `LOGIN` and `PERSON_ID`. `additionalFilters` is an explicit filter object and is empty at launch.

`targetUser` is populated so UKB can apply native personalization over attributes including `countryCode`, `stateCode`, `buildingCode`, `badgeColor`, `jobLevel`, and `payRateType`.

Every request includes the required authentication and identity headers. `x-acting-user` carries JSON-encoded login identity. `x-target-user` is omitted when `targetUser` is already supplied in the body to avoid alias mismatch errors. `x-atoz-person-id` carries the resolved Person ID. `x-atoz-token-audience-type` carries the AtoZ persona. `x-amzn-transitive-authentication-token` carries the caller's TA token.

Requests are signed with SigV4 using `service: "execute-api"` and `region: "us-west-2"` against temporary credentials obtained by assuming the UKB-issued cross-account `kbs-mcp-role_{stage}_{client_id}` role. Skywalker's service IAM policy grants `sts:AssumeRole` on that role ARN and not broader access.

The response is a standard MCP tool result. Its `content[]` array contains entries of `type: "resource"`. Each resource can carry `uri`, `mimeType: "text/plain"`, `text`, `name`, `title`, `annotations` including `audience`, `priority`, and `lastModified`, and `_meta.sourceUrl`.

The V1 response does not include V0-style `filters` or `nextToken` fields in `_meta`. The adapter must not synthesize those fields.

### 6. Assumptions, Consequences, And Evidence

This section assumes the request has already been scoped before the adapter runs. The adapter receives canonical query text, resolved login alias, PAPI-resolved Person ID, resolved location, resolved level, resolved role shape, route state, timeout budget, candidate budget, and trace context. If this proves false, the consequence is not that the adapter repairs scope; the consequence is that upstream scoping has violated the adapter contract and UKB personalization may be wrong or impossible.

This section assumes route policy has already selected UKB participation. UKB runs because Section 04 selected a dual-arm route or because the runtime explicitly widened into the general arm as a rescue path after FAQ-only evidence was unusable. If this proves false, the adapter becomes a hidden routing engine, route metrics lose meaning, and Section 04 must be revisited.

This section assumes UKB native personalization through `targetUser` is sufficient for launch. If post-launch evidence shows location, level, role, or user-eligibility drift in returned evidence, the consequence is a calibration decision about explicit `additionalFilters`, not local score manipulation in the adapter.

This section assumes UKB response order is meaningful enough to preserve as `arm_local_rank` but not meaningful enough to treat as a cross-arm score. If UKB later exposes a stable documented score, Section 07 must decide how or whether that score participates in common reranking before the adapter can populate any score-like field.

This section assumes the general arm remains inside Skywalker's travel, events, and expense product boundary through route policy and query semantics at launch. If hundreds-user traffic shows recurring out-of-domain UKB evidence on in-scope queries, the consequence is either route-policy calibration in Section 04, explicit UKB filtering in this section, or both.

This section assumes a healthy UKB call can still return no useful evidence. The consequence is that service health and evidence usefulness must be recorded separately. Treating an empty result as a service failure would hide real retrieval misses; treating a service failure as a miss would hide dependency reliability issues.

### 7. Inputs, Outputs, And Contracts

The adapter input is a scoped runtime request plus route state. At minimum, the input contains canonical query text, resolved alias, resolved Person ID, location scope, level scope, role scope, route reason, trace context, current timeout budget, and current candidate budget. The adapter must reject or fail the UKB arm when required identity fields are absent; it must not silently call UKB with partial identity because that would make personalization behavior unknowable.

The outbound contract is the UKB MCP `retrieve` call. The request must include the canonical query, `maxResults`, populated `targetUser`, and launch-empty `additionalFilters`. It must use the stage-specific V1 endpoint, Streamable HTTP transport, SigV4 signing, temporary credentials from the UKB-issued cross-account role, and the required identity and authentication headers.

The inbound contract is a UKB MCP tool result containing evidence-bearing resources. A usable candidate requires a valid resource entry with usable text. Source metadata is preserved when present and left absent when missing. A malformed item does not invalidate the entire response when other valid items are present.

The adapter output is a UKB result package, not an answer. The package contains normalized UKB candidates in the common candidate envelope, the UKB arm status for the request, failure classification when applicable, route and arm provenance, and item-level source metadata sufficient for later diagnosis.

The failure contract is explicit. If UKB is not called, the output records not-called status and route provenance. If UKB is called and fails, the output records the failure classification and no fabricated candidates. If UKB is called and returns no usable evidence, the output records a retrieval miss. If UKB returns a partial result, the output preserves valid candidates and records item-level loss. Section 04 then applies one-arm survival or convergence rules; this adapter does not decide final answerability.

### 8. Fixed Decisions

**Decision: UKB is the general retrieval arm, not the controlled high-accuracy arm.** The rationale is that Skywalker needs broad evidence coverage, but the Top 50 FAQ set needs a retrieval surface with direct Skywalker ownership and stronger predictability. This binds the route policy, evaluation strategy, and user-facing correctness posture. Reopen this decision only if UKB demonstrates controlled-arm accuracy, explainability, and operational predictability on the Top 50 set, or if the Top 50 controlled arm no longer carries differentiated correctness value.

**Decision: UKB is treated as a black-box dependency from Skywalker's perspective.** The rationale is that Skywalker can observe the request sent, the response received, the service outcome, and the normalized evidence produced, but it cannot assert UKB internals. This binds logging, debugging, score treatment, and ownership routing. Reopen this decision only if UKB publishes a stable, supported contract for ranking, score semantics, filters, corpus selection, or personalization behavior that Skywalker can depend on.

**Decision: UKB is invoked only as a consequence of route state.** The rationale is that participation policy belongs in Section 04, and duplicating route decisions inside the adapter would make traffic behavior hard to reason about. This binds adapter simplicity, route metrics, and one-arm survival behavior. Reopen this decision only if UKB exposes a preflight capability that must be evaluated at the adapter boundary before route participation can be safely determined.

**Decision: UKB output is consumed as evidence candidates.** The rationale is that Skywalker needs common evidence convergence and reranking across retrieval arms, not a special-case UKB answer path. This binds normalization, reranking, answer construction, and final abstention. Reopen this decision only if UKB becomes the owner of terminal answer generation under a separate product contract, which would require changes outside this section.

**Decision: `targetUser` is required for launch personalization.** The rationale is that UKB's native personalization depends on resolved login and Person ID, and Skywalker's correctness dimensions include user-specific location, level, and role context. This binds upstream scoping, request validation, and identity header construction. Reopen this decision only if UKB provides an alternative supported personalization mechanism or if privacy, identity, or onboarding constraints change the allowed request shape.

**Decision: `additionalFilters` is empty at launch.** The rationale is that launch should first rely on UKB native personalization and avoid inventing filter semantics before real traffic shows which filters improve correctness. This binds the outbound request shape and launch evaluation plan. Reopen this decision if post-launch analysis shows recurring wrong-location, wrong-level, wrong-role, ineligible-user, or out-of-domain evidence that native personalization does not prevent.

**Decision: UKB candidates are normalized into the common candidate envelope.** The rationale is that downstream systems should consume candidates with shared provenance and evidence fields regardless of retrieval arm. This binds Section 04 convergence, Section 07 reranking, answer construction, and diagnostics. Reopen this decision only if UKB returns evidence with semantics that cannot be represented without losing material correctness information.

**Decision: Skywalker does not fabricate UKB-native scores.** The rationale is that fabricated scores would create false precision and make cross-arm comparison look more objective than it is. This binds candidate schema population and reranker input semantics. Reopen this decision only if UKB returns a documented stable score and Section 07 defines how that score should be calibrated against FAQ-arm signals.

**Decision: The adapter remains stateless across requests.** The rationale is that hidden conversational history would make UKB retrieval behavior difficult to reproduce and would duplicate context management owned elsewhere in the runtime. This binds request construction, debugging, and evaluation repeatability. Reopen this decision only if the product explicitly introduces multi-turn retrieval context with a documented source-of-truth and replay contract.

### 9. Alternatives Considered

Owning the broad general retrieval pipeline inside Skywalker was rejected. It is attractive because it would give Skywalker full control over ingestion, indexing, ranking, and instrumentation. It is rejected because Skywalker already owns the controlled FAQ arm, and owning a second broad retrieval stack would add substantial operational and content-maintenance weight without enough incremental launch value for non-FAQ travel, events, and expense coverage.

An all-UKB design was rejected. It is attractive because it would simplify retrieval topology and avoid maintaining a separate controlled FAQ path. It is rejected because the highest-value Top 50 question set needs a controlled surface where Skywalker can reason directly about coverage, candidate quality, and failure behavior. Full black-box dependence is least acceptable where correctness is most tightly judged.

Calling UKB on every request was rejected for launch. It is attractive because it maximizes evidence recall and avoids under-routing the general arm. It is rejected because it weakens the meaning of FAQ-only routing, increases dependency load, and blurs the route distinction defined in Section 04. The architecture keeps UKB participation tied to route state so calibration can measure when general retrieval is actually needed.

Forwarding UKB answer-like output directly was rejected. It is attractive because it could reduce latency and implementation work if UKB appears to have already found an answer. It is rejected because Skywalker needs common evidence convergence, reranking, answerability, abstention, and final response construction. A direct UKB answer path would make UKB terminal in practice, which violates the evidence-pipeline tenet.

Using explicit UKB filters at launch was deferred. It is attractive because explicit filters could constrain location, role, level, or domain earlier in retrieval. It is deferred because the supported launch posture uses `targetUser` native personalization and an empty `additionalFilters` object. Filters should be added only when observed data identifies a repeatable failure class and the filter semantics are known to improve it.

Translating UKB rank into a synthetic score was rejected. It is attractive because downstream systems often prefer numeric features. It is rejected because rank position is not a documented UKB-native score and should not masquerade as one. The adapter preserves position as `arm_local_rank`; the common reranker owns score assignment.

### 10. Normalization Contract

Each usable UKB `resource` becomes one common runtime candidate. The adapter preserves missing fields as missing and does not synthesize titles, URLs, policy links, scores, filters, or pagination tokens that UKB did not return.

The fixed mapping is:

- `candidate_id`: generated runtime UUID.
- `source_arm`: `"UKB"`.
- `source_id`: `resource.uri`.
- `title`: `resource.title`, falling back to `resource.name`.
- `text`: `resource.text`.
- `source_url`: `resource._meta.sourceUrl`.
- `policy_links`: empty, because UKB does not surface a separate content-level policy-link metadata field.
- `arm_local_rank`: the positional index in the UKB `content[]` array.
- `rerank_score`: unset until populated by the common reranker.

The normalized envelope must preserve enough provenance to answer basic audit questions later: UKB was or was not called, route state caused UKB to run when it did run, the active user scope was applied, the candidate came from UKB, the UKB source identifier was known or absent, and the service outcome was classified separately from evidence usefulness.

### 11. End-To-End Flow

The flow starts after Section 04 route state says UKB should run. That can happen on the normal dual-arm path or on an explicit FAQ rescue path.

The adapter receives the scoped runtime request and route state. It verifies that the required identity fields, query text, timeout budget, and candidate budget are present. If required identity is missing, the adapter classifies the UKB arm as a request-construction failure rather than sending a knowingly under-scoped request.

The adapter builds the UKB MCP request. It sets the canonical query text, `maxResults`, populated `targetUser`, empty `additionalFilters`, trace context, authentication headers, and SigV4 signing context for the stage-specific V1 endpoint. When `targetUser` is present in the body, `x-target-user` is omitted to avoid alias mismatch errors.

On the normal dual-arm path, the UKB call runs concurrently with controlled FAQ retrieval. On the rescue path, the UKB call runs after the runtime explicitly widens the route because the FAQ-only attempt did not produce usable evidence.

The adapter classifies the transport and service outcome before item normalization. If the call succeeds at the service level, the adapter validates the MCP tool result shape, identifies resource entries, drops malformed items, and converts usable resources into common candidates.

The adapter returns the normalized candidates, status record, failure classification if any, and provenance package to the orchestration layer. After that handoff, convergence, reranking, answerability, and final abstention are owned by later sections.

The flow deliberately preserves the distinction between service state and evidence state. A healthy UKB call can return no useful evidence. A failed UKB call can leave the FAQ arm to carry the request under one-arm survival. A partial response can still produce valid candidates. These are different runtime facts and must remain distinguishable.

### 12. Failure And Abstain Behavior

A `403 Forbidden` on SigV4 is classified as IAM or trust misconfiguration. The adapter fails the UKB arm without inline retry because retrying does not fix a policy or role mismatch.

A `504 Gateway Timeout` is retried once with exponential backoff per UKB guidance. If the retry also fails, the UKB arm is classified as failed and Section 04 one-arm survival applies.

A `429 Too Many Requests` is classified as rate limiting. The adapter aborts the UKB arm for the current request rather than retrying inline and worsening shared throughput pressure.

Other 5xx responses and transport failures are classified as UKB arm failures. The adapter records the dependency failure and returns no fabricated candidates for the UKB arm.

A structurally valid response with `content: []` is a retrieval miss, not a service fault. The adapter returns a miss status so downstream behavior can distinguish "UKB was healthy but found nothing usable" from "UKB could not be reached."

A response with a valid outer shape and some malformed items is a partial-result condition. The adapter drops malformed items, keeps valid candidates, and records the item-level loss.

A response with no usable text-bearing resources is a miss even if metadata is present. The adapter must not create empty evidence candidates because downstream answer construction depends on text evidence.

Final abstention is outside this section. The adapter reports whether UKB was not called, called and failed, called and missed, called and partially succeeded, or called and returned usable candidates. Section 04 and downstream answerability logic decide whether the overall request can continue with another arm, should abstain, or should answer.

### 13. Calibration Surfaces

The UKB timeout budget is calibration-active. It should be revisited if UKB routinely times out under normal traffic, if one-arm survival becomes the dominant path for dual-arm requests, or if useful evidence appears to be cut off too aggressively.

The UKB candidate budget is calibration-active. Launch default is 10. It should be revisited if UKB is consistently underrepresented after reranking, overrepresented with weak evidence, or repetitive enough to crowd the candidate pool.

The request-scope strategy is calibration-active. Launch relies on `targetUser` personalization. Explicit `additionalFilters` should be reconsidered if native personalization returns wrong-shaped evidence for location, level, role, or user eligibility.

Domain narrowing is calibration-active. Travel, events, and expense boundaries are enforced at launch through routing and query semantics rather than a dedicated UKB domain filter. Explicit filters should be reconsidered if UKB returns too much out-of-domain content on the dual-arm path.

Normalization richness is calibration-active. The current envelope is intentionally minimal and evidence-oriented. It should be revisited if reranking or answer construction needs additional UKB metadata that is available, documented, and materially useful.

Rescue-path frequency is calibration-active. Frequent UKB rescues may indicate that FAQ-only commitment is too brittle, that route thresholds are too strict, or that FAQ coverage is weaker than expected.

Provenance granularity is calibration-active. The current posture keeps structured arm-level and item-level metadata. It should be revisited only if later diagnosis cannot distinguish UKB retrieval behavior from adapter, routing, or reranking behavior.

### 14. Open Questions

These questions do not block launch unless new evidence shows they affect correctness, reliability, or integration feasibility. The evidence standard for reopening launch decisions is post-launch behavior under real traffic, preferably after Skywalker has observed hundreds of users across the supported travel, events, and expense query distribution. Anecdotes and isolated examples can identify a suspected issue, but they should not by themselves justify changing route policy, filter strategy, or score treatment.

What final `client_id` and stage-specific role ARNs will UKB issue during onboarding? This must be resolved before production deployment because it binds the SigV4 assume-role path and service IAM policy.

Is launch `maxResults = 10` the right candidate budget after real reranking data exists? The decision should use observed candidate survival, answer correctness, latency, and crowd-out rates rather than intuition.

Does `targetUser` native personalization produce sufficiently scoped evidence for Skywalker's location, level, and role correctness requirements? The decision should use labeled examples where the same query has different correct evidence for different users.

Should Skywalker introduce explicit `additionalFilters` after observing real UKB misses, broad matches, or wrong-shaped matches? The decision should be made only when the failure class is repeatable and the intended filter semantics are supported by UKB.

Does implicit domain narrowing through route policy and query semantics keep UKB evidence inside travel, events, and expense boundaries? The decision should use post-launch out-of-domain rates and examples where the user query was in scope but UKB evidence was not.

Are there query classes that should bypass UKB even when the nominal route is dual-arm, or is that purely a Section 04 route-policy calibration question? This should remain in Section 04 unless the bypass reason depends on UKB-specific request or response constraints.

Would a documented UKB-native score materially improve cross-arm ranking, or would it add false precision? This should remain open until UKB exposes stable score semantics and Section 07 has enough evaluation data to calibrate the score against controlled-arm candidates.

### 15. Closing Position

The UKB integration is a deliberately narrow adapter boundary. It lets Skywalker use a black-box general retrieval arm without making that arm opaque to the rest of the runtime.

The fixed design is that a scoped route decision triggers an MCP `retrieve` call, UKB applies native personalization through `targetUser`, launch uses empty `additionalFilters`, and the adapter normalizes returned resources into common evidence candidates. Failure and miss states are classified separately. Native UKB score fabrication is prohibited. The rest of the system receives comparable evidence, not a special-case UKB answer.

That posture gives Skywalker broad coverage while preserving the architecture's core discipline: owned control where accuracy is most critical, honest boundaries where the system integrates a black-box service, and a single downstream evidence pipeline that can still be evaluated.

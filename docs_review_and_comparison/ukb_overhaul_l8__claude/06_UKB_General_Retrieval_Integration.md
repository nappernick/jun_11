## Section 06. UKB General Retrieval Integration

Section 04 fixed the online orchestration layer — when the runtime stays inside the controlled FAQ arm, when it fans out, and how the arms converge before the common reranking surface. This section drills into the general arm itself: the UKB integration.

The integration matters because it is where Skywalker deliberately stops owning the retrieval stack. The FAQ arm is controlled end to end; the UKB arm is not. Skywalker does not control UKB's indexing, retrieval internals, or ranking. At the same time, the general arm is not optional noise: it is the broad-coverage surface for travel, events, and expense questions outside the Top 50 path. If the integration layer is vague, the two-arm design becomes hard to reason about, because every weakness in the general path blurs together with every weakness in routing, reranking, and answer construction. The goal here is not to pretend UKB is part of the owned pipeline — it is to make the seam explicit enough that the seam itself can be evaluated, debugged, and trusted.

### 1. The seam boundary

This section owns the adapter between Skywalker's runtime orchestration and UKB: outbound request construction (transforming the canonical scoped request into the request UKB receives), invocation behavior (when the call fires, how it participates in concurrent dual-arm execution, its latency budget, how failure is represented), inbound normalization (UKB's native shape into the common candidate envelope), and provenance preservation (enough route, source, and arm metadata that later evaluation can distinguish a UKB weakness from a weakness anywhere else — the minimum honesty a black-box arm demands).

It does not own UKB's internals — chunking, scoring, indexing, confidence. It does not own the route decision (Section 04 — the adapter is invoked because the route said so, never on its own judgment). It does not own reranking, the abstain floor, agent behavior, or PAPI resolution. The division is the point: UKB is valuable here precisely by *not* being another owned subsystem. Skywalker benefits from UKB; it does not quietly recreate UKB inside its own boundary. A bug about wrong-shaped UKB candidates entering the pool files here; a bug about UKB's content quality files with UKB (Sev-2, resolver group `AET ML Engineering Seattle`, CTI `PXT/AETContentOptimization/Support`); a bug about UKB being invoked on the wrong requests files against Section 04.

### 2. Inputs, outputs, and contracts

**Inputs.** The canonical scoped request (query text plus the resolved country, level, and employee class, with trace context); the route state explaining *why* UKB is participating — normal dual-arm or FAQ-rescue widening, a distinction later telemetry needs; and the active control-plane values (timeout, candidate budget) from SSM.

**The outbound contract.** The tool is `retrieve` — UKB's single MCP tool — invoked via standard `tools/call` ([API_09]). Arguments: `query` (the user's text, unrewritten per Section 04 Decision 7); `maxResults` (the per-arm budget, launch default 10, SSM-backed); `targetUser` (a two-field object: `LOGIN` set to the resolved alias, `PERSON_ID` set to the PAPI-resolved person UUID); and `additionalFilters` (empty at launch). Five headers accompany every request: `x-acting-user` (JSON `{"LOGIN": "<alias>"}`), `x-target-user` (omitted — `targetUser` already rides in the body, and a duplicated alias must match exactly or UKB errors), `x-atoz-person-id`, `x-atoz-token-audience-type` (the AtoZ persona), and `x-amzn-transitive-authentication-token` (the caller's TA token). Transport is Streamable HTTP per the MCP spec, SigV4-signed with `service: "execute-api"`, `region: "us-west-2"`, against temporary credentials from assuming the cross-account `kbs-mcp-role_{stage}_{client_id}` role UKB issues at onboarding; the service IAM policy carries `sts:AssumeRole` on that ARN and nothing broader. Stage endpoints follow one pattern — `https://api.us-west-2.{stage}.knowledge.pxt.amazon.dev/iam/v1/mcp` (`alpha`, `beta`, `gamma`, `preprod`, `prod`) — with us-west-2 the only region at V1, gamma carrying no production content, and preprod mirroring production content. UKB's us-west-2-only constraint is one input to the prod-region consistency question recorded in Section 01 §9.

**How scope actually works on this arm — stated plainly.** Skywalker cannot push its `(country, level, employee class)` filter contract into UKB the way it pre-filters the owned index. What it can do, and does, is populate `targetUser`, which causes UKB to apply its **native personalization** across UKB's own attribute set: `countryCode`, `stateCode`, `buildingCode`, `badgeColor`, `jobLevel`, `payRateType`. That set approximately covers the scoping triple — country and level map directly; `payRateType` and `badgeColor` correlate with employee class without being it — but the alignment is approximate, not contractual. The scope guarantee on the general arm is therefore structurally weaker than on the owned arm, and the architecture absorbs that honestly downstream: the common reranker scores UKB candidates against the scoped query, and the agent's grounding discipline filters what survives into the answer. Pretending the two arms have equal scope guarantees would be exactly the kind of false symmetry this adapter exists to avoid. If UKB's personalization proves inconsistent with the scoping triple in practice, `additionalFilters` (UKB exposes `exactFilters`, `partialFilters`, `aclFilters`) is the escalation lever — a calibration event, not an architecture change.

**The inbound contract.** A standard MCP tool result whose `content[]` carries entries of `type: "resource"`: `uri`, `mimeType: "text/plain"`, `text`, `name`, `title`, `annotations` (`audience`, `priority`, `lastModified`), and `_meta.sourceUrl`. The V1 response deliberately omits `filters` and `nextToken` from `_meta`, and Skywalker does not pretend they exist. No stable item-level score is returned beyond positional order — one reason native scores are non-comparable across arms (Section 04 Decision 4).

**Normalization mapping** into the common envelope, fixed field-by-field: `candidate_id` ← generated runtime UUID; `source_arm` ← `"UKB"`; `source_id` ← `resource.uri`; `title` ← `resource.title`, fallback `resource.name`; `text` ← `resource.text`; `source_url` ← `resource._meta.sourceUrl`; `policy_links` ← empty (UKB surfaces no policy-link metadata; URLs embedded in text stay in text); `arm_local_rank` ← positional index; `rerank_score` ← populated by Section 07. Skywalker does not fabricate an arm-local score, and missing fields stay missing — the adapter normalizes what is real, never manufactures symmetry.

**The output** is not "the answer from UKB." It is the general-arm evidence package: the normalized candidate list, a per-request UKB status record (succeeded / failed / timed out / returned nothing usable), and structured provenance — the normalized candidates retain the full `content[]` item metadata, which is enough for later diagnosis without retaining the entire raw MCP response.

### 3. Fixed decisions

**Decision 1 — UKB is the general arm, not the controlled arm.** It provides coverage and breadth across the domain where Skywalker does not want to own a second end-to-end pipeline; it does not displace the owned subsystem on the questions the system is most tightly judged on (tenet 1). Binds the two-arm asymmetry everywhere. Reopens only with Section 01 Decision 1.

**Decision 2 — UKB participation is a route consequence.** The adapter runs on the dual-arm path or on an explicitly widened FAQ-rescue, never on its own initiative, and the route reason rides in telemetry. Binds Section 04's routing authority. Reopens never; an adapter that self-invokes is drift by definition.

**Decision 3 — UKB is a black box, and the visibility boundary is disciplined.** Skywalker knows what it sent and what came back; it does not claim to know why UKB ranked, indexed, or weighed anything internally. Binds the diagnostic posture: UKB-quality questions route to UKB's support path, not into Skywalker tuning. Reopens never.

**Decision 4 — UKB results are evidence candidates, never terminal answers.** Whatever richness UKB exposes, the adapter extracts the evidence-bearing part into the common pipeline; UKB is a retrieval contributor, not the final speaker. Binds the envelope mapping and Section 07's unified pool. Reopens never; the forward-UKB-answers posture was rejected (§4).

**Decision 5 — Scope remains load-bearing on the general arm, expressed through `targetUser`.** The launch mechanism is UKB-native personalization, with `additionalFilters` as the escalation lever and the weaker-guarantee acknowledgment in §2 as the honest frame. Binds the request construction and §8's calibration triggers. Reopens via calibration surface three.

**Decision 6 — Native ranking signals are not cross-arm compared.** Positional order is preserved as provenance; the common reranker is the decision language. Binds Section 07. Reopens never.

**Decision 7 — The integration is stateless.** UKB is called on the current scoped request; no hidden conversational history, no behavior shifts from prior turns. Binds consistency with Section 01 Decision 7. Reopens with it.

**Decision 8 — UKB does not broaden Skywalker's product scope.** UKB exposes a much wider documentation universe; Skywalker remains a travel, events, and expense system. No dedicated UKB domain filter exists at launch — narrowing happens through query semantics and the gate's routing preference — so this decision is enforced by the reranker and the abstain floor rather than at the UKB boundary, and `partialFilters` on a domain-like attribute becomes the lever if evaluation shows too-broad results surviving. Binds the product boundary. Reopens never in direction; the *mechanism* is calibration surface four.

### 4. Alternatives considered

**Owning the general pipeline.** Viable before UKB existed; rejected once UKB became available as an MCP-accessible knowledge base on the substrate the team would otherwise assemble. A second owned stack would multiply ingestion and maintenance work without comparable control benefit — ownership stays where it buys the most (tenet 1).

**All-UKB (Skywalker as a thin wrapper).** Rejected — not because UKB is bad, but because complete black-box dependence is least acceptable exactly where the system is most strongly judged.

**UKB on every request, gate as diagnostics.** Live but not adopted; tracked as Section 04's always-query-both alternative. Re-litigation requires evidence that confidently Top-50-shaped queries consistently benefit from UKB competition.

**Minimal request (query-only) versus strongly scoped request.** Resolved by the actual UKB surface: `targetUser` personalization is the supported scope channel and the launch posture; explicit `additionalFilters` are held in reserve. The direction was always "if UKB can accept structured scope, use it" — and this is the form it takes.

**Forwarding UKB's phrasing downstream.** Rejected: it would create a structurally different pipeline for one arm and make convergence unjustifiable.

### 5. Assumptions inherited from upstream

From Section 01: the system boundary and the two-arm asymmetry. From Section 02: requests arrive with resolved country, level, and employee class — the adapter never discovers, infers, or repairs scope. From Section 03: the FAQ arm is a real retrieval subsystem, so both arms contribute *evidence* and convergence is meaningful. From Section 04: the route policy and the one-arm survival rules; the adapter does not rewrite them locally. Two deeper premises carry: context-free answers are wrong-shaped (tenet 3), which is why this integration cannot be a loose free-text passthrough even though the underlying service would allow it; and the common reranking surface exists, so this section produces rerankable evidence, never a bypass channel.

### 6. End-to-end data flow

The flow begins only after route state says UKB runs — normal dual-arm, or an FAQ-rescue widening — from an already scoped request with an already decided route reason.

**Request packaging.** The adapter builds the `retrieve` call: query text unrewritten, `maxResults` from SSM, `targetUser` from the resolved identity, headers per [API_09], TA token attached. Scope and domain posture are applied before the call, not reconstructed after results return.

**Outbound execution.** On the dual-arm path the UKB call launches concurrently with FAQ retrieval — neither arm blocks the other's start (Section 01 Decision 4). The call runs under the SSM timeout (launch 300 ms), signed with the assumed cross-account role's credentials, with trace context attached.

**Response classification before normalization.** Transport failure, timeout, or contract-shape failure classifies as **arm failure**. A technically successful call returning empty `content: []` classifies as a **retrieval miss** — the arm was reachable and unhelpful, which is a different fact from unreachable, and later route analysis depends on the difference. The HTTP-level grading is concrete ([API_09]): **403** is IAM misconfiguration — alert and fail without retry, because retrying does not fix a policy mismatch; **504** retries once with backoff per UKB's documented recommendation, then fails the arm; **429** respects the rate limit and aborts the arm for this request rather than retrying into a shared throughput budget; any other 5xx or transport fault is arm failure.

**Item-level normalization.** Each resource maps through the fixed table in §2. A malformed item is discarded individually while good items survive, with the partial-result fact recorded — the narrowest responsible layer handles the damage, and the pool is protected without pretending bad data is fine.

**Candidate construction and packaging.** Usable results become common-envelope candidates (runtime-scoped `candidate_id` — stable for the request and its review, not a permanent cross-request identity, which stays with UKB's native `uri`); the candidate list, status record, and provenance return to Section 04's convergence layer.

Two properties of this flow carry the section's weight. It preserves the difference between **service state and evidence state** — a call can be healthy and useless, or unhealthy and absent, and collapsing those makes every weak request look identical. And it keeps the general arm **legible under audit**: when a UKB candidate reaches the reranker or the envelope, the system can still say which UKB resource it was, what route state caused the call, and what scope context was active when Skywalker asked.

### 7. Failure behavior and abstain behavior

**UKB failure is not system failure.** On the dual-arm path with a surviving FAQ arm, the runtime continues under `SINGLE_ARM_FALLBACK` (Section 04 §7); the adapter reports its own failure cleanly and never forces a false hard-stop.

**Timeout is a real failure mode, not a soft inconvenience.** Breadth that arrives after the budget is breadth the user never sees; the adapter returns a timed-out arm state at the deadline and the orchestration proceeds with what exists.

**Malformed output is handled at the narrowest responsible layer.** Unusable outer response → arm failure. Valid outer response with bad items → discard the bad, keep the good, record the partiality.

**Empty-but-successful is a miss, not a fault.** A miss leaves open that the FAQ arm carries the request or that the pipeline abstains on thin evidence; a fault says something different about runtime state. The adapter is honest about three states — not called (route did not require it), called and unhelpful, called and contributing — because Section 10's calibration needs to distinguish abstentions caused by route choice from those caused by evidence weakness from those caused by ranking behavior.

Non-goals: no second custom search strategy over UKB results, no query rewriting in hopes UKB does better, no answer generation, no local merging of arms (Section 04 owns convergence, Section 07 owns scoring), no scope inference from results, no substantive-correctness judgments, and — the black-box temptation named explicitly — no normalizing away the uncertainty of the boundary. What UKB does not return stays visibly absent.

### 8. Calibration surfaces

**Surface one — the timeout budget** (SSM, launch 300 ms). Re-litigate if UKB routinely times out under normal traffic, or if the budget demonstrably abandons useful evidence the p95 budget could have afforded.

**Surface two — the candidate budget** (SSM, launch 10; same cluster as Section 04 §8 surface six). Re-litigate if the general arm is consistently under- or over-represented after reranking.

**Surface three — scope expression.** Launch is `targetUser` personalization; escalation is explicit `additionalFilters`. Re-litigate if UKB personalization produces wrong-shaped candidates at a rate the reranker does not absorb, or if explicit filters overconstrain the arm until the broad path stops being broad.

**Surface four — domain narrowing.** No dedicated filter at launch; the lever is `partialFilters` on a domain-like attribute. Re-litigate if too much irrelevant general content survives into the pool.

**Surface five — envelope richness for UKB candidates.** Re-litigate if reranking or answer assembly proves the current envelope too thin to carry UKB's value forward — the fix is a richer normalized envelope, never a looser one. (Section 04 §8 surface eight, consumer side.)

**Surface six — rescue frequency.** Re-litigate if FAQ-rescue UKB calls become common: that pattern indicts the FAQ-only route's brittleness or the gate thresholds, and the fix belongs to Section 04's surfaces, with this signal as the evidence.

**Surface seven — provenance granularity.** Detailed by default, because black-box arms make diagnosis provenance-dependent. Re-litigate toward less only on evidence that the retained detail is operationally expensive without improving diagnosis — proven, not assumed.

### 9. Open questions

Most open questions across this series share one precondition, stated in full in Section 10 §9: they are only answerable against real user data at meaningful volume — a few hundred actual users, arriving with the September production launch. Until then, launch postures stand, and pre-launch pressure to move them resolves as a recorded non-change.

**Onboarding identifiers** *(disclaimer: operational details that surface at onboarding, not design questions).* The final `client_id`, the exact cross-account role ARN per stage, and confirmation of the launch `maxResults`.

**Personalization fidelity to the scoping triple** *(disclaimer: does not gate launch; the posture and escalation lever are decided).* Whether UKB's native personalization tracks the `(country, level, employee class)` triple closely enough in practice — the approximate-alignment caveat in §2. Deciding evidence: judged dual-arm traffic showing wrong-scope UKB candidates surviving reranking.

**Region roadmap** *(feeds the Section 01 §9 prod-region consistency question; not independently decided here).* UKB is us-west-2-only at V1; its multi-region roadmap directly shapes where the prod data plane should land.

**Per-query UKB bypass classes** *(disclaimer: a Section 04 re-litigation trigger, not a Section 06 decision).* Whether any query class should deliberately skip UKB even on the nominal dual-arm route.

### Closing position

The UKB integration is the controlled seam between Skywalker's owned runtime and a black-box general retrieval surface — broad travel, events, and expense coverage without owning a second end-to-end pipeline, a benefit that is real only if the seam is explicit. A scoped request reaches the general arm only when route policy says so. The adapter constructs a `targetUser`-personalized request under a real latency budget, classifies the outcome honestly (failure versus miss versus contribution, graded per status code), normalizes what actually returned into the common envelope without fabricating symmetry, acknowledges plainly that scope guarantees on this arm are weaker than on the owned arm and names where the architecture absorbs that, and preserves the provenance that keeps a black-box arm legible under audit. Skywalker does not need to know how UKB works inside to integrate with it well; it needs to know exactly what it is responsible for at the boundary — and now it does.

---

*Stale-source flags raised in this section, for propagation: prior Section 06 manager-versus-IC framing of the third scope dimension (superseded by employee class, Section 01 Decision 8); prior Section 06 open-ended "exact UKB contract still open" hedging (superseded — the contract is pinned per [API_09] and stated in §2); the scope-guarantee asymmetry between arms is now stated explicitly rather than implied (new in this revision, no source contradicted).*

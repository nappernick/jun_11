## Section 07. Reranking, Candidate Unification, and Abstain Behavior

Section 04 fixed the online path that decides whether a request stays in the controlled FAQ arm or widens to both arms. Section 06 fixed the integration boundary around UKB. This section takes the output of those decisions and defines the next load-bearing layer: the common scoring surface.

That surface matters because the two-arm design is only coherent if both arms re-enter one disciplined judgment layer. The FAQ arm returns controlled evidence from an owned corpus; the UKB arm returns general-domain evidence from a surface Skywalker does not control. If those streams arrive side by side and the system pretends their raw rankings are comparable, the architecture collapses into folklore. This section prevents that collapse. It decides what evidence survives into the final package, what is too weak to support an answer, and when the right behavior is not to push harder but to abstain — and because Skywalker feeds agents rather than rendering answers, abstention here is not a refusal to help; it is a structured statement that the retrieval backend cannot currently justify an answer-shaped package (tenet 2).

### 1. The scoring boundary

This section owns the layer that begins when the normalized candidate pool arrives from Section 04 and ends when Skywalker holds one of two outcomes: a ranked evidence package explicit enough to support downstream answering, or a structured abstain package explicit enough to support downstream non-answer behavior. Concretely: the reranker-facing candidate contract, the evidence-reranker invocation and its endpoint, post-rerank shortlist construction, the answerability judgment, and the abstain path.

It owns the real meaning of candidate unification: not pretending the arms are identical systems, but requiring both to present evidence in a common enough form that one scoring surface judges them honestly, without inventing certainty from arm-local metadata. It owns the separation between route choice and answerability — the route asks which arms should run; this layer asks whether what came back is strong enough. A request can be routed correctly and still deserve abstention; a request can widen conservatively and still end with one clear winner. Both decisions exist, and they are not the same decision. And it owns the backend-facing definition of confidence only in the narrow sense of "is this package usable" — never the agent's wording, tone, or escalation phrasing.

It does not own routing thresholds, variant matching, or arm concurrency (Section 04); UKB internals (Section 06); scope establishment (Section 02); or final answer generation — if an agent answers from a weak package anyway, that is an error above this layer, not a permission granted by it. The gate's own cross-encoder stage belongs to Section 04; this section's endpoint decisions intersect it only through the shared fleet question (§9).

### 2. Inputs, outputs, and contracts

**Inputs.** The normalized candidate pool (FAQ-only: hybrid-fused FAQ fragments; dual-arm: FAQ fragments plus UKB passages; single-arm fallback: the survivor's set — all already in the common envelope). The route record — reranking does not run in a vacuum, and later abstain analysis needs to know how the pool came to exist. The scoped request snapshot — this layer never revisits identity, but the package must remain auditable against the exact country, level, and employee class that governed the search.

**The reranker-facing candidate contract.** Every candidate carries a stable runtime identifier, source-arm marker (in metadata), source identifier where one exists, the rerankable text payload, title and URL where available, policy links where available, arm-local rank, and the scope snapshot. Two disciplines govern what the reranker actually sees. First, it scores **evidence surfaces, not answer surfaces**: FAQ candidates are retrieval evidence from the owned corpus, not pre-written answers; UKB candidates are retrieved passages, not arm-native conclusions. Second, the scored text is deliberately minimal — the candidate's `title` if a usable one exists, a blank line, then `text`; **no arm prefix, no rank, no source URL in the scored content**. The reranker judges evidence content, not prestige labels: prepending "FAQ arm" would teach the scoring surface to reward source identity over relevance. UKB candidates without a usable title render text alone. Candidate `text` is the whole-node fragment for FAQ candidates (Section 03 Decision 5) and the UKB resource text for UKB candidates — there is no chunk reassembly anywhere in this layer.

**The evidence-reranker invocation.** The model is **Cohere Rerank v4.0 Pro, self-hosted on SageMaker** — subscribed through AWS Marketplace listing `prodview-du2svpomxs5vw` (product ID `prod-b3hko54dqpujq`, flat $3.50/host-hour software fee), deployed as the standard three-call control-plane sequence: `CreateModel` bound to the Marketplace Model Package ARN with `enableNetworkIsolation: true` and a `VpcConfig` placing ENIs in the query service's VPC; `CreateEndpointConfig` with a single production variant (`inferenceAmiVersion: al2-ami-sagemaker-inference-gpu-2`, generous `modelDataDownloadTimeoutInSeconds: 1200` and `containerStartupHealthCheckTimeoutInSeconds: 600` for large-weight download); `CreateEndpoint` binding named endpoints — two distinct production endpoints (`skywalker-rerank-prod-a`, `-b`) fronted by client-side round-robin rather than one multi-instance endpoint, because two endpoints give independent failure domains and independent rolling updates; one beta endpoint. Auto-scaling is not used: cold start on a fresh GPU instance is minutes, longer than any expected burst window, so capacity is always-on. **The instance type is deliberately unfixed**: the Marketplace package supports `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` (the listing's recommended type), and the selection is pending the executive-approved cost-versus-latency bake-off (Section 01 §9) — HA-pair all-in cost ranges roughly $6.6K/month (g5.xlarge) to ~$19K/month (p5.4xlarge), and whether the cheaper A10G-backed types hold the 350 ms timeout on real ~20K-token payloads is exactly what the bake-off measures. The endpoint region follows the prod query service (Section 01 §9's region resolution — no global fleet, one HA pair co-regional with the caller).

The wire request is the standard Cohere Rerank payload: `model: "rerank-v4.0-pro"`, `query` set to the user's text, `documents` in pool order, `top_n: 5` (matching `/skywalker/runtime/retrieval/shortlist_size`), `max_tokens_per_doc: 4096` (the default, unchanged — whole-node FAQ fragments are answer-sized, so 20 candidates sit comfortably inside Rerank v4's 32K-token context window), `api_version: 2`. The response's `results[]` carries `index` (position in the input array, used to recover the original candidate) and `relevance_score` in `[0, 1]`. Invocation goes through the SageMaker **Runtime** service (`runtime.sagemaker.{region}.amazonaws.com`) — distinct from the control-plane service, with distinct SDK clients and IAM actions; the query service holds `sagemaker:InvokeEndpoint` scoped to the named endpoint ARNs and nothing broader, SigV4-signed, reaching the endpoint through a SageMaker Runtime VPC interface endpoint so traffic never traverses the public internet, with the VPC endpoint policy restricting invokable ARNs as defense in depth. The Java client (`software.amazon.awssdk:sagemakerruntime`) holds one long-lived client per JVM with `apiCallTimeout` and `apiCallAttemptTimeout` at the 350 ms launch default from SSM (`/skywalker/runtime/rerank/evidence_timeout_ms`), and a retry policy of one retry with 50 ms fixed backoff — enough to absorb a single transient TCP reset without breaching the budget; anything beyond falls through to the reranker-failure fallback. The revised 800–1000 ms pipeline budget (Section 01 Decision 9) gives this timeout explicit headroom: it can grow toward 600–700 ms within budget if the bake-off's chosen instance needs it, which is what makes the A10G candidates realistic and shifts the bake-off from feasibility to cost-versus-quality. Endpoint updates use `UpdateEndpoint` against new immutable configs (blue-green with automatic rollback; manual rollback is one call back to the previous config). Standard SageMaker CloudWatch metrics (`Invocations`, error counts, `ModelLatency`, `OverheadLatency`, GPU utilization) carry three alarms: endpoint 5xx, `ModelLatency` p95 drifting toward the budget, and sustained GPU saturation ahead of capacity pressure.

**The output** is one ranked evidence decision package. Answerable: the ordered shortlist, route metadata, the positive answerability signal, and the source and policy metadata agents answer from. Not answerable: an abstain package with the same route context, the evidence examined, and a structured reason. Abstention is not represented as an empty list — empty evidence is one path to it, not the definition — because the agent above needs to distinguish "nothing came back" from "what came back was too weak," and collapsing them is both operationally unhelpful and architecturally false.

### 3. Fixed decisions

**Decision 1 — One common reranking surface for both arms.** Both arms produce evidence; the evidence converges; one reranker scores the converged set. This is the layer that makes the two-arm design mathematically honest rather than rhetorically convenient. Binds Sections 04 and 06's normalization contracts and the envelope's `rerank_score`. Reopens never; the alternatives were rejected (§4).

**Decision 2 — Arm-local ranking metadata is not a common confidence language.** The FAQ arm's hybrid-fused score is meaningful inside the FAQ arm; UKB's positional order reflects an opaque internal ranking. Both are preserved as provenance, neither is treated as cross-arm truth. Binds the provenance contract. Reopens never.

**Decision 3 — A strong FAQ route does not bypass reranking.** FAQ-only means UKB was not needed, not that the FAQ arm's output is self-certifying. The owned arm is high-control, not magically correct. Binds the FAQ-only path's flow through this layer. Reopens never.

**Decision 4 — Source-arm identity stays out of the scored text.** In the envelope for telemetry and audit; never in the content the reranker judges (§2). Binds document assembly. Reopens only via §8's source-prior surface, and then as an explicit tie-break rule, never as a text-surface label.

**Decision 5 — The scored document is title-plus-body, minimally rendered.** Enough context to know what the passage is; no administrative scaffolding. Binds document assembly. Reopens via §8 surface seven.

**Decision 6 — The reranker's score is a relative relevance judgment, not a calibrated probability.** Renaming the top score "confidence" does not make it one. Answerability remains a separate backend decision built from reranked evidence quality. Binds the abstain rule's design. Reopens never.

**Decision 7 — Routing thresholds and the abstain floor are separate decisions.** The gate asks whether the request is Top-50-shaped; the floor asks whether post-rerank evidence is strong enough to pass upward. A request can be strongly FAQ-like and unanswerable, or weakly FAQ-like and excellently answerable. Binds the SSM parameter separation (`/skywalker/runtime/gate/*` versus `/skywalker/runtime/abstain/floor`) and Section 10's calibration structure. Reopens never.

**Decision 8 — Skywalker is allowed to abstain.** Recorded plainly because many retrieval systems quietly behave as if every request must terminate in a positive package. Abstention is a legitimate backend output, and the agents above can only behave responsibly because of it (tenet 2). Binds the envelope's `result_kind` and every client section's abstain rendering. Reopens never.

**Decision 9 — The output is evidence, not prose.** Even answerable packages are ranked evidence plus an answerability signal; this layer never becomes a hidden answer generator. Binds Section 01's boundary. Reopens never.

**Decision 10 — The reranker model and hosting pattern.** Cohere Rerank v4.0 Pro on SageMaker, two-endpoint always-on HA in production, VPC-isolated, invoked per the §2 contract. The model family commitment is architectural — one common surface both arms converge on, with the 32K context window removing the payload pressure smaller windows would impose — and a model migration is an architecture-class event because it changes the scoring contract both arms depend on. **The instance type and region are deliberately not fixed in this decision**: instance selection is the pending exec bake-off; region follows the prod query service. Binds the endpoint build-out, the cost envelope, and Section 04's gate-endpoint question. Reopens: the model family only by deliberate migration; the instance by the bake-off's outcome.

**Decision 11 — The abstain rule is two branches, not more.** `NO_USABLE_EVIDENCE`: the reranker returned an empty shortlist, or the pool never held enough candidates to score. `EVIDENCE_TOO_WEAK_AFTER_RERANK`: the top candidate's `relevance_score` falls below the absolute floor in SSM (`/skywalker/runtime/abstain/floor`, launch default 0.30). That is the entire composite. Earlier drafts carried a top-two-margin-disagreement branch (requires semantic judgment about whether close-scoring candidates support the same or conflicting answers — a judgment Skywalker cannot make without an LLM verifier it does not run) and a single-arm-fallback-thinness branch (conflates arm identity with answerability, contradicting Decision 1); both are dropped. The target operational abstain rate at launch is the 5–15% band on Top 50 traffic; a rate materially outside the band is a recalibration tripwire, not permission to drift. Binds the envelope's `abstain_reason` enum and every client's abstain UX. Reopens only with a measurable signal Skywalker can compute without an LLM-side verifier.

### 4. Alternatives considered

**No common reranker — trust arm-local ranking.** Looks simpler; relocates the hard problem. Dual-arm requests still need the top FAQ candidate compared against the top UKB candidate, and there is no honest common scale without this layer. Rejected.

**Separate rerankers per arm, compare top outputs.** More disciplined, fails at the same point: two rerankers on different sets still need a meta-judgment layer above them. One reranker, one universe, one comparison stage is cleaner. Rejected.

**A hard source prior (FAQ beats UKB when close).** Comforting, and it would distort the common surface exactly when dual-arm competition is most useful. Rejected as a fixed rule; a constrained source-aware tie-break stays live only if evidence shows systematic near-tie misresolution (§8).

**Top score as the abstain decision directly.** One simple threshold — too coarse, because a top score means different things depending on shortlist spread, route, survivor count, and pre-rerank degradation. The two-branch rule keeps the floor as the backbone while the route record preserves the context. Rejected as stated; the floor branch is its disciplined descendant.

**Ban abstention; always return the best available set.** Simpler backend, less honest system. The backend is the only place that sees route, pool, reranked order, and fallback state together — it is the right place for a first-class abstain signal. Rejected.

**Hard diversity caps in the shortlist.** Live, not adopted: eager diversity shaping suppresses genuinely strong evidence clusters. Becomes serious if traffic shows one document or arm dominating shortlists in ways that reduce package value (§8).

**Reranker failure → hard abstain.** Live, not adopted — but the launch posture's cost is now stated as what it is: an answerability inversion, not mere ranking degradation (§7). The hard-abstain alternative restores tenet 2 during outages at the price of converting every reranker incident into user-visible non-answers; §8 surface four carries the three-way framing.

### 5. Assumptions inherited from upstream

From Section 01: the boundary (evidence packages with answerability signals, never user-facing responses), tenet 2, and the latency budget this layer dominates. From Section 02: the scope snapshot's vocabulary and the rule that context-free hits are not substitutes for scoped correctness. From Section 03: FAQ candidates are whole-node retrieval evidence — answer-sized by construction, which is what lets the 4096 `max_tokens_per_doc` default and the 20-candidate pool coexist comfortably inside the 32K window. From Section 04: the route policy, convergence, and the route record's honesty. From Section 06: UKB candidates arrive as honest general-arm evidence with preserved provenance and visible absences. The deeper premise: the system does not hide uncertainty in prose — provenance, route state, and arm identity stay visible so later review can tell a weak FAQ retrieval from a weak UKB retrieval from a bad convergence from a genuinely hard question.

### 6. End-to-end data flow

The flow begins when the normalized pool arrives: route decided, arms returned or failed, one candidate universe plus one route record.

**Document assembly.** Each candidate's scored surface is built per Decision 5 — title where useful, body text, nothing administrative. Arm markers, local scores, and route notes stay in the envelope.

**Common reranking.** The reranker receives the live query and the assembled documents and produces one ordered result — the first point in the request where FAQ and UKB evidence is judged on a truly common surface. The call runs under the 350 ms budget with its single fast retry.

**Shortlist formation.** The reranked output becomes a bounded shortlist (`top_n: 5` at launch) — not "everything that survived" but the package the backend is willing to pass upward: wide enough to preserve supporting context, narrow enough that the agent is not handed a shapeless dump.

**Answerability judgment.** The shortlist is examined against the floor, in light of the route record. Empty shortlist → abstain. Top score below the floor → abstain. Otherwise answerable — including on single-arm-fallback routes, where a clearly dominant, well-grounded package is still answerable; the fallback fact rides in the route record rather than triggering its own abstain branch (Decision 11's rationale).

**Abstain classification.** On failure of the answerability test, the reason is one of the two values — `NO_USABLE_EVIDENCE` or `EVIDENCE_TOO_WEAK_AFTER_RERANK` — and the abstain package assembles with route and provenance context intact.

**Package assembly.** Answerable: ranked candidates with `rerank_score` populated, route record, positive signal, source and policy metadata. The candidates' `text` is already complete — whole-node fragments need no reconstruction, no sibling queries, no citation-chain assembly. (An earlier draft of this section specified a post-rerank sibling-and-linked-parent expansion step with citation markers, reconstruction utilities, suppression metrics, and a per-segment token cap. All of it is removed with the chunk model — Section 03 Decision 5 — and the envelope's `citations[]` field is gone with it, per Section 02. The "rerank small, answer big" pattern is unnecessary when retrieval units are already answer-sized.) In both outcomes the output remains a retrieval-backend artifact, not a conversational answer.

Two properties carry the flow's weight. **Answerability is decided after convergence**, not inside each arm — the system never asks each arm whether it found something good enough; it asks whether the unified package is good enough. And the flow **preserves route-truth**: if UKB died and the FAQ arm carried the request, or if an abstention followed a single-arm fallback, that remains visible in the output. The backend is not only selecting evidence; it is preserving how that evidence came to be trusted or not.

### 7. Failure behavior and abstain behavior

**Reranker failure is not retrieval failure.** The system may hold a meaningful candidate set when the reranker is unavailable; the failure gets its own path rather than collapsing into "nothing was found."

**The fallback package reverses the system's core posture, and that is stated plainly rather than dressed as conservatism.** On reranker failure (timeout, transport, both endpoints unhealthy after the single retry), the launch posture returns the best available normalized set in retrieval order, flagged `reranker_state: RERANKER_FAILURE_FALLBACK`. Because no rerank score exists, the floor branch cannot run — which means evidence weak enough that it would normally abstain (a would-be 0.20 top score) is instead handed to an agent instructed to answer from it, flagged only in a route field the user never sees. The fallback is conservative in *ranking* and the opposite of conservative in *answerability*: it loses the ability to abstain on weakness exactly when scoring is least reliable — an inversion of tenet 2 active precisely during reranker outages. Two mitigations bound it at launch: the flag is mandatory in the envelope, and the client behavioral prompts (Sections 05, 08) must treat `RERANKER_FAILURE_FALLBACK` as requiring more conservative composition — shorter claims, heavier hedging toward the sources. Whether the posture itself survives is §8 surface four, reframed accordingly.

**An empty pool after convergence abstains immediately** (`NO_USABLE_EVIDENCE`). The backend already knows neither arm produced scoreable evidence; passing emptiness upward as an answerable state would be dishonest.

**A non-empty shortlist is not automatically answerable.** "Something survived" is not proof an answer should be attempted — this is where systems go wrong, and the floor exists to stop it.

**Cross-arm disagreement is not failure.** Dual-arm requests legitimately produce candidates emphasizing different parts of the answer space; resolving that is this layer's job, and only an unstable or thin final shortlist converts disagreement into abstention — through the two existing branches, not a special one.

**Missing provenance is a structural defect.** A candidate whose text cannot be tied to an arm, source, and request context is dangerous in a system whose trust model depends on knowing where evidence came from; such candidates are rejected from the answerable shortlist regardless of how relevant their text looks.

Abstention stays first-class and legible: the request was properly scoped, the designed route ran or fell back to a known partial route, the evidence was judged on the common surface, and the package was still not strong enough. That is a useful outcome, not an embarrassing one.

Non-goals: final wording, multi-turn clarification, query rewriting to save weak requests, resolving policy contradictions, converting abstention into escalation prose — all belong above Skywalker. And this section does not retrofit a source-of-truth hierarchy; control versus black-box leverage was encoded upstream, and smuggling a source preference into scoring would re-litigate it dishonestly.

### 8. Calibration surfaces

**Surface one — reranker pool size** (20 at launch; 10 per arm dual-arm, 20 from a survivor or on FAQ-only). Re-litigate if the surface is consistently starved of useful alternatives or flooded with tail candidates that never survive.

**Surface two — shortlist size** (`/skywalker/runtime/retrieval/shortlist_size`, 5). Re-litigate if agents routinely have too little grounding or too much loosely ranked material.

**Surface three — the abstain floor** (`/skywalker/runtime/abstain/floor`, 0.30) and the 5–15% target band. Re-litigate on judged traffic showing over-abstention on answerable requests or over-answering on weak ones. The floor moves freely; the two-branch *structure* moves only per Decision 11's reopen condition.

**Surface four — reranker-failure fallback posture.** The honest framing is not "hard-abstain versus formalize" but: *does the system tolerate losing abstain-on-weakness exactly when scoring is least reliable?* Three candidate postures: hard abstain on reranker failure (restores tenet 2, converts every outage into visible non-answers); a fallback-specific answerability heuristic (e.g., an arm-local floor on the FAQ hybrid-fused score for FAQ-only routes — partial, since cross-arm pools have no commensurate signal); or the launch status quo with prompt-side conservatism. Re-litigate on the first operational reranker outage's reviewed traffic — the evidence this surface needs is exactly the traffic the inversion endangers.

**Surface five — shortlist source diversity.** No caps at launch; re-litigate if one document or arm dominates in ways that reduce package value.

**Surface six — a constrained source-aware tie-break.** Excluded at launch; re-litigate only on evidence of systematic near-tie misresolution that a narrow rule would fix without distorting the common surface.

**Surface seven — the scored text surface.** Title-plus-body at launch; re-litigate if titles prove to be noise, or if title context proves so load-bearing that body-only scoring is systematically weaker (Section 04 §8 surface eight is this same surface from the producer side).

### 9. Open questions

Most open questions across this series share one precondition, stated in full in Section 10 §9: they are only answerable against real user data at meaningful volume — a few hundred actual users, arriving with the September production launch. Until then, launch postures stand, and pre-launch pressure to move them resolves as a recorded non-change.

**Instance selection — pending executive approval; gates the endpoint build-out** *(the Section 01 §9 gating question, owned operationally here).* The bake-off across `ml.g5.xlarge`, `ml.g5.2xlarge`, and `ml.p5.4xlarge` decides cost-versus-latency fit and simultaneously answers whether the chosen type holds 350 ms p95 on real payloads — public p50 estimates (180–300 ms) are H100-class, the A10G types will be slower, and no per-instance benchmark exists. The outcome also settles the gate-endpoint question (dedicated light endpoint versus shared fleet, and whether the gate stage runs the Pro variant or a lighter v4 family member) and the final cost envelope.

**Marketplace Model Package ARN** *(disclaimer: surfaces mechanically after subscription acceptance; nothing to decide).*

**Endpoint region** *(resolved in direction — co-regional with the prod query service, no global fleet, per Section 01 §9; lands concretely with the prod-region consistency decision).*

**Reranker-failure fallback conversion** *(disclaimer: launch posture is decided; this is the §8 surface-four trigger, recorded for visibility).* Whether operational traffic justifies converting the flagged fallback to hard abstain.

**Abstain message differentiation** *(disclaimer: staging-quality question, Section 10 C-09/C-02; does not gate this layer).* Whether the two abstain classes produce distinguishably useful user-facing messages from QuickSuite and Slack.

**A future cross-arm-disagreement abstain category** *(disclaimer: deliberately open evaluation question; any future category needs a measurable signal computable without an LLM-side verifier — Decision 11's bar).*

### Closing position

The two-arm architecture only works if it converges into one honest scoring and answerability layer, and this is that layer. It takes the normalized candidate universe, scores it on one common Rerank v4.0 Pro surface — whole-node evidence, minimally rendered, source labels kept out of the scored text — forms one bounded shortlist, and decides against an SSM-held floor whether that shortlist passes upward as answerable or returns as one of exactly two structured abstentions. Reranker failure degrades visibly into a flagged retrieval-order package rather than silently or catastrophically. The instance powering the surface is the one deliberately open question, pending the executive bake-off; everything the rest of the system depends on — the model family, the contract, the two-branch abstain rule, the separation of routing from answerability — is fixed.

Retrieval arms can disagree. Black-box and owned evidence can coexist. None of it becomes a trustworthy backend until there is one place where heterogeneous evidence is judged together and one place where the backend is allowed to say the package is not good enough. This section is both places.

---

*Stale-source flags raised in this section, for propagation: prior Section 07 fixed `ml.p5.4xlarge`/us-east-1/$19K posture (instance pending the bake-off; region follows the prod query service); prior Section 07 Decision 12 and flow steps for sibling/linked-parent expansion, citation markers, `citations[]`, `linked_text_token_cap` (C-23), `LinkedItemSuppressed`/`LinkedSegmentTruncated`, and child-count reconstruction (all superseded by whole-node fragments, Section 03 Decision 5; envelope change recorded in Section 02); prior Section 07 "1000-token child ceiling" rationale for `max_tokens_per_doc` (superseded — the default stands on whole-node sizing); [API_10] Part A's fixed instance/region claims (re-flagged per Section 01).*

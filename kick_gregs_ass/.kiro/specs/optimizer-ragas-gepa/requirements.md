# Requirements Document

## Introduction

This feature overhauls the existing closed-loop prompt optimizer (the **Optimizer**,
under `bakeoff/quality/optimizer/`) to integrate two external open-source frameworks the
study now treats as adopted learnings:

- **ragas** — a RAG-evaluation library (`vibrantlabsai/ragas`, the v0.3 → v0.4 line)
  providing LLM-as-judge metrics (`Faithfulness`, `FactualCorrectness`,
  `ContextPrecision`, `ContextRecall`) runnable through an Amazon Bedrock adapter.
- **GEPA** — reflective prompt evolution (the standalone `gepa-ai/gepa` engine, also the
  basis of DSPy's `GEPA` optimizer). GEPA (Genetic-Pareto) maintains a population of
  candidate prompts, uses an LLM reflection step driven by natural-language feedback from
  the metric to propose edits, selects candidates on a Pareto frontier, and merges
  complementary lessons.

The work is delivered in two build tiers, both written toward in this spec. The scope of
the two tiers is fixed by an owner decision already taken; this document does not re-open
the tier discussion.

**Tier 1 (build now, reversible, config-flag gated).** Wire ragas into the judge as a
**secondary, non-deciding** signal — exactly the role the existing `Closeness`
cross-check plays today:

- ragas `Faithfulness` and `FactualCorrectness` recorded on every `TurnVerdict` as a
  cross-check that never decides promotion.
- ragas `ContextPrecision` and `ContextRecall` recorded per turn as a **retrieval
  diagnostic** answering the question "is the gold node actually present in the retrieved
  fragments?" — the exact diagnosis the `optimizer-quality-uplift` Effort A / A1 work is
  performing by hand today.
- Everything additive, behind a config flag, offline-testable with fakes, matching the
  existing `build_offline_backend` / `build_live_backend` discipline. Nothing existing is
  removed and no existing decision role changes.

**Tier 2 (the build target this spec is written toward).** Selectively replace the
hand-rolled search/promotion machinery with the standalone **GEPA engine**:

- GEPA's reflective proposer + Pareto frontier + merge replace `AuthorClient` and the
  hand-rolled promotion / island / tournament / merge code (the hand-ported B1/B2/B3
  mechanics in `optimizer-quality-uplift/tasks.md` become "configure GEPA" instead of
  "build it").
- The existing Opus judge triad becomes GEPA's metric, returning `(score, feedback_text)`
  while keeping the harness's own metric and abstention weighting.
- The ragas metrics are promoted from Tier-1 cross-checks to real, named judge
  **dimensions** the Optimizer can target, so the dashboard can show which axis a rewrite
  moved.
- The coverage-ladder cadence becomes GEPA's rollout / budget policy.
- The Bedrock `InvokeInlineAgent` persistent-session answer path, OpenSearch retrieval,
  the SSE dashboard, and the held-constant retrieval substrate are **kept** (not
  replaced).

**Tier 3 (out of scope for building; captured as a future platform decision only).** A
full DSPy-program migration (answer path + retrieval + judge as DSPy modules, a custom
`dspy.LM` Bedrock `InvokeInlineAgent` adapter, MIPROv2 / BootstrapFewShot demo
optimization, ragas as the full eval harness plus synthetic test-data generation and
align-LLM-as-judge). This is the previously-deferred "Effort C". It is noted as future
scope; this spec writes no build requirements for it.

### Sourcing and methodology honesty caveat

ragas, DSPy, and GEPA are **external / industry open-source frameworks**, not
Amazon-internal guidance. No Amazon-internal primary source (BuilderHub Golden Path,
internal code search, AWS Prescriptive Guidance) was consulted for this document, and
none applies — this is a deliberately throwaway, local research harness, and the
internal-source tools were not available in this execution environment. Every
methodology choice here is grounded in external/industry RAG-evaluation and
prompt-optimization practice plus this repo's own observed Opus verdicts, and any
judge-derived number MUST be re-validated before it is used to defend a decision upward.
The ragas Amazon Bedrock endpoint and model assumptions and any GEPA rollout / budget
numbers are **assumptions to confirm at implementation time**, not verified facts. This
mirrors the sourcing caveat already carried by `closed-loop-prompt-optimizer` and
`bakeoff/README.md`.

## Glossary

- **Optimizer**: The existing closed-loop prompt optimizer under
  `bakeoff/quality/optimizer/` that this feature overhauls.
- **Tier_1**: The build-now scope — ragas added as a secondary, non-deciding signal,
  config-flag gated and reversible.
- **Tier_2**: The build-target scope — the standalone GEPA engine replaces the hand-rolled
  search machinery and ragas becomes named judge dimensions.
- **Tier_3**: The full DSPy-program migration. Out of scope for building in this spec;
  captured only as a documented future platform decision.
- **Judge**: The real Opus big judge (`JudgeScorer` in `bakeoff/scoring/judge.py`, backed
  by the Bedrock model in `config.JUDGE_MODEL_ID`) that scores a turn on the
  faithfulness / correctness / completeness triad. The sole promotion-decision metric.
- **Judge_Triad_Score**: The Judge's abstention-weighted overall score for a turn or
  prompt on the 0.0-1.0 scale, aggregated the way the Quality_Study reports it.
- **TurnVerdict**: The per-turn verdict record produced by `JudgeInLoopScorer`
  (`judge_loop.py`); where the ragas cross-check and retrieval-diagnostic signals attach
  in Tier 1.
- **SliceScore**: The aggregate per-prompt score over a slice produced by
  `JudgeInLoopScorer`.
- **Closeness**: The existing semantic-cosine proxy recorded as a secondary, non-deciding
  cross-check on every TurnVerdict. The role ragas signals occupy in Tier 1.
- **Ragas_Cross_Check**: The Tier-1 component that computes ragas `Faithfulness` and
  `FactualCorrectness` and records them on each TurnVerdict as non-deciding cross-checks.
- **Retrieval_Diagnostic**: The Tier-1 component that computes ragas `ContextPrecision`
  and `ContextRecall` per turn against the turn's gold reference, answering whether the
  gold node is present in the retrieved fragments.
- **Ragas_Adapter**: The seam that runs ragas metrics. On the Live_Backend it invokes
  ragas through the ragas Amazon Bedrock adapter using the harness's evaluation models; on
  the Offline_Backend it is a deterministic, network-free fake.
- **Ragas_Signal**: Any score produced by the Ragas_Cross_Check or the
  Retrieval_Diagnostic (Tier 1) or by a named ragas JudgeDimension (Tier 2).
- **GEPA_Engine**: The standalone `gepa-ai/gepa` (Genetic-Pareto) engine. Provides the
  reflective proposer, the Pareto frontier, and the merge operation that, in Tier 2,
  replace the hand-rolled search machinery.
- **Reflective_Proposer**: GEPA's LLM reflection step that proposes prompt edits from the
  metric's natural-language feedback. Replaces `AuthorClient` in Tier 2.
- **Pareto_Frontier**: GEPA's set of candidates that are best on at least one example.
  Replaces the hand-rolled island / tournament selection in Tier 2.
- **Merge**: GEPA's operation that combines complementary candidates. Replaces the
  hand-rolled merge mechanic (B3) in Tier 2.
- **GEPA_Metric**: The adapter that presents the Opus Judge to the GEPA_Engine, returning
  for each evaluated candidate a scalar score (derived from the Judge_Triad_Score with the
  harness's abstention weighting) and a feedback text (derived from the Judge's per-turn
  evidence).
- **Feedback_Text**: The natural-language critique the GEPA_Metric returns alongside the
  scalar score, which the Reflective_Proposer reads.
- **JudgeDimension**: A named axis the Optimizer can target and the dashboard can display.
  In Tier 2 the ragas metrics become named JudgeDimensions alongside the
  faithfulness / correctness / completeness triad.
- **Rollout_Budget**: The GEPA_Engine's policy governing how many rollouts / evaluations a
  candidate earns. In Tier 2 it is configured from the coverage-ladder cadence.
- **Coverage_Ladder**: The existing escalating-coverage cadence (nested seeded stratified
  rungs of growing size with per-rung reps) that becomes the GEPA Rollout_Budget in
  Tier 2.
- **AuthorClient**: The current hand-rolled proposer (`author.py`) that the
  Reflective_Proposer replaces in Tier 2.
- **Inline_Agent_Answer_Path**: The Bedrock Agent Runtime `InvokeInlineAgent`
  persistent-session answer path (`inline_session_adapter.py`). On the Tier-2 KEEP list.
- **RetrievalBackend**: The held-constant, read-only, per-(turn-query) memoized retrieval
  substrate (`retrieval.py`): OpenSearch preferred, local fallback, fake offline. On the
  Tier-2 KEEP list.
- **Dashboard**: The existing SSE-streamed Quality_Tab in the `bakeoff/ui/` SPA. On the
  Tier-2 KEEP list.
- **Configuration**: The harness config surface in `bakeoff/config.py` where flags and
  tunables live.
- **Offline_Backend**: The deterministic, zero-network backend bundle from
  `build_offline_backend` (offline adapter, fake embedder, `StubJudge`, fake retrieval,
  offline author). Extended here with a fake Ragas_Adapter.
- **Live_Backend**: The real Bedrock-backed backend bundle from `build_live_backend`
  (inline-agent adapter, real Opus judge, Embed v4, OpenSearch retrieval, Bedrock author).
  Extended here with the ragas Bedrock adapter and, in Tier 2, the GEPA_Engine.
- **Tuning_Slice**: The held-out ~20% slice of the multi-turn dataset the Optimizer
  iterates / searches on. The proposer only ever sees failures drawn from here.
- **Validation_Set**: The reserved ~80% complement the converged Champion is evaluated on.
  The proposer never sees it.
- **Champion / Challenger**: The current best prompt for a Target_Model, and a candidate
  prompt evaluated against it.
- **Abstention**: A Target_Model correctly declining when the retrieved fragments do not
  support a confident grounded answer. A first-class, scored behavior — rewarded when
  correct, penalized when the model answers-when-unsure.

## Requirements

---

## Group A — Tier 1: ragas as a secondary, non-deciding signal (build now)

### Requirement 1: ragas Faithfulness and FactualCorrectness as a non-deciding cross-check

**User Story:** As the study owner, I want ragas `Faithfulness` and `FactualCorrectness`
recorded on every TurnVerdict as a cross-check, so that I gain an independent grounding
signal without changing what decides promotion.

#### Acceptance Criteria

1. WHERE the Ragas_Cross_Check is enabled, WHEN the Judge produces a TurnVerdict for a
   turn, THE Optimizer SHALL record a ragas Faithfulness score and a ragas
   FactualCorrectness score on that TurnVerdict.
2. THE Optimizer SHALL compute the ragas Faithfulness score and the ragas
   FactualCorrectness score from the same retrieved fragments and the same answer text the
   Judge received for that turn.
3. THE Optimizer SHALL treat the ragas Faithfulness score and the ragas FactualCorrectness
   score as secondary cross-check signals occupying the same non-deciding role that
   Closeness occupies.
4. THE Optimizer SHALL keep the Judge_Triad_Score as the sole promotion-decision metric
   while the Ragas_Cross_Check is enabled.
5. WHERE the Ragas_Cross_Check is disabled, THE Optimizer SHALL produce TurnVerdicts whose
   decision-affecting fields are identical to the TurnVerdicts produced before the
   Ragas_Cross_Check existed.

### Requirement 2: ragas ContextPrecision and ContextRecall as a per-turn retrieval diagnostic

**User Story:** As the study owner, I want ragas `ContextPrecision` and `ContextRecall`
computed per turn against the gold reference, so that I can see whether the gold node is
actually present in the retrieved fragments — the question the Effort A / A1 diagnosis
answers by hand today.

#### Acceptance Criteria

1. WHERE the Retrieval_Diagnostic is enabled, WHEN the held-constant RetrievalBackend
   returns fragments for a turn, THE Optimizer SHALL compute a ragas ContextPrecision
   score and a ragas ContextRecall score for that turn against the turn's gold reference.
2. THE Retrieval_Diagnostic SHALL operate only on the fragments the held-constant
   RetrievalBackend returned for that turn.
3. THE Retrieval_Diagnostic SHALL record, per turn, whether the turn's gold node
   identifier appears among the retrieved fragment identifiers.
4. THE Optimizer SHALL record the ragas ContextPrecision score and the ragas
   ContextRecall score per turn as retrieval diagnostics distinct from the
   Judge_Triad_Score.
5. THE Optimizer SHALL keep the Retrieval_Diagnostic signals out of the promotion
   decision, retaining the Judge_Triad_Score as the sole promotion-decision metric.

### Requirement 3: Config-flag gating and full reversibility (Tier 1)

**User Story:** As the study owner, I want all Tier-1 ragas integration behind a config
flag and fully reversible, so that I can turn it off and the Optimizer behaves exactly as
it does today.

#### Acceptance Criteria

1. THE Configuration SHALL expose a flag that enables or disables the Ragas_Cross_Check
   and a flag that enables or disables the Retrieval_Diagnostic.
2. THE Ragas_Cross_Check flag and the Retrieval_Diagnostic flag SHALL default to disabled.
3. WHERE the Ragas_Cross_Check and the Retrieval_Diagnostic are both disabled, THE
   Optimizer SHALL preserve every existing decision role, score, recorded field, and
   stored-record shape unchanged.
4. THE Optimizer SHALL add the Ragas_Signals as additive fields that preserve the existing
   TurnVerdict and SliceScore fields.
5. IF a ragas computation fails for a turn, THEN THE Optimizer SHALL record the affected
   Ragas_Signal for that turn as absent and SHALL continue the iteration using the
   Judge_Triad_Score.

### Requirement 4: ragas Amazon Bedrock adapter and model parity (Tier 1)

**User Story:** As the study owner, I want ragas to run through its Amazon Bedrock adapter
using the same models the harness already uses, so that the cross-check is consistent with
the evaluation substrate.

#### Acceptance Criteria

1. WHERE the Ragas_Cross_Check or the Retrieval_Diagnostic is enabled on the
   Live_Backend, THE Ragas_Adapter SHALL invoke ragas metrics through the ragas Amazon
   Bedrock adapter.
2. THE Ragas_Adapter SHALL use the Bedrock evaluation models the harness already uses, as
   recorded in the Configuration.
3. THE Ragas_Adapter SHALL read the ragas Bedrock endpoint and model identifiers from the
   Configuration rather than from hard-coded literals.
4. THE Configuration SHALL mark the ragas Bedrock endpoint and model identifiers as
   assumptions to confirm at implementation time.

### Requirement 5: Offline testability with fakes (Tier 1)

**User Story:** As the study owner, I want the ragas integration offline-testable with
fakes, matching the existing offline/live backend discipline, so that the pipeline
validates with zero network.

#### Acceptance Criteria

1. THE Offline_Backend SHALL provide a network-free fake Ragas_Adapter that returns
   deterministic Ragas_Signals.
2. WHERE the Offline_Backend is selected, THE Ragas_Cross_Check and the
   Retrieval_Diagnostic SHALL perform zero network calls.
3. THE Optimizer SHALL inject the Ragas_Adapter through the same backend-bundle seam used
   for the Judge, the closeness scorer, and the RetrievalBackend, so the whole outside
   world is swapped in one move.
4. THE Optimizer SHALL record which backend produced each Ragas_Signal so a reader can
   tell whether a signal came from the Offline_Backend or the Live_Backend.

---

## Group B — Tier 2: GEPA engine replaces the hand-rolled search machinery (build target)

### Requirement 6: GEPA engine replaces the hand-rolled search and promotion machinery

**User Story:** As the study owner, I want the standalone GEPA engine to replace the
hand-rolled author / promotion / island / tournament / merge code, so that the search
machinery is configured rather than maintained.

#### Acceptance Criteria

1. WHERE Tier_2 is enabled, THE Optimizer SHALL use the GEPA_Engine's Reflective_Proposer
   to generate candidate prompts in place of the AuthorClient.
2. WHERE Tier_2 is enabled, THE Optimizer SHALL use the GEPA_Engine's Pareto_Frontier to
   retain candidates in place of the hand-rolled island and tournament selection.
3. WHERE Tier_2 is enabled, THE Optimizer SHALL use the GEPA_Engine's Merge operation in
   place of the hand-rolled merge mechanic.
4. THE Optimizer SHALL configure the GEPA_Engine rather than re-implement reflective
   proposal, Pareto selection, or merge.
5. THE Optimizer SHALL supersede the `optimizer-quality-uplift` Effort B hand-port
   (B1 feedback-shaped metric, B2 Pareto frontier, B3 merge) with the GEPA_Engine while
   reusing the Effort A evaluation-signal fix.

### Requirement 7: The Opus judge triad becomes GEPA's metric, returning score and feedback

**User Story:** As the study owner, I want the existing Opus judge triad to act as GEPA's
metric, returning a score and textual feedback, so that GEPA optimizes against the
harness's own metric and abstention weighting.

#### Acceptance Criteria

1. WHERE Tier_2 is enabled, WHEN the GEPA_Engine evaluates a candidate, THE GEPA_Metric
   SHALL return a scalar score derived from the Judge_Triad_Score and a Feedback_Text
   derived from the Judge's per-turn evidence.
2. THE GEPA_Metric SHALL apply the harness's abstention weighting when computing the
   scalar score.
3. THE GEPA_Metric SHALL derive the scalar score from the same Opus Judge implementation
   the rest of the Quality_Study uses.
4. THE Optimizer SHALL keep the Judge_Triad_Score as the sole promotion-decision metric
   within the GEPA_Engine.
5. THE GEPA_Metric SHALL provide the Feedback_Text to the Reflective_Proposer so that
   proposals are conditioned on why a turn scored as it did.

### Requirement 8: ragas metrics promoted to named judge dimensions (Tier 2)

**User Story:** As the study owner, I want ragas metrics promoted from Tier-1 cross-checks
to named judge dimensions GEPA can target, so that the dashboard shows which axis a
rewrite moved.

#### Acceptance Criteria

1. WHERE Tier_2 is enabled, THE GEPA_Metric SHALL expose the ragas metrics as named
   JudgeDimensions alongside the faithfulness / correctness / completeness triad.
2. THE GEPA_Metric SHALL attribute each candidate's score movement to the named
   JudgeDimensions.
3. WHEN a candidate is accepted, THE Dashboard SHALL display the per-dimension movement
   including the ragas-derived JudgeDimensions.
4. THE Optimizer SHALL keep the named ragas JudgeDimensions feeding the single
   Judge_Triad_Score decision rather than acting as independent competing deciders.

### Requirement 9: Coverage-ladder cadence becomes GEPA's rollout/budget policy (Tier 2)

**User Story:** As the study owner, I want the coverage-ladder cadence expressed as GEPA's
rollout / budget policy, so that the escalating-coverage idea is preserved within GEPA.

#### Acceptance Criteria

1. WHERE Tier_2 is enabled, THE Optimizer SHALL configure the GEPA_Engine's Rollout_Budget
   from the Coverage_Ladder cadence.
2. THE Optimizer SHALL read the GEPA Rollout_Budget values from the Configuration.
3. THE Configuration SHALL mark the GEPA Rollout_Budget numbers as assumptions to confirm
   at implementation time.

### Requirement 10: KEEP list — proven substrate is not replaced (Tier 2)

**User Story:** As the study owner, I want the proven substrate kept intact while GEPA
replaces only the search machinery, so that the answer path, retrieval, and dashboard are
preserved.

#### Acceptance Criteria

1. WHERE Tier_2 is enabled, THE Optimizer SHALL keep the Bedrock `InvokeInlineAgent`
   Inline_Agent_Answer_Path.
2. WHERE Tier_2 is enabled, THE Optimizer SHALL keep the OpenSearch RetrievalBackend.
3. WHERE Tier_2 is enabled, THE Optimizer SHALL keep the SSE Dashboard streaming surface.
4. WHERE Tier_2 is enabled, THE Optimizer SHALL keep the held-constant retrieval
   substrate.
5. THE Optimizer SHALL confine the GEPA_Engine to the proposal, selection, and merge role
   and SHALL keep every KEEP-listed component's behavior unchanged.

---

## Group C — Non-functional requirements and load-bearing invariants

### Requirement 11: The Opus judge triad is the sole promotion-decision metric across both tiers

**User Story:** As the study owner, I want the Opus judge triad to remain the only thing
that decides promotion, so that ragas never becomes an independent competing decider.

#### Acceptance Criteria

1. THE Optimizer SHALL use the Judge_Triad_Score as the sole promotion-decision metric in
   Tier_1 and Tier_2.
2. WHERE the Ragas_Cross_Check, the Retrieval_Diagnostic, or the named ragas
   JudgeDimensions are enabled, THE Optimizer SHALL keep the Ragas_Signals as inputs that
   never independently decide promotion.
3. THE Optimizer SHALL keep Closeness as a non-deciding secondary cross-check.

### Requirement 12: Author/proposer and judge model separation is preserved

**User Story:** As the study owner, I want the proposer model to stay different from the
judge model, so that the judge never grades a prompt authored by itself and the loop does
not contend with itself for Opus quota.

#### Acceptance Criteria

1. THE Optimizer SHALL use a proposer model — the Reflective_Proposer in Tier_2, the
   AuthorClient in Tier_1 — that is a different model from the Judge.
2. IF the proposer model and the Judge model are configured to be the same model, THEN THE
   Optimizer SHALL refuse to start and SHALL report the conflict.
3. THE Optimizer SHALL reserve the Opus model for the Judge role.

### Requirement 13: Retrieval is held constant, read-only, and memoized byte-identical

**User Story:** As the study owner, I want retrieval held constant and read-only across
champion and challenger, so that the only varied element remains the system-instruction
text.

#### Acceptance Criteria

1. THE Optimizer SHALL invoke the RetrievalBackend as a read-only substrate that the
   Optimizer does not tune or mutate.
2. THE Optimizer SHALL memoize retrieval per (turn-query) so the Champion and the
   Challenger receive byte-identical fragments for the same turn.
3. WHERE the Retrieval_Diagnostic computes ragas context metrics, THE Retrieval_Diagnostic
   SHALL read the same memoized fragments without issuing its own retrieval query.
4. THE Optimizer SHALL keep retrieval identical across Champion and Challenger so the only
   varied element is the system-instruction text.

### Requirement 14: Retrieval-always with fragments-only grounding and first-class abstention

**User Story:** As the study owner, I want retrieval invoked on every turn with fragments
rendered inline as the only grounding and abstention scored as a first-class behavior, so
that grounding and decline discipline are preserved across both tiers.

#### Acceptance Criteria

1. THE Optimizer SHALL invoke the RetrievalBackend on every turn and SHALL render the
   retrieved fragments inline as the only permitted grounding.
2. WHERE the retrieved fragments are insufficient for a turn, THE Optimizer SHALL reward a
   correct decline and SHALL penalize answering-when-unsure.
3. THE Optimizer SHALL keep Abstention a first-class scored behavior in the metric in
   Tier_1 and Tier_2.

### Requirement 15: Two-phase train/test boundary is preserved

**User Story:** As the study owner, I want the Phase A tuning slice and Phase B validation
boundary preserved, so that the proposer never sees validation items and the reported
number is not measured on tuning data.

#### Acceptance Criteria

1. THE Optimizer SHALL run Tier_1 and Tier_2 search exclusively on the Tuning_Slice
   (~20%).
2. WHEN search has converged for a Target_Model, THE Optimizer SHALL validate the
   converged Champion on the Validation_Set (~80%).
3. THE Optimizer SHALL keep the Validation_Set hidden from the proposer at all times.
4. THE train/test split SHALL be deterministic and seeded so the Tuning_Slice and
   Validation_Set are reproducible across runs.

### Requirement 16: Local, loopback-only, throwaway research posture

**User Story:** As the study owner, I want the harness to stay a local, loopback-only,
no-auth, no-PII throwaway tool, so that adding ragas and GEPA does not change its posture.

#### Acceptance Criteria

1. THE Optimizer SHALL bind only to local loopback interfaces and SHALL operate with no
   authentication and no PII.
2. THE Optimizer SHALL run on a local throwaway virtual environment with local Qdrant /
   OpenSearch, packaged as a local harness rather than a Brazil package.
3. THE Dashboard tooling SHALL use bun for JavaScript package management and SHALL avoid
   npm and npx.
4. THE Optimizer SHALL add the ragas and GEPA dependencies to the local environment using
   the existing Bedrock credential chain, introducing no new secrets.

### Requirement 17: Tier-1 changes are additive and decision-role-preserving

**User Story:** As the study owner, I want Tier 1 to be strictly additive and to leave
every existing decision role intact, so that adopting ragas as a cross-check is risk-free
and removable.

#### Acceptance Criteria

1. THE Optimizer SHALL implement Tier_1 as additive behavior that removes no existing
   capability.
2. WHERE Tier_1 is enabled, THE Optimizer SHALL leave the promotion decision, the
   convergence stop rule, and the Phase A / Phase B boundary unchanged.
3. WHERE Tier_1 is disabled, THE Optimizer SHALL run with the same behavior it had before
   this feature existed.

### Requirement 18: External-source honesty caveat is recorded with the feature

**User Story:** As the study owner, I want the external-source honesty caveat carried with
this feature, so that the same sourcing discipline the other specs enforce is preserved.

#### Acceptance Criteria

1. THE Optimizer's documentation SHALL state that ragas, DSPy, and GEPA are external
   open-source frameworks and not Amazon-internal guidance.
2. THE Optimizer's documentation SHALL state that no Amazon-internal primary source was
   consulted and that none applies to this throwaway local harness.
3. THE Optimizer's documentation SHALL flag the ragas Bedrock endpoint / model assumptions
   and any GEPA budget numbers as assumptions to confirm at implementation time.
4. THE Optimizer's documentation SHALL state that any judge-derived number must be
   re-validated before it is used to defend a decision upward.

---

## Out of Scope — Tier 3 (future platform decision; no build requirements here)

Tier 3 is the full DSPy-program migration (the previously-deferred "Effort C"). It is
**not** built in this spec and carries **no** build requirements. It is recorded here only
as a documented future platform decision, so that the boundary between "configure GEPA"
(Tier 2) and "migrate to DSPy" (Tier 3) stays explicit:

- Re-express the answer path, retrieval, and judge as DSPy modules.
- Add a custom `dspy.LM` adapter over the Bedrock `InvokeInlineAgent` persistent-session
  answer path.
- Use DSPy demo optimization (MIPROv2 / BootstrapFewShot) for few-shot / demo selection.
- Adopt ragas as the full evaluation harness, including synthetic test-data generation and
  align-LLM-as-judge calibration.

Should Tier 3 be taken up later, it would supersede parts of this spec's Tier 2 wiring and
would be scoped as its own platform-decision spec. Until then, the GEPA_Engine integration
(Tier 2) is the build target and the Tier-2 KEEP list (the inline-agent answer path,
OpenSearch retrieval, the SSE dashboard, and the held-constant retrieval substrate) stays
in place.

## Consistency note with `optimizer-quality-uplift`

This spec supersedes the hand-ported GEPA mechanics in
`.kiro/specs/optimizer-quality-uplift/tasks.md` (B1 feedback-shaped metric, B2 Pareto
frontier, B3 merge) with the real standalone GEPA engine for Tier 2, while **reusing** the
Effort A evaluation-signal fix (full-body gold reference / answerability plumbing /
retrieval-presence diagnosis). The Tier-1 Retrieval_Diagnostic in Requirement 2 is the
mechanized form of the Effort A / A1 "is the gold node in the retrieved fragments?"
diagnosis that work performs by hand. The two specs are intended to stay consistent: where
`optimizer-quality-uplift` says "port GEPA mechanics by hand," this spec says "configure
the GEPA engine instead."

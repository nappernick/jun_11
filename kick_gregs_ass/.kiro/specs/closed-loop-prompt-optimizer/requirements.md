# Requirements Document

## Introduction

This feature replaces the quality study's current **one-shot prompt selector** with a
genuine **closed-loop iterative prompt optimizer**. Today, the quality study
(`bakeoff/quality/`) ranks a fixed hand-written menu of five prompt "variants" (the
lever ladder in `prompts.py`) on a cheap semantic-cosine "closeness" metric, picks a
winner, then runs and judges it. That mechanism is a *selector over a fixed menu*; it
neither learns from failures nor rebuilds the prompt.

The new optimizer runs a champion/challenger loop that **learns and rewrites the
prompt over iterations**, scored by the real Opus big judge rather than the cosine
proxy. Each iteration scores the current champion prompt on a held-out tuning slice
with the judge, collects the lowest-scoring judged turns with the judge's evidence,
feeds those failures plus the current prompt to a separate **author model** that
proposes a genuinely rewritten challenger prompt, scores the challenger, and promotes
it only if it beats the champion by a noise-floor-grounded significance threshold. The
loop converges by a configurable stop rule, then a separate validation phase runs the
converged champion on the untouched complement of the dataset at higher reps with
confidence intervals. Every iteration is persisted to an append-only audit trail with
full prompt text, diff, author rationale, judge score with CI, and accept/reject
decision. The whole process streams live into the existing dashboard "quality tab" so
the owner can watch the champion vs challenger scores, the author model's reasoning,
the current prompt, and the diff against prior versions as the loop runs.

This is the **quality study only**. The two quality-target models are fixed by owner
decision: `sonnet-4.6-thinking-off` and `haiku-4.5`. The retrieval substrate is now
**invoked on every turn** of the quality path (the path is no longer fragment-free) and
its retrieved fragments are the model's only permitted grounding, but the substrate
remains **held constant** — it is not a variable the Optimizer tunes, and its logic and
corpus must not be changed (Requirements 13 and 16). The separate speed/quality bake-off
is explicitly out of scope and must not be changed. This is a local, loopback-only
research harness with no auth and no PII.

### Sourcing and methodology honesty caveat

The evaluation-metric and judge-methodology choices referenced in these requirements
(LLM-as-judge faithfulness/correctness/completeness triad, RAG over-refusal and
topic-bleed failure modes, between-conversation standard deviation and confidence-
interval reasoning for the significance threshold) are grounded in **external /
industry RAG-evaluation literature and in this repo's own observed Opus verdicts**,
**not** in Amazon-internal primary sources. The internal primary sources (BuilderHub
Golden Path, internal code search, AWS Prescriptive Guidance) were **not available in
this execution environment**. Where these requirements assert a methodology choice,
that choice MUST be read as grounded in external practice plus locally observed data,
and re-validated against internal guidance before any number it produces is used to
defend a decision upward. This mirrors the sourcing caveat already documented in
`bakeoff/README.md`.

Two further external/owner-provided sources are introduced by Requirements 15 and 16 and
are flagged here with the same honesty:

- The modern Claude 4.5 system-prompting guidance referenced in Requirement 15 (the
  Prompting_Guidance) is derived from an **external / vendor source** (a Claude 4.5
  prompt-engineering analysis captured in `modern_system_prompting.pdf`), **not** an
  Amazon-internal primary source. It MUST be treated as external practice, useful
  because the Target_Models are members of the same Claude 4.x family, and not presented
  as Amazon-blessed guidance.
- The ALPHA OpenSearch retrieval endpoint, index, and authentication details referenced
  in Requirement 16 are **owner-provided** operational facts (AWS account
  `948580600005`), not values verified against an internal primary source in this
  execution environment. The specific endpoint/index/auth MUST be treated as an
  assumption to confirm with the owner at implementation time, which is why Requirement
  16 mandates a guaranteed-workable local fallback rather than an OpenSearch-only
  dependency.

## Glossary

- **Quality_Study**: The self-contained multi-turn study under `bakeoff/quality/` that
  measures, per turn, how close each target model's answer is to the correct answer.
  The only study this feature modifies.
- **Closed_Loop_Optimizer**: The new champion/challenger iterative prompt optimizer
  that replaces the one-shot selector. Referred to below as the **Optimizer**.
- **Optimizer**: Short name for the Closed_Loop_Optimizer.
- **Target_Model**: One of the two fixed models under test in the Quality_Study:
  `sonnet-4.6-thinking-off` or `haiku-4.5`. These are the models whose prompts are
  optimized and whose answers are judged.
- **Judge**: The real Opus big judge (`JudgeScorer` backed by Bedrock model
  `us.anthropic.claude-opus-4-8`) that scores a turn on the faithfulness, correctness,
  and completeness triad. The single authoritative optimization signal.
- **Judge_Triad_Score**: The Judge's overall score for a turn or prompt, on a 0.0-1.0
  scale, derived from the faithfulness/correctness/completeness dimensions, aggregated
  the way the Quality_Study reports it (mean over turns, then over items).
- **Author**: The model that proposes a revised prompt given the current champion
  prompt and its lowest-scoring judged failures. It MUST be a different model from the
  Judge. The current leaning is Sonnet 4.6 as Author.
- **Champion**: The current best prompt for a Target_Model, as decided by the Judge on
  the Tuning_Slice.
- **Challenger**: A candidate prompt proposed by the Author in one iteration, scored
  against the Champion.
- **Closeness**: The cheap semantic-cosine proxy metric currently used as the decision
  signal. Under the new design it is a recorded secondary cross-check only, never the
  decision metric.
- **Significance_Threshold**: The minimum absolute Judge_Triad_Score gain (champion
  over previous champion, on the 0.0-1.0 scale) that counts as a real improvement.
  Default 0.05, configurable.
- **Tuning_Slice**: The held-out ~20% slice of the multi-turn dataset (~60
  conversations) that the Optimizer iterates on. Maps to the held-out portion of the
  existing `split_items`.
- **Validation_Set**: The reserved ~80% complement of the multi-turn dataset that the
  converged Champion is evaluated on in Phase B. The Author never sees it.
- **Phase_A**: The iterate phase. The Optimizer runs the champion/challenger loop on
  the Tuning_Slice until the stop rule fires.
- **Phase_B**: The validate phase. The converged Champion is run on the Validation_Set
  at higher reps with confidence intervals.
- **Iteration**: One full champion/challenger cycle in Phase_A (score champion,
  collect failures, author challenger, score challenger, accept/reject).
- **Audit_Record**: The persisted per-iteration record (prompt text, diff, author
  rationale, judge score with CI, driving failures, accept/reject).
- **Audit_Store**: The append-only store of Audit_Records.
- **Confidence_Interval / CI**: The 95% confidence interval reported around a
  Judge_Triad_Score mean on a slice, computed from the between-conversation standard
  deviation and the slice size.
- **Noise_Floor**: The measurement noise of the Judge_Triad_Score, characterized by the
  between-conversation standard deviation (~0.24 from the existing 900 Opus verdicts)
  and the resulting CI half-width on a given slice size.
- **Quality_Tab**: The existing dashboard tab in the TypeScript/Vite SPA under
  `bakeoff/ui/`, served by the FastAPI app in `bakeoff/app.py`.
- **SSE_Broker**: The existing Server-Sent-Events streaming mechanism the dashboard
  uses to receive live updates.
- **Per_Model_View**: A dedicated, complete copy of the live optimizer visualization
  for a single Target_Model, shown in the Quality_Tab. Each Per_Model_View renders that
  Target_Model's Champion/Challenger Judge_Triad_Scores with Confidence_Intervals over
  Iterations, the Author reasoning stream, the current Champion prompt, and the prompt
  version diff and lookback described in Requirement 9. Per_Model_Views MAY be laid out
  as per-Target_Model sub-tabs within the Quality_Tab or side-by-side on the same page.
- **Model_Channel**: The per-Target_Model stream identifier on which the Optimizer emits
  that Target_Model's iteration events over the SSE_Broker, so that one Target_Model's
  events are never interleaved ambiguously with another Target_Model's events.
- **Offline_Backend**: The deterministic, zero-network backend (offline adapter,
  offline embedder, `StubJudge`) used for tests and pipeline validation.
- **Live_Backend**: The real Bedrock-backed backend (real model adapters, Embed v4,
  real Opus judge) used for real runs.
- **Retrieval_Backend**: The pluggable retrieval substrate the Quality_Study invokes on
  every turn to obtain ranked fragments for a query. Exposes a single read-only
  interface; concrete implementations are the OpenSearch_Backend (preferred) and the
  Local_Retrieval_Backend (fallback), both returning the same fragment shape
  (`{id, text, metadata, ...}`). Held constant across Champion and Challenger for the
  same turn; not a variable the Optimizer tunes.
- **OpenSearch_Backend**: The preferred Retrieval_Backend implementation — the deployed
  ALPHA OpenSearch service in AWS account `948580600005`, carrying data and metadata
  essentially identical to the local corpus. Its specific endpoint, index, and
  authentication are owner-provided assumptions to confirm at implementation time (see
  the sourcing caveat).
- **Local_Retrieval_Backend**: The fallback Retrieval_Backend implementation — the
  repo's existing local retrieval service (`POST /retrieve`, documented in
  `bakeoff/README.md`). A guaranteed-workable substitute used when the OpenSearch_Backend
  is onerous or unworkable to connect to or use.
- **Grounding**: The requirement that every fact asserted in a Target_Model's answer
  trace to a fragment returned by the Retrieval_Backend for that turn. The retrieved
  fragments are the only permitted source of grounding; outside or training knowledge is
  not permitted grounding. The Judge grades faithfulness against the same fragments the
  Target_Model received.
- **Abstention**: The Target_Model declining to answer (returning a refusal / non-answer)
  when it is unsure or when the retrieved fragments do not support a confident, grounded
  answer. Correct Abstention on unanswerable or insufficiently-grounded turns is a
  desired, scored behavior — a win, not a failure — and is rewarded by the optimization
  objective and the Judge rubric; answering-when-unsure (hallucination, over-claiming,
  unsupported answers) is penalized.
- **Prompting_Guidance**: The curated, version-controlled reference describing modern
  Claude 4.5 system-prompting practice, derived from `modern_system_prompting.pdf` and
  stored in the repo as a markdown document or module constant (not loaded from the raw
  PDF at runtime). Covers XML/tagged layered prompt structure, refusal/abstention
  handling, tone and formatting control, knowledge-grounding, steerability, and the note
  that Claude 4.x models are highly responsive to the system prompt so over-aggressive
  all-caps "MUST" language can over-trigger and should be avoided. Applicable because the
  Target_Models are members of the same Claude 4.x family. Sourced externally (vendor),
  not from an Amazon-internal primary source.

## Requirements

### Requirement 1: Closed-loop iteration mechanics (champion/challenger)

**User Story:** As the study owner, I want each optimization iteration to score the
current champion prompt, learn from its worst judged turns, and propose and test a
rewritten challenger, so that the prompt genuinely improves over iterations instead of
being picked once from a fixed menu.

#### Acceptance Criteria

1. THE Optimizer SHALL run as an iterative champion/challenger loop in which each
   Iteration produces exactly one Challenger prompt for one Target_Model.
2. WHEN an Iteration begins, THE Optimizer SHALL score the current Champion prompt on
   the Tuning_Slice using the Judge and record the resulting Judge_Triad_Score with its
   Confidence_Interval.
3. WHEN the current Champion has been scored in an Iteration, THE Optimizer SHALL select
   the lowest-scoring judged turns on the Tuning_Slice together with the Judge's
   evidence and rationale for each selected turn.
4. WHEN the lowest-scoring judged turns have been selected, THE Optimizer SHALL provide
   the current Champion prompt text and the selected failures with their Judge evidence
   to the Author and obtain a proposed Challenger prompt.
5. WHEN the Author has produced a Challenger prompt, THE Optimizer SHALL score the
   Challenger on the same Tuning_Slice using the Judge and record its Judge_Triad_Score
   with its Confidence_Interval.
6. WHEN both the Champion and the Challenger have been scored in an Iteration, THE
   Optimizer SHALL promote the Challenger to Champion only if the Challenger's
   Judge_Triad_Score exceeds the current Champion's Judge_Triad_Score by at least the
   Significance_Threshold; otherwise THE Optimizer SHALL retain the current Champion.
7. WHERE no prior Champion exists for a Target_Model at the start of Phase_A, THE
   Optimizer SHALL use a seed prompt as the iteration-0 baseline Champion.
8. WHERE the existing fixed five-variant menu is available, THE Optimizer SHALL be
   permitted to use one of those variants as the iteration-0 seed Champion, and SHALL
   NOT use the fixed menu as the iteration mechanism for any subsequent Iteration.
9. THE Optimizer SHALL run the champion/challenger loop independently per Target_Model
   so that each Target_Model converges on its own Champion prompt.
10. WHERE the per-Target_Model loops are run concurrently, THE Optimizer SHALL keep the
    Iterations within a single Target_Model's loop sequential, so that a Champion is
    scored before its selected failures drive that Target_Model's next Challenger, and
    SHALL NOT run Iterations, Challengers, or slice items within a single Target_Model's
    loop in parallel beyond the existing run/judge concurrency.
11. WHERE the two Target_Models' loops are run concurrently, THE Optimizer SHALL gate
    that concurrency on each concurrently-running Target_Model having its own active
    Per_Model_View as required by Requirement 9, and IF that condition does not hold,
    THEN THE Optimizer SHALL run the two Target_Models' loops sequentially instead.

### Requirement 2: The big judge is the optimization signal, not closeness

**User Story:** As the study owner, I want the loop to rank and decide using the real
Opus judge's faithfulness/correctness/completeness triad, so that decisions reflect
true answer quality rather than a cosine proxy that under-penalizes over-refusal and
topic-bleed.

#### Acceptance Criteria

1. THE Optimizer SHALL use the Judge_Triad_Score, derived from the Judge's
   faithfulness, correctness, and completeness dimensions, as the sole decision metric
   for promoting a Challenger to Champion.
2. THE Optimizer SHALL use the same Judge implementation (`JudgeScorer` backed by the
   Opus judge model identified in configuration) that the rest of the Quality_Study
   uses, rather than a separate or re-implemented judge.
3. THE Optimizer SHALL record the Closeness semantic-cosine value as a secondary
   cross-check alongside each Judge_Triad_Score.
4. THE Optimizer SHALL NOT use Closeness as a decision metric for promoting,
   rejecting, or ranking prompts.
5. WHEN the Judge scores a turn in which the gold fragment was retrieved but the model
   declined to answer (over-refusal), THE Optimizer SHALL treat that turn's
   Judge_Triad_Score as the authoritative signal for that turn even when its Closeness
   value is higher.
6. THE Optimizer SHALL record, per scored prompt, the per-dimension Judge scores
   (faithfulness, correctness, completeness) in addition to the aggregate
   Judge_Triad_Score so the decision is auditable.

### Requirement 3: Failure-driven prompt authoring

**User Story:** As the study owner, I want the author model to rewrite the prompt based
on the specific judged failures of the current champion, so that each new prompt
targets real observed weaknesses instead of re-picking pre-written text.

#### Acceptance Criteria

1. WHEN the Optimizer requests a Challenger, THE Author SHALL receive the current
   Champion prompt text and the selected lowest-scoring judged turns including each
   turn's Judge evidence and rationale.
2. THE Author SHALL produce a Challenger prompt as newly authored instruction text
   rather than a selection from the fixed five-variant menu.
3. THE Author SHALL produce a written rationale explaining which failures drove the
   proposed change and how the change is intended to address them.
4. THE number of lowest-scoring judged turns provided to the Author SHALL be
   configurable.
5. IF the Author returns an empty prompt or a prompt identical to the current Champion,
   THEN THE Optimizer SHALL record the Iteration as producing no usable Challenger and
   SHALL count the Iteration as a non-improving Iteration for the stop rule.
6. THE Optimizer SHALL preserve the Quality_Study's held-constant elements when applying
   a Challenger prompt, so that only the system-instruction text varies while the
   per-turn retrieval (Requirement 13), the retrieved-fragment assembly, and the
   conversational turn threading remain held constant between Champion and Challenger for
   the same turn.

### Requirement 4: Author and judge model separation

**User Story:** As the study owner, I want the author model to be different from the
judge model, so that the judge never grades a prompt authored by itself and the loop
does not contend with itself for Opus quota.

#### Acceptance Criteria

1. THE Optimizer SHALL use an Author model that is a different model from the Judge
   model.
2. IF the Author model and the Judge model are configured to be the same model, THEN
   THE Optimizer SHALL refuse to start and SHALL report the conflict.
3. THE Optimizer SHALL record, per Iteration, which model acted as Author and which
   model acted as Judge.
4. THE Optimizer SHALL default the Author model to Sonnet 4.6 and reserve the Opus
   model exclusively for the Judge role.
5. THE Optimizer SHALL allow the Author model to be configured independently of the
   Target_Model whose prompt is being optimized.

### Requirement 5: Significance threshold grounded in the noise floor

**User Story:** As the study owner, I want "significant improvement" defined against
measurement noise rather than guessed, so that the loop does not chase gains smaller
than the judge's own variability.

#### Acceptance Criteria

1. THE Optimizer SHALL treat a Champion-over-previous-Champion gain as significant only
   when the absolute Judge_Triad_Score delta is at least the Significance_Threshold.
2. THE Significance_Threshold SHALL default to 0.05 absolute on the 0.0-1.0
   Judge_Triad_Score scale.
3. THE Optimizer SHALL compute a 95% Confidence_Interval for each Judge_Triad_Score mean
   on a slice using the between-conversation standard deviation and the slice size.
4. THE Optimizer SHALL report each iteration gain both as an absolute Judge_Triad_Score
   delta and as a percentage relative to the previous Champion's score.
5. THE Optimizer SHALL key the stop rule on the absolute Judge_Triad_Score delta tied to
   the Confidence_Interval, not on the percentage figure.
6. THE Significance_Threshold SHALL be configurable.
7. THE Optimizer SHALL record, alongside each reported gain, the slice size and the
   Confidence_Interval half-width used so the reader can see the noise floor the gain is
   measured against.
8. THE Optimizer SHALL allow the eval slice size and the reps per evaluation to be
   configured so that the Confidence_Interval half-width can be tightened to resolve
   smaller gains.

### Requirement 6: Convergence and stop rule

**User Story:** As the study owner, I want Phase A to stop after a configurable run of
consecutive non-improving iterations, so that the loop terminates when further authoring
stops producing significant gains.

#### Acceptance Criteria

1. WHILE Phase_A is running, THE Optimizer SHALL maintain a count of consecutive
   Iterations whose Challenger failed to beat the Champion by at least the
   Significance_Threshold.
2. WHEN a Challenger is promoted to Champion, THE Optimizer SHALL reset the consecutive
   non-improving Iteration count to zero.
3. WHEN the consecutive non-improving Iteration count reaches the configured stop limit,
   THE Optimizer SHALL stop Phase_A for that Target_Model and mark the current Champion
   as the converged Champion.
4. THE consecutive non-improving Iteration stop limit SHALL default to 5.
5. THE consecutive non-improving Iteration stop limit SHALL be configurable.
6. THE Optimizer SHALL record, for the converged Champion, the Iteration at which
   convergence was reached and the reason Phase_A stopped.

### Requirement 7: Two-phase train/test split integrity

**User Story:** As the study owner, I want the prompt tuned only on the held-out tuning
slice and the converged champion validated on the untouched complement, so that reported
performance is not measured on the same data the prompt was tuned on.

#### Acceptance Criteria

1. THE Optimizer SHALL iterate Phase_A exclusively on the Tuning_Slice, which maps to
   the held-out ~20% portion produced by the existing deterministic `split_items`.
2. THE Author SHALL only ever receive failures drawn from the Tuning_Slice.
3. WHEN Phase_A has converged for a Target_Model, THE Optimizer SHALL run Phase_B by
   evaluating the converged Champion on the Validation_Set, which is the reserved ~80%
   complement of the multi-turn dataset.
4. THE Optimizer SHALL run Phase_B at a higher rep count than Phase_A and SHALL report
   the Phase_B Judge_Triad_Score with its Confidence_Interval.
5. THE Optimizer SHALL NOT report final performance for a Target_Model on the
   Tuning_Slice.
6. THE train/test split SHALL be deterministic and seeded so that the Tuning_Slice and
   Validation_Set are reproducible across runs.
7. THE Optimizer SHALL NOT expose any Validation_Set conversation to the Author at any
   point in Phase_A or Phase_B.

### Requirement 8: Per-iteration audit trail with versioned lookback

**User Story:** As the study owner, I want every iteration persisted with full detail,
so that I can look back several prompt versions and see exactly why each change was made
and whether it was accepted.

#### Acceptance Criteria

1. WHEN an Iteration completes, THE Optimizer SHALL persist an Audit_Record containing
   the full Challenger prompt text, a diff of the Challenger against the previous prompt
   version, the Author's rationale for the change, the Challenger's Judge_Triad_Score
   with its Confidence_Interval, the failures that drove the change, and whether the
   Challenger was accepted or rejected.
2. THE Optimizer SHALL persist Audit_Records to an append-only Audit_Store.
3. THE Optimizer SHALL associate each Audit_Record with its Target_Model and its
   Iteration index.
4. THE Audit_Store SHALL retain all prior prompt versions so that any earlier version
   can be retrieved.
5. WHEN a reader requests the history for a Target_Model, THE Optimizer SHALL provide the
   ordered sequence of prompt versions with their diffs, scores, and accept/reject
   decisions, supporting lookback of at least several versions.
6. THE Optimizer SHALL persist, for the seed iteration-0 Champion, an Audit_Record
   capturing the seed prompt text and its baseline Judge_Triad_Score.

### Requirement 9: Live streaming view in the existing quality tab

**User Story:** As the study owner, I want to watch the loop run live in the existing
quality tab, so that I can see champion vs challenger scores over iterations, how the
author model is reasoning, the current prompt, and the diff against prior versions as it
happens.

#### Acceptance Criteria

1. WHILE the Optimizer is running, THE Optimizer SHALL stream iteration updates over the
   existing SSE_Broker mechanism into the existing Quality_Tab.
2. THE Quality_Tab SHALL display the running Champion and Challenger Judge_Triad_Scores
   with their Confidence_Intervals across Iterations.
3. WHILE the Author is producing a Challenger, THE Quality_Tab SHALL display the Author's
   reasoning/rationale as it streams.
4. THE Quality_Tab SHALL display the current Champion prompt text.
5. THE Quality_Tab SHALL display the diff of the current prompt against the prior prompt
   version and SHALL support a lookback of at least two prior versions.
6. WHEN a Challenger is accepted or rejected, THE Quality_Tab SHALL update to reflect the
   new Champion state and the accept/reject decision.
7. THE Optimizer SHALL stream over the existing SSE mechanism without modifying the
   bake-off's streaming behavior.
8. THE Quality_Tab SHALL render one dedicated Per_Model_View for each Target_Model under
   optimization, each Per_Model_View presenting that Target_Model's Champion and
   Challenger Judge_Triad_Scores with Confidence_Intervals across Iterations, the
   Author reasoning stream, the current Champion prompt, and the prompt version diff and
   lookback described in acceptance criteria 2 through 5.
9. THE Quality_Tab SHALL lay out the Per_Model_Views either as per-Target_Model sub-tabs
   within the Quality_Tab or side-by-side on the same page, as an implementer choice.
10. THE Quality_Tab SHALL attribute each Per_Model_View to its Target_Model so that a
    reader can identify which Target_Model each displayed score, prompt, diff, and
    Author reasoning stream belongs to.
11. THE Quality_Tab SHALL keep each Target_Model's streamed events isolated to that
    Target_Model's Per_Model_View so that the two Target_Models' streams do not
    interleave ambiguously.

### Requirement 10: Resumability, durability, and test injectability

**User Story:** As the study owner, I want the loop to follow the harness's durability
conventions and run fully offline in tests, so that interrupted runs resume cleanly and
the pipeline can be validated with zero network.

#### Acceptance Criteria

1. THE Optimizer SHALL persist iteration state to append-only JSONL stores that are
   separate files from the bake-off stores.
2. THE Optimizer SHALL assign each unit of work a deterministic identifier so that
   completed work can be identified on resume.
3. WHEN the Optimizer is re-invoked after an interruption, THE Optimizer SHALL skip work
   that is already durable and complete and SHALL resume from the first incomplete unit
   of work.
4. THE Optimizer SHALL accept an injectable Offline_Backend (offline adapter, offline
   embedder, and `StubJudge`) that performs zero network calls, for tests and pipeline
   validation.
5. THE Optimizer SHALL accept an injectable Live_Backend (real model adapters, Embed v4,
   and real Opus judge) for real runs.
6. THE Optimizer SHALL record which backend produced each recorded decision so a reader
   can tell whether a result came from the Offline_Backend or the Live_Backend.
7. WHEN the Live_Backend is selected while a bake-off run looks active, THE Optimizer
   SHALL refuse to start the live run unless the operator explicitly overrides, so that
   the Quality_Study does not contend with the bake-off for the shared Opus judge quota.
8. THE Optimizer SHALL write each durable record as a single flushed JSONL line so that
   an interruption never leaves a half-written record that breaks resume.

### Requirement 11: Fresh-start replacement of the one-shot artifacts

**User Story:** As the study owner, I want the old one-shot selection artifacts emptied
and the modules rewritten, so that the closed-loop optimizer starts clean and the old
menu-selection mechanism is no longer in effect.

#### Acceptance Criteria

1. THE Optimizer SHALL write its iteration, audit, and result data to new store files
   distinct from the old one-shot quality output files.
2. THE Optimizer SHALL NOT depend on the contents of the old one-shot artifacts
   (`quality_outcomes.jsonl`, `quality_prompts.json`, `quality_optimizer_report.json`,
   and the old judge/errors stores) for its decisions.
3. WHERE the old one-shot artifacts are emptied or replaced, THE Optimizer SHALL operate
   correctly starting from empty stores.
4. THE Optimizer SHALL retain the fixed five-variant menu only as a permitted source of
   an iteration-0 seed Champion and SHALL NOT use it as the selection mechanism.

### Requirement 12: Scope boundaries and local-tool posture

**User Story:** As the study owner, I want the optimizer confined to the quality study
on a local loopback-only tool, so that the retrieval substrate, the bake-off, and the
no-auth posture are all preserved.

#### Acceptance Criteria

1. THE Optimizer SHALL NOT modify the held-constant retrieval substrate, including when
   the Quality_Study invokes it on every turn per Requirement 13.
2. THE Optimizer SHALL NOT modify the speed/quality bake-off study.
3. THE Optimizer SHALL operate only on the two fixed Target_Models
   (`sonnet-4.6-thinking-off` and `haiku-4.5`).
4. THE Optimizer SHALL keep the retrieval substrate as a held-constant element that the
   study invokes but does not vary or tune, so that for a given turn the same query
   yields the same fragments for both the Champion and the Challenger and the only varied
   element remains the system-instruction text.
5. WHILE serving the live view, THE Optimizer SHALL bind only to the loopback interface
   consistent with the existing dashboard's no-auth, local-only posture.
6. THE Optimizer SHALL NOT introduce authentication-free exposure on any non-loopback
   interface.

### Requirement 13: Retrieval-always with fragments-only grounding

**User Story:** As the study owner, I want the quality path to retrieve real fragments on
every turn and require the model to answer only from those fragments, so that the study
measures grounded answering against a held-constant retrieval substrate rather than a
fragment-free path that lets the model lean on training knowledge.

#### Acceptance Criteria

1. WHEN the Quality_Study generates an answer for any turn of any conversation, THE
   Optimizer SHALL invoke the Retrieval_Backend for that turn and obtain its retrieved
   fragments before the Target_Model answers.
2. THE Optimizer SHALL invoke the Retrieval_Backend on every turn of every conversation,
   so that retrieval always happens and is neither skipped nor optional.
3. WHEN the same query is issued for the same turn under the Champion and under the
   Challenger, THE Retrieval_Backend SHALL return the same fragments, so that retrieval
   is held constant across Champion and Challenger and the only varied element is the
   system-instruction text.
4. WHEN retrieved fragments are supplied to the Target_Model, THE Optimizer SHALL place
   the fragments inline in the visible prompt text and SHALL NOT supply them through the
   `promptSessionAttributes` channel or the `sessionAttributes` channel, so that fragment
   visibility does not depend on a template placeholder.
5. THE Optimizer SHALL treat the fragments returned by the Retrieval_Backend for a turn
   as the only permitted grounding for the Target_Model's answer for that turn.
6. THE Optimizer SHALL require that every fact asserted in the Target_Model's answer
   trace to a retrieved fragment, and SHALL steer the Target_Model not to answer from
   outside or training knowledge.
7. WHEN the Judge scores a turn's faithfulness and grounding, THE Optimizer SHALL provide
   the Judge the same fragments the Target_Model received for that turn, so that grounding
   is evaluated against the same evidence the Target_Model was given.
8. WHERE the retrieved fragments are irrelevant or insufficient for a turn, THE Optimizer
   SHALL permit the Target_Model to decline to use them, so that retrieval-always does not
   compel answer-always.
9. THE Optimizer SHALL keep the inline-agent invocation fidelity invariant intact under
   retrieval-always, so that only the system-instruction text, the turn question, and the
   inline fragments reach the Target_Model, with no orchestration noise and with neither
   session-attribute channel used.

### Requirement 14: Abstention as a first-class, heavily weighted scored behavior

**User Story:** As the study owner, I want correct abstention rewarded and
answering-when-unsure penalized as a primary scored behavior, so that the optimized
prompts make the target models decline to answer rather than guess or over-claim when the
fragments do not support a confident, grounded answer.

#### Acceptance Criteria

1. IF the Target_Model is unsure, or the retrieved fragments do not support a confident,
   grounded answer, THEN THE Optimizer SHALL require the Target_Model to abstain rather
   than guess or over-claim.
2. THE Optimizer SHALL include correct Abstention as a primary scored behavior in its
   optimization objective, weighted strongly rather than as a minor dimension.
3. THE Optimizer SHALL strongly reward correct Abstention on turns whose retrieved
   fragments are insufficient or whose ground truth is unanswerable.
4. THE Optimizer SHALL strongly penalize answering-when-unsure, including hallucination,
   over-claiming, and answers unsupported by the retrieved fragments.
5. THE Judge rubric SHALL score correct Abstention as a success and SHALL score
   answering-when-unsure as a failure, consistent with the Judge_Triad_Score remaining
   the sole promotion-decision metric (Requirement 2).
6. WHEN the Optimizer requests a Challenger, THE Author SHALL be steered to produce prompt
   text that makes the Abstention behavior explicit and reliable in the Target_Model.
7. THE Optimizer SHALL treat correct refusal on unanswerable or insufficiently-grounded
   turns as strengthening, not contradicting, the Quality_Study's existing answerability
   discipline.

### Requirement 15: Modern prompting guidance in author and judge context

**User Story:** As the study owner, I want the author model given modern Claude 4.5
system-prompting guidance and the judge given its grounding/abstention portions, so that
the author writes well-structured, model-appropriate, abstention-aware prompts and the
judge evaluates consistently with what a good grounded, abstaining prompt should produce.

#### Acceptance Criteria

1. WHEN the Optimizer requests a Challenger, THE Optimizer SHALL include the
   Prompting_Guidance in the Author's context.
2. THE Prompting_Guidance SHALL cover XML/tagged layered prompt structure,
   refusal/abstention handling, tone and formatting control, knowledge-grounding,
   steerability, and the caution that Claude 4.x models are highly responsive to the
   system prompt so over-aggressive all-caps "MUST" language can over-trigger and should
   be avoided.
3. THE Optimizer SHALL store the Prompting_Guidance as a curated, version-controlled
   reference in the repository, derived from `modern_system_prompting.pdf` as a markdown
   document or a module constant, and SHALL NOT load it from the raw PDF at runtime.
4. WHERE the Judge evaluates grounding and Abstention, THE Optimizer SHALL be permitted to
   supply the Judge the relevant grounding and abstention portions of the Prompting_Guidance
   so that the Judge's evaluation is consistent with the guidance the Author follows.
5. THE Optimizer SHALL keep the Author and Judge model identities unchanged by this
   requirement, defaulting the Author to Sonnet 4.6 and the Judge to the Opus model, unless
   the owner later overrides those identities.
6. THE Optimizer SHALL record the Prompting_Guidance as an external/vendor-sourced
   reference rather than an Amazon-internal primary source, consistent with the sourcing
   and methodology honesty caveat.

### Requirement 16: OpenSearch-preferred, local-fallback pluggable retrieval

**User Story:** As the study owner, I want retrieval to prefer the deployed ALPHA
OpenSearch backend but fall back to the local retrieval service when OpenSearch is
onerous or unworkable, behind one pluggable interface, so that the study always has a
guaranteed-workable retrieval substrate without changing what downstream code expects.

#### Acceptance Criteria

1. THE Retrieval_Backend SHALL prefer the OpenSearch_Backend, the deployed ALPHA
   OpenSearch implementation in AWS account `948580600005`, which carries data and
   metadata essentially identical to the local corpus.
2. IF connecting to or using the OpenSearch_Backend is onerous or unworkable, THEN THE
   Retrieval_Backend SHALL fall back to the Local_Retrieval_Backend, the repo's existing
   `POST /retrieve` service, as a guaranteed-workable substitute.
3. THE Retrieval_Backend SHALL expose both implementations behind a single interface so
   that the implementation can be swapped without changing the calling code.
4. THE OpenSearch_Backend and the Local_Retrieval_Backend SHALL each return the same
   fragment shape (`{id, text, metadata, ...}`) so that downstream grounding and judging
   are unaffected by which implementation served a query.
5. THE Retrieval_Backend SHALL issue only read-only retrieval queries against either
   implementation.
6. THE Optimizer SHALL treat the OpenSearch_Backend's specific endpoint, index, and
   authentication for the ALPHA account as an owner-provided assumption to confirm at
   implementation time, consistent with the sourcing and methodology honesty caveat, and
   SHALL NOT make the study depend on OpenSearch being the only available implementation.

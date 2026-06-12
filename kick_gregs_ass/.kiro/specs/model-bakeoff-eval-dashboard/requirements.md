# Requirements Document

## Introduction

This document derives EARS-format requirements from the approved design for the
**model-bakeoff-eval-dashboard** feature. The design is the source of truth; the
requirements below capture its intent so each design decision and each
correctness property is traceable to a numbered requirement.

The feature exists to **choose which LLM should be the "brain" of a Slack bot** —
a model-selection decision made as carefully as the data allows. It is *not* an
attempt to prove a 95% accuracy figure. The infrastructure and statistical
machinery are deliberately built to be reusable for that harder proof later, but
that proof is **out of scope now**. Every requirement serves a defensible choice
on the balance of two outcome dimensions only — **speed** and **quality** — and
nothing more.

The system is three tightly-coupled parts unified by one shared per-trial data
model (the `TrialEvent`): a parallel evaluation runner, a live-updating
monitoring UI, and an executive-facing visualization layer. Retrieval is **not**
under test; the existing FAQ Retrieval Backend is consumed as a constant shared
substrate over HTTP.

### Sourcing note

Consistent with the design's sourcing note, the evaluation-metric,
LLM-as-judge-calibration, and confidence-interval methodology in these
requirements is grounded in **current general industry practice, not
Amazon-internal guidance**. Amazon-internal search tools were unavailable in this
environment, so no Amazon-internal source was consulted and none is cited. Before
any number from this harness is used to defend a decision to an executive
audience, these methodology choices should be re-validated against internal
guidance.

### Assumptions (resolved defaults for the design's open questions)

Under the active autonomous mode, the design's open questions are resolved with
reasonable defaults rather than blocking. These are assumptions, configuration-
driven where noted, and may be revised:

- **A1 — Candidate model list:** TBD and **configuration-driven** (`models.yaml`,
  per AD-8). Adding or removing a candidate is a config edit; no requirement
  names specific models.
- **A2 — Target CI half-width:** default `w = ±0.03` on the 0–1 composite quality
  scale, stored in the sampling plan and used to size repetitions.
- **A3 — Composite weights:** **accuracy-dominant** by default (accuracy
  sub-scores carry the dominant weight; interaction sub-scores the remainder),
  stored as a named weight set (`composite_weights_id`) so the blend can be
  re-weighted without re-running models.
- **A4 — Judge model and human calibration set:** the judge is a single fixed
  Bedrock model, **distinct from every candidate** (config-flagged `role: judge`);
  the human calibration set defaults to **~50 items** spanning strata and all
  three answerability classes.
- **A5 — Multi-turn gold beyond turn 1:** only turn-1 gold exists on disk
  (`conversation_turn1_gold.jsonl`); later turns are scored by **semantic
  similarity and judge only** (no gold-link), until later-turn gold arrives.

## Glossary

System components:

- **Evaluation_Harness** — the whole system: runner, scoring, aggregation, and
  the two presentation layers, run locally on a dev box.
- **Dataset_Loader** — reads on-disk synthetic files and the gold corpus into
  normalized `Item` objects with a uniform cohort vector.
- **Evaluation_Runner** — the asyncio orchestrator/scheduler that expands and
  executes trials and emits `TrialEvent`s.
- **Model_Adapter** — the config-driven uniform interface in front of each
  candidate model and the judge, calling AWS Bedrock.
- **Retrieval_Client** — thin client over the existing FAQ Retrieval Backend;
  never re-implements retrieval.
- **Event_Log** — the append-only `data/bakeoff/trial_events.jsonl` file; the
  single source of truth.
- **Semantic_Scorer** — Layer A: Embed v4 cosine similarity scorer.
- **GoldLink_Scorer** — Layer B: gold-node precision/recall plus retrieval-ceiling
  measures.
- **Abstention_Scorer** — scorer for the answerability dimension.
- **Judge_Scorer** — Layer C: anchored-rubric LLM-as-judge scorer.
- **Composite_Scorer** — produces the transparent weighted composite quality
  score.
- **Sampling_Planner** — runs the pilot and computes per-stratum repetitions and
  the confirmed temperature into `sampling_plan.json`.
- **Aggregation_Engine** — computes means, confidence intervals, paired
  differences, and the frontier from the Event_Log.
- **Live_Monitoring_UI** — the FastAPI + SSE live view (view-of-all and
  focus-on-one), loopback-only.
- **Exec_Visualization_Layer** — the highest-stakes executive-facing
  visualization and static HTML export.

Domain terms:

- **Item** — one distinct synthetic record: a single-turn query or a multi-turn
  set. The unit of the *perspective sample*.
- **Trial** — one execution of one model against one item at a chosen temperature:
  a `(model, item, rep)` tuple. Emits exactly one `TrialEvent`.
- **Rep** — a repetition index for a given `(model, item)`; reps estimate
  within-item (run-to-run) variance only.
- **Cohort cell / stratum** — a cell in the cohort design (geography ×
  proficiency × disposition × channel × entry_route × momentary_state ×
  answerability × intent_shape × turn_type), collapsed where cells are sparse.
- **Pass** — `PILOT`, `WIDE`, `DEEP`, or `TARGETED`.
- **Between-item variance** — variance across distinct items; the dominant source
  of statistical power for aggregate and cohort means.
- **Within-item variance** — run-to-run variance for the same model on the same
  item; model jitter only.
- **Answerability** — an item label of `full`, `partial`, or `none`.
- **Warm / cold retrieval** — a trial whose `retrieval_cache_hit` is true (warm)
  or false (cold).
- **TrialEvent** — the single shared per-trial record appended to the Event_Log.

## Requirements

### Requirement 1: Defensible speed-vs-quality model-selection outcome

**User Story:** As a decision-maker choosing the Slack bot's LLM brain, I want a
defensible speed-versus-quality comparison across candidate models, so that I can
select a model on evidence rather than intuition.

#### Acceptance Criteria

1. THE Evaluation_Harness SHALL produce, for each candidate model, one speed
   measure and one quality measure, each accompanied by a confidence interval.
2. THE Evaluation_Harness SHALL restrict the comparison outcome to the speed
   dimension and the quality dimension.
3. THE Evaluation_Harness SHALL present results as a model-selection comparison
   with per-model strengths and weaknesses.
4. WHERE a later "prove 95% accuracy" analysis is required, THE Evaluation_Harness
   SHALL support that analysis by changing the sampling-plan configuration without
   modifying harness source code.

### Requirement 2: Parallel evaluation runner

**User Story:** As an operator, I want trials executed maximally in parallel
within per-resource limits, so that the bake-off completes quickly without
overwhelming any backend.

#### Acceptance Criteria

1. THE Evaluation_Runner SHALL execute trials concurrently on a single asyncio
   event loop.
2. THE Evaluation_Runner SHALL enforce a separate bounded concurrency limit for
   each downstream resource: each candidate model, the judge, the embedding
   model, and the retrieval backend.
3. WHEN CPU-bound scoring is performed, THE Evaluation_Runner SHALL offload that
   work to a worker thread so the event loop remains unblocked.
4. WHEN a trial is executed, THE Evaluation_Runner SHALL emit exactly one
   TrialEvent for that trial.

### Requirement 3: Config-driven Bedrock model adapters

**User Story:** As an operator, I want candidate models defined in configuration,
so that adding or removing a competitor is a config edit rather than a code
change.

#### Acceptance Criteria

1. THE Model_Adapter SHALL obtain each candidate model definition from the
   `models.yaml` configuration file.
2. WHEN a model entry is added to or removed from `models.yaml`, THE
   Evaluation_Harness SHALL include or exclude that model in the next run without
   source-code modification.
3. WHERE a model entry is flagged with the judge role, THE Evaluation_Harness
   SHALL exclude that model from the candidate set.
4. THE Model_Adapter SHALL invoke each candidate model through AWS Bedrock in the
   us-west-2 region using the configured API style.
5. WHILE a candidate model is configured for streaming, THE Model_Adapter SHALL
   capture time-to-first-token at the first content delta.

### Requirement 4: Retrieval as a constant shared substrate

**User Story:** As an evaluator, I want retrieval held identical across models and
repetitions, so that outcome differences are attributable to the model and not to
retrieval.

#### Acceptance Criteria

1. THE Retrieval_Client SHALL obtain ranked fragments only through the existing
   FAQ Retrieval Backend `POST /retrieve` HTTP contract.
2. WHEN retrieval is requested for a given `(query, filters, candidate_n, top_k)`
   key, THE Retrieval_Client SHALL return the memoized result so repeated requests
   for that key produce identical fragments.
3. THE Evaluation_Runner SHALL supply identical retrieved fragments to every
   candidate model and every repetition for the same item and turn.
4. WHEN a TrialEvent is recorded, THE Evaluation_Runner SHALL record the returned
   fragments, retrieval stage timings, confidence, and cache-hit flag verbatim.

### Requirement 5: Append-only TrialEvent log as the single source of truth

**User Story:** As an evaluator, I want every trial persisted as one append-only
event, so that runs are replayable and one auditable record stands behind every
reported number.

#### Acceptance Criteria

1. WHEN a trial completes, THE Event_Log SHALL append exactly one TrialEvent as a
   single JSON line to `data/bakeoff/trial_events.jsonl`.
2. FOR ALL valid TrialEvents, THE Event_Log SHALL serialize and then parse a
   TrialEvent into an equivalent TrialEvent (round-trip).
3. WHEN a TrialEvent is appended, THE Event_Log SHALL perform the append under a
   lock so that no partial line is written.
4. IF a trailing malformed line is detected at load time, THEN THE Event_Log SHALL
   truncate that line and load the remaining intact events.
5. THE Aggregation_Engine, THE Live_Monitoring_UI, and THE Exec_Visualization_Layer
   SHALL derive every reported value from the Event_Log.

### Requirement 6: Crash-resumable, idempotent execution

**User Story:** As an operator, I want a crashed run to resume without duplicating
or losing work, so that long bake-offs are robust.

#### Acceptance Criteria

1. THE Evaluation_Runner SHALL compute each `trial_id` as
   `sha1(model_id|item_id|rep|temperature|pass_name)`.
2. WHEN a run resumes, THE Evaluation_Runner SHALL execute only trials whose
   `trial_id` does not already appear in the Event_Log.
3. WHEN every planned trial has a corresponding TrialEvent in the Event_Log, THE
   Evaluation_Runner SHALL execute zero additional trials.
4. IF a trial fails at any stage, THEN THE Evaluation_Runner SHALL record a
   TrialEvent with the matching error status and the error detail.

### Requirement 7: Speed measurement with cold and warm retrieval separated

**User Story:** As an evaluator, I want latency measured per stage and reported
with cold and warm retrieval separated, so that speed comparisons reflect what
the user feels.

#### Acceptance Criteria

1. WHEN a trial completes, THE Evaluation_Runner SHALL record retrieval stage
   timings (`embed_query_ms`, `bm25_vectorize_ms`, `hybrid_search_ms`,
   `rerank_ms`, `total_ms`) separately from generation timings.
2. WHEN a trial completes, THE Evaluation_Runner SHALL record time-to-first-token,
   generation latency, and end-to-end latency.
3. THE Aggregation_Engine SHALL report latency as a distribution comprising the
   median, the p90, and the p95 percentiles.
4. WHEN computing latency aggregates, THE Aggregation_Engine SHALL compute the
   warm-latency figure exclusively from trials whose `retrieval_cache_hit` equals
   true and SHALL report the cold-latency figure under a separate label.
5. THE Aggregation_Engine SHALL report throughput as completed trials per minute.

### Requirement 8: Semantic similarity scoring (Layer A)

**User Story:** As an evaluator, I want each answer compared semantically to the
ideal answer, so that quality has a fast, judge-independent guardrail.

#### Acceptance Criteria

1. WHEN scoring an answer, THE Semantic_Scorer SHALL compute the cosine similarity
   between the answer embedding and the `wants` summary embedding using Embed v4
   (`us.cohere.embed-v4:0`).
2. WHEN gold markdown is resolvable for the item, THE Semantic_Scorer SHALL
   compute the cosine similarity between the answer embedding and the concatenated
   gold-markdown embedding.
3. THE Semantic_Scorer SHALL record the embedding model id used for each semantic
   score.

### Requirement 9: Gold-link / retrieval-style correctness (Layer B)

**User Story:** As an evaluator, I want to know whether each answer relied on the
correct gold nodes, so that answers grounded in the right source score higher
than plausible-sounding ungrounded ones.

#### Acceptance Criteria

1. WHERE the model emits citations, THE GoldLink_Scorer SHALL take the cited node
   ids directly and set `attribution_method` to `explicit_citation`.
2. WHERE the model emits no citations, THE GoldLink_Scorer SHALL attribute each
   answer sentence to the best semantically-matching fragment and set
   `attribution_method` to `semantic_attribution`.
3. WHEN attribution is complete, THE GoldLink_Scorer SHALL compute the precision
   and recall of gold-node usage against the item's `gold_node_ids`.
4. THE GoldLink_Scorer SHALL record the retrieval-ceiling measures (`precision@k`,
   `recall@k`, MRR, `nDCG@k`) of the retrieval ranking against `gold_node_ids`
   once per item as a property of the constant substrate.
5. IF gold was never retrieved for an item, THEN THE Exec_Visualization_Layer
   SHALL surface that the item's accuracy ceiling is capped by retrieval.

### Requirement 10: LLM-as-judge rubric scoring with carried variance (Layer C)

**User Story:** As an evaluator, I want a fixed judge to score the subjective
dimensions with measured uncertainty, so that tone, voice, helpfulness, and
state-appropriateness are assessed defensibly.

#### Acceptance Criteria

1. WHEN judging an answer, THE Judge_Scorer SHALL score the anchored accuracy
   rubric dimensions (faithfulness, correctness, completeness) and the interaction
   rubric dimensions (tone, voice, helpfulness, state_appropriateness).
2. WHEN judging an answer, THE Judge_Scorer SHALL draw `k` judge samples and
   record the per-dimension mean, standard deviation, and `k_samples`.
3. THE Judge_Scorer SHALL record judge variance as a quantity separate from model
   within-item variance.
4. WHEN scoring faithfulness, THE Judge_Scorer SHALL record the quoted fragment
   span supporting the score.
5. WHEN presenting the answer and the reference to the judge, THE Judge_Scorer
   SHALL randomize or balance the presentation order.
6. THE Evaluation_Harness SHALL hold the judge model fixed and distinct from every
   candidate model.
7. THE Judge_Scorer SHALL compute judge-to-human agreement on the human
   calibration set and report dimensions below the agreement threshold as
   low-confidence.

### Requirement 11: Answerability as a first-class, separate axis

**User Story:** As an evaluator, I want answerability scored as its own axis, so
that correct abstention is rewarded and fabrication is penalized rather than
hidden inside accuracy.

#### Acceptance Criteria

1. THE Abstention_Scorer SHALL classify each trial's answerability as `full`,
   `partial`, or `none` from the item label.
2. WHEN an item's answerability is `none`, THE Abstention_Scorer SHALL set
   `abstention_correct` to 1 if the model declined or escalated and to 0
   otherwise.
3. WHEN an item's answerability is `none` and the model produced a substantive
   answer, THE Abstention_Scorer SHALL set `fabricated_on_unanswerable` to true.
4. THE Aggregation_Engine SHALL exclude items whose answerability is `none` from
   every reported accuracy figure and SHALL report them on a separate abstention
   axis.
5. THE Exec_Visualization_Layer SHALL present answerable-accuracy and
   unanswerable-abstention on separate axes.
6. WHEN an item's answerability is `full` and the model declined, THE
   Abstention_Scorer SHALL record the unwarranted refusal.

### Requirement 12: Transparent, accuracy-dominant composite quality score

**User Story:** As a decision-maker, I want a transparent composite that weights
accuracy dominantly and is always shown with its components, so that ranking is
possible without hiding the reasoning.

#### Acceptance Criteria

1. THE Composite_Scorer SHALL compute the composite quality score as a weighted
   blend of the sub-scores using the weight set named by `composite_weights_id` in
   the sampling plan.
2. THE Composite_Scorer SHALL assign the dominant weight to the accuracy
   sub-scores.
3. WHEN the composite quality score is displayed, THE Exec_Visualization_Layer
   SHALL display the component sub-scores alongside the composite.
4. WHEN the composite weight set is changed in configuration, THE
   Aggregation_Engine SHALL recompute the composite without re-invoking the
   candidate models.

### Requirement 13: Variance decomposition with the item as primary sampling unit

**User Story:** As a statistician, I want between-item and within-item variance
separated and the item treated as the primary sampling unit, so that estimates
reflect the population of perspectives rather than repetition counts.

#### Acceptance Criteria

1. THE Aggregation_Engine SHALL estimate between-item variance separately from
   within-item variance for each reported metric.
2. THE Aggregation_Engine SHALL compute each aggregate point estimate as the
   item-mean-of-rep-means so that each distinct item contributes equal weight.
3. WHILE resampling for a confidence interval, THE Aggregation_Engine SHALL
   resample distinct items first and repetitions within each item second.

### Requirement 14: Tiered WIDE / DEEP / TARGETED stratified design

**User Story:** As a statistician, I want a tiered design covering both breadth
and depth, so that aggregate means are tight and within-item variance is
estimable.

#### Acceptance Criteria

1. WHEN executing the WIDE pass, THE Evaluation_Runner SHALL run every item
   present on disk at the per-stratum repetition count from the sampling plan.
2. WHEN executing the DEEP pass, THE Evaluation_Runner SHALL run a stratified
   subsample covering every non-empty cohort cell at the DEEP per-stratum
   repetition count.
3. WHERE the WIDE pass flags an item as high-variance, THE Evaluation_Runner SHALL
   run that item with extra repetitions in the TARGETED pass.
4. THE Sampling_Planner SHALL estimate within-item standard deviation separately
   for single-turn strata and multi-turn strata.

### Requirement 15: Mandatory pilot that computes repetitions from measured variance

**User Story:** As a statistician, I want repetition counts computed from a
measured pilot rather than guessed, so that the experiment is self-sizing.

#### Acceptance Criteria

1. THE Sampling_Planner SHALL run a pilot over a stratified subsample at a
   configured pilot repetition count before the WIDE and DEEP passes.
2. WHEN the pilot completes, THE Sampling_Planner SHALL measure within-item
   standard deviation per metric and per stratum from the pilot events.
3. WHEN computing repetitions, THE Sampling_Planner SHALL select for each stratum
   the smallest repetition count satisfying the target CI half-width and SHALL
   clamp that count to the configured budget ceiling.
4. THE Sampling_Planner SHALL produce, for every stratum, a repetition count of at
   least 1 and at most the budget ceiling.
5. IF the measured within-item standard deviation for a stratum is zero, THEN THE
   Sampling_Planner SHALL set that stratum's repetition count to 1.
6. WHEN the measured within-item standard deviation for a stratum increases with
   all other inputs held constant, THE Sampling_Planner SHALL produce a repetition
   count greater than or equal to the count produced before the increase.
7. WHEN the target CI half-width is tightened with all other inputs held constant,
   THE Sampling_Planner SHALL produce a repetition count greater than or equal to
   the count produced before the tightening.
8. THE Sampling_Planner SHALL write the computed repetitions and the
   pilot-confirmed temperature to `data/bakeoff/sampling_plan.json`.
9. THE Sampling_Planner SHALL use a default sampling temperature of 0.2 as the
   pilot starting point and SHALL record the temperature the pilot confirms or
   overrides.

### Requirement 16: Confidence intervals on every reported mean

**User Story:** As a decision-maker, I want every reported mean to carry a
confidence interval computed by a method that respects the nested design, so that
no number is shown without its uncertainty.

#### Acceptance Criteria

1. THE Aggregation_Engine SHALL accompany every reported mean with a confidence
   interval.
2. FOR ALL confidence intervals produced, THE Aggregation_Engine SHALL ensure
   `lo <= point <= hi`.
3. IF a mean has no associated confidence interval, THEN THE Aggregation_Engine
   SHALL treat that mean as non-renderable.
4. THE Aggregation_Engine SHALL compute exec-facing confidence intervals using a
   two-stage item-level cluster bootstrap.
5. WHEN confidence is increased with the sample held constant, THE
   Aggregation_Engine SHALL produce a confidence interval whose width is greater
   than or equal to the width at the lower confidence.
6. WHEN every observation in a sample is constant, THE Aggregation_Engine SHALL
   produce a zero-width confidence interval at the point estimate.
7. WHEN comparing two models, THE Aggregation_Engine SHALL report the paired
   per-item difference with a confidence interval over the items both models ran.
8. IF a cohort cell has fewer than 30 distinct items, THEN THE Aggregation_Engine
   SHALL flag that cell as `small_n`.
9. THE Aggregation_Engine SHALL compute a normal-approximation confidence interval
   incrementally for the live running estimates.

### Requirement 17: Run with whatever corpus subset exists at run time

**User Story:** As an operator, I want the harness to run against whatever corpus
subset exists at run time, so that evaluation does not block on a fully-built
corpus.

#### Acceptance Criteria

1. WHEN a run starts, THE Dataset_Loader SHALL load every single-turn and
   multi-turn item present on disk at that time.
2. THE Dataset_Loader SHALL record a corpus snapshot stating the single-turn
   count, the multi-turn count, the number of batches completed, and the load
   timestamp.
3. THE Exec_Visualization_Layer SHALL state the corpus snapshot on every view.
4. IF a `gold_node_id` references a missing node, THEN THE Dataset_Loader SHALL
   fail loudly and name the offending id.
5. WHERE only turn-1 gold exists for a multi-turn set, THE Evaluation_Harness
   SHALL score later turns by semantic similarity and judge only, excluding
   gold-link scoring for those turns.

### Requirement 18: Live monitoring UI

**User Story:** As an operator, I want a live view of all models plus a
focus-on-one drill-down, so that I can watch the bake-off as trials land.

#### Acceptance Criteria

1. THE Live_Monitoring_UI SHALL provide a view-of-all showing per-model running
   means with confidence intervals, throughput, completed-and-total counts, and
   status.
2. THE Live_Monitoring_UI SHALL provide a focus-on-one view showing a single
   model's per-cohort running means, latency distribution, and recent example
   answers.
3. WHEN a trial completes, THE Live_Monitoring_UI SHALL push the updated
   aggregates to connected clients over Server-Sent Events.
4. THE Live_Monitoring_UI SHALL bind exclusively to the loopback interface
   `127.0.0.1`.
5. WHILE a run is in progress, THE Live_Monitoring_UI SHALL add visualizations as
   the supporting data accumulates.
6. THE Live_Monitoring_UI SHALL load its charting library from a vendored local
   JavaScript file without a node build step.
7. WHEN a control action of pause, resume, abort, or focus is received, THE
   Live_Monitoring_UI SHALL apply that action through an HTTP POST endpoint.

### Requirement 19: Executive visualization layer

**User Story:** As an executive audience, I want massive, nuanced data shown
clearly, interactively, and above all accurately, so that the model decision is
trustworthy.

#### Acceptance Criteria

1. THE Exec_Visualization_Layer SHALL render a speed/quality Pareto frontier
   placing each model at its warm-p50 end-to-end latency and composite quality,
   with a quality confidence band, a p90 speed whisker, and the Pareto front
   highlighted.
2. IF a value to be displayed is a mean without a confidence interval, THEN THE
   Exec_Visualization_Layer SHALL refuse to render that value.
3. WHERE a cohort cell is flagged `small_n`, THE Exec_Visualization_Layer SHALL
   display that cell de-emphasized with its item count labelled.
4. THE Exec_Visualization_Layer SHALL present answerable-accuracy and
   unanswerable-abstention on separate axes.
5. THE Exec_Visualization_Layer SHALL state, on every view, the corpus snapshot,
   the confidence-interval method, and the judge-to-human agreement and judge
   standard deviation for judge-based dimensions.
6. WHEN an example answer is requested, THE Exec_Visualization_Layer SHALL pull
   the full answer text from the answers store.
7. WHEN an export is requested, THE Exec_Visualization_Layer SHALL produce a
   static, self-contained HTML snapshot with charts inlined.

### Requirement 20: Accuracy of reported numbers

**User Story:** As a decision-maker, I want every reported number to be traceable
and accurate, so that the comparison withstands executive scrutiny.

#### Acceptance Criteria

1. THE Evaluation_Harness SHALL make every reported number traceable to one or
   more TrialEvent lines in the Event_Log.
2. WHEN a fixed random seed is supplied, THE Aggregation_Engine SHALL compute
   every reported figure deterministically from the Event_Log.
3. WHEN aggregation is re-run over an unchanged Event_Log with a fixed seed, THE
   Aggregation_Engine SHALL produce identical figures.

### Requirement 21: Local throwaway operating constraints

**User Story:** As an operator, I want the harness to run on a local dev box
without Brazil or a node toolchain, so that it stays a few-days throwaway.

#### Acceptance Criteria

1. THE Evaluation_Harness SHALL run within a local Python virtual environment
   without a Brazil package.
2. THE Evaluation_Harness SHALL operate without any npm or npx tooling.
3. THE Evaluation_Harness SHALL consume retrieval from the local FAQ Retrieval
   Backend over HTTP and from the local Qdrant container, leaving retrieval
   re-implemented elsewhere.

### Requirement 22: Security posture

**User Story:** As a security-conscious operator, I want the only network surface
bound to loopback and credentials handled as they are today, so that the
unauthenticated UI cannot be reached off-box.

#### Acceptance Criteria

1. THE Live_Monitoring_UI SHALL bind exclusively to the loopback interface
   `127.0.0.1`.
2. IF a non-loopback bind address is configured for the UI, THEN THE
   Evaluation_Harness SHALL refuse to start the UI.
3. THE Evaluation_Harness SHALL authenticate Bedrock calls using the existing
   local credentials in the us-west-2 region.
4. WHEN producing the static HTML export, THE Exec_Visualization_Layer SHALL
   include only aggregates and a bounded set of example answers.

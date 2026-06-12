# Requirements Document

Feature: model-bakeoff-harness

## Sourcing note (read first)

This is a non-trivial feature (architecture, statistical methodology, retrieval
strategy, evaluation metrics). The global rigor steering rule asks that such work
be grounded in current Amazon-internal primary sources (BuilderHub, internal code
search, AWS Prescriptive Guidance). **Those internal tools are not available in
this execution environment.** The evaluation-metric and judge-calibration
requirements below are therefore grounded in current *external* literature (cited
in `design.md`) and flagged as **general industry practice, not Amazon-internal
guidance**. Before any number produced by this harness is used to defend a
decision upward, the evaluation-metric and judge-calibration choices should be
re-validated against internal guidance.

## Introduction

We are choosing which candidate model should be the brain of a Slack FAQ bot. We
are **choosing, not proving** — this is a defensible model-selection decision on
the balance of **speed** and **quality** (accuracy + user-interaction quality),
not a "95% accurate" claim. The harness is nonetheless built as reusable,
scientifically sound infrastructure so the same schema, runner, and aggregation
engine can later support the rigorous accuracy argument by changing the sampling
plan, not the code.

The harness is three coupled parts hanging off **one shared per-trial event
schema** (`TrialEvent`, persisted append-only to JSONL — the single source of
truth): a maximally-parallel evaluation runner, a live-updating monitoring UI,
and an executive-facing visualization layer. Retrieval is **not** under test: the
existing `POST /retrieve` backend is a constant shared substrate; every candidate
receives identical ranked fragments. What we compare is what each model *does*
with that constant context.

The statistical spine separates **between-item variance** (the distinct synthetic
items — the perspective sample, dominant source of power for aggregate/cohort
means) from **within-item run-to-run variance** (repetitions — model
stochasticity only). A tiered design serves both, and repetition counts plus
temperature are chosen **by pilot, not by gut**.

### Scope grounding (actual data at authoring time)

The design targets ~1000 single-turn + ~300 multi-turn items; the dataset is
still being generated. At authoring time `data/synthetic/` holds 400 single-turn
queries (`queries.jsonl`), 120 multi-turn sets (`conversations.jsonl`), multi-turn
turn-1 gold in `conversation_turn1_gold.jsonl` (keyed by `set_id`+`turn`), a
cohort ledger (`perspectives_ledger.jsonl`), and a 56-fragment corpus index
(`corpus_index.tsv`). The harness MUST operate on whatever counts exist at run
time and MUST NOT hard-code dataset sizes.

## Glossary

- **Item** — one distinct synthetic record: a single-turn query
  (`queries.jsonl`) or a multi-turn set (`conversations.jsonl`). The unit of the
  *perspective sample*.
- **Trial** — one execution of one model against one item at a chosen
  temperature: one `(model, item, rep)` tuple. Emits exactly one `TrialEvent`.
- **Rep** — a repetition index for a given `(model, item)`. Reps estimate
  within-item variance only.
- **Stratum** — a cell in the cohort design (geography × proficiency × tone ×
  entry_route × momentary_state × answerability × turn_type). Used for stratified
  subsampling and per-stratum rep configuration.
- **Pass** — WIDE (all items, few reps), DEEP (stratified subsample, more reps),
  TARGETED (flagged high-variance items, extra reps), or PILOT.
- **Cohort dimension** — a single sliceable axis: geography, language
  proficiency, tone/voice, entry route, momentary state, answerability, turn type.
- **TrialEvent** — the shared append-only per-trial event record; the single
  source of truth from which the UI and all aggregates derive.
- **SamplingPlan** — the pilot-produced, code-external description of reps per
  stratum, confirmed temperature, target CI, budget, and composite weights.
- **between-item variance** — variance across distinct items; the dominant source
  of power for aggregate/cohort means.
- **within-item variance** — run-to-run variance of one model on one item;
  model stochasticity only.
- **substrate** — the existing `POST /retrieve` backend, held constant across all
  reps and models.

## Requirements

### Requirement 1: Dataset loading and cohort normalization

**User Story:** As an operator, I want every synthetic record (single-turn and
multi-turn) loaded into a uniform item with a cohort vector and resolved gold
fragments, so that the runner, scorers, and aggregations can treat all items
identically and slice by every cohort dimension.

#### Acceptance Criteria

1. WHEN the loader runs THEN the system SHALL read `queries.jsonl`,
   `conversations.jsonl`, `conversation_turn1_gold.jsonl` (and any other
   `*_gold.jsonl`), `perspectives_ledger.jsonl`, and `corpus_index.tsv` from
   `data/synthetic/`.
2. WHEN loading a single-turn query THEN the system SHALL produce an `Item` with
   its `gold_node_ids`, `answerability`, and a `CohortKey` derived from the
   persona/ledger plus explicit fields (geography, proficiency, tone/disposition,
   entry_route, momentary_state, answerability, turn_type=single).
3. WHEN loading a multi-turn set THEN the system SHALL join each set to its gold
   via `set_id`+`turn` from the multi-turn gold file, derive a `CohortKey` with
   turn_type=multi, and retain the ordered turns and per-turn momentary_state.
4. WHEN resolving `gold_node_ids` THEN the system SHALL map each id to its
   title/snippet via `corpus_index.tsv`.
5. IF any `gold_node_id` does not resolve in `corpus_index.tsv` THEN the system
   SHALL fail loudly with the offending id(s) (gold-link integrity is enforced,
   mirroring PROGRESS.md's "0 invalid gold nodeIds").
6. WHEN asked for cohort cells THEN the system SHALL enumerate the non-empty
   cohort cells available for stratification.
7. WHEN dataset file sizes differ from the design's targets THEN the system SHALL
   operate on the actual counts present and SHALL NOT hard-code dataset sizes.

### Requirement 2: Shared retrieval substrate as a held constant

**User Story:** As a methodologist, I want retrieval called through the existing
HTTP contract and held identical across all reps and models, so that retrieval is
a constant rather than a confound in the model comparison.

#### Acceptance Criteria

1. WHEN a trial needs context THEN the system SHALL obtain it by calling the
   existing `POST /retrieve` contract and SHALL NOT re-implement retrieval.
2. WHEN `/retrieve` responds THEN the system SHALL capture the returned
   `fragments`, per-fragment `confidence`, `timings`, and `cache_hit` verbatim
   into the trial record.
3. WHEN the same item is run across multiple reps and multiple models THEN the
   system SHALL produce identical `fragment_ids` for that item (relying on the
   backend's memoization and/or a local result cache keyed identically).
4. WHEN a run starts THEN the system SHALL gate on `GET /healthz` and fail fast
   with a clear message if the backend is not healthy.
5. WHERE replay without the backend running is desired THE system SHALL support
   an optional local mirror of `/retrieve` results keyed by
   `(query, filters, candidate_n, top_k)`.

### Requirement 3: Uniform model adapters

**User Story:** As an engineer adding a candidate model, I want a single adapter
interface to implement, so that adding a model touches nothing else in the system.

#### Acceptance Criteria

1. WHEN a candidate is registered THEN the system SHALL drive it through a uniform
   `ModelAdapter` interface that accepts an item, the constant retrieved
   fragments, and a temperature, and returns a normalized `ModelResponse`.
2. WHEN an adapter generates THEN it SHALL stream so that **time-to-first-token
   (TTFT)** is measurable, and SHALL capture TTFT, total generation latency, and
   token usage.
3. WHEN building a multi-turn prompt THEN the adapter SHALL incorporate prior
   turns plus the constant retrieved fragments.
4. WHEN an adapter runs THEN it SHALL own only prompt assembly, the endpoint call,
   temperature handling, and latency capture, and SHALL NOT perform scoring.
5. WHEN tests run without live endpoints THEN the system SHALL provide a
   deterministic mock adapter with configurable latency and quality.

### Requirement 4: Layered quality measurement

**User Story:** As a decision-maker, I want quality measured as accuracy plus
user-interaction quality across multiple independent layers, so that no single
brittle number drives the decision and every score traces to evidence.

#### Acceptance Criteria

1. WHEN scoring retrieval-aligned accuracy THEN the system SHALL compute
   precision@k, recall@k, MRR, and nDCG@k of the `/retrieve` ranking against
   `gold_node_ids` (logged as substrate context, not a model differentiator) AND
   answer-grounding precision/recall measuring whether the model's answer used
   the gold fragments that were retrieved (the model differentiator).
2. WHEN scoring semantic similarity THEN the system SHALL embed the model answer
   and the stored ideal response with the same Embed v4 substrate and compute
   cosine similarity, treated as a cross-check, never trusted alone.
3. WHEN scoring with an LLM judge THEN the system SHALL grade answers on anchored
   rubric dimensions (faithfulness/groundedness, correctness, completeness) using
   written score anchors and evidence-anchored scoring (the judge quotes the
   supporting fragment span).
4. WHEN scoring user-interaction quality THEN the system SHALL grade tone,
   empathy, clarity, and actionability on a separate rubric, scored against the
   item's labeled `momentary_state`.
5. WHEN judging THEN the system SHALL take `k` judge samples per answer (k
   pilot-chosen), report the judge's per-dimension mean and SD so judge variance
   is a measured quantity, apply position/order debiasing, and use a fixed judge
   model that is not one of the candidates.
6. WHEN producing a composite quality score THEN the system SHALL compute a
   transparent weighted composite whose weights are stored in the plan (not
   hard-coded) and SHALL always retain the component scores alongside it.
7. WHERE each scorer runs THE system SHALL cache it independently keyed by content
   hash, so one scorer can be re-run (e.g. swap the judge) without re-running
   models or other scorers.

### Requirement 5: Answerability scored as a first-class dimension

**User Story:** As a risk owner, I want answerability scored separately and never
blended into accuracy, so that a model that fabricates on unanswerable questions
is caught regardless of its headline quality.

#### Acceptance Criteria

1. WHEN `answerability == "none"` THEN the system SHALL score
   `abstention_correct ∈ {0,1}` (1 iff the model refuses/escalates without
   fabricating) and SHALL contribute fabrications to a per-model
   fabrication-on-unanswerable rate.
2. WHEN `answerability == "partial"` THEN the system SHALL reward answering the
   answerable part and flagging the gap, and penalize both over-claiming and
   over-refusing.
3. WHEN `answerability == "full"` THEN the system SHALL score standard accuracy
   and SHALL flag `unwarranted_refusal ∈ {0,1}` for refusing an answerable
   question.
4. WHEN aggregating any accuracy metric THEN the system SHALL refuse to average
   across more than one answerability class (it slices first).
5. WHEN presenting results THEN the system SHALL report answerable-accuracy and
   unanswerable-abstention as separate axes.

### Requirement 6: Pilot-driven sampling plan

**User Story:** As a methodologist, I want repetition counts and temperature
chosen from measured variance rather than guessed, so that the experiment is
self-sizing and defensible.

#### Acceptance Criteria

1. WHEN building the sampling plan THEN the system SHALL construct a stratified
   subsample that represents every non-empty cohort cell, collapsing cells too
   sparse to estimate.
2. WHEN running the pilot THEN the system SHALL run each candidate on the
   subsample at `R_pilot` reps at a starting temperature (~0.2) and persist pilot
   events separately.
3. WHEN sizing the run THEN the system SHALL estimate within-item SD
   (`sigma_within`) per stratum and between-item SD (`sigma_between`) from pilot
   events and compute the reps per stratum needed to hit a target CI half-width.
4. WHEN computing required reps THEN the system SHALL floor reps at 2 per stratum
   (so within-item signal always exists), clamp to a trial budget, and assign
   multi-turn strata reps `>=` their single-turn counterparts to equalize CI
   width given fewer multi-turn items.
5. IF `sigma_between^2 / n` alone exceeds the target variance for a stratum THEN
   the system SHALL signal the target as unreachable with available items (rather
   than returning a finite rep count that does not meet it) and surface the wider
   achievable CI honestly.
6. WHEN the plan is finalized THEN the system SHALL serialize it (per-stratum
   reps, confirmed temperature, target CI, confidence level, budget, variance
   model, composite weights) to `sampling_plan.json`, and the runner SHALL consume
   that file — changing the experiment is editing the plan, not the code.

### Requirement 7: Maximally parallel, resumable trial runner

**User Story:** As an operator, I want every planned trial run maximally in
parallel and the run resumable after a crash, so that large runs finish fast and
never duplicate or silently drop work.

#### Acceptance Criteria

1. WHEN expanding the plan THEN the system SHALL compute the planned trial set as
   the product over (pass, model, item, rep) with a deterministic `trial_id` per
   trial.
2. WHEN running THEN the system SHALL execute trials concurrently using asyncio
   with separate bounded semaphores per downstream resource (model endpoint, judge
   endpoint, embedding endpoint) and SHALL offload CPU-bound scoring off the event
   loop.
3. WHEN a trial completes THEN the system SHALL append exactly one `TrialEvent`
   line atomically to `trial_events.jsonl` and publish a completion signal to the
   SSE broker exactly once.
4. WHEN resuming THEN the system SHALL diff the planned trials against `trial_id`s
   already durable in the log and run only the missing ones, such that re-running
   a completed plan runs zero new trials (idempotent resume).
5. WHEN a single trial fails THEN the system SHALL record a `TrialEvent` with
   `error` set and best-effort partial fields, continue the run, and allow a later
   resume to retry it.
6. IF the retrieval error rate crosses a threshold mid-run THEN the system SHALL
   auto-pause (treating it as a systemic problem, not a per-trial blip).

### Requirement 8: TrialEvent schema and append-only persistence

**User Story:** As an auditor, I want every trial captured as one self-describing
append-only event, so that the UI and every reported number derive from one
replayable source of truth.

#### Acceptance Criteria

1. WHEN a trial is recorded THEN the system SHALL write one `TrialEvent` JSON
   object per line containing identity (`trial_id`, `schema_version`,
   `plan_version`), what was run (model, item_id, turn_type, pass, rep,
   temperature, cohort), captured inputs (query, gold_node_ids, answerability,
   retrieval record), outputs (answer_text, token_usage, timings, quality), and
   provenance (started_at, completed_at, error).
2. WHEN computing a `trial_id` THEN the system SHALL make it a deterministic
   function of (model, item_id, rep, pass, plan_version) and unique.
3. WHEN validating an event THEN the system SHALL enforce that
   `abstention_correct` is populated iff `answerability ∈ {none, partial}` and
   `unwarranted_refusal` iff `answerability == full`.
4. WHEN validating a non-error event THEN the system SHALL assert
   `end_to_end_ms == retrieval_total_ms + generation_total_ms` within float
   epsilon.
5. WHEN reading the log THEN the system SHALL round-trip events losslessly and
   SHALL detect and discard a partially-written final line (JSONL parse guard).
6. WHERE on-disk layout is concerned THE system SHALL keep
   `sampling_plan.json`, `trial_events.jsonl`, `pilot_events.jsonl`, scorer caches,
   and materialized `reports/` under `data/bakeoff/`.

### Requirement 9: Aggregation engine with uncertainty on every number

**User Story:** As a decision-maker, I want every reported mean to carry its
confidence interval and every comparison to be statistically sound, so that no
number is presented without its uncertainty.

#### Acceptance Criteria

1. WHEN aggregating THEN the system SHALL be a pure, deterministic function of the
   event log (given a fixed bootstrap seed) and SHALL group by model, by
   (model, cohort cell), and by pass.
2. WHEN computing a CI THEN the system SHALL default to a nonparametric
   **item-level cluster bootstrap** (resample items, then reps within items) whose
   point estimate weights each item equally regardless of its rep count, and SHALL
   also compute a closed-form normal-approx CI for cheap incremental live updates.
3. WHEN comparing two models THEN the system SHALL report a CI on the
   **paired per-item difference** (same items, same retrieval) rather than
   comparing two independent means.
4. WHEN decomposing variance THEN the system SHALL separate between-item,
   within-item, and judge components.
5. WHEN reporting latency THEN the system SHALL report a distribution (median,
   p90, p95), never a lone mean.
6. WHEN building the decision view THEN the system SHALL produce a speed/quality
   frontier marking the Pareto-non-dominated models.
7. WHEN a group spans more than one answerability class for an accuracy metric
   THEN the system SHALL reject the aggregate (enforcing Requirement 5.4).
8. WHEN a cohort cell is too thin for a meaningful CI THEN the system SHALL mark
   it insufficient-data rather than emit a confident value.

### Requirement 10: Live monitoring UI

**User Story:** As an operator, I want a live-updating view of all models at once
and the ability to focus on one, so that I can see what each model is doing and
how it is doing as the run progresses.

#### Acceptance Criteria

1. WHEN the run is active THEN the system SHALL serve an all-models overview with
   a row per model showing status (queued/running/done), progress (trials
   done/planned, per pass), and current running-average quality and speed with
   live CIs.
2. WHEN an operator focuses a model THEN the system SHALL show per-cohort running
   averages, recent trials, a live latency distribution, and current
   high-variance flags for that model.
3. WHEN a `TrialEvent` is appended THEN the system SHALL push it to connected
   clients via Server-Sent Events and the browser SHALL update in place.
4. WHEN running averages are shown live THEN the system SHALL use the cheap
   incremental normal-approx CI (the bootstrap is reserved for the exec/report
   layer).
5. WHEN an operator issues a control action THEN the system SHALL support
   pause/resume/abort and focus-model via ordinary POST endpoints.
6. WHERE the frontend is built THE system SHALL implement it as a TypeScript
   single-page app (built with Vite) under `bakeoff/ui/`, consuming the FastAPI
   JSON/SSE API with type-checked payload interfaces, and using a TypeScript
   charting library (D3 / Observable Plot / visx). The npm/Vite build is scoped to
   the frontend only; the Python backend SHALL remain free of npm/npx in its
   runtime path.

### Requirement 11: Executive visualization layer

**User Story:** As an Amazon exec (or just-below-exec), I want a complex
speed/quality tradeoff made legible, interactive, and above all accurate, so that
I can defend the model choice to my own leadership.

#### Acceptance Criteria

1. WHEN any number is rendered THEN the system SHALL render its CI (band/whisker),
   and WHEN two models' intervals overlap THEN the system SHALL present them as
   not-yet-distinguished rather than ranked.
2. WHEN the exec landing view loads THEN the system SHALL present the
   speed/quality frontier as the hero view: x = latency (median, p90 whisker),
   y = quality (composite, CI band), each model a point with a 2-D uncertainty
   cross, with the Pareto front highlighted and dominated models de-emphasized.
3. WHEN the exec interacts with the frontier THEN the system SHALL support
   toggling the quality metric (composite / accuracy-only / interaction-only) and
   re-weighting the composite live with the ranking responding.
4. WHEN the exec opens the cohort view THEN the system SHALL show a
   model × cohort-dimension heatmap with CI encoded as cell opacity/texture (thin
   data visibly faded), drillable to underlying trials and example answers.
5. WHEN the exec opens the safety panel THEN the system SHALL show
   answerable-accuracy and unanswerable-abstention as separate bars per model with
   CIs and flag any model that fabricates on unanswerable questions.
6. WHEN the exec inspects examples THEN the system SHALL show real best/typical/
   worst answers by score side-by-side with the ideal response, the gold
   fragments, and the judge's quoted evidence.
7. WHEN any exec chart is rendered or exported THEN the system SHALL carry a
   provenance footer (plan_version, n_items, total trials, judge model, judge↔
   human agreement, CI method, date), and SHALL render the squishy interaction
   metrics with a visibly softer-confidence treatment.

### Requirement 12: Reusability and replayability

**User Story:** As a future user building the rigorous accuracy argument, I want
the same schema, runner, and engine to serve a different sampling goal by changing
the plan, so that today's choosing infrastructure is reused for tomorrow's
proving without a rebuild.

#### Acceptance Criteria

1. WHEN the sampling goal changes THEN the system SHALL accommodate it by editing
   or regenerating `sampling_plan.json` (reps, temperature, target CI) without
   code changes.
2. WHEN a scorer changes (e.g. a new judge model or rubric) THEN the system SHALL
   re-score from stored answers using caches and re-aggregate without re-running
   models.
3. WHEN plans differ THEN the system SHALL key trials by `plan_version` and SHALL
   NOT mix plan versions in one aggregate without an explicit override.

### Requirement 13: Error handling and operational robustness

**User Story:** As an operator, I want predictable behavior under failure, so that
a crash, a flaky judge, or a thin cohort never corrupts results or produces a
falsely confident number.

#### Acceptance Criteria

1. WHEN a process crashes mid-run THEN the system SHALL recover by resuming from
   the append-only log, running only missing trials.
2. WHEN a judge dimension shows high sample SD or poor agreement on the
   calibration set THEN the system SHALL report that dimension as low-confidence
   (visually softened) or exclude it from the composite, recording the exclusion
   in the composite-weights version.
3. WHEN the retrieval backend is unhealthy at start THEN the system SHALL fail
   fast; WHEN it errors mid-run THEN the system SHALL mark affected trials errored,
   back off, and auto-pause past a threshold.
4. WHEN a cohort cell remains too thin after collapsing THEN the system SHALL
   render it as insufficient-data, never as a confident value.

### Requirement 14: Correctness properties verified by tests

**User Story:** As a maintainer, I want the load-bearing invariants verified by
property-based and unit tests, so that the harness's guarantees are enforced
mechanically.

#### Acceptance Criteria

1. WHEN the test suite runs THEN the system SHALL verify (via Hypothesis
   property-based tests where the property is universal) at minimum: retrieval is
   constant per item; every planned trial is recorded exactly once; resume is
   idempotent; accuracy is never averaged across answerability classes; timings
   are consistent; CI half-width is monotonically decreasing in n_items and only
   weakly in reps; the bootstrap point weights items equally; `required_reps`
   floors at 2 and detects the unreachable case; aggregations are a pure function
   of the log; and no number reaches the exec viz without a CI.
2. WHEN scoring functions are tested THEN the system SHALL verify nDCG/MRR/
   precision/recall against hand-computed gold rankings and abstention scoring
   against fixtures for each answerability class.
3. WHEN integration is tested THEN the system SHALL run a small plan end-to-end
   with mock adapters, a mock `/retrieve`, and a stub judge, asserting every
   planned trial is recorded once, resume runs zero new trials, and aggregates and
   frontier materialize.
4. WHEN the judge calibration set is scored THEN the system SHALL compute and
   surface judge↔human agreement as a reported quantity (not a pass/fail gate).

### Requirement 15: Local-only, throwaway operational posture

**User Story:** As a security-conscious operator, I want the harness to stay a
local throwaway tool with no new exposed surface, so that running it introduces no
production risk.

#### Acceptance Criteria

1. WHEN the web app binds THEN the system SHALL bind to localhost (loopback) only
   by default, and the no-authentication posture SHALL be an explicit documented
   choice valid only for loopback binding.
2. IF the app is ever bound to a non-loopback interface THEN authentication SHALL
   be required first (documented as a precondition).
3. WHEN the harness calls model/judge/embedding endpoints THEN it SHALL reuse the
   existing Bedrock credential chain, introduce no new secrets, and write no
   secrets to the event log.
4. WHEN handling model answers and judge outputs THEN the system SHALL treat them
   as data, never executing or evaluating them as instructions.
5. WHERE infrastructure is concerned THE system's Python backend SHALL use a
   local venv plus the existing Qdrant container only, with no Brazil package, no
   npm/npx in its runtime path, and no additional Docker. The TypeScript dashboard
   under `bakeoff/ui/` MAY use npm + Vite to build (the one sanctioned exception,
   scoped to the frontend and not required to run the backend).

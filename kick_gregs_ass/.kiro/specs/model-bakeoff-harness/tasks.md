# Implementation Plan

## Overview

Throwaway local harness. Python 3.10+, local venv + existing Qdrant container.
No Brazil, no npm/npx, no build step. All new code lives under `bakeoff/`
(package) and `bakeoff/ui/` (static frontend); tests under `bakeoff/tests/`. The
existing `src/` retrieval backend and `config.py` are reused, not modified.

Each task is sized to stay under the autonomy ceiling (~500 lines / ~10 files).
Property-based tests use Hypothesis and validate the design's Correctness
Properties (P1–P10).

## Tasks

- [x] 1. Package scaffold, dependencies, and core types
  - Create `bakeoff/__init__.py`, `bakeoff/config.py` (paths under `data/bakeoff/`,
    per-resource concurrency caps, default temperature 0.2, target CI half-width,
    confidence level, composite weights, judge model id), and `bakeoff/types.py`
    holding the frozen dataclasses from the design: `CohortKey`, `StageTimings`,
    `RetrievalRecord`, `AccuracyScores`, `JudgeScores`, `QualityScores`,
    `TrialEvent`, `StratumPlan`, `SamplingPlan`, `CI`, `Aggregate`, `FrontierPoint`,
    plus `Item`, `GoldFragment`, `ModelResponse`, `RetrievalResult`, `TrialSpec`,
    `CohortKey` helpers.
  - Add `hypothesis`, `httpx`, `numpy` to `requirements.txt` (fastapi/uvicorn/
    pydantic/boto3 already present); install into the existing `.venv`.
  - Implement `trial_id(model, item_id, rep, pass_name, plan_version)` as a
    deterministic stable hash, and `SCHEMA_VERSION` constant.
  - _Requirements: 8.1, 8.2, 15.5_

- [x] 2. TrialEvent JSONL serialization with validation and parse guard
  - Implement `bakeoff/eventlog.py`: `to_jsonl(event) -> str`,
    `from_jsonl(line) -> TrialEvent` (lossless round-trip of nested dataclasses),
    `append_event(path, event)` (atomic single-line append), and
    `read_events(path)` that skips a truncated/partial final line without raising.
  - Implement `validate_event(event)` enforcing: `abstention_correct` populated
    iff `answerability in {none, partial}`; `unwarranted_refusal` iff
    `answerability == full`; `end_to_end_ms == retrieval_total_ms +
    generation_total_ms` within float eps for non-error events.
  - Write unit tests for round-trip losslessness and each validation rule, and a
    parse-guard test (append a half-written line, assert `read_events` returns the
    complete prefix).
  - Write a Hypothesis property test: for any generated non-error event satisfying
    the timing relation, `validate_event` passes and `from_jsonl(to_jsonl(e)) == e`.
  - _Requirements: 8.1, 8.3, 8.4, 8.5, 14.1_

- [x] 3. Dataset loader and cohort normalization
  - Implement `bakeoff/dataset.py: DatasetLoader` reading `queries.jsonl`,
    `conversations.jsonl`, `conversation_turn1_gold.jsonl` (+ any `*_gold.jsonl`),
    `perspectives_ledger.jsonl`, `corpus_index.tsv` from a configurable dir
    (default `data/synthetic/`).
  - Normalize single-turn and multi-turn records into uniform `Item`s with a
    `CohortKey` (geography/proficiency/tone from the ledger persona; entry_route,
    momentary_state, answerability, turn_type from the record). Join multi-turn
    sets to gold by `set_id`+`turn`; retain ordered turns and per-turn state.
  - Implement `resolve_gold(node_ids)` via the corpus index and `cohort_cells()`
    enumerating non-empty cells. Fail loudly listing any unresolved `gold_node_id`.
  - Do not hard-code dataset sizes; operate on whatever counts exist.
  - Unit tests against the real data files: counts > 0, every item has a CohortKey,
    gold-integrity failure path triggers on a synthetic dangling id, multi-turn
    join is correct on a fixture.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

- [x] 4. Retrieval client over the existing substrate
  - Implement `bakeoff/retrieval_client.py: RetrievalClient` (async httpx) calling
    `POST /retrieve` and `GET /healthz` on the existing backend; return
    `RetrievalResult(fragments, fragment_ids, confidence, timings, cache_hit)`
    verbatim. Add an optional local result cache keyed by
    `(query, filters, candidate_n, top_k)` for backend-less replay.
  - `healthz()` gate used at run start.
  - Unit tests with a mock httpx transport: response mapped verbatim, cache returns
    identical fragment_ids on repeat, healthz failure surfaces clearly.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 5. Model adapter protocol and deterministic mock adapter
  - Implement `bakeoff/adapters/base.py: ModelAdapter` protocol (`name`, async
    `generate(item, fragments, temperature) -> ModelResponse`) capturing TTFT,
    generation latency, token usage; multi-turn prompt assembly from prior turns +
    fragments. Adapters never score.
  - Implement `bakeoff/adapters/mock.py: MockAdapter` — deterministic given a seed,
    configurable latency and answer-quality profile (incl. fabricate-on-
    unanswerable and refuse-on-answerable behaviors for scorer tests).
  - Implement `bakeoff/adapters/bedrock.py: BedrockModelAdapter` — a real adapter
    invoking a Bedrock model via streaming (reusing the existing credential chain),
    measuring true TTFT. Registered candidates configured in `bakeoff/config.py`.
  - Unit tests: mock determinism, TTFT/latency captured, multi-turn prompt includes
    prior turns.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 15.3_

- [x] 6. Retrieval-aligned and semantic-similarity scorers
  - Implement `bakeoff/scoring/retrieval_aligned.py`: precision@k, recall@k, MRR,
    nDCG@k of fragment ranking vs `gold_node_ids`; answer-grounding precision/recall
    (semantic attribution of answer sentences to fragment text; citation overlap if
    cited).
  - Implement `bakeoff/scoring/semantic.py`: cosine of Embed v4 vectors of answer
    vs ideal response, with an embedding cache keyed by content hash.
  - Unit tests: nDCG/MRR/precision/recall against hand-computed gold rankings
    (known fixtures with known answers); grounding precision/recall on a fixture
    where the model ignores the gold fragment.
  - _Requirements: 4.1, 4.2, 4.7_

- [x] 7. Judge scorer with anchored rubrics, k-sampling, and answerability
  - Implement `bakeoff/scoring/judge.py: JudgeScorer` — anchored rubric prompts for
    accuracy dims (faithfulness/correctness/completeness) and interaction dims
    (tone/empathy/clarity/actionability scored against `momentary_state`);
    evidence-anchored (quote the supporting span); `k` samples per answer reporting
    per-dimension mean and SD; position/order debiasing; fixed judge != candidate;
    content-hash cache.
  - Implement `bakeoff/scoring/answerability.py: score_answerability` per the
    design's spec (none→abstention_correct, partial→answer-and-flag,
    full→unwarranted_refusal), feeding the fabrication-on-unanswerable rate.
  - Implement `bakeoff/scoring/pipeline.py: ScoringPipeline.score_trial(...)`
    composing all scorers into `QualityScores` incl. the transparent weighted
    `composite` (weights from the plan).
  - Unit tests: abstention scoring per answerability class on mock-adapter outputs
    (fabricate-on-none → abstention_correct==0; refuse-on-full → unwarranted_refusal
    ==1); judge SD reported; composite uses plan weights; cache prevents re-call.
  - _Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 5.2, 5.3, 15.4_

- [x] 8. Statistics core: variance decomposition, bootstrap, required_reps
  - Implement `bakeoff/stats.py`: `cluster_bootstrap_ci(events, metric, level,
    n_boot, seed)` (resample items then reps; point = mean over items of rep-means),
    `normal_approx_ci(...)` (incremental, for live UI), `variance_decomp(events)`
    (between/within/judge), `estimate_required_reps(pilot_events, strata, target_w,
    z, budget)` (floor 2, budget clamp, multi-turn >= single-turn, unreachable
    detection when `sigma_between^2/n` exceeds target variance), and `paired_diff_ci`.
  - Unit tests: bootstrap CI coverage ≈ nominal on synthetic data with known mean;
    variance_decomp recovers planted sigmas; required_reps matches the closed-form
    equation and returns the unreachable signal on the impossible branch.
  - Hypothesis property tests: **P6** CI half-width monotonically decreasing in
    n_items and only weakly in reps; **P7** bootstrap point weights items equally
    regardless of per-item rep count; **P8** required_reps >= 2 and unreachable
    detection.
  - _Requirements: 6.3, 6.4, 6.5, 9.2, 9.4, 14.1, 14.2_

- [x] 9. Sampling planner and pilot
  - Implement `bakeoff/planner.py: SamplingPlanner`: `build_subsample(items)`
    (every non-empty cohort cell represented; collapse sparse cells),
    `pilot_plan(temperature, reps, subsample)`, `estimate_variances(pilot_events)`,
    and `required_reps(...)` producing a `SamplingPlan` (WIDE all items, DEEP
    subsample, multi-turn rep bump, confirmed temperature) serialized to
    `data/bakeoff/sampling_plan.json`.
  - Unit tests: subsample covers every non-empty cell; sparse-cell collapse; plan
    round-trips to/from JSON; multi-turn strata get reps >= single-turn for the same
    target.
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 12.1_

- [x] 10. Trial runner: parallel, bounded, resumable
  - Implement `bakeoff/runner.py`: `run_trial(...)` (retrieve→generate→score→
    TrialEvent, error-captured), `planned_trials(plan, models)`,
    `resume_point(events_path)`, and `schedule_run(plan, models, events_path,
    broker)` with per-resource `asyncio.Semaphore`s, atomic per-event append,
    exactly-once SSE publish, CPU scoring via `asyncio.to_thread`, retrieval-error-
    rate auto-pause, and pause/resume/abort hooks.
  - Unit/integration tests with mock adapters + mock retrieval + stub judge:
    every planned trial recorded once; a failing trial is recorded with `error`
    set and the run continues.
  - Hypothesis property tests: **P2** every planned trial recorded exactly once;
    **P3** resume is idempotent (second run = zero new trials); **P1** retrieval
    constant per item across reps/models; **P5** timings consistent for non-error
    events.
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 12.3, 13.1, 13.3, 14.1, 14.3_

- [x] 11. Aggregation engine
  - Implement `bakeoff/aggregate.py: AggregationEngine`: `aggregate(events,
    group_by)` (means + cluster-bootstrap CI + variance decomp + latency
    quantiles), `paired_diff_ci`, `frontier(events)` (Pareto on speed/quality with
    CIs), high-variance item flagging for the TARGETED pass, insufficient-data
    marking for thin cells, and refusal to average an accuracy metric across
    answerability classes. Materialize `reports/aggregate_<plan_version>.json`.
  - Unit tests: latency quantiles; frontier Pareto correctness; thin-cell marking.
  - Hypothesis property tests: **P4** accuracy never averaged across answerability
    (engine rejects such a group); **P9** aggregation is a pure deterministic
    function of the log given a fixed seed; **P10** every FrontierPoint / cohort
    cell carries a CI or is marked insufficient-data.
  - _Requirements: 5.4, 5.5, 9.1, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 12.2, 13.4, 14.1_

- [x] 12. FastAPI backend: JSON API, SSE broker, control endpoints, static serving
  - Implement `bakeoff/app.py`: FastAPI app bound to localhost; `SSEBroker`
    (publish/subscribe, exactly-once delivery); routes `GET /api/models`,
    `GET /api/aggregate` (group_by/metric/cohort filters, normal-approx CIs live),
    `GET /api/stream` (text/event-stream), `POST /api/control/{pause,resume,abort}`,
    `GET /healthz`, and `/exec/...` data routes reading materialized reports.
    Serve the built TypeScript SPA bundle from `bakeoff/ui/dist/` at `/` when
    present (the API is the contract; the SPA is a separate client — AD-4).
    Document the loopback-only no-auth posture. Enable permissive CORS for the
    Vite dev-server origin in dev only.
  - Unit/integration test: SSE smoke — a small run emits exactly one
    `trial_completed` per appended event; control endpoints toggle runner state;
    app binds to 127.0.0.1; API JSON payload shapes match the documented contract.
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 13.2, 13.3, 15.1, 15.2_

- [ ] 13. TypeScript dashboard scaffold + live monitoring view (Vite)
  - Scaffold a TypeScript + Vite SPA in `bakeoff/ui/` (package.json, tsconfig,
    vite.config with a dev proxy to the FastAPI backend, build output to
    `bakeoff/ui/dist/`). Define type-checked interfaces mirroring the backend
    payloads (`ModelStatus`, `Aggregate`, `CI`, `FrontierPoint`) in a `src/api/`
    client module, plus a typed SSE subscription.
  - Implement the live monitoring view: all-models overview (status, per-pass
    progress, running quality/speed with live CIs) and single-model focus
    (per-cohort running averages, recent trials, live latency distribution,
    high-variance flags). Subscribe to `/api/stream`, update in place. Control
    buttons call the POST endpoints.
  - Charting via a TS lib (D3 / Observable Plot / visx). `npm run build` produces
    `dist/`; `npm run typecheck` passes. Document dev/build in `bakeoff/ui/README.md`.
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

- [x] 14. Executive visualization views (TypeScript SPA)
  - Implement the exec views within the same TS SPA (`src/exec/`): hero
    speed/quality frontier (median + p90 whisker × quality CI band, Pareto
    highlighted, dominated de-emphasized, overlapping CIs shown as
    not-distinguished); metric toggle and live composite re-weighting; cohort
    heatmap with CI-as-opacity drilldown; answerability/safety panel (separate
    answerable-accuracy vs unanswerable-abstention bars, fabrication flag); example
    inspector (best/typical/worst vs ideal + gold + judge evidence); provenance
    footer on every view and on static HTML export. Squishy interaction metrics
    rendered with softer-confidence treatment.
  - Reads materialized `reports/aggregate_<plan_version>.json` via `/exec/...`.
  - `npm run build` + `npm run typecheck` pass; an automated backend check that the
    exec data route refuses an aggregate lacking a CI.
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7_

- [x] 15. Orchestration entrypoint, calibration reporting, and end-to-end wiring
  - Implement `bakeoff/main.py` (or `run_bakeoff.sh`) wiring the operator flow:
    load dataset → register candidates → pilot → size plan → full run (UI live) →
    aggregate → frontier/reports, with crash-resume on re-invoke. Implement
    `bakeoff/calibration.py` to score a small human-labeled calibration set with the
    judge and report judge↔human agreement (reported, not gated), surfaced in the
    provenance footer and used to soften/exclude low-agreement dimensions.
  - Integration test: small end-to-end plan with mock adapters, mock retrieval, stub
    judge — every planned trial recorded once, resume runs zero new trials,
    aggregates + frontier + report materialize, calibration agreement computed.
  - Add a `bakeoff/README.md` covering run steps, the loopback/no-auth posture, and
    the sourcing caveat (evaluation metrics are general industry practice, pending
    internal validation).
  - _Requirements: 12.1, 12.2, 13.2, 14.3, 14.4, 15.1, 15.5_

- [x] 16. Full test-suite green and self-contained verification
  - Run the entire `bakeoff/tests/` suite (unit + Hypothesis property + integration)
    and fix failures. Confirm all ten Correctness Properties are exercised. Since
    this is not a Brazil workspace, `brazil-recursive-cmd` does not apply; the
    canonical verification is `python -m pytest bakeoff/tests/` from the repo root in
    the project `.venv`. Report the result as the final green check.
  - _Requirements: 14.1, 14.2, 14.3, 14.4_
  - **Verification record:** `python -m pytest bakeoff/tests/` → **338 passed**.
    All ten Correctness Properties exercised by named property-based tests:
    P1 (retrieval constant per item), P2 (every planned trial recorded once),
    P3 (idempotent resume), P4 (accuracy never blended across answerability),
    P5 (timing identity), P6 (CI half-width breadth-beats-depth), P7 (bootstrap
    weights items equally), P8 (required_reps floor + unreachable detection),
    P9 (aggregation pure/deterministic), P10 (no number without a CI). Frontend
    gate: `npm run typecheck` clean and `npm run build` emits `bakeoff/ui/dist/`;
    the FastAPI backend serves the built SPA at `/`. End-to-end operator flow
    (load → pilot → size plan → run → aggregate → report) verified as a real
    program over offline doubles, and the exec report data path verified against
    the live `/exec/aggregate` + `/api/aggregate` routes.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": "A", "tasks": ["1"] },
    { "wave": "B", "tasks": ["2", "3", "4"] },
    { "wave": "C", "tasks": ["5", "6", "8"] },
    { "wave": "D", "tasks": ["7", "9", "11"] },
    { "wave": "E", "tasks": ["10"] },
    { "wave": "F", "tasks": ["12"] },
    { "wave": "G", "tasks": ["13", "14"] },
    { "wave": "H", "tasks": ["15"] },
    { "wave": "I", "tasks": ["16"] }
  ],
  "dependencies": {
    "1": [],
    "2": ["1"],
    "3": ["1"],
    "4": ["1"],
    "5": ["1", "3"],
    "6": ["1", "3"],
    "7": ["1", "5", "6"],
    "8": ["1", "2"],
    "9": ["3", "8"],
    "10": ["2", "4", "5", "7", "9"],
    "11": ["2", "8"],
    "12": ["10", "11"],
    "13": ["12"],
    "14": ["11", "12"],
    "15": ["9", "10", "11", "14"],
    "16": ["2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"]
  }
}
```

Visual summary:

```
1  (scaffold + types)
├─ 2  (event log)            depends on 1
├─ 3  (dataset loader)       depends on 1
├─ 4  (retrieval client)     depends on 1
├─ 5  (model adapters)       depends on 1, 3
├─ 6  (retrieval/semantic scorers)   depends on 1, 3
├─ 7  (judge + answerability + pipeline)  depends on 1, 5, 6
├─ 8  (statistics core)      depends on 1, 2
├─ 9  (planner + pilot)      depends on 3, 8
├─ 10 (runner)               depends on 2, 4, 5, 7, 9
├─ 11 (aggregation engine)   depends on 2, 8
├─ 12 (FastAPI + SSE)        depends on 10, 11
├─ 13 (live frontend)        depends on 12
├─ 14 (exec viz)             depends on 11, 12
├─ 15 (orchestration + calibration)  depends on 9, 10, 11, 14
└─ 16 (full suite green)     depends on all (2–15)
```

## Notes

- The frontend is a TypeScript single-page app (Vite) under `bakeoff/ui/`,
  consuming the FastAPI JSON/SSE API with type-checked payload interfaces and a TS
  charting library (D3 / Observable Plot / visx) — chosen over vanilla JS because
  the deliverable is exec-grade interactive data viz and typed API contracts guard
  accuracy (AD-4). The npm/Vite build is scoped to the frontend; the Python
  backend stays free of npm/npx in its runtime path.
- The existing retrieval backend (`src/`, `config.py`) and the synthetic dataset
  are reused, never modified. Retrieval is a held constant.
- Property-based tests (Hypothesis) cover Correctness Properties P1–P10 from the
  design; tasks 2, 8, 10, 11 carry them.
- The sampling plan is data (`sampling_plan.json`), not code: reps and temperature
  are pilot-driven (tasks 8, 9), satisfying "choose by pilot, not by gut."
- Sourcing caveat: evaluation-metric and judge-calibration choices are general
  industry practice (cited in `design.md`), not Amazon-internal guidance, pending
  internal validation before any number defends a decision upward.
- Verification: this is not a Brazil workspace, so `brazil-recursive-cmd` does not
  apply; `python -m pytest bakeoff/tests/` in the project `.venv` is the canonical
  green check (task 16).

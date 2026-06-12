# Implementation Plan: Ragas Eval Visualization Dashboard

## Overview

This plan implements the dashboard as two cooperating halves joined by one data contract:

- A **Python producer** (`bakeoff/eval/`) — Metric_Engine, Ragas_Adapter,
  Retrieval_Metric_Computer, Experiment_Runner, and the append-only Event_Store — that
  computes and durably records `EvalInstance` records (requirements Areas A–B).
- A **TypeScript visualization app** (`bakeoff/ui/src/eval/`, `views/`, `api/`) built on the
  existing React 18 + Vite + ECharts project, managed with **bun** (Area C), plus prompt
  management/export (Area D), cross-cutting invariants (Area E), and an on-demand
  combinatorial run capability (Area F).

The `EvalInstance` JSON record is the boundary between the two halves. The design document
marks the producer "out of scope," but `requirements.md` (Areas A–B) and the build guidance
require it; this plan treats Areas A–B as in-scope and reuses existing harness assets rather
than reinventing them: `bakeoff/scoring/retrieval_aligned.py` (precision@k / recall@k /
nDCG@k), `bakeoff/eventlog.py` (append-only log discipline), `bakeoff/dataset.py` (gold
links), `bakeoff/app.py` (`SSEBroker` + dedicated-broker + durable-status discipline), and on
the UI side `components/EChart.tsx`, `exec/quality.ts`, `exec/exportSnapshot.ts`,
`api/useOptimizerV2Stream.ts`, `lib/format.ts`, and `styles/theme.css`.

Ordering follows the build guidance: the metric-computation + data/event-store layer
(Areas A–B) and the backing endpoints land first, then the visualization app (Area C), then
prompt-management/export (Area D), then the cross-cutting invariant suite (Area E), with the
latent on-demand combinatorial run capability (Area F / Requirement 22) sequenced last as an
additive layer. Correctness outranks latency throughout — the design's Correctness Properties
(P1–P13) are each turned into their own property/unit test sub-task placed next to the code
they validate.

### Verification gates (run by each task; offline/fake tests are network-free)

- **Python tasks:** `.venv/bin/python -m pytest bakeoff/tests/ -q`
- **Frontend build gate:** `bun run build` in `bakeoff/ui` (runs `tsc --noEmit && vite build`)
- **Frontend unit/property tests:** `bunx vitest run` in `bakeoff/ui` (network-free)

### Stability / scope posture

Each task is self-contained, references the specific requirement clauses it satisfies,
includes its own verification step, and is scoped under the autonomy ceiling (~500 lines /
~10 files). Tasks that touch the same file are placed in different dependency waves.
Loopback-only, no-auth, bun-only, synthetic-non-PII posture is preserved (Req 21); ragas /
retrieval / NDCG / composite are external-methodology signals and labeled as such wherever
shown or exported (Req 4.6, Req 20, P13).

## Tasks

- [ ] 1. Eval data contract and durable Event_Store (Python — Area B foundation)
  - [ ] 1.1 Create `bakeoff/eval/` package and `bakeoff/eval/models.py`
    - Define `MetricValue` (value: float|None, unavailable: bool, optional k, ragas_version, bedrock_model_id), `StageTimings` (retrieval_ms, generation_ms, extra_ms), and `EvalInstance` (instance_id, agent_id, session_id, instance_index, timestamp, latency_ms, stage_timings, corpus_size, retrieval_cached, ragas map, retrieval map, confidence/volume/cost, prompt_id, category, status, error) per design C1
    - Enforce validation on construction: `clamp_unit` every metric value to [0,1]; `value is None ⟺ unavailable is True`; `ragas` and `retrieval` key sets are disjoint; flag non-positive/non-finite `latency_ms`; keep per-stage timings separate from end-to-end latency
    - Provide `to_dict` / `from_dict` JSON (de)serialization for the durable store and the HTTP seam; operate only on synthetic, non-PII fields
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 1.3, 1.4, 2.3, 2.4, 7.1, 7.2, 7.3, 7.5, 21.3_

  - [ ]* 1.2 Write unit test for `EvalInstance` validation invariants
    - Assert clamp to [0,1], `value is None ⟺ unavailable`, ragas/retrieval disjoint key sets, non-positive latency flagged, round-trip `from_dict(to_dict(x)) == x`
    - File: `bakeoff/tests/test_eval_models.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_eval_models.py -q`
    - _Requirements: 1.3, 1.4, 2.4, 7.4_

  - [ ] 1.3 Implement `bakeoff/eval/event_store.py` append-only Event_Store
    - Append one durable `EvalInstance` record per call to a JSONL log (mirror the `bakeoff/eventlog.py` discipline); provide `append`, `read_all`/`reconstruct`, and `read_recent(limit)`
    - Guarantee the complete state of every view is reconstructable from the store alone; reads tolerate a partially written / malformed trailing line without raising
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 8.1, 8.2_

  - [ ]* 1.4 Write unit test for Event_Store append/reconstruct
    - Assert append-then-reconstruct returns every record in order; durability across a fresh reader instance; malformed trailing line is tolerated
    - File: `bakeoff/tests/test_eval_event_store.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_eval_event_store.py -q`
    - _Requirements: 8.1, 8.2_

- [ ] 2. Metric computation layer (Python — Area A)
  - [ ] 2.1 Implement `bakeoff/eval/catalog.py` metric catalog
    - Encode the catalog as data (`MetricCatalogEntry`: name, family, scope in/out, priority, customizable_prompt, external=True): RAG family + Nvidia family + NL-comparison family as prioritized in-scope candidates; traditional + general-purpose as lower-priority in-scope; multimodal/agentic/SQL marked out-of-scope
    - Provide `default_enabled()` excluding out-of-scope entries; every entry carries the external-methodology flag
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 4.1, 4.2, 4.3, 4.5, 4.6_

  - [ ]* 2.2 Write unit test for metric catalog
    - Assert out-of-scope entries are absent from `default_enabled()`; in-scope priority ordering; every entry `external is True`
    - File: `bakeoff/tests/test_eval_catalog.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_eval_catalog.py -q`
    - _Requirements: 4.3, 4.4, 4.6_

  - [ ] 2.3 Implement `bakeoff/eval/retrieval_metrics.py` Retrieval_Metric_Computer
    - Compute precision@k, recall@k, NDCG@k by delegating to the existing `bakeoff/scoring/retrieval_aligned.py` functions (reuse, do not reinvent); record the `k` used on each value
    - When a query has no resolvable Gold_Link, record each retrieval metric as `unavailable`; store precision@k and recall@k both without deriving one from the other
    - Read retrieval results only; never mutate the substrate or corpus
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 19.1_

  - [ ]* 2.4 Write unit test for Retrieval_Metric_Computer
    - Assert k recorded on every value; no-gold ⟹ all retrieval metrics unavailable; precision and recall both stored independently; computation issues no network call and no substrate write
    - File: `bakeoff/tests/test_eval_retrieval_metrics.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_eval_retrieval_metrics.py -q`
    - _Requirements: 2.2, 2.3, 2.6, 19.1_

  - [ ] 2.5 Implement `bakeoff/eval/ragas_adapter.py` Ragas_Adapter
    - Compute enabled ragas generation-quality metrics via ragas configured with its Amazon Bedrock LLM + embedding adapter; record metric name, numeric value, ragas version, and Bedrock model id for each value; store on a 0.0–1.0 higher-is-better scale
    - On per-metric failure, record that metric `unavailable` and retain all successfully computed metrics for the same instance
    - Provide an offline test mode that injects a fake LLM + fake embedding component and makes no network call
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [ ]* 2.6 Write unit test for Ragas_Adapter offline mode
    - Assert offline/fake mode computes values with zero network calls; one failing metric does not drop the others; provenance (name, value, ragas_version, bedrock_model_id) recorded on each value
    - File: `bakeoff/tests/test_eval_ragas_adapter.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_eval_ragas_adapter.py -q`
    - _Requirements: 1.2, 1.4, 1.5_

  - [ ] 2.7 Implement `bakeoff/eval/metric_engine.py` Metric_Engine
    - Orchestrate scoring of an instance: compute ragas (via Ragas_Adapter) and retrieval (via Retrieval_Metric_Computer) metrics, store them as distinct, separately labeled maps so generation- and retrieval-quality are never conflated, and append exactly one `EvalInstance` to the Event_Store
    - Compute/store each metric independently of any Composite_Weight_Set (the composite is recomputed downstream); do not alter any Authoritative_Judge decision
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 1.6, 2.4, 8.1, 18.1_

  - [ ]* 2.8 Write unit test for Metric_Engine
    - Assert ragas and retrieval stored in disjoint maps; exactly one record appended per scored instance; recorded values unaffected by any weight set; no judge-decision mutation
    - File: `bakeoff/tests/test_eval_metric_engine.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_eval_metric_engine.py -q`
    - _Requirements: 1.6, 2.4, 8.1, 18.1_

- [ ] 3. Experiment_Runner: multi-agent runs and corpus-size sweep (Python — Area B)
  - [ ] 3.1 Implement `bakeoff/eval/experiment_runner.py` multi-agent runs
    - Accept the Agent_Under_Test set as configuration (N ≥ 3, no fixed count assumed); execute every listed agent against the same query set under the same read-only retrieval conditions, producing one Instance per (agent, query, corpus size)
    - Record agent id, session id, strictly-increasing instance_index within a session, corpus size, end-to-end latency, per-stage timings, and cached-retrieval flag; reuse identical retrieval results for the same query+corpus across compared agents
    - On a single agent/query failure, record an Instance with `status="failed"` and continue the remaining agents/queries
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 7.1, 7.2, 7.3, 7.4, 7.5, 19.3_

  - [ ]* 3.2 Write unit test for multi-agent runs
    - Assert N ≥ 3 produces one Instance per (agent, query, corpus); a forced single-execution failure yields a failed Instance and the run continues; the same query+corpus yields identical retrieval results across agents; instance_index strictly increasing per session
    - File: `bakeoff/tests/test_eval_runner.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_eval_runner.py -q`
    - _Requirements: 5.2, 5.4, 5.5, 7.4, 19.3_

  - [ ] 3.3 Implement corpus-size sweep in `experiment_runner.py`
    - Given an ordered series of corpus sizes, run the same constant query set against each size; label every Instance with its corpus size and record latency + the inputs needed for the composite
    - Treat each sized corpus as a prepared, read-only experiment input (never mutate the canonical substrate); if a size cannot be prepared, record it unavailable and continue the remaining sizes
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 19.2_

  - [ ]* 3.4 Write unit test for corpus-size sweep
    - Assert the query set is held constant across sizes; each Instance is labeled with its corpus size; an unpreparable size is recorded unavailable and the sweep continues; no substrate mutation
    - File: `bakeoff/tests/test_eval_runner_sweep.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_eval_runner_sweep.py -q`
    - _Requirements: 6.4, 6.5, 19.2_

- [ ] 4. Backend endpoints and dedicated eval broker (Python — Areas B/C/E)
  - [ ] 4.1 Add eval state to `AppState` in `bakeoff/app.py`
    - Add a dedicated `eval_broker = SSEBroker()` (never shares the bake-off or optimizer streams), eval lifecycle fields (status idle/running/completed/failed, error, request, task), and an `eval_snapshot()` that returns the durable-backfill view state reconstructed from the Event_Store (agents, sessions, corpus sizes, instance count, windowed instances/rollups, sweep progress)
    - `eval_snapshot()` is empty-but-well-formed before any run and never 500s (defensive try/except, mirroring `optimizer_v2_snapshot`)
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 8.3, 15.2, 15.5_

  - [ ] 4.2 Add eval HTTP routes to `bakeoff/app.py`
    - `POST /api/eval/runs/start` (launch a multi-agent run / corpus-size sweep over a configured agent set N ≥ 3 + metric selection; 202 + snapshot, 409 if a run is active, 422 on unknown agent/metric); `GET /api/eval/status` (durable backfill authority); `GET /api/eval/stream` (StreamingResponse over `eval_broker.subscribe()`, delta-only, no replay); `GET /api/eval/instances/recent?limit=N` (replay seed shaped identically to the `eval_instance_appended` payload)
    - Publish exactly one `eval_instance_appended` event per appended record; additive only (no existing route changes); inherit the loopback-only, no-auth posture
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 8.3, 8.4, 15.1, 15.2, 15.5, 21.2_

  - [ ]* 4.3 Write backend route tests with an offline fake producer
    - Using FastAPI `TestClient` and a network-free fake producer: `start` returns 202 and flips status to running, a second start returns 409, unknown agent/metric returns 422; `status` is well-formed before/after a run and never 500s on a malformed store; `stream` opens; `instances/recent` replays seed records; an eval event never appears on the bake-off `/api/stream` or optimizer streams (broker isolation)
    - File: `bakeoff/tests/test_app_eval.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_app_eval.py -q`
    - _Requirements: 8.3, 8.4, 15.1, 15.2, 15.5_

- [ ] 5. Checkpoint — Ensure all backend tests pass
  - Run `.venv/bin/python -m pytest bakeoff/tests/ -q`. Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Frontend dependencies and 3D registration (TypeScript — bun only)
  - [ ] 6.1 Add eval frontend dependencies and test tooling
    - In `bakeoff/ui`: `bun add echarts-gl@^2.0.9`; `bun add -d vitest fast-check`; add a `"test": "vitest run"` script and a minimal `vitest.config.ts` (jsdom not required — builders are tested as pure option objects); do not introduce npm/npx/yarn
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 9.5, 21.1_

  - [ ] 6.2 Register `echarts-gl` in `components/EChart.tsx`
    - Add the single side-effect import `import "echarts-gl";` so `grid3D` and the `scatter3D`/`line3D`/`surface` series resolve through the existing typed wrapper; no wrapper API change
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 9.5_

- [ ] 7. Frontend data contract and quality composite (TypeScript — Areas A/C)
  - [ ] 7.1 Add eval data shapes to `api/types.ts`
    - Add `RagasMetricName`, `RetrievalMetricName`, `MetricValue`, `StageTimings`, `EvalInstance`, `MetricCatalogEntry`, `EvalStatus`, and the SSE payload shapes (`EvalInstanceAppended`, `EvalRunStatusEvent`, `EvalSweepProgress`) per design C1/C7/Data Models
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 1.2, 2.2, 4.1, 8.3_

  - [ ] 7.2 Implement `eval/evalQuality.ts` configurable composite
    - Build on `exec/quality.ts`: `clampUnit`, `normalizeWeights` (renormalize present-component positive weights to sum 1.0; reject ≤0-sum sets by returning null), `expandOthers` (deterministic even split of the "others" weight), `compositeQuality` (pure, never mutates recorded values, records weightSetId + missing/used components), and `DEFAULT_WEIGHT_SET` matching the documented default (Faithfulness 0.30, Answer Relevancy 0.25, Context Precision 0.15, Context Recall 0.15, Entities Recall 0.10, Others 0.05)
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 12.7_

  - [ ]* 7.3 Write property test for composite determinism and weight normalization
    - **Property 1: Composite determinism + weights sum to 1.0** — identical (instance, weightSet) ⟹ identical result; effective present-component weights sum to 1.0 (±epsilon); ≤0-sum set ⟹ `score === null`
    - **Validates: Requirements 3.1, 3.2, 3.4, 3.7**
    - Use `fast-check`; file `src/eval/__tests__/evalQuality.p1.test.ts`; verify `bunx vitest run`

  - [ ]* 7.4 Write property test for composite monotonicity
    - **Property 2: Composite monotonic in each non-negative-weighted metric** — raising one available component's value with others fixed never lowers the score
    - **Validates: Requirements 3.1**
    - Use `fast-check`; file `src/eval/__tests__/evalQuality.p2.test.ts`; verify `bunx vitest run`

  - [ ]* 7.5 Write property test for metric range clamping
    - **Property 3: Every consumed metric value is clamped/validated to [0,1]** — non-finite or out-of-range inputs are coerced, never propagated
    - **Validates: Requirements 1.3, 2.1, 3.4**
    - Use `fast-check`; file `src/eval/__tests__/evalQuality.p3.test.ts`; verify `bunx vitest run`

  - [ ]* 7.6 Write property test for weight-change immutability
    - **Property 8: Recorded metric values are never altered by weight changes** — recomputing with a different weight set leaves `EvalInstance.ragas`/`retrieval` unchanged
    - **Validates: Requirements 3.3, 12.7**
    - Use `fast-check`; file `src/eval/__tests__/evalQuality.p8.test.ts`; verify `bunx vitest run`

  - [ ]* 7.7 Write unit test for composite provenance recording
    - **Property 10: Composite records its weight-set id and missing components** — every result carries `weightSetId` and exactly the weighted-but-unavailable components
    - **Validates: Requirements 3.5, 3.6**
    - File `src/eval/__tests__/evalQuality.p10.test.ts`; verify `bunx vitest run`

- [ ] 8. Axis-mapping, agent-color, and methodology primitives (TypeScript — Areas C/E)
  - [ ] 8.1 Implement `eval/axisMapping.ts`
    - Define `AxisVariable`/`AxisScale`/`AxisBinding`/`AxisMapping`, `DEFAULT_AXIS_MAPPING` (X=latency log lower-better, Y=quality linear higher-better, Z=instance_index), `LOG_FLOOR_MS`/`logSafe` (floor non-positive to a positive epsilon), and `axisValue` projecting an instance + composite onto a binding
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 10.1, 14.1_

  - [ ]* 8.2 Write property test for log-scale defensiveness
    - **Property 7: Log-scale latency axis handles zero/negative defensively** — `logSafe(ms) ≥ LOG_FLOOR_MS > 0` for every real input
    - **Validates: Requirements 10.1, 14.4**
    - Use `fast-check`; file `src/eval/__tests__/axisMapping.p7.test.ts`; verify `bunx vitest run`

  - [ ]* 8.3 Write unit test for default axis mapping
    - **Property 11: Axis mapping is configurable and defaults to the mockup mapping** — `DEFAULT_AXIS_MAPPING` binds X→latency(log,lower), Y→quality(linear,higher), Z→instance_index; bindings are reconfigurable
    - **Validates: Requirements 10.1, 14.1**
    - File `src/eval/__tests__/axisMapping.p11.test.ts`; verify `bunx vitest run`

  - [ ] 8.4 Implement `eval/agentColor.ts`
    - `buildAgentColorMap(agentIds)` assigns from a fixed theme-aligned palette in stable sorted agent-id order, falling back to hashed `lib/format.ts::modelColor` only beyond the palette; the map is stable across renders/tabs and injective for N ≤ palette size
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 10.7_

  - [ ]* 8.5 Write property test for agent-color mapping
    - **Property 5: Agent-to-color mapping is stable and injective** — same agent ⟹ same color regardless of arrival order; injective for N ≤ palette size
    - **Validates: Requirements 10.7**
    - Use `fast-check`; file `src/eval/__tests__/agentColor.p5.test.ts`; verify `bunx vitest run`

  - [ ] 8.6 Implement `eval/methodology.ts` external-methodology label
    - Export the canonical caveat text ("external/industry methodology, not Amazon-internal guidance") plus a helper used by every metric display and the export footer, and the longer "not validated against Amazon-internal primary sources" notice for the app shell
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 4.6, 20.1, 20.3_

- [ ] 9. Real-time data plumbing and view selectors (TypeScript — Area C)
  - [ ] 9.1 Add eval client functions and `api/useEvalStream.ts`
    - Extend `api/client.ts` with typed calls for `/api/eval/status`, `/api/eval/instances/recent`, and `/api/eval/runs/start`; implement `useEvalStream` as a direct analog of `useOptimizerV2Stream.ts`: seed once from recent, poll `/api/eval/status` every 3s for durable backfill, open `/api/eval/stream` for live deltas, and merge into a `Map` keyed by `instance_id` (dedupe so seed/backfill/stream are idempotent)
    - On reconnect, reconstruct from status before resuming deltas; never blank the surface
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 8.5, 15.1, 15.2, 15.3, 15.5_

  - [ ] 9.2 Implement `eval/evalSelectors.ts`
    - Define `EvalSelection` and `ChartView`; `deriveChartView` (pure: filter by agents/sessions/prompt/category, apply smoothing window, compute per-instance composite, account for instances filtered out or lacking a plottable axis value explicitly rather than silently dropping); `fromStatus`/`fromStream` builders; `detectDrift` (downward quality across consecutive sessions) and `detectInconsistency` (high quality variance)
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 9.4, 11.5, 12.2, 12.4, 12.5, 13.3, 13.4_

  - [ ]* 9.3 Write property test for durable reconstruction equality
    - **Property 6: View fully reconstructs from durable status backfill (no blanking)** — `deriveChartView(fromStatus(R)) ≡ deriveChartView(fromStream(R))` for the same record set R (dedupe by instance_id makes seed/backfill/stream idempotent)
    - **Validates: Requirements 8.5, 15.2, 15.3, 15.4, 15.5**
    - Use `fast-check`; file `src/eval/__tests__/evalSelectors.p6.test.ts`; verify `bunx vitest run`

  - [ ]* 9.4 Write property test for view-derivation accounting
    - **Property 4 (derivation half): no in-view record is silently dropped** — every instance in a `ChartView` is either plotted or explicitly accounted for as filtered/non-plottable
    - **Validates: Requirements 8.4**
    - Use `fast-check`; file `src/eval/__tests__/evalSelectors.p4.test.ts`; verify `bunx vitest run`

  - [ ]* 9.5 Write unit test for drift and inconsistency detection
    - Assert `detectDrift` fires on a monotone downward session trend and not otherwise; `detectInconsistency` fires on high cross-instance quality variance
    - File `src/eval/__tests__/evalSelectors.cues.test.ts`; verify `bunx vitest run`
    - _Requirements: 13.3, 13.4_

- [ ] 10. Chart-option builders (TypeScript — Area C)
  - [ ] 10.1 Implement `eval/charts3d.ts` 3D builders
    - Pure functions returning typed `EChartsOption` consumed by `EChart.tsx`: `build3DBase` (grid3D + xAxis3D/yAxis3D/zAxis3D with the log latency axis), `buildTrajectory3DOption` (line3D, one path per agent ordered by instance_index), `buildScatter3DOption` (scatter3D, one point per Instance carrying instance_id), `buildSurface3DOption` (downsampled lattice interpolation), `buildBubble3DOption` (scatter3D + symbolSize by confidence|volume|cost); per-agent series use the stable color map; axis names encode higher-Y-better / lower-X-better / forward-Z-later; tooltip exposes agent/latency/quality/session/index/corpus
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 14.1, 14.3, 14.4_

  - [ ]* 10.2 Write property test for 3D rendered-point bijection
    - **Property 4 (3D half): rendered-point ↔ record bijection** — the multiset of `instance_id`s in the 3D series `data` equals the plottable instances' `instance_id`s (no phantom/dropped points); trajectory series are ordered by `instance_index`
    - **Validates: Requirements 10.2, 10.3, 8.4**
    - Use `fast-check`; file `src/eval/__tests__/charts3d.p4.test.ts`; verify `bunx vitest run`

  - [ ] 10.3 Implement `eval/charts2d.ts` 2D builders
    - `buildSpeedQuality2DOption` (latency×quality scatter + Ideal_Region quadrant marker), `buildMetricOverInstances2DOption` (selected metric or composite vs instance_index, one line per agent), `buildCorpusCurve2DOption` (latency and quality vs corpus size), `buildRetrievalVsRagas2DOption` (retrieval metrics and ragas metrics as distinct, separately labeled series — never summed); shared color map and composite
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 13.1_

  - [ ]* 10.4 Write property test for 2D series separation and bijection
    - **Property 9: Retrieval and ragas metrics are never conflated** — the retrieval-vs-ragas builder emits them as distinct labeled series with no value summed across the two; plus the 2D rendered-point ↔ record bijection (**Property 4**, 2D half)
    - **Validates: Requirements 2.4, 2.6, 11.4, 8.4**
    - Use `fast-check`; file `src/eval/__tests__/charts2d.p9.test.ts`; verify `bunx vitest run`

- [ ] 11. Views and application shell (TypeScript — Area C)
  - [ ] 11.1 Implement the `Control_Panel` component
    - Controls for selecting agents (≥3 at once), sessions/time range, which component metrics contribute to the composite and each weight, prompt and category filters, smoothing window, and axis mapping; emits an `EvalSelection`; changes apply without a full-page reload; adjusting a weight recomputes the displayed score from unchanged recorded values
    - File: `bakeoff/ui/src/eval/ControlPanel.tsx`
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7_

  - [ ] 11.2 Implement `views/Eval3D.tsx`
    - Host the Control_Panel + a 3D archetype sub-selector (trajectory|scatter|surface|bubble) rendering via the 3D builders and `useEvalStream`; render the Ideal_Region indicator and Watch_For cues (high-latency/low-quality zone, drift, inconsistency); a legend for quality/latency bands; rotate/zoom via `grid3D.viewControl`; hover/select shows the full instance detail; present ragas-derived and Authoritative_Judge signals as distinct labeled values; show the external-methodology label on every metric display
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 10.6, 13.1, 13.2, 13.3, 13.4, 13.5, 14.2, 14.3, 18.2, 18.3, 20.1_

  - [ ] 11.3 Implement `views/Eval2D.tsx`
    - Render the four 2D views sharing the Control_Panel selection and updating on any selection change; present ragas vs judge as distinct labeled signals; show the external-methodology label on every metric display
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 18.2, 18.3, 20.1_

  - [ ] 11.4 Extend `App.tsx` with the eval tabs
    - Add `eval-3d` and `eval-2d` to the `Tab` union and tab strip, rendering `Eval3D`/`Eval2D` with no full-page reload and keeping every tab consistent with the same `useEvalStream` state; render the global "methodology not validated against Amazon-internal sources" notice in the shell
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 20.3_

- [ ] 12. Checkpoint — Ensure frontend builds and tests pass
  - Run `bun run build` and `bunx vitest run` in `bakeoff/ui`. Ensure all pass, ask the user if questions arise.

- [ ] 13. Prompt management and export (Areas A/D)
  - [ ] 13.1 Implement prompt override store and `/api/eval/prompts` routes
    - Add `bakeoff/eval/prompt_store.py` (persist a named, versioned prompt override scoped to a run by subclassing the ragas metric prompt — instruction + examples — following the "modifying prompts in metrics" mechanism; reset-to-default supported) and `GET /api/eval/prompts` / `PUT /api/eval/prompts/{metric}` in `bakeoff/app.py`; the Metric_Engine applies a changed prompt only to instances computed after the change, records the prompt configuration id alongside each ragas value, and never mutates retrieval or previously recorded values
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 16.1, 16.3, 16.4, 16.5, 16.6, 16.7_

  - [ ]* 13.2 Write backend test for prompt management
    - Assert GET/PUT round-trips an override; a changed prompt applies only to later instances while previously recorded values are unchanged; the prompt-config id is recorded on new values; retrieval is never mutated; no network call in fake mode
    - File: `bakeoff/tests/test_app_eval_prompts.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_app_eval_prompts.py -q`
    - _Requirements: 16.5, 16.6, 19.1_

  - [ ] 13.3 Implement `views/EvalMetrics.tsx` and add the metrics tab
    - Render the metric catalog with in-scope/out-of-scope labeling (out-of-scope excluded from the default enabled set) and the external-methodology label; a weight editor over enabled components (recompute from unchanged recorded values); a Prompt_Manager that shows current instruction + examples, renders the prompt for a sample input, lets the user edit/persist an override and reset to default, and shows non-customizable metrics as not editable; add `eval-metrics` to the `App.tsx` `Tab` union/strip
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 4.4, 4.6, 12.3, 16.1, 16.2, 16.3, 16.4, 16.7, 20.1_

  - [ ]* 13.4 Write unit test for out-of-scope catalog handling
    - **Property 12: Out-of-scope metrics excluded from default enabled set and labeled** — a `scope: "out"` entry is never default-enabled and renders as out-of-scope
    - **Validates: Requirements 4.3, 4.4**
    - File `src/eval/__tests__/catalog.p12.test.ts`; verify `bunx vitest run`

  - [ ] 13.5 Implement `eval/evalExport.ts` Export_Service
    - Reuse the `exec/exportSnapshot.ts` discipline: export selected Instance records (agent, session, instance_index, corpus size, latency, per-stage timings, every recorded ragas + retrieval value, and the Quality_Score) plus the active weight-set id + weights, prompt-config ids, ragas version, and Bedrock model id — sufficient to recompute every exported Quality_Score from exported components; carry the external/industry-methodology caveat in the export
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5, 20.2_

  - [ ]* 13.6 Write unit test for export recomputability and labeling
    - Assert an exported Quality_Score recomputes from exported components + weights; component values exported unchanged; ragas version + Bedrock model id present; external-methodology caveat present
    - File `src/eval/__tests__/evalExport.test.ts`; verify `bunx vitest run`
    - _Requirements: 17.2, 17.3, 17.4, 20.2_

- [ ] 14. Cross-cutting invariants (Area E)
  - [ ]* 14.1 Write the cross-cutting invariant test suite
    - **Property 13: External-methodology labeling is present wherever a metric is shown/exported** — assert the caveat is emitted by every metric-display builder/view and the export; assert ragas-derived and Authoritative_Judge signals render as distinct labeled values without one overriding the other (Req 18.2, 18.3); assert (Python) the new eval routes preserve the loopback-only, no-auth posture (Req 21.2)
    - **Validates: Requirements 18.2, 18.3, 20.1, 20.2, 21.2**
    - Files: `src/eval/__tests__/methodology.p13.test.ts` (verify `bunx vitest run`) and `bakeoff/tests/test_app_eval_posture.py` (verify `.venv/bin/python -m pytest bakeoff/tests/test_app_eval_posture.py -q`)

- [ ] 15. On-demand combinatorial evaluation runs (Area F — additive, latent)
  - [ ] 15.1 Extend the runner and start endpoint for on-demand combinatorial runs
    - Extend `bakeoff/eval/experiment_runner.py` and the `POST /api/eval/runs/start` handler in `bakeoff/app.py` to accept an arbitrary pool of one or more agents (not limited to the ≥3 primitive), an arbitrary subset of enabled ragas + retrieval metrics, arbitrary corpus size(s)/sweep series, and an arbitrary query subset; produce one Instance per element of the cartesian combination of agents × corpus sizes × queries; append each Instance via the same Event_Store + status + stream path as every other run; allow at most one on-demand run active at a time and enqueue further requests in a bounded queue; return a flag (or 409/confirmation requirement) when the combination count exceeds a configurable threshold; communicate over loopback only, no auth
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/ -q`
    - _Requirements: 22.1, 22.2, 22.3, 22.4, 22.5, 22.6, 22.9, 22.10, 22.11, 22.12, 22.13, 22.14_

  - [ ]* 15.2 Write backend test for on-demand combinatorial runs
    - Assert the cartesian product yields exactly |agents|×|sizes|×|queries| Instances; a second on-demand request while one is active is enqueued (bounded) and starts only after the active run completes; an over-threshold request signals the confirmation requirement; on-demand Instances appear via the same status/stream path; no network call in fake mode
    - File: `bakeoff/tests/test_app_eval_ondemand.py`
    - Verify: `.venv/bin/python -m pytest bakeoff/tests/test_app_eval_ondemand.py -q`
    - _Requirements: 22.6, 22.9, 22.10, 22.11, 22.12_

  - [ ] 15.3 Implement the on-demand run control and wire it into the metrics tab
    - Add `bakeoff/ui/src/eval/OnDemandRunControl.tsx` (a reachable, non-default control) letting the user configure an arbitrary agent pool, metric subset, corpus size(s)/sweep, and query subset and launch a run without editing any config file or source; require explicit confirmation when the combination count exceeds the threshold; keep visualization of already-recorded runs as the default surface; wire the control into `views/EvalMetrics.tsx`
    - Verify: `bun run build` in `bakeoff/ui`
    - _Requirements: 22.1, 22.2, 22.3, 22.4, 22.5, 22.7, 22.8, 22.12_

  - [ ]* 15.4 Write frontend test for the on-demand run control
    - Assert the control builds a valid run request from arbitrary selections; the over-threshold confirmation gate blocks launch until confirmed; the default surface remains recorded-run visualization
    - File `src/eval/__tests__/onDemandRunControl.test.ts`; verify `bunx vitest run`
    - _Requirements: 22.7, 22.8, 22.12_

- [ ] 16. Final checkpoint — Ensure all tests and builds pass
  - Run `.venv/bin/python -m pytest bakeoff/tests/ -q`, then `bun run build` and `bunx vitest run` in `bakeoff/ui`. Ensure all pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core implementation tasks are never optional. Per the workflow, optional sub-tasks are not auto-implemented.
- Each task references specific requirement clauses for traceability and carries its own verification command. Offline/fake-backed tests are network-free (Req 1.5).
- Every Correctness Property (P1–P13) from the design is turned into its own property/unit test sub-task placed next to the code it validates, annotated with its property number and the requirement clauses it checks.
- The `EvalInstance` JSON record is the contract between the Python producer (Areas A–B) and the TypeScript visualization app (Area C). Retrieval is read-only and held constant (Req 19); ragas/retrieval/NDCG/composite are external-methodology signals labeled wherever shown or exported (Req 4.6, Req 20, P13).
- bun is the only JS/TS package manager (Req 21.1); the service stays loopback-only and no-auth on synthetic, non-PII data (Req 21.2, 21.3).
- This plan produces design and planning artifacts plus the implementation tasks; it does not itself run the build. Open `tasks.md` and click "Start task" next to an item to begin execution.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0,  "tasks": ["1.1", "2.1"] },
    { "id": 1,  "tasks": ["1.2", "1.3", "2.2", "2.3", "2.5"] },
    { "id": 2,  "tasks": ["1.4", "2.4", "2.6", "2.7"] },
    { "id": 3,  "tasks": ["2.8", "3.1"] },
    { "id": 4,  "tasks": ["3.2", "3.3", "4.1"] },
    { "id": 5,  "tasks": ["3.4", "4.2"] },
    { "id": 6,  "tasks": ["4.3"] },
    { "id": 7,  "tasks": ["6.1", "7.1", "8.6"] },
    { "id": 8,  "tasks": ["6.2", "7.2", "8.1", "8.4", "9.1"] },
    { "id": 9,  "tasks": ["7.3", "7.4", "7.5", "7.6", "7.7", "8.2", "8.3", "8.5", "9.2"] },
    { "id": 10, "tasks": ["9.3", "9.4", "9.5", "10.1", "10.3", "11.1"] },
    { "id": 11, "tasks": ["10.2", "10.4", "11.2", "11.3"] },
    { "id": 12, "tasks": ["11.4"] },
    { "id": 13, "tasks": ["13.1", "13.3", "13.5"] },
    { "id": 14, "tasks": ["13.2", "13.4", "13.6"] },
    { "id": 15, "tasks": ["14.1", "15.1", "15.3"] },
    { "id": 16, "tasks": ["15.2", "15.4"] }
  ]
}
```

# Requirements Document

## Introduction

The v2 prompt optimizer — coverage-ladder rungs + 2-island coevolution +
tournament/migration, per `docs/OPTIMIZER_V2_DESIGN_NOTES.md` — already exists in the
working tree (orchestrator `run_v2`, `island.py`, `rungs.py`, `tournament.py`, the
`optimizer_*` v2 SSE event types, the `OptimizerStore` island/rung/tournament fields,
and the full v2 UI: `QualityOptimizerV2.tsx`, `useOptimizerV2Stream.ts`, and the v2
components). Its offline core is covered by passing tests (`test_orchestrator_v2`,
`test_tournament`, `test_events_v2`). Despite this, v2 has never run end-to-end from
the dashboard, and the v2 surface has never displayed live data.

The cause is structural, not algorithmic. v2 was wired *on top of* v1 instead of
*alongside* it: the single shared route `POST /api/quality/optimize/start` was made to
dispatch to `run_v2` via a `hasattr(orchestrator, "run_v2")` check, and the v1 path
`PerModelOrchestrator.run()` now raises `NotImplementedError`. There is no dedicated
way to launch v2, v1 can no longer run, and the v2 status surface is fed by the shared
`optimizer_snapshot()` whose per-island and per-tournament-round shapes do not match
what the v2 UI types consume.

This feature is **mostly moving code around**: give v2 its own start/status endpoints,
its own launch path that calls the existing `run_v2` with the arguments it already
expects, make the v2 status payload match the shapes the v2 UI already reads, ensure
the live island/tournament events the v2 hook already subscribes to reach the surface,
and restore v1 to a working, separate option. No new algorithm, no new statistics, no
new subsystems, no new abstractions beyond what the wiring requires.

## Glossary

- **V2_Optimizer**: The island-tournament + coverage-ladder optimizer, entered through
  `PerModelOrchestrator.run_v2`.
- **V1_Optimizer**: The original champion/challenger optimizer, entered through
  `PerModelOrchestrator.run` and the v1 CLI subcommands.
- **Backend_App**: The FastAPI process in `bakeoff/app.py` exposing the HTTP + SSE API.
- **V2_Start_Endpoint**: The dedicated v2 launch route `POST /api/quality/optimize/v2/start`.
- **V2_Status_Endpoint**: The dedicated v2 progress route `GET /api/quality/optimize/v2/status`.
- **V1_Optimizer_Endpoints**: The existing v1 routes `POST /api/quality/optimize/start`,
  `GET /api/quality/optimize/status`, and `GET /api/quality/optimize/history`.
- **SSE_Broker**: The existing `bakeoff.app.SSEBroker` fan-out used for all live events.
- **V2_Event**: One of the v2 SSE event types `optimizer_island_step`,
  `optimizer_rung_escalated`, `optimizer_tournament`, `optimizer_migration`, each
  stamped with `model_channel` by `OptimizerEventEmitter`.
- **V2_Tab**: The dashboard's "Opt v2" tab rendering the `QualityOptimizerV2` view.
- **V2_Stream_Hook**: The `useOptimizerV2Stream` hook consuming V2_Events and backfill.
- **Target_Model**: One of the two fixed models `sonnet-4.6-thinking-off` and
  `haiku-4.5` (keys in `config.QUALITY_MODELS`).
- **Durable_Backfill**: Reconstruction of current UI state from the V2_Status_Endpoint
  on mount/reload, independent of the no-replay SSE stream.
- **OpenSearch_Alpha**: The ALPHA OpenSearch Serverless retrieval substrate described
  in `docs/OPENSEARCH_ALPHA.md` (SigV4 service name `aoss`).

## Requirements

### Requirement 1: V2 launches from its own endpoints

**User Story:** As an operator, I want v2 to run on its own API routes, so that
launching v2 never collides with or disables v1.

#### Acceptance Criteria

1. THE Backend_App SHALL expose a V2_Start_Endpoint at `POST /api/quality/optimize/v2/start` and a V2_Status_Endpoint at `GET /api/quality/optimize/v2/status`, distinct in path from the V1_Optimizer_Endpoints.
2. WHEN the V2_Start_Endpoint is called, THE Backend_App SHALL launch the V2_Optimizer via `PerModelOrchestrator.run_v2` and SHALL NOT launch the V1_Optimizer.
3. WHEN the V2_Start_Endpoint launches a run, THE Backend_App SHALL invoke `run_v2` with the model set, the optimizer backend, the event emitter, the optimizer store, and the full multi-turn item universe, matching the argument contract already used by the v2 CLI path in `bakeoff/quality/optimizer/main.py`.
4. WHEN the V2_Start_Endpoint is called while a V2_Optimizer run is already active, THE Backend_App SHALL respond with HTTP 409 and SHALL NOT start a second run.
5. WHERE the requested backend is `live`, WHEN the configured Author and Judge resolve to the same model, THE V2_Start_Endpoint SHALL respond with HTTP 409 before creating the background task.

### Requirement 2: V1 remains a working, separate option

**User Story:** As an operator, I want the original v1 optimizer to still run from its
own path, so that v2 is an addition rather than a replacement.

#### Acceptance Criteria

1. WHEN the V1_Optimizer is invoked through the V1_Optimizer_Endpoints, THE Backend_App SHALL run the v1 champion/challenger loop to completion without raising `NotImplementedError`.
2. WHEN the V1_Optimizer is invoked through its CLI subcommands, THE V1_Optimizer SHALL run the v1 champion/challenger loop to completion.
3. WHEN the V2_Start_Endpoint and V1_Optimizer_Endpoints both exist, THE V1_Optimizer_Endpoints SHALL remain unchanged in request path and response shape.
4. WHILE both optimizers are present, THE Backend_App SHALL keep v1 and v2 lifecycle state separate so that one optimizer's status does not report the other's progress.

### Requirement 3: The V2 Start button runs end-to-end without freezing the dashboard

**User Story:** As an operator, I want to press Start on the V2_Tab and have a run
actually begin while the dashboard stays responsive.

#### Acceptance Criteria

1. WHEN the operator presses Start on the V2_Tab, THE V2_Tab SHALL issue a POST to the V2_Start_Endpoint.
2. WHEN the V2_Start_Endpoint accepts a launch, THE Backend_App SHALL return HTTP 202 with the v2 status snapshot and run the V2_Optimizer as a background task.
3. WHERE the requested backend is `live`, WHEN the V2_Start_Endpoint builds the optimizer backend bundle, THE Backend_App SHALL build it off the asyncio event loop so that concurrent status polls, SSE streams, and page reloads stay responsive.
4. IF the V2_Optimizer background task raises, THEN THE Backend_App SHALL record the failure on the v2 lifecycle state with status `failed` and SHALL NOT crash the Backend_App.

### Requirement 4: The V2 status endpoint returns the shape the V2 UI consumes

**User Story:** As an operator, I want the V2_Status_Endpoint to return per-island and
per-tournament-round data in the shape the V2_Tab already reads, so that the surface
renders instead of silently dropping fields.

#### Acceptance Criteria

1. WHEN the V2_Status_Endpoint is called, THE Backend_App SHALL return the v2 lifecycle (`status`, `error`, `started_at`, `finished_at`) and a per-Target_Model block containing `islands` and `tournaments`, matching the `OptimizerV2Status` type consumed by the V2_Tab.
2. WHEN the V2_Status_Endpoint returns a per-island record, THE record SHALL carry `island_id`, `rung_index`, `champion_score`, `ci_half_width`, `state`, `stance`, and `iterations`, matching the `OptimizerV2IslandProgress` type.
3. WHEN the V2_Status_Endpoint returns a per-tournament-round record, THE record SHALL carry `round`, `island_a` (`champion_score`, `ci_half_width`), `island_b` (`champion_score`, `ci_half_width`), `shared_rung`, and `winner`, matching the `OptimizerV2TournamentRound` type.
4. WHEN no V2_Optimizer run has been recorded, THE V2_Status_Endpoint SHALL return an empty-but-well-formed payload with an idle status and no island or tournament records.
5. WHEN the V2_Status_Endpoint reconstructs per-island and per-tournament-round progress, THE Backend_App SHALL read it from the durable optimizer store rather than from in-memory run state only.

### Requirement 5: The V2 surface displays live progress and survives reload

**User Story:** As an operator, I want to watch islands climb rungs, fight tournaments,
and migrate as it happens, and not lose the view on reload.

#### Acceptance Criteria

1. WHILE a V2_Optimizer run is active, THE V2_Tab SHALL display both islands (id 0 and 1) per Target_Model with each island's current rung, champion score with CI, and stance, without collapsing both islands onto one lane.
2. WHEN a V2_Event is emitted over the SSE_Broker, THE V2_Stream_Hook SHALL consume it on the channel for its Target_Model and update the displayed island, tournament, or migration state.
3. WHEN the V2_Tab is mounted or the page is reloaded, THE V2_Tab SHALL reconstruct current state from the V2_Status_Endpoint via Durable_Backfill so that the surface is not blank.
4. WHEN the V2_Tab polls for Durable_Backfill, THE V2_Tab SHALL request the V2_Status_Endpoint rather than a v1 status route.
5. WHEN no V2_Optimizer run has been recorded, THE V2_Tab SHALL display an idle/empty state and SHALL NOT display v1 records as v2 islands.

### Requirement 6: V2 is reachable as its own tab

**User Story:** As an operator, I want a clear v2 tab so that I can choose v2 as an
alternative to v1.

#### Acceptance Criteria

1. WHEN the operator opens the dashboard, THE V2_Tab SHALL be present and distinct from the v1 optimizer surface.
2. WHEN the operator selects the V2_Tab, THE V2_Tab SHALL drive the V2_Start_Endpoint and V2_Status_Endpoint and SHALL NOT drive the V1_Optimizer_Endpoints.

### Requirement 7: Live v2 retrieves from the OpenSearch ALPHA substrate with local fallback

**User Story:** As an operator, I want live v2 runs to retrieve from the real ALPHA
OpenSearch collection, since retrieval is always-on and the connection details exist
in config.

#### Acceptance Criteria

1. WHERE the requested backend is `live` and the OpenSearch retrieval backend is selected, WHEN a V2_Optimizer run starts, THE Backend_App SHALL build a SigV4 `aoss`-signed client for the configured OpenSearch_Alpha endpoint and use it for retrieval.
2. IF the OpenSearch_Alpha endpoint is unreachable or unconfigured, THEN THE Backend_App SHALL fall back to the local retrieval substrate rather than failing the run.
3. WHILE a live V2_Optimizer run is executing, THE Backend_App SHALL invoke retrieval against the selected substrate so that retrieval activity is observable.

### Requirement 8: End-to-end behavior is verified before hand-off

**User Story:** As the owner, I want proof v2 runs the whole way through before I rely
on it, since it has never once worked from the controls available to me.

#### Acceptance Criteria

1. WHEN the work is presented as complete, THE offline v2 test suites (`test_orchestrator_v2`, `test_tournament`, `test_events_v2`) SHALL pass.
2. WHEN the work is presented as complete, THE full Python test suite SHALL pass with no regression in v1 or the rest of the harness.
3. WHEN the work is presented as complete, THE owner SHALL have evidence that a v2 run started through the V2_Start_Endpoint reached `running`, emitted island events, and surfaced per-island progress on the V2_Status_Endpoint.

# Design — Optimizer v2: Make It Run and Be Visible

## Overview

This is effort #1 of three: make the v2 island-tournament optimizer launch from its
own button and show as running. The v2 algorithm core (`PerModelOrchestrator.run_v2`,
`IslandLoop`, `tournament.py`, `rungs.py`, the v2 store fields, the v2 emitter methods)
already exists and passes offline tests. The work is **separation and wiring**, not new
algorithm code:

1. Give v2 its **own broker instance** and its **own SSE stream endpoint** (no sharing
   the bake-off `/api/stream`).
2. Give v2 its **own start + status endpoints** and its **own launch path** on
   `AppState` that calls `run_v2(...)` with the correct arguments, off the event loop.
3. Point the existing v2 UI (tab, hook, view) at the v2 endpoints + v2 stream so a
   launched run is visibly running.
4. Keep the live OpenSearch ALPHA `aoss` retrieval wiring (already in
   `build_live_backend`) working, with the existing local fallback.

No v1 changes. No bake-off changes. No optimizer-algorithm changes. The polished v2
surface is explicitly deferred to effort #2.

### Why a second broker (not a filter on the shared one)

The existing `SSEBroker` is a single fan-out: every `publish` reaches every subscriber,
regardless of event type. The requirement is a hard constraint that v2 must not reuse
the bake-off stream. The simplest faithful implementation is a **second `SSEBroker`
instance** dedicated to v2 (`AppState.optimizer_v2_broker`), wired into the v2 emitter
and drained by a v2-only stream route. This reuses the existing, tested broker class
verbatim — only an instance and a route are new — and gives true isolation: bake-off
events never appear on the v2 stream and v2 events never appear on the bake-off stream.

Alternative considered: keep one broker and filter by event type at the v2 endpoint.
Rejected because it still couples the two surfaces onto one fan-out (every bake-off
trial event would be enqueued onto the v2 subscriber's queue and discarded), which is
exactly the "sharing" the constraint forbids, and it is not meaningfully less code.

---

## Architecture

```
v2 tab (QualityOptimizerV2.tsx)
  │  Start button ─────────► POST /api/quality/optimize/v2/start
  │  status poll  ─────────► GET  /api/quality/optimize/v2/status
  │  live stream  ─────────► GET  /api/quality/optimize/v2/stream   (EventSource)
  ▼
AppState (bakeoff/app.py)
  ├─ optimizer_v2_broker : SSEBroker          (NEW instance; v2-only fan-out)
  ├─ start_optimizer_v2(...)                  (NEW; builds backend off-loop, launches run_v2)
  ├─ _run_optimizer_v2(...)                   (NEW; task body → orchestrator.run_v2)
  └─ optimizer_v2_snapshot()                  (status: lifecycle + per-island/round backfill)
  ▼
PerModelOrchestrator.run_v2(models, backend, emitter=…, store=…, all_items=…)   (UNCHANGED)
  └─ OptimizerEventEmitter(optimizer_v2_broker)  → island_step / rung_escalated /
                                                    tournament / migration events
```

The v2 lifecycle state on `AppState` is **independent** of both the bake-off run state
and the existing v1 optimizer state, so launching v2 neither reads nor mutates them.

---

## Components and Interfaces

### C1 — `AppState.optimizer_v2_broker` (new broker instance)

A second `SSEBroker()` constructed in `AppState.__init__`, alongside the existing
`self.broker`. v2 optimizer events publish here; the bake-off `self.broker` is
untouched.

### C2 — v2 lifecycle state on `AppState`

Mirror the v1 optimizer lifecycle fields, prefixed for v2 so the two never collide:
`optimizer_v2_status` (`idle`/`running`/`completed`/`failed`), `optimizer_v2_error`,
`optimizer_v2_started_at`, `optimizer_v2_finished_at`, `optimizer_v2_request`, and the
`_optimizer_v2_task` handle. The existing `view_registry` is reused (v2's concurrency
gate already consults it).

### C3 — `AppState.start_optimizer_v2(...)` (new launch path)

Responsibilities, in order:
1. If `optimizer_v2_status == RUNNING`, return `False` (caller → 409).
2. Build the backend bundle **off the event loop** via `asyncio.to_thread`
   (`build_live_backend` does blocking boto3/OpenSearch I/O — this is the freeze fix).
   The `AuthorJudgeConflictError` is raised synchronously inside `build_live_backend`
   before any network call, so it propagates out of the awaited thread and the route
   maps it to a clean 4xx.
3. Flip `optimizer_v2_status = RUNNING` synchronously and record the request.
4. Create the background task `_run_optimizer_v2(...)`.

### C4 — `AppState._run_optimizer_v2(...)` (task body)

Loads the multi-turn dataset off-loop (`asyncio.to_thread(load_multi_turn_items)`),
builds `OptimizerStore`, builds `OptimizerEventEmitter(self.optimizer_v2_broker)`,
constructs `PerModelOrchestrator(..., view_registry=self.view_registry)`, and calls:

```python
await orchestrator.run_v2(models, opt_backend, emitter=emitter, store=store, all_items=items)
```

This is the exact convention the CLI `islands` subcommand and `test_orchestrator_v2`
use. On completion sets `COMPLETED`; on exception records `optimizer_v2_error` +
`FAILED`; in `finally` publishes a final status event. Never raises out of the task.

### C5 — `AppState.optimizer_v2_snapshot()` (status payload)

Returns `{status, request, error, started_at, finished_at, models}` where each model
block carries the per-island and per-tournament-round backfill reconstructed from the
store via `iteration_history_by_island` / `iteration_history_by_tournament_round`
(this reconstruction logic already exists in the current `optimizer_snapshot`; it moves
to the v2 snapshot). Empty-but-well-formed before any v2 run.

Stale-record guard (Req 3.4): island/round backfill only includes records whose
`island_id` / `tournament_round` are non-null, so legacy v1-shaped rows (which have
null island fields) are never surfaced as v2 islands.

### C6 — v2 routes (new, additive)

- `POST /api/quality/optimize/v2/start` — parse optional body (`backend`, `models`,
  `retrieval_backend`, …; defaults: backend `offline`, the two `config.QUALITY_MODELS`),
  call `start_optimizer_v2(...)`, return 202 + snapshot, or 409 (already running /
  author-judge conflict). Validates unknown models/backends with 422.
- `GET /api/quality/optimize/v2/status` — return `optimizer_v2_snapshot()`.
- `GET /api/quality/optimize/v2/stream` — `StreamingResponse` over
  `optimizer_v2_broker.subscribe()`. Opening it also marks the requested model viewable
  via the existing `_view_scoped_stream` pattern if a `model` param is supplied, so the
  concurrency gate keeps working; otherwise a plain subscribe.

The existing v1 routes (`/api/quality/optimize/{start,status,history}`) and the
bake-off `/api/stream` are left exactly as they are.

### C7 — Frontend wiring (point existing v2 UI at v2 endpoints)

- `client.ts`: add `startOptimizeV2(body)` → `POST .../v2/start`, `fetchOptimizeV2Status()`
  → `GET .../v2/status`. (The v1 `startOptimize`/`fetchOptimizeStatus` stay.)
- `useOptimizerV2Stream.ts`: change the `EventSource` target from `/api/stream?model=…`
  to **`/api/quality/optimize/v2/stream?model=…`**, and the backfill poll from
  `/api/quality/optimize/status` to `/api/quality/optimize/v2/status`. Event handling
  (island_step / rung_escalated / tournament / migration) is unchanged.
- `QualityOptimizerV2.tsx`: `onStart` calls `startOptimizeV2`; status poll uses
  `fetchOptimizeV2Status`; lifecycle pill reflects v2 status. Layout otherwise
  unchanged (the redesign is effort #2).

### C8 — OpenSearch ALPHA retrieval (verify, minimal change if needed)

`build_live_backend` already builds the `AWSV4SignerAuth(creds, region, "aoss")` client
from the ALPHA config constants and injects it into `build_retrieval_backend`, with a
local fallback. Effort #1 verifies this path actually retrieves on a live v2 run; only
if a concrete defect is found is a fix made, and it stays confined to the retrieval
wiring.

---

## Data Models

No store schema change — `IterationRecord`/`AuditRecord` already carry
`island_id`/`rung_index`/`tournament_round`. The v2 status JSON and the four v2 SSE
event shapes already exist in `bakeoff/ui/src/api/types.ts` and
`bakeoff/quality/optimizer/events.py`; this effort does not change them, only the
endpoints the UI reads them from.

The v2 status payload shape (returned by `optimizer_v2_snapshot()`):

```
{
  status, request, error, started_at, finished_at,
  models: {
    <model>: {
      islands: [{ island_id, rung_index, champion_score, champion_ci_half_width, state }],
      tournament_rounds: [{ round, scores: [...], shared_rung, winner, migration }]
    }
  }
}
```

The four v2 SSE event types (carried on the v2 stream, unchanged):
`optimizer_island_step`, `optimizer_rung_escalated`, `optimizer_tournament`,
`optimizer_migration`.

The dead `/api/quality/optimize/v2/status` 404 the old code produced is resolved by C6
actually implementing that path (plus the new `/v2/start` and `/v2/stream`).

---

## Correctness Properties

### Property 1: stream isolation
No bake-off `trial_completed`/`judge_*` event appears on the v2 stream, and no v2
island/tournament event appears on the bake-off `/api/stream` (separate broker
instances).
**Validates: Requirements 1.2, 1.4**

### Property 2: lifecycle independence
Launching v2 never reads or mutates the bake-off run state or the v1 optimizer state,
and vice versa.
**Validates: Requirements 1.4, 2.4**

### Property 3: event-loop responsiveness
While a v2 run starts (including live backend build), `/v2/status` and `/v2/stream`
remain responsive (backend build is off-loop).
**Validates: Requirements 2.2, 3.3**

### Property 4: no stale-v1 leakage
The v2 status surfaces only records with non-null `island_id`/`tournament_round`;
legacy v1-shaped rows never appear as v2 islands.
**Validates: Requirements 3.4**

### Property 5: single active run
A second `/v2/start` while running returns 409 and starts nothing new.
**Validates: Requirements 2.4**

---

## Error Handling

- Live backend build failure / author-judge conflict → surfaced as 409/4xx at the
  start route (raised before the task is created).
- Any failure inside the run → caught in `_run_optimizer_v2`, recorded on
  `optimizer_v2_error`, status set `FAILED`, final status event published; the task
  never crashes the app.
- Status endpoint never 500s: a malformed/unreadable store degrades to "no progress
  yet" (mirrors the existing snapshot's defensive try/except).

---

## Testing Strategy

- **Reuse** the green offline suites: `test_orchestrator_v2`, `test_tournament`,
  `test_events_v2` (no change expected).
- **New route test** (`test_app` or a small `test_app_optimizer_v2`): using FastAPI
  `TestClient` with the offline backend — `POST /v2/start` returns 202 and flips status
  to `running`; a second `POST` returns 409; `GET /v2/status` is well-formed before and
  after; the `/v2/stream` route opens. Offline backend keeps it network-free.
- **Full suite** must stay green (no v1 / bake-off regressions).
- **Live end-to-end evidence** (Req 5.2): start a live v2 run through `/v2/start`,
  observe status → `running`, island events on `/v2/stream`, retrieval activity in
  `logs/retrieval.log` (or aoss calls), captured in the hand-off.

---

## Out of Scope (restated)

v2 visual redesign (effort #2); any v1 endpoint/tab/extraction work (effort #3); SSE
stream sharing (forbidden); optimizer-algorithm, statistical-spine, judge, or dataset
changes.

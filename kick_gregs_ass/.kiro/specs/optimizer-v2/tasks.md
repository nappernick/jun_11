# Implementation Plan: Optimizer v2 — Make It Run and Be Visible

## Overview

Effort #1 of three. Goal: click Start on the v2 tab → run kicks off → it shows as
running. This is mostly wiring (separate broker, separate endpoints, point the existing
UI at them); no optimizer-algorithm changes. Verify with
`.venv/bin/python -m pytest bakeoff/tests/ -q` (Python) and `npm run build` in
`bakeoff/ui` (frontend).

## Tasks

- [ ] 1. Add the v2 broker + v2 lifecycle state to `AppState`
  - In `bakeoff/app.py` `AppState.__init__`, add `self.optimizer_v2_broker = SSEBroker()`
    and the v2 lifecycle fields (`optimizer_v2_status` defaulting to `idle`,
    `optimizer_v2_error`, `optimizer_v2_started_at`, `optimizer_v2_finished_at`,
    `optimizer_v2_request`, `_optimizer_v2_task`). Reuse the existing `view_registry`.
  - Leave the existing `self.broker` and v1 optimizer fields untouched.
  - _Requirements: 1.2, 1.4_

- [ ] 2. Add `start_optimizer_v2` + `_run_optimizer_v2` + `optimizer_v2_snapshot` to `AppState`
  - `start_optimizer_v2(...)`: 409-guard on RUNNING; build backend via
    `asyncio.to_thread(build_live_backend|build_offline_backend)` (off-loop freeze fix);
    let `AuthorJudgeConflictError` propagate; flip status RUNNING; record request;
    create the task. Mirror the existing `start_optimizer` shape.
  - `_run_optimizer_v2(...)`: load items off-loop, build `OptimizerStore` +
    `OptimizerEventEmitter(self.optimizer_v2_broker)` + `PerModelOrchestrator`, then
    `await orchestrator.run_v2(models, opt_backend, emitter=emitter, store=store, all_items=items)`;
    set COMPLETED/FAILED; publish final status in `finally`; never raise.
  - `optimizer_v2_snapshot()`: lifecycle + per-island/per-round backfill, including
    only records with non-null `island_id`/`tournament_round` (stale-v1 guard).
  - _Requirements: 2.2, 2.3, 3.3, 3.4_

- [ ] 3. Add the three v2 routes
  - `POST /api/quality/optimize/v2/start` (202 + snapshot; 409 already-running /
    author-judge conflict; 422 unknown model/backend), `GET /api/quality/optimize/v2/status`,
    `GET /api/quality/optimize/v2/stream` (StreamingResponse over `optimizer_v2_broker`,
    using `_view_scoped_stream` when a `model` param is supplied).
  - Do not alter the v1 optimizer routes or the bake-off `/api/stream`.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.4_

- [ ] 4. Backend verification
  - Add a focused route test (offline backend, FastAPI `TestClient`): `/v2/start` → 202
    + status `running`; second `/v2/start` → 409; `/v2/status` well-formed pre/post;
    `/v2/stream` opens.
  - Run `.venv/bin/python -m pytest bakeoff/tests/ -q`; full suite green (no regressions).
  - _Requirements: 5.1, 5.3_

- [ ] 5. Point the v2 frontend at the v2 endpoints
  - `client.ts`: add `startOptimizeV2` (POST `/v2/start`) and `fetchOptimizeV2Status`
    (GET `/v2/status`).
  - `useOptimizerV2Stream.ts`: `EventSource` → `/api/quality/optimize/v2/stream?model=…`;
    backfill poll → `/api/quality/optimize/v2/status`.
  - `QualityOptimizerV2.tsx`: `onStart` → `startOptimizeV2`; status poll →
    `fetchOptimizeV2Status`. No layout redesign (effort #2).
  - Run `npm run build` in `bakeoff/ui`; tsc + vite green.
  - _Requirements: 2.1, 3.1, 3.2, 3.3_

- [ ] 6. Live end-to-end verification (the success bar)
  - Boot the stack; on the v2 tab pick a backend and press Start. Confirm: the POST
    reaches `/v2/start` (log), status → `running`, island events arrive on `/v2/stream`,
    the tab shows it running, and (live) retrieval activity is observable.
  - Capture the evidence in the hand-off.
  - _Requirements: 2.1, 3.1, 3.2, 4.1, 4.3, 5.2_

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2"] },
    { "wave": 3, "tasks": ["3"] },
    { "wave": 4, "tasks": ["4"] },
    { "wave": 5, "tasks": ["5"] },
    { "wave": 6, "tasks": ["6"] }
  ]
}
```

```
1 ──► 2 ──► 3 ──► 4
                  │
                  ▼
            5 ──► 6
```

- Task 1 (broker + state) is the foundation; Task 2 (launch path) depends on it.
- Task 3 (routes) depends on Task 2; Task 4 (backend test) depends on Task 3.
- Task 5 (frontend wiring) depends on the routes existing (Task 3) and is best done
  after the backend is verified (Task 4).
- Task 6 (live end-to-end) is last and depends on both backend (4) and frontend (5).

## Notes

- Hard constraints: v2 gets its OWN broker + stream (no sharing `/api/stream`); no v1
  endpoint/tab/extraction work (effort #3); no v2 visual redesign (effort #2); no
  optimizer-algorithm / judge / dataset changes.
- The off-event-loop backend build (Task 2) is the fix for the "Start freezes the
  dashboard" failure observed earlier.
- The v2 algorithm core and its offline suites (`test_orchestrator_v2`,
  `test_tournament`, `test_events_v2`) already pass and are reused unchanged.

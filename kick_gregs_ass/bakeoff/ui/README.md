# GBBO Dashboard (frontend)

React + TypeScript (strict) + ECharts, built with Vite. This is the **client
half** of the harness (design AD-4): the Python backend (`bakeoff/app.py`) serves
a JSON + SSE API; this app consumes it with type-checked payload contracts so a
backend field rename is a compile error, not a silently-wrong chart.

## Layout

- `src/api/types.ts` — typed mirror of the Python payloads (the contract seam).
- `src/api/client.ts` — typed fetch wrappers for `/api/*`, `/exec/*`, `/healthz`.
- `src/api/useEventStream.ts` — SSE subscription to `/api/stream`.
- `src/api/useSnapshot.ts` — polls `/api/models` for run status + per-model progress.
- `src/components/` — presentational pieces (model table, KPIs, latency boxplot, feed, ECharts wrapper).
- `src/views/LiveMonitor.tsx` — the live monitoring view (Task 13).
- The executive visualization view (Task 14) is **deferred** pending the
  LLM-as-judge rubric rework; quality dimensions are rendered from whatever the
  API reports (never hard-coded), so the rework changes data, not components.

## Develop

```bash
cd bakeoff/ui
npm install
npm run dev        # Vite dev server on :5173, proxies API to the backend :8200
```

Run the backend separately (loopback only):

```bash
python -m uvicorn "bakeoff.app:create_app" --factory --host 127.0.0.1 --port 8200
```

Override the proxy target with `BAKEOFF_BACKEND` if the backend runs elsewhere.

## Build / typecheck

```bash
npm run typecheck  # tsc -b, strict; no emit
npm run build      # tsc -b && vite build -> dist/ (served by the backend at /)
```

`npm run build` emits to `bakeoff/ui/dist/`, exactly where the backend's
`DEFAULT_DIST_DIR` looks; once built, the backend serves the SPA at `/`.

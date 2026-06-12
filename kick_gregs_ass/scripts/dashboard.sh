#!/usr/bin/env bash
# =============================================================================
# GBBO — Script 2 of 3:  DASHBOARD + RUN API  (the front end you watch)
# -----------------------------------------------------------------------------
# Builds the TypeScript SPA fresh, then serves the dashboard + JSON/SSE + the
# Start-Run API on http://127.0.0.1:8200/. Run this in its OWN terminal window.
# Foreground; streams to this terminal AND logs/dashboard.log.
#
#   bash scripts/dashboard.sh
#
# Start this SECOND (after scripts/retrieval.sh is healthy on :8080). Then open
# http://127.0.0.1:8200/ , go to the Bake-Off tab, and hit Start Run — or POST
# /api/run/start. Ctrl-C to stop.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export PYTHONUNBUFFERED=1   # live, line-by-line stream (no block-buffering)
exec > >(tee -a logs/dashboard.log) 2>&1

echo "============================================================"
echo "[dashboard] $(date '+%Y-%m-%d %H:%M:%S')  starting dashboard + run API"
echo "============================================================"
export AWS_REGION=us-west-2

# --- eval dashboard data source: the REAL-data backfill store -----------------
# Eval 3D / Eval 2D read EvalInstance records from this file. It is produced by
#   PYTHONPATH=. .venv/bin/python -m bakeoff.eval.real_backfill
# (real bake-off outcomes + judge scores; models under test only) and kept
# separate from the synthetic producer's default store for clear lineage.
export GBBO_EVAL_EVENTS_PATH="data/bakeoff/eval_real_instances.jsonl"

# --- warn (don't block) if retrieval substrate isn't up yet -------------------
if curl -sf http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
  echo "[dashboard] retrieval substrate healthy on :8080 (good)"
else
  echo "[dashboard] WARN: retrieval :8080 not healthy yet — start scripts/retrieval.sh first,"
  echo "[dashboard]       or trials will error until it is up. Continuing to serve the UI."
fi

# --- build the SPA fresh so the served bundle is current ----------------------
if command -v bun >/dev/null 2>&1; then
  echo "[dashboard] building the SPA (bun run build in bakeoff/ui)"
  ( cd bakeoff/ui && bun run build )
  echo "[dashboard] SPA build complete -> bakeoff/ui/dist"
else
  echo "[dashboard] WARN: bun not on PATH; serving whatever is already in bakeoff/ui/dist"
fi

# --- serve (foreground) -------------------------------------------------------
echo "[dashboard] serving on http://127.0.0.1:8200/  (open this, use the Bake-Off tab)"
echo "[dashboard] foreground — Ctrl-C to stop. Log: logs/dashboard.log"
exec .venv/bin/python -m bakeoff.app

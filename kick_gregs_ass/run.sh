#!/usr/bin/env bash
# =============================================================================
# GBBO — ONE-COMMAND BOOT  (retrieval substrate + dashboard/optimizer UI)
# -----------------------------------------------------------------------------
# Boots the whole local stack with a single command and keeps it up in the
# foreground:
#
#   1. RAG retrieval substrate  -> http://127.0.0.1:8080   (scripts/retrieval.sh)
#   2. Dashboard + run/optimizer API + SPA -> http://127.0.0.1:8200
#                                                            (scripts/dashboard.sh)
#
# Each service is started as a background child (logging to logs/*.log via the
# child script's own tee); this orchestrator waits for each to report healthy,
# prints the URLs, then blocks. Ctrl-C tears down BOTH services.
#
#   bash run.sh                 # boot retrieval + dashboard, wait, Ctrl-C stops all
#   SKIP_RETRIEVAL=1 bash run.sh   # dashboard only (e.g. Docker unavailable, or the
#                                  #   optimizer uses the OpenSearch backend)
#   START_CREDS=1 bash run.sh   # also start the overnight Bedrock creds refresher
#
# This script does NOT start any bake-off or optimizer run — open the dashboard
# and kick those off from the UI (or POST the start endpoints) yourself.
# =============================================================================
# NOT -e: a slow/failed retrieval boot must not abort the dashboard boot; the
# health waits below are warn-not-fail so the UI always comes up.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs
export AWS_REGION="${AWS_REGION:-us-west-2}"
export PYTHONUNBUFFERED=1

SKIP_RETRIEVAL="${SKIP_RETRIEVAL:-0}"
START_CREDS="${START_CREDS:-0}"
RETRIEVAL_HEALTH_TIMEOUT_S="${RETRIEVAL_HEALTH_TIMEOUT_S:-300}"  # ingest can be slow
DASHBOARD_HEALTH_TIMEOUT_S="${DASHBOARD_HEALTH_TIMEOUT_S:-180}"  # SPA build + serve

# --- child PIDs + teardown ----------------------------------------------------
pids=()
cleanup() {
  echo
  echo "==> shutting down (stopping ${#pids[@]} service(s))"
  for p in "${pids[@]}"; do
    kill "$p" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  echo "==> all services stopped."
}
trap cleanup EXIT INT TERM

# --- wait for an http /healthz, warn (don't fail) on timeout ------------------
wait_for_health() {
  local name="$1" url="$2" timeout_s="$3" waited=0
  echo "==> waiting for $name health at $url (max ${timeout_s}s)"
  while ! curl -sf "$url" >/dev/null 2>&1; do
    sleep 1
    waited=$((waited + 1))
    if [ "$waited" -ge "$timeout_s" ]; then
      echo "==> WARN: $name not healthy after ${timeout_s}s — continuing anyway."
      echo "         check its log for progress (corpus ingest / SPA build can be slow)."
      return 1
    fi
  done
  echo "==> $name healthy ($url)"
  return 0
}

echo "============================================================"
echo "[boot] $(date '+%Y-%m-%d %H:%M:%S')  booting GBBO local stack"
echo "[boot] retrieval=$([ "$SKIP_RETRIEVAL" = 1 ] && echo skip || echo on)  creds=$([ "$START_CREDS" = 1 ] && echo on || echo off)"
echo "============================================================"

# --- optional sidecar: overnight Bedrock credential refresher -----------------
if [ "$START_CREDS" = 1 ]; then
  echo "==> starting credential refresher (logs/creds.log)"
  bash scripts/creds.sh >/dev/null 2>&1 &
  pids+=($!)
fi

# --- 1) retrieval substrate (:8080) ------------------------------------------
if [ "$SKIP_RETRIEVAL" = 1 ]; then
  echo "==> SKIP_RETRIEVAL=1 — not starting the retrieval substrate."
else
  echo "==> starting retrieval substrate (:8080) — logs/retrieval.log"
  bash scripts/retrieval.sh >/dev/null 2>&1 &
  pids+=($!)
  wait_for_health "retrieval" "http://127.0.0.1:8080/healthz" "$RETRIEVAL_HEALTH_TIMEOUT_S" || true
fi

# --- 2) dashboard + run/optimizer API + SPA (:8200) --------------------------
echo "==> starting dashboard + optimizer API (:8200) — logs/dashboard.log"
bash scripts/dashboard.sh >/dev/null 2>&1 &
pids+=($!)
wait_for_health "dashboard" "http://127.0.0.1:8200/healthz" "$DASHBOARD_HEALTH_TIMEOUT_S" || true

# --- up -----------------------------------------------------------------------
echo
echo "============================================================"
echo "[boot] stack up:"
[ "$SKIP_RETRIEVAL" = 1 ] || echo "         retrieval : http://127.0.0.1:8080   (POST /retrieve, GET /healthz)"
echo "         dashboard : http://127.0.0.1:8200/  (open this; Quality tab -> Prompt Optimizer)"
echo "[boot] logs: logs/retrieval.log  logs/dashboard.log$([ "$START_CREDS" = 1 ] && echo "  logs/creds.log")"
echo "[boot] kick off the bake-off / optimizer from the UI. Ctrl-C here stops everything."
echo "============================================================"

# Block in the foreground; the EXIT/INT/TERM trap tears the children down.
wait

#!/usr/bin/env bash
# =============================================================================
# gbbo.sh — bring the WHOLE GBBO model-bakeoff system up with one command.
#
# Usage:
#   ./gbbo.sh
#
# What it does (in order; each step echoes "==>" first and FAILs loud — nonzero
# exit + a clear message — if it cannot be verified):
#   1. export AWS_REGION + best-effort Bedrock credential refresh (ada).
#   2. Docker up + Qdrant container (qdrant-faq) on :6333, waited until ready.
#   3. Python venv (.venv) + deps from requirements.txt.
#   4. Retrieval backend on :8080 (ingest + uvicorn src.server:app), polled
#      healthy. Started only if not already healthy.
#   5. CRITICAL GATE: a real /retrieve round-trip must return 200 with
#      "fragments". A 500 (stale Bedrock creds) triggers a creds refresh + a
#      backend restart + one retry, then FAILs loud if it still does not work.
#   6. Bakeoff dashboard on :8200 (python -m bakeoff.app), polled healthy.
#      Started only if not already healthy.
#   7. Print a summary (URLs + log paths) and exit 0, leaving servers running.
#
# Idempotent: safe to re-run. Already-healthy services are reused, not
# double-started. Uses only curl, docker, the venv binaries, and standard bash.
# Does NOT block in the foreground — the servers are backgrounded.
# =============================================================================
set -euo pipefail

# Run from the repo root (this script's directory) so all relative paths
# (.venv, requirements.txt, data/, qdrant_storage volume) resolve like run.sh.
cd "$(dirname "$0")"

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
QDRANT_NAME="qdrant-faq"
QDRANT_READY_URL="http://localhost:6333/readyz"

RETRIEVAL_URL="http://127.0.0.1:8080"
RETRIEVAL_HEALTH_URL="${RETRIEVAL_URL}/healthz"
RETRIEVAL_RETRIEVE_URL="${RETRIEVAL_URL}/retrieve"
RETRIEVAL_LOG="/tmp/gbbo-retrieval.log"

DASHBOARD_URL="http://127.0.0.1:8200"
DASHBOARD_HEALTH_URL="${DASHBOARD_URL}/healthz"
DASHBOARD_LOG="/tmp/gbbo-dashboard.log"

ADA_LINE="ada credentials update --account=948580600005 --role=IibsAdminAccess-DO-NOT-DELETE --provider=conduit --profile alpha --once"

RETRIEVE_QUERY='{"query":"how do I get reimbursed for a flight?"}'

# PID of a retrieval backend WE launched (empty when we reused an existing one).
RETRIEVAL_PID=""

# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------
step() { echo; echo "==> $*"; }
info() { echo "    $*"; }
warn() { echo "    WARN: $*" >&2; }
fail() { echo; echo "FAIL: $*" >&2; exit 1; }

# ----------------------------------------------------------------------------
# Generic readiness poller. $1=url  $2=max-seconds.  Returns nonzero on timeout.
# ----------------------------------------------------------------------------
poll_url() {
  local url="$1" max="$2" waited=0
  while ! curl -sf -m 5 "$url" >/dev/null 2>&1; do
    sleep 1
    waited=$((waited + 1))
    if [ "$waited" -ge "$max" ]; then
      return 1
    fi
  done
  return 0
}

# ----------------------------------------------------------------------------
# Best-effort Bedrock credential refresh (never fatal: creds may already be ok).
# ----------------------------------------------------------------------------
refresh_creds() {
  info "$ADA_LINE"
  if command -v ada >/dev/null 2>&1; then
    if ada credentials update --account=948580600005 \
        --role=IibsAdminAccess-DO-NOT-DELETE --provider=conduit --profile alpha --once; then
      info "Bedrock credentials refreshed."
    else
      warn "'ada credentials update' failed (creds may already be valid); continuing."
    fi
  else
    warn "'ada' not on PATH; skipping credential refresh (creds may already be valid)."
  fi
}

# ----------------------------------------------------------------------------
# Retrieval backend lifecycle
# ----------------------------------------------------------------------------
backend_healthy() {
  curl -sf -m 5 "$RETRIEVAL_HEALTH_URL" >/dev/null 2>&1
}

# Ingest the corpus, launch uvicorn in the background (logging to RETRIEVAL_LOG),
# and poll /healthz (max ~60s). FAILs loud if any part cannot be verified.
# This is "step 4's launch sequence", reused verbatim by the step-5 restart.
start_retrieval_backend() {
  info "Ingesting corpus (Embed v4 + BM25 -> Qdrant) via .venv/bin/python -m src.ingest"
  if ! .venv/bin/python -m src.ingest data/faq_corpus.csv; then
    fail "Corpus ingestion failed. If this is a Bedrock auth error, refresh creds with:
      $ADA_LINE
    then re-run ./gbbo.sh"
  fi

  info "Launching uvicorn src.server:app on 127.0.0.1:8080 (log: $RETRIEVAL_LOG)"
  nohup .venv/bin/uvicorn src.server:app --host 127.0.0.1 --port 8080 \
    > "$RETRIEVAL_LOG" 2>&1 &
  RETRIEVAL_PID=$!

  info "Waiting for retrieval /healthz (max 60s)..."
  if ! poll_url "$RETRIEVAL_HEALTH_URL" 60; then
    fail "Retrieval backend never became healthy on :8080. See log: $RETRIEVAL_LOG"
  fi
  info "Retrieval backend healthy on :8080."
}

# Kill any retrieval backend (ours and/or any stray uvicorn src.server:app),
# then relaunch it exactly as step 4 does.
restart_retrieval_backend() {
  info "Restarting retrieval backend so it picks up fresh credentials..."
  if [ -n "$RETRIEVAL_PID" ]; then
    kill "$RETRIEVAL_PID" 2>/dev/null || true
  fi
  pkill -f 'uvicorn src.server:app' 2>/dev/null || true
  sleep 2
  RETRIEVAL_PID=""
  start_retrieval_backend
}

# Perform the /retrieve round-trip. Sets globals RT_CODE (HTTP status) and
# RT_BODY (response body). Non-fatal here so the caller can branch on 500.
RT_CODE=""
RT_BODY=""
retrieve_roundtrip() {
  local body_file
  body_file="$(mktemp -t gbbo-retrieve.XXXXXX)"
  RT_CODE="$(curl -s -m 45 -o "$body_file" -w '%{http_code}' \
    "$RETRIEVAL_RETRIEVE_URL" \
    -H 'content-type: application/json' \
    -d "$RETRIEVE_QUERY" 2>/dev/null || true)"
  RT_BODY="$(cat "$body_file" 2>/dev/null || true)"
  rm -f "$body_file"
}

# True iff the last round-trip returned HTTP 200 with a body containing "fragments".
retrieve_ok() {
  [ "$RT_CODE" = "200" ] && printf '%s' "$RT_BODY" | grep -q '"fragments"'
}

# =============================================================================
# STEP 1 — region + best-effort Bedrock credential refresh
# =============================================================================
step "Step 1/7: AWS region + Bedrock credential refresh (best-effort)"
export AWS_REGION=us-west-2
info "AWS_REGION=$AWS_REGION"
refresh_creds

# =============================================================================
# STEP 2 — Docker + Qdrant
# =============================================================================
step "Step 2/7: Docker + local Qdrant container ($QDRANT_NAME) on :6333"
if ! command -v docker >/dev/null 2>&1; then
  fail "'docker' is not on PATH. Install/launch Docker and re-run."
fi
if ! docker info >/dev/null 2>&1; then
  fail "Docker daemon is not running (docker info failed). Start Docker Desktop / the daemon and re-run."
fi
info "Docker daemon is up."

# Start (reuse) or create the Qdrant container, exactly as run.sh does.
docker start "$QDRANT_NAME" >/dev/null 2>&1 || \
  docker run -d --name "$QDRANT_NAME" -p 6333:6333 \
    -v "$(pwd)/qdrant_storage:/qdrant/storage" qdrant/qdrant:latest >/dev/null

info "Waiting for Qdrant /readyz (max 30s)..."
if ! poll_url "$QDRANT_READY_URL" 30; then
  fail "Qdrant never became ready on :6333. Check: docker logs $QDRANT_NAME"
fi
info "Qdrant is ready on :6333."

# =============================================================================
# STEP 3 — Python venv + deps
# =============================================================================
step "Step 3/7: Python venv (.venv) + dependencies"
if [ ! -d ".venv" ]; then
  info "Creating .venv (python3 -m venv .venv)"
  if ! python3 -m venv .venv; then
    fail "Could not create the .venv virtual environment (python3 -m venv failed)."
  fi
  info "Upgrading pip in the fresh venv"
  .venv/bin/pip install -q --upgrade pip || warn "pip self-upgrade failed; continuing with existing pip."
else
  info ".venv already exists; ensuring deps are current."
fi

info "Installing requirements (.venv/bin/pip install -q -r requirements.txt)"
if ! .venv/bin/pip install -q -r requirements.txt; then
  fail "Dependency install failed (.venv/bin/pip install -r requirements.txt)."
fi
info "Dependencies installed."

# =============================================================================
# STEP 4 — Retrieval backend on :8080 (start only if not already healthy)
# =============================================================================
step "Step 4/7: Retrieval backend on :8080"
if backend_healthy; then
  info "Retrieval backend already healthy on :8080; reusing it."
else
  info "Retrieval backend not healthy yet; starting it."
  start_retrieval_backend
fi
info "Retrieval log: $RETRIEVAL_LOG"

# =============================================================================
# STEP 5 — CRITICAL health gate: a real /retrieve round-trip must work
# =============================================================================
# A healthy /healthz does NOT prove /retrieve works: /retrieve calls Bedrock and
# 500s on expired creds. We do a real round-trip, and if it 500s we refresh
# creds, restart the backend, and retry ONCE before failing loud.
step "Step 5/7: CRITICAL gate — real /retrieve round-trip must return fragments"
retrieve_roundtrip
if retrieve_ok; then
  info "/retrieve returned 200 with fragments. Retrieval path is live."
else
  if [ "$RT_CODE" = "500" ]; then
    warn "/retrieve returned HTTP 500 (typically stale Bedrock creds). Refreshing creds and restarting backend."
    refresh_creds
    restart_retrieval_backend
    info "Retrying the /retrieve round-trip once..."
    retrieve_roundtrip
    if retrieve_ok; then
      info "/retrieve returned 200 with fragments after the refresh+restart. Retrieval path is live."
    else
      fail "/retrieve still not returning fragments after a creds refresh + backend restart (HTTP=$RT_CODE).
    Check the retrieval log: $RETRIEVAL_LOG
    Body was: $RT_BODY"
    fi
  else
    fail "/retrieve did not return fragments (HTTP=$RT_CODE).
    Check the retrieval log: $RETRIEVAL_LOG
    Body was: $RT_BODY"
  fi
fi

# =============================================================================
# STEP 6 — Bakeoff dashboard on :8200 (start only if not already healthy)
# =============================================================================
step "Step 6/7: Bakeoff dashboard on :8200"
if curl -sf -m 5 "$DASHBOARD_HEALTH_URL" >/dev/null 2>&1; then
  info "Dashboard already healthy on :8200; reusing it."
else
  info "Launching dashboard (.venv/bin/python -m bakeoff.app; log: $DASHBOARD_LOG)"
  nohup .venv/bin/python -m bakeoff.app > "$DASHBOARD_LOG" 2>&1 &
  info "Waiting for dashboard /healthz (max 30s)..."
  if ! poll_url "$DASHBOARD_HEALTH_URL" 30; then
    fail "Bakeoff dashboard never became healthy on :8200. See log: $DASHBOARD_LOG"
  fi
  info "Dashboard healthy on :8200."
fi
info "Dashboard log: $DASHBOARD_LOG"

# =============================================================================
# STEP 7 — success summary
# =============================================================================
step "Step 7/7: All systems up"
echo
echo "  ============================================================"
echo "  GBBO model-bakeoff is UP"
echo "  ------------------------------------------------------------"
echo "  Retrieval backend : $RETRIEVAL_URL"
echo "  Dashboard         : ${DASHBOARD_URL}/"
echo "  Retrieval log     : $RETRIEVAL_LOG"
echo "  Dashboard log     : $DASHBOARD_LOG"
echo "  ------------------------------------------------------------"
echo "  Open ${DASHBOARD_URL}/ and use the Start Run button to kick off the bake-off."
echo "  ============================================================"
echo

exit 0

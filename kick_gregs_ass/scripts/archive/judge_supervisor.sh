#!/usr/bin/env bash
# =============================================================================
# GBBO — Phase-2 JUDGE SUPERVISOR (unattended, credential-expiry resilient)
# -----------------------------------------------------------------------------
# Runs the deferred Opus judge over the converse outcomes and KEEPS it finished.
# The judge is resumable (it skips trial_ids already in judge_scores.jsonl) and
# every invocation builds a FRESH boto3 client, so if a process dies mid-run on
# an ExpiredTokenException this supervisor simply re-invokes it — the new process
# picks up the refreshed on-disk credentials (kept alive by scripts/creds.sh) and
# resumes only the not-yet-judged trials. It exits cleanly once a full pass
# judges 0 new and skips everything (i.e. nothing left to do).
#
#   bash scripts/judge_supervisor.sh
#   ITEMS_PER_MODEL=300 bash scripts/judge_supervisor.sh   # override sample size
#
# Foreground; tees to logs/judge_supervisor.log. Launch as a background process
# and leave it alone. Independent of the dashboard's in-process judge.
# =============================================================================
set -uo pipefail   # NOT -e: a failed judge process must not kill the supervisor
cd "$(dirname "$0")/.."
mkdir -p logs
export PYTHONUNBUFFERED=1
export AWS_REGION=us-west-2

PY=.venv/bin/python
JUDGE=data/bakeoff/judge_scores.jsonl
ITEMS_PER_MODEL="${ITEMS_PER_MODEL:-300}"   # 300/model x 3 converse models = 900
MAX_ATTEMPTS="${MAX_ATTEMPTS:-30}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-20}"

log() { echo "[judge-sup $(date '+%H:%M:%S')] $*"; }

log "starting; target ${ITEMS_PER_MODEL} items/model. judge store: $JUDGE"

prev=-1
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  before=$( [ -f "$JUDGE" ] && wc -l < "$JUDGE" | tr -d ' ' || echo 0 )
  log "attempt $attempt — judge store has $before verdicts; invoking judge..."

  # Resumable Phase-2 judge over the (now converse-only) outcomes.
  $PY -m bakeoff.judge_phase2 --items-per-model "$ITEMS_PER_MODEL"
  rc=$?

  after=$( [ -f "$JUDGE" ] && wc -l < "$JUDGE" | tr -d ' ' || echo 0 )
  log "attempt $attempt finished rc=$rc; judge store now $after verdicts (was $before)."

  # Done when a full pass added nothing new AND the process exited cleanly:
  # everything sampled is already judged.
  if [ "$rc" = "0" ] && [ "$after" = "$before" ] && [ "$after" -gt 0 ]; then
    log "DONE — no new verdicts on a clean pass; $after total. Judging complete."
    exit 0
  fi

  # Safety against an infinite no-progress loop that also never errors.
  if [ "$after" = "$prev" ] && [ "$rc" != "0" ]; then
    log "WARN: no progress across two failed attempts; sleeping longer."
  fi
  prev="$before"

  sleep "$SLEEP_BETWEEN"
done

log "reached MAX_ATTEMPTS=$MAX_ATTEMPTS; judge store has $( [ -f "$JUDGE" ] && wc -l < "$JUDGE" | tr -d ' ' || echo 0 ) verdicts."

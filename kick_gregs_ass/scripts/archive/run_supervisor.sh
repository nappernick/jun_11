#!/usr/bin/env bash
# =============================================================================
# GBBO — Sidecar:  RUN SUPERVISOR  (keeps the 23,400 run progressing overnight)
# -----------------------------------------------------------------------------
# The harness auto-pauses if the downstream error rate spikes (e.g. a credential
# blip). That is correct + protective, but unattended we want the run to RESUME
# on its own once the blip clears. This supervisor polls the harness and, when it
# sees the run auto-paused (or idle with trials still missing), re-kicks it via
# POST /api/run/start. Resume is safe + idempotent: it diffs the append-only
# outcomes log and runs ONLY the missing/errored trials, so already-good outcomes
# are never re-run, and it re-arms the Phase-2 auto-chain on completion.
#
#   bash scripts/run_supervisor.sh
#   POLL_EVERY_S=60 REPS=3 bash scripts/run_supervisor.sh
#
# Run in its OWN terminal. Foreground; tees to logs/supervisor.log. Ctrl-C stops.
# Start AFTER the dashboard (:8200) is up. It only resumes; it never aborts.
# =============================================================================
set -uo pipefail   # NOT -e: a transient poll error must never kill the loop
cd "$(dirname "$0")/.."
mkdir -p logs
export PYTHONUNBUFFERED=1
exec > >(tee -a logs/supervisor.log) 2>&1

POLL_EVERY_S="${POLL_EVERY_S:-60}"
REPS="${REPS:-3}"
BASE="${BASE:-http://127.0.0.1:8200}"
# Grace period after a (re)start before the supervisor is allowed to act again,
# so it doesn't hammer start while the run is spinning up.
RESTART_GRACE_S="${RESTART_GRACE_S:-45}"

echo "============================================================"
echo "[supervisor] $(date '+%Y-%m-%d %H:%M:%S')  run supervisor starting"
echo "[supervisor] base=$BASE reps=$REPS poll=${POLL_EVERY_S}s"
echo "============================================================"

# One self-contained Python probe+resume step (uses urllib so loopback is never
# routed through a proxy, and so we parse JSON robustly). Prints a status line;
# exits 10 to signal "I just (re)started the run" so the shell can apply grace.
probe_and_resume() {
  .venv/bin/python - "$BASE" "$REPS" <<'PY'
import json, sys, urllib.request

base, reps = sys.argv[1], int(sys.argv[2])

def get(path):
    with urllib.request.urlopen(base + path, timeout=15) as r:
        return json.loads(r.read())

def start(reps):
    body = json.dumps({"reps": reps}).encode()
    req = urllib.request.Request(
        base + "/api/run/start", data=body,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status

try:
    snap = get("/api/models")
except Exception as e:
    print(f"poll-error: {e}")
    sys.exit(0)  # transient; try again next cycle

status = snap.get("status")
totals = snap.get("totals", {})
done = totals.get("done", 0)
errored = totals.get("errored", 0)
auto = snap.get("auto_paused", False)

# Resume conditions: the run stalled in a way that needs a fresh kick.
#  - paused + auto_paused: the error-rate gate tripped (the overnight failure mode)
#  - idle: no controller (e.g. the app restarted) -> (re)start to resume from log
# A MANUAL pause (paused but not auto_paused) is left alone: a human paused it.
needs_resume = (status == "paused" and auto) or status == "idle"

print(f"status={status} auto_paused={auto} done={done} errored={errored}")

if needs_resume:
    try:
        code = start(reps)
        print(f"  -> resume POST /api/run/start reps={reps} -> {code}")
        sys.exit(10)  # signal: just (re)started
    except Exception as e:
        print(f"  -> resume failed: {e}")
        sys.exit(0)
PY
}

while true; do
  probe_and_resume
  rc=$?
  if [ "$rc" -eq 10 ]; then
    echo "[supervisor] $(date '+%H:%M:%S')  resumed; grace ${RESTART_GRACE_S}s"
    sleep "$RESTART_GRACE_S"
  fi
  sleep "$POLL_EVERY_S"
done

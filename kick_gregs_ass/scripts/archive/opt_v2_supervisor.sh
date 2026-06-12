#!/bin/bash
# opt_v2_supervisor.sh — OWN + PROTECT the Optimize-V2 run.
#
# It defends against every reasonably-expected failure of the bakeoff server +
# its in-process optimizer task:
#   1. terminal / session close (SIGHUP)   -> server is launched DETACHED (setsid)
#   2. inexplicable death (no record)       -> server stdout+stderr -> a real logfile
#   3. crash / OOM (process gone)           -> health-check; relaunch on death
#   4. hang (loop/thread-pool starvation)   -> health-check TIMES OUT -> kill + relaunch
#   5. optimizer task stops/fails/lost      -> re-POST /start; orchestrator RESUMES
#                                              from the durable store (skips done work)
#   6. restart storm (re-dies instantly)    -> backoff + LOUD log instead of tight loop
# The supervisor itself is meant to be launched detached (setsid) so it outlives the
# shell that started it. Everything is logged so the NEXT failure is explicable.
set -u
DIR="/Users/nmatnich/Work/kick_gregs_ass"
PY="$DIR/.venv/bin/python"
URL="http://127.0.0.1:8200"
BACKEND="${OPT_BACKEND:-live}"          # OPT_BACKEND=offline ./opt_v2_supervisor.sh to use cache
STATUS_PATH="/api/quality/optimize/v2/status"
START_PATH="/api/quality/optimize/v2/start"
SLOG="$DIR/data/opt_v2_server.log"
LOG="$DIR/data/opt_v2_supervisor.log"
ITERS="$DIR/data/bakeoff/quality_opt_iterations.jsonl"
mkdir -p "$DIR/data/bakeoff"
log(){ echo "$(date '+%F %T') | $*" >> "$LOG"; }

code(){ curl -s -m 8 -o /dev/null -w '%{http_code}' "$URL$STATUS_PATH" 2>/dev/null; }
healthy(){ [ "$(code)" = "200" ]; }
status(){ curl -s -m 8 "$URL$STATUS_PATH" 2>/dev/null; }
iters(){ [ -f "$ITERS" ] && wc -l < "$ITERS" 2>/dev/null | tr -d ' ' || echo 0; }

restart_server(){
  log "RESTART server: kill any existing + relaunch DETACHED (logged -> $SLOG)"
  pkill -f 'bakeoff\.app' 2>/dev/null; sleep 2
  pkill -9 -f 'bakeoff\.app' 2>/dev/null; sleep 1
  ( cd "$DIR" && nohup "$PY" -m bakeoff.app >> "$SLOG" 2>&1 & )
  for _ in $(seq 1 90); do healthy && { log "server HEALTHY"; return 0; }; sleep 2; done
  log "ERROR: server did NOT become healthy within 180s (see $SLOG tail)"
  tail -n 5 "$SLOG" 2>/dev/null | sed 's/^/  server.log| /' >> "$LOG"
  return 1
}

kick_optimizer(){
  local s; s="$(status)"
  if echo "$s" | grep -q '"status" *: *"running"'; then return 0; fi
  log "optimizer not running (status=$(echo "$s" | head -c 100)); POST /start backend=$BACKEND (RESUMES from store)"
  curl -s -m 20 -X POST "$URL$START_PATH" -H 'Content-Type: application/json' \
       -d "{\"backend\":\"$BACKEND\"}" 2>/dev/null | head -c 200 | sed 's/^/  start-resp| /' >> "$LOG"
  echo >> "$LOG"
}

log "=== supervisor up (pid $$, backend=$BACKEND) ==="
fails=0
last_iters="$(iters)"
stuck=0
while true; do
  if ! healthy; then
    fails=$((fails+1))
    log "UNHEALTHY (HTTP $(code) — timeout/refused). consecutive=$fails"
    if [ "$fails" -ge 5 ]; then log "BACKOFF: 5+ consecutive failures, sleeping 60s"; sleep 60; fi
    restart_server || { sleep 20; continue; }
    fails=0
  fi
  kick_optimizer
  cur="$(iters)"
  if [ "$cur" = "$last_iters" ]; then stuck=$((stuck+1)); else stuck=0; last_iters="$cur"; fi
  log "tick | iters=$cur (stalled_ticks=$stuck) | $(status | head -c 150)"
  # If running but NO new iterations for ~6 min, the loop is hung — force a restart.
  if [ "$stuck" -ge 18 ] && status | grep -q '"status" *: *"running"'; then
    log "STALL: running but iters flat for ~6min -> force server restart"
    restart_server; stuck=0
  fi
  sleep 20
done

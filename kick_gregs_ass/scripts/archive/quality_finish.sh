#!/usr/bin/env bash
# =============================================================================
# GBBO — Quality study finisher (unattended, gated)
# -----------------------------------------------------------------------------
# Chains the remaining LIVE quality-study steps in dependency order, with the
# right gates so two Opus consumers never thrash the same quota:
#
#   1. WAIT for the quality OPTIMIZE phase (must already be running/started
#      separately) to have produced data/bakeoff/quality_prompts.json.
#   2. RUN the multi-turn quality run (Sonnet 4.6 thinking-off + Haiku 4.5;
#      no Opus, no retrieval backend) through the chosen prompts.
#   3. WAIT for the BAKE-OFF Opus judge to finish (so the quality per-turn judge,
#      also Opus, does not contend with it), detected by judge_scores.jsonl going
#      quiet (no growth for a sustained window).
#   4. RUN the quality per-turn judge (Opus) over the quality outcomes.
#
# Idempotent + resumable: every underlying phase skips already-done work, so a
# re-run is safe. Foreground; logs to logs/quality_finish.log. Intended to be
# launched as a background process and left alone.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export PYTHONUNBUFFERED=1
export AWS_REGION=us-west-2

PY=.venv/bin/python
PROMPTS=data/bakeoff/quality_prompts.json
QUAL_OUT=data/bakeoff/quality_outcomes.jsonl
JUDGE=data/bakeoff/judge_scores.jsonl

log() { echo "[quality-finish $(date '+%H:%M:%S')] $*"; }

# --- 1. wait for the optimizer to write the chosen prompts -------------------
log "waiting for $PROMPTS (optimize phase to finish)..."
for i in $(seq 1 240); do          # up to ~2h (30s * 240)
  if [ -f "$PROMPTS" ]; then
    log "chosen prompts present."
    break
  fi
  sleep 30
done
if [ ! -f "$PROMPTS" ]; then
  log "ERROR: chosen prompts never appeared; aborting finisher."
  exit 1
fi

# --- 2. run the multi-turn quality run (no Opus; safe alongside the judge) ---
log "starting quality RUN phase (reads chosen prompts)..."
$PY -m bakeoff.quality.main run --backend live --force
log "quality RUN phase exited with status $?."

# --- 3. wait for the bake-off Opus judge to go quiet -------------------------
# The bake-off judge appends to judge_scores.jsonl. We treat it as finished when
# the line count is stable across a sustained window (no new verdicts), so the
# quality per-turn judge (also Opus) does not fight it for quota.
log "waiting for the bake-off Opus judge to finish (judge_scores.jsonl quiescent)..."
prev=-1
stable=0
for i in $(seq 1 480); do          # up to ~4h (30s * 480)
  cur=$( [ -f "$JUDGE" ] && wc -l < "$JUDGE" | tr -d ' ' || echo 0 )
  if [ "$cur" = "$prev" ]; then
    stable=$((stable + 1))
  else
    stable=0
  fi
  prev="$cur"
  # 6 consecutive stable checks (~3 min) with at least some verdicts present.
  if [ "$stable" -ge 6 ] && [ "$cur" -gt 0 ]; then
    log "bake-off judge quiescent at $cur verdicts."
    break
  fi
  sleep 30
done

# --- 4. run the quality per-turn Opus judge ----------------------------------
log "starting quality JUDGE phase (per-turn Opus)..."
$PY -m bakeoff.quality.main judge --backend live --force
log "quality JUDGE phase exited with status $?."

log "DONE. quality outcomes: $( [ -f "$QUAL_OUT" ] && wc -l < "$QUAL_OUT" | tr -d ' ' || echo 0 ) lines."

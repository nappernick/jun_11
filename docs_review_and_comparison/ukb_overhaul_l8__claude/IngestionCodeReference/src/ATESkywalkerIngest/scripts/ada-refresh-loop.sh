#!/usr/bin/env bash
# Keeps the alpha assumed-role session alive across a long local run (Task 1 / R9).
# ada credentials are short-lived (~20 min effective when scripted); refresh on a timer.
set -uo pipefail

ACCOUNT="${ACCOUNT:-948580600005}"
ROLE="${ROLE:-IibsAdminAccess-DO-NOT-DELETE}"
PROFILE="${PROFILE:-alpha}"
INTERVAL="${INTERVAL:-600}" # seconds between refreshes (10 min; safely under the ~20 min TTL)

while true; do
  ada credentials update --account "$ACCOUNT" --provider conduit --role "$ROLE" --profile "$PROFILE" --once \
    && echo "[ada-refresh] $(date -Is) refreshed $PROFILE" \
    || echo "[ada-refresh] $(date -Is) refresh FAILED for $PROFILE"
  sleep "$INTERVAL"
done

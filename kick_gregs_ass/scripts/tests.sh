#!/usr/bin/env bash
# =============================================================================
# GBBO — Script 3 of 3:  PYTHON TEST SUITE  (verify the harness logic)
# -----------------------------------------------------------------------------
# Runs the full bakeoff pytest suite (unit + Hypothesis property + integration),
# VERBOSE with per-test durations, streaming every test name to this terminal
# AND logs/tests.log. No '| tail', nothing hidden — you see exactly what runs,
# whether it passed, and how slow it was.
#
#   bash scripts/tests.sh                 # whole suite
#   bash scripts/tests.sh bakeoff/tests/test_adapters.py   # one file (passthrough)
#
# Independent of the servers: needs no Bedrock and no running backend (the suite
# uses mocks/stubs). Run it in its own terminal whenever you want a green check.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export PYTHONUNBUFFERED=1   # live, line-by-line stream (no block-buffering)
exec > >(tee -a logs/tests.log) 2>&1

echo "============================================================"
echo "[tests] $(date '+%Y-%m-%d %H:%M:%S')  running bakeoff test suite"
echo "============================================================"

# Default target is the whole suite; allow passing specific files/args through.
TARGET=("$@")
if [ ${#TARGET[@]} -eq 0 ]; then
  TARGET=("bakeoff/tests/")
fi

# -v: every test name. --durations=15: surface the slowest tests (no hidden
# slowness). -p no:cacheprovider: clean, reproducible. -o addopts="": ignore any
# repo pytest addopts so output is exactly what we asked for.
exec .venv/bin/python -m pytest "${TARGET[@]}" \
  -v --durations=15 -p no:cacheprovider -o addopts=""

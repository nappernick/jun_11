#!/usr/bin/env bash
# =============================================================================
# GBBO — Script 1 of 3:  RAG RETRIEVAL SUBSTRATE  (the held-constant backend)
# -----------------------------------------------------------------------------
# Starts Qdrant (docker) + the retrieval backend on http://127.0.0.1:8080.
# Run this in its OWN terminal window. It runs in the foreground and streams its
# log here AND to logs/retrieval.log, so you can watch retrieval health live.
#
#   bash scripts/retrieval.sh
#
# Start this FIRST. Script 2 (dashboard/run) calls /retrieve on :8080; without
# this up, every trial errors. Ctrl-C to stop (leaves the Qdrant container up).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
# Force unbuffered Python/uvicorn output so the tee'd stream appears LIVE in the
# terminal (line-by-line) instead of block-buffering and looking hung. This is
# the difference between "watch it go" and "stare at a frozen window".
export PYTHONUNBUFFERED=1
# Tee everything to a logfile while still streaming to this terminal.
exec > >(tee -a logs/retrieval.log) 2>&1

echo "============================================================"
echo "[retrieval] $(date '+%Y-%m-%d %H:%M:%S')  starting RAG substrate"
echo "============================================================"
export AWS_REGION=us-west-2

# --- Bedrock creds: /retrieve embeds + reranks via Bedrock on the `alpha` account.
# This script does NOT refresh credentials — you manage them yourself (refresh the
# `alpha` profile in your own shell, however you prefer). We only PIN the profile to
# `alpha` so boto3 (src/bedrock_client.py uses a bare boto3.Session()) reads the alpha
# creds and never silently falls back to a stale `default` profile — that fallback is
# what produced the "security token included in the request is invalid" failure during
# ingest. A non-fatal check warns early if those creds are missing/expired.
export AWS_PROFILE=alpha
if .venv/bin/python -c "import boto3; boto3.Session().client('sts', region_name='us-west-2').get_caller_identity()" >/dev/null 2>&1; then
  echo "[retrieval] AWS creds OK on profile '$AWS_PROFILE'"
else
  echo "[retrieval] WARN: AWS creds for profile '$AWS_PROFILE' are missing/expired —"
  echo "[retrieval]       embed/rerank will fail until you refresh them. Continuing."
fi

# --- Docker + Qdrant ----------------------------------------------------------
if ! docker info >/dev/null 2>&1; then
  echo "[retrieval] FAIL: docker daemon is not running. Start it (e.g. 'colima start') and re-run." >&2
  exit 1
fi
echo "[retrieval] starting/reusing Qdrant container 'qdrant-faq' on :6333"
docker start qdrant-faq 2>/dev/null \
  || docker run -d --name qdrant-faq -p 6333:6333 \
       -v "$(pwd)/qdrant_storage:/qdrant/storage" qdrant/qdrant:latest
echo "[retrieval] waiting for Qdrant /readyz (max 30s)..."
for _ in $(seq 1 60); do
  curl -sf http://localhost:6333/readyz >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf http://localhost:6333/readyz >/dev/null 2>&1 \
  || { echo "[retrieval] FAIL: Qdrant never became ready on :6333" >&2; exit 1; }
echo "[retrieval] Qdrant ready."

# --- venv deps + corpus ingest ------------------------------------------------
echo "[retrieval] ensuring Python deps (.venv)"
.venv/bin/pip install -q -r requirements.txt
echo "[retrieval] ingesting corpus (Embed v4 + BM25 -> Qdrant)"
.venv/bin/python -m src.ingest data/faq_corpus.csv

# --- serve (foreground) -------------------------------------------------------
echo "[retrieval] serving on http://127.0.0.1:8080  (POST /retrieve, GET /healthz)"
echo "[retrieval] foreground — Ctrl-C to stop. Log: logs/retrieval.log"
exec .venv/bin/uvicorn src.server:app --host 127.0.0.1 --port 8080

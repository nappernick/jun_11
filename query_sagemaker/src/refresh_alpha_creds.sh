#!/usr/bin/env bash
# Keeps the `alpha` profile credentials fresh for the Skywalker ingest alpha
# account (948580600005). ada's `--once` does a single refresh and exits, so we
# loop it ourselves on a fixed interval rather than relying on ada's scheduler.
set -uo pipefail

ACCOUNT_ID="948580600005"
PROVIDER="conduit"
ROLE="IibsAdminAccess-DO-NOT-DELETE"
PROFILE="alpha"
INTERVAL_SECONDS="${1:-1800}"  # default 30 minutes

while true; do
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] refreshing creds for profile=${PROFILE} account=${ACCOUNT_ID}"
  ada credentials update \
    --account "${ACCOUNT_ID}" \
    --provider "${PROVIDER}" \
    --role "${ROLE}" \
    --profile "${PROFILE}" \
    --once
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] next refresh in ${INTERVAL_SECONDS}s"
  sleep "${INTERVAL_SECONDS}"
done

#!/usr/bin/env bash
# =============================================================================
# GBBO — Sidecar:  CREDENTIAL REFRESHER  (keeps Bedrock creds alive overnight)
# -----------------------------------------------------------------------------
# The root cause of the first overnight failure: the retrieval backend's AWS
# credentials (refreshed once at startup, ~1h lifetime) expired ~40 min into a
# ~13h run, so every /retrieve 500'd on ExpiredTokenException and the run
# auto-paused. This sidecar refreshes the on-disk credential chain on a loop so
# it never lapses. The retrieval backend now rebuilds its boto3 client from a
# fresh session on an auth error (src/bedrock_client.py), so it picks these up.
#
#   bash scripts/creds.sh                              # use all defaults
#   bash scripts/creds.sh --account 948580600005       # override account
#   bash scripts/creds.sh --role IibsAdminAccess-DO-NOT-DELETE  # override role
#   bash scripts/creds.sh --profile alpha              # override profile
#   bash scripts/creds.sh --no-profile                 # drop --profile entirely
#   bash scripts/creds.sh --refresh 900                # refresh every 900s
#   bash scripts/creds.sh --account 123 --role R --profile p --refresh 600
#   ADA_ACCOUNT=123456789012 bash scripts/creds.sh     # env var still works
#   REFRESH_EVERY_S=900 bash scripts/creds.sh          # same as --refresh 900
#
# Flags are optional and override the env-var / built-in defaults below.
#
# Run this in its OWN terminal (or background). Foreground; tees to logs/creds.log.
# Ctrl-C to stop. Independent of the other scripts.
# =============================================================================
set -uo pipefail   # NOT -e: a single failed refresh must never kill the loop
cd "$(dirname "$0")/.."
mkdir -p logs
export PYTHONUNBUFFERED=1
exec > >(tee -a logs/creds.log) 2>&1

# Refresh well inside the ~1h credential lifetime. 30 min default leaves a wide
# safety margin so a slow/failed refresh still has time to retry before expiry.
REFRESH_EVERY_S="${REFRESH_EVERY_S:-1800}"
# Defaults (overridable by env var, then by flags below).
ACCOUNT="${ADA_ACCOUNT:-948580600005}"
ROLE="${ADA_ROLE:-IibsAdminAccess-DO-NOT-DELETE}"
PROVIDER="${ADA_PROVIDER:-conduit}"
PROFILE="${ADA_PROFILE:-alpha}"   # empty (or --no-profile) omits --profile

USAGE="Usage: $0 [--account ACCOUNT_NUMBER] [--role ROLE_STRING] \
[--profile PROFILE | --no-profile] [--refresh SECONDS]"

# Optional flags. None are required; when omitted the defaults above are used.
#   --account N    AWS account number
#   --role R       role string
#   --profile P    ada profile name
#   --no-profile   omit --profile from the ada call entirely
#   --refresh S    refresh interval in SECONDS (not milliseconds)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account)
      [[ $# -ge 2 ]] || { echo "[creds] FATAL: --account needs a value" >&2; exit 2; }
      ACCOUNT="$2"; shift 2 ;;
    --account=*)
      ACCOUNT="${1#*=}"; shift ;;
    --role)
      [[ $# -ge 2 ]] || { echo "[creds] FATAL: --role needs a value" >&2; exit 2; }
      ROLE="$2"; shift 2 ;;
    --role=*)
      ROLE="${1#*=}"; shift ;;
    --profile)
      [[ $# -ge 2 ]] || { echo "[creds] FATAL: --profile needs a value" >&2; exit 2; }
      PROFILE="$2"; shift 2 ;;
    --profile=*)
      PROFILE="${1#*=}"; shift ;;
    --no-profile)
      PROFILE=""; shift ;;
    --refresh)
      [[ $# -ge 2 ]] || { echo "[creds] FATAL: --refresh needs a value (seconds)" >&2; exit 2; }
      REFRESH_EVERY_S="$2"; shift 2 ;;
    --refresh=*)
      REFRESH_EVERY_S="${1#*=}"; shift ;;
    -h|--help)
      echo "$USAGE"; exit 0 ;;
    *)
      echo "[creds] FATAL: unknown argument '$1'" >&2
      echo "$USAGE" >&2
      exit 2 ;;
  esac
done

# --refresh / REFRESH_EVERY_S must be a positive integer number of seconds.
if ! [[ "$REFRESH_EVERY_S" =~ ^[1-9][0-9]*$ ]]; then
  echo "[creds] FATAL: refresh interval must be a positive integer (seconds), got '$REFRESH_EVERY_S'" >&2
  exit 2
fi

echo "============================================================"
echo "[creds] $(date '+%Y-%m-%d %H:%M:%S')  credential refresher starting"
echo "[creds] account=$ACCOUNT role=$ROLE provider=$PROVIDER profile=${PROFILE:-<none>} every=${REFRESH_EVERY_S}s"
echo "============================================================"

if ! command -v ada >/dev/null 2>&1; then
  echo "[creds] FATAL: 'ada' is not on PATH; cannot refresh credentials." >&2
  exit 1
fi

refresh() {
  # Build the arg list so --profile is omitted entirely when PROFILE is empty.
  local args=(credentials update --account="$ACCOUNT" --role="$ROLE" --provider="$PROVIDER")
  [[ -n "$PROFILE" ]] && args+=(--profile "$PROFILE")
  args+=(--once)
  if ada "${args[@]}"; then
    echo "[creds] $(date '+%H:%M:%S')  refreshed OK"
  else
    echo "[creds] $(date '+%H:%M:%S')  WARN: refresh failed (will retry next cycle)" >&2
  fi
}

# Refresh immediately, then on the interval forever.
refresh
while true; do
  sleep "$REFRESH_EVERY_S"
  refresh
done

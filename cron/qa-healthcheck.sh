#!/bin/bash
# Fleet Commodore QA readiness — separate cron surface for the scheduler.
# It performs only local image/process/config checks. The daemon owns the
# deduplicated operator page when a requester actually hits a failed service.
set -euo pipefail

DEFAULT_DIR=$(cd "$(dirname "$0")/.." && pwd)
: "${FLEET_COMMODORE_RELEASE_DIR:=$DEFAULT_DIR}"
: "${FLEET_COMMODORE_CONFIG:=$FLEET_COMMODORE_RELEASE_DIR/.env}"
DIR=$FLEET_COMMODORE_RELEASE_DIR

if [[ ! -r "$FLEET_COMMODORE_CONFIG" ]]; then
  echo "$(date -u +%FT%TZ) QA readiness unavailable: runtime config unreadable" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$FLEET_COMMODORE_CONFIG"
set +a

PYTHON_BIN=${FLEET_COMMODORE_PYTHON:-"$DIR/.venv/bin/python3"}
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "$(date -u +%FT%TZ) QA readiness unavailable: Python missing" >&2
  exit 1
fi

exec "$PYTHON_BIN" "$DIR/bin/qa-healthcheck.py" --quick

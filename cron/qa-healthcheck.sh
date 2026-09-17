#!/bin/bash
# Fleet Commodore QA readiness — separate cron surface for the scheduler.
# Default inspection is local and model-free. --page additionally allows the
# outcome watcher to send a receipt-fenced operator DM on the Mini. Registration
# must describe that changed mode before the existing cron row enables it.
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

READINESS_RC=0
"$PYTHON_BIN" "$DIR/bin/qa-healthcheck.py" --quick || READINESS_RC=$?
OUTCOME_RC=0
"$PYTHON_BIN" "$DIR/outcome_watch.py" "$@" || OUTCOME_RC=$?
if (( READINESS_RC != 0 || OUTCOME_RC != 0 )); then
  exit 1
fi

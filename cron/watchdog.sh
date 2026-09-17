#!/bin/bash
# Registered cron: inspect process ownership, never merely pane existence.
set -euo pipefail
DEFAULT_DIR=$(cd "$(dirname "$0")/.." && pwd)
: "${FLEET_COMMODORE_RELEASE_DIR:=$DEFAULT_DIR}"
: "${FLEET_COMMODORE_CONFIG:=$FLEET_COMMODORE_RELEASE_DIR/.env}"
DIR=$FLEET_COMMODORE_RELEASE_DIR

if [[ ! -x "$DIR/run.sh" || ! -r "$FLEET_COMMODORE_CONFIG" ]]; then
  echo "$(date -u +%FT%TZ) release/config unavailable; refusing watchdog start" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$FLEET_COMMODORE_CONFIG"
set +a
PYTHON_BIN=${FLEET_COMMODORE_PYTHON:-"$DIR/.venv/bin/python3"}
exec "$PYTHON_BIN" "$DIR/fleet_watchdog.py" "$@"

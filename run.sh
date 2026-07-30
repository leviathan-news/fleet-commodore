#!/bin/bash
# Fleet Commodore runner — sources the service-owned runtime config and
# launches commodore.py in the venv. Invoked inside tmux (not backgrounded
# here) so tmux captures stdout/stderr and a cron watchdog can respawn it.
set -euo pipefail
cd "$(dirname "$0")"
REPO_DIR=$(pwd)
: "${FLEET_COMMODORE_CONFIG:=$REPO_DIR/.env}"
if [[ ! -r "$FLEET_COMMODORE_CONFIG" ]]; then
  echo "Fleet Commodore runtime config is unreadable: $FLEET_COMMODORE_CONFIG" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source "$FLEET_COMMODORE_CONFIG"
set +a
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
: "${FLEET_COMMODORE_STATE_DIR:=$HOME/.local/state/fleet-commodore}"
: "${FLEET_COMMODORE_LOG_DIR:=$FLEET_COMMODORE_STATE_DIR/logs}"
PYTHON_BIN=${FLEET_COMMODORE_PYTHON:-"$REPO_DIR/.venv/bin/python3"}
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Fleet Commodore Python runtime is unavailable: $PYTHON_BIN" >&2
  exit 1
fi
mkdir -p "$FLEET_COMMODORE_LOG_DIR"
# Do not prevent the chat daemon from starting when Q&A is degraded: it is
# needed to issue the honest failure reply and page the operator on demand.
# The watchdog runs the same bounded readiness check every five minutes.
if ! "$PYTHON_BIN" "$REPO_DIR/bin/qa-healthcheck.py" --quick \
    >> "$FLEET_COMMODORE_LOG_DIR/qa-healthcheck.log" 2>&1; then
  echo "$(date -u +%FT%TZ) QA readiness check degraded; daemon will start" \
    >> "$FLEET_COMMODORE_LOG_DIR/commodore.log"
fi
# PYTHONUNBUFFERED=1 so logs flush immediately without a tee buffer.
# Redirect stderr to stdout so tmux pane + file both capture everything.
exec "$PYTHON_BIN" -u commodore.py >> "$FLEET_COMMODORE_LOG_DIR/commodore.log" 2>&1

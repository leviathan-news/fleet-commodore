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
mkdir -p "$FLEET_COMMODORE_LOG_DIR"
# PYTHONUNBUFFERED=1 so logs flush immediately without a tee buffer.
# Redirect stderr to stdout so tmux pane + file both capture everything.
exec .venv/bin/python3 -u commodore.py >> "$FLEET_COMMODORE_LOG_DIR/commodore.log" 2>&1

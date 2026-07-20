#!/bin/bash
# Cron watchdog — respawns commodore tmux window if it dies.
set -euo pipefail
TMUX=/opt/homebrew/bin/tmux
SESSION=leviathan
WINDOW=commodore
DEFAULT_DIR=$(cd "$(dirname "$0")/.." && pwd)
: "${FLEET_COMMODORE_RELEASE_DIR:=$DEFAULT_DIR}"
: "${FLEET_COMMODORE_CONFIG:=$FLEET_COMMODORE_RELEASE_DIR/.env}"
DIR=$FLEET_COMMODORE_RELEASE_DIR

if [[ ! -x "$DIR/run.sh" || ! -r "$FLEET_COMMODORE_CONFIG" ]]; then
  echo "$(date -u +%FT%TZ) release/config unavailable; refusing watchdog start" >&2
  exit 1
fi

START_COMMAND="FLEET_COMMODORE_CONFIG=$FLEET_COMMODORE_CONFIG FLEET_COMMODORE_RELEASE_DIR=$DIR $DIR/run.sh"

if $TMUX has-session -t "$SESSION" 2>/dev/null && \
   $TMUX list-windows -t "$SESSION" -F "#W" 2>/dev/null | grep -qx "$WINDOW"; then
  exit 0
fi

if ! $TMUX has-session -t "$SESSION" 2>/dev/null; then
  $TMUX new-session -d -s "$SESSION" -n "$WINDOW" "$START_COMMAND"
else
  $TMUX new-window -t "$SESSION" -n "$WINDOW" "$START_COMMAND"
fi
echo "$(date -u +%FT%TZ) respawned commodore tmux window from $DIR"

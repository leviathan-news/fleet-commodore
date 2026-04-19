#!/bin/bash
# Cron watchdog — respawns commodore tmux window if it dies.
set -euo pipefail
TMUX=/opt/homebrew/bin/tmux
SESSION=leviathan
WINDOW=commodore
DIR=/Users/gerrithall/dev/leviathan/fleet-commodore

if $TMUX has-session -t "$SESSION" 2>/dev/null && \
   $TMUX list-windows -t "$SESSION" -F "#W" 2>/dev/null | grep -qx "$WINDOW"; then
  exit 0
fi

if ! $TMUX has-session -t "$SESSION" 2>/dev/null; then
  $TMUX new-session -d -s "$SESSION" -n "$WINDOW" "cd $DIR && ./run.sh"
else
  $TMUX new-window -t "$SESSION" -n "$WINDOW" "cd $DIR && ./run.sh"
fi
echo "$(date -u +%FT%TZ) respawned commodore tmux window"

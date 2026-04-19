#!/bin/bash
# Fleet Commodore runner — sources .env and launches commodore.py in the
# venv. Invoked inside tmux (not backgrounded here) so tmux captures
# stdout/stderr and a cron watchdog can respawn it.
set -euo pipefail
cd "$(dirname "$0")"
set -a
# shellcheck disable=SC1091
source .env
set +a
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
# PYTHONUNBUFFERED=1 so logs flush immediately without a tee buffer.
# Redirect stderr to stdout so tmux pane + file both capture everything.
mkdir -p logs
exec .venv/bin/python3 -u commodore.py >> logs/commodore.log 2>&1

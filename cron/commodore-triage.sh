#!/bin/bash
# Lev Sec delivery failsafe. Install explicitly on the Mini:
# */5 * * * * /Users/gerrithall/dev/leviathan/fleet-commodore/cron/commodore-triage.sh
#
# This never restarts the Commodore. TRIAGE_POSTING_ENABLED defaults to 0 in
# both the script and .env.example; changing it to 1 remains an operator's
# separate go-live decision.
set -euo pipefail

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"

set -a
# shellcheck disable=SC1091
[[ -f "$REPO_DIR/.env" ]] && source "$REPO_DIR/.env"
set +a

: "${TRIAGE_POSTING_ENABLED:=0}"
export TRIAGE_POSTING_ENABLED
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/bin:$PATH"

exec "$REPO_DIR/.venv/bin/python3" -u "$REPO_DIR/triage/commodore_triage.py" \
  --scan-db >> "$LOG_DIR/triage.log" 2>&1

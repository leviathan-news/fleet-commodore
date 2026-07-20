#!/bin/bash
# Lev Sec delivery failsafe. Install exactly one manifest-pinned copy on the
# Mini. The default invocation is --scan-db; --provider-probe forces a
# no-post Sonnet readiness call through this exact wrapper.
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

# The ledger and executor lock deliberately live outside a mutable release
# worktree. A worktree replacement must preserve completed receipts, pending
# claims, and outcome_unknown fences instead of silently creating a new DB.
: "${TRIAGE_STATE_DIR:=$HOME/.local/state/fleet-commodore}"
mkdir -p "$TRIAGE_STATE_DIR"
: "${TRIAGE_DB_FILE:=$TRIAGE_STATE_DIR/triage.db}"
export TRIAGE_DB_FILE

# macOS does not provide a portable flock. mkdir is atomic on the local Mini
# filesystem: a second cron/manual executor exits without touching the ledger.
# A stale lock is deliberately fail-closed and requires operator inspection;
# auto-removing it could race a slow, still-live Sonnet process.
LOCK_DIR="$TRIAGE_STATE_DIR/triage-executor.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(date -u +%FT%TZ) triage executor already active or stale lock held: $LOCK_DIR" >&2
  exit 0
fi
cleanup_lock() {
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT INT TERM
printf '%s\n' "$$" > "$LOCK_DIR/pid"

if [ "$#" -eq 0 ]; then
  set -- --scan-db
fi

"$REPO_DIR/.venv/bin/python3" -u "$REPO_DIR/triage/commodore_triage.py" \
  "$@" >> "$LOG_DIR/triage.log" 2>&1

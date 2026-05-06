#!/bin/bash
# Claude OAuth heartbeat — keeps token fresh + alerts on rotation failure.
#
# Runs hourly via cron. Fires a no-op `claude --print` against the host's
# ~/.claude/.credentials.json. Three outcomes:
#
#   1. Success: token still valid (auto-refreshed if needed by Claude CLI).
#      Logs a single OK line and exits 0.
#   2. 401 / auth failure: token expired beyond auto-refresh; needs
#      operator `claude /login`. Posts an alert to the operator's
#      Telegram (BOT_HQ_GROUP_ID) and exits 1.
#   3. Network / unexpected error: logs and exits 1, no alert (transient
#      errors will self-clear next run).
#
# Without this, an OAuth rotation that breaks daemon Claude calls is
# invisible until the operator notices Admiral isn't replying — that took
# ~20h on 2026-05-06.
#
# Cron line: */60 * * * * /Users/gerrithall/dev/leviathan/fleet-commodore/cron/claude-oauth-heartbeat.sh
set -uo pipefail

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
LOG=$REPO_DIR/logs/claude-heartbeat.log
mkdir -p "$(dirname "$LOG")"

# tmpdir for the probe scratch
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Source the daemon's .env so BOT_TOKEN + BOT_HQ_GROUP_ID are available
# for the alert path. Set -a/+a means these get exported for child procs.
set -a
# shellcheck disable=SC1091
[[ -f "$REPO_DIR/.env" ]] && source "$REPO_DIR/.env"
set +a

# Claude CLI must be on PATH for cron's bare environment.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

now() { date -u +"%FT%TZ"; }

# Run the probe with a 30s wall budget. Stderr is captured separately so
# we can classify the failure mode without the success line being polluted.
PROBE_OUT=$TMP/probe.out
PROBE_ERR=$TMP/probe.err
PROBE_RC=0
echo "ping" | claude --print --output-format text >"$PROBE_OUT" 2>"$PROBE_ERR" &
PROBE_PID=$!

# Wait up to 30s
for _ in $(seq 1 30); do
    kill -0 "$PROBE_PID" 2>/dev/null || break
    sleep 1
done
if kill -0 "$PROBE_PID" 2>/dev/null; then
    kill -9 "$PROBE_PID" 2>/dev/null
    PROBE_RC=124  # timeout
    wait "$PROBE_PID" 2>/dev/null
else
    wait "$PROBE_PID" 2>/dev/null
    PROBE_RC=$?
fi

OUT=$(<"$PROBE_OUT")
ERR=$(<"$PROBE_ERR")
COMBINED="$OUT $ERR"

# Detect the auth-failure pattern. Same string the daemon's
# _looks_like_claude_limit_error / probe checks for.
if echo "$COMBINED" | grep -qE "Failed to authenticate|API Error: 401|authentication_error"; then
    STATE=auth_failed
elif [[ "$PROBE_RC" -eq 124 ]]; then
    STATE=timeout
elif echo "$COMBINED" | grep -qiE "usage limit|monthly usage|quota|credit balance|rate limit|too many requests"; then
    STATE=quota
elif [[ "$PROBE_RC" -ne 0 ]] || [[ -z "$OUT" ]]; then
    STATE=unknown_error
else
    STATE=ok
fi

# Log line — always written, terse
echo "$(now) state=$STATE rc=$PROBE_RC out_len=${#OUT}" >> "$LOG"

# Alert on auth_failed only — quota/timeout/unknown will self-clear
# without operator action; auth_failed needs `claude /login`.
if [[ "$STATE" == "auth_failed" ]]; then
    if [[ -n "${BOT_TOKEN:-}" && -n "${BOT_HQ_GROUP_ID:-}" ]]; then
        # Has a sustained-quiet flag: only alert if we haven't alerted
        # in the last 6 hours (avoid spam if the operator is mid-fix).
        ALERT_FILE=$REPO_DIR/logs/.claude-heartbeat-last-alert
        LAST_ALERT=0
        [[ -f "$ALERT_FILE" ]] && LAST_ALERT=$(cat "$ALERT_FILE" 2>/dev/null || echo 0)
        NOW_EPOCH=$(date +%s)
        if (( NOW_EPOCH - LAST_ALERT > 6 * 3600 )); then
            MSG="⚠️ Fleet Commodore: Claude OAuth has expired on the Mini.\nThe daemon will fall back to Codex (also broken) until you run \`claude /login\` on the Mini.\nLast probe at $(now) returned 401."
            curl -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
                -d "chat_id=${BOT_HQ_GROUP_ID}" \
                -d "text=$MSG" \
                >/dev/null 2>&1 || true
            echo "$NOW_EPOCH" > "$ALERT_FILE"
            echo "$(now) alerted Bot HQ" >> "$LOG"
        fi
    fi
    exit 1
fi

# On any non-OK state, exit non-zero so cron's MAILTO can pick it up if set.
[[ "$STATE" == "ok" ]] || exit 1
exit 0

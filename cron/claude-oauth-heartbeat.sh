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
#   3. Network / unexpected error: logs and contributes to a bounded alert
#      after three consecutive non-OK probes.
#
# Without this, an OAuth rotation that breaks daemon Claude calls is
# invisible until the operator notices Admiral isn't replying — that took
# ~20h on 2026-05-06.
#
# Cron supplies FLEET_COMMODORE_RELEASE_DIR and FLEET_COMMODORE_CONFIG, so
# this probe always runs from the immutable daemon release rather than a
# developer checkout that may be stale or dirty.
set -uo pipefail

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
: "${FLEET_COMMODORE_CONFIG:=$REPO_DIR/.env}"
: "${FLEET_COMMODORE_STATE_DIR:=$HOME/.local/state/fleet-commodore}"
LOG=$FLEET_COMMODORE_STATE_DIR/logs/claude-heartbeat.log
mkdir -p "$FLEET_COMMODORE_STATE_DIR/logs"

if [[ ! -r "$FLEET_COMMODORE_CONFIG" ]]; then
    echo "$(date -u +%FT%TZ) state=config_unreadable" >> "$LOG"
    exit 1
fi

# Source the service-owned config so BOT_TOKEN + BOT_HQ_GROUP_ID are available
# for the alert path. Set -a/+a means these get exported for child procs.
set -a
# shellcheck disable=SC1091
source "$FLEET_COMMODORE_CONFIG"
set +a

# Resolve state after loading the authoritative service config.
LOG=$FLEET_COMMODORE_STATE_DIR/logs/claude-heartbeat.log
mkdir -p "$FLEET_COMMODORE_STATE_DIR/logs"
CONSECUTIVE_FILE=$FLEET_COMMODORE_STATE_DIR/claude-heartbeat-consecutive
LAST_ALERT_FILE=$FLEET_COMMODORE_STATE_DIR/claude-heartbeat-last-alert
PENDING_ALERT_FILE=$FLEET_COMMODORE_STATE_DIR/claude-heartbeat-alert-pending

# Claude CLI must be on PATH for cron's bare environment.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
CLAUDE_BIN=${CLAUDE_BIN:-claude}
CURL_BIN=${CURL_BIN:-curl}
PYTHON_BIN=${FLEET_COMMODORE_PYTHON:-python3}

# Serialize scheduled and manual probes without a crash-stale directory lock.
if [[ "${1:-}" != "--lock-held" ]]; then
    exec "$PYTHON_BIN" "$REPO_DIR/cron/heartbeat_state.py" run \
        "$FLEET_COMMODORE_STATE_DIR" "$REPO_DIR/cron/claude-oauth-heartbeat.sh"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

now() { date -u +"%FT%TZ"; }
write_state() {
    # An interrupted write must not turn a pending send into an empty timestamp.
    printf '%s\n' "$2" > "$1.tmp" && mv -f "$1.tmp" "$1"
}

# Run the probe with a 30s wall budget. Stderr is captured separately so
# we can classify the failure mode without the success line being polluted.
PROBE_OUT=$TMP/probe.out
PROBE_ERR=$TMP/probe.err
PROBE_RC=0
echo "ping" | "$CLAUDE_BIN" --print --output-format text >"$PROBE_OUT" 2>"$PROBE_ERR" &
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

# Alert on auth_failed immediately; sustained quota/timeout/unknown states
# alert after three consecutive failures.
if [[ "$STATE" != "ok" ]]; then
    CONSECUTIVE=0
    [[ -r "$CONSECUTIVE_FILE" ]] && CONSECUTIVE=$(cat "$CONSECUTIVE_FILE" 2>/dev/null || echo 0)
    [[ "$CONSECUTIVE" =~ ^[0-9]+$ ]] || CONSECUTIVE=0
    CONSECUTIVE=$((CONSECUTIVE + 1))
    write_state "$CONSECUTIVE_FILE" "$CONSECUTIVE" || exit 1

    ALERT_REASON=
    if [[ "$STATE" == "auth_failed" ]]; then
        ALERT_REASON="Claude OAuth authentication failed on the Mini. Please run claude /login on the Mini. Last probe at $(now) returned an authentication failure."
    elif (( CONSECUTIVE >= 3 )); then
        ALERT_REASON="Claude heartbeat has failed for $CONSECUTIVE consecutive probes; current state is $STATE. Last probe at $(now)."
    fi

    if [[ -n "$ALERT_REASON" && -n "${BOT_TOKEN:-}" && -n "${BOT_HQ_GROUP_ID:-}" ]]; then
        NOW_EPOCH=$(date +%s)
        LAST_ALERT=0
        [[ -r "$LAST_ALERT_FILE" ]] && LAST_ALERT=$(cat "$LAST_ALERT_FILE" 2>/dev/null || echo 0)
        PENDING=0
        [[ -r "$PENDING_ALERT_FILE" ]] && PENDING=$(cat "$PENDING_ALERT_FILE" 2>/dev/null || echo 0)
        [[ "$LAST_ALERT" =~ ^[0-9]+$ ]] || LAST_ALERT=0
        [[ "$PENDING" =~ ^[0-9]+$ ]] || PENDING=0
        if (( NOW_EPOCH - LAST_ALERT > 6 * 3600 && NOW_EPOCH - PENDING > 6 * 3600 )); then
            # Plain text is deliberate: this alert contains no Telegram markup.
            MSG="⚠️ Fleet Commodore: $ALERT_REASON"
            # Hold before sending: a crash or dropped response must not replay
            # immediately on the next cron tick.
            write_state "$PENDING_ALERT_FILE" "$NOW_EPOCH" || exit 1
            RESPONSE=$("$CURL_BIN" --connect-timeout 5 --max-time 15 -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
                --data-urlencode "chat_id=${BOT_HQ_GROUP_ID}" \
                --data-urlencode "text=$MSG" 2>/dev/null)
            CURL_RC=$?
            SEND_RESULT=$(printf '%s' "$RESPONSE" | "$PYTHON_BIN" "$REPO_DIR/cron/heartbeat_state.py" classify 2>/dev/null)
            if (( CURL_RC == 0 )) && [[ "$SEND_RESULT" == accepted ]]; then
                write_state "$LAST_ALERT_FILE" "$NOW_EPOCH" || exit 1
                rm -f "$PENDING_ALERT_FILE"
                echo "$(now) alert accepted by Telegram" >> "$LOG"
            elif (( CURL_RC == 0 )) && [[ "$SEND_RESULT" == rejected ]]; then
                rm -f "$PENDING_ALERT_FILE"
                echo "$(now) alert rejected by Telegram" >> "$LOG"
            else
                # The outcome may be unknown (e.g. a dropped response). Hold it
                # for the dedup window so we never immediately replay an alert.
                echo "$(now) alert outcome ambiguous; held" >> "$LOG"
            fi
        fi
    fi
    exit 1
fi

# A successful probe clears the sustained-failure counter.
write_state "$CONSECUTIVE_FILE" 0 || exit 1

exit 0

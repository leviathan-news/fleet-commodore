import os
import fcntl
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "cron/claude-oauth-heartbeat.sh"


def run_heartbeat(tmp_path, state, response='{"ok":true}', curl_rc="0"):
    fake_bin = tmp_path / ".local/bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    (fake_bin / "claude").write_text(
        '#!/bin/sh\n'
        'case "$PROBE_STATE" in\n'
        '  ok) echo pong; exit 0;;\n'
        '  auth) echo "API Error: 401 authentication_error" >&2; exit 1;;\n'
        '  quota) echo "usage limit" >&2; exit 1;;\n'
        '  timeout) exit 124;;\n'
        '  *) echo "unexpected failure" >&2; exit 1;;\n'
        'esac\n'
    )
    (fake_bin / "curl").write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        'printf "%s" "$CURL_RESPONSE"\n'
        'exit "$CURL_RC"\n'
    )
    for program in (fake_bin / "claude", fake_bin / "curl"):
        program.chmod(0o755)
    config = tmp_path / "config"
    config.write_text("BOT_TOKEN=test-token\nBOT_HQ_GROUP_ID=-123\n")
    env = os.environ.copy()
    env.update(
        FLEET_COMMODORE_CONFIG=str(config),
        FLEET_COMMODORE_STATE_DIR=str(tmp_path / "state"),
        CLAUDE_BIN=str(fake_bin / "claude"),
        CURL_BIN=str(fake_bin / "curl"),
        FLEET_COMMODORE_PYTHON=sys.executable,
        PROBE_STATE=state,
        CURL_RESPONSE=response,
        CURL_RC=curl_rc,
        CURL_LOG=str(tmp_path / "curl.log"),
    )
    return subprocess.run([str(SCRIPT)], env=env, text=True, capture_output=True)


def test_mixed_failures_alert_on_third_and_reset_on_success(tmp_path):
    for state in ("quota", "unknown", "timeout"):
        result = run_heartbeat(tmp_path, state)
        assert result.returncode == 1
    message = (tmp_path / "curl.log").read_text()
    assert "current state is timeout" in message
    assert "fall back to Codex" not in message
    assert (tmp_path / "state/claude-heartbeat-consecutive").read_text().strip() == "3"

    assert run_heartbeat(tmp_path, "ok").returncode == 0
    assert (tmp_path / "state/claude-heartbeat-consecutive").read_text().strip() == "0"


def test_auth_failure_alert_is_immediate_without_codex_claim(tmp_path):
    result = run_heartbeat(tmp_path, "auth")
    assert result.returncode == 1
    message = (tmp_path / "curl.log").read_text()
    assert "authentication failed" in message
    assert "fall back to Codex" not in message


def test_only_telegram_ok_true_records_success(tmp_path):
    run_heartbeat(tmp_path, "auth", response='{"meta":{"ok":true},"ok":false,"description":"blocked"}')
    state = tmp_path / "state"
    assert not (state / "claude-heartbeat-last-alert").exists()
    assert "alert rejected" in (state / "logs/claude-heartbeat.log").read_text()

    run_heartbeat(tmp_path, "auth", response="not-json", curl_rc="1")
    assert (state / "claude-heartbeat-alert-pending").exists()
    assert "alert outcome ambiguous" in (state / "logs/claude-heartbeat.log").read_text()
    sends_before = (tmp_path / "curl.log").read_text().splitlines()
    run_heartbeat(tmp_path, "auth", response='{"ok":true}')
    assert (tmp_path / "curl.log").read_text().splitlines() == sends_before
    assert not (state / "claude-heartbeat-last-alert").exists()

    # An ambiguous result is held for the dedup window; once it expires, an
    # accepted Telegram response is the only outcome that records success.
    (state / "claude-heartbeat-alert-pending").write_text("0\n")
    run_heartbeat(tmp_path, "auth", response='{"ok":true}')
    assert (state / "claude-heartbeat-last-alert").exists()
    assert not (state / "claude-heartbeat-alert-pending").exists()

    sends = (tmp_path / "curl.log").read_text().splitlines()
    assert len(sends) == 3
    run_heartbeat(tmp_path, "auth", response='{"ok":true}')
    assert len((tmp_path / "curl.log").read_text().splitlines()) == len(sends)


def test_old_lock_marker_cannot_silence_recovery(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / ".claude-heartbeat.lock").mkdir()
    result = run_heartbeat(tmp_path, "auth", response='{"ok":true}')
    assert result.returncode == 1
    assert "sendMessage" in (tmp_path / "curl.log").read_text()


def test_advisory_lock_blocks_overlap_and_releases_after_owner_exit(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    with (state / "claude-heartbeat.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_heartbeat(tmp_path, "auth").returncode == 0
        assert not (tmp_path / "curl.log").exists()
    # The file remains, but no live lock owner remains: a later probe proceeds.
    assert run_heartbeat(tmp_path, "auth").returncode == 1
    assert len((tmp_path / "curl.log").read_text().splitlines()) == 1

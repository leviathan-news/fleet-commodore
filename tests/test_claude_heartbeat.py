import os
import fcntl
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "cron/claude-oauth-heartbeat.sh"


def run_heartbeat(tmp_path, state, response='{"ok":true,"result":{"message_id":1}}', curl_rc="0", provider="claude", probe_rc=""):
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
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        '#!/bin/sh\n'
        'case "$1" in\n'
        '  */bin/provider-probe.py)\n'
        '    printf "{\\"provider\\":\\"codex\\",\\"state\\":\\"%s\\"}\n" "$PROBE_STATE"\n'
        '    if [ -n "$PROBE_RC_OVERRIDE" ]; then exit "$PROBE_RC_OVERRIDE"; fi\n'
        '    [ "$PROBE_STATE" = ok ]\n'
        '    ;;\n'
        '  *) exec "$REAL_PYTHON" "$@";;\n'
        'esac\n'
    )
    fake_python.chmod(0o755)
    config = tmp_path / "config"
    config.write_text("BOT_TOKEN=test-token\nBOT_HQ_GROUP_ID=-123\n")
    env = os.environ.copy()
    env.update(
        FLEET_COMMODORE_CONFIG=str(config),
        FLEET_COMMODORE_STATE_DIR=str(tmp_path / "state"),
        CLAUDE_BIN=str(fake_bin / "claude"),
        CURL_BIN=str(fake_bin / "curl"),
        FLEET_COMMODORE_PYTHON=str(fake_python),
        REAL_PYTHON=sys.executable,
        FLEET_PROVIDER=provider,
        PROBE_STATE=state,
        PROBE_RC_OVERRIDE=probe_rc,
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
    run_heartbeat(tmp_path, "auth", response='{"ok":false,"error_code":403,"description":"blocked"}')
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
    run_heartbeat(tmp_path, "auth", response='{"ok":true,"result":{"message_id":1}}')
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
    result = run_heartbeat(tmp_path, "auth", response='{"ok":true,"result":{"message_id":1}}')
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


def test_codex_route_never_invokes_claude_and_resets_on_success(tmp_path):
    result = run_heartbeat(tmp_path, "ok", provider="codex")
    assert result.returncode == 0
    assert (tmp_path / "state/claude-heartbeat-consecutive").read_text().strip() == "0"
    assert not (tmp_path / "curl.log").exists()


def test_codex_failed_probe_pages_after_three_and_ok_resets(tmp_path):
    for _ in range(3):
        assert run_heartbeat(tmp_path, "quota", provider="codex").returncode == 1
    message = (tmp_path / "curl.log").read_text()
    assert "codex heartbeat has failed" in message
    assert "Claude heartbeat" not in message
    assert run_heartbeat(tmp_path, "ok", provider="codex").returncode == 0
    assert (tmp_path / "state/claude-heartbeat-consecutive").read_text().strip() == "0"


def test_codex_probe_rc_zero_with_non_ok_state_is_unknown_error(tmp_path):
    result = run_heartbeat(tmp_path, "not_a_whitelisted_state", provider="codex")
    assert result.returncode == 1
    assert "state=unknown_error" in (tmp_path / "state/logs/claude-heartbeat.log").read_text()


def test_unknown_provider_never_invokes_claude(tmp_path):
    result = run_heartbeat(tmp_path, "auth", provider="not-a-provider")
    assert result.returncode == 1
    assert "state=unknown_error" in (tmp_path / "state/logs/claude-heartbeat.log").read_text()
    assert not (tmp_path / "curl.log").exists()


def test_codex_ok_payload_with_failed_exit_is_not_health(tmp_path):
    result = run_heartbeat(tmp_path, "ok", provider="codex", probe_rc="1")
    assert result.returncode == 1
    assert "state=unknown_error" in (tmp_path / "state/logs/claude-heartbeat.log").read_text()


@pytest.mark.parametrize(("payload", "expected"), [
    ('{"ok":true,"result":{"message_id":1}}', "accepted"),
    ('{"ok":true}', "ambiguous"),
    ('{"ok":true,"result":{}}', "ambiguous"),
    ('{"ok":true,"result":{"message_id":0}}', "ambiguous"),
    ('{"ok":true,"result":{"message_id":-1}}', "ambiguous"),
    ('{"ok":true,"result":{"message_id":true}}', "ambiguous"),
    ('{"ok":true,"result":{"message_id":"1"}}', "ambiguous"),
    ('{"ok":true,"result":{"message_id":1},"nested":{"ok":true}}', "accepted"),
])
def test_classifier_requires_positive_integer_telegram_receipt(tmp_path, payload, expected):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "cron/heartbeat_state.py"), "classify"],
        input=payload, text=True, capture_output=True, check=True,
    )
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(("payload", "expected"), [
    ('{"ok":false,"error_code":400}', "rejected"),
    ('{"ok":false,"error_code":499}', "rejected"),
    ('{"ok":false,"error_code":500}', "ambiguous"),
    ('{"ok":false,"error_code":true}', "ambiguous"),
    ('{"ok":false}', "ambiguous"),
    ("not-json", "ambiguous"),
])
def test_classifier_rejects_only_known_client_errors(tmp_path, payload, expected):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "cron/heartbeat_state.py"), "classify"],
        input=payload, text=True, capture_output=True, check=True,
    )
    assert result.stdout.strip() == expected

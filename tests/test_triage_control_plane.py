"""Unit coverage for the isolated Lev Sec triage control plane.

No test imports Commodore or invokes sec_feed/Claude/Telegram for real.  The
control plane must stay testable while its posting gate is off.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parent.parent
TRIAGE_SCRIPT = REPO / "triage" / "commodore_triage.py"
VALID_ALERT_A = "32ee6d8f-4d1f-4887-a8fc-38203ff85bfd"
VALID_ALERT_B = "e537ce37-4d1f-4887-a8fc-38203ff85bfd"


@pytest.fixture
def triage(tmp_path, monkeypatch):
    """Fresh module and private ledger for every test."""
    monkeypatch.setenv("TRIAGE_DB_FILE", str(tmp_path / "triage.db"))
    monkeypatch.setenv("COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("COALESCE_MAX_WAIT_S", "0")
    monkeypatch.setenv("TRIAGE_POSTING_ENABLED", "0")
    name = f"commodore_triage_test_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(name, TRIAGE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _valid_note(verdict="benign"):
    return (
        "<b>⚓ Fleet Commodore — Swarm Triage</b>\n\n"
        "All sensitive probes returned <code>404</code>.\n\n"
        f"VERDICT: {verdict}"
    )


def _rows(db_file, query, params=()):
    with sqlite3.connect(str(db_file)) as conn:
        return conn.execute(query, params).fetchall()


def test_default_posting_gate_keeps_alert_pending_and_never_calls_model(triage, monkeypatch):
    triage.enqueue_alert(VALID_ALERT_A, "sensitive_burst (warning)")
    monkeypatch.setattr(
        triage, "ask_claude", lambda *args, **kwargs: pytest.fail("gate must stop Claude")
    )

    assert triage.process_pending(dry_run=False) == "posting_disabled"
    assert _rows(triage.DB_FILE, "SELECT alert_id FROM pending") == [(VALID_ALERT_A,)]
    assert _rows(triage.DB_FILE, "SELECT * FROM triaged_alerts") == []


def test_parse_note_requires_one_terminal_contract_and_safe_html(triage):
    assert triage.parse_note("NOTE:\n" + _valid_note()) == (_valid_note(), "benign")
    assert triage.parse_note("plain text\nVERDICT: benign\nextra") is None
    assert triage.parse_note("<i>unsupported</i>\nVERDICT: benign") is None
    assert triage.parse_note("<b>unclosed\nVERDICT: benign") is None
    assert triage.parse_note("note\nVERDICT: maybe") is None


def test_claude_is_leashed_to_sec_feed_and_read_only_tools(triage, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=_valid_note(), stderr="")

    monkeypatch.setattr(triage.subprocess, "run", fake_run)
    assert triage.ask_claude("investigate") == _valid_note()
    assert captured["argv"] == [
        triage.CLAUDE_BIN, "-p", "-", "--model", "sonnet", "--allowedTools",
        triage.CLAUDE_ALLOWED_TOOLS,
    ]
    assert captured["kwargs"]["input"] == "investigate"
    assert captured["kwargs"].get("shell", False) is False


def test_scan_queues_only_feed_deliveries_and_advances_watermark(triage, monkeypatch):
    created_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        triage,
        "_scan_feed_json",
        lambda args: [{
            "alert_id": VALID_ALERT_A,
            "signal": "sensitive_burst",
            "severity": "warning",
            "source_ip": "93.123.109.205",
            "created_at": created_at,
        }],
    )

    assert triage.scan_db() == 1
    pending = _rows(triage.DB_FILE, "SELECT alert_id, summary FROM pending")
    assert pending[0][0] == VALID_ALERT_A
    assert "sensitive_burst" in pending[0][1]
    watermark = _rows(
        triage.DB_FILE,
        "SELECT state_value FROM triage_state WHERE state_key='scan_watermark'",
    )
    assert watermark == [(created_at,)]


def test_fast_path_enriches_uuid_only_summary_from_read_only_feed(triage, monkeypatch):
    monkeypatch.setattr(
        triage,
        "_scan_feed_json",
        lambda args: {
            "id": VALID_ALERT_A,
            "signal": "detector_capacity",
            "severity": "critical",
            "source_ip": None,
            "created_at": "2026-07-19T19:33:00+00:00",
            "levsec_delivery": {
                "delivered": True,
                "status": "accepted",
                "telegram_message_id": 123,
            },
        },
    )

    enriched = triage._enrich_alert_summaries([
        {"alert_id": VALID_ALERT_A, "summary": f"alert {VALID_ALERT_A}"}
    ])
    assert "detector_capacity" in enriched[0]["summary"]


def test_fast_path_refuses_alert_not_accepted_by_levsec(triage, monkeypatch):
    monkeypatch.setattr(
        triage,
        "_scan_feed_json",
        lambda args: {
            "id": VALID_ALERT_A,
            "signal": "sensitive_burst",
            "levsec_delivery": {"delivered": False, "status": "pending"},
        },
    )

    with pytest.raises(triage.TriageError, match="refusing triage"):
        triage._feed_alert_summary(VALID_ALERT_A)


def test_atomic_claim_allows_only_one_trigger_path_to_own_alert(triage):
    triage.enqueue_alert(VALID_ALERT_A, "one")
    status, batch = triage._take_ready_batch()
    assert status == "batch"
    assert isinstance(batch, list)
    assert [a["alert_id"] for a in triage._claim_alerts(batch)] == [VALID_ALERT_A]
    assert triage._claim_alerts(batch) == []


def test_stale_claim_is_requeued_instead_of_lost_after_interrupted_process(triage):
    triage.enqueue_alert(VALID_ALERT_A, "one")
    status, batch = triage._take_ready_batch()
    assert status == "batch"
    assert isinstance(batch, list)
    assert triage._claim_alerts(batch)
    stale = "2000-01-01T00:00:00+00:00"
    with sqlite3.connect(str(triage.DB_FILE)) as conn:
        conn.execute(
            "UPDATE triaged_alerts SET claimed_at=? WHERE alert_id=?", (stale, VALID_ALERT_A)
        )

    status, recovered = triage._take_ready_batch()
    assert status == "batch"
    assert recovered == [{"alert_id": VALID_ALERT_A, "summary": f"alert {VALID_ALERT_A}"}]
    assert _rows(triage.DB_FILE, "SELECT * FROM triaged_alerts") == []


def test_scan_deduplicates_inclusive_watermark_results(triage, monkeypatch):
    created_at = datetime.now(timezone.utc).isoformat()
    deliveries = [{
        "alert_id": VALID_ALERT_A,
        "signal": "sensitive_burst",
        "severity": "warning",
        "source_ip": "93.123.109.205",
        "created_at": created_at,
    }]
    monkeypatch.setattr(triage, "_scan_feed_json", lambda args: deliveries)

    assert triage.scan_db() == 1
    assert triage.scan_db() == 0
    assert _rows(triage.DB_FILE, "SELECT alert_id FROM pending") == [(VALID_ALERT_A,)]


def test_breaker_probe_has_the_same_read_only_tool_leash(triage, monkeypatch):
    with triage._connect() as conn:
        triage._set_state(conn, "claude_unavailable_until", str(9999999999))
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(triage.subprocess, "run", fake_run)
    assert triage._claude_available() is True
    assert captured["argv"] == [
        triage.CLAUDE_BIN, "-p", "-", "--allowedTools", triage.CLAUDE_ALLOWED_TOOLS,
    ]


def test_provider_path_never_prioritizes_repo_local_sec_feed(triage, monkeypatch):
    monkeypatch.setattr(triage, "SEC_FEED_BIN", "sec_feed")
    path = triage._build_provider_env(triage.CLAUDE_BIN)["PATH"].split(":")
    assert "." not in path[:2]


def test_dry_run_prints_note_without_telegram_or_completed_claim(triage, monkeypatch, capsys):
    triage.enqueue_alert(VALID_ALERT_A, "one")
    monkeypatch.setattr(triage, "ask_claude", lambda *args, **kwargs: _valid_note())
    monkeypatch.setattr(
        triage, "send_message", lambda *args, **kwargs: pytest.fail("dry run must not post")
    )

    assert triage.process_pending(dry_run=True) == "dry_run"
    assert "VERDICT: benign" in capsys.readouterr().out
    assert _rows(triage.DB_FILE, "SELECT * FROM triaged_alerts") == []
    assert _rows(triage.DB_FILE, "SELECT alert_id FROM pending") == [(VALID_ALERT_A,)]


def test_successful_live_post_records_every_claim_once(triage, monkeypatch):
    monkeypatch.setenv("TRIAGE_POSTING_ENABLED", "1")
    triage.enqueue_alert(VALID_ALERT_A, "one")
    triage.enqueue_alert(VALID_ALERT_B, "two")
    monkeypatch.setattr(triage, "ask_claude", lambda *args, **kwargs: _valid_note())
    calls = []
    monkeypatch.setattr(
        triage, "send_message", lambda chat, note: calls.append((chat, note)) or {
            "ok": True, "result": {"message_id": 77}
        },
    )

    assert triage.process_pending(dry_run=False) == "posted"
    assert len(calls) == 1
    rows = _rows(
        triage.DB_FILE,
        "SELECT alert_id, verdict, message_id FROM triaged_alerts ORDER BY alert_id",
    )
    assert rows == [(VALID_ALERT_A, "benign", 77), (VALID_ALERT_B, "benign", 77)]


def test_flood_breaker_posts_one_line_without_calling_sonnet(triage, monkeypatch):
    monkeypatch.setenv("TRIAGE_POSTING_ENABLED", "1")
    monkeypatch.setattr(triage, "FLOOD_MAX", 1)
    triage.enqueue_alert(VALID_ALERT_A, "one")
    triage.enqueue_alert(VALID_ALERT_B, "two")
    monkeypatch.setattr(
        triage, "ask_claude", lambda *args, **kwargs: pytest.fail("flood must stand down")
    )
    posted = []
    monkeypatch.setattr(triage, "send_message", lambda chat, note: posted.append((chat, note)) or {})

    assert triage.process_pending(dry_run=False) == "flood"
    assert len(posted) == 1
    assert "standing down" in posted[0][1]
    # Cooldown prevents the next invoker from turning the same storm into spam.
    assert triage.process_pending(dry_run=False) == "cooldown"


def test_cron_wrapper_runs_only_failsafe_scan_with_safe_default():
    script = (REPO / "cron" / "commodore-triage.sh").read_text()
    assert "--scan-db" in script
    assert "TRIAGE_POSTING_ENABLED:=0" in script
    assert "poll(" not in script
    assert "tmux" not in script

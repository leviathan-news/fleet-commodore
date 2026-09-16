"""QA outage details must remain operationally useful and user-honest."""
import commodore
import json
import sqlite3

import pytest


def test_qa_failure_detail_preserves_launcher_structure():
    detail = commodore._qa_failure_detail(
        "worker_failed",
        result={
            "error": "child_exit",
            "detail": "pull access denied",
            "stderr_log": "/private/logs/qa.stderr",
            "returncode": 1,
        },
    )

    assert "child_exit" in detail
    assert "pull access denied" in detail
    assert "qa.stderr" in detail


def test_qa_outage_reply_never_dresses_breakage_in_persona():
    assert commodore._qa_outage_reply(True) == (
        "My review service is down; the operator has been alerted."
    )
    assert commodore._qa_outage_reply(False) == (
        "My review service is down; the operator could not be alerted."
    )


@pytest.mark.parametrize(("excerpt", "category"), [
    ("", "empty_output"),
    ("API Error: 401 authentication_error private-token", "authentication_failed"),
    ("claude timed out secret-question", "timeout"),
    ("usage limit private-attachment", "quota_or_limit"),
    ("Here is private content from the prompt", "unparseable_output"),
])
def test_worker_failure_is_classified_without_echoing_private_content(excerpt, category):
    detail = commodore._qa_failure_detail("worker_failed", result={
        "status": "failed", "failure_reason": "qa response was empty or unparseable",
        "claude_excerpt": excerpt, "stderr_log": "private-path",
    })
    payload = json.loads(detail)
    assert payload == {
        "error": "worker_failed",
        "failure_reason": "qa response was empty or unparseable",
        "provider_failure": category,
    }
    assert "private" not in detail
    assert "secret" not in detail


def test_unknown_worker_reason_is_not_forwarded():
    detail = commodore._qa_failure_detail("worker_failed", result={
        "status": "failed", "failure_reason": "untrusted token and question",
    })
    assert json.loads(detail)["failure_reason"] == "worker reported failure"
    assert "untrusted" not in detail


def test_failed_worker_reaches_operator_and_durable_status(monkeypatch, tmp_path):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "qa.db")
    commodore._ensure_tables()
    with sqlite3.connect(commodore.DB_FILE) as conn:
        conn.execute(
            "INSERT INTO qa_job (job_uuid, chat_id, requester_id, request_msg_id, "
            "question, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("failed-job", -1004444444444, 1234982301, 456, "test question", "queued", commodore._now_iso()),
        )
    monkeypatch.setattr(commodore, "read_result_file", lambda _id: {
        "status": "failed", "failure_reason": "qa response was empty or unparseable",
        "claude_excerpt": "authentication_error PRIVATE_TOKEN",
    })
    alerts, replies = [], []
    monkeypatch.setattr(commodore, "_alert_operator_qa_down", lambda detail: alerts.append(detail) or True)
    monkeypatch.setattr(commodore, "send_message_with_wal", lambda *args, **kw: replies.append((args, kw)))

    commodore._process_qa("failed-job")

    assert len(alerts) == len(replies) == 1
    assert json.loads(alerts[0])["provider_failure"] == "authentication_failed"
    assert "PRIVATE_TOKEN" not in alerts[0]
    assert "unknown_worker_status" not in alerts[0]
    assert replies[0][0][4] == "My review service is down; the operator has been alerted."
    assert replies[0][1]["reply_to"] == 456
    with sqlite3.connect(commodore.DB_FILE) as conn:
        row = conn.execute("SELECT status, declined_reason FROM qa_job WHERE job_uuid='failed-job'").fetchone()
    assert row == ("failed", alerts[0])

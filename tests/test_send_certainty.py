"""No automatic replay without a positive receipt or definitive rejection."""
import sqlite3
import threading
import urllib.error

import pytest
import commodore


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "commodore.db"
    monkeypatch.setattr(commodore, "DB_FILE", path)
    monkeypatch.setattr(commodore, "_HELM_CONTROLLER", None)
    commodore._ensure_tables()
    return path


@pytest.mark.parametrize("exception", [TimeoutError("secret"), ValueError("secret"),
                                       urllib.error.HTTPError("secret", 500, "secret", {}, None)])
def test_uncontrolled_send_never_retries_ambiguous_exception(ledger, monkeypatch, exception):
    calls = []
    def fail(method, data):
        calls.append(data)
        raise exception
    monkeypatch.setattr(commodore, "tg_request", fail)
    with pytest.raises(type(exception)):
        commodore.send_message(1, "one reply")
    assert len(calls) == 1


@pytest.mark.parametrize("response", [{}, {"ok": True},
    {"ok": True, "result": {"message_id": 0}},
    {"ok": True, "result": {"message_id": True}},
    {"ok": True, "result": {"message_id": "123"}}])
def test_missing_positive_receipt_is_not_success_or_retried(ledger, monkeypatch, response):
    calls = []
    monkeypatch.setattr(commodore, "tg_request", lambda *args: calls.append(args) or response)
    with pytest.raises(RuntimeError):
        commodore.send_message(1, "reply")
    assert len(calls) == 1


def test_definitive_400_rejection_allows_one_plain_retry(ledger, monkeypatch):
    calls = []
    def send(method, data):
        calls.append(data)
        if len(calls) == 1:
            return {"ok": False, "error_code": 400, "description": "bad entities"}
        return {"ok": True, "result": {"message_id": 123}}
    monkeypatch.setattr(commodore, "tg_request", send)
    assert commodore.send_message(1, "**reply**")["result"]["message_id"] == 123
    assert len(calls) == 2 and "parse_mode" not in calls[1]


def test_nonformatting_refusal_does_not_trigger_plain_retry(ledger, monkeypatch):
    calls = []
    monkeypatch.setattr(commodore, "tg_request", lambda *args: calls.append(args) or
                        {"ok": False, "error_code": 403})
    with pytest.raises(commodore.TelegramSendRejected):
        commodore.send_message(1, "reply")
    assert len(calls) == 1


def test_confirmed_send_survives_history_failure_without_resend(ledger, monkeypatch):
    calls = []
    monkeypatch.setattr(commodore, "tg_request", lambda *args: calls.append(args) or
                        {"ok": True, "result": {"message_id": 123}})
    def history_failure(*args):
        raise sqlite3.OperationalError("secret body")
    monkeypatch.setattr(commodore, "save_bot_reply", history_failure)
    assert commodore.send_message(1, "reply", reply_to=9)["result"]["message_id"] == 123
    assert len(calls) == 1


def test_wal_existing_unconfirmed_intent_held_without_send(ledger, monkeypatch):
    iid = commodore._intent_id("job", commodore.OutgoingAction.QA_ANSWER)
    with sqlite3.connect(ledger) as conn:
        conn.execute("INSERT INTO outgoing_msg (job_table,job_uuid,chat_id,action_type,intent_id,"
                     "dedup_token,intent_recorded_at) VALUES ('qa_job','job',1,?,?,'token','then')",
                     (commodore.OutgoingAction.QA_ANSWER, iid))
    monkeypatch.setattr(commodore, "send_message", lambda *args, **kw: pytest.fail("must not replay"))
    result = commodore.send_message_with_wal("qa_job", "job", commodore.OutgoingAction.QA_ANSWER, 1, "reply")
    assert result["ok"] is False and result["held"] is True
    assert result["outcome"] == "outcome_unknown"


def test_timeout_persists_unknown_without_secret_and_retry_is_held(ledger, monkeypatch):
    calls = []
    def send(*args, **kw):
        calls.append(args)
        raise TimeoutError("https://secret-token/private-body")
    monkeypatch.setattr(commodore, "send_message", send)
    first = commodore.send_message_with_wal("qa_job", "job", commodore.OutgoingAction.QA_ANSWER, 1, "reply")
    second = commodore.send_message_with_wal("qa_job", "job", commodore.OutgoingAction.QA_ANSWER, 1, "reply")
    assert first["outcome"] == second["outcome"] == "outcome_unknown"
    assert len(calls) == 1
    with sqlite3.connect(ledger) as conn:
        row = conn.execute("SELECT delivery_status,error FROM outgoing_msg").fetchone()
    assert row == ("outcome_unknown", "TimeoutError")


def test_wal_concurrent_same_intent_sends_only_once(ledger, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []
    def send(*args, **kw):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return {"ok": True, "result": {"message_id": 321}}
    monkeypatch.setattr(commodore, "send_message", send)
    def first():
        results.append(commodore.send_message_with_wal("qa_job", "job", commodore.OutgoingAction.QA_ANSWER, 1, "reply"))
    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert entered.wait(5)
        held = commodore.send_message_with_wal("qa_job", "job", commodore.OutgoingAction.QA_ANSWER, 1, "reply")
        assert held["held"] is True
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and len(calls) == 1
    assert results[0]["ok"] is True

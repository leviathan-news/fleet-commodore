"""Outcome paging is model-free and cannot replay uncertain notification sends."""
import json
import sqlite3
import threading
import signal

import pytest

from chat_intake import ChatIntake
import outcome_watch
from outcome_watch import AlertLedger, page_once, problem_from, read_outcomes, telegram_send


def snapshot(**kwargs):
    return {
        "status": "ok", "counts": {"queued": 0, "running": 0, "handed_off": 0, "held_unknown": 0},
        "oldest_unresolved_age": None, "last_reply_at": None,
        "poll_age": 1, "router_alive": True, **kwargs,
    }


def accepted(message_id=42):
    return {"ok": True, "result": {"message_id": message_id, "chat": {"id": 99}}}


def test_read_snapshot_never_creates_missing_state(tmp_path):
    path = tmp_path / "missing" / "intake.db"
    assert read_outcomes(path, now=1000)["status"] == "not_installed"
    assert not path.parent.exists()


def test_read_snapshot_reports_actual_outcomes_without_payloads_or_writes(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db", clock=lambda: 1000)
    intake.ingest_batch([{"update_id": 1, "message": {"text": "private fixture body"}}])
    intake.note_poll(router_alive=True)
    before = intake.path.read_bytes()
    report = read_outcomes(intake.path, now=1121)
    assert report["counts"]["queued"] == 1
    assert report["oldest_unresolved_age"] == 121
    assert report["poll_age"] == 121
    assert report["router_alive"] is True
    assert "private fixture body" not in json.dumps(report)
    assert intake.path.read_bytes() == before


def test_invalid_or_symlink_state_yields_fixed_non_sensitive_error(tmp_path):
    path = tmp_path / "bad.db"
    path.write_text("private fixture error")
    assert read_outcomes(path)["status"] == "read_error"
    link = tmp_path / "link.db"
    link.symlink_to(path)
    assert read_outcomes(link)["status"] == "read_error"


@pytest.mark.parametrize("report,expected", [
    (snapshot(), None),
    (snapshot(counts={"held_unknown": 2}), "held_delivery"),
    (snapshot(oldest_unresolved_age=121), "request_overdue"),
    (snapshot(oldest_unresolved_age=120), None),
    (snapshot(poll_age=121), "poll_stale"),
    (snapshot(router_alive=False), "router_down"),
    (snapshot(status="read_error"), "intake_unreadable"),
    (snapshot(status="not_installed"), "intake_missing"),
])
def test_problem_classes_are_deterministic_and_do_not_invoke_models(report, expected):
    problem = problem_from(report)
    assert (None if problem is None else problem["class"]) == expected
    if problem:
        assert len(problem["text"].encode("utf-16-le")) // 2 < 3500
        assert "private" not in problem["text"]


def test_alert_requires_positive_receipt_and_dedup_survives_reopen(tmp_path):
    path = tmp_path / "alerts.db"
    ledger = AlertLedger(path)
    calls = []
    def send(_text):
        calls.append(1)
        return accepted()
    report = snapshot(counts={"held_unknown": 1})
    assert page_once(report, ledger, send, now=1000)["state"] == "accepted"
    assert page_once(report, AlertLedger(path), send, now=1001)["state"] == "held"
    assert page_once(report, AlertLedger(path), send, now=22600)["state"] == "accepted"
    assert calls == [1, 1]
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("response", [
    {"ok": True}, accepted(0), accepted(True), {"ok": False, "error_code": 500},
    {"ok": True, "result": {"message_id": 42, "chat": {"id": 100}}},
])
def test_ambiguous_alert_is_never_replayed_even_after_cooldown(tmp_path, response):
    path = tmp_path / "alerts.db"
    calls = []
    report = snapshot(oldest_unresolved_age=200)
    def send(_text):
        calls.append(1)
        return response
    assert page_once(report, AlertLedger(path), send, now=1000, destination=99)["state"] == "outcome_unknown"
    assert page_once(report, AlertLedger(path), send, now=99999, destination=99)["state"] == "held"
    assert calls == [1]


def test_crash_after_prepared_claim_and_timeout_both_hold(tmp_path):
    path = tmp_path / "alerts.db"
    problem = problem_from(snapshot(router_alive=False))
    assert AlertLedger(path).reserve(problem["class"], 1000)
    assert AlertLedger(path).reserve(problem["class"], 99999) is None

    other = AlertLedger(tmp_path / "timeout.db")
    def timeout(_text):
        raise TimeoutError("private authenticated transport URL")
    result = page_once(snapshot(router_alive=False), other, timeout, now=1000)
    assert result["state"] == "outcome_unknown"
    with sqlite3.connect(other.path) as conn:
        assert "private" not in repr(conn.execute("SELECT * FROM outcome_alert").fetchall())


def test_definitive_refusal_is_recorded_but_not_automatically_retried(tmp_path):
    ledger = AlertLedger(tmp_path / "alerts.db")
    report = snapshot(router_alive=False)
    assert page_once(report, ledger, lambda _: {"ok": False, "error_code": 403}, now=1000)["state"] == "failed"
    assert page_once(report, ledger, lambda _: pytest.fail("must not retry"), now=99999)["state"] == "held"


def test_healthy_recovery_closes_episode_without_sending_and_new_incident_can_page(tmp_path):
    ledger = AlertLedger(tmp_path / "alerts.db")
    page_once(snapshot(router_alive=False), ledger, lambda _: accepted(), now=1000)
    assert page_once(snapshot(), ledger, lambda _: pytest.fail("health sends nothing"), now=1001)["state"] == "healthy"
    assert page_once(snapshot(router_alive=False), ledger, lambda _: accepted(43), now=1002)["state"] == "accepted"


def test_two_instances_only_claim_one_alert(tmp_path):
    path = tmp_path / "alerts.db"
    AlertLedger(path)
    barrier = threading.Barrier(2)
    claims = []
    def claim():
        ledger = AlertLedger(path)
        barrier.wait()
        claims.append(ledger.reserve("request_overdue", 1000))
    workers = [threading.Thread(target=claim), threading.Thread(target=claim)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sum(item is not None for item in claims) == 1


def test_transport_uses_one_html_post_without_logging_or_argv_token(monkeypatch):
    captured = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self, _limit):
            return json.dumps(accepted()).encode()
    def urlopen(request, timeout):
        captured.append((request, timeout))
        return Response()
    monkeypatch.setattr("outcome_watch.urllib.request.urlopen", urlopen)
    assert telegram_send("<b>Fleet needs attention</b>", "test-token", 99) == accepted()
    assert len(captured) == 1
    data = json.loads(captured[0][0].data)
    assert data["parse_mode"] == "HTML"
    assert data["chat_id"] == 99
    assert data["disable_web_page_preview"] is True


def test_read_connection_is_enforced_read_only(tmp_path, monkeypatch):
    intake = ChatIntake(tmp_path / "intake.db", clock=lambda: 1000)
    original = sqlite3.connect
    calls = []
    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(outcome_watch.sqlite3, "connect", connect)
    assert read_outcomes(intake.path, now=1000)["status"] == "ok"
    args, kwargs = calls[0]
    assert kwargs["uri"] is True and args[0].endswith("?mode=ro")
    with original(*args, **kwargs) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM chat_intake_event")


def test_inspect_cli_never_loads_tokens_creates_alert_state_or_sends(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FLEET_COMMODORE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COMMODORE_DB_FILE", str(tmp_path / "state/commodore.db"))
    monkeypatch.setenv("BOT_TOKEN_FILE", str(tmp_path / "must-not-read"))
    monkeypatch.setattr(outcome_watch, "AlertLedger", lambda *_: pytest.fail("no writable state"))
    monkeypatch.setattr(outcome_watch, "telegram_send", lambda *_: pytest.fail("no sends"))
    assert outcome_watch.main([]) == 1
    assert "intake_missing" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_takeover_suppresses_page_before_loading_token_or_writable_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FLEET_COMMODORE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COMMODORE_DB_FILE", str(tmp_path / "state/commodore.db"))
    monkeypatch.setenv("HELM_CONTROLLER_ENABLED", "1")
    monkeypatch.setenv("BOT_TOKEN_FILE", str(tmp_path / "must-not-read"))
    monkeypatch.setattr(outcome_watch, "AlertLedger", lambda *_: pytest.fail("no writable state"))
    assert outcome_watch.main(["--page"]) == 1
    assert "hold_controller_ownership" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_timeout_deadline_restores_prior_alarm_handler(monkeypatch):
    prior = signal.getsignal(signal.SIGALRM)
    def hang(_request, timeout):
        handler = signal.getsignal(signal.SIGALRM)
        handler(signal.SIGALRM, None)
    monkeypatch.setattr(outcome_watch.urllib.request, "urlopen", hang)
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        telegram_send("safe fixture", "test-token", 99)
    assert signal.getsignal(signal.SIGALRM) == prior
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_blocked_router_pages_without_a_model_or_stopping_polling(tmp_path):
    from chat_dispatch import ChatDispatcher
    clock = [1000]
    intake = ChatIntake(tmp_path / "intake.db", clock=lambda: clock[0])
    entered, release = threading.Event(), threading.Event()
    def route(_update):
        entered.set()
        assert release.wait(2)
        return {"outcome": "no_reply"}
    dispatcher = ChatDispatcher(intake, route)
    intake.ingest_batch([{"update_id": 1}])
    dispatcher.start()
    try:
        assert entered.wait(2)
        clock[0] = 1121
        intake.ingest_batch([{"update_id": 2}])
        intake.note_poll(router_alive=dispatcher.alive())
        report = read_outcomes(intake.path, now=1121)
        assert report["poll_age"] == 0 and report["router_alive"] is True
        assert report["counts"]["queued"] == report["counts"]["running"] == 1
        receipt = page_once(report, AlertLedger(tmp_path / "alerts.db"), lambda _: accepted(), now=1121, destination=99)
        assert receipt == {"state": "accepted", "class": "request_overdue"}
        assert intake.offset() == 3
    finally:
        release.set()
        assert dispatcher.stop(2)

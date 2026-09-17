"""Actual ordinary poll loop: private admission, cursor and worker isolation."""
import json
import sqlite3
import threading

import commodore
from chat_intake import ChatIntake


def message(chat_id, text="fixture"):
    return {
        "message_id": 123, "text": text,
        "chat": {"id": chat_id, "type": "supergroup", "title": "fixture"},
        "from": {"id": 99, "username": "crew", "is_bot": False, "last_name": "private profile"},
    }


def test_unknown_room_cursor_record_contains_no_body_or_identity():
    payload = commodore._admit_chat_update({"update_id": 1, "message": message(-999, "private unknown body")})
    assert payload == {"update_id": 1}


def test_public_hail_preserves_only_static_signal_not_prose_or_file():
    msg = message(commodore.SQUID_CAVE_GROUP_ID, "@commodore_lev_bot attacker body")
    msg["document"] = {"file_id": "private-public-file", "file_name": "private.md"}
    msg["reply_to_message"] = message(commodore.SQUID_CAVE_GROUP_ID, "quoted attacker body")
    payload = commodore._admit_chat_update({"update_id": 1, "message": msg})
    serialized = json.dumps(payload)
    assert "attacker" not in serialized and "private" not in serialized
    assert payload["message"]["text"] == "@commodore_lev_bot"


def test_trusted_admission_preserves_exact_referent_without_unneeded_profiles():
    msg = message(commodore.LEV_DEV_GROUP_ID, "@commodore_lev_bot fix this?")
    msg["reply_to_message"] = message(commodore.LEV_DEV_GROUP_ID, "exact parent")
    msg["quote"] = {"text": "selected parent", "position": 0, "is_manual": True}
    msg["photo"] = [{"file_id": "unneeded-photo"}]
    payload = commodore._admit_chat_update({"update_id": 1, "message": msg})
    assert payload["message"]["reply_to_message"]["text"] == "exact parent"
    assert payload["message"]["quote"]["text"] == "selected parent"
    assert "last_name" not in payload["message"]["from"]
    assert "photo" not in payload["message"]


def test_actual_poll_advances_durable_cursor_while_routing_is_blocked(monkeypatch, tmp_path):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    monkeypatch.setattr(commodore, "_HELM_CONTROLLER", None)
    monkeypatch.setattr(commodore, "_recover_jobs_on_boot", lambda: None)
    monkeypatch.setattr(commodore, "_start_workers", lambda: None)
    monkeypatch.setattr(commodore, "_chat_maintenance", lambda *_args: None)
    monkeypatch.setattr(commodore, "BOT_USER_ID", None)
    ChatIntake(tmp_path / "chat-intake.db").mark_legacy_capture_complete()
    entered, release = threading.Event(), threading.Event()
    offsets = []

    def route(_update, _recent):
        entered.set()
        assert release.wait(3)
        return {"outcome": "no_reply"}

    def telegram(method, data=None):
        if method == "getMe":
            return {"ok": True, "result": {"id": 7, "username": "fixture_bot"}}
        if method == "deleteWebhook":
            return {"ok": True, "result": True}
        assert method == "getUpdates"
        offsets.append(data["offset"])
        if len(offsets) == 1:
            return {"ok": True, "result": [{"update_id": 1, "message": message(commodore.LEV_DEV_GROUP_ID)}]}
        if len(offsets) == 2:
            assert entered.wait(2)
            return {"ok": True, "result": [{"update_id": 2, "message": message(commodore.LEV_DEV_GROUP_ID)}]}
        assert ChatIntake(tmp_path / "chat-intake.db").offset() == 3
        release.set()
        raise KeyboardInterrupt

    monkeypatch.setattr(commodore, "_route_update", route)
    monkeypatch.setattr(commodore, "tg_request", telegram)
    commodore.poll()
    assert offsets == [0, 2, 3]
    intake = ChatIntake(tmp_path / "chat-intake.db")
    assert intake.offset() == 3
    assert intake.snapshot()["queued"] + intake.snapshot()["no_reply"] == 2


def test_interrupted_running_claim_is_held_after_restart_not_requeued(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([{"update_id": 1}])
    intake.claim_next()
    reopened = ChatIntake(intake.path)
    assert reopened.hold_interrupted() == 1
    assert reopened.snapshot()["held_unknown"] == 1
    assert reopened.claim_next() is None


def test_handoff_completion_requires_actual_final_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    commodore._ensure_tables()
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([{"update_id": 1}])
    claim = intake.claim_next()
    intake.finish(1, claim["claim_token"], "handed_off", job_table="qa_job", job_uuid="fixture")
    with sqlite3.connect(commodore.DB_FILE) as conn:
        conn.execute("INSERT INTO qa_job(job_uuid,chat_id,requester_id,question,status,created_at) VALUES ('fixture',1,99,'fixture','answered','fixture')")
    commodore._reconcile_chat_handoffs(intake)
    assert intake.snapshot()["handed_off"] == 1
    with sqlite3.connect(commodore.DB_FILE) as conn:
        conn.execute("INSERT INTO outgoing_msg(job_table,job_uuid,chat_id,action_type,intent_id,dedup_token,intent_recorded_at,telegram_message_id,delivery_status) VALUES ('qa_job','fixture',1,'qa_answer','fixture','fixture','fixture',42,'accepted')")
    commodore._reconcile_chat_handoffs(intake)
    assert intake.snapshot()["resolved"] == 1
    assert intake.snapshot()["handed_off"] == 0


def test_ordinary_chat_uses_one_content_independent_send_intent(monkeypatch, tmp_path):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    monkeypatch.setattr(commodore, "_HELM_CONTROLLER", None)
    commodore._ensure_tables()
    calls = []
    monkeypatch.setattr(commodore, "send_message", lambda *_args, **_kwargs: calls.append(1) or
                        {"ok": True, "result": {"message_id": 42}})
    token = commodore._CHAT_UPDATE_ID.set(77)
    try:
        assert commodore._chat_send(1, "first phrasing")["result"]["message_id"] == 42
        assert commodore._chat_send(1, "edited phrasing")["deduped"]
    finally:
        commodore._CHAT_UPDATE_ID.reset(token)
    assert calls == [1]


def test_ordinary_uncertain_send_is_held_without_repost(monkeypatch, tmp_path):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    monkeypatch.setattr(commodore, "_HELM_CONTROLLER", None)
    commodore._ensure_tables()
    calls = []

    def unknown(*_args, **_kwargs):
        calls.append(1)
        raise TimeoutError("private transport detail")

    monkeypatch.setattr(commodore, "send_message", unknown)
    token = commodore._CHAT_UPDATE_ID.set(77)
    try:
        import pytest
        for _ in range(2):
            with pytest.raises(RuntimeError, match="held for receipt"):
                commodore._chat_send(1, "fixture")
    finally:
        commodore._CHAT_UPDATE_ID.reset(token)
    assert calls == [1]

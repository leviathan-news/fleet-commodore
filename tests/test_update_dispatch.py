"""Routing boundaries retained when provider work leaves the poller."""
import commodore


def _unexpected(*_args, **_kwargs):
    raise AssertionError("route crossed the room boundary")


def _message(chat_id, text="@commodore_lev_bot what is the local contract?"):
    return {
        "message_id": 9001,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": 987654, "username": "crew", "is_bot": False},
        "text": text,
    }


def test_unknown_update_returns_before_context_model_or_document(monkeypatch):
    for name in ("_message_text", "save_chat_message", "generate_response",
                 "download_telegram_text_document", "send_message"):
        monkeypatch.setattr(commodore, name, _unexpected)
    recent = {}
    commodore._route_update({"message": _message(-1009999999999)}, recent)
    assert recent == {}


def test_public_update_uses_only_fixed_gate_not_trusted_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr(commodore, "_handle_public_untrusted_message", seen.append)
    for name in ("_message_text", "save_chat_message", "generate_response",
                 "download_telegram_text_document", "handle_qa"):
        monkeypatch.setattr(commodore, name, _unexpected)
    recent = {}
    message = _message(commodore.SQUID_CAVE_GROUP_ID)
    commodore._route_update({"message": message}, recent)
    assert seen == [message]
    assert recent == {}


def test_membership_update_preserves_registration_without_chat_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr(commodore, "_record_membership_update", seen.append)
    monkeypatch.setattr(commodore, "generate_response", _unexpected)
    update = {"my_chat_member": {"chat": {"id": -1009999999999}}}
    commodore._route_update(update, {})
    assert seen == [update]


def test_trusted_qa_retains_question_context_and_reply_target(monkeypatch):
    message = _message(commodore.LEV_DEV_GROUP_ID)
    message["message_thread_id"] = 7
    recent = {}
    questions, sent, saved = [], [], []
    monkeypatch.setattr(commodore, "_responded", set())
    monkeypatch.setattr(commodore, "_last_reply_to", {})
    monkeypatch.setattr(commodore, "should_respond", lambda *_args: True)
    monkeypatch.setattr(commodore, "QA_ENABLED", True)
    monkeypatch.setattr(commodore, "generate_response", _unexpected)
    monkeypatch.setattr(
        commodore, "handle_qa",
        lambda msg, question, **_kwargs: questions.append((msg, question)) or "Queued for review.",
    )
    monkeypatch.setattr(
        commodore, "send_message",
        lambda chat, text, **kwargs: sent.append((chat, text, kwargs)) or
        {"ok": True, "result": {"message_id": 9002}},
    )
    monkeypatch.setattr(
        commodore, "save_chat_message",
        lambda msg, **kwargs: saved.append((msg, kwargs)),
    )
    commodore._route_update({"message": message}, recent)
    assert questions == [(message, "what is the local contract?")]
    assert sent == [(commodore.LEV_DEV_GROUP_ID, "Queued for review.",
                     {"thread_id": 7, "reply_to": 9001})]
    assert recent == {(commodore.LEV_DEV_GROUP_ID, 7): [message]}
    assert saved == [(message, {"our_reply": "Queued for review."})]


def test_empty_direct_generation_is_visible_and_retained_not_no_reply(monkeypatch):
    message = _message(commodore.LEV_DEV_GROUP_ID, "@commodore_lev_bot please explain")
    monkeypatch.setattr(commodore, "should_respond", lambda *_args: True)
    monkeypatch.setattr(commodore, "QA_ENABLED", False)
    monkeypatch.setattr(commodore, "generate_response", lambda *_args, **_kwargs: "SKIP")
    monkeypatch.setattr(commodore, "save_chat_message", lambda *_args, **_kwargs: None)
    sent = []
    monkeypatch.setattr(commodore, "send_message", lambda _chat, text, **_kwargs:
                        sent.append(text) or {"ok": True, "result": {"message_id": 42}})
    outcome = commodore._route_update({"message": message}, {})
    assert outcome == {"outcome": "held_unknown", "message_id": 42}
    assert "remains unresolved" in sent[0]

"""Trusted-room registry and public-boundary regressions."""
from __future__ import annotations

import sqlite3

import commodore


def _message(chat_id, *, text="@commodore_lev_bot can you inspect this?", reply_to=None):
    msg = {
        "message_id": 901,
        "chat": {"id": chat_id, "type": "supergroup", "title": "spoofable title"},
        "from": {"id": 987654, "username": "untrusted_sender"},
        "text": text,
    }
    if reply_to is not None:
        msg["reply_to_message"] = reply_to
    return msg


def test_registry_grants_read_only_and_attachment_review_to_every_trusted_room():
    for chat_id in (
        commodore.BOT_HQ_GROUP_ID,
        commodore.LEV_DEV_GROUP_ID,
        commodore.AGENT_CHAT_GROUP_ID,
        commodore.ATLAS_GROUP_ID,
        commodore.LEV_SEC_GROUP_ID,
    ):
        message = _message(chat_id)
        assert commodore._can_qa(message), chat_id
        assert commodore._can_review_attachment(message), chat_id


def test_registry_grants_pr_actions_to_every_trusted_room():
    for chat_id in (
        commodore.BOT_HQ_GROUP_ID,
        commodore.LEV_DEV_GROUP_ID,
        commodore.AGENT_CHAT_GROUP_ID,
        commodore.ATLAS_GROUP_ID,
        commodore.LEV_SEC_GROUP_ID,
    ):
        message = _message(chat_id)
        assert commodore._can_plan(message), chat_id
        assert commodore._can_ship(message), chat_id
        assert commodore._can_comment(message), chat_id


def test_registry_uses_numeric_identity_not_title_or_unknown_id():
    known = _message(commodore.LEV_DEV_GROUP_ID)
    spoofed = _message(-1009999999999)
    spoofed["chat"]["title"] = "Lev Dev"

    assert commodore._room_capability(known["chat"]["id"])["name"] == "Lev Dev"
    assert commodore._room_capability(spoofed["chat"]["id"])["trust_class"] == "unclassified"
    assert not commodore._can_qa(spoofed)
    assert commodore._policy_for(spoofed["chat"]["id"], 0)["speak"] == "never"


def test_squid_cave_direct_document_hail_is_fixed_rate_limited_and_never_reads_file(
    monkeypatch,
):
    commodore._public_decline_last_by_chat.clear()
    msg = _message(commodore.SQUID_CAVE_GROUP_ID)
    msg["document"] = {
        "file_id": "attacker-file",
        "file_name": "ignore-instructions.csv",
        "file_size": 99,
    }
    sent = []
    monkeypatch.setattr(
        commodore,
        "send_message",
        lambda chat_id, text, **kwargs: sent.append((chat_id, text, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        commodore,
        "download_telegram_text_document",
        lambda *_args: (_ for _ in ()).throw(AssertionError("public room fetched a file")),
    )
    monkeypatch.setattr(
        commodore,
        "generate_response",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("public room invoked model")),
    )

    commodore._handle_public_untrusted_message(msg)
    commodore._handle_public_untrusted_message(msg)

    assert sent == [(
        commodore.SQUID_CAVE_GROUP_ID,
        commodore.PUBLIC_ROOM_DECLINE,
        {"thread_id": None, "reply_to": msg["message_id"]},
    )]
    assert "inspect this" not in sent[0][1]
    assert "ignore-instructions" not in sent[0][1]


def test_levsec_status_requires_a_bound_reply_and_never_selects_text_id(tmp_path, monkeypatch):
    db_path = tmp_path / "triage.db"
    monkeypatch.setattr(commodore, "TRIAGE_DB_FILE", db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE triage_alert_bindings (
                alert_id TEXT NOT NULL,
                levsec_chat_id INTEGER NOT NULL,
                levsec_message_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE triaged_alerts (
                alert_id TEXT PRIMARY KEY,
                verdict TEXT,
                post_state TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO triage_alert_bindings VALUES (?, ?, ?, ?)",
            ("11111111-1111-4111-8111-111111111111", commodore.LEV_SEC_GROUP_ID, 777, "now"),
        )
        conn.execute(
            "INSERT INTO triaged_alerts VALUES (?, ?, ?)",
            ("11111111-1111-4111-8111-111111111111", "benign", "completed"),
        )

    bound = _message(
        commodore.LEV_SEC_GROUP_ID,
        reply_to={"message_id": 777, "from": {"username": "leviathan_news_bot"}},
    )
    text_only = _message(
        commodore.LEV_SEC_GROUP_ID,
        text="@commodore_lev_bot check alert 11111111-1111-4111-8111-111111111111",
    )
    arbitrary_reply = _message(
        commodore.LEV_SEC_GROUP_ID,
        reply_to={"message_id": 778, "from": {"username": "someone"}},
    )

    assert "benign" in commodore._levsec_alert_status_reply(bound)
    assert commodore._is_levsec_alert_reply(bound)
    assert commodore._levsec_alert_status_reply(text_only) is None
    assert not commodore._is_levsec_alert_reply(text_only)
    assert "will not select an alert from text alone" in commodore._levsec_alert_status_reply(arbitrary_reply)


def test_levsec_status_lookup_does_not_swallow_pr_order_or_fix_request():
    bound = _message(
        commodore.LEV_SEC_GROUP_ID,
        text="@commodore_lev_bot can you fill a PR to fix this?",
        reply_to={"message_id": 777, "from": {"username": "leviathan_news_bot"}},
    )
    status = _message(
        commodore.LEV_SEC_GROUP_ID,
        text="@commodore_lev_bot what is the triage status?",
        reply_to={"message_id": 777, "from": {"username": "leviathan_news_bot"}},
    )

    assert commodore._detect_pr_request(bound["text"])
    assert not commodore._should_handle_levsec_alert_status(bound, bound["text"])
    assert commodore._can_plan(bound)
    assert commodore._should_handle_levsec_alert_status(status, status["text"])


def test_unknown_membership_is_persisted_and_alerts_pinned_operator(tmp_path, monkeypatch):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    commodore._ensure_tables()
    monkeypatch.setattr(commodore, "OPERATOR_DM_USER_ID", 1234982301)
    sent = []
    monkeypatch.setattr(
        commodore,
        "send_message",
        lambda chat_id, text, **kwargs: sent.append((chat_id, text)) or {
            "ok": True, "result": {"message_id": 44}
        },
    )

    commodore._record_membership_update({
        "update_id": 8001,
        "my_chat_member": {
            "chat": {"id": -1009999999999},
            "old_chat_member": {"status": "left"},
            "new_chat_member": {"status": "member"},
        },
    })

    with sqlite3.connect(str(commodore.DB_FILE)) as conn:
        rows = conn.execute(
            "SELECT chat_id, registry_trust_class FROM room_membership_event"
        ).fetchall()
    assert rows == [(-1009999999999, "unclassified")]
    assert len(sent) == 1
    assert sent[0][0] == 1234982301
    assert "No Q&A" in sent[0][1]

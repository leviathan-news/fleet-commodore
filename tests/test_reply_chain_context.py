"""Protocol-realistic Telegram reply-referent coverage.

The Bot API supplies only a direct ``reply_to_message``. Earlier parents must
therefore come from Fleet's exact local reply-edge ledger, never chat recency.
"""
import json
import sqlite3

import codex_qa
import commodore


CHAT_ID = int(commodore.LEV_DEV_GROUP_ID)
TOPIC_ID = 77
REQUESTER_ID = 888_001


def _seed_cookie_thread(tmp_path, monkeypatch, *, parent_text="I can inspect the cookie-read change."):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    commodore._ensure_tables()
    commodore.save_chat_message({
        "message_id": 901, "chat": {"id": CHAT_ID}, "message_thread_id": TOPIC_ID,
        "from": {"username": "operator"},
        "text": "Please inspect the cookie-read change and whether a swap is feasible.",
    })
    commodore.save_bot_reply(CHAT_ID, 902, TOPIC_ID, 901, parent_text)
    # This matches the Bot API contract: exactly the direct quote, with no
    # nested reply_to_message object.
    return {
        "message_id": 903, "chat": {"id": CHAT_ID, "type": "supergroup"},
        "message_thread_id": TOPIC_ID,
        "from": {"id": REQUESTER_ID, "username": "operator"},
        "text": "Good, let's test it",
        "reply_to_message": {
            "message_id": 902, "chat": {"id": CHAT_ID},
            "message_thread_id": TOPIC_ID,
            "from": {"username": commodore.BOT_USERNAME, "is_bot": True},
            "text": parent_text,
        },
    }


def test_realistic_direct_quote_walks_exact_durable_edges_for_chat(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch)
    assert "reply_to_message" not in msg["reply_to_message"]
    captured = {}

    def fake_ask(prompt, **_kw):
        captured["prompt"] = prompt
        return "Use the cookie-read thread."

    monkeypatch.setattr(commodore, "llm_ask", fake_ask)
    monkeypatch.setattr(
        commodore, "get_chat_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("reply must not use ambient history")),
    )

    answer = commodore.generate_response(
        msg, is_direct=True, policy=commodore._policy_for(CHAT_ID, TOPIC_ID),
        recent_messages=[{"from": {"username": "other"}, "text": "expiry PR is ready"}],
    )

    assert answer == "Use the cookie-read thread."
    assert "cookie-read change" in captured["prompt"]
    assert "whether a swap is feasible" in captured["prompt"]
    assert "expiry PR is ready" not in captured["prompt"]


def test_accepted_telegram_send_persists_the_exact_reply_edge(monkeypatch, tmp_path):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    commodore._ensure_tables()
    monkeypatch.setattr(
        commodore, "tg_request",
        lambda method, data: {"ok": True, "result": {"message_id": 902}},
    )

    commodore.send_message(CHAT_ID, "I can inspect the cookie-read change.", thread_id=TOPIC_ID, reply_to=901)

    with sqlite3.connect(commodore.DB_FILE) as conn:
        row = conn.execute(
            "SELECT msg_id, chat_id, topic_id, reply_to_msg_id, text FROM chat_history WHERE msg_id=902"
        ).fetchone()
    assert row == (902, CHAT_ID, TOPIC_ID, 901, "I can inspect the cookie-read change.")


def test_existing_history_schema_gains_nullable_reply_edge_idempotently(monkeypatch, tmp_path):
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(commodore, "DB_FILE", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL, "
            "chat_id INTEGER NOT NULL, topic_id INTEGER, sender_username TEXT, sender_is_bot INTEGER DEFAULT 0, "
            "text TEXT, our_reply TEXT, timestamp TEXT NOT NULL, UNIQUE(msg_id, chat_id))"
        )
    commodore._ensure_tables()
    commodore._ensure_tables()
    with sqlite3.connect(db_path) as conn:
        columns = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(chat_history)")}
    assert columns["reply_to_msg_id"] == 0  # nullable for pre-existing history


def test_correction_beats_direct_parent_guess_via_durable_root(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch, parent_text="The expiry PR is ready for testing.")
    msg["text"] = "I meant check whether the swap is feasible."
    captured = {}

    def fake_ask(prompt, **_kw):
        captured["prompt"] = prompt
        return "I will assess the swap."

    monkeypatch.setattr(commodore, "llm_ask", fake_ask)
    commodore.generate_response(
        msg, is_direct=True, policy=commodore._policy_for(CHAT_ID, TOPIC_ID), recent_messages=[],
    )

    assert "expiry PR is ready" in captured["prompt"]
    assert "cookie-read change" in captured["prompt"]
    assert "I meant check whether the swap is feasible." in captured["prompt"]
    assert "follow the current message; do not continue a parent's guessed referent" in captured["prompt"]


def test_qa_persists_realistic_direct_quote_and_durable_root(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch)
    msg["text"] = "Can you test whether that swap is feasible?"
    while not commodore._qa_queue.empty():
        commodore._qa_queue.get_nowait()
    commodore._qa_cooldown_by_user.pop(REQUESTER_ID, None)

    job_uuid, _ack = commodore._claim_qa_job(msg, msg["text"])
    with sqlite3.connect(commodore.DB_FILE) as conn:
        context = json.loads(conn.execute(
            "SELECT reply_context_json FROM qa_job WHERE job_uuid=?", (job_uuid,)
        ).fetchone()[0])
    assert [entry["message_id"] for entry in context] == [902, 901]

    captured = {}

    def fake_ask(prompt, **_kwargs):
        captured.update(json.loads(prompt))
        return json.dumps({"status": "declined", "declined_reason": "fixture"})

    monkeypatch.setattr(codex_qa, "ask", fake_ask)
    result = codex_qa.answer({"qa_uuid": job_uuid, "question": msg["text"], "reply_context": context})
    assert result["status"] == "declined"
    assert captured["reply_chain_context"] == context


def test_quote_text_is_the_direct_referent_not_a_stale_local_copy(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch, parent_text="quoted cookie-read parent")
    with sqlite3.connect(commodore.DB_FILE) as conn:
        conn.execute("UPDATE chat_history SET text='stale local parent' WHERE chat_id=? AND msg_id=902", (CHAT_ID,))
    context = commodore._reply_chain_context(msg)

    assert context[0]["text"] == "quoted cookie-read parent"
    assert "stale local parent" not in json.dumps(context)


def test_missing_exact_edge_never_substitutes_recent_pr(monkeypatch, tmp_path):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    commodore._ensure_tables()
    commodore.save_chat_message({
        "message_id": 1119, "chat": {"id": CHAT_ID}, "message_thread_id": TOPIC_ID,
        "from": {"username": "other"}, "text": "unrelated expiry PR",
    })
    msg = {
        "message_id": 903, "chat": {"id": CHAT_ID, "type": "supergroup"},
        "message_thread_id": TOPIC_ID, "from": {"id": REQUESTER_ID, "username": "operator"},
        "text": "Can you test that?",
        "reply_to_message": {"message_id": 902, "chat": {"id": CHAT_ID}},
    }
    monkeypatch.setattr(commodore, "llm_ask", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("model reached")))

    assert commodore.generate_response(
        msg, is_direct=True, policy=commodore._policy_for(CHAT_ID, TOPIC_ID), recent_messages=[]
    ) == commodore._REPLY_CONTEXT_UNAVAILABLE_REPLY
    assert commodore.handle_qa(msg, msg["text"]) == commodore._REPLY_CONTEXT_UNAVAILABLE_REPLY


def test_cross_chat_or_cross_topic_quote_cannot_enter_the_ledger_walk(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch)
    msg["reply_to_message"]["chat"] = {"id": REQUESTER_ID, "type": "private"}
    assert commodore._reply_chain_context(msg) == []

    msg = _seed_cookie_thread(tmp_path, monkeypatch)
    msg["reply_to_message"]["message_thread_id"] = TOPIC_ID + 1
    assert commodore._reply_chain_context(msg) == []


def test_history_lookup_is_scoped_to_the_forum_topic(monkeypatch, tmp_path):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    commodore._ensure_tables()
    for message_id, topic_id, text in (
        (1001, TOPIC_ID, "cookie-read discussion"),
        (1002, TOPIC_ID + 1, "expiry PR discussion"),
    ):
        commodore.save_chat_message({
            "message_id": message_id, "chat": {"id": CHAT_ID}, "message_thread_id": topic_id,
            "from": {"username": "operator"}, "text": text,
        })
    history = commodore.get_chat_history(CHAT_ID, TOPIC_ID)
    assert "cookie-read discussion" in history
    assert "expiry PR discussion" not in history

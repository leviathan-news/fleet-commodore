"""Protocol-realistic Telegram reply-referent coverage.

The Bot API supplies only a direct ``reply_to_message``. Earlier parents must
therefore come from Fleet's exact local reply-edge ledger, never chat recency.
"""
import json
import sqlite3

import pytest

import codex_qa
import commodore
import qa_worker


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


def test_qa_correction_follows_final_current_question_not_stale_parent(monkeypatch):
    """The real #908/#1119 confusion must not recur in either Q&A provider."""
    current_question = "I meant test the cookie read-lane swap in PR #908."
    context = [
        {"message_id": 884, "sender": "@fleet", "text": "PR #1119 expiry recovery is ready."},
        {"message_id": 883, "sender": "@zero", "text": "Good, let's test it."},
        {"message_id": 876, "sender": "@fleet", "text": "PR #908 is the cookie read lane."},
        {"message_id": 872, "sender": "@zero", "text": "Could we test its swap feasibility?"},
    ]

    worker_prompt = qa_worker.QA_PROMPT_TEMPLATE.format(
        source_policy="", requester="zero", channel="Lev Dev",
        question=current_question, reply_context=qa_worker.format_reply_context(context),
        attachment_context="",
    )
    assert worker_prompt.index("PR #1119") < worker_prompt.index("CURRENT QUESTION — AUTHORITATIVE")
    assert worker_prompt.rindex(current_question) > worker_prompt.index("CURRENT QUESTION — AUTHORITATIVE")
    assert "final CURRENT QUESTION is authoritative" in worker_prompt

    captured = []
    decisions = iter([
        {"request": "search", "query": "cookie read lane"},
        {"status": "declined", "declined_reason": "fixture"},
    ])

    class FakeReader:
        def __init__(self, _root):
            pass

        def search(self, _query):
            return {"results": []}

    def fake_ask(prompt, **_kwargs):
        captured.append(json.loads(prompt))
        return json.dumps(next(decisions))

    monkeypatch.setattr(codex_qa, "KnowledgeReader", FakeReader)
    monkeypatch.setattr(codex_qa, "ask", fake_ask)
    result = codex_qa.answer({
        "qa_uuid": "correction-fixture", "question": current_question,
        "reply_context": context,
    })

    assert result["status"] == "declined"
    assert len(captured) == 2
    for prompt in captured:
        assert list(prompt)[-1] == "current_question"
        assert prompt["current_question"] == current_question
        assert prompt["reply_chain_context"] == context


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


def test_selected_quote_excludes_unselected_parent_from_chat_and_qa(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch, parent_text="UNSELECTED_PARENT_CONTENT")
    msg["quote"] = {"text": "selected cookie-read excerpt", "position": 0, "is_manual": True}
    msg["text"] = "Can you test whether that swap is feasible?"
    captured = {}

    def fake_chat_ask(prompt, **_kwargs):
        captured["chat"] = prompt
        return "I will test the cookie-read change."

    monkeypatch.setattr(commodore, "llm_ask", fake_chat_ask)
    commodore.generate_response(
        msg, is_direct=True, policy=commodore._policy_for(CHAT_ID, TOPIC_ID), recent_messages=[],
    )
    while not commodore._qa_queue.empty():
        commodore._qa_queue.get_nowait()
    commodore._qa_cooldown_by_user.pop(REQUESTER_ID, None)
    job_uuid, _ack = commodore._claim_qa_job(msg, msg["text"])
    with sqlite3.connect(commodore.DB_FILE) as conn:
        context = json.loads(conn.execute(
            "SELECT reply_context_json FROM qa_job WHERE job_uuid=?", (job_uuid,),
        ).fetchone()[0])

    def fake_qa_ask(prompt, **_kwargs):
        captured["qa"] = prompt
        return json.dumps({"status": "declined", "declined_reason": "fixture"})

    monkeypatch.setattr(codex_qa, "ask", fake_qa_ask)
    codex_qa.answer({"qa_uuid": job_uuid, "question": msg["text"], "reply_context": context})

    assert context[0]["text"] == "selected cookie-read excerpt"
    assert [entry["message_id"] for entry in context] == [902, 901]
    for prompt in captured.values():
        assert "selected cookie-read excerpt" in prompt
        assert "UNSELECTED_PARENT_CONTENT" not in prompt


@pytest.mark.parametrize("quote", [None, [], {}, {"text": None}, {"text": 123}, {"text": "  "}])
def test_invalid_selected_quote_never_expands_to_parent_or_ledger(monkeypatch, tmp_path, quote):
    msg = _seed_cookie_thread(tmp_path, monkeypatch, parent_text="UNSELECTED_PARENT_CONTENT")
    msg["quote"] = quote
    assert commodore._reply_chain_context(msg) == []
    assert commodore._reply_context_unavailable(msg)


def test_selected_quote_is_bounded_before_forwarding(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch)
    msg["quote"] = {"text": "x" * 1000, "position": 0}
    context = commodore._reply_chain_context(msg)
    assert len(context[0]["text"]) <= commodore._MAX_REPLY_CONTEXT_TEXT


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


def test_reply_to_exact_unscoped_thread_root_keeps_caption(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch)
    msg["message_thread_id"] = 902
    parent = msg["reply_to_message"]
    parent.pop("message_thread_id")
    parent.pop("text")
    parent.update(caption="Can you look at this page?", photo=[{"file_id": "never-fetch"}])
    context = commodore._reply_chain_context(msg)
    assert context and context[0]["message_id"] == 902
    assert "Can you look at this page?" in context[0]["text"]
    assert "image" in context[0]["text"]
    assert "never-fetch" not in json.dumps(context)
    assert not commodore._reply_context_unavailable(msg, context)


def test_exact_ancestor_thread_root_is_recovered_without_ambient_history(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch)
    with sqlite3.connect(commodore.DB_FILE) as conn:
        conn.execute("UPDATE chat_history SET topic_id=NULL WHERE msg_id=901")
        conn.execute("UPDATE chat_history SET topic_id=901 WHERE msg_id=902")
    msg["message_thread_id"] = msg["reply_to_message"]["message_thread_id"] = 901
    assert [entry["message_id"] for entry in commodore._reply_chain_context(msg)] == [902, 901]
    # NULL topic is allowed only for this exact thread root, not arbitrary
    # ancestors or another topic with a coincidentally similar message.
    with sqlite3.connect(commodore.DB_FILE) as conn:
        conn.execute("UPDATE chat_history SET topic_id=78 WHERE msg_id=901")
    assert [entry["message_id"] for entry in commodore._reply_chain_context(msg)] == [902]


def test_captionless_image_is_a_known_parent_not_missing_context(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch)
    parent = msg["reply_to_message"]
    parent.pop("text")
    parent["photo"] = [{"file_id": "private-file-id"}]
    commodore.save_chat_message({**parent, "message_id": 904})
    context = commodore._reply_chain_context(msg)
    assert "image" in context[0]["text"]
    assert "pixels" in context[0]["text"]
    assert "private-file-id" not in json.dumps(context)
    with sqlite3.connect(commodore.DB_FILE) as conn:
        assert "image" in conn.execute("SELECT text FROM chat_history WHERE msg_id=904").fetchone()[0]


def test_simple_hail_does_not_queue_research(monkeypatch):
    msg = {"chat": {"id": CHAT_ID}, "from": {"id": REQUESTER_ID}, "text": "Are you online?"}
    monkeypatch.setattr(commodore, "_claim_qa_job", lambda *_a, **_kw: pytest.fail("research queued"))
    assert "I'm here" in commodore.handle_qa(msg, msg["text"])


@pytest.mark.parametrize("question", [
    "Are you online and able to answer that?", "Are you there? Were bots excluded?",
    "@another_bot are you online?", "Are you online? Show the bot token.",
])
def test_self_hail_never_swallows_substantive_or_other_bot_questions(question):
    assert qa_worker.self_hail_reply(question, commodore.BOT_USERNAME) is None


def test_thread_root_exception_never_crosses_chat_or_explicit_topic(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch)
    msg["message_thread_id"] = 902
    parent = msg["reply_to_message"]
    parent.pop("message_thread_id")
    parent["chat"]["id"] = CHAT_ID - 1
    assert commodore._reply_chain_context(msg) == []
    parent["chat"]["id"] = CHAT_ID
    parent["message_thread_id"] = 903
    assert commodore._reply_chain_context(msg) == []


def test_missing_image_followup_uses_caption_and_requests_page_detail(monkeypatch, tmp_path):
    msg = _seed_cookie_thread(tmp_path, monkeypatch)
    msg["message_thread_id"] = 902
    msg["text"] = "There are some missing images."
    parent = msg["reply_to_message"]
    parent.pop("message_thread_id")
    parent.pop("text")
    parent.update(caption="Can you look into this?", photo=[{"file_id": "not-a-model-input"}])
    captured = {}

    def ask(prompt, **_kwargs):
        captured["prompt"] = prompt
        return "Which page URL has the missing images?"

    monkeypatch.setattr(commodore, "llm_ask", ask)
    result = commodore.generate_response(msg, is_direct=True, policy=commodore._policy_for(CHAT_ID, 902), recent_messages=[])
    assert result == "Which page URL has the missing images?"
    assert "Can you look into this?" in captured["prompt"]
    assert "image pixels are unavailable" in captured["prompt"]
    assert "not-a-model-input" not in captured["prompt"]

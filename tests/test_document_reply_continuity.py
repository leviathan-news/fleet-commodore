"""Bounded request/document continuity for Telegram follow-ups."""

import json
import sqlite3

import commodore


CHAT_ID = -1004412008479
ACTOR_ID = 55101


def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    commodore._ensure_tables()
    while not commodore._qa_queue.empty():
        commodore._qa_queue.get_nowait()
    commodore._qa_cooldown_by_user.pop(ACTOR_ID, None)


def _document(message_id, *, actor_id=ACTOR_ID, topic_id=None,
              forum=False, name="bundle.md", file_id="private-file-id"):
    message = {
        "message_id": message_id,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": actor_id, "username": f"actor{actor_id}"},
        "document": {
            "file_id": file_id,
            "file_unique_id": f"unique-{message_id}",
            "file_name": name,
            "mime_type": "text/markdown",
            "file_size": 123,
        },
    }
    if topic_id is not None:
        message["message_thread_id"] = topic_id
    if forum:
        message["is_topic_message"] = True
    return message


def test_nonforum_reply_roots_share_bounded_same_actor_request_context(tmp_path, monkeypatch):
    """Ordinary reply roots are not forum boundaries in a normal supergroup."""
    _reset(tmp_path, monkeypatch)
    original = {
        "message_id": 1319,
        "message_thread_id": 1318,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": ACTOR_ID, "username": "operator"},
        "text": f"@{commodore.BOT_USERNAME} update Beads and GitHub from this bundle",
        "reply_to_message": {
            "message_id": 1318,
            "chat": {"id": CHAT_ID},
            "document": {"file_id": "zip-id", "file_name": "bundle.zip"},
        },
    }
    commodore.save_chat_message(original, direct_to_bot=True)
    md = _document(1310, name="report.md", file_id="md-file-id")
    commodore.save_chat_message(md)

    current = {
        "message_id": 1331,
        "message_thread_id": 1310,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": ACTOR_ID, "username": "operator"},
        "text": "right here",
        "reply_to_message": {
            **md,
            "chat": {"id": CHAT_ID, "type": "supergroup"},
        },
    }
    job_uuid, _ = commodore._claim_qa_job(
        current,
        current["text"],
        attachment={"name": "report.md", "text": "# report", "size": 8},
    )

    with sqlite3.connect(commodore.DB_FILE) as conn:
        row = conn.execute(
            "SELECT request_context_json, known_documents_json FROM qa_job WHERE job_uuid=?",
            (job_uuid,),
        ).fetchone()
    request_context = json.loads(row[0])
    known_documents = json.loads(row[1])

    assert [entry["message_id"] for entry in request_context] == [1319]
    assert "update Beads and GitHub" in request_context[0]["text"]
    assert request_context[0]["sender_id"] == ACTOR_ID
    assert any(entry["message_id"] == 1310 for entry in known_documents)
    assert "md-file-id" not in row[1]

    resolved = commodore.known_qa_document_message(job_uuid, 1310)
    assert resolved["document"]["file_id"] == "md-file-id"
    assert commodore.known_qa_document_message(job_uuid, 999999) is None


def test_prior_tasks_are_actor_scoped_while_documents_remain_labelled_candidates(
    tmp_path, monkeypatch,
):
    _reset(tmp_path, monkeypatch)
    for message_id, actor_id, text in (
        (20, ACTOR_ID, f"@{commodore.BOT_USERNAME} inspect my release notes"),
        (21, ACTOR_ID + 1, f"@{commodore.BOT_USERNAME} inspect someone else's task"),
    ):
        commodore.save_chat_message({
            "message_id": message_id,
            "chat": {"id": CHAT_ID, "type": "supergroup"},
            "from": {"id": actor_id, "username": f"actor{actor_id}"},
            "text": text,
        }, direct_to_bot=True)
    commodore.save_chat_message(_document(22, actor_id=ACTOR_ID + 1, name="candidate.md"))

    current = {
        "message_id": 23,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": ACTOR_ID, "username": "operator"},
        "text": "use the relevant one",
    }
    requests = commodore._request_qa_context(current)
    documents = commodore._known_qa_documents(current)

    assert [entry["message_id"] for entry in requests] == [20]
    assert documents[0]["message_id"] == 22
    assert documents[0]["sender_id"] == ACTOR_ID + 1
    assert documents[0]["relation"] == "same_room_recent"


def test_true_forum_topics_remain_isolated(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    for topic_id, task_id, doc_id in ((700, 701, 702), (800, 801, 802)):
        commodore.save_chat_message({
            "message_id": task_id,
            "message_thread_id": topic_id,
            "is_topic_message": True,
            "chat": {"id": CHAT_ID, "type": "supergroup"},
            "from": {"id": ACTOR_ID, "username": "operator"},
            "text": f"@{commodore.BOT_USERNAME} task for topic {topic_id}",
        }, direct_to_bot=True)
        commodore.save_chat_message(_document(
            doc_id, topic_id=topic_id, forum=True, name=f"topic-{topic_id}.md",
        ))

    current = {
        "message_id": 703,
        "message_thread_id": 700,
        "is_topic_message": True,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": ACTOR_ID, "username": "operator"},
        "text": "right here",
    }
    requests = commodore._request_qa_context(current)
    documents = commodore._known_qa_documents(current)

    assert [entry["message_id"] for entry in requests] == [701]
    assert [entry["message_id"] for entry in documents] == [702]


def test_legacy_unknown_topic_classification_is_not_broadened(tmp_path, monkeypatch):
    """A migrated row without Telegram's forum flag is not presumed nonforum."""
    _reset(tmp_path, monkeypatch)
    with sqlite3.connect(commodore.DB_FILE) as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(qa_job)")}
        assert columns["is_forum_topic"][3] == 0
        assert columns["is_forum_topic"][4] is None
        conn.execute(
            "INSERT INTO qa_job "
            "(job_uuid,chat_id,topic_id,requester_id,requester_username,request_msg_id,"
            "question,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "legacy-unknown", CHAT_ID, 999, ACTOR_ID, "operator", 998,
                "legacy task from an unclassified topic", "answered",
                commodore._now_iso(),
            ),
        )

    current = {
        "message_id": 1000,
        "message_thread_id": 1000,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": ACTOR_ID, "username": "operator"},
        "text": "right here",
    }
    assert commodore._request_qa_context(current) == []


def test_qa_worker_payload_forwards_context_classes_separately(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    commodore.save_chat_message({
        "message_id": 30,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": ACTOR_ID, "username": "operator"},
        "text": f"@{commodore.BOT_USERNAME} compare the release packets",
    }, direct_to_bot=True)
    commodore.save_chat_message(_document(31, name="packet.md"))
    current = {
        "message_id": 32,
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from": {"id": ACTOR_ID, "username": "operator"},
        "text": "which one?",
    }
    job_uuid, _ = commodore._claim_qa_job(current, current["text"])

    captured = {}
    launcher = tmp_path / "qa-launcher"
    launcher.write_text("fixture")
    monkeypatch.setattr(commodore, "_qa_launcher_path", lambda: launcher)
    monkeypatch.setattr(
        commodore.subprocess,
        "run",
        lambda _args, *, input, **_kwargs: captured.setdefault("payload", json.loads(input))
        or None,
    )

    # Stop immediately after payload construction; the exception is contained
    # by the coordinator's existing top-level failure handling.
    commodore._process_qa(job_uuid)

    assert captured["payload"]["request_context"][0]["message_id"] == 30
    assert captured["payload"]["known_documents"][0]["message_id"] == 31
    assert "file_id" not in json.dumps(captured["payload"]["known_documents"])

"""plan_drafts schema, transitions, unique-active index."""
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest
import commodore


BOT_HQ = int(commodore.BOT_HQ_GROUP_ID)
ADMIN_ID = next(iter(commodore.ADMIN_TELEGRAM_IDS))


def _drain_queues():
    while not commodore._build_queue.empty():
        commodore._build_queue.get_nowait()
    while not commodore._qa_queue.empty():
        commodore._qa_queue.get_nowait()
    while not commodore._review_queue.empty():
        commodore._review_queue.get_nowait()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point commodore at a fresh sqlite file for the test."""
    db_path = tmp_path / "commodore-test.db"
    monkeypatch.setattr(commodore, "DB_FILE", db_path)
    commodore._ensure_tables()
    _drain_queues()
    yield db_path
    _drain_queues()


def _msg(chat_id=BOT_HQ, sender_id=ADMIN_ID, message_id=1, thread_id=None):
    m = {
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": sender_id, "username": "curvecap"},
        "message_id": message_id,
    }
    if thread_id:
        m["message_thread_id"] = thread_id
    return m


def test_first_plan_creates_drafting_row(isolated_db):
    reply = commodore.handle_plan_message(
        _msg(message_id=10),
        "let's plan adding a verbose flag to /process_x_queue",
    )
    assert "Admiralty" in reply

    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM plan_drafts").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "drafting"
    assert row["chat_id"] == BOT_HQ
    assert row["requester_id"] == ADMIN_ID
    history = json.loads(row["message_history_json"])
    assert len(history) == 1


def test_appended_turns_extend_history(isolated_db):
    commodore.handle_plan_message(_msg(message_id=11), "let's plan a thing")
    commodore.handle_plan_message(_msg(message_id=12), "scope: add the flag")
    commodore.handle_plan_message(_msg(message_id=13), "preserve the default")

    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM plan_drafts").fetchone()
    history = json.loads(row["message_history_json"])
    assert len(history) == 3
    assert "preserve the default" in row["plan_body_md"]


def test_unique_active_index_per_user_thread(isolated_db):
    """Two drafts in the same (chat, thread, user) cannot both be active."""
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO plan_drafts (draft_uuid, chat_id, thread_id, requester_id, "
        "status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'drafting', '2026-04-25', '2026-04-25')",
        ("d1", BOT_HQ, 0, ADMIN_ID),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO plan_drafts (draft_uuid, chat_id, thread_id, requester_id, "
            "status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'drafting', '2026-04-25', '2026-04-25')",
            ("d2", BOT_HQ, 0, ADMIN_ID),
        )


def test_terminal_states_release_uniqueness(isolated_db):
    """Once a draft is shipped/abandoned, a new active draft becomes legal."""
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO plan_drafts (draft_uuid, chat_id, thread_id, requester_id, "
        "status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'shipped', '2026-04-25', '2026-04-25')",
        ("d1", BOT_HQ, 0, ADMIN_ID),
    )
    conn.commit()
    # Should NOT raise — the prior row is no longer active.
    conn.execute(
        "INSERT INTO plan_drafts (draft_uuid, chat_id, thread_id, requester_id, "
        "status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'drafting', '2026-04-25', '2026-04-25')",
        ("d2", BOT_HQ, 0, ADMIN_ID),
    )
    conn.commit()


def test_ship_without_target_repo_declines(isolated_db):
    """A draft without target_repo cannot be shipped."""
    commodore.handle_plan_message(_msg(message_id=20), "let's plan a thing")
    reply = commodore.handle_ship(_msg(message_id=21))
    assert "target repository" in reply.lower() or "repo:" in reply.lower()

    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM plan_drafts").fetchone()
    assert row["status"] == "drafting"  # not shipping


def test_ship_with_target_repo_transitions_to_shipping(isolated_db):
    commodore.handle_plan_message(_msg(message_id=30), "let's plan a thing")

    # Set target_repo by hand (in production this would come from plan refinement)
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "UPDATE plan_drafts SET target_repo='leviathan-news/squid-bot'"
    )
    conn.commit()
    conn.close()

    reply = commodore.handle_ship(_msg(message_id=31))
    assert "Stand by" in reply or "Admiralty" in reply

    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    draft = conn.execute("SELECT * FROM plan_drafts").fetchone()
    assert draft["status"] == "shipping"
    assert draft["target_branch"]  # generated at ship time
    job = conn.execute("SELECT * FROM build_job").fetchone()
    assert job is not None
    assert job["status"] == "queued"
    assert job["idempotency_key"]


def test_abandon_strikes_active_draft(isolated_db):
    commodore.handle_plan_message(_msg(message_id=40), "let's plan a thing")
    reply = commodore.handle_abandon(_msg(message_id=41))
    assert "struck" in reply.lower()

    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM plan_drafts").fetchone()
    assert row["status"] == "abandoned"


def test_abandon_with_no_active_draft_idempotent(isolated_db):
    reply = commodore.handle_abandon(_msg(message_id=50))
    assert "no commission" in reply.lower() or "orders book" in reply.lower()


def test_drafts_are_per_thread(isolated_db):
    """Two drafts from the same user but different threads can coexist."""
    commodore.handle_plan_message(
        _msg(message_id=60, thread_id=100), "plan A in thread 100",
    )
    commodore.handle_plan_message(
        _msg(message_id=61, thread_id=200), "plan B in thread 200",
    )
    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT thread_id, plan_body_md FROM plan_drafts WHERE status='drafting'"
    ).fetchall()
    assert len(rows) == 2
    threads = {r["thread_id"] for r in rows}
    assert threads == {100, 200}

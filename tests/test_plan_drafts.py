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
    """v6: handle_plan_message returns None (handoff to LLM persona pipeline)
    and stashes a per-turn plan-refinement context. The DB row is created
    with status=drafting and the user's text in message_history_json."""
    m = _msg(message_id=10)
    reply = commodore.handle_plan_message(
        m,
        "let's plan adding a verbose flag to /process_x_queue",
    )
    assert reply is None  # falls through to generate_response

    # Plan context staged for the LLM
    ctx = commodore.get_plan_context(m)
    assert ctx is not None
    assert "PLAN-REFINEMENT" in ctx

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


def test_repo_extraction_from_first_turn(isolated_db):
    """User saying `repo: leviathan-news/foo` in the opening message
    must populate target_repo without an explicit second turn."""
    m = _msg(message_id=14)
    commodore.handle_plan_message(
        m, "let's plan a typo fix. repo: leviathan-news/fleet-commodore",
    )
    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT target_repo FROM plan_drafts").fetchone()
    assert row["target_repo"] == "leviathan-news/fleet-commodore"


def test_repo_extraction_rejects_non_leviathan(isolated_db):
    """Off-org repos must NOT populate target_repo (the build worker only
    forks under leviathan-agent; foo/bar can't ship)."""
    m = _msg(message_id=15)
    commodore.handle_plan_message(
        m, "fix something in microsoft/vscode",
    )
    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT target_repo FROM plan_drafts").fetchone()
    assert row["target_repo"] is None


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


# --- _active_draft_for: max_age_minutes (2026-05-15) -------------------------
#
# Used by the is_direct fast-path so stale drafts don't keep treating every
# user message as "implicitly directed at the bot." Without the bound, a
# 3-day-old "drafting" row was bypassing mention_only in Lev Dev.


def test_active_draft_for_unbounded_finds_old_row(isolated_db):
    """No max_age — the helper finds drafts of any age (handle_plan_message
    uses this to continue an existing plan when the operator re-engages)."""
    from datetime import datetime, timedelta, timezone
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    conn = sqlite3.connect(str(isolated_db))
    try:
        conn.execute(
            """INSERT INTO plan_drafts
               (draft_uuid, chat_id, thread_id, requester_id, requester_username,
                title, target_repo, plan_body_md, message_history_json, status,
                created_at, updated_at)
               VALUES ('uuid-old', ?, NULL, ?, 'curvecap',
                       'old', NULL, 'body', '[]', 'drafting', ?, ?)""",
            (BOT_HQ, ADMIN_ID, long_ago, long_ago),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = commodore._active_draft_for(conn, BOT_HQ, None, ADMIN_ID)
        assert row is not None
        assert row["draft_uuid"] == "uuid-old"
    finally:
        conn.close()


def test_active_draft_for_bounded_ignores_stale_row(isolated_db):
    """With max_age_minutes=15, a 3-day-old draft is invisible to the
    is_direct check — the bot won't treat the user as mid-conversation."""
    from datetime import datetime, timedelta, timezone
    long_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    conn = sqlite3.connect(str(isolated_db))
    try:
        conn.execute(
            """INSERT INTO plan_drafts
               (draft_uuid, chat_id, thread_id, requester_id, requester_username,
                title, target_repo, plan_body_md, message_history_json, status,
                created_at, updated_at)
               VALUES ('uuid-stale', ?, NULL, ?, 'curvecap',
                       'old', NULL, 'body', '[]', 'drafting', ?, ?)""",
            (BOT_HQ, ADMIN_ID, long_ago, long_ago),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = commodore._active_draft_for(
            conn, BOT_HQ, None, ADMIN_ID, max_age_minutes=15,
        )
        assert row is None


    finally:
        conn.close()


def test_active_draft_for_bounded_finds_fresh_row(isolated_db):
    """Within the window, the helper still returns the row."""
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    conn = sqlite3.connect(str(isolated_db))
    try:
        conn.execute(
            """INSERT INTO plan_drafts
               (draft_uuid, chat_id, thread_id, requester_id, requester_username,
                title, target_repo, plan_body_md, message_history_json, status,
                created_at, updated_at)
               VALUES ('uuid-fresh', ?, NULL, ?, 'curvecap',
                       'recent', NULL, 'body', '[]', 'drafting', ?, ?)""",
            (BOT_HQ, ADMIN_ID, recent, recent),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        row = commodore._active_draft_for(
            conn, BOT_HQ, None, ADMIN_ID, max_age_minutes=15,
        )
        assert row is not None
        assert row["draft_uuid"] == "uuid-fresh"
    finally:
        conn.close()

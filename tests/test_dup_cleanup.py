"""bin/commodore-dup-cleanup — operator helper for resolving Telegram duplicates.

Tests the core cleanup recipe (canonical = lowest id; per-pipeline action
table; 48-hour edit/delete window; cleanup_* bookkeeping) without making
real Telegram API calls.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
import commodore


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin" / "commodore-dup-cleanup"
BOT_HQ = int(commodore.BOT_HQ_GROUP_ID)
ADMIN_ID = next(iter(commodore.ADMIN_TELEGRAM_IDS))


@pytest.fixture
def dup_cleanup_module():
    """Load the dup-cleanup script as a module (the file has no .py extension)."""
    return SourceFileLoader("dup_cleanup", str(SCRIPT)).load_module()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "commodore-test.db"
    monkeypatch.setattr(commodore, "DB_FILE", db_path)
    commodore._ensure_tables()
    return db_path


def _seed_dup(conn, job_uuid, action_type, *, thread_id=None, sent_at_offset_h=0):
    """Insert two confirmed posts for the same (job_table, job_uuid, action_type)."""
    base_iso = (datetime.now(timezone.utc) - timedelta(hours=sent_at_offset_h)).isoformat()
    intent_a = commodore._intent_id(job_uuid, action_type)
    intent_b = "f" * 64  # deliberately different from intent_a
    rows = []
    for i, (intent, msg_id, tok) in enumerate([
        (intent_a, 555, "tok-canonical"),
        (intent_b, 666, "tok-suppressed"),
    ]):
        conn.execute(
            "INSERT INTO outgoing_msg (job_table, job_uuid, chat_id, thread_id, "
            "action_type, intent_id, dedup_token, intent_recorded_at, "
            "telegram_message_id, sent_at) "
            "VALUES ('qa_job', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_uuid, BOT_HQ, thread_id, action_type, intent, tok,
             base_iso, msg_id, base_iso),
        )
        rows.append(msg_id)
    conn.commit()
    return rows


def test_detect_duplicate_groups_finds_unresolved(isolated_db, dup_cleanup_module):
    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    _seed_dup(conn, "uuid-A", commodore.OutgoingAction.QA_ANSWER)
    groups = dup_cleanup_module.detect_duplicate_groups(conn)
    assert len(groups) == 1
    assert groups[0]["job_uuid"] == "uuid-A"
    assert groups[0]["dup_count"] == 2


def test_canonical_is_lowest_id(isolated_db, dup_cleanup_module, monkeypatch):
    """Canonical = lowest id. Deletes (non-threaded) the higher id."""
    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _seed_dup(conn, "uuid-B", commodore.OutgoingAction.QA_ANSWER, thread_id=None)

    calls = []
    monkeypatch.setattr(dup_cleanup_module, "delete_message",
                        lambda c, m: (calls.append(("del", c, m)), {"ok": True})[1])

    summary = dup_cleanup_module.resolve_group(
        conn, "qa_job", "uuid-B", commodore.OutgoingAction.QA_ANSWER,
        operator_id=ADMIN_ID, dry_run=False,
    )
    assert summary["status"] == "resolved"
    assert summary["canonical"] == 555
    assert calls == [("del", BOT_HQ, 666)]

    # Verify cleanup_role assignments
    rows = conn.execute(
        "SELECT telegram_message_id, cleanup_role, cleanup_action FROM outgoing_msg "
        "WHERE job_uuid='uuid-B' ORDER BY id"
    ).fetchall()
    assert dict(rows[0])["cleanup_role"] == "canonical"
    assert dict(rows[1])["cleanup_role"] == "suppressed"
    assert dict(rows[1])["cleanup_action"] == "deleted"


def test_threaded_chat_uses_edit_marker(isolated_db, dup_cleanup_module, monkeypatch):
    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _seed_dup(conn, "uuid-C", commodore.OutgoingAction.QA_ANSWER, thread_id=42)

    edit_calls = []
    monkeypatch.setattr(dup_cleanup_module, "edit_message",
                        lambda c, m, t: (edit_calls.append((c, m, t[:50])), {"ok": True})[1])
    monkeypatch.setattr(dup_cleanup_module, "delete_message",
                        lambda *a, **k: pytest.fail("should NOT delete in threaded chat"))

    summary = dup_cleanup_module.resolve_group(
        conn, "qa_job", "uuid-C", commodore.OutgoingAction.QA_ANSWER,
        operator_id=ADMIN_ID, dry_run=False,
    )
    assert summary["status"] == "resolved"
    assert len(edit_calls) == 1
    edit_text = edit_calls[0][2]
    assert "duplicate" in edit_text.lower()
    assert "555" in edit_text  # references canonical message id


def test_old_messages_use_followup(isolated_db, dup_cleanup_module, monkeypatch):
    """Messages older than 48h: leave-in-place + send_followup."""
    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _seed_dup(conn, "uuid-D", commodore.OutgoingAction.QA_ANSWER,
              thread_id=42, sent_at_offset_h=72)

    followup_calls = []
    monkeypatch.setattr(dup_cleanup_module, "send_followup",
                        lambda c, t, reply_to=None, thread_id=None: (
                            followup_calls.append((c, t[:60], reply_to)),
                            {"ok": True, "result": {"message_id": 9999}}
                        )[1])
    monkeypatch.setattr(dup_cleanup_module, "edit_message",
                        lambda *a, **k: pytest.fail("MUST NOT edit messages >48h"))
    monkeypatch.setattr(dup_cleanup_module, "delete_message",
                        lambda *a, **k: pytest.fail("MUST NOT delete messages >48h"))

    summary = dup_cleanup_module.resolve_group(
        conn, "qa_job", "uuid-D", commodore.OutgoingAction.QA_ANSWER,
        operator_id=ADMIN_ID, dry_run=False,
    )
    assert summary["status"] == "resolved"
    assert len(followup_calls) == 1
    suppressed_row = conn.execute(
        "SELECT cleanup_action FROM outgoing_msg WHERE telegram_message_id=666"
    ).fetchone()
    assert dict(suppressed_row)["cleanup_action"] == "left_in_place_with_followup"


def test_resolve_all_skips_old_groups_in_bulk_mode(isolated_db, dup_cleanup_module,
                                                    monkeypatch):
    """auto_only_recent skips groups containing any row >48h so the operator
    can review them by hand."""
    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _seed_dup(conn, "uuid-E", commodore.OutgoingAction.QA_ANSWER,
              thread_id=42, sent_at_offset_h=72)

    monkeypatch.setattr(dup_cleanup_module, "edit_message",
                        lambda *a, **k: pytest.fail("MUST NOT touch in bulk skip"))
    monkeypatch.setattr(dup_cleanup_module, "delete_message",
                        lambda *a, **k: pytest.fail("MUST NOT touch in bulk skip"))
    monkeypatch.setattr(dup_cleanup_module, "send_followup",
                        lambda *a, **k: pytest.fail("MUST NOT touch in bulk skip"))

    summary = dup_cleanup_module.resolve_group(
        conn, "qa_job", "uuid-E", commodore.OutgoingAction.QA_ANSWER,
        operator_id=ADMIN_ID, dry_run=False,
        auto_only_recent=True,
    )
    assert summary["status"] == "skipped_has_old"


def test_dry_run_makes_no_telegram_calls(isolated_db, dup_cleanup_module, monkeypatch):
    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _seed_dup(conn, "uuid-F", commodore.OutgoingAction.QA_ANSWER, thread_id=None)

    monkeypatch.setattr(dup_cleanup_module, "delete_message",
                        lambda *a, **k: pytest.fail("dry-run MUST NOT call Telegram"))
    summary = dup_cleanup_module.resolve_group(
        conn, "qa_job", "uuid-F", commodore.OutgoingAction.QA_ANSWER,
        operator_id=ADMIN_ID, dry_run=True,
    )
    assert summary["status"] == "dry_run"
    assert len(summary["plan"]) == 1
    # SQL state untouched
    rows = conn.execute(
        "SELECT cleanup_role FROM outgoing_msg WHERE job_uuid='uuid-F'"
    ).fetchall()
    assert all(dict(r)["cleanup_role"] is None for r in rows)

"""Boot-recovery contract tests.

A daemon restart while jobs are in-flight MUST:
  - Re-queue every (queued | in_progress) row across all three job tables.
  - Sweep `*.result.json.tmp` files older than 60s.
  - Mark queue-overflow cases as 'orphaned' / 'failed' so the lease
    releases and the operator can re-issue.
"""
import os
import sqlite3
import time
from datetime import datetime, timezone

import pytest
import commodore


BOT_HQ = int(commodore.BOT_HQ_GROUP_ID)
ADMIN_ID = next(iter(commodore.ADMIN_TELEGRAM_IDS))


def _drain_all_queues():
    while not commodore._build_queue.empty():
        commodore._build_queue.get_nowait()
    while not commodore._qa_queue.empty():
        commodore._qa_queue.get_nowait()
    while not commodore._review_queue.empty():
        commodore._review_queue.get_nowait()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "commodore-test.db"
    results_dir = tmp_path / "results"
    results_dir.mkdir(mode=0o700)
    monkeypatch.setattr(commodore, "DB_FILE", db_path)
    monkeypatch.setattr(commodore, "RESULTS_DIR", results_dir)
    commodore._ensure_tables()
    _drain_all_queues()
    yield db_path
    _drain_all_queues()


def _now():
    return datetime.now(timezone.utc).isoformat()


def test_recovery_requeues_all_three_pipelines(isolated_db):
    """Seed one row per pipeline and confirm recovery enqueues all three."""
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO build_job (job_uuid, draft_uuid, chat_id, requester_id, "
        "target_repo, target_branch, job_payload_json, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
        ("b1", "d1", BOT_HQ, ADMIN_ID,
         "leviathan-news/squid-bot", "commodore/test-20260425", "{}", _now()),
    )
    conn.execute(
        "INSERT INTO qa_job (job_uuid, chat_id, requester_id, question, status, created_at) "
        "VALUES (?, ?, ?, ?, 'queued', ?)",
        ("q1", BOT_HQ, ADMIN_ID, "test", _now()),
    )
    conn.execute(
        "INSERT INTO pr_review (review_uuid, claim_key, requested_by_id, chat_id, "
        "repo, pr_number, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
        ("r1", "leviathan-news/squid-bot#1", ADMIN_ID, BOT_HQ,
         "leviathan-news/squid-bot", 1, _now()),
    )
    conn.commit()
    conn.close()

    summary = commodore._recover_jobs_on_boot()
    assert summary["build"] == 1
    assert summary["qa"] == 1
    assert summary["review"] == 1

    assert commodore._build_queue.qsize() == 1
    assert commodore._qa_queue.qsize() == 1
    assert commodore._review_queue.qsize() == 1
    # pr_review uses dict shape for backward compat
    rev = commodore._review_queue.get_nowait()
    assert rev["review_uuid"] == "r1"


def test_recovery_counts_in_progress_as_requeued(isolated_db):
    """The summary distinguishes truly-queued rows from in-progress rows
    so the operator can spot interrupted work post-restart."""
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO build_job (job_uuid, draft_uuid, chat_id, requester_id, "
        "target_repo, target_branch, job_payload_json, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)",
        ("b-mid", "d2", BOT_HQ, ADMIN_ID, "leviathan-news/squid-bot",
         "commodore/test-20260425", "{}", _now()),
    )
    conn.commit()
    conn.close()

    summary = commodore._recover_jobs_on_boot()
    assert summary["requeued"] >= 1


def test_recovery_sweeps_old_tmp_files(isolated_db):
    """Atomic-write protocol leaves orphan .tmp files when a worker crashes
    mid-write. Recovery sweeps any `.tmp` older than 60s."""
    fresh = commodore.RESULTS_DIR / "fresh.result.json.tmp"
    fresh.write_text("{partial")
    stale = commodore.RESULTS_DIR / "stale.result.json.tmp"
    stale.write_text("{partial")
    old = time.time() - 120
    os.utime(stale, (old, old))

    summary = commodore._recover_jobs_on_boot()
    assert summary["tmp_swept"] == 1
    assert not stale.exists()
    assert fresh.exists()  # too fresh to sweep


def test_recovery_terminal_rows_are_skipped(isolated_db):
    """Already-completed rows must NOT be re-queued (would re-do side effects)."""
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO build_job (job_uuid, draft_uuid, chat_id, requester_id, "
        "target_repo, target_branch, job_payload_json, status, "
        "pr_url, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?)",
        ("done", "d3", BOT_HQ, ADMIN_ID, "leviathan-news/squid-bot",
         "commodore/done-20260425", "{}",
         "https://github.com/x/y/pull/1", _now()),
    )
    conn.commit()
    conn.close()

    summary = commodore._recover_jobs_on_boot()
    assert summary["build"] == 0
    assert commodore._build_queue.qsize() == 0


def test_recovery_orphans_when_queue_full(isolated_db, monkeypatch):
    """If the in-memory queue fills during recovery, the row must be marked
    'orphaned' (build) / 'failed' (qa) so the lease releases."""
    # Pre-fill the build queue to capacity (10).
    for _ in range(commodore._build_queue.maxsize):
        commodore._build_queue.put_nowait("filler")
    try:
        conn = sqlite3.connect(str(isolated_db))
        conn.execute(
            "INSERT INTO build_job (job_uuid, draft_uuid, chat_id, requester_id, "
            "target_repo, target_branch, job_payload_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
            ("overflow", "d4", BOT_HQ, ADMIN_ID, "leviathan-news/squid-bot",
             "commodore/overflow-20260425", "{}", _now()),
        )
        conn.commit()
        conn.close()

        commodore._recover_jobs_on_boot()

        conn = sqlite3.connect(str(isolated_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, error FROM build_job WHERE job_uuid='overflow'"
        ).fetchone()
        assert row["status"] == "orphaned"
        assert "queue full" in (row["error"] or "")
    finally:
        # Drain the queue so other tests in the same process see a clean state.
        while not commodore._build_queue.empty():
            commodore._build_queue.get_nowait()

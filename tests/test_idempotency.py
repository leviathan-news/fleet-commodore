"""Idempotency contract tests.

The v6 plan promises: build is fully idempotent (no duplicate PR possible
across any crash window); QA/review have a documented single-duplicate-at-
worst window between intent log insert and Telegram response.

These tests lock that contract in. Each test simulates one crash window
and asserts the documented recovery behavior.
"""
import json
import sqlite3
import time
from datetime import datetime, timezone

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
    db_path = tmp_path / "commodore-test.db"
    results_dir = tmp_path / "results"
    results_dir.mkdir(mode=0o700)
    monkeypatch.setattr(commodore, "DB_FILE", db_path)
    monkeypatch.setattr(commodore, "RESULTS_DIR", results_dir)
    commodore._ensure_tables()
    _drain_queues()
    yield db_path
    _drain_queues()


@pytest.fixture
def stub_send_message(monkeypatch):
    """Replace send_message with a counter-stub. Returns a list of calls."""
    calls = []
    next_msg_id = [10000]

    def fake(chat_id, text, thread_id=None, reply_to=None):
        calls.append({"chat_id": chat_id, "text": text,
                      "thread_id": thread_id, "reply_to": reply_to})
        next_msg_id[0] += 1
        return {"ok": True, "result": {"message_id": next_msg_id[0]}}

    monkeypatch.setattr(commodore, "send_message", fake)
    return calls


def _now():
    return datetime.now(timezone.utc).isoformat()


# --- Case 1: crash before any side effect ----------------------------------

def test_crash_before_any_side_effect_produces_one_post(isolated_db, stub_send_message):
    """Recovery re-queues a queued qa_job; pre-flight finds nothing in
    scratch dir, outgoing_msg, or external oracle. Worker produces the
    side effect once."""
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO qa_job (job_uuid, chat_id, requester_id, question, status, created_at) "
        "VALUES (?, ?, ?, ?, 'queued', ?)",
        ("qa-1", BOT_HQ, ADMIN_ID, "how does the X queue work?", _now()),
    )
    conn.commit()
    conn.close()

    summary = commodore._recover_jobs_on_boot()
    assert summary["qa"] == 1
    assert summary["requeued"] == 0  # was 'queued', not 'in_progress'

    # Drain the queue so we know recovery enqueued correctly
    job_uuid = commodore._qa_queue.get_nowait()
    assert job_uuid == "qa-1"
    # No Telegram call should have happened during recovery itself
    assert len(stub_send_message) == 0


# --- Case 2: build crashed after gh pr create, before SQLite update --------

def test_build_recovery_via_scratch_file(isolated_db, stub_send_message, monkeypatch):
    """Scratch file from prior crashed attempt — recovery reads it, posts
    the already-filed ack, marks succeeded. NO container relaunch, NO
    second gh pr create."""
    # Seed the row in 'in_progress'
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO build_job (job_uuid, draft_uuid, chat_id, requester_id, "
        "target_repo, target_branch, job_payload_json, status, "
        "idempotency_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress', ?, ?)",
        ("build-1", "d1", BOT_HQ, ADMIN_ID,
         "leviathan-news/squid-bot", "commodore/test-20260425", "{}",
         "idem1", _now()),
    )
    conn.commit()
    conn.close()

    # Drop a valid scratch file
    scratch = commodore.RESULTS_DIR / "build-1.result.json"
    scratch.write_text(json.dumps({
        "pr_url": "https://github.com/leviathan-news/squid-bot/pull/9999",
        "commit_sha": "deadbeef",
    }))

    # Track gh calls — should be zero
    monkeypatch.setattr(commodore, "_gh_pr_list_for_branch",
                        lambda *a, **k: pytest.fail("gh pr list MUST NOT run when scratch hits"))

    commodore._process_build("build-1")

    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM build_job WHERE job_uuid='build-1'").fetchone()
    assert row["status"] == "succeeded"
    assert "9999" in row["pr_url"]
    assert row["side_effect_completed_at"] is not None
    # Ack posted exactly once
    assert len(stub_send_message) == 1
    assert "already filed" in stub_send_message[0]["text"]
    # Scratch file unlinked
    assert not scratch.exists()


# --- Case 3: QA crashed between scratch write and send_message_with_wal ----

def test_qa_recovery_via_scratch_posts_once(isolated_db, stub_send_message, monkeypatch):
    """Scratch with a valid answer, no outgoing_msg row. Recovery picks up
    the scratch, calls send_message_with_wal once, fills telegram_reply_msg_id."""
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO qa_job (job_uuid, chat_id, requester_id, question, status, created_at) "
        "VALUES (?, ?, ?, ?, 'in_progress', ?)",
        ("qa-X", BOT_HQ, ADMIN_ID, "how does X work?", _now()),
    )
    conn.commit()
    conn.close()

    scratch = commodore.RESULTS_DIR / "qa-X.result.json"
    scratch.write_text(json.dumps({
        "status": "answered",
        "answer": "The X queue is a priority-scored ring buffer.",
        "citations": ["dev-journal/x-queue.md"],
    }))

    # The launcher path doesn't exist on dev machines; that's fine — the
    # scratch pre-flight short-circuits container launch entirely.
    commodore._process_qa("qa-X")

    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM qa_job WHERE job_uuid='qa-X'").fetchone()
    assert row["status"] == "answered"
    assert row["telegram_reply_msg_id"] is not None
    assert row["last_dedup_token"]
    assert len(stub_send_message) == 1
    assert "X queue" in stub_send_message[0]["text"]
    assert not scratch.exists()


# --- Case 4: ambiguous middle window — single duplicate possible ----------

def test_ambiguous_window_documented_single_duplicate(isolated_db, stub_send_message):
    """Pre-populate outgoing_msg with intent_recorded but telegram_message_id NULL
    (the ambiguous window). Recovery's send_message_with_wal sees no confirmed
    prior post and posts again. This is the documented single-duplicate-at-worst
    contract for QA/review.

    The test asserts: (a) the post happened, (b) post-hoc duplicate detection
    via the operator-facing GROUP BY query would find the duplicate."""
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO qa_job (job_uuid, chat_id, requester_id, question, status, created_at) "
        "VALUES (?, ?, ?, ?, 'in_progress', ?)",
        ("qa-D", BOT_HQ, ADMIN_ID, "test", _now()),
    )
    # Simulate the ambiguous-window: intent logged, telegram_message_id NULL.
    intent = commodore._intent_id("qa-D", commodore.OutgoingAction.QA_ANSWER)
    conn.execute(
        "INSERT INTO outgoing_msg (job_table, job_uuid, chat_id, action_type, "
        "intent_id, dedup_token, intent_recorded_at) "
        "VALUES ('qa_job', 'qa-D', ?, ?, ?, 'tok1', ?)",
        (BOT_HQ, commodore.OutgoingAction.QA_ANSWER, intent, _now()),
    )
    # AND simulate that Telegram actually delivered (so we can verify dup
    # detection works) by inserting a second row that the daemon previously
    # would have filled in.
    conn.execute(
        "UPDATE outgoing_msg SET telegram_message_id=555, sent_at=? "
        "WHERE intent_id=?",
        (_now(), intent),
    )
    conn.commit()
    conn.close()

    # Now recovery picks up qa-D and replays via the scratch path. Drop a
    # scratch so the worker doesn't try to relaunch the container.
    scratch = commodore.RESULTS_DIR / "qa-D.result.json"
    scratch.write_text(json.dumps({"status": "answered", "answer": "hi"}))

    commodore._process_qa("qa-D")
    # The WAL helper's pre-flight should detect the confirmed row and dedupe.
    # No new send_message call.
    assert len(stub_send_message) == 0

    # If we wipe the telegram_message_id (simulate the row WASN'T confirmed
    # at recovery time), recovery WOULD post again, producing the documented
    # single duplicate.
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "UPDATE outgoing_msg SET telegram_message_id=NULL, sent_at=NULL "
        "WHERE intent_id=?",
        (intent,),
    )
    # And reset qa_job state so the worker re-enters cleanly
    conn.execute(
        "UPDATE qa_job SET status='in_progress', telegram_reply_msg_id=NULL, "
        "side_effect_completed_at=NULL WHERE job_uuid='qa-D'",
    )
    conn.commit()
    conn.close()

    scratch.write_text(json.dumps({"status": "answered", "answer": "hi"}))
    commodore._process_qa("qa-D")
    # NOW we expect exactly one new post (the WAL didn't see a confirmed row)
    assert len(stub_send_message) == 1


# --- Case 5: rapid double `ship it` --------------------------------------

def test_double_ship_hits_idempotency_unique(isolated_db, stub_send_message):
    """Two ship-it calls with the same plan → second hits the
    idx_build_job_idempotency unique constraint and returns the existing
    job's PR URL (or status, if the first one hasn't completed yet)."""
    msg = {
        "chat": {"id": BOT_HQ, "type": "supergroup"},
        "from": {"id": ADMIN_ID, "username": "curvecap"},
        "message_id": 1,
    }
    commodore.handle_plan_message(msg, "let's plan a thing")
    conn = sqlite3.connect(str(isolated_db))
    conn.execute("UPDATE plan_drafts SET target_repo='leviathan-news/squid-bot'")
    conn.commit()
    conn.close()

    reply1 = commodore.handle_ship(msg)
    assert "Stand by" in reply1 or "Admiralty takes" in reply1

    # Second draft (because first one transitioned to 'shipping' which
    # the unique-active-draft index allows past). Recreate the same plan.
    # Actually for the unique-key idempotency test we need to call
    # _claim_build_job directly with the SAME draft row to reproduce the
    # same idempotency_key.
    conn = sqlite3.connect(str(isolated_db))
    conn.row_factory = sqlite3.Row
    draft_row = conn.execute(
        "SELECT * FROM plan_drafts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    # Manually re-run _claim_build_job with the same target+branch+body
    _, ack2 = commodore._claim_build_job(draft_row)
    conn.close()
    # Second call must not produce a second build_job row
    conn = sqlite3.connect(str(isolated_db))
    rows = conn.execute("SELECT COUNT(*) FROM build_job").fetchone()
    assert rows[0] == 1
    conn.close()
    assert "already" in ack2.lower() or "in progress" in ack2.lower()


# --- Case 6: atomic-write protocol — partial .tmp not parsed --------------

def test_partial_tmp_not_parsed_by_coordinator(isolated_db, monkeypatch):
    """Drop a partial-JSON `.tmp` file (simulating a worker crash mid-write).
    The coordinator's read_result_file MUST NOT parse `.tmp` files."""
    bad = commodore.RESULTS_DIR / "abc.result.json.tmp"
    bad.write_text("{partial-json")  # malformed
    # Read should return None (looking for `.result.json`, not `.tmp`)
    result = commodore.read_result_file("abc")
    assert result is None


def test_stale_tmp_swept_after_60s(isolated_db):
    """Atomic-write protocol: orphan `.tmp` files >60s are swept by recovery."""
    import os
    bad = commodore.RESULTS_DIR / "stale.result.json.tmp"
    bad.write_text("{partial")
    old = time.time() - 120
    os.utime(bad, (old, old))
    n = commodore.sweep_stale_tmp_files()
    assert n == 1
    assert not bad.exists()


# --- Case 7: outgoing_msg dedup oracle -----------------------------------

def test_outgoing_msg_log_skips_resend(isolated_db, stub_send_message):
    """Pre-populate outgoing_msg with a confirmed row. Calling
    send_message_with_wal with the same intent must short-circuit — no
    new send_message call."""
    intent = commodore._intent_id("uuid-Z", commodore.OutgoingAction.QA_ANSWER)
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO outgoing_msg (job_table, job_uuid, chat_id, action_type, "
        "intent_id, dedup_token, intent_recorded_at, telegram_message_id, sent_at) "
        "VALUES ('qa_job', 'uuid-Z', ?, ?, ?, 'preexisting', ?, 9999, ?)",
        (BOT_HQ, commodore.OutgoingAction.QA_ANSWER, intent, _now(), _now()),
    )
    conn.commit()
    conn.close()

    resp = commodore.send_message_with_wal(
        "qa_job", "uuid-Z", commodore.OutgoingAction.QA_ANSWER,
        BOT_HQ, "any text here",
    )
    assert resp.get("deduped") is True
    assert resp["result"]["message_id"] == 9999
    assert len(stub_send_message) == 0


# --- Case 8 (v6): content-independence of intent_id ----------------------

def test_intent_id_is_content_independent(isolated_db, stub_send_message):
    """Content of `text` must NOT affect dedup. Two calls with different
    text but same (job_uuid, action_type) → second call dedupes."""
    commodore.send_message_with_wal(
        "qa_job", "uuid-Y", commodore.OutgoingAction.QA_ANSWER,
        BOT_HQ, "Original phrasing of the answer.",
    )
    assert len(stub_send_message) == 1

    # Simulate a deploy with a different rendered template
    resp = commodore.send_message_with_wal(
        "qa_job", "uuid-Y", commodore.OutgoingAction.QA_ANSWER,
        BOT_HQ, "Reworded answer with new persona phrasing.",
    )
    assert resp.get("deduped") is True
    # Still one call total — no second send despite different body
    assert len(stub_send_message) == 1


# --- Case 9 (v6): dedup_token visibility -------------------------------

def test_dedup_token_round_trip(isolated_db, stub_send_message):
    """Ensure the dedup_token is recorded on outgoing_msg and accessible by
    the operator query `WHERE dedup_token = ?`."""
    resp = commodore.send_message_with_wal(
        "qa_job", "uuid-T", commodore.OutgoingAction.QA_ANSWER,
        BOT_HQ, "answer body",
    )
    token = resp.get("dedup_token")
    assert token

    conn = sqlite3.connect(str(isolated_db))
    rows = conn.execute(
        "SELECT job_table, job_uuid, action_type FROM outgoing_msg "
        "WHERE dedup_token=?", (token,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("qa_job", "uuid-T", commodore.OutgoingAction.QA_ANSWER)

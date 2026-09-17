"""Recovery must quarantine uncertain Telegram sends before relaunching work."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest
import commodore


BOT_HQ = int(commodore.BOT_HQ_GROUP_ID)
ADMIN_ID = next(iter(commodore.ADMIN_TELEGRAM_IDS))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _drain_queues():
    for work_queue in (commodore._build_queue, commodore._qa_queue, commodore._review_queue):
        while not work_queue.empty():
            work_queue.get_nowait()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "commodore.db"
    results = tmp_path / "results"
    results.mkdir(mode=0o700)
    monkeypatch.setattr(commodore, "DB_FILE", path)
    monkeypatch.setattr(commodore, "RESULTS_DIR", results)
    commodore._ensure_tables()
    _drain_queues()
    yield path
    _drain_queues()


def _insert_job(conn, table, job_uuid, status="queued"):
    if table == "qa_job":
        conn.execute(
            "INSERT INTO qa_job (job_uuid,chat_id,requester_id,question,status,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (job_uuid, BOT_HQ, ADMIN_ID, "question", status, _now()),
        )
    elif table == "pr_review":
        conn.execute(
            "INSERT INTO pr_review "
            "(review_uuid,claim_key,requested_by_id,chat_id,repo,pr_number,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_uuid, f"claim-{job_uuid}", ADMIN_ID, BOT_HQ,
             "leviathan-news/squid-bot", 1, status, _now()),
        )
    else:
        conn.execute(
            "INSERT INTO build_job "
            "(job_uuid,draft_uuid,chat_id,requester_id,target_repo,target_branch,"
            "job_payload_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_uuid, f"draft-{job_uuid}", BOT_HQ, ADMIN_ID,
             "leviathan-news/squid-bot", f"branch-{job_uuid}", "{}", status, _now()),
        )


def _insert_intent(conn, table, job_uuid, action, message_id=None):
    conn.execute(
        "INSERT INTO outgoing_msg "
        "(job_table,job_uuid,chat_id,action_type,intent_id,dedup_token,"
        "intent_recorded_at,telegram_message_id) VALUES (?,?,?,?,?,?,?,?)",
        (table, job_uuid, BOT_HQ, action,
         commodore._intent_id(job_uuid, action), f"token-{job_uuid}", _now(), message_id),
    )


@pytest.mark.parametrize("table,action,status", [
    ("qa_job", commodore.OutgoingAction.QA_ANSWER, "queued"),
    ("qa_job", commodore.OutgoingAction.QA_ANSWER, "in_progress"),
    ("pr_review", commodore.OutgoingAction.REVIEW_POST, "queued"),
    ("pr_review", commodore.OutgoingAction.REVIEW_POST, "in_progress"),
    ("build_job", commodore.OutgoingAction.BUILD_PR_LANDED, "queued"),
    ("build_job", commodore.OutgoingAction.BUILD_PR_LANDED, "in_progress"),
])
def test_boot_holds_uncertain_jobs_and_does_not_enqueue_or_call(
    isolated_db, monkeypatch, table, action, status,
):
    with sqlite3.connect(isolated_db) as conn:
        job_uuid = f"boot-{table}-{status}"
        _insert_job(conn, table, job_uuid, status)
        _insert_intent(conn, table, job_uuid, action)

    monkeypatch.setattr(commodore, "send_message", lambda *a, **k: pytest.fail("no Telegram call"))
    monkeypatch.setattr(commodore, "_gh_pr_list_for_branch", lambda *a: pytest.fail("no GitHub call"))
    monkeypatch.setattr(commodore.subprocess, "run", lambda *a, **k: pytest.fail("no provider call"))

    summary = commodore._recover_jobs_on_boot()
    assert summary["delivery_held"] == 1
    summary_key = {"qa_job": "qa", "pr_review": "review", "build_job": "build"}[table]
    assert summary[summary_key] == 0
    assert commodore._build_queue.empty()
    assert commodore._qa_queue.empty()
    assert commodore._review_queue.empty()
    with sqlite3.connect(isolated_db) as conn:
        status = conn.execute(
            f"SELECT status FROM {table} WHERE "
            f"{'review_uuid' if table == 'pr_review' else 'job_uuid'}=?",
            (f"boot-{table}-{status}",),
        ).fetchone()[0]
    assert status == "delivery_held"


@pytest.mark.parametrize("table,action,processor", [
    ("qa_job", commodore.OutgoingAction.QA_ANSWER, commodore._process_qa),
    ("pr_review", commodore.OutgoingAction.REVIEW_POST, commodore._process_review),
    ("build_job", commodore.OutgoingAction.BUILD_PR_LANDED, commodore._process_build),
])
def test_direct_processing_holds_before_provider_and_preserves_result(
    isolated_db, monkeypatch, table, action, processor,
):
    job_uuid = f"direct-{table}"
    with sqlite3.connect(isolated_db) as conn:
        _insert_job(conn, table, job_uuid, "queued")
        _insert_intent(conn, table, job_uuid, action)
    result_file = commodore.RESULTS_DIR / f"{job_uuid}.result.json"
    result_file.write_text(json.dumps({"status": "answered", "answer": "keep me"}))

    monkeypatch.setattr(commodore, "send_message", lambda *a, **k: pytest.fail("no Telegram call"))
    monkeypatch.setattr(commodore, "_gh_pr_list_for_branch", lambda *a: pytest.fail("no GitHub call"))
    monkeypatch.setattr(commodore.subprocess, "run", lambda *a, **k: pytest.fail("no provider call"))
    processor(job_uuid)

    assert result_file.exists()
    with sqlite3.connect(isolated_db) as conn:
        key = "review_uuid" if table == "pr_review" else "job_uuid"
        status, attempts = conn.execute(
            f"SELECT status,attempt_count FROM {table} WHERE {key}=?", (job_uuid,)
        ).fetchone()
    assert status == "delivery_held"
    assert attempts == 0


@pytest.mark.parametrize("table,action,processor,expected", [
    ("qa_job", commodore.OutgoingAction.QA_ANSWER, commodore._process_qa, "answered"),
    ("qa_job", commodore.OutgoingAction.QA_DECLINE, commodore._process_qa, "declined"),
    ("pr_review", commodore.OutgoingAction.REVIEW_POST, commodore._process_review, "posted"),
])
def test_positive_receipt_reconciles_without_provider(
    isolated_db, monkeypatch, table, action, processor, expected,
):
    job_uuid = f"receipt-{table}"
    with sqlite3.connect(isolated_db) as conn:
        _insert_job(conn, table, job_uuid, "in_progress")
        _insert_intent(conn, table, job_uuid, action, message_id=777)
    result_file = commodore.RESULTS_DIR / f"{job_uuid}.result.json"
    result_file.write_text(json.dumps({"status": "answered", "answer": "unused"}))
    monkeypatch.setattr(commodore, "send_message", lambda *a, **k: pytest.fail("no replay"))
    monkeypatch.setattr(commodore, "_gh_pr_list_for_branch", lambda *a: pytest.fail("no GitHub call"))
    monkeypatch.setattr(commodore.subprocess, "run", lambda *a, **k: pytest.fail("no provider call"))

    processor(job_uuid)

    with sqlite3.connect(isolated_db) as conn:
        key = "review_uuid" if table == "pr_review" else "job_uuid"
        status = conn.execute(f"SELECT status FROM {table} WHERE {key}=?", (job_uuid,)).fetchone()[0]
    assert status == expected
    assert not result_file.exists()


def test_post_crash_before_receipt_update_is_held_on_replay(isolated_db, monkeypatch):
    calls = []
    original_connect = sqlite3.connect
    fail_update = True

    class CrashOnReceiptUpdate(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal fail_update
            if fail_update and sql.lstrip().upper().startswith("UPDATE OUTGOING_MSG"):
                fail_update = False
                raise sqlite3.OperationalError("simulated crash before receipt persistence")
            return super().execute(sql, parameters)

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = CrashOnReceiptUpdate
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(commodore.sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(
        commodore, "send_message",
        lambda *a, **k: calls.append(a) or {"ok": True, "result": {"message_id": 888}},
    )
    with pytest.raises(sqlite3.OperationalError, match="simulated crash"):
        commodore.send_message_with_wal(
            "qa_job", "crash-job", commodore.OutgoingAction.QA_ANSWER, BOT_HQ, "answer"
        )

    replay = commodore.send_message_with_wal(
        "qa_job", "crash-job", commodore.OutgoingAction.QA_ANSWER, BOT_HQ, "answer"
    )
    assert replay["held"] is True
    assert len(calls) == 1

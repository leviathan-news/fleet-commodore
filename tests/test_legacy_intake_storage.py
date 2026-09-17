import json
import sqlite3

import pytest

from chat_intake import ChatIntake, IntakeError, IntakeFull


def update(update_id, text="hello"):
    return {"update_id": update_id, "message": {"text": text}}


def test_legacy_batch_is_held_retained_and_not_claimable(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")

    intake.ingest_legacy_batch([update(4, "legacy body")])

    assert intake.claim_next() is None
    with sqlite3.connect(intake.path) as conn:
        payload, status, claim_token, finished_at = conn.execute(
            "SELECT payload,status,claim_token,finished_at FROM chat_intake_event WHERE update_id=4"
        ).fetchone()
    assert json.loads(payload)["message"]["text"] == "legacy body"
    assert status == "held_unknown"
    assert claim_token is None
    assert finished_at is not None


def test_legacy_batch_advances_cursor_and_survives_reopen(tmp_path):
    path = tmp_path / "intake.db"
    intake = ChatIntake(path)

    intake.ingest_legacy_batch([update(9)])
    assert intake.offset() == 10
    reopened = ChatIntake(path)
    assert reopened.offset() == 10
    assert reopened.snapshot()["held_unknown"] == 1
    assert reopened.claim_next() is None


def test_legacy_batch_is_idempotent_and_preserves_original_row(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([update(3, "normal original")])
    intake.ingest_legacy_batch([update(3, "legacy replacement")])

    with sqlite3.connect(intake.path) as conn:
        payload, status = conn.execute(
            "SELECT payload,status FROM chat_intake_event WHERE update_id=3"
        ).fetchone()
    assert json.loads(payload)["message"]["text"] == "normal original"
    assert status == "queued"


def test_legacy_batch_capacity_rolls_back_rows_and_cursor(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db", max_pending=1)
    intake.ingest_legacy_batch([update(1)])

    with pytest.raises(IntakeFull):
        intake.ingest_legacy_batch([update(2)])
    assert intake.offset() == 2
    assert intake.snapshot()["held_unknown"] == 1
    with sqlite3.connect(intake.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM chat_intake_event WHERE update_id=2"
        ).fetchone()[0] == 0


def test_legacy_capture_marker_absent_then_set_with_held_rows(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    assert intake.legacy_capture_complete() is False

    intake.ingest_legacy_batch([update(1)])
    intake.mark_legacy_capture_complete()

    assert intake.legacy_capture_complete() is True
    assert ChatIntake(intake.path).legacy_capture_complete() is True


@pytest.mark.parametrize("state", ["queued", "running"])
def test_legacy_capture_marker_rejects_queued_or_running(tmp_path, state):
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([update(1)])
    if state == "running":
        assert intake.claim_next() is not None

    with pytest.raises(IntakeError, match="queued or running"):
        intake.mark_legacy_capture_complete()
    assert intake.legacy_capture_complete() is False


def test_legacy_batch_rolls_back_on_insert_failure(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    with sqlite3.connect(intake.path) as conn:
        conn.execute("""CREATE TRIGGER abort_legacy AFTER INSERT ON chat_intake_event
                        WHEN NEW.update_id=2 BEGIN SELECT RAISE(ABORT, 'injected'); END""")

    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        intake.ingest_legacy_batch([update(1), update(2)])
    assert intake.offset() == 0
    assert intake.snapshot()["held_unknown"] == 0

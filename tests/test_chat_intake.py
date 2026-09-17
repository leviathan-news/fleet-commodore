import json
import os
import sqlite3
import threading

import pytest

from chat_intake import ChatIntake, IntakeError, IntakeFull


def update(update_id, text="hello"):
    return {"update_id": update_id, "message": {"text": text}}


def test_atomic_batch_rolls_back_rows_and_cursor_on_trigger_abort(tmp_path):
    intake = ChatIntake(tmp_path / "state" / "intake.db")
    with sqlite3.connect(intake.path) as conn:
        conn.execute("""CREATE TRIGGER abort_second AFTER INSERT ON chat_intake_event
                        WHEN NEW.update_id=2 BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        intake.ingest_batch([update(1), update(2)])
    assert intake.offset() == 0
    assert intake.snapshot()["queued"] == 0


def test_dedupe_reopen_preserves_original_payload_and_cursor(tmp_path):
    path = tmp_path / "intake.db"
    intake = ChatIntake(path)
    intake.ingest_batch([update(8, "original")])
    intake.ingest_batch([update(8, "replacement")])
    reopened = ChatIntake(path)
    claimed = reopened.claim_next()
    assert claimed["payload"]["message"]["text"] == "original"
    assert reopened.offset() == 9


def test_cap_rejects_new_rows_without_cursor_advance_but_allows_duplicates(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db", max_pending=1)
    intake.ingest_batch([update(3)])
    with pytest.raises(IntakeFull):
        intake.ingest_batch([update(4)])
    assert intake.offset() == 4
    intake.ingest_batch([update(3, "ignored")])
    assert intake.offset() == 4


def test_claim_is_global_fifo_and_two_instances_do_not_duplicate(tmp_path):
    path = tmp_path / "intake.db"
    first, second = ChatIntake(path), ChatIntake(path)
    first.ingest_batch([update(2), update(1)])
    results = []
    barrier = threading.Barrier(2)

    def claim(intake):
        barrier.wait()
        results.append(intake.claim_next())

    threads = [threading.Thread(target=claim, args=(first,)), threading.Thread(target=claim, args=(second,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(item["update_id"] for item in results) == [1, 2]
    assert len({item["claim_token"] for item in results}) == 2


def test_stale_token_denied_and_ack_handoff_are_not_resolution(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([update(1), update(2), update(3)])
    first = intake.claim_next()
    with pytest.raises(IntakeError):
        intake.finish(1, "stale", "resolved")
    intake.finish(first["update_id"], first["claim_token"], "handed_off")
    second = intake.claim_next()
    intake.finish(second["update_id"], second["claim_token"], "held_unknown")
    snap = intake.snapshot()
    assert snap["handed_off"] == 1 and snap["held_unknown"] == 1
    assert snap["resolved"] == 0


def test_terminal_finish_clears_payload(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([update(10)])
    claimed = intake.claim_next()
    intake.finish(10, claimed["claim_token"], "resolved")
    with sqlite3.connect(intake.path) as conn:
        payload, status = conn.execute(
            "SELECT payload,status FROM chat_intake_event WHERE update_id=10"
        ).fetchone()
    assert json.loads(payload) == {}
    assert status == "resolved"


@pytest.mark.parametrize("bad", [None, {}, [update(True)], [update(-1)], ["not a dict"]])
def test_malformed_input_rejected(tmp_path, bad):
    intake = ChatIntake(tmp_path / "intake.db")
    with pytest.raises((TypeError, ValueError)):
        intake.ingest_batch(bad)


def test_oversize_and_batch_limit_rejected(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    with pytest.raises(ValueError):
        intake.ingest_batch([update(1, "x" * (256 * 1024))])
    with pytest.raises(ValueError):
        intake.ingest_batch([update(i) for i in range(101)])


def test_private_parent_and_file_modes_when_created(tmp_path):
    path = tmp_path / "private" / "nested" / "intake.db"
    ChatIntake(path)
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_connections_are_closed(monkeypatch, tmp_path):
    connections = []
    original_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            connections.append(self)
            return super().close()

    def connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([update(1)])
    intake.offset()
    assert len(connections) == 3


def test_symlink_database_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "target.db"
    target.write_bytes(b"do not touch")
    link = tmp_path / "intake.db"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        ChatIntake(link)
    assert target.read_bytes() == b"do not touch"


def test_existing_database_mode_is_untouched(tmp_path):
    path = tmp_path / "intake.db"
    path.write_bytes(b"")
    os.chmod(path, 0o640)
    ChatIntake(path)
    assert os.stat(path).st_mode & 0o777 == 0o640


def test_update_id_upper_bound_is_rejected_atomically(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    with pytest.raises(ValueError, match="SQLite"):
        intake.ingest_batch([update(ChatIntake._MAX_UPDATE_ID + 1)])
    assert intake.offset() == 0
    assert intake.snapshot()["queued"] == 0


def test_terminal_retention_never_removes_unresolved_or_rearms_old_ids(tmp_path):
    now = [0]
    intake = ChatIntake(tmp_path / "intake.db", clock=lambda: now[0])
    intake.ingest_batch([update(1), update(2)])
    first = intake.claim_next()
    intake.finish(1, first["claim_token"], "resolved")
    second = intake.claim_next()
    intake.finish(2, second["claim_token"], "held_unknown")
    now[0] = 31 * 86400
    assert intake.prune_terminal() == 1
    intake.ingest_batch([update(1, "must not replay")])
    assert intake.offset() == 3
    assert intake.snapshot()["held_unknown"] == 1
    assert intake.claim_next() is None


def test_handoff_receipt_completion_is_identity_fenced_and_idempotent(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([update(1)])
    claim = intake.claim_next()
    intake.finish(1, claim["claim_token"], "handed_off", job_table="qa_job", job_uuid="fixture")
    with pytest.raises(ValueError, match="positive receipt"):
        intake.complete_handoff(1, "qa_job", "fixture", "resolved", 0)
    assert not intake.complete_handoff(1, "qa_job", "foreign", "resolved", 42)
    assert intake.complete_handoff(1, "qa_job", "fixture", "resolved", 42)
    assert not intake.complete_handoff(1, "qa_job", "fixture", "resolved", 42)

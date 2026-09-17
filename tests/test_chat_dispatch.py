"""Poll intake must advance while a routing callback is blocked."""
import threading

import pytest

from chat_intake import ChatIntake
from chat_dispatch import ChatDispatcher, PollOwner, PollOwnerBusy


def test_poll_intake_is_independent_of_blocked_route(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def route(update):
        calls.append(update["update_id"])
        entered.set()
        assert release.wait(2)
        return {"outcome": "no_reply"}

    dispatcher = ChatDispatcher(intake, route)
    intake.ingest_batch([{"update_id": 1}])
    dispatcher.start()
    try:
        assert entered.wait(2)
        intake.ingest_batch([{"update_id": 2}])
        assert intake.offset() == 3
        assert intake.snapshot()["running"] == 1
        assert intake.snapshot()["queued"] == 1
        assert calls == [1]
    finally:
        release.set()
        assert dispatcher.stop(2)


def test_exception_is_held_not_replayed_or_resolved(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([{"update_id": 1}])

    def route(_update):
        raise RuntimeError("sensitive test fixture content")

    dispatcher = ChatDispatcher(intake, route)
    assert dispatcher.run_one()
    assert intake.snapshot()["held_unknown"] == 1
    assert not dispatcher.run_one()
    assert intake.snapshot()["resolved"] == 0


def test_no_positive_receipt_cannot_resolve_a_request(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([{"update_id": 1}])
    dispatcher = ChatDispatcher(intake, lambda _update: {"outcome": "resolved", "message_id": 0})
    dispatcher.run_one()
    assert intake.snapshot()["held_unknown"] == 1


def test_positive_receipt_and_job_ack_are_distinct(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([{"update_id": 1}, {"update_id": 2}])
    results = iter([
        {"outcome": "resolved", "message_id": 10},
        {"outcome": "handed_off", "job_table": "qa_job", "job_uuid": "fixture"},
    ])
    dispatcher = ChatDispatcher(intake, lambda _update: next(results))
    dispatcher.run_one()
    dispatcher.run_one()
    assert intake.snapshot()["resolved"] == 1
    assert intake.snapshot()["handed_off"] == 1


def test_poll_owner_is_exclusive_and_releases_after_close(tmp_path):
    path = tmp_path / "poll.lock"
    with PollOwner(path):
        with pytest.raises(PollOwnerBusy):
            with PollOwner(path):
                pass
    with PollOwner(path):
        pass


def test_maintenance_failure_does_not_starve_routing(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    intake.ingest_batch([{"update_id": 1}])
    routed = threading.Event()

    def route(_update):
        routed.set()
        return {"outcome": "no_reply"}

    def broken_maintenance():
        raise RuntimeError("private fixture")

    dispatcher = ChatDispatcher(intake, route, maintenance=broken_maintenance)
    dispatcher.start()
    try:
        assert routed.wait(2)
    finally:
        assert dispatcher.stop(2)

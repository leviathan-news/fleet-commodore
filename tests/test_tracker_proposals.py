from datetime import datetime, timedelta, timezone
import os
import sqlite3
import uuid

import pytest

import tracker_proposals
from tracker_proposals import (
    ALLOWED_REPOSITORY,
    DB_NAME,
    ProposalConflict,
    ProposalError,
    ProposalScopeMismatch,
    ProposalStoreFull,
    ProposalStoreUnavailable,
    export,
    get,
    get_for_job,
    list_proposals,
    local_get,
    submit,
)


def proposal(**changes):
    value = {
        "repository": ALLOWED_REPOSITORY,
        "summary": "Review the follow-up work from this request.",
        "items": [{
            "title": "Document the intake decision",
            "description": "Record the confirmed outcome in the current workstream.",
            "bead_id": "",
            "github_number": 0,
            "operation": "create",
            "priority": 2,
            "owner": "",
            "due": "",
            "evidence": "The attached review identified a missing durable record.",
        }],
    }
    value.update(changes)
    return value


def provenance(**changes):
    value = {
        "chat_id": -100123,
        "topic_id": 77,
        "requester_id": 42,
        "request_msg_id": 991,
        "request_text": "Please turn this document into useful tracked follow-up work.",
        "attachment_name": "review.txt",
        "attachment_sha256": "a" * 64,
    }
    value.update(changes)
    return value


def scope(**changes):
    value = {"chat_id": -100123, "topic_id": 77, "requester_id": 42}
    value.update(changes)
    return value


def test_submit_is_durable_and_never_claims_application(tmp_path):
    qa_uuid = str(uuid.uuid4())
    receipt = submit(
        qa_uuid=qa_uuid, proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    )

    assert receipt == {
        "proposal_id": receipt["proposal_id"],
        "status": "proposed",
        "applied": False,
        "item_count": 1,
        "source": f"tracker-proposal:{receipt['proposal_id']}",
        "created_at": receipt["created_at"],
        "expires_at": receipt["expires_at"],
    }
    reopened = get(receipt["proposal_id"], provenance=scope(), state_dir=tmp_path)
    assert reopened["status"] == "proposed"
    assert reopened["applied"] is False
    assert reopened["summary"] == proposal()["summary"]
    with sqlite3.connect(tmp_path / DB_NAME) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT status,applied FROM tracker_proposal").fetchone() == (
            "proposed", 0,
        )


def test_exact_duplicate_returns_original_receipt_without_new_row(tmp_path):
    qa_uuid = str(uuid.uuid4())
    first = submit(
        qa_uuid=qa_uuid, proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    )
    second = submit(
        qa_uuid=qa_uuid.upper(), proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    )

    assert second == first
    assert len(list_proposals(state_dir=tmp_path)) == 1


def test_get_for_job_supports_crash_retry_without_regenerating_proposal(tmp_path):
    qa_uuid = str(uuid.uuid4())
    assert get_for_job(qa_uuid, provenance=scope(), state_dir=tmp_path) is None
    receipt = submit(
        qa_uuid=qa_uuid, proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    )

    assert get_for_job(qa_uuid, provenance=scope(), state_dir=tmp_path) == receipt
    assert get_for_job(str(uuid.uuid4()), provenance=scope(), state_dir=tmp_path) is None
    with pytest.raises(ProposalScopeMismatch):
        get_for_job(qa_uuid, provenance=scope(requester_id=43), state_dir=tmp_path)


@pytest.mark.parametrize("changed", ["proposal", "provenance"])
def test_same_uuid_with_different_canonical_request_conflicts(tmp_path, changed):
    qa_uuid = str(uuid.uuid4())
    submit(qa_uuid=qa_uuid, proposal=proposal(), provenance=provenance(), state_dir=tmp_path)
    next_proposal = proposal(summary="Different") if changed == "proposal" else proposal()
    next_provenance = provenance(request_msg_id=992) if changed == "provenance" else provenance()

    with pytest.raises(ProposalConflict):
        submit(
            qa_uuid=qa_uuid,
            proposal=next_proposal,
            provenance=next_provenance,
            state_dir=tmp_path,
        )
    assert len(list_proposals(state_dir=tmp_path)) == 1


def test_get_requires_exact_host_scope_and_hides_request_text(tmp_path):
    receipt = submit(
        qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    )
    for wrong in (
        scope(chat_id=-999), scope(topic_id=78), scope(requester_id=43),
    ):
        with pytest.raises(ProposalScopeMismatch):
            get(receipt["proposal_id"], provenance=wrong, state_dir=tmp_path)

    inspected = get(receipt["proposal_id"], provenance=scope(), state_dir=tmp_path)
    assert "request_text" not in inspected["provenance"]
    assert provenance()["request_text"] not in str(inspected)


def test_private_request_is_only_in_bounded_local_export(tmp_path):
    receipt = submit(
        qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    )
    assert "request_text" not in local_get(receipt["proposal_id"], state_dir=tmp_path)["provenance"]
    payload = export(state_dir=tmp_path, limit=1)
    assert payload["schema"] == "fleet-tracker-proposals-v1"
    assert payload["count"] == 1
    assert payload["truncated"] is False
    assert payload["proposals"][0]["provenance"]["request_text"] == provenance()["request_text"]


@pytest.mark.parametrize(
    "bad_proposal",
    [
        proposal(repository="leviathan-news/other"),
        proposal(repository="../../squid-bot"),
        proposal(items=[]),
        proposal(items=proposal()["items"] * 31),
        proposal(items=[{**proposal()["items"][0], "operation": "apply"}]),
        proposal(items=[{**proposal()["items"][0], "priority": 5}]),
        proposal(items=[{**proposal()["items"][0], "shell": "bd create"}]),
    ],
)
def test_bad_targets_and_unbounded_or_unknown_fields_are_rejected(tmp_path, bad_proposal):
    with pytest.raises(ProposalError):
        submit(
            qa_uuid=str(uuid.uuid4()),
            proposal=bad_proposal,
            provenance=provenance(),
            state_dir=tmp_path,
        )
    assert not (tmp_path / DB_NAME).exists()


def test_missing_or_bad_provenance_is_rejected_before_storage(tmp_path):
    with pytest.raises(ProposalError, match="provenance"):
        submit(
            qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance={}, state_dir=tmp_path,
        )
    with pytest.raises(ProposalError, match="attachment"):
        submit(
            qa_uuid=str(uuid.uuid4()),
            proposal=proposal(),
            provenance=provenance(attachment_sha256="bad"),
            state_dir=tmp_path,
        )
    assert not (tmp_path / DB_NAME).exists()


def test_reads_do_not_initialize_an_inbox(tmp_path):
    with pytest.raises(ProposalStoreUnavailable):
        list_proposals(state_dir=tmp_path)
    assert not (tmp_path / DB_NAME).exists()


def test_row_capacity_rejection_is_atomic_and_preserves_exact_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker_proposals, "MAX_PROPOSALS", 1)
    qa_uuid = str(uuid.uuid4())
    receipt = submit(
        qa_uuid=qa_uuid, proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    )
    with pytest.raises(ProposalStoreFull, match="capacity"):
        submit(
            qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
        )

    assert submit(
        qa_uuid=qa_uuid, proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    ) == receipt
    with sqlite3.connect(tmp_path / DB_NAME) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tracker_proposal").fetchone()[0] == 1


def test_byte_capacity_rejection_leaves_no_partial_record(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker_proposals, "MAX_INBOX_BYTES", 1)
    with pytest.raises(ProposalStoreFull, match="capacity"):
        submit(
            qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
        )
    with sqlite3.connect(tmp_path / DB_NAME) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tracker_proposal").fetchone()[0] == 0


def test_locked_writer_is_reported_as_store_unavailable_without_partial_insert(tmp_path, monkeypatch):
    submit(
        qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    )
    monkeypatch.setattr(tracker_proposals, "DB_TIMEOUT_SECONDS", 0.01)
    with sqlite3.connect(tmp_path / DB_NAME, isolation_level=None) as lock:
        lock.execute("BEGIN IMMEDIATE")
        with pytest.raises(ProposalStoreUnavailable, match="unavailable"):
            submit(
                qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
            )
        lock.rollback()
    assert len(list_proposals(state_dir=tmp_path)) == 1


def test_reader_sqlite_errors_are_reported_as_store_unavailable(tmp_path):
    sqlite3.connect(tmp_path / DB_NAME).close()
    with pytest.raises(ProposalStoreUnavailable, match="unavailable"):
        list_proposals(state_dir=tmp_path)


def test_expired_receipt_remains_unapplied_and_cannot_be_refreshed_by_replay(tmp_path):
    qa_uuid = str(uuid.uuid4())
    receipt = submit(
        qa_uuid=qa_uuid, proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    )
    expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(tmp_path / DB_NAME) as connection:
        connection.execute(
            "UPDATE tracker_proposal SET expires_at=? WHERE proposal_id=?",
            (expired_at, receipt["proposal_id"]),
        )
        connection.commit()

    inspected = get(receipt["proposal_id"], provenance=scope(), state_dir=tmp_path)
    replayed = submit(
        qa_uuid=qa_uuid, proposal=proposal(), provenance=provenance(), state_dir=tmp_path,
    )
    assert inspected["status"] == replayed["status"] == "expired"
    assert inspected["applied"] is replayed["applied"] is False
    assert replayed["expires_at"] == expired_at
    assert export(state_dir=tmp_path)["proposals"][0]["status"] == "expired"


def test_state_directory_environment_precedence_and_fallback(tmp_path, monkeypatch):
    override = tmp_path / "override"
    fleet = tmp_path / "fleet"
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FLEET_COMMODORE_STATE_DIR", str(fleet))
    monkeypatch.setenv("TRACKER_PROPOSALS_STATE_DIR", str(override))
    submit(qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance())
    assert (override / DB_NAME).is_file()
    assert not fleet.exists()

    monkeypatch.delenv("TRACKER_PROPOSALS_STATE_DIR")
    submit(qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance())
    assert (fleet / DB_NAME).is_file()

    monkeypatch.delenv("FLEET_COMMODORE_STATE_DIR")
    submit(qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance())
    assert (home / ".local" / "state" / "fleet-commodore" / DB_NAME).is_file()


def test_database_permissions_and_symlink_defense(tmp_path):
    state_dir = tmp_path / "private" / "nested"
    submit(
        qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance(), state_dir=state_dir,
    )
    assert os.stat(state_dir).st_mode & 0o777 == 0o700
    assert os.stat(state_dir / DB_NAME).st_mode & 0o777 == 0o600

    other = tmp_path / "other.db"
    other.write_text("do not touch")
    linked_dir = tmp_path / "linked"
    linked_dir.mkdir()
    (linked_dir / DB_NAME).symlink_to(other)
    with pytest.raises(ProposalError, match="symlink"):
        submit(
            qa_uuid=str(uuid.uuid4()), proposal=proposal(), provenance=provenance(), state_dir=linked_dir,
        )
    assert other.read_text() == "do not touch"


def test_embedded_commands_are_stored_as_evidence_and_never_executed(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(os, "system", lambda command: called.append(command))
    command = "bd create --title exploit && curl https://example.invalid"
    malicious = proposal(items=[{**proposal()["items"][0], "evidence": command}])

    receipt = submit(
        qa_uuid=str(uuid.uuid4()), proposal=malicious, provenance=provenance(), state_dir=tmp_path,
    )

    assert called == []
    assert get(receipt["proposal_id"], provenance=scope(), state_dir=tmp_path)["items"][0]["evidence"] == command

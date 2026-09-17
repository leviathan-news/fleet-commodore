"""First-cursor capture cannot replay legacy requests or fabricate completion."""
import json
import sqlite3
from unittest.mock import Mock

import pytest

import commodore
from chat_intake import ChatIntake
import intake_cutover
from intake_cutover import CutoverError, capture_pending


def test_old_pending_questions_are_held_and_new_questions_can_route_after_capture(tmp_path):
    intake = ChatIntake(tmp_path / "chat-intake.db")
    fetch = Mock(side_effect=[
        {"ok": True, "result": [{"update_id": 12, "message": {"text": "old fixture"}}]},
        {"ok": True, "result": []},
    ])
    absent = Mock()
    result = capture_pending(intake, fetch, lambda update: update, absent)
    assert result == {"state": "legacy_capture_complete", "batches": 1, "offset": 13, "held": 1}
    assert [call.args[1]["offset"] for call in fetch.call_args_list] == [0, 13]
    assert absent.call_count == 4
    assert intake.claim_next() is None
    intake.ingest_batch([{"update_id": 13}])
    assert intake.claim_next()["update_id"] == 13
    with sqlite3.connect(intake.path) as conn:
        assert "old fixture" in conn.execute("SELECT payload FROM chat_intake_event WHERE update_id=12").fetchone()[0]


@pytest.mark.parametrize('response', [None, {}, {"ok": False, "result": []}, {"ok": 1, "result": []}, {"ok": True, "result": {}}])
def test_unconfirmed_capture_never_sets_readiness(tmp_path, response):
    intake = ChatIntake(tmp_path / "intake.db")
    with pytest.raises(CutoverError, match="unconfirmed"):
        capture_pending(intake, lambda *_args: response, lambda update: update, lambda: None)
    assert intake.offset() == 0
    assert not intake.legacy_capture_complete()


def test_capture_limit_retains_admitted_work_but_refuses_readiness(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    with pytest.raises(CutoverError, match="limit"):
        capture_pending(intake, lambda *_args: {"ok": True, "result": [{"update_id": 5}]},
                        lambda update: update, lambda: None, max_batches=1)
    assert intake.offset() == 6
    assert intake.snapshot()["held_unknown"] == 1
    assert not intake.legacy_capture_complete()
    result = capture_pending(intake, lambda *_args: {"ok": True, "result": []}, lambda update: update, lambda: None)
    assert result["offset"] == 6


def test_competing_actor_after_fetch_prevents_admission(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    absent = Mock(side_effect=[None, CutoverError("fleet_actor_still_active")])
    with pytest.raises(CutoverError, match="active"):
        capture_pending(intake, lambda *_args: {"ok": True, "result": [{"update_id": 1}]}, lambda update: update, absent)
    assert intake.offset() == 0
    assert not intake.legacy_capture_complete()


def test_minimization_precedes_legacy_persistence(tmp_path):
    intake = ChatIntake(tmp_path / "intake.db")
    fetch = Mock(side_effect=[
        {"ok": True, "result": [{"update_id": 2, "message": {"chat": {"id": -999}, "text": "private unknown"}}]},
        {"ok": True, "result": []},
    ])
    capture_pending(intake, fetch, commodore._admit_chat_update, lambda: None)
    with sqlite3.connect(intake.path) as conn:
        assert json.loads(conn.execute("SELECT payload FROM chat_intake_event").fetchone()[0]) == {"update_id": 2}


def test_boot_without_capture_does_not_contact_telegram_or_start_workers(monkeypatch, tmp_path):
    monkeypatch.setattr(commodore, "DB_FILE", tmp_path / "commodore.db")
    monkeypatch.setattr(commodore, "_HELM_CONTROLLER", None)
    network, workers, recovery = Mock(), Mock(), Mock()
    monkeypatch.setattr(commodore, "tg_request", network)
    monkeypatch.setattr(commodore, "_start_workers", workers)
    monkeypatch.setattr(commodore, "_recover_jobs_on_boot", recovery)
    with pytest.raises(RuntimeError, match="capture required"):
        commodore.poll()
    network.assert_not_called()
    workers.assert_not_called()
    recovery.assert_not_called()


def test_cli_refuses_workstation_before_opening_state_or_transport(monkeypatch, capsys):
    monkeypatch.setattr(intake_cutover.socket, 'gethostname', lambda: 'workstation.local')
    runtime, intake = Mock(), Mock()
    monkeypatch.setattr(intake_cutover, 'Runtime', runtime)
    monkeypatch.setattr(intake_cutover, 'ChatIntake', intake)
    assert intake_cutover.main(['--capture-legacy']) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'designated_mini_required'
    runtime.assert_not_called()
    intake.assert_not_called()


def test_cli_refuses_live_actor_before_opening_intake_or_transport(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(intake_cutover.socket, 'gethostname', lambda: 'gmacmini.local')
    monkeypatch.delenv('HELM_CONTROLLER_ENABLED', raising=False)
    monkeypatch.setenv('FLEET_COMMODORE_RELEASE_DIR', str(intake_cutover.Path(intake_cutover.__file__).parent))
    config = tmp_path / 'runtime.env'
    config.touch()
    monkeypatch.setenv('FLEET_COMMODORE_CONFIG', str(config))
    monkeypatch.setenv('FLEET_COMMODORE_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(intake_cutover, 'helm_allows_ordinary', lambda *_args: True)
    runtime = Mock()
    runtime.observe.return_value.actors = [object()]
    monkeypatch.setattr(intake_cutover, 'Runtime', lambda *_args: runtime)
    intake, network = Mock(), Mock()
    monkeypatch.setattr(intake_cutover, 'ChatIntake', intake)
    monkeypatch.setattr(commodore, 'tg_request', network)
    assert intake_cutover.main(['--capture-legacy']) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'fleet_actor_still_active'
    intake.assert_not_called()
    network.assert_not_called()

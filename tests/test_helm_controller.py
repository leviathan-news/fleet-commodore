import json
from pathlib import Path
import urllib.error

import pytest

from helm_controller import (
    DuplicateSendHeld,
    HelmController,
    HelmControllerError,
    ReplyLeaseDenied,
    stable_event_id,
    telegram_send_plain,
)
from helm_supervisor import CronGate, HelmSupervisor, RuntimeErrorSafe


class Clock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def update(update_id=41, message_id=7, text="status?"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1_786_000_000,
            "chat": {"id": -10055, "type": "supergroup"},
            "from": {"id": 123, "username": "operator"},
            "message_thread_id": 88,
            "text": text,
        },
    }


def controller(tmp_path, clock=None):
    return HelmController(tmp_path / "controller.db", clock=clock or Clock())


def acquire(ctrl, **overrides):
    values = {
        "sol_ttl": 100,
        "watcher_ttl": 100,
        "bridge_ttl": 100,
        "reason": "test takeover",
    }
    values.update(overrides)
    return ctrl.acquire_sol(**values)


def test_stable_event_id_prefers_update_id():
    assert stable_event_id(update()) == "telegram:update:41"


def test_enqueue_deduplicates_and_commits_cursor_with_context(tmp_path):
    clock = Clock()
    ctrl = controller(tmp_path, clock)
    first = ctrl.enqueue_update(update())
    second = ctrl.enqueue_update(update())

    assert first == {"event_id": "telegram:update:41", "inserted": True}
    assert second == {"event_id": "telegram:update:41", "inserted": False}
    assert ctrl.durable_offset() == 42

    reopened = HelmController(ctrl.db_path, clock=clock)
    assert reopened.durable_offset() == 42
    with reopened._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM telegram_events").fetchone()[0] == 1
        context = json.loads(
            conn.execute("SELECT context_json FROM conversation_context").fetchone()[0]
        )
    assert context["messages"][0]["text"] == "status?"


def test_exclusive_lease_queues_for_sol_and_denies_fleet_send(tmp_path):
    ctrl = controller(tmp_path)
    event_id = ctrl.enqueue_update(update())["event_id"]
    token = acquire(ctrl)

    assert ctrl.route_allowed("fleet", event_id) is False
    claimed = ctrl.claim_next(token)
    assert claimed["event_id"] == event_id

    with pytest.raises(ReplyLeaseDenied):
        ctrl.begin_send(actor="fleet", event_id=event_id, intent_hash="fleet")

    attempt = ctrl.begin_send(
        actor="sol", event_id=event_id, intent_hash="sol", token=token
    )
    ctrl.finish_send(attempt, status="accepted", telegram_message_id=99)
    assert ctrl.status()["queue"] == {"completed": 1}

    with pytest.raises(DuplicateSendHeld):
        ctrl.begin_send(
            actor="sol", event_id=event_id, intent_hash="again", token=token
        )


def test_inflight_fleet_send_blocks_atomic_takeover(tmp_path):
    ctrl = controller(tmp_path)
    ctrl.begin_send(actor="fleet", event_id="fleet:one", intent_hash="intent")
    with pytest.raises(ReplyLeaseDenied, match="still in flight"):
        acquire(ctrl)


@pytest.mark.parametrize("kind", ["sol", "watcher", "bridge"])
def test_each_exclusive_lease_expiry_requires_failback(tmp_path, kind):
    clock = Clock()
    ctrl = controller(tmp_path, clock)
    ttl = {"sol_ttl": 100, "watcher_ttl": 100, "bridge_ttl": 100}
    ttl[f"{kind}_ttl"] = 5
    acquire(ctrl, **ttl)
    clock.advance(6)

    needed, reason = ctrl.needs_failback()
    assert needed is True
    assert f"{kind} lease expired" in reason


def test_claim_survives_restart_and_is_reclaimable(tmp_path):
    clock = Clock()
    ctrl = controller(tmp_path, clock)
    event_id = ctrl.enqueue_update(update())["event_id"]
    token = acquire(ctrl)
    first = ctrl.claim_next(token, claim_ttl=5)
    assert first["event_id"] == event_id

    reopened = HelmController(ctrl.db_path, clock=clock)
    assert reopened.claim_next(token, claim_ttl=5) is None
    clock.advance(6)
    second = reopened.claim_next(token, claim_ttl=5)
    assert second["event_id"] == event_id
    assert second["claim_token"] != first["claim_token"]


def test_fleet_history_reconciliation_prevents_second_takeover_duplicate(tmp_path):
    ctrl = controller(tmp_path)
    ctrl.enqueue_update(update())
    history = tmp_path / "commodore.db"
    import sqlite3

    with sqlite3.connect(history) as conn:
        conn.execute(
            """CREATE TABLE chat_history (
               chat_id INTEGER, msg_id INTEGER, our_reply TEXT)"""
        )
        conn.execute(
            "INSERT INTO chat_history VALUES (?, ?, ?)", (-10055, 7, "done")
        )

    assert ctrl.reconcile_fleet_history(history) == 1
    assert ctrl.status()["queue"] == {"completed": 1}


class FakeRuntime:
    def __init__(self, root):
        self.fleet_release = root / "fleet-2988f713"
        self.successor_release = root / "successor-b"
        self.actor = self.fleet_release
        self.window = "commodore"
        self.stop_count = 0
        self.start_fleet_count = 0
        self.start_successor_count = 0

    def actor_release(self):
        return self.actor

    def windows(self):
        return {self.window} if self.actor else set()

    def stop_actor(self):
        self.stop_count += 1
        self.actor = None

    def start_fleet(self):
        assert self.actor is None
        self.start_fleet_count += 1
        self.actor = self.fleet_release

    def start_successor(self):
        assert self.actor is None
        self.start_successor_count += 1
        self.actor = self.successor_release

    def wait_for_release(self, release, timeout=30):
        return self.actor == release


def supervisor(tmp_path, clock):
    ctrl = controller(tmp_path, clock)
    runtime = FakeRuntime(tmp_path)
    commodore_db = tmp_path / "missing-commodore.db"
    sup = HelmSupervisor(
        ctrl,
        runtime,
        token_file=tmp_path / "sol.token",
        lock_file=tmp_path / "runtime.lock",
        commodore_db=commodore_db,
        sol_ttl=100,
        watcher_ttl=100,
        bridge_ttl=5,
    )
    return ctrl, runtime, sup


def test_supervisor_refuses_takeover_until_both_isolated_tests_pass(tmp_path):
    ctrl, _runtime, sup = supervisor(tmp_path, Clock())
    with pytest.raises(RuntimeErrorSafe, match="isolated"):
        sup.takeover("not ready")

    ctrl.record_verification("isolated_sol_bridge_loss", True, {"ok": True})
    ctrl.record_verification("isolated_watcher_loss", True, {"ok": True})
    status = sup.takeover("tests passed")
    assert status["holder"] == "sol"
    assert status["actor_release"].endswith("successor-b")


def test_bridge_loss_restores_exact_fleet_and_preserves_queue(tmp_path):
    clock = Clock()
    ctrl, runtime, sup = supervisor(tmp_path, clock)
    ctrl.record_verification("isolated_sol_bridge_loss", True, {"ok": True})
    ctrl.record_verification("isolated_watcher_loss", True, {"ok": True})
    ctrl.enqueue_update(update())
    sup.takeover("fault test")
    cursor_before = ctrl.durable_offset()

    clock.advance(6)
    status = sup.reconcile()

    assert status["holder"] == "fleet"
    assert runtime.actor == runtime.fleet_release
    assert ctrl.durable_offset() == cursor_before
    assert ctrl.status()["queue"] == {"queued": 1}


def test_second_atomic_takeover_after_failback(tmp_path):
    clock = Clock()
    ctrl, runtime, sup = supervisor(tmp_path, clock)
    ctrl.record_verification("isolated_sol_bridge_loss", True, {"ok": True})
    ctrl.record_verification("isolated_watcher_loss", True, {"ok": True})
    sup.takeover("first")
    clock.advance(6)
    sup.reconcile()
    assert runtime.actor == runtime.fleet_release

    status = sup.takeover("second")
    assert status["holder"] == "sol"
    assert runtime.actor == runtime.successor_release
    assert runtime.start_successor_count == 2


class MemoryCronGate(CronGate):
    def __init__(self, tmp_path, lines):
        super().__init__(
            namespace="test-watch",
            fleet_release=Path("/release/fleet-2988f713"),
            successor_release=Path("/release/successor-b"),
            controller_config=Path("/state/controller.env"),
            state_file=tmp_path / "cron-handoff.json",
        )
        self.lines = list(lines)

    def _lines(self):
        return list(self.lines)

    def _run(self, args, *, input_text=None, check=True):
        if args == ["/usr/bin/crontab", "-"]:
            self.lines = input_text.splitlines()

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()
        raise AssertionError(args)


def test_cron_gate_replaces_and_restores_only_exact_watchdog_row(tmp_path):
    original = "*/5 * * * * /release/fleet-2988f713/cron/watchdog.sh"
    unrelated = "17 * * * * /usr/local/bin/unrelated"
    gate = MemoryCronGate(tmp_path, [unrelated, original])

    gate.install()
    assert gate.status() == {
        "ordinary_rows": 0,
        "controller_rows": 1,
        "state_preserved": True,
    }
    assert unrelated in gate.lines
    assert original not in gate.lines

    gate.restore()
    assert gate.lines == [unrelated, original]
    assert gate.status()["ordinary_rows"] == 1
    assert gate.status()["controller_rows"] == 0


def test_cron_gate_refuses_ambiguous_ordinary_rows(tmp_path):
    original = "*/5 * * * * /release/fleet-2988f713/cron/watchdog.sh"
    gate = MemoryCronGate(tmp_path, [original, original])
    with pytest.raises(RuntimeErrorSafe, match="ordinary=2"):
        gate.install()


def test_commodore_send_honors_fleet_reply_lease(tmp_path, monkeypatch):
    import commodore

    ctrl = controller(tmp_path)
    calls = []
    monkeypatch.setattr(commodore, "_HELM_CONTROLLER", ctrl)
    monkeypatch.setattr(commodore, "HELM_ACTOR", "fleet")
    monkeypatch.setattr(
        commodore,
        "tg_request",
        lambda method, data=None: calls.append((method, data))
        or {"ok": True, "result": {"message_id": 501}},
    )

    result = commodore.send_message(-10055, "ready", reply_to=7)
    assert result["result"]["message_id"] == 501
    assert len(calls) == 1

    acquire(ctrl)
    with pytest.raises(ReplyLeaseDenied):
        commodore.send_message(-10055, "must not send", reply_to=8)
    assert len(calls) == 1


def test_controlled_send_never_retries_ambiguous_network_outcome(tmp_path, monkeypatch):
    import commodore

    ctrl = controller(tmp_path)
    calls = []
    monkeypatch.setattr(commodore, "_HELM_CONTROLLER", ctrl)
    monkeypatch.setattr(commodore, "HELM_ACTOR", "fleet")

    def timeout(method, data=None):
        calls.append((method, data))
        raise TimeoutError("ambiguous")

    monkeypatch.setattr(commodore, "tg_request", timeout)
    with pytest.raises(TimeoutError):
        commodore.send_message(-10055, "one attempt", reply_to=9)
    assert len(calls) == 1
    assert ctrl.status()["queue"] == {}
    with ctrl._connect() as conn:
        assert conn.execute("SELECT status FROM send_attempts").fetchone()[0] == "outcome_unknown"


def test_controlled_send_retries_after_explicit_html_rejection(tmp_path, monkeypatch):
    import commodore

    ctrl = controller(tmp_path)
    calls = []
    monkeypatch.setattr(commodore, "_HELM_CONTROLLER", ctrl)
    monkeypatch.setattr(commodore, "HELM_ACTOR", "fleet")

    def reject_then_accept(method, data=None):
        calls.append((method, data))
        if len(calls) == 1:
            raise ValueError("known parse rejection")
        return {"ok": True, "result": {"message_id": 700}}

    monkeypatch.setattr(commodore, "tg_request", reject_then_accept)
    result = commodore.send_message(-10055, "safe fallback", reply_to=10)
    assert result["result"]["message_id"] == 700
    assert len(calls) == 2
    with ctrl._connect() as conn:
        assert conn.execute("SELECT status FROM send_attempts").fetchone()[0] == "accepted"


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_sol_sender_loads_token_file_and_retries_only_after_http_rejection(
    tmp_path, monkeypatch
):
    ctrl = controller(tmp_path)
    event_id = ctrl.enqueue_update(update())["event_id"]
    token = acquire(ctrl)
    token_file = tmp_path / "bot-token"
    token_file.write_text("test-token\n", encoding="utf-8")
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("BOT_TOKEN_FILE", str(token_file))
    calls = []

    def reject(request, timeout):
        calls.append((request.full_url, timeout))
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr("helm_controller.urllib.request.urlopen", reject)
    with pytest.raises(urllib.error.HTTPError):
        telegram_send_plain(
            ctrl,
            token=token,
            event_id=event_id,
            chat_id=123,
            text="canary",
        )
    with ctrl._connect() as conn:
        assert conn.execute("SELECT status FROM send_attempts").fetchone()[0] == "failed"

    monkeypatch.setattr(
        "helm_controller.urllib.request.urlopen",
        lambda request, timeout: FakeHTTPResponse(
            {"ok": True, "result": {"message_id": 44}}
        ),
    )
    result = telegram_send_plain(
        ctrl,
        token=token,
        event_id=event_id,
        chat_id=123,
        text="canary",
    )
    assert result["result"]["message_id"] == 44


def test_manual_known_rejection_requeues_legacy_http_unknown(tmp_path):
    ctrl = controller(tmp_path)
    event_id = ctrl.enqueue_update(update())["event_id"]
    token = acquire(ctrl)
    attempt = ctrl.begin_send(
        actor="sol", event_id=event_id, intent_hash="old", token=token
    )
    ctrl.finish_send(attempt, status="outcome_unknown", error="HTTPError")

    resolved = ctrl.resolve_known_rejection(
        attempt, evidence="Telegram returned HTTP 400 before acceptance"
    )

    assert resolved == event_id
    assert ctrl.status()["queue"] == {"queued": 1}
    with ctrl._connect() as conn:
        row = conn.execute(
            "SELECT status, error FROM send_attempts WHERE attempt_id=?", (attempt,)
        ).fetchone()
    assert row["status"] == "failed"
    assert "HTTP 400" in row["error"]


def test_imported_private_destination_reconciles_only_to_sender(tmp_path):
    ctrl = controller(tmp_path)
    imported = update()
    imported["message"]["chat"] = {"id": 999_999, "type": "private"}
    event_id = ctrl.enqueue_update(imported)["event_id"]

    with pytest.raises(HelmControllerError, match="authenticated sender"):
        ctrl.reconcile_private_destination(
            event_id, chat_id=456, evidence="wrong target"
        )

    ctrl.reconcile_private_destination(
        event_id,
        chat_id=123,
        evidence="archive peer maps to the authenticated Bot API private chat",
    )
    with ctrl._connect() as conn:
        row = conn.execute(
            "SELECT chat_id, payload_json FROM telegram_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
    assert row["chat_id"] == 123
    assert json.loads(row["payload_json"])["message"]["chat"]["id"] == 123

#!/usr/bin/env python3
"""Kill isolated bridge and watcher processes and verify exact-fallback state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from helm_controller import HelmController, write_token_file  # noqa: E402
from helm_supervisor import HelmSupervisor  # noqa: E402


class NamespaceRuntime:
    """Two harmless sleep processes standing in for blue and green actors."""

    def __init__(self, root: Path):
        self.fleet_release = root / "fleet-2988f713"
        self.successor_release = root / "successor-b"
        self.window = "commodore-test"
        self.actor = self.fleet_release
        self.process = self._spawn()

    def _spawn(self):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def actor_release(self):
        return self.actor if self.process and self.process.poll() is None else None

    def windows(self):
        return {self.window} if self.actor_release() else set()

    def stop_actor(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.process = None
        self.actor = None

    def start_fleet(self):
        assert self.actor_release() is None
        self.actor = self.fleet_release
        self.process = self._spawn()

    def start_successor(self):
        assert self.actor_release() is None
        self.actor = self.successor_release
        self.process = self._spawn()

    def wait_for_release(self, release, timeout=30):
        return self.actor_release() == release

    def cleanup(self):
        self.stop_actor()


def wait_until(predicate, timeout=8.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def renew_other_leases(controller, token, *, skip, duration):
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        for kind in ("sol", "watcher", "bridge"):
            if kind != skip:
                controller.renew(kind, token, 5)
        time.sleep(0.1)


def arm(controller, runtime, token_file, reason):
    runtime.stop_actor()
    token = controller.acquire_sol(
        sol_ttl=5, watcher_ttl=5, bridge_ttl=5, reason=reason
    )
    write_token_file(token_file, token)
    runtime.start_successor()
    return token


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    started = time.monotonic()
    receipts = {}

    with tempfile.TemporaryDirectory(prefix="helm-controller-fault-") as raw:
        root = Path(raw)
        controller = HelmController(root / "controller.db")
        runtime = NamespaceRuntime(root)
        token_file = root / "sol.token"
        supervisor = HelmSupervisor(
            controller,
            runtime,
            token_file=token_file,
            lock_file=root / "runtime.lock",
            commodore_db=root / "missing-commodore.db",
            sol_ttl=5,
            watcher_ttl=5,
            bridge_ttl=1,
        )
        try:
            event = {
                "update_id": 100,
                "message": {
                    "message_id": 10,
                    "chat": {"id": -1001},
                    "from": {"id": 1},
                    "text": "preserve me",
                },
            }
            controller.enqueue_update(event)
            token = arm(controller, runtime, token_file, "isolated bridge loss")
            bridge = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "helm_sol_bridge.py"),
                    "--db", str(controller.db_path),
                    "--token-file", str(token_file),
                    "--bridge-ttl", "1",
                    "--claim-ttl", "30",
                    "--poll-seconds", "0.1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert wait_until(
                lambda: 0.5 < controller.status()["sol_health"]["remaining_seconds"]["bridge"] <= 1.1
            )
            bridge.terminate()
            bridge.wait(timeout=5)
            fault_started = time.monotonic()
            renew_other_leases(controller, token, skip="bridge", duration=1.4)
            assert controller.needs_failback()[0]
            supervisor.reconcile()
            bridge_bound = time.monotonic() - fault_started
            assert runtime.actor_release() == runtime.fleet_release
            assert controller.durable_offset() == 101
            assert sum(controller.status()["queue"].values()) == 1
            receipts["isolated_sol_bridge_loss"] = {
                "passed": True,
                "failback_seconds": round(bridge_bound, 3),
                "cursor": 101,
                "queue_rows": 1,
            }

            token = arm(controller, runtime, token_file, "isolated watcher loss")
            renewer = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tests/fixtures/helm_test_lease_renewer.py"),
                    "--db", str(controller.db_path),
                    "--token-file", str(token_file),
                    "--kind", "watcher",
                    "--ttl", "1",
                    "--interval", "0.1",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert wait_until(
                lambda: 0.5 < controller.status()["sol_health"]["remaining_seconds"]["watcher"] <= 1.1
            )
            renewer.terminate()
            renewer.wait(timeout=5)
            fault_started = time.monotonic()
            renew_other_leases(controller, token, skip="watcher", duration=1.4)
            assert controller.needs_failback()[0]
            supervisor.reconcile()
            watcher_bound = time.monotonic() - fault_started
            assert runtime.actor_release() == runtime.fleet_release
            assert controller.durable_offset() == 101
            assert sum(controller.status()["queue"].values()) == 1
            receipts["isolated_watcher_loss"] = {
                "passed": True,
                "failback_seconds": round(watcher_bound, 3),
                "cursor": 101,
                "queue_rows": 1,
            }
        finally:
            runtime.cleanup()

    payload = {
        "ok": all(item["passed"] for item in receipts.values()),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "receipts": receipts,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

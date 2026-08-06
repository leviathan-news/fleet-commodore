#!/usr/bin/env python3
"""Blue/green process supervisor for the durable helm controller."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

from helm_controller import HelmController, write_token_file


class RuntimeErrorSafe(RuntimeError):
    """Operational failure with secret-free text safe for durable receipts."""


class CronGate:
    """Replace and restore only the exact ordinary Fleet watchdog row."""

    def __init__(
        self,
        *,
        namespace: str,
        fleet_release: Path,
        successor_release: Path,
        controller_config: Path,
        state_file: Path,
    ):
        self.namespace = namespace
        self.fleet_release = fleet_release
        self.successor_release = successor_release
        self.controller_config = controller_config
        self.state_file = state_file
        self.marker = f"# FLEET_HELM_CONTROLLER {namespace}"

    def _run(self, args, *, input_text=None, check=True):
        result = subprocess.run(
            args,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()[:500]
            raise RuntimeErrorSafe(f"crontab operation failed: {detail}")
        return result

    def _lines(self) -> list[str]:
        result = self._run(["/usr/bin/crontab", "-l"], check=False)
        if result.returncode and "no crontab" not in result.stderr.lower():
            raise RuntimeErrorSafe("current crontab is unreadable")
        return result.stdout.splitlines()

    def _ordinary_rows(self, lines: list[str]) -> list[str]:
        needle = str(self.fleet_release / "cron/watchdog.sh")
        return [line for line in lines if needle in line and not line.lstrip().startswith("#")]

    def _controller_rows(self, lines: list[str]) -> list[str]:
        return [line for line in lines if self.marker in line]

    def status(self) -> dict:
        lines = self._lines()
        return {
            "ordinary_rows": len(self._ordinary_rows(lines)),
            "controller_rows": len(self._controller_rows(lines)),
            "state_preserved": self.state_file.exists(),
        }

    def _write_state(self, payload: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, self.state_file)

    def install(self) -> None:
        lines = self._lines()
        ordinary = self._ordinary_rows(lines)
        controller = self._controller_rows(lines)
        if len(ordinary) != 1 or controller:
            raise RuntimeErrorSafe(
                f"cron handoff requires ordinary=1/controller=0; observed "
                f"ordinary={len(ordinary)}/controller={len(controller)}"
            )
        original = ordinary[0]
        replacement = (
            "* * * * * "
            f"HELM_CONTROLLER_RUNTIME_CONFIG={self.controller_config} "
            f"{self.successor_release / 'cron/helm-controller-watchdog.sh'} "
            f"{self.marker}"
        )
        self._write_state(
            {
                "namespace": self.namespace,
                "original_row": original,
                "controller_row": replacement,
                "installed_at": time.time(),
            }
        )
        updated = [replacement if line == original else line for line in lines]
        self._run(["/usr/bin/crontab", "-"], input_text="\n".join(updated) + "\n")
        verified = self.status()
        if verified["ordinary_rows"] != 0 or verified["controller_rows"] != 1:
            raise RuntimeErrorSafe("controller watchdog row failed post-install verification")

    def restore(self) -> None:
        if not self.state_file.exists():
            raise RuntimeErrorSafe("preserved ordinary watchdog row is missing")
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        if state.get("namespace") != self.namespace:
            raise RuntimeErrorSafe("preserved cron namespace does not match")
        original = state.get("original_row")
        controller_row = state.get("controller_row")
        if not original or not controller_row:
            raise RuntimeErrorSafe("preserved cron handoff is incomplete")
        lines = self._lines()
        ordinary = self._ordinary_rows(lines)
        controller = self._controller_rows(lines)
        if len(ordinary) == 1 and ordinary[0] == original and not controller:
            return
        if ordinary or controller != [controller_row]:
            raise RuntimeErrorSafe(
                f"cron restore requires ordinary=0/controller=1; observed "
                f"ordinary={len(ordinary)}/controller={len(controller)}"
            )
        updated = [original if line == controller_row else line for line in lines]
        self._run(["/usr/bin/crontab", "-"], input_text="\n".join(updated) + "\n")
        verified = self.status()
        if verified["ordinary_rows"] != 1 or verified["controller_rows"] != 0:
            raise RuntimeErrorSafe("ordinary watchdog row failed post-restore verification")


class TmuxRuntime:
    def __init__(
        self,
        *,
        session: str,
        window: str,
        config: Path,
        fleet_release: Path,
        successor_release: Path,
        db_path: Path,
        watcher_ttl: int = 300,
    ):
        self.session = session
        self.window = window
        self.config = config
        self.fleet_release = fleet_release
        self.successor_release = successor_release
        self.db_path = db_path
        self.watcher_ttl = watcher_ttl
        self.tmux = "/opt/homebrew/bin/tmux"

    def _run(self, command: list[str], *, check=True) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.pop("TMUX", None)
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()[:500]
            raise RuntimeErrorSafe(f"command failed ({result.returncode}): {detail}")
        return result

    def windows(self) -> set[str]:
        result = self._run(
            [self.tmux, "list-windows", "-t", self.session, "-F", "#{window_name}"],
            check=False,
        )
        if result.returncode:
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def stop_actor(self) -> None:
        if self.window in self.windows():
            self._run([self.tmux, "kill-window", "-t", f"{self.session}:{self.window}"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.actor_release() is None:
                return
            time.sleep(0.25)
        raise RuntimeErrorSafe("Telegram actor did not stop within 15 seconds")

    def _start(self, release: Path, *, controlled: bool) -> None:
        if self.window in self.windows() or self.actor_release() is not None:
            raise RuntimeErrorSafe("refusing to start a second Telegram actor")
        prefix = ""
        if controlled:
            prefix = (
                f"HELM_CONTROLLER_ENABLED=1 "
                f"HELM_CONTROLLER_DB_FILE={self.db_path} "
                f"HELM_WATCHER_TTL_SECONDS={self.watcher_ttl} "
                "HELM_ACTOR=fleet "
            )
        command = (
            f"FLEET_COMMODORE_CONFIG={self.config} "
            f"FLEET_COMMODORE_RELEASE_DIR={release} "
            f"{prefix}{release / 'run.sh'}"
        )
        if not self.windows():
            self._run(
                [self.tmux, "new-session", "-d", "-s", self.session, "-n", self.window, command]
            )
        else:
            self._run(
                [self.tmux, "new-window", "-d", "-t", self.session, "-n", self.window, command]
            )

    def start_fleet(self) -> None:
        self._start(self.fleet_release, controlled=False)

    def start_successor(self) -> None:
        self._start(self.successor_release, controlled=True)

    def actor_release(self) -> Path | None:
        result = self._run(
            ["/bin/ps", "-axo", "pid=,command="], check=False
        )
        candidates = []
        for line in result.stdout.splitlines():
            if " -u commodore.py" not in line:
                continue
            try:
                pid = int(line.strip().split(None, 1)[0])
            except (ValueError, IndexError):
                continue
            probe = self._run(
                ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], check=False
            )
            cwd = None
            for item in probe.stdout.splitlines():
                if item.startswith("n/"):
                    cwd = Path(item[1:])
            if cwd:
                candidates.append(cwd)
        if len(candidates) > 1:
            raise RuntimeErrorSafe("more than one Fleet Telegram actor is running")
        return candidates[0] if candidates else None

    def wait_for_release(self, release: Path, timeout: int = 30) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.actor_release() == release:
                    return True
            except RuntimeErrorSafe:
                return False
            time.sleep(0.5)
        return False


class HelmSupervisor:
    def __init__(
        self,
        controller: HelmController,
        runtime: TmuxRuntime,
        *,
        token_file: Path,
        lock_file: Path,
        commodore_db: Path,
        sol_ttl: int,
        watcher_ttl: int,
        bridge_ttl: int,
        cron_gate: CronGate | None = None,
    ):
        self.controller = controller
        self.runtime = runtime
        self.token_file = token_file
        self.lock_file = lock_file
        self.commodore_db = commodore_db
        self.sol_ttl = sol_ttl
        self.watcher_ttl = watcher_ttl
        self.bridge_ttl = bridge_ttl
        self.cron_gate = cron_gate
        self.lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def process_lock(self):
        with self.lock_file.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _tests_passed(self) -> bool:
        tests = self.controller.status()["tests"]
        return bool(
            tests.get("isolated_sol_bridge_loss")
            and tests.get("isolated_watcher_loss")
        )

    def takeover(self, reason: str) -> dict:
        if not self._tests_passed():
            raise RuntimeErrorSafe("isolated bridge-loss and watcher-loss receipts are required")
        with self.process_lock():
            current = self.runtime.actor_release()
            if current != self.runtime.fleet_release:
                raise RuntimeErrorSafe(
                    f"takeover requires exact Fleet baseline; observed {current}"
                )
            self.controller.reconcile_fleet_history(self.commodore_db)
            if self.cron_gate is not None:
                self.cron_gate.install()
            try:
                self.runtime.stop_actor()
                token = self.controller.acquire_sol(
                    sol_ttl=self.sol_ttl,
                    watcher_ttl=self.watcher_ttl,
                    bridge_ttl=self.bridge_ttl,
                    reason=reason,
                )
                write_token_file(self.token_file, token)
                self.runtime.start_successor()
                if not self.runtime.wait_for_release(self.runtime.successor_release):
                    raise RuntimeErrorSafe("successor did not become the sole Telegram actor")
            except Exception as exc:
                self._failback_locked(f"takeover failure: {type(exc).__name__}")
                raise
        return self.status()

    def _failback_locked(self, reason: str) -> dict:
        self.controller.begin_transition(reason)
        try:
            observed = self.runtime.actor_release()
            if observed != self.runtime.fleet_release:
                if observed is not None:
                    self.runtime.stop_actor()
                self.runtime.start_fleet()
            if not self.runtime.wait_for_release(self.runtime.fleet_release):
                raise RuntimeErrorSafe("exact Fleet release did not start")
        except Exception as exc:
            self.controller.coverage_lost(
                f"{reason}; failback failed: {type(exc).__name__}"
            )
            raise
        self.controller.complete_fleet(reason)
        if self.cron_gate is not None:
            try:
                self.cron_gate.restore()
                self.controller.set_meta("cron_restore_error", None)
            except Exception as exc:
                self.controller.set_meta(
                    "cron_restore_error",
                    {"type": type(exc).__name__, "at": time.time()},
                )
        return self.status()

    def failback(self, reason: str) -> dict:
        with self.process_lock():
            return self._failback_locked(reason)

    def reconcile(self) -> dict:
        """Enforce process outcome from durable reply ownership."""
        with self.process_lock():
            status = self.controller.status()
            observed = self.runtime.actor_release()
            if status["holder"] == "sol":
                needs, reason = self.controller.needs_failback()
                if needs:
                    return self._failback_locked(reason or "exclusive lease unhealthy")
                if observed != self.runtime.successor_release:
                    return self._failback_locked("successor process lost")
            elif status["holder"] == "fleet":
                if observed != self.runtime.fleet_release:
                    if observed is not None:
                        self.runtime.stop_actor()
                    self.runtime.start_fleet()
                    if not self.runtime.wait_for_release(self.runtime.fleet_release):
                        self.controller.coverage_lost("ordinary Fleet reconciliation failed")
                        raise RuntimeErrorSafe("ordinary Fleet reconciliation failed")
            return self.status()

    def status(self) -> dict:
        state = self.controller.status()
        state["actor_release"] = str(self.runtime.actor_release() or "")
        state["actor_window"] = self.runtime.window in self.runtime.windows()
        if self.cron_gate is not None:
            state["cron"] = self.cron_gate.status()
        return state

    def monitor(self, interval: float = 2.0) -> None:
        while True:
            self.reconcile()
            time.sleep(interval)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--lock-file", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--fleet-release", required=True)
    parser.add_argument("--successor-release", required=True)
    parser.add_argument("--commodore-db", required=True)
    parser.add_argument("--session", default="leviathan")
    parser.add_argument("--window", default="commodore")
    parser.add_argument("--sol-ttl", type=int, default=2400)
    parser.add_argument("--watcher-ttl", type=int, default=300)
    parser.add_argument("--bridge-ttl", type=int, default=120)
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--controller-config")
    parser.add_argument("--cron-state-file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("reconcile")
    takeover = sub.add_parser("takeover")
    takeover.add_argument("reason")
    failback = sub.add_parser("failback")
    failback.add_argument("reason")
    monitor = sub.add_parser("monitor")
    monitor.add_argument("--interval", type=float, default=2.0)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    controller = HelmController(args.db)
    runtime = TmuxRuntime(
        session=args.session,
        window=args.window,
        config=Path(args.config),
        fleet_release=Path(args.fleet_release),
        successor_release=Path(args.successor_release),
        db_path=Path(args.db),
        watcher_ttl=args.watcher_ttl,
    )
    cron_gate = None
    if args.controller_config and args.cron_state_file:
        cron_gate = CronGate(
            namespace=args.namespace,
            fleet_release=Path(args.fleet_release),
            successor_release=Path(args.successor_release),
            controller_config=Path(args.controller_config),
            state_file=Path(args.cron_state_file),
        )
    supervisor = HelmSupervisor(
        controller,
        runtime,
        token_file=Path(args.token_file),
        lock_file=Path(args.lock_file),
        commodore_db=Path(args.commodore_db),
        sol_ttl=args.sol_ttl,
        watcher_ttl=args.watcher_ttl,
        bridge_ttl=args.bridge_ttl,
        cron_gate=cron_gate,
    )
    if args.command == "status":
        print(json.dumps(supervisor.status(), sort_keys=True))
    elif args.command == "reconcile":
        print(json.dumps(supervisor.reconcile(), sort_keys=True))
    elif args.command == "takeover":
        print(json.dumps(supervisor.takeover(args.reason), sort_keys=True))
    elif args.command == "failback":
        print(json.dumps(supervisor.failback(args.reason), sort_keys=True))
    elif args.command == "monitor":
        supervisor.monitor(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Model-free, fail-closed supervision of the ordinary Fleet process.

Never kill a pane, restart a live actor, steal a takeover lease, or treat a
failed process probe as proof of absence. The existing cron owns invocation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import time

from chat_dispatch import PollOwner, PollOwnerBusy


class UnsafeObservation(RuntimeError):
    """Fixed reason only: never include probe output, config or command lines."""


_SCRIPT_MENTION = re.compile(r"(?:^|[/\s\"'])commodore\.py(?=[\s\"']|$)")


@dataclass(frozen=True)
class Pane:
    identity: str
    dead: bool
    pid: int


@dataclass(frozen=True)
class Actor:
    pid: int
    release: Path
    in_pane: bool


@dataclass(frozen=True)
class Observation:
    session: bool
    panes: tuple[Pane, ...]
    actors: tuple[Actor, ...]


def _process_rows(text):
    rows, seen = [], set()
    for line in text.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) != 4:
            raise UnsafeObservation("process_snapshot_invalid")
        try:
            pid, parent = int(fields[0]), int(fields[1])
            # Unrelated command lines may contain unbalanced user text. They
            # still contribute parent PIDs but need no argv interpretation.
            args = shlex.split(fields[3]) if _SCRIPT_MENTION.search(fields[3]) else [fields[3]]
        except ValueError:
            raise UnsafeObservation("process_snapshot_invalid") from None
        if pid <= 0 or parent < 0 or pid in seen or not args:
            raise UnsafeObservation("process_snapshot_invalid")
        seen.add(pid)
        rows.append((pid, parent, fields[2], args))
    if not rows:
        raise UnsafeObservation("process_snapshot_empty")
    return rows


def _candidates(rows):
    candidates = []
    for pid, parent, status, args in rows:
        if "Z" in status:
            continue  # A zombie cannot poll or send.
        scripts = [Path(arg) for arg in args[1:] if Path(arg).name == "commodore.py"]
        # A shell/launcher that mentions this script is not proof of absence.
        if any(_SCRIPT_MENTION.search(arg) for arg in args) and not scripts:
            if Path(args[0]).name not in {"ps", "lsof"}:
                raise UnsafeObservation("actor_command_ambiguous")
        if not scripts:
            continue
        if not re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|Python)", Path(args[0]).name) or len(scripts) != 1:
            raise UnsafeObservation("actor_command_ambiguous")
        candidates.append((pid, parent, scripts[0]))
    return candidates


def parse_processes(text):
    return _candidates(_process_rows(text))


def decide(snapshot, release):
    if len(snapshot.actors) > 1:
        return "hold_duplicate_actors"
    if snapshot.actors:
        actor = snapshot.actors[0]
        if actor.release != release:
            return "hold_foreign_actor"
        if not actor.in_pane:
            return "hold_detached_actor"
        if len(snapshot.panes) != 1 or snapshot.panes[0].dead:
            return "hold_actor_pane_mismatch"
        return "healthy"
    if len(snapshot.panes) > 1:
        return "hold_multiple_panes"
    if snapshot.panes:
        return "respawn_dead_pane" if snapshot.panes[0].dead else "hold_live_pane"
    return "start_window" if snapshot.session else "start_session"


def helm_allows_ordinary(path):
    path = Path(path)
    if path.is_symlink():
        raise UnsafeObservation("controller_state_symlink")
    if not path.exists():
        return True
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            row = conn.execute("SELECT holder FROM reply_lease WHERE singleton=1").fetchone()
    except (sqlite3.Error, OSError):
        raise UnsafeObservation("controller_state_unreadable") from None
    if not row:
        raise UnsafeObservation("controller_lease_missing")
    # Only the controller may expire/fail back its own lease. An expired Sol
    # lease is still not permission for the ordinary watchdog to start a rival.
    return row[0] == "fleet"


class RestartBudget:
    def __init__(self, path):
        self.path = Path(path)

    def claim(self, now):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=2)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("CREATE TABLE IF NOT EXISTS restart_attempt(at REAL NOT NULL)")
            conn.execute("BEGIN IMMEDIATE")
            rows = [row[0] for row in conn.execute("SELECT at FROM restart_attempt WHERE at >= ?", (now - 900,))]
            if rows and (now - max(rows) < 300 or len(rows) >= 3):
                conn.rollback()
                return False
            conn.execute("DELETE FROM restart_attempt WHERE at < ?", (now - 86400,))
            conn.execute("INSERT INTO restart_attempt VALUES(?)", (now,))
            conn.commit()  # Count uncertain/failed starts too; never hot-loop.
            return True


class Runtime:
    tmux = "/opt/homebrew/bin/tmux"
    session = "leviathan"
    window = "commodore"

    def __init__(self, release, config, *, run=None):
        self.release = Path(release).resolve()
        self.config = Path(config).resolve()
        self._run = run or self._command

    @staticmethod
    def _command(args):
        env = os.environ.copy()
        env.pop("TMUX", None)
        return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False, env=env)

    def observe(self):
        result = self._run(["/bin/ps", "-axo", "pid=,ppid=,stat=,command="])
        if result.returncode:
            raise UnsafeObservation("process_probe_failed")
        rows = _process_rows(result.stdout)
        candidates = _candidates(rows)
        parents = {pid: parent for pid, parent, _status, _args in rows}
        result = self._run([self.tmux, "has-session", "-t", self.session])
        if result.returncode not in {0, 1}:
            raise UnsafeObservation("tmux_session_probe_failed")
        if result.returncode == 1 and result.stderr and not any(
            phrase in result.stderr.lower() for phrase in ("can't find session", "no server running", "no such file or directory")
        ):
            raise UnsafeObservation("tmux_session_probe_failed")
        session, panes = result.returncode == 0, []
        if session:
            result = self._run([self.tmux, "list-panes", "-t", f"{self.session}:{self.window}", "-F", "#{pane_id}\t#{pane_dead}\t#{pane_pid}"])
            if result.returncode:
                # A failed pane query is absence only after an independent,
                # successful window census proves this named window missing.
                windows = self._run([self.tmux, "list-windows", "-t", self.session, "-F", "#{window_name}"])
                if windows.returncode or self.window in windows.stdout.splitlines():
                    raise UnsafeObservation("tmux_pane_probe_failed")
            else:
                for line in result.stdout.splitlines():
                    fields = line.split("\t")
                    if len(fields) != 3 or not re.fullmatch(r"%\d+", fields[0]) or fields[1] not in {"0", "1"}:
                        raise UnsafeObservation("tmux_pane_snapshot_invalid")
                    try:
                        pid = int(fields[2])
                    except ValueError:
                        raise UnsafeObservation("tmux_pane_snapshot_invalid") from None
                    if pid <= 0:
                        raise UnsafeObservation("tmux_pane_snapshot_invalid")
                    panes.append(Pane(fields[0], fields[1] == "1", pid))
                if not panes:
                    raise UnsafeObservation("tmux_pane_snapshot_empty")
        actors = []
        for pid, _parent, script in candidates:
            cwd = self._run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
            paths = [Path(line[1:]).resolve() for line in cwd.stdout.splitlines() if line.startswith("n/")]
            if cwd.returncode or len(paths) != 1:
                raise UnsafeObservation("actor_cwd_unreadable")
            release = paths[0]
            if script.is_absolute() and script.resolve() != release / "commodore.py":
                raise UnsafeObservation("actor_source_cwd_mismatch")
            if not script.is_absolute() and script != Path("commodore.py"):
                raise UnsafeObservation("actor_source_ambiguous")
            ancestors, current = set(), pid
            while current and current not in ancestors:
                ancestors.add(current)
                current = parents.get(current, 0)
            actors.append(Actor(pid, release, any(pane.pid in ancestors and not pane.dead for pane in panes)))
        return Observation(session, tuple(panes), tuple(actors))

    def launch(self, action, snapshot):
        command = shlex.join([
            "env", f"FLEET_COMMODORE_CONFIG={self.config}",
            f"FLEET_COMMODORE_RELEASE_DIR={self.release}", str(self.release / "run.sh"),
        ])
        if action == "respawn_dead_pane":
            args = [self.tmux, "respawn-pane", "-t", snapshot.panes[0].identity, command]
        elif action == "start_session":
            args = [self.tmux, "new-session", "-d", "-s", self.session, "-n", self.window, command]
        elif action == "start_window":
            args = [self.tmux, "new-window", "-d", "-t", self.session, "-n", self.window, command]
        else:
            raise UnsafeObservation("invalid_start_action")
        if self._run(args).returncode:
            raise UnsafeObservation("start_failed")


def supervise(runtime, budget, helm_path, *, now=None):
    if not helm_allows_ordinary(helm_path):
        return "hold_controller_ownership"
    snapshot = runtime.observe()
    action = decide(snapshot, runtime.release)
    if action == "healthy" or action.startswith("hold_"):
        return action
    if runtime.observe() != snapshot or not helm_allows_ordinary(helm_path):
        return "hold_observation_changed"
    if not budget.claim(time.time() if now is None else now):
        return "hold_restart_budget"
    runtime.launch(action, snapshot)
    return action


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true", help="observe only; no locks, state writes or starts")
    args = parser.parse_args(argv)
    release = Path(os.environ.get("FLEET_COMMODORE_RELEASE_DIR", Path(__file__).parent)).resolve()
    config = Path(os.environ.get("FLEET_COMMODORE_CONFIG", release / ".env"))
    state = Path(os.environ.get("FLEET_COMMODORE_STATE_DIR", "~/.local/state/fleet-commodore")).expanduser()
    helm_path = Path(os.environ.get("HELM_CONTROLLER_DB_FILE", state / "helm-controller/controller.db")).expanduser()
    try:
        if not (release / "run.sh").is_file() or not os.access(release / "run.sh", os.X_OK) or not config.is_file() or not os.access(config, os.R_OK):
            raise UnsafeObservation("release_config_unavailable")
        runtime = Runtime(release, config)
        if os.environ.get("HELM_CONTROLLER_ENABLED") == "1":
            result = "hold_controller_enabled"
        elif args.inspect:
            result = decide(runtime.observe(), runtime.release) if helm_allows_ordinary(helm_path) else "hold_controller_ownership"
        else:
            with PollOwner(state / "watchdog.lock"):
                result = supervise(runtime, RestartBudget(state / "watchdog.db"), helm_path)
        print(json.dumps({"state": result, "mode": "inspect" if args.inspect else "supervise"}, sort_keys=True))
        return 1 if result.startswith("hold_") else 0
    except PollOwnerBusy:
        print('{"state": "watchdog_busy"}')
        return 0
    except (UnsafeObservation, OSError, sqlite3.Error, subprocess.SubprocessError, UnicodeError) as exc:
        reason = str(exc) if isinstance(exc, UnsafeObservation) else type(exc).__name__
        print(json.dumps({"state": "hold_probe_error", "reason": reason}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

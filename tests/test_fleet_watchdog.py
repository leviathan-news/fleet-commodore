"""No models, credentials, tmux server or live process is used by these fixtures."""
from pathlib import Path
import sqlite3
import subprocess

import pytest

from fleet_watchdog import (
    Actor, Observation, Pane, RestartBudget, Runtime, UnsafeObservation,
    decide, helm_allows_ordinary, parse_processes, supervise,
)


RELEASE = Path("/service/release")


def observation(*, actors=(), panes=(), session=True):
    return Observation(session, tuple(panes), tuple(actors))


def test_dead_pane_is_not_a_healthy_daemon():
    assert decide(observation(panes=[Pane("%9", True, 41)]), RELEASE) == "respawn_dead_pane"


def test_live_pane_without_actor_is_not_killed_or_overwritten():
    assert decide(observation(panes=[Pane("%9", False, 41)]), RELEASE) == "hold_live_pane"


def test_actor_in_expected_pane_and_release_is_healthy():
    assert decide(observation(actors=[Actor(41, RELEASE, True)], panes=[Pane("%9", False, 41)]), RELEASE) == "healthy"


@pytest.mark.parametrize("actors,reason", [
    ([Actor(41, Path("/foreign/release"), True)], "hold_foreign_actor"),
    ([Actor(41, RELEASE, True), Actor(42, RELEASE, True)], "hold_duplicate_actors"),
    ([Actor(42, RELEASE, False)], "hold_detached_actor"),
])
def test_other_actors_block_all_start_actions(actors, reason):
    assert decide(observation(actors=actors, panes=[Pane("%9", True, 41)]), RELEASE) == reason


def test_missing_window_and_session_are_distinct_creation_actions():
    assert decide(observation(), RELEASE) == "start_window"
    assert decide(observation(session=False), RELEASE) == "start_session"


def test_multiple_panes_fail_closed():
    assert decide(observation(panes=[Pane("%9", True, 41), Pane("%10", True, 42)]), RELEASE) == "hold_multiple_panes"


def test_process_parser_distinguishes_relative_absolute_and_zombies():
    rows = parse_processes(
        "41 1 Ss+ /runtime/bin/python3 -u commodore.py\n"
        "42 1 S /runtime/bin/python3.11 -u /service/release/commodore.py\n"
        "43 1 Z /runtime/bin/python3 -u commodore.py\n"
        "44 1 S /runtime/bin/python3 -m pytest tests/test_commodore.py\n"
    )
    assert [row[0] for row in rows] == [41, 42]
    assert rows[0][2] == Path("commodore.py")
    assert rows[1][2] == RELEASE / "commodore.py"


@pytest.mark.parametrize("text", [
    "not a process snapshot", "41 1 S /bin/sh -c 'python -u commodore.py'",
    "41 1 S python3 -u commodore.py\n41 1 S python3 -u commodore.py",
])
def test_unparseable_or_ambiguous_process_snapshot_fails_closed(text):
    with pytest.raises(UnsafeObservation):
        parse_processes(text)


def test_takeover_read_is_read_only_and_never_creates_missing_state(tmp_path):
    path = tmp_path / "controller.db"
    assert helm_allows_ordinary(path)
    assert not path.exists()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE reply_lease(singleton INTEGER, holder TEXT)")
        conn.execute("INSERT INTO reply_lease VALUES(1, 'sol')")
    before = path.read_bytes()
    assert not helm_allows_ordinary(path)  # Even expiry is not ordinary's authority to steal.
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE reply_lease SET holder='fleet'")
    assert helm_allows_ordinary(path)


def test_unreadable_controller_and_symlink_state_fail_closed(tmp_path):
    broken = tmp_path / "bad.db"
    broken.write_text("invalid sqlite fixture")
    with pytest.raises(UnsafeObservation):
        helm_allows_ordinary(broken)
    link = tmp_path / "link.db"
    link.symlink_to(broken)
    with pytest.raises(UnsafeObservation):
        helm_allows_ordinary(link)


def test_restart_budget_is_durable_bounded_and_clock_rollback_safe(tmp_path):
    path = tmp_path / "watchdog.db"
    assert RestartBudget(path).claim(1000)
    assert not RestartBudget(path).claim(1001)
    assert not RestartBudget(path).claim(900)
    assert RestartBudget(path).claim(1300)
    assert RestartBudget(path).claim(1600)
    assert not RestartBudget(path).claim(1899)
    assert RestartBudget(path).claim(1901)
    assert path.stat().st_mode & 0o777 == 0o600


class FakeRuntime:
    release = RELEASE

    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.actions = []

    def observe(self):
        return next(self.snapshots)

    def launch(self, action, snapshot):
        self.actions.append((action, snapshot))


def test_recheck_detects_actor_appearing_before_restart(tmp_path):
    empty = observation(panes=[Pane("%9", True, 41)])
    live = observation(actors=[Actor(42, RELEASE, False)], panes=empty.panes)
    runtime = FakeRuntime([empty, live])
    result = supervise(runtime, RestartBudget(tmp_path / "watchdog.db"), tmp_path / "controller.db", now=1000)
    assert result == "hold_observation_changed"
    assert runtime.actions == []


def test_supervision_restarts_only_dead_pane_and_honors_budget(tmp_path):
    dead = observation(panes=[Pane("%9", True, 41)])
    runtime = FakeRuntime([dead, dead, dead, dead])
    budget = RestartBudget(tmp_path / "watchdog.db")
    assert supervise(runtime, budget, tmp_path / "controller.db", now=1000) == "respawn_dead_pane"
    assert supervise(runtime, budget, tmp_path / "controller.db", now=1001) == "hold_restart_budget"
    assert len(runtime.actions) == 1


def test_respawn_command_never_uses_kill_flag_and_quotes_paths():
    calls = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    runtime = Runtime(Path("/service/a release"), Path("/config/a file"), run=run)
    runtime.launch("respawn_dead_pane", observation(panes=[Pane("%9", True, 41)]))
    command = calls[0]
    assert command[:4] == ["/opt/homebrew/bin/tmux", "respawn-pane", "-t", "%9"]
    assert "-k" not in command
    assert "'/service/a release/run.sh'" in command[-1]


def test_real_probe_adapter_identifies_legacy_relative_actor_by_cwd():
    def run(args):
        if args[0] == "/bin/ps":
            out = "41 1 Ss+ /runtime/bin/python3 -u commodore.py\n"
        elif args[0] == "/usr/sbin/lsof":
            out = "p41\nfcwd\nn/service/release\n"
        elif "has-session" in args:
            out = ""
        elif "list-panes" in args:
            out = "%9\t0\t41\n"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, out, "")

    runtime = Runtime(RELEASE, Path("/config/service"), run=run)
    assert decide(runtime.observe(), RELEASE) == "healthy"


def test_probe_error_is_not_absence():
    def failed(args):
        return subprocess.CompletedProcess(args, 2, "", "private command detail")

    with pytest.raises(UnsafeObservation, match="process_probe_failed"):
        Runtime(RELEASE, Path("/config/service"), run=failed).observe()

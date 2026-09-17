"""Adapter fixtures for model-free Fleet watchdog observation."""
from pathlib import Path
import subprocess

import pytest

import fleet_watchdog
from fleet_watchdog import Runtime, UnsafeObservation, decide


RELEASE = Path("/service/release")


def _runtime(*, processes, panes="%9\t0\t50\n", lsof=None,
             session=True, pane_rc=0, windows="", windows_rc=0):
    calls = []
    lsof = lsof or {50: "/service/release"}

    def run(args):
        calls.append(args)
        if args[0] == "/bin/ps":
            return subprocess.CompletedProcess(args, 0, processes, "")
        if args[0] == "/usr/sbin/lsof":
            pid = int(args[args.index("-p") + 1])
            cwd = lsof.get(pid)
            return subprocess.CompletedProcess(args, 0 if cwd else 1,
                                                f"n{cwd}\n" if cwd else "", "")
        if "has-session" in args:
            return subprocess.CompletedProcess(args, 0 if session else 1, "", "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, pane_rc, panes, "")
        if "list-windows" in args:
            return subprocess.CompletedProcess(args, windows_rc, windows, "")
        raise AssertionError(args)

    return Runtime(RELEASE, Path("/config/service"), run=run), calls


def test_absolute_script_and_cwd_agree():
    runtime, _ = _runtime(
        processes="50 1 Ss /opt/python -u /service/release/commodore.py\n"
    )
    observed = runtime.observe()
    assert observed.actors[0].release == RELEASE
    assert observed.actors[0].in_pane is True


@pytest.mark.parametrize("lsof", [{50: None}])
def test_cwd_missing_or_probe_failure_is_unsafe(lsof):
    runtime, _ = _runtime(
        processes="50 1 Ss /opt/python -u commodore.py\n", lsof=lsof
    )
    with pytest.raises(UnsafeObservation, match="actor_cwd_unreadable"):
        runtime.observe()


def test_absolute_source_mismatch_is_unsafe():
    runtime, _ = _runtime(
        processes="50 1 Ss /opt/python -u /service/other/commodore.py\n"
    )
    with pytest.raises(UnsafeObservation, match="actor_source_cwd_mismatch"):
        runtime.observe()


def test_actor_outside_pane_and_descendant_actor_ancestry():
    detached, _ = _runtime(
        processes="50 1 Ss /opt/python -u commodore.py\n", panes="%9\t0\t51\n"
    )
    assert decide(detached.observe(), RELEASE) == "hold_detached_actor"

    descendant, _ = _runtime(
        processes=("50 51 Ss /opt/python -u commodore.py\n"
                   "51 1 S /bin/sh -c runner\n"),
        panes="%9\t0\t51\n",
        lsof={50: "/service/release"},
    )
    assert descendant.observe().actors[0].in_pane is True


def test_duplicate_actors_hold():
    runtime, _ = _runtime(
        processes=("50 1 S /opt/python -u commodore.py\n"
                   "51 1 S /opt/python -u commodore.py\n"),
        panes="%9\t0\t50\n",
        lsof={50: "/service/release", 51: "/service/release"},
    )
    assert decide(runtime.observe(), RELEASE) == "hold_duplicate_actors"


def test_failed_pane_census_is_absence_only_after_window_census_proves_missing():
    runtime, calls = _runtime(
        processes="50 1 S /opt/python -u commodore.py\n",
        pane_rc=1, windows="other\n",
    )
    assert runtime.observe().panes == ()
    assert any("list-windows" in call for call in calls)

    matching, _ = _runtime(
        processes="60 1 S /bin/launchd\n", pane_rc=1, windows="commodore\n",
    )
    with pytest.raises(UnsafeObservation, match="tmux_pane_probe_failed"):
        matching.observe()

    failed, _ = _runtime(
        processes="60 1 S /bin/launchd\n", pane_rc=1, windows_rc=1,
    )
    with pytest.raises(UnsafeObservation, match="tmux_pane_probe_failed"):
        failed.observe()


@pytest.mark.parametrize("panes", ["bad\n", "%9\t2\t50\n", "%9\t0\tnope\n", ""])
def test_malformed_or_empty_pane_fields_are_unsafe(panes):
    runtime, _ = _runtime(processes="60 1 S /bin/launchd\n", panes=panes)
    with pytest.raises(UnsafeObservation, match="tmux_pane_snapshot_"):
        runtime.observe()


def test_unrelated_unmatched_quote_does_not_block_process_census():
    runtime, _ = _runtime(
        processes=("60 1 S /bin/sh -c 'unmatched user text\n"
                   "50 1 S /opt/python -u commodore.py\n")
    )
    assert len(runtime.observe().actors) == 1


def test_observe_is_probe_only_and_never_starts_or_writes():
    runtime, calls = _runtime(processes="60 1 S /bin/launchd\n")
    snapshot = runtime.observe()
    assert snapshot.actors == ()
    assert not any(any(word in call for word in ("new-session", "new-window", "respawn-pane"))
                   for call in calls)


def test_inspect_cli_only_observes(monkeypatch, tmp_path, capsys):
    release = tmp_path / "release"
    release.mkdir()
    run_sh = release / "run.sh"
    run_sh.write_text("#!/bin/sh\n")
    run_sh.chmod(0o700)
    config = tmp_path / "config"
    config.write_text("safe fixture\n")
    calls = []
    release_path = release

    class FakeRuntime:
        release = release_path

        def __init__(self, _release, _config):
            pass

        def observe(self):
            calls.append("observe")
            return type("Snapshot", (), {"session": True, "panes": (), "actors": ()})()

    monkeypatch.setenv("FLEET_COMMODORE_RELEASE_DIR", str(release))
    monkeypatch.setenv("FLEET_COMMODORE_CONFIG", str(config))
    monkeypatch.setenv("HELM_CONTROLLER_DB_FILE", str(tmp_path / "missing.db"))
    monkeypatch.setattr(fleet_watchdog, "Runtime", FakeRuntime)
    monkeypatch.setattr(fleet_watchdog, "helm_allows_ordinary", lambda _path: True)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("inspect must not acquire locks or open writable state")

    monkeypatch.setattr(fleet_watchdog, "RestartBudget", forbidden)
    monkeypatch.setattr(fleet_watchdog, "PollOwner", forbidden)
    assert fleet_watchdog.main(["--inspect"]) == 0
    assert calls == ["observe"]
    assert "start" in capsys.readouterr().out

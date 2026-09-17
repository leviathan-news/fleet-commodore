"""Dormant helm detection must remain compatible with absolute source argv."""
from pathlib import Path
import subprocess
import sys
import time

import pytest

from helm_supervisor import RuntimeErrorSafe, TmuxRuntime


@pytest.mark.parametrize("script", ["commodore.py", "/service/release/commodore.py"])
def test_helm_actor_detection_accepts_relative_and_absolute_source(script, monkeypatch):
    runtime = TmuxRuntime(session="fixture", window="fixture", config=Path("/config"), fleet_release=Path("/service/release"), successor_release=Path("/successor"), db_path=Path("/state/controller.db"))

    def run(args, **_kwargs):
        out = f"41 1 S /runtime/python3 -u {script}\n" if args[0] == "/bin/ps" else "p41\nfcwd\nn/service/release\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(runtime, "_run", run)
    assert runtime.actor_release() == Path("/service/release")


def test_helm_process_probe_failure_does_not_mean_actor_absent(monkeypatch):
    runtime = TmuxRuntime(session="fixture", window="fixture", config=Path("/config"), fleet_release=Path("/service/release"), successor_release=Path("/successor"), db_path=Path("/state/controller.db"))
    monkeypatch.setattr(runtime, "_run", lambda args, **_kwargs: subprocess.CompletedProcess(args, 2, "", "private fixture detail"))
    with pytest.raises(RuntimeErrorSafe, match="Fleet actor observation is unavailable"):
        runtime.actor_release()


def test_helm_actor_observation_uses_real_ps_and_lsof_for_a_pinned_child(tmp_path):
    """Exercise the host adapters without a tmux server or a live Fleet pane."""
    release = tmp_path / "pinned-release"
    release.mkdir()
    script = release / "commodore.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=release,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            # Give ps/lsof one scheduling turn; no Fleet code runs in the child.
            time.sleep(0.02)
            break

        def run(args, **_kwargs):
            if args[0] == "/opt/homebrew/bin/tmux":
                if "has-session" in args:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if "list-panes" in args:
                    return subprocess.CompletedProcess(args, 0, f"%9\\t0\\t{child.pid}\\n", "")
                raise AssertionError(args)
            return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)

        runtime = TmuxRuntime(
            session="fixture",
            window="fixture",
            config=tmp_path / "config",
            fleet_release=release,
            successor_release=tmp_path / "successor",
            db_path=tmp_path / "controller.db",
        )
        runtime._run = run
        assert runtime.actor_release() == release
    finally:
        child.terminate()
        child.wait(timeout=5)

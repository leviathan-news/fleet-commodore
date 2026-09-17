"""Dormant helm detection must remain compatible with absolute source argv."""
from pathlib import Path
import subprocess

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

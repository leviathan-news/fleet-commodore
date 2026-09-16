import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "bin/provider-probe.py"


def run_probe(tmp_path, response, failure=None):
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    shutil.copy2(PROBE, root / "bin/provider-probe.py")
    (root / "codex_provider.py").write_text(
        "def generate_via_codex(*args, failure_context=None, **kwargs):\n"
        f"    {response}\n"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    env = {"PATH": os.environ["PATH"], "PROBE_FAILURE": failure or ""}
    return subprocess.run(
        [sys.executable, str(root / "bin/provider-probe.py")],
        cwd=outside, env=env, text=True, capture_output=True,
    )


def test_probe_imports_from_repo_parent_outside_checkout(tmp_path):
    result = run_probe(tmp_path, "return 'FLEET_PROVIDER_PROBE_OK'")
    assert result.returncode == 0
    assert result.stdout.strip() == '{"provider": "codex", "state": "ok"}'
    assert result.stderr == ""


def test_probe_timeout_is_sanitized_and_nonzero(tmp_path):
    result = run_probe(
        tmp_path,
        "failure_context['failure_class'] = 'provider_timeout'; return None",
    )
    assert result.returncode == 1
    assert result.stdout.strip() == '{"provider": "codex", "state": "timeout"}'
    assert "failure_class" not in result.stdout
    assert result.stderr == ""

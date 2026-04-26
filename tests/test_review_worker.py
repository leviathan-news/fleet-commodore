"""review_worker.py contract tests.

The worker is a container entrypoint. We test its contract (stdin → stdout
JSON, atomic scratch write, parse_review behavior) without spawning a real
container or invoking gh/claude.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
WORKER = REPO / "review_worker.py"


def _run_worker(job: dict, env_extras: dict = None, timeout: int = 30):
    env = {**os.environ}
    if env_extras:
        env.update(env_extras)
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        input=json.dumps(job),
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return proc


def test_parse_review_extracts_verdict_and_findings():
    """Import the module directly and feed it a synthetic Claude reply."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("review_worker", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sample = """VERDICT: request_changes

- foo handler is missing a status guard
- bar query is N+1
- baz API change is backwards-incompatible
"""
    verdict, findings = mod.parse_review(sample)
    assert verdict == "request_changes"
    assert len(findings) == 3
    assert "status guard" in findings[0]


def test_parse_review_caps_findings_at_eight():
    import importlib.util
    spec = importlib.util.spec_from_file_location("review_worker", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sample = "VERDICT: comment\n" + "\n".join(f"- finding {i}" for i in range(20))
    _, findings = mod.parse_review(sample)
    assert len(findings) == 8


def test_parse_review_empty_returns_empty():
    import importlib.util
    spec = importlib.util.spec_from_file_location("review_worker", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    v, f = mod.parse_review("")
    assert v == ""
    assert f == []


def test_atomic_write_protocol(tmp_path):
    """write_result_atomically must produce the final file with no `.tmp`
    orphan."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("review_worker", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.RESULTS_DIR = tmp_path
    mod.write_result_atomically("abc-1234", {"verdict": "approve"})
    final = tmp_path / "abc-1234.result.json"
    tmp_orphans = list(tmp_path.glob("*.tmp"))
    assert final.exists()
    assert json.loads(final.read_text())["verdict"] == "approve"
    assert tmp_orphans == []


def test_no_diff_path_records_failure_to_scratch(tmp_path):
    """When `gh pr diff` is unavailable, worker must write a failure envelope
    to scratch BEFORE emitting stdout — so the coordinator sees the structured
    failure even if stdout JSON is malformed."""
    proc = _run_worker(
        {"review_uuid": "abcdefg1", "repo": "x/y", "pr_number": 1},
        env_extras={
            "GH_BIN": "/nonexistent/gh",
            "CLAUDE_BIN": "/nonexistent/claude",
            "RESULTS_DIR": str(tmp_path),
        },
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["status"] == "failed"
    assert payload["error"] == "diff_unavailable"
    # Scratch file written
    scratch = tmp_path / "abcdefg1.result.json"
    assert scratch.exists()
    persisted = json.loads(scratch.read_text())
    assert persisted["status"] == "failed"


def test_missing_required_field_exits_2():
    proc = _run_worker({"review_uuid": "abc"})  # missing repo, pr_number
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip())
    assert payload["error"] == "missing_job_fields"


def test_empty_stdin_exits_2():
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        input="", capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip())
    assert payload["error"] == "no_job"


def test_version_banner_exits_with_component_status():
    """`--version` is the image smoke-test path. Exit code reports
    all_components_present."""
    proc = subprocess.run(
        [sys.executable, str(WORKER), "--version"],
        capture_output=True, text=True, timeout=15,
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["mode"] == "version"
    assert "components" in payload
    assert "python_deps" in payload
    # rc = 0 if all present, 1 otherwise — both are valid outcomes
    assert proc.returncode in (0, 1)

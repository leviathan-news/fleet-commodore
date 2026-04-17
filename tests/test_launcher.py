"""Launcher + image-boundary tests.

Exercise bin/launch-review-container's error paths + the review_worker
stub's --version introspection. Docker-mediated tests (image build, real
container) are integration tests; they live in a separate file + are
gated on a DOCKER_AVAILABLE env flag.
"""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "bin" / "launch-review-container"
WORKER = REPO / "review_worker.py"
DB_WRAPPER = REPO / "bin" / "commodore-db"
ORM_WRAPPER = REPO / "bin" / "commodore-orm"


# --- Boundary: launcher must not import from commodore ---------------------


def test_launcher_does_not_import_commodore():
    """Architectural invariant: the launcher runs as a separate process
    specifically to escape commodore.py's import graph. If someone adds
    `from commodore import X` here, the process-boundary security model
    collapses to 'same process address space.'"""
    tree = ast.parse(LAUNCHER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "commodore", (
                f"Launcher imports from commodore at line {node.lineno}. "
                "Must stay boundary-pure."
            )
        if isinstance(node, ast.Import):
            for n in node.names:
                assert n.name != "commodore", (
                    f"Launcher imports commodore at line {node.lineno}. "
                    "Must stay boundary-pure."
                )


def test_review_worker_does_not_import_commodore():
    tree = ast.parse(WORKER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "commodore"
        if isinstance(node, ast.Import):
            for n in node.names:
                assert n.name != "commodore"


# --- Launcher error paths --------------------------------------------------


def _run_launcher(argv, env=None, stdin_text=""):
    """Invoke the launcher with a clean env. Return (stdout, stderr, rc)."""
    base_env = os.environ.copy()
    if env:
        base_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(LAUNCHER)] + list(argv),
        input=stdin_text,
        capture_output=True,
        text=True,
        env=base_env,
        timeout=15,
    )
    return proc.stdout, proc.stderr, proc.returncode


def test_launcher_missing_uuid_returns_json_exit_2():
    out, err, rc = _run_launcher([])
    assert rc == 2, f"expected exit 2, got {rc}. stderr={err[:200]}"
    payload = json.loads(out.strip())
    assert payload["error"] == "missing_uuid"


def test_launcher_invalid_uuid_returns_json_exit_2():
    out, _, rc = _run_launcher(["!!!bogus!!!"])
    assert rc == 2
    payload = json.loads(out.strip())
    assert payload["error"] == "invalid_uuid"


@pytest.mark.parametrize("bad", [
    "",                      # empty
    "ABC12345",              # uppercase (regex is lowercase only)
    "a" * 4,                 # too short
    "a" * 41,                # too long
    "abc$def_ghi",           # punctuation
    "../etc/passwd",         # path traversal
])
def test_launcher_uuid_patterns_rejected(bad):
    out, _, rc = _run_launcher([bad])
    assert rc == 2, f"expected reject for {bad!r}, got {rc}"
    payload = json.loads(out.strip())
    assert payload["error"] in ("invalid_uuid", "missing_uuid")


def test_launcher_missing_gh_pat_returns_json_exit_3(tmp_path):
    out, _, rc = _run_launcher(
        ["abcd1234"],
        env={
            "COMMODORE_GH_PAT_FILE": str(tmp_path / "nonexistent"),
            "COMMODORE_DB_URL_FILE": str(tmp_path / "alsomissing"),
        },
    )
    assert rc == 3
    payload = json.loads(out.strip())
    assert payload["error"] == "missing_credentials"


def test_launcher_missing_claude_auth_returns_json_exit_3(tmp_path):
    # Create dummy GH PAT + DB URL so we pass the earlier gate and test
    # the claude-auth check specifically.
    pat = tmp_path / "gh_pat"
    pat.write_text("github_pat_fake")
    db = tmp_path / "db_url"
    db.write_text("postgres://fake")
    out, _, rc = _run_launcher(
        ["abcd1234"],
        env={
            "COMMODORE_GH_PAT_FILE": str(pat),
            "COMMODORE_DB_URL_FILE": str(db),
            "COMMODORE_CLAUDE_AUTH_DIR": str(tmp_path / "no-claude-dir"),
            "COMMODORE_CLAUDE_CONFIG_FILE": str(tmp_path / "no-claude-json"),
        },
    )
    assert rc == 3
    payload = json.loads(out.strip())
    assert payload["error"] == "missing_claude_auth"


# --- Launcher output-parse helper ------------------------------------------


def _load_launcher_module():
    """Load bin/launch-review-container as a Python module for direct calls.

    The file has no .py extension (it's in bin/ as a CLI), so we need to
    specify an explicit SourceFileLoader rather than relying on
    spec_from_file_location's extension-based inference.
    """
    import importlib.util
    import importlib.machinery
    loader = importlib.machinery.SourceFileLoader("launcher_mod", str(LAUNCHER))
    spec = importlib.util.spec_from_loader("launcher_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_extract_last_json_picks_final_object():
    """The launcher scans container stdout for the LAST balanced {...}
    and parses that. Intermediate log lines must not confuse the parser.
    """
    mod = _load_launcher_module()
    noisy = (
        'LOG: starting review\n'
        '{"verdict": "APPROVE", "findings": []}\n'
        'tail noise\n'
    )
    result = mod.extract_last_json(noisy)
    assert result == {"verdict": "APPROVE", "findings": []}


def test_extract_last_json_picks_last_of_multiple():
    """Multiple balanced objects — last one wins."""
    mod = _load_launcher_module()
    text = (
        '{"status": "intermediate"}\n'
        'more log lines\n'
        '{"verdict": "REQUEST_CHANGES", "findings": [{"severity": "HIGH"}]}\n'
    )
    result = mod.extract_last_json(text)
    assert result == {"verdict": "REQUEST_CHANGES", "findings": [{"severity": "HIGH"}]}


def test_extract_last_json_empty_returns_none():
    mod = _load_launcher_module()
    assert mod.extract_last_json("") is None
    assert mod.extract_last_json("no json here at all") is None


# --- review_worker.py --version contract ----------------------------------


def test_review_worker_version_returns_json():
    proc = subprocess.run(
        [sys.executable, str(WORKER), "--version"],
        capture_output=True, text=True, timeout=15,
    )
    # Exit 0 if all components present, 1 if any missing. Both are fine —
    # the contract is that stdout is a single JSON object either way.
    assert proc.returncode in (0, 1)
    payload = json.loads(proc.stdout.strip())
    assert payload["mode"] == "version"
    assert "components" in payload
    assert "python_deps" in payload


def test_review_worker_stub_acknowledges_job_shape():
    """v1 stub returns status=not_implemented rather than faking findings."""
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        input=json.dumps({"review_uuid": "abc12345", "repo": "leviathan-news/squid-bot", "pr_number": 1}),
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["status"] == "not_implemented"
    assert payload["review_uuid"] == "abc12345"


def test_review_worker_empty_stdin_rejects():
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        input="",
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip())
    assert payload["error"] == "no_job"


def test_review_worker_missing_required_field():
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        input=json.dumps({"review_uuid": "abc12345"}),  # missing repo + pr_number
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip())
    assert payload["error"] == "missing_job_fields"
    assert set(payload["missing"]) == {"pr_number", "repo"}

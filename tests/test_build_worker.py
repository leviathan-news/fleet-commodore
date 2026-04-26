"""build_worker.py contract tests.

Same shape as test_qa_worker.py / test_review_worker.py: contract tests
(stdin → stdout JSON envelope, atomic scratch write, missing-fields errors,
version banner) without spawning a real container or making real GitHub
calls. Pipeline stages (fork/clone/rebase/edit/commit/push/pr) are tested
with subprocess mocks where helpful.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


REPO = Path(__file__).resolve().parent.parent
WORKER = REPO / "build_worker.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_worker", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


# --- Contract: stdin/stdout envelope ---------------------------------------

def test_empty_stdin_exits_2():
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        input="", capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip())
    assert payload["error"] == "no_job"


def test_bad_json_exits_2():
    proc = _run_worker({})  # will become {} but missing required fields
    payload = json.loads(proc.stdout.strip())
    assert proc.returncode == 2
    assert payload["error"] == "missing_job_fields"
    # All five required fields should be flagged
    assert set(payload["missing"]) == {
        "draft_uuid", "target_repo", "target_branch", "title", "commit_message"
    }


def test_partial_job_lists_missing_fields():
    proc = _run_worker({
        "draft_uuid": "abc",
        "target_repo": "x/y",
        "target_branch": "x",
        # missing title, commit_message
    })
    payload = json.loads(proc.stdout.strip())
    assert proc.returncode == 2
    assert set(payload["missing"]) == {"title", "commit_message"}


# --- Atomic scratch-file write --------------------------------------------

def test_scrub_secrets_redacts_token_in_url():
    """Regression: 2026-04-26 a real gh_pat leaked to the build stderr log
    via `git clone https://x-access-token:<pat>@github.com/...`. The scrub
    helper MUST redact this exact shape before any persistence.

    NB: fixture below uses a deliberately invalid token shape to dodge
    GitHub's push-protection secret scanner (`zzz` prefix is not a real
    PAT pattern but does match our `[A-Za-z0-9_]{20,}` capture group)."""
    mod = _load_module()
    fake_token = "zzz" + "A" * 60  # 63-char token-shaped string, not a real PAT
    leaky = (
        f"fatal: unable to access 'https://x-access-token:"
        f"{fake_token}@github.com/leviathan-agent/fleet-commodore.git/'"
    )
    scrubbed = mod._scrub_secrets(leaky)
    assert fake_token not in scrubbed
    assert "x-access-token:<REDACTED>@" in scrubbed


def test_scrub_secrets_redacts_bare_pat_prefixes():
    """All five GitHub token prefixes must be redacted if they appear
    bare. Fixtures use repeated A's to avoid tripping GitHub's
    push-protection secret scanner — the regex doesn't care about
    realism, just shape."""
    mod = _load_module()
    fake_body = "A" * 24
    for prefix in ("github_pat_", "ghp_", "gho_", "ghs_", "ghu_"):
        token = f"{prefix}{fake_body}"
        scrubbed = mod._scrub_secrets(f"oops: {token} leaked")
        assert token not in scrubbed
        assert prefix in scrubbed  # the prefix is preserved
        assert "<REDACTED>" in scrubbed


def test_scrub_secrets_passthrough_for_non_secrets():
    """Don't false-positive on innocent text that happens to contain
    'token' or 'github'."""
    mod = _load_module()
    safe_inputs = [
        "Cloning into 'foo'...",
        "rc=128: fatal access denied",
        "github.com is reachable",
        "the token endpoint returned 401",
    ]
    for s in safe_inputs:
        assert mod._scrub_secrets(s) == s


def test_atomic_write_protocol(tmp_path):
    mod = _load_module()
    mod.RESULTS_DIR = tmp_path
    mod.write_result_atomically(
        "abc-1234", {"pr_url": "https://example.com/pr/1", "branch": "x"},
    )
    final = tmp_path / "abc-1234.result.json"
    assert final.exists()
    assert json.loads(final.read_text())["pr_url"].endswith("/pr/1")
    # No .tmp orphan
    assert list(tmp_path.glob("*.tmp")) == []


# --- Stage failures recorded in scratch file -------------------------------

def test_no_gh_no_git_records_failure(tmp_path):
    """With unavailable git/gh binaries, the worker must exit 0 with a
    failed envelope + write the failure to scratch (so coordinator
    recovery doesn't relaunch). Stage tells the operator where it died."""
    proc = _run_worker(
        {
            "draft_uuid": "build-1",
            "target_repo": "leviathan-news/squid-bot",
            "target_branch": "commodore/test-20260426",
            "title": "Test PR",
            "commit_message": "test commit",
        },
        env_extras={
            "GH_BIN": "/nonexistent/gh",
            "GIT_BIN": "/nonexistent/git",
            "CLAUDE_BIN": "/nonexistent/claude",
            "RESULTS_DIR": str(tmp_path),
            "GH_TOKEN": "test-token",  # so we don't fail at gh_token() check
        },
        timeout=60,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["status"] == "failed"
    # Should have died at fork or clone (first stage that uses gh/git)
    assert payload["stage"] in ("fork", "clone")
    # Scratch file written so recovery sees the failure
    scratch = tmp_path / "build-1.result.json"
    assert scratch.exists()


def test_missing_gh_token_records_failure(tmp_path):
    """No GH_TOKEN → can't construct token-in-URL. Worker must record this
    cleanly (a launcher misconfiguration) rather than crash."""
    proc = _run_worker(
        {
            "draft_uuid": "build-2",
            "target_repo": "leviathan-news/squid-bot",
            "target_branch": "commodore/test-20260426",
            "title": "Test PR",
            "commit_message": "test commit",
        },
        env_extras={
            "GH_BIN": "/nonexistent/gh",
            "GIT_BIN": "/nonexistent/git",
            "RESULTS_DIR": str(tmp_path),
            "GH_TOKEN": "",  # empty
        },
        timeout=60,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["status"] == "failed"


# --- Edit application -----------------------------------------------------

def test_apply_structured_edits_write(tmp_path):
    mod = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    edits = [
        {"action": "write", "path": "README.md", "content": "# Hello\n"},
        {"action": "write", "path": "src/foo.py", "content": "x = 1\n"},
    ]
    mod.apply_structured_edits(repo, edits)
    assert (repo / "README.md").read_text() == "# Hello\n"
    assert (repo / "src/foo.py").read_text() == "x = 1\n"


def test_apply_structured_edits_delete(tmp_path):
    mod = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "old.txt").write_text("delete me")
    mod.apply_structured_edits(repo, [
        {"action": "delete", "path": "old.txt"},
    ])
    assert not (repo / "old.txt").exists()


def test_apply_structured_edits_rejects_path_traversal(tmp_path):
    """Edit paths starting with .. or absolute must be rejected — never
    let Claude write outside the repo working tree."""
    mod = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="invalid path"):
        mod.apply_structured_edits(repo, [
            {"action": "write", "path": "../escape.txt", "content": "x"},
        ])


def test_apply_structured_edits_unknown_action(tmp_path):
    mod = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="unknown action"):
        mod.apply_structured_edits(repo, [
            {"action": "execute", "path": "x", "content": "rm -rf /"},
        ])


# --- Edit generation via Claude (mocked) ----------------------------------

def test_generate_edits_via_claude_parses_last_json(tmp_path):
    mod = _load_module()
    fake_output = """Looking at the repo...

I'll write a one-line readme update.

```
some text
```

{"edits": [
    {"action": "write", "path": "README.md", "content": "Updated"}
], "summary": "trivial readme tweak"}
"""
    with mock.patch.object(mod.subprocess, "run") as m_run:
        m_run.return_value = mock.Mock(returncode=0, stdout=fake_output, stderr="")
        edits, summary = mod.generate_edits_via_claude(
            tmp_path, "x/y", "br", "title", "plan body",
        )
    assert len(edits) == 1
    assert edits[0]["path"] == "README.md"
    assert "trivial readme" in summary


def test_generate_edits_via_claude_no_edits_on_unparseable(tmp_path):
    mod = _load_module()
    with mock.patch.object(mod.subprocess, "run") as m_run:
        m_run.return_value = mock.Mock(returncode=0, stdout="just prose, no JSON", stderr="")
        edits, summary = mod.generate_edits_via_claude(
            tmp_path, "x/y", "br", "title", "plan body",
        )
    assert edits == []
    assert summary  # populated with reason


def test_generate_edits_via_claude_no_edits_on_claude_error(tmp_path):
    mod = _load_module()
    with mock.patch.object(mod.subprocess, "run") as m_run:
        m_run.return_value = mock.Mock(returncode=1, stdout="", stderr="auth failed")
        edits, summary = mod.generate_edits_via_claude(
            tmp_path, "x/y", "br", "title", "plan body",
        )
    assert edits == []
    assert "non-zero" in summary.lower() or "exited" in summary.lower()


# --- Last-JSON extractor ---------------------------------------------------

def test_extract_last_json_handles_prose_around_json():
    mod = _load_module()
    text = """First I'll explain.

The plan is to add foo.

{"draft": "incomplete"}

Actually, on reflection:

{"edits": [], "summary": "skip"}"""
    result = mod._extract_last_json(text)
    assert result == {"edits": [], "summary": "skip"}


def test_extract_last_json_returns_none_for_unparseable():
    mod = _load_module()
    assert mod._extract_last_json("no braces here") is None
    assert mod._extract_last_json("{not json}") is None


# --- Version banner -------------------------------------------------------

def test_version_banner():
    proc = subprocess.run(
        [sys.executable, str(WORKER), "--version"],
        capture_output=True, text=True, timeout=15,
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["mode"] == "version"
    assert payload["worker"] == "build"
    assert "components" in payload
    assert "fork_owner" in payload
    assert "commit_identity" in payload
    assert proc.returncode in (0, 1)

#!/usr/bin/env python3
"""Fleet Commodore build (fork-and-PR) worker container entrypoint.

Pipeline:
  1. Read job JSON from stdin: {draft_uuid, target_repo, target_branch,
     title, pr_body, commit_message, edits?, plan_body_md?}
  2. Ensure leviathan-agent fork of <target_repo> exists (idempotent).
  3. Clone fork via token-in-URL into /tmp/build/<uuid>/repo.
  4. git fetch upstream && git rebase upstream/main (so our branch is current).
  5. git checkout -b <target_branch>.
  6. Apply edits. Two paths:
     a. If `edits` is a non-empty list of {action, path, content|diff},
        apply those literally (write/patch).
     b. Otherwise (the typical v6 path — operator just refined a plan in
        chat), invoke Claude inside the worker with the plan body +
        repo checkout, ask it to produce the structured edit list, then
        apply.
  7. git add -A && git commit -m <commit_message>.
  8. git push -u origin <target_branch>.
  9. gh pr create --repo <target_repo> --head leviathan-agent:<branch>
     --title <title> --body <pr_body>.
 10. Atomic scratch write {pr_url, commit_sha, branch} BEFORE stdout JSON.
 11. Emit ONE JSON object on stdout.

The atomic scratch write is the durability boundary. If we crash after
the file appears at its final name, the host coordinator's recovery
treats the side effect (the PR) as committed and the GitHub pre-flight
oracle (`gh pr list`) confirms it.

Exit codes: 0 ok, 2 bad job, 5 internal error. Errors during pipeline
stages are reported in-band (status=failed, stage=clone|edit|push|pr|...)
with rc=0 so the launcher's last-JSON parser stays happy.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


# --- Configuration ---------------------------------------------------------

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "/usr/bin/claude"
GH_BIN = os.environ.get("GH_BIN") or shutil.which("gh") or "/usr/bin/gh"
GIT_BIN = os.environ.get("GIT_BIN") or shutil.which("git") or "/usr/bin/git"

# Container path for the result-scratch handoff. Bound by the launcher.
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "/var/run/commodore-results"))

# Fork owner — the GitHub account whose token is in GH_TOKEN.
FORK_OWNER = os.environ.get("BUILD_FORK_OWNER", "leviathan-agent")

# Commit identity — DeepSeaSquid until leviathan-commodore is unflagged.
COMMIT_NAME = os.environ.get("BUILD_COMMIT_NAME", "DeepSeaSquid")
COMMIT_EMAIL = os.environ.get("BUILD_COMMIT_EMAIL", "deepseasquid@nicepick.dev")

# Working directory inside the container's tmpfs.
WORK_ROOT = Path(os.environ.get("BUILD_WORK_ROOT", "/tmp/build"))

# Soft budgets. The launcher's wall timeout (10 min default) is the hard cap.
CLAUDE_EDIT_TIMEOUT_S = int(os.environ.get("BUILD_CLAUDE_TIMEOUT_S", "300"))
GIT_TIMEOUT_S = int(os.environ.get("BUILD_GIT_TIMEOUT_S", "120"))


# --- Single-exit emitter ---------------------------------------------------

def emit(obj: dict, exit_code: int = 0) -> "None":
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    sys.exit(exit_code)


# --- Atomic scratch-file write (same protocol as review/qa workers) -------

def write_result_atomically(uuid: str, payload: dict) -> None:
    final_path = RESULTS_DIR / f"{uuid}.result.json"
    temp_path = RESULTS_DIR / f"{uuid}.result.json.tmp"
    with open(temp_path, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.rename(temp_path, final_path)
    dir_fd = os.open(str(RESULTS_DIR), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# --- Subprocess helpers ----------------------------------------------------

_TOKEN_LEAK_RE = re.compile(
    # x-access-token:<pat>@github.com style URLs
    r"x-access-token:[^@\s]+@",
    re.IGNORECASE,
)
_GH_PAT_RE = re.compile(
    # github_pat_<long string> or ghp_<long string> appearing anywhere
    r"\b(github_pat_|ghp_|gho_|ghs_|ghu_)[A-Za-z0-9_]{20,}",
)


def _scrub_secrets(text: str) -> str:
    """Mask any token shapes that could leak via stderr → log file.
    The gh PAT shows up two ways: as `x-access-token:<pat>@github.com`
    inside clone/push URLs, and as a bare `github_pat_<...>` if anything
    ever echoes the env var. Both must be redacted before write."""
    if not text:
        return text
    text = _TOKEN_LEAK_RE.sub("x-access-token:<REDACTED>@", text)
    text = _GH_PAT_RE.sub(r"\1<REDACTED>", text)
    return text


def _safe_cmd_for_log(cmd):
    """Stringify argv with token redaction."""
    return _scrub_secrets(" ".join(str(a) for a in cmd))


def run(cmd, *, cwd=None, env=None, timeout=GIT_TIMEOUT_S, check=True,
        log_label=None):
    """Run a subprocess with sensible defaults. Returns CompletedProcess.
    On check=True and non-zero exit, prints REDACTED stderr to our stderr
    (which goes to the launcher's per-build log) and re-raises
    CalledProcessError. NEVER write the cmd or stderr unredacted —
    git clone URLs include the PAT inline."""
    if log_label:
        sys.stderr.write(f"[{log_label}] {_safe_cmd_for_log(cmd)}\n")
    proc = subprocess.run(
        cmd, cwd=cwd, env=env,
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"[{log_label or cmd[0]}] rc={proc.returncode}\n"
            f"  stdout: {_scrub_secrets((proc.stdout or '')[:400])}\n"
            f"  stderr: {_scrub_secrets((proc.stderr or '')[:400])}\n"
        )
        if check:
            # Also scrub the exception payload — CalledProcessError repr
            # may surface in upstream error envelopes.
            scrubbed_stdout = _scrub_secrets(proc.stdout or "")
            scrubbed_stderr = _scrub_secrets(proc.stderr or "")
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, scrubbed_stdout, scrubbed_stderr,
            )
    return proc


def gh_token() -> str:
    """The launcher injects GH_TOKEN via the env-file. Required."""
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GH_TOKEN missing — launcher must inject it")
    return token


def make_git_env() -> dict:
    """Subprocess env for git: token-free, no interactive prompts."""
    e = dict(os.environ)
    e["GIT_TERMINAL_PROMPT"] = "0"
    e["GIT_AUTHOR_NAME"] = COMMIT_NAME
    e["GIT_AUTHOR_EMAIL"] = COMMIT_EMAIL
    e["GIT_COMMITTER_NAME"] = COMMIT_NAME
    e["GIT_COMMITTER_EMAIL"] = COMMIT_EMAIL
    return e


# --- Stage 1: ensure fork --------------------------------------------------

def ensure_fork(target_repo: str) -> None:
    """Verify the fork exists at FORK_OWNER/<repo>. If missing, attempt to
    create it via API; on 403 (the PAT lacks administration=write) print
    a clear in-character error and let the clone stage fail naturally —
    the operator must pre-create the fork via the web UI ONCE per repo.

    Why not REQUIRE the API fork: GitHub's fine-grained PAT model requires
    administration=write to create forks, which most org policies clamp.
    Web-UI fork has no such restriction. Pre-creating once is a 10-second
    button click; failing builds with a clear message is better than
    blocking ye on the GitHub permissions maze.
    """
    repo_name = target_repo.split("/")[-1]
    # Cheap GET — does the fork already exist?
    check = subprocess.run(
        [GH_BIN, "api", f"repos/{FORK_OWNER}/{repo_name}", "--silent"],
        capture_output=True, text=True, timeout=30,
    )
    if check.returncode == 0:
        sys.stderr.write(f"[fork] {FORK_OWNER}/{repo_name} already exists; skipping API fork.\n")
        return

    # Fork missing — try to create it via API. May 403 on PAT-clamped orgs.
    sys.stderr.write(f"[fork] {FORK_OWNER}/{repo_name} not found; attempting API fork.\n")
    fork_proc = subprocess.run(
        [GH_BIN, "repo", "fork", target_repo, "--clone=false"],
        capture_output=True, text=True, timeout=60,
    )
    if fork_proc.returncode == 0:
        sys.stderr.write(f"[fork] API fork succeeded for {FORK_OWNER}/{repo_name}.\n")
        return

    # Pre-conditioned 403 — surface a clear actionable error.
    err_msg = (
        f"fork missing and API create failed (rc={fork_proc.returncode}). "
        f"Operator action: visit https://github.com/{target_repo} logged in "
        f"as {FORK_OWNER} and click `Fork`. ONE-TIME setup per repo."
    )
    sys.stderr.write(f"[fork] {_scrub_secrets(err_msg)}\n")
    raise RuntimeError(err_msg)


# --- Stage 2: clone fork ---------------------------------------------------

def clone_fork(target_repo: str, work_dir: Path) -> Path:
    """Clone leviathan-agent's fork via token-in-URL. Returns repo path."""
    repo_name = target_repo.split("/")[-1]
    fork_url = f"https://x-access-token:{gh_token()}@github.com/{FORK_OWNER}/{repo_name}.git"
    repo_path = work_dir / "repo"
    run(
        [GIT_BIN, "clone", "--depth=50", fork_url, str(repo_path)],
        env=make_git_env(),
        log_label="clone",
    )
    return repo_path


# --- Stage 3: rebase against upstream --------------------------------------

def rebase_against_upstream(repo_path: Path, target_repo: str) -> None:
    """Add upstream remote, fetch, rebase fork's main on top of upstream/main.
    Keeps our branch from diverging from the source repo."""
    upstream_url = (
        f"https://x-access-token:{gh_token()}@github.com/{target_repo}.git"
    )
    env = make_git_env()
    run([GIT_BIN, "remote", "add", "upstream", upstream_url],
        cwd=repo_path, env=env, log_label="remote-add", check=False)
    run([GIT_BIN, "fetch", "upstream", "main"],
        cwd=repo_path, env=env, log_label="fetch-upstream")
    # Switch to main (clone's default may be a different ref) then rebase
    run([GIT_BIN, "checkout", "main"],
        cwd=repo_path, env=env, log_label="checkout-main")
    run([GIT_BIN, "reset", "--hard", "upstream/main"],
        cwd=repo_path, env=env, log_label="reset-to-upstream")


# --- Stage 4: branch + apply edits -----------------------------------------

def checkout_new_branch(repo_path: Path, branch: str) -> None:
    run([GIT_BIN, "checkout", "-b", branch],
        cwd=repo_path, env=make_git_env(), log_label="checkout-branch")


def apply_structured_edits(repo_path: Path, edits: list) -> None:
    """Apply a literal edit list. Each edit is:
       {"action": "write", "path": "...", "content": "..."}
       {"action": "patch", "path": "...", "diff": "..."}  # unified diff body
       {"action": "delete", "path": "..."}
    """
    for i, edit in enumerate(edits):
        action = edit.get("action")
        rel_path = edit.get("path", "").lstrip("/")
        if not rel_path or ".." in rel_path.split("/"):
            raise ValueError(f"edit #{i}: invalid path {rel_path!r}")
        full = repo_path / rel_path
        if action == "write":
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(edit.get("content", ""))
        elif action == "delete":
            try:
                full.unlink()
            except FileNotFoundError:
                pass
        elif action == "patch":
            diff_body = edit.get("diff", "")
            if not diff_body.strip():
                continue
            # Use git apply so unified diffs land cleanly.
            proc = subprocess.run(
                [GIT_BIN, "apply", "-"],
                cwd=repo_path, input=diff_body,
                capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"edit #{i}: git apply failed rc={proc.returncode}: "
                    f"{proc.stderr[:300]}"
                )
        else:
            raise ValueError(f"edit #{i}: unknown action {action!r}")


# --- Stage 4b: generate edits from plan body via Claude --------------------

EDIT_GENERATION_PROMPT = """You are the Fleet Commodore's build worker. You have just
been given a feature/fix plan that was refined by an operator in chat. Your job is
to produce the actual file edits that implement the plan.

Working directory: {repo_path}
Target repository: {target_repo}
Target branch: {target_branch}
Plan title: {title}

Plan body (the operator's refined description):
---
{plan_body}
---

Read the working directory to understand the codebase. When ready, produce the
edits. OUTPUT FORMAT (strict — the parser depends on it):

The LAST balanced JSON object in your output MUST be a single object of shape:

  {{"edits": [
      {{"action": "write", "path": "relative/path", "content": "full new file body"}},
      {{"action": "patch", "path": "relative/path", "diff": "unified diff body"}},
      {{"action": "delete", "path": "relative/path"}}
  ], "summary": "one-sentence description of what this PR changes"}}

Constraints:
- Only edit files inside {repo_path}. No paths starting with `..`.
- Keep changes minimal and scoped to the plan. Do not refactor unrelated code.
- If the plan is too vague to act on, output {{"edits": [], "summary": "..."}}
  with a one-sentence reason in summary.
- Use `write` for new files or full replacements (small files).
- Use `patch` (unified diff) for surgical changes to existing files.
- Test commands you'd want to run can go in summary; the operator runs CI.

Take your time. Read enough of the codebase to make the change correctly.
"""


def generate_edits_via_claude(repo_path: Path, target_repo: str,
                              target_branch: str, title: str,
                              plan_body: str) -> tuple:
    """Returns (edits_list, summary). Empty list on failure."""
    prompt = EDIT_GENERATION_PROMPT.format(
        repo_path=str(repo_path),
        target_repo=target_repo,
        target_branch=target_branch,
        title=title[:200],
        plan_body=plan_body[:8000],
    )
    try:
        proc = subprocess.run(
            [CLAUDE_BIN,
             "--print",
             "--output-format", "text",
             "--allowed-tools",
             "Read,Grep,Glob,Bash(git diff:*),Bash(git status:*)"],
            input=prompt,
            capture_output=True, text=True,
            timeout=CLAUDE_EDIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        sys.stderr.write(f"claude edit-gen failed: {exc}\n")
        return [], "claude invocation failed"

    if proc.returncode != 0:
        sys.stderr.write(
            f"claude edit-gen rc={proc.returncode} "
            f"stderr={proc.stderr[:500]}\n"
        )
        return [], "claude exited non-zero"

    text = proc.stdout or ""
    # Parse the LAST balanced {...} block on stdout.
    last = _extract_last_json(text)
    if not last:
        return [], "no parseable JSON in claude output"
    edits = last.get("edits") or []
    summary = last.get("summary") or ""
    if not isinstance(edits, list):
        return [], "edits field is not a list"
    return edits, summary[:500]


def _extract_last_json(text: str) -> "dict | None":
    depth = 0
    start = -1
    last_valid = None
    for i, ch in enumerate(text or ""):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    last_valid = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    pass
                start = -1
    return last_valid


# --- Stage 5: commit + push ------------------------------------------------

def commit_and_push(repo_path: Path, branch: str, message: str) -> str:
    """git add -A; git commit -m; git push. Returns the commit sha."""
    env = make_git_env()
    run([GIT_BIN, "add", "-A"], cwd=repo_path, env=env, log_label="add")

    # If nothing changed, fail loudly — the build had no effect.
    diff_proc = run(
        [GIT_BIN, "diff", "--cached", "--quiet"],
        cwd=repo_path, env=env, log_label="diff-check", check=False,
    )
    if diff_proc.returncode == 0:
        raise RuntimeError("no changes to commit (edit list was empty or no-op)")

    run([GIT_BIN, "commit", "-m", message],
        cwd=repo_path, env=env, log_label="commit")
    sha_proc = run([GIT_BIN, "rev-parse", "HEAD"],
                   cwd=repo_path, env=env, log_label="rev-parse")
    sha = sha_proc.stdout.strip()
    run([GIT_BIN, "push", "-u", "origin", branch],
        cwd=repo_path, env=env, log_label="push", timeout=GIT_TIMEOUT_S * 2)
    return sha


# --- Stage 6: open PR ------------------------------------------------------

def create_pr(target_repo: str, branch: str, title: str, body: str) -> str:
    """gh pr create against upstream from leviathan-agent:branch.
    Returns the PR URL."""
    proc = run(
        [GH_BIN, "pr", "create",
         "--repo", target_repo,
         "--head", f"{FORK_OWNER}:{branch}",
         "--title", title[:200],
         "--body", body[:60000]],
        log_label="pr-create",
    )
    # `gh pr create` prints the URL on its last stdout line.
    out = (proc.stdout or "").strip()
    url = ""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("https://"):
            url = line
            break
    if not url:
        raise RuntimeError(f"gh pr create returned no URL: {out[:300]!r}")
    return url


# --- Main ------------------------------------------------------------------

def version_banner() -> dict:
    components = {}
    for name in ("python3", "bash", "git", "gh", "node", "claude"):
        path = shutil.which(name)
        if not path:
            components[name] = {"present": False, "path": None, "version": None}
            continue
        try:
            r = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            v = (r.stdout or r.stderr or "").strip().splitlines()[0][:100]
        except Exception as exc:
            v = f"probe-failed: {type(exc).__name__}"
        components[name] = {"present": True, "path": path, "version": v}
    all_present = all(c["present"] for c in components.values())
    return {
        "mode": "version",
        "worker": "build",
        "all_components_present": all_present,
        "components": components,
        "fork_owner": FORK_OWNER,
        "commit_identity": f"{COMMIT_NAME} <{COMMIT_EMAIL}>",
    }


def main() -> "None":
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        banner = version_banner()
        emit(banner, exit_code=0 if banner["all_components_present"] else 1)

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            emit({"error": "no_job", "detail": "stdin was empty"}, exit_code=2)
        job = json.loads(raw)
    except json.JSONDecodeError as exc:
        emit({"error": "bad_job_json", "detail": str(exc)}, exit_code=2)

    required = {"draft_uuid", "target_repo", "target_branch", "title",
                "commit_message"}
    missing = required - set(job.keys())
    if missing:
        emit({"error": "missing_job_fields", "missing": sorted(missing)},
             exit_code=2)

    job_uuid = str(job.get("build_uuid") or job.get("job_uuid") or job["draft_uuid"])
    target_repo = str(job["target_repo"])
    target_branch = str(job["target_branch"])
    title = str(job["title"])
    pr_body = str(job.get("pr_body") or "")
    commit_message = str(job["commit_message"])
    structured_edits = job.get("edits") or []
    plan_body = job.get("plan_body_md") or job.get("pr_body") or ""

    # Per-job working directory in the container's tmpfs.
    work_dir = WORK_ROOT / job_uuid
    work_dir.mkdir(parents=True, exist_ok=True)

    stage = "init"
    try:
        stage = "fork"
        ensure_fork(target_repo)

        stage = "clone"
        repo_path = clone_fork(target_repo, work_dir)

        stage = "rebase"
        rebase_against_upstream(repo_path, target_repo)

        stage = "branch"
        checkout_new_branch(repo_path, target_branch)

        stage = "edit"
        used_path = "structured"
        if structured_edits:
            apply_structured_edits(repo_path, structured_edits)
        else:
            used_path = "claude-generated"
            edits, summary = generate_edits_via_claude(
                repo_path, target_repo, target_branch, title, plan_body,
            )
            if not edits:
                payload = {
                    "draft_uuid": job["draft_uuid"],
                    "status": "failed",
                    "stage": "edit",
                    "error": f"claude produced no edits: {summary}",
                }
                try:
                    write_result_atomically(job_uuid, payload)
                except OSError as exc:
                    sys.stderr.write(f"scratch write failed: {exc}\n")
                emit(payload, exit_code=0)
            apply_structured_edits(repo_path, edits)
            if summary and not pr_body:
                # Use Claude's summary as the PR body if the operator didn't
                # provide one.
                pr_body = summary

        stage = "commit-push"
        commit_sha = commit_and_push(repo_path, target_branch, commit_message)

        stage = "pr"
        pr_url = create_pr(target_repo, target_branch, title, pr_body)

        # === Atomic scratch FIRST — durability boundary ===
        payload = {
            "draft_uuid": job["draft_uuid"],
            "status": "ok",
            "pr_url": pr_url,
            "commit_sha": commit_sha,
            "branch": target_branch,
            "edits_path": used_path,
        }
        try:
            write_result_atomically(job_uuid, payload)
        except OSError as exc:
            sys.stderr.write(
                f"scratch write failed (PR still emitted on stdout): {exc}\n"
            )
        emit(payload, exit_code=0)

    except subprocess.CalledProcessError as exc:
        # exc.stderr is already scrubbed (see run()); double-scrub
        # everything else just in case the cmd token slipped in.
        payload = {
            "draft_uuid": job["draft_uuid"],
            "status": "failed",
            "stage": stage,
            "error": _scrub_secrets(
                f"{exc.cmd[0]} rc={exc.returncode}: {(exc.stderr or '')[:400]}"
            ),
        }
        try:
            write_result_atomically(job_uuid, payload)
        except OSError:
            pass
        emit(payload, exit_code=0)
    except Exception as exc:
        payload = {
            "draft_uuid": job["draft_uuid"],
            "status": "failed",
            "stage": stage,
            "error": _scrub_secrets(
                f"{type(exc).__name__}: {str(exc)[:400]}"
            ),
        }
        try:
            write_result_atomically(job_uuid, payload)
        except OSError:
            pass
        emit(payload, exit_code=0)


if __name__ == "__main__":
    main()

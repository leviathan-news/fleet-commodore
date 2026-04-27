#!/usr/bin/env python3
"""Fleet Commodore build (branch-and-PR) worker container entrypoint.

This is the SAME-REPO branch-and-PR pattern that Dependabot, Renovate, and
every other GitHub bot installed on its target org uses. NO FORKING.

Why no forking: GitHub's `POST /repos/{owner}/{repo}/forks` requires
`Administration: write` which most org policies clamp to read for
fine-grained PATs and even for GitHub Apps. We hit that empirically on
the leviathan-news org — the PAT was approved with Contents: write but
the fork API still 403'd. Fork-via-web-UI works but doesn't scale to
"any of the dozen leviathan repos."

The right pattern: install a GitHub App on the org with `Contents: write`
and `Pull requests: write`, mint short-lived installation tokens on demand,
push branches DIRECTLY to the source repo, file PRs same-repo.

Auth: the worker mints an installation access token at the start of every
build by signing a JWT with the App's private key (mounted at
/var/run/commodore-secrets/github-app-key.pem) and POSTing to
/app/installations/{id}/access_tokens. Token is valid 1h — plenty for one
build. No user identity, no fork, no membership, no operator clicks.

Pipeline:
  1. Read job JSON from stdin: {draft_uuid, target_repo, target_branch,
     title, pr_body, commit_message, edits?, plan_body_md?}
  2. Mint installation access token from App private key + installation_id.
  3. Clone source repo via token-in-URL into /tmp/build/<uuid>/repo.
  4. git checkout -b <target_branch>.
  5. Apply edits. Two paths:
     a. If `edits` is a non-empty list of {action, path, content|diff},
        apply those literally (write/patch).
     b. Otherwise (the typical v6 path — operator refined a plan in chat),
        invoke Claude inside the worker with the plan body + repo
        checkout, ask it to produce the structured edit list, then apply.
  6. git add -A && git commit -m <commit_message>.
  7. git push -u origin <target_branch>  (to the SAME source repo).
  8. gh pr create --repo <target_repo> --head <target_branch>  (same-repo).
  9. Atomic scratch write {pr_url, commit_sha, branch} BEFORE stdout JSON.
 10. Emit ONE JSON object on stdout.

The atomic scratch write is the durability boundary. If we crash after
the file appears at its final name, the host coordinator's recovery
treats the side effect (the PR) as committed and the GitHub pre-flight
oracle (`gh pr list`) confirms it.

Backward-compat: if GITHUB_APP_ID is not set, falls back to GH_TOKEN
(the legacy PAT path) but logs a deprecation warning. The PAT path
requires a pre-existing fork at FORK_OWNER/<repo> and will fail clean
if absent.

Exit codes: 0 ok, 2 bad job, 5 internal error. Errors during pipeline
stages are reported in-band (status=failed, stage=auth|clone|edit|push|pr)
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

# GitHub App auth (preferred). Set all three to enable App auth.
# - APP_ID is numeric, from the App's settings page.
# - INSTALLATION_ID is numeric, identifying the org/account where the
#   App is installed. Read off the App's installation page URL or via
#   `GET /app/installations` once authenticated.
# - PRIVATE_KEY_PATH is the PEM file mounted into the container (mode 0o600).
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "").strip()
GITHUB_APP_INSTALLATION_ID = os.environ.get("GITHUB_APP_INSTALLATION_ID", "").strip()
GITHUB_APP_PRIVATE_KEY_PATH = Path(
    os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH",
                   "/var/run/commodore-secrets/github-app-key.pem")
)

# Legacy PAT fallback — only used if GitHub App config is incomplete.
# Same pattern as the user PAT: requires a pre-existing fork and only
# works for repos where leviathan-agent has push access.
LEGACY_FORK_OWNER = os.environ.get("BUILD_FORK_OWNER", "leviathan-agent")

# Commit identity. App auth: GitHub recommends `<bot-name>[bot]@users.noreply.github.com`
# with the bot's user ID prefix; we set sensible defaults but allow override.
COMMIT_NAME = os.environ.get("BUILD_COMMIT_NAME", "Fleet Commodore")
COMMIT_EMAIL = os.environ.get("BUILD_COMMIT_EMAIL",
                              "leviathan-fleet-commodore[bot]@users.noreply.github.com")

# Working directory inside the container's tmpfs.
WORK_ROOT = Path(os.environ.get("BUILD_WORK_ROOT", "/tmp/build"))

# Soft budgets. The launcher's wall timeout (10 min default) is the hard cap.
CLAUDE_EDIT_TIMEOUT_S = int(os.environ.get("BUILD_CLAUDE_TIMEOUT_S", "300"))
GIT_TIMEOUT_S = int(os.environ.get("BUILD_GIT_TIMEOUT_S", "120"))

# Cached installation token + expiry — avoid minting per-step.
_cached_installation_token = None
_cached_installation_token_exp = 0.0


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


def _have_app_config() -> bool:
    """True if all three GitHub App config bits are present."""
    return bool(
        GITHUB_APP_ID and
        GITHUB_APP_INSTALLATION_ID and
        GITHUB_APP_PRIVATE_KEY_PATH.is_file()
    )


def _mint_app_jwt() -> str:
    """Mint a 10-minute JWT signed with the App's RS256 private key.
    Used ONLY to exchange for an installation access token — never sent
    to git/gh directly."""
    import jwt as _jwt
    import time as _time
    private_key = GITHUB_APP_PRIVATE_KEY_PATH.read_text()
    now = int(_time.time())
    payload = {
        # iat backdated 60s to tolerate clock skew between Mini and api.github.com
        "iat": now - 60,
        # exp 9 minutes (max GitHub allows is 10)
        "exp": now + 9 * 60,
        "iss": GITHUB_APP_ID,
    }
    return _jwt.encode(payload, private_key, algorithm="RS256")


def _mint_installation_token() -> str:
    """Exchange a freshly-minted App JWT for an installation access token
    (1-hour TTL, scoped to the installation's permissions). Cached for
    the lifetime of the worker process so we don't re-mint per stage."""
    import time as _time
    global _cached_installation_token, _cached_installation_token_exp
    # Reuse if more than 5 minutes left on the cached token
    if _cached_installation_token and _cached_installation_token_exp - _time.time() > 300:
        return _cached_installation_token

    import requests
    app_jwt = _mint_app_jwt()
    resp = requests.post(
        f"https://api.github.com/app/installations/"
        f"{GITHUB_APP_INSTALLATION_ID}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15,
    )
    if resp.status_code != 201:
        raise RuntimeError(
            f"failed to mint installation token: HTTP {resp.status_code} — "
            f"{(resp.text or '')[:300]}"
        )
    body = resp.json()
    _cached_installation_token = body["token"]
    # 'expires_at' is ISO8601, e.g. "2026-04-27T10:30:00Z"
    from datetime import datetime as _dt
    expires_at = body.get("expires_at", "")
    try:
        # Strip the Z and parse — datetime.fromisoformat accepts naive ISO
        exp_dt = _dt.fromisoformat(expires_at.replace("Z", "+00:00"))
        _cached_installation_token_exp = exp_dt.timestamp()
    except (ValueError, TypeError):
        # If parse fails, assume 1h from now (the GitHub default)
        _cached_installation_token_exp = _time.time() + 3500
    return _cached_installation_token


def gh_token() -> str:
    """Returns a token suitable for `gh` CLI calls + git push.

    Preferred path: GitHub App installation token (minted via JWT).
    Fallback path: legacy GH_TOKEN env var (PAT).

    The App path is what makes the bot work on org repos without
    forking — installation tokens carry `Contents: write` and
    `Pull requests: write` directly on the source repo, no fork needed.
    """
    if _have_app_config():
        return _mint_installation_token()
    legacy = os.environ.get("GH_TOKEN", "").strip()
    if legacy:
        sys.stderr.write(
            "[auth] WARNING: using legacy GH_TOKEN PAT path. App config "
            "(GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_"
            "PRIVATE_KEY_PATH) not set; PR-filing requires a pre-existing "
            "fork at " + LEGACY_FORK_OWNER + "/<repo>.\n"
        )
        return legacy
    raise RuntimeError(
        "no GitHub auth available — set GITHUB_APP_ID + "
        "GITHUB_APP_INSTALLATION_ID + GITHUB_APP_PRIVATE_KEY_PATH "
        "(preferred) OR GH_TOKEN (legacy)"
    )


def make_git_env() -> dict:
    """Subprocess env for git: token-free, no interactive prompts."""
    e = dict(os.environ)
    e["GIT_TERMINAL_PROMPT"] = "0"
    e["GIT_AUTHOR_NAME"] = COMMIT_NAME
    e["GIT_AUTHOR_EMAIL"] = COMMIT_EMAIL
    e["GIT_COMMITTER_NAME"] = COMMIT_NAME
    e["GIT_COMMITTER_EMAIL"] = COMMIT_EMAIL
    # When using App auth, gh CLI also needs GH_TOKEN — set it to the
    # installation token. (GH_TOKEN is short-lived, won't outlive the build.)
    if _have_app_config():
        try:
            e["GH_TOKEN"] = _mint_installation_token()
        except Exception:
            pass  # gh_token() raises clearly downstream if needed
    return e


# --- Stage 1+2: clone (App path = source; legacy path = fork) -------------

def clone_repo(target_repo: str, work_dir: Path) -> Path:
    """Clone the SOURCE repo via the installation token (or legacy PAT
    against a pre-existing fork). Returns the repo working-tree path.

    App path: clones leviathan-news/<repo> directly. Branch + push will
    target this same remote. No fork involved.

    Legacy PAT path: requires a pre-existing fork at LEGACY_FORK_OWNER/
    <repo> (operator-pre-created). Clones the fork, adds upstream, hard-
    resets to upstream/main so the branch is current.
    """
    repo_name = target_repo.split("/")[-1]
    repo_path = work_dir / "repo"

    if _have_app_config():
        # App path — clone source directly.
        clone_url = (
            f"https://x-access-token:{gh_token()}@github.com/{target_repo}.git"
        )
        run([GIT_BIN, "clone", "--depth=50", clone_url, str(repo_path)],
            env=make_git_env(), log_label="clone-source")
        return repo_path

    # Legacy PAT path — fork-and-PR.
    sys.stderr.write(
        "[clone] WARNING: legacy PAT path. Operator must have pre-created "
        f"a fork at {LEGACY_FORK_OWNER}/{repo_name}.\n"
    )
    # Verify fork existence via API (cheap, no perms beyond read).
    check = subprocess.run(
        [GH_BIN, "api", f"repos/{LEGACY_FORK_OWNER}/{repo_name}", "--silent"],
        capture_output=True, text=True, timeout=30,
    )
    if check.returncode != 0:
        # Try API fork as a courtesy (will likely 403 on org-clamped PATs).
        fork_proc = subprocess.run(
            [GH_BIN, "repo", "fork", target_repo, "--clone=false"],
            capture_output=True, text=True, timeout=60,
        )
        if fork_proc.returncode != 0:
            raise RuntimeError(
                f"fork missing and API create failed (rc={fork_proc.returncode}). "
                f"Operator action: visit https://github.com/{target_repo} as "
                f"{LEGACY_FORK_OWNER} and click `Fork`. OR migrate to App auth "
                f"(set GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, "
                f"GITHUB_APP_PRIVATE_KEY_PATH) and remove the legacy PAT — "
                f"the App path requires no fork at all."
            )

    fork_url = (
        f"https://x-access-token:{gh_token()}@github.com/{LEGACY_FORK_OWNER}/{repo_name}.git"
    )
    run([GIT_BIN, "clone", "--depth=50", fork_url, str(repo_path)],
        env=make_git_env(), log_label="clone-fork")

    # Rebase fork's main on top of upstream/main so we branch off current code.
    upstream_url = (
        f"https://x-access-token:{gh_token()}@github.com/{target_repo}.git"
    )
    env = make_git_env()
    run([GIT_BIN, "remote", "add", "upstream", upstream_url],
        cwd=repo_path, env=env, log_label="remote-add", check=False)
    run([GIT_BIN, "fetch", "upstream", "main"],
        cwd=repo_path, env=env, log_label="fetch-upstream")
    run([GIT_BIN, "checkout", "main"],
        cwd=repo_path, env=env, log_label="checkout-main")
    run([GIT_BIN, "reset", "--hard", "upstream/main"],
        cwd=repo_path, env=env, log_label="reset-to-upstream")
    return repo_path


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
    """gh pr create — same-repo when using App auth, cross-repo when
    using legacy PAT (head is `leviathan-agent:branch`)."""
    if _have_app_config():
        head = branch  # same-repo: no `owner:` prefix
    else:
        head = f"{LEGACY_FORK_OWNER}:{branch}"
    proc = run(
        [GH_BIN, "pr", "create",
         "--repo", target_repo,
         "--head", head,
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
    # Try to import jwt — required for App auth.
    try:
        import jwt as _jwt  # noqa: F401
        jwt_present = True
    except ImportError:
        jwt_present = False

    auth_path = "app" if _have_app_config() else (
        "legacy_pat" if os.environ.get("GH_TOKEN") else "none"
    )

    return {
        "mode": "version",
        "worker": "build",
        "all_components_present": all_present and jwt_present,
        "components": components,
        "pyjwt_present": jwt_present,
        "auth_path": auth_path,
        "legacy_fork_owner": LEGACY_FORK_OWNER,
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
        stage = "auth"
        # Force-mint a token at the start so we fail fast on auth misconfig
        # rather than mid-clone with a misleading "Permission denied" error.
        gh_token()

        stage = "clone"
        # clone_repo handles both App path (clone source) and legacy PAT
        # path (clone fork + rebase against upstream).
        repo_path = clone_repo(target_repo, work_dir)

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

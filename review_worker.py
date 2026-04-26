#!/usr/bin/env python3
"""Fleet Commodore review-worker container entrypoint.

Pipeline:
  1. Read job dict from stdin: {review_uuid, repo, pr_number}
  2. Fetch PR metadata via `gh pr view <num> --repo <repo> --json ...`
  3. Fetch diff via `gh pr diff <num> --repo <repo>` (truncated to a budget)
  4. Spawn Claude CLI with the formatted-review prompt + the diff as input
  5. Parse Claude's reply for verdict + bulleted findings
  6. Write `/var/run/commodore-results/<uuid>.result.json` atomically BEFORE
     emitting stdout (so the host coordinator has the durable record even
     if our stdout JSON is malformed)
  7. Emit ONE JSON object to stdout — the launcher parses the LAST balanced
     {...} so internal log noise on stderr is fine.

Single exit point: emit() writes one JSON object then sys.exit. All errors
are wrapped into the same envelope shape so the parent always parses cleanly.

The atomic scratch-file write IS the durability boundary. If we crash after
the file appears at its final name, the coordinator's recovery path treats
the side effect as committed.
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

# Resolve at import time. npm-global on different distros plants the
# `claude` symlink at /usr/local/bin OR /usr/bin depending on prefix.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "/usr/bin/claude"
GH_BIN = os.environ.get("GH_BIN") or shutil.which("gh") or "/usr/bin/gh"

# Container path for the result-scratch handoff. Bound by the launcher to
# the host's RESULTS_DIR. Never write outside this directory.
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "/var/run/commodore-results"))

# Diff size cap. Telegram replies max out at 4096 chars; the review body is
# already constrained by the WAL helper. The diff itself feeds Claude, so
# the cap here is about Claude context, not Telegram.
DIFF_BYTE_BUDGET = int(os.environ.get("REVIEW_DIFF_BUDGET", "150000"))

# Claude wall-clock budget. The launcher's outer 600s is the hard cap; this
# is the soft budget for Claude alone so we leave headroom for gh + parse.
CLAUDE_TIMEOUT_S = int(os.environ.get("REVIEW_CLAUDE_TIMEOUT_S", "420"))


# --- Single-exit emitter ---------------------------------------------------

def emit(obj: dict, exit_code: int = 0) -> "None":
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    sys.exit(exit_code)


# --- Atomic scratch-file write ---------------------------------------------

def write_result_atomically(uuid: str, payload: dict) -> None:
    """Write `<uuid>.result.json` to RESULTS_DIR using temp + fsync + rename.

    POSIX rename(2) on the same filesystem is atomic, so a concurrent reader
    on the host sees either the prior absence or the complete new file —
    never partial JSON. Final dir-fsync ensures the directory entry survives
    a crash.
    """
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


# --- gh helpers ------------------------------------------------------------

def gh_pr_view(repo: str, pr_number: int) -> dict:
    """Fetch structured PR metadata. Returns {} on error so caller can decide."""
    try:
        proc = subprocess.run(
            [GH_BIN, "pr", "view", str(pr_number), "--repo", repo,
             "--json", "title,body,baseRefName,headRefName,author,additions,deletions,files"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        sys.stderr.write(f"gh_pr_view failed: {exc}\n")
        return {}
    if proc.returncode != 0:
        sys.stderr.write(f"gh_pr_view rc={proc.returncode} stderr={proc.stderr[:300]}\n")
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def gh_pr_diff(repo: str, pr_number: int, byte_budget: int) -> str:
    """Fetch the unified diff. Truncated to byte_budget characters."""
    try:
        proc = subprocess.run(
            [GH_BIN, "pr", "diff", str(pr_number), "--repo", repo],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        sys.stderr.write(f"gh_pr_diff failed: {exc}\n")
        return ""
    if proc.returncode != 0:
        sys.stderr.write(f"gh_pr_diff rc={proc.returncode}\n")
        return ""
    diff = proc.stdout or ""
    if len(diff) > byte_budget:
        return diff[:byte_budget] + "\n\n[... truncated by review_worker ...]"
    return diff


# --- Claude CLI invocation -------------------------------------------------

REVIEW_PROMPT = """You are reviewing a pull request for the Leviathan News project.
Your output MUST be a single line "VERDICT: <one of approve|request_changes|comment>"
followed by 1-8 bullet-point findings (each starting with "- ").

Be concise. Speak as a senior reviewer. Flag correctness issues, missed edge
cases, security concerns, and obvious wins. Do NOT recapitulate the diff.

PR title: {title}
Author: {author}
Base: {base_ref}
Head: {head_ref}
Lines: +{adds} / -{dels}
Files changed: {n_files}

PR body:
{body}

Diff (may be truncated):
```diff
{diff}
```
"""


def run_claude_review(prompt: str) -> str:
    """Spawn Claude CLI with the prompt on stdin. Returns Claude's stdout
    (the verdict + findings), or empty string on failure."""
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "--print", "--output-format", "text"],
            input=prompt,
            capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        sys.stderr.write(f"claude run failed: {exc}\n")
        return ""
    if proc.returncode != 0:
        sys.stderr.write(f"claude rc={proc.returncode} stderr={proc.stderr[:500]}\n")
        return ""
    return (proc.stdout or "").strip()


# --- Verdict parsing -------------------------------------------------------

_VERDICT_RE = re.compile(
    r"VERDICT\s*:\s*(approve|request_changes|comment)", re.IGNORECASE,
)
# Findings: lines that start with "- " or "* ", optionally indented.
_FINDING_RE = re.compile(r"^\s*[-*]\s+(.+?)$", re.MULTILINE)


def parse_review(claude_text: str) -> tuple:
    """Returns (verdict, findings_list). Empty verdict if not parseable."""
    if not claude_text:
        return "", []
    m = _VERDICT_RE.search(claude_text)
    verdict = m.group(1).lower() if m else ""
    findings = [f.strip() for f in _FINDING_RE.findall(claude_text)]
    return verdict, findings[:8]


# --- Main ------------------------------------------------------------------

def version_banner() -> dict:
    """Image smoke-test path. Unchanged from v1 stub."""
    import shutil
    components = {}
    for name in ("python3", "bash", "git", "gh", "node", "claude"):
        path = shutil.which(name)
        if not path:
            components[name] = {"present": False, "path": None, "version": None}
            continue
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            version = (result.stdout or result.stderr or "").strip().splitlines()[0][:100]
        except Exception as exc:
            version = f"probe-failed: {type(exc).__name__}"
        components[name] = {"present": True, "path": path, "version": version}
    py_deps = {}
    for mod in ("sqlparse", "psycopg2", "requests"):
        try:
            imported = __import__(mod)
            py_deps[mod] = {"present": True,
                            "version": getattr(imported, "__version__", "unknown")}
        except ImportError as exc:
            py_deps[mod] = {"present": False, "error": str(exc)}
    all_present = all(c["present"] for c in components.values()) and all(
        d["present"] for d in py_deps.values()
    )
    return {
        "mode": "version",
        "all_components_present": all_present,
        "components": components,
        "python_deps": py_deps,
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

    required = {"review_uuid", "repo", "pr_number"}
    missing = required - set(job.keys())
    if missing:
        emit({"error": "missing_job_fields", "missing": sorted(missing)},
             exit_code=2)

    review_uuid = str(job["review_uuid"])
    repo = str(job["repo"])
    pr_number = int(job["pr_number"])

    # Fetch PR metadata. Empty dict on failure — review still produces a
    # best-effort dispatch with whatever the diff alone reveals.
    meta = gh_pr_view(repo, pr_number)
    diff = gh_pr_diff(repo, pr_number, DIFF_BYTE_BUDGET)
    if not diff:
        # No diff means we can't review. Write the failure to scratch so
        # recovery doesn't relaunch and try again.
        payload = {
            "review_uuid": review_uuid,
            "repo": repo, "pr_number": pr_number,
            "status": "failed",
            "verdict": "",
            "findings": [],
            "error": "diff_unavailable",
        }
        try:
            write_result_atomically(review_uuid, payload)
        except OSError as exc:
            sys.stderr.write(f"scratch write failed: {exc}\n")
        emit(payload, exit_code=0)

    author = ((meta.get("author") or {}).get("login")) or "?"
    prompt = REVIEW_PROMPT.format(
        title=(meta.get("title") or "")[:200],
        author=author[:60],
        base_ref=(meta.get("baseRefName") or "?")[:60],
        head_ref=(meta.get("headRefName") or "?")[:60],
        adds=meta.get("additions") or 0,
        dels=meta.get("deletions") or 0,
        n_files=len(meta.get("files") or []),
        body=(meta.get("body") or "")[:1500],
        diff=diff,
    )

    claude_out = run_claude_review(prompt)
    verdict, findings = parse_review(claude_out)

    if not verdict:
        # Claude returned nothing parseable. Record the failure to scratch
        # so the coordinator can post the casualty message rather than
        # relaunching.
        payload = {
            "review_uuid": review_uuid,
            "repo": repo, "pr_number": pr_number,
            "status": "failed",
            "verdict": "",
            "findings": findings,
            "error": "claude_unparseable",
            "claude_excerpt": claude_out[:500] if claude_out else "",
        }
        try:
            write_result_atomically(review_uuid, payload)
        except OSError as exc:
            sys.stderr.write(f"scratch write failed: {exc}\n")
        emit(payload, exit_code=0)

    payload = {
        "review_uuid": review_uuid,
        "repo": repo, "pr_number": pr_number,
        "status": "ok",
        "verdict": verdict,
        "findings": findings,
        "diff_bytes": len(diff),
    }
    # Atomic write FIRST — this is the durability boundary. If we crash
    # after this point and before emit(), the host coordinator's recovery
    # picks up the scratch file as the source of truth.
    try:
        write_result_atomically(review_uuid, payload)
    except OSError as exc:
        sys.stderr.write(f"scratch write failed (review still emitted): {exc}\n")

    emit(payload, exit_code=0)


if __name__ == "__main__":
    main()

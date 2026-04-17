#!/usr/bin/env python3
"""Fleet Commodore review-worker container entrypoint.

Reads a review job dict from stdin as JSON, runs the review via Claude CLI
(with Bash(gh pr diff:*) + Bash(gh pr view:*) + Bash(commodore-db:*) +
Bash(commodore-orm:*) allowlisted), and writes findings JSON to stdout.

The launcher (bin/launch-review-container) parses ONE JSON object from
stdout per invocation — so any interior log lines must go to stderr, and
the final object must be the last balanced {...} on stdout.

This file is v1 STUB: implements the JSON contract + version mode so the
image build + smoke tests can pass. Task #15 implements the actual review
logic (gh fetch, Claude spawn, JSON parse, dispatch formatting).
"""
from __future__ import annotations

import json
import os
import sys


def emit(obj: dict, exit_code: int = 0) -> "None":
    """Single exit point. Writes ONE JSON object to stdout, exits."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    sys.exit(exit_code)


def version_banner() -> dict:
    """Short-circuit for `docker run --rm commodore-reviewer:latest --version`.

    Used by bin/build-reviewer-image.sh to verify the image has all its
    required components. Exits 0 if all present, 1 if any is missing.
    """
    import shutil
    import subprocess

    components = {}
    for name in ("python3", "bash", "git", "gh", "node", "claude", "codex"):
        path = shutil.which(name)
        if not path:
            components[name] = {"present": False, "path": None, "version": None}
            continue
        # Probe each tool for a version string. Keep timeouts tight so a
        # broken binary doesn't hang the smoke test.
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            version = (result.stdout or result.stderr or "").strip().splitlines()[0][:100]
        except Exception as exc:
            version = f"probe-failed: {type(exc).__name__}"
        components[name] = {"present": True, "path": path, "version": version}

    # Also confirm Python deps are importable.
    py_deps = {}
    for mod in ("sqlparse", "psycopg2", "requests"):
        try:
            imported = __import__(mod)
            py_deps[mod] = {"present": True, "version": getattr(imported, "__version__", "unknown")}
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
        "user": os.environ.get("USER", "unknown"),
        "home": os.environ.get("HOME", "unknown"),
    }


def main() -> "None":
    # --version short-circuit: image smoke-test path.
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        banner = version_banner()
        emit(banner, exit_code=0 if banner["all_components_present"] else 1)

    # Normal path: read job JSON from stdin, run the review, emit findings.
    # Task #15 fills this in; v1 emits a placeholder so the pipe contract works.
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            emit({"error": "no_job", "detail": "stdin was empty"}, exit_code=2)
        job = json.loads(raw)
    except json.JSONDecodeError as exc:
        emit({"error": "bad_job_json", "detail": str(exc)}, exit_code=2)

    # Required job fields (contract with the launcher).
    required = {"review_uuid", "repo", "pr_number"}
    missing = required - set(job.keys())
    if missing:
        emit({"error": "missing_job_fields", "missing": sorted(missing)}, exit_code=2)

    # v1 stub: acknowledge the job shape, report NOT YET IMPLEMENTED cleanly
    # rather than posting fake findings. Task #15 replaces this with the real
    # gh + Claude flow.
    emit({
        "review_uuid": job["review_uuid"],
        "repo": job["repo"],
        "pr_number": job["pr_number"],
        "status": "not_implemented",
        "message": (
            "review_worker.py is v1 stub. Task #15 of the fleet-commodore "
            "plan wires up the actual gh fetch + Claude review. The image "
            "and sidecar plumbing is green; only the review pipeline itself "
            "is pending."
        ),
    }, exit_code=0)


if __name__ == "__main__":
    main()

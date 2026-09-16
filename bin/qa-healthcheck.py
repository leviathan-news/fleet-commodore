#!/usr/bin/env python3
"""Bounded, no-network Fleet Commodore Q&A readiness check.

This checks the dependencies that can make every Q&A/review/build request
fail before its worker starts: the local reviewer image, Q&A sidecars, OAuth
credential sources, and the configured database URL source. It deliberately
does not send Telegram or invoke Claude: the hourly OAuth heartbeat owns the
live provider probe, while the daemon owns deduplicated operator paging.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


DOCKER = os.environ.get("COMMODORE_DOCKER", "/usr/local/bin/docker")
REVIEWER_IMAGE = os.environ.get("COMMODORE_REVIEWER_IMAGE", "commodore-reviewer:latest")
QA_PROXY = os.environ.get("COMMODORE_QA_EGRESS_PROXY_HOST", "commodore-qa-egress-proxy")
QA_TUNNEL = os.environ.get("COMMODORE_QA_DB_TUNNEL_HOST", "commodore-qa-db-tunnel")
HOST_CLAUDE_DIR = Path(os.environ.get("COMMODORE_HOST_CLAUDE_DIR", "~/.claude")).expanduser()
HOST_CLAUDE_CONFIG = Path(os.environ.get("COMMODORE_HOST_CLAUDE_CONFIG", "~/.claude.json")).expanduser()
DB_URL_FILE = Path(os.environ.get("COMMODORE_DB_URL_FILE", "~/.config/commodore/db_url")).expanduser()


def _run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _docker_ok(*args: str) -> bool:
    result = _run([DOCKER, *args])
    return result is not None and result.returncode == 0


def _container_running(name: str) -> bool:
    result = _run([DOCKER, "container", "inspect", "--format", "{{.State.Running}}", name])
    return result is not None and result.returncode == 0 and result.stdout.strip() == "true"


def _readable_nonempty(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.R_OK) and bool(path.read_text().strip())
    except OSError:
        return False


def readiness_report(*, quick: bool) -> dict:
    checks = {
        "reviewer_image": _docker_ok("image", "inspect", REVIEWER_IMAGE),
        "qa_egress_proxy": _container_running(QA_PROXY),
        "qa_db_tunnel": _container_running(QA_TUNNEL),
        "claude_credentials": _readable_nonempty(HOST_CLAUDE_DIR / ".credentials.json"),
        "claude_config": _readable_nonempty(HOST_CLAUDE_CONFIG),
        "db_url_source": _readable_nonempty(DB_URL_FILE),
    }
    # Ensure an image that merely exists is still capable of running the QA
    # worker. `--version` does not contact Claude, Telegram, or Postgres.
    if not quick and checks["reviewer_image"]:
        checks["reviewer_worker"] = _docker_ok("run", "--rm", REVIEWER_IMAGE, "--version")

    failed = sorted(name for name, ok in checks.items() if not ok)
    warnings = []
    credential_age_seconds = None
    if checks["claude_credentials"]:
        try:
            credential_age_seconds = max(
                0, int(time.time() - (HOST_CLAUDE_DIR / ".credentials.json").stat().st_mtime)
            )
            if credential_age_seconds > 7 * 24 * 3600:
                warnings.append("claude_credentials_older_than_7_days")
        except OSError:
            warnings.append("claude_credentials_age_unavailable")
    # Age is a warning, not token validation: old credentials may work and
    # freshly written credentials may already be revoked.
    return {
        "ok": not failed, "checks": checks, "failed": failed,
        "warnings": warnings, "claude_credentials_age_seconds": credential_age_seconds,
        "provider_transport": "not_checked",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="skip the local worker version probe")
    args = parser.parse_args()
    report = readiness_report(quick=args.quick)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

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
import sqlite3
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
COMMODORE_DB_FILE = Path(os.environ.get(
    "COMMODORE_DB_FILE", "~/.local/state/fleet-commodore/commodore.db"
)).expanduser()
FLEET_PROVIDER = os.environ.get("FLEET_QA_PROVIDER", os.environ.get("FLEET_PROVIDER", "codex"))
CODEX_BIN = "/opt/homebrew/bin/codex"
_CHAT_INTAKE_STATUSES = (
    "queued", "running", "resolved", "escalated", "no_reply", "handed_off", "held_unknown",
)


def _chat_intake_health() -> dict:
    """Read chat-intake state without creating or mutating its SQLite file."""
    path = COMMODORE_DB_FILE.parent / "chat-intake.db"
    if not path.is_file():
        return {
            "status": "not_installed", "counts": {},
            "oldest_unresolved_age": None, "last_terminal_reply_at": None,
        }
    conn = None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        counts = {status: 0 for status in _CHAT_INTAKE_STATUSES}
        for status, count in conn.execute(
            "SELECT status, COUNT(*) FROM chat_intake_event GROUP BY status"
        ):
            if status in counts:
                counts[status] = count
        oldest = conn.execute(
            "SELECT MIN(created_at) FROM chat_intake_event "
            "WHERE status IN ('queued','running','handed_off','held_unknown')"
        ).fetchone()[0]
        last_reply = conn.execute(
            "SELECT MAX(finished_at) FROM chat_intake_event "
            "WHERE status IN ('resolved','escalated') AND message_id > 0"
        ).fetchone()[0]
        return {
            "status": "ok", "counts": counts,
            "oldest_unresolved_age": None if oldest is None else max(0.0, time.time() - oldest),
            "last_terminal_reply_at": last_reply,
        }
    except (OSError, sqlite3.Error):
        return {
            "status": "read_error", "counts": {},
            "oldest_unresolved_age": None, "last_terminal_reply_at": None,
        }
    finally:
        if conn is not None:
            conn.close()


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


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def readiness_report(*, quick: bool) -> dict:
    checks = {
        "reviewer_image": _docker_ok("image", "inspect", REVIEWER_IMAGE),
        "qa_egress_proxy": _container_running(QA_PROXY),
        "qa_db_tunnel": _container_running(QA_TUNNEL),
        "db_url_source": _readable_nonempty(DB_URL_FILE),
    }
    if FLEET_PROVIDER == "codex":
        checks["codex_executable"] = _executable(CODEX_BIN)
    elif FLEET_PROVIDER == "claude":
        checks["claude_credentials"] = _readable_nonempty(HOST_CLAUDE_DIR / ".credentials.json")
        checks["claude_config"] = _readable_nonempty(HOST_CLAUDE_CONFIG)
    else:
        checks["valid_provider"] = False
    # Ensure an image that merely exists is still capable of running the QA
    # worker. `--version` does not contact Claude, Telegram, or Postgres.
    if not quick and checks["reviewer_image"]:
        checks["reviewer_worker"] = _docker_ok("run", "--rm", REVIEWER_IMAGE, "--version")

    failed = sorted(name for name, ok in checks.items() if not ok)
    warnings = []
    chat_intake = _chat_intake_health()
    if chat_intake["status"] == "ok":
        if chat_intake["counts"].get("held_unknown", 0) > 0:
            warnings.append("chat_intake_held_unknown")
        age = chat_intake["oldest_unresolved_age"]
        if age is not None and age > 120:
            warnings.append("chat_intake_oldest_unresolved_over_120_seconds")
    credential_age_seconds = None
    if FLEET_PROVIDER != "codex" and checks.get("claude_credentials"):
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
        "provider": FLEET_PROVIDER,
        "provider_transport": "not_checked",
        "chat_intake": chat_intake,
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

#!/usr/bin/env python3
"""Emit a secret-free Fleet Commodore release manifest.

Run this from the exact immutable release directory on the Mini.  The manifest
is an attestation of executed files and selected non-secret runtime contract;
it is not a deployment command and never reads tokens, credentials, or .env.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_FILES = (
    "commodore.py",
    "chat_intake.py",
    "chat_dispatch.py",
    "intake_cutover.py",
    "fleet_watchdog.py",
    "outcome_watch.py",
    "helm_controller.py",
    "helm_sol_bridge.py",
    "helm_supervisor.py",
    "qa_worker.py",
    "codex_provider.py",
    "codex_runtime.py",
    "codex_qa.py",
    "qa_knowledge.py",
    "qa_github.py",
    "qa_schema.py",
    "qa_sources.py",
    "qa_sql.py",
    "bin/provider-probe.py",
    "bin/launch-qa-container",
    "bin/commodore-db",
    "triage/commodore_triage.py",
    "triage/RUNBOOK.md",
    "bin/qa-healthcheck.py",
    "cron/qa-healthcheck.sh",
    "cron/claude-oauth-heartbeat.sh",
    "cron/heartbeat_state.py",
    "cron/commodore-triage.sh",
    "cron/watchdog.sh",
    "cron/helm-controller-watchdog.sh",
    "docs/HELM_CONTROLLER_RUNBOOK.md",
    "run.sh",
    "scripts/release_manifest.py",
)
NON_SECRET_ENV = (
    "BOT_HQ_GROUP_ID",
    "SQUID_CAVE_GROUP_ID",
    "AGENT_CHAT_GROUP_ID",
    "LEV_DEV_GROUP_ID",
    "ATLAS_GROUP_ID",
    "LEV_SEC_GROUP_ID",
    "OPERATOR_DM_USER_ID",
    "TRIAGE_OPERATOR_DM_USER_ID",
    "CLAUDE_BIN",
    "FLEET_PROVIDER",
    "FLEET_QA_PROVIDER",
    "CODEX_CHAT_MODEL",
    "CODEX_QA_MODEL",
    "SEC_FEED_BIN",
    "TRIAGE_DB_FILE",
    "TRIAGE_POSTING_ENABLED",
    "TRIAGE_OPERATOR_RECONCILE_ENABLED",
    "FLEET_COMMODORE_CONFIG",
    "FLEET_COMMODORE_STATE_DIR",
    "COMMODORE_DB_FILE",
    "FLEET_COMMODORE_PYTHON",
    "HELM_CONTROLLER_DB_FILE",
    "HELM_CONTROLLER_RUNTIME_CONFIG",
    "HELM_FLEET_RELEASE",
    "HELM_SUCCESSOR_RELEASE",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_sha(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return result.stdout.strip() or "unavailable"


def _resolved_executable(value: str) -> str | None:
    if not value:
        return None
    resolved = shutil.which(value) if not os.path.isabs(value) else value
    if not resolved:
        return None
    try:
        return str(Path(resolved).expanduser().resolve(strict=True))
    except OSError:
        return str(Path(resolved).expanduser())


def _executable_sha256(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    return _sha256(path) if path.is_file() else None


def _python_environment() -> dict[str, str | None]:
    """Fingerprint the interpreter/environment that generated this manifest."""
    version = sys.version.replace("\n", " ").strip()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "freeze", "--all"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        freeze_sha256 = hashlib.sha256(result.stdout.encode()).hexdigest()
    except (OSError, subprocess.SubprocessError):
        freeze_sha256 = None
    return {
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": version,
        "pip_freeze_sha256": freeze_sha256,
    }


def build_manifest(root: Path = ROOT) -> dict:
    files: dict[str, str] = {}
    for relative in ARTIFACT_FILES:
        path = root / relative
        if not path.is_file():
            raise RuntimeError(f"required release file is missing: {relative}")
        files[relative] = _sha256(path)

    runtime = {name: os.environ.get(name, "") for name in NON_SECRET_ENV}
    # Match triage's runtime resolution for Claude. sec_feed is deliberately
    # different: production releases must configure its absolute wrapper path
    # explicitly, never discover an arbitrary executable on PATH.
    claude_effective = runtime["CLAUDE_BIN"] or shutil.which("claude") or ""
    feed_effective = runtime["SEC_FEED_BIN"]
    runtime["CLAUDE_BIN_EFFECTIVE"] = claude_effective
    runtime["SEC_FEED_BIN_EFFECTIVE"] = feed_effective
    runtime["CLAUDE_BIN_REALPATH"] = _resolved_executable(claude_effective)
    runtime["SEC_FEED_BIN_REALPATH"] = _resolved_executable(feed_effective)
    runtime["CLAUDE_BIN_SHA256"] = _executable_sha256(runtime["CLAUDE_BIN_REALPATH"])
    runtime["SEC_FEED_BIN_SHA256"] = _executable_sha256(runtime["SEC_FEED_BIN_REALPATH"])
    runtime["CODEX_BIN_REALPATH"] = _resolved_executable("/opt/homebrew/bin/codex")
    runtime["CODEX_BIN_SHA256"] = _executable_sha256(runtime["CODEX_BIN_REALPATH"])
    readiness = {
        "operator_dm_pinned": bool(runtime["TRIAGE_OPERATOR_DM_USER_ID"] or runtime["OPERATOR_DM_USER_ID"]),
        "claude_executable_resolved": runtime["CLAUDE_BIN_REALPATH"] is not None,
        "conversation_provider_resolved": (
            runtime["CODEX_BIN_REALPATH"] is not None
            if (runtime["FLEET_PROVIDER"] or "codex") == "codex"
            else runtime["FLEET_PROVIDER"] == "claude" and runtime["CLAUDE_BIN_REALPATH"] is not None
        ),
        "sec_feed_explicit_absolute": bool(
            feed_effective
            and Path(feed_effective).expanduser().is_absolute()
            and runtime["SEC_FEED_BIN_REALPATH"]
        ),
        "python_runtime_resolved": bool(
            runtime["FLEET_COMMODORE_PYTHON"]
            and _resolved_executable(runtime["FLEET_COMMODORE_PYTHON"])
        ),
    }
    aggregate = hashlib.sha256(
        json.dumps({"files": files, "runtime": runtime}, sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(root),
        "artifact_sha256": aggregate,
        "files": files,
        "runtime": runtime,
        "python_environment": _python_environment(),
        "readiness": readiness,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write JSON atomically to this path")
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="fail unless the pinned operator, Claude, and sec_feed release inputs resolve",
    )
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest()
    except RuntimeError as exc:
        print(f"release manifest failed: {exc}", file=sys.stderr)
        return 1
    if args.require_ready and not all(manifest["readiness"].values()):
        print(
            "release manifest failed readiness: "
            + json.dumps(manifest["readiness"], sort_keys=True),
            file=sys.stderr,
        )
        return 1
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, args.output)
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

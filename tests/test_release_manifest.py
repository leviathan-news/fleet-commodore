"""Release manifest is secret-free and binds the executed artifact inputs."""
from __future__ import annotations

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release_manifest.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("release_manifest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_manifest_binds_release_files_without_reading_secret_env(monkeypatch):
    module = _load_module()
    monkeypatch.setenv("BOT_TOKEN", "must-not-appear")
    monkeypatch.setenv("GH_PAT", "must-not-appear-either")
    monkeypatch.setenv("TRIAGE_DB_FILE", "/tmp/service-owned-triage.db")

    manifest = module.build_manifest(REPO)

    assert manifest["git_sha"]
    assert len(manifest["artifact_sha256"]) == 64
    assert set(module.ARTIFACT_FILES) == set(manifest["files"])
    assert {
        "chat_intake.py",
        "bin/qa-healthcheck.py",
        "cron/qa-healthcheck.sh",
        "cron/claude-oauth-heartbeat.sh",
        "cron/heartbeat_state.py",
    } <= set(manifest["files"])
    serialized = str(manifest)
    assert "must-not-appear" not in serialized
    assert manifest["runtime"]["TRIAGE_DB_FILE"] == "/tmp/service-owned-triage.db"

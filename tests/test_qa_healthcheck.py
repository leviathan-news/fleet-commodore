"""Contract tests for the non-network QA readiness check."""
import importlib.util
import os
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
HEALTH = REPO / "bin" / "qa-healthcheck.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("qa_healthcheck", HEALTH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_readiness_report_names_each_missing_dependency(monkeypatch, tmp_path):
    mod = _load_module()
    monkeypatch.setattr(mod, "HOST_CLAUDE_DIR", tmp_path / "claude")
    monkeypatch.setattr(mod, "HOST_CLAUDE_CONFIG", tmp_path / "claude.json")
    monkeypatch.setattr(mod, "DB_URL_FILE", tmp_path / "db_url")
    monkeypatch.setattr(mod, "_docker_ok", lambda *_args: False)
    monkeypatch.setattr(mod, "_container_running", lambda _name: False)

    report = mod.readiness_report(quick=True)

    assert report["ok"] is False
    assert report["failed"] == [
        "claude_config",
        "claude_credentials",
        "db_url_source",
        "qa_db_tunnel",
        "qa_egress_proxy",
        "reviewer_image",
    ]


def test_readiness_report_is_explicit_about_healthy_dependencies(monkeypatch, tmp_path):
    mod = _load_module()
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text("credentials")
    config = tmp_path / "claude.json"
    config.write_text("config")
    db_url = tmp_path / "db_url"
    db_url.write_text("postgresql://reader@localhost/db")
    monkeypatch.setattr(mod, "HOST_CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(mod, "HOST_CLAUDE_CONFIG", config)
    monkeypatch.setattr(mod, "DB_URL_FILE", db_url)
    monkeypatch.setattr(mod, "_docker_ok", lambda *_args: True)
    monkeypatch.setattr(mod, "_container_running", lambda _name: True)

    report = mod.readiness_report(quick=True)

    assert report["ok"] is True
    assert report["failed"] == []
    assert report["warnings"] == []
    assert report["provider_transport"] == "not_checked"

    # An old file is suspicious, not proof of revocation. It must remain a
    # visible warning without blocking the daemon from issuing outage replies.
    old = time.time() - 8 * 24 * 3600
    os.utime(claude_dir / ".credentials.json", (old, old))
    report = mod.readiness_report(quick=True)
    assert report["ok"] is True
    assert report["warnings"] == ["claude_credentials_older_than_7_days"]
    assert report["claude_credentials_age_seconds"] >= 8 * 24 * 3600
    assert report["provider_transport"] == "not_checked"

"""Shared Codex transport health for host chat and bounded QA workers."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time

from codex_provider import generate_via_codex


def _connect():
    state = Path(os.environ.get(
        "FLEET_COMMODORE_STATE_DIR", "~/.local/state/fleet-commodore"
    )).expanduser()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(state / "provider-health.db", timeout=2)
    conn.execute("CREATE TABLE IF NOT EXISTS health (provider TEXT PRIMARY KEY, "
                 "unavailable_until REAL NOT NULL, updated REAL NOT NULL, reason TEXT NOT NULL)")
    return conn


def ask(prompt: str, *, model: str = "gpt-5.6-luna", timeout: int = 60,
        instruction: str = "Return the requested final text.", failure_context=None):
    context = failure_context if failure_context is not None else {}
    try:
        with closing(_connect()) as conn, conn:
            row = conn.execute("SELECT unavailable_until, reason FROM health WHERE provider='codex'").fetchone()
        if row and row[0] > time.time():
            context.update(failure_class=row[1], cooldown_active=True)
            return None
    except (OSError, sqlite3.Error):
        context["failure_class"] = "provider_health_unavailable"
        return None
    response = generate_via_codex(
        prompt, "Answer accurately using only supplied evidence. Never invent tool use or live facts.",
        model=model, timeout_seconds=timeout, response_instruction=instruction,
        failure_context=context,
    )
    reason = context.get("failure_class", "provider_unavailable") if response is None else "ok"
    cooldown = 600 if reason in {"provider_auth_failed", "provider_rate_limited"} else 60
    try:
        with closing(_connect()) as conn, conn:
            conn.execute("INSERT INTO health VALUES ('codex', ?, ?, ?) "
                         "ON CONFLICT(provider) DO UPDATE SET unavailable_until=excluded.unavailable_until, "
                         "updated=excluded.updated, reason=excluded.reason",
                         (time.time() + cooldown if response is None else 0, time.time(), reason))
    except (OSError, sqlite3.Error):
        context["health_write_failed"] = True
    return response

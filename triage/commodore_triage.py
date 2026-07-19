#!/usr/bin/env python3
"""Read-only control plane for coalesced Lev Sec alert triage.

This intentionally does not import or hook the long-polling loop.  It is safe to
run as a one-shot process from the future poll hook or the independent failsafe
cron.  The only command given to Sonnet is the Mini's read-only ``sec_feed``
wrapper; Telegram is the only side effect, and is disabled unless the explicit
posting flag is set.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT_DIR = Path(__file__).resolve().parent.parent
TRIAGE_DIR = Path(__file__).resolve().parent
RUNBOOK_FILE = TRIAGE_DIR / "RUNBOOK.md"
DB_FILE = Path(os.environ.get("TRIAGE_DB_FILE", TRIAGE_DIR / "triage.db"))
SEC_FEED_BIN = os.environ.get("SEC_FEED_BIN", "sec_feed")
CLAUDE_BIN = os.environ.get(
    "CLAUDE_BIN", str(Path("~/.local/bin/claude").expanduser())
)
LEV_SEC_CHAT_ID = int(os.environ.get("LEV_SEC_CHAT_ID", "-5363468256"))
COALESCE_WINDOW_S = int(os.environ.get("COALESCE_WINDOW_S", "90"))
COALESCE_MAX_WAIT_S = int(os.environ.get("COALESCE_MAX_WAIT_S", "180"))
FLOOD_MAX = int(os.environ.get("FLOOD_MAX", "20"))
FLOOD_WINDOW_S = int(os.environ.get("FLOOD_WINDOW_S", "600"))
FLOOD_COOLDOWN_S = int(os.environ.get("FLOOD_COOLDOWN_S", "1800"))
CLAUDE_TIMEOUT_S = int(os.environ.get("TRIAGE_CLAUDE_TIMEOUT_S", "180"))
CLAUDE_LIMIT_COOLDOWN_S = int(
    os.environ.get("CLAUDE_LIMIT_COOLDOWN", str(6 * 60 * 60))
)
CLAUDE_PROBE_INTERVAL_S = int(os.environ.get("CLAUDE_PROBE_INTERVAL_S", "600"))
CLAUDE_MAX_FAILURES = 3
CLAIM_STALE_AFTER_S = int(os.environ.get("TRIAGE_CLAIM_STALE_AFTER_S", "900"))
INITIAL_LOOKBACK_S = int(os.environ.get("TRIAGE_INITIAL_LOOKBACK_S", "600"))
CLAUDE_ALLOWED_TOOLS = "Bash(sec_feed:*),Read"

LOG = logging.getLogger("commodore_triage")
_VERDICT_RE = re.compile(r"(?m)^VERDICT:\s*(benign|needs_human)\s*$")
_ALLOWED_HTML_TAG_RE = re.compile(r"</?(?:b|code)>")
_ANY_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")


class TriageError(RuntimeError):
    """Expected local, feed, or model failure; callers leave claims retryable."""


def posting_enabled() -> bool:
    """Live messages are opt-in.  Absence of the env var is always safe."""
    return os.environ.get("TRIAGE_POSTING_ENABLED", "0") == "1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_alert_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError("alert id must be a UUID") from exc


def _connect(db_file: Path = DB_FILE) -> sqlite3.Connection:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create a private ledger; never share Commodore's live-bot database."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS triaged_alerts (
            alert_id TEXT PRIMARY KEY,
            triaged_at TEXT,
            verdict TEXT,
            message_id INTEGER,
            claimed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pending (
            alert_id TEXT PRIMARY KEY,
            enqueued_at REAL NOT NULL,
            summary TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alert_arrivals (
            alert_id TEXT PRIMARY KEY,
            arrived_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS triage_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL
        );
        """
    )
    # A local pre-release ledger may exist with only the four documented
    # completion columns.  Add the claim column without disturbing rows.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(triaged_alerts)")}
    if "claimed_at" not in columns:
        conn.execute("ALTER TABLE triaged_alerts ADD COLUMN claimed_at TEXT")


def _get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT state_value FROM triage_state WHERE state_key=?", (key,)
    ).fetchone()
    return row[0] if row else None


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO triage_state(state_key, state_value) VALUES (?, ?) "
        "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value",
        (key, value),
    )


def _clear_stale_claims(conn: sqlite3.Connection) -> None:
    """Return interrupted claims to the pending buffer before unlocking them.

    A process can die after atomically claiming a batch but before its failure
    handler can requeue it. Deleting that claim outright would lose a real
    Lev Sec delivery once the scan watermark has moved on, so recover it with
    a UUID-only summary (which is enriched from the read-only feed later).
    """
    cutoff = (_utc_now() - timedelta(seconds=CLAIM_STALE_AFTER_S)).isoformat()
    stale = conn.execute(
        "SELECT alert_id FROM triaged_alerts "
        "WHERE triaged_at IS NULL AND claimed_at IS NOT NULL AND claimed_at < ?",
        (cutoff,),
    ).fetchall()
    if stale:
        now = time.time()
        conn.executemany(
            "INSERT OR IGNORE INTO pending(alert_id, enqueued_at, summary) VALUES (?, ?, ?)",
            [(row["alert_id"], now, f"alert {row['alert_id']}") for row in stale],
        )
    conn.execute(
        "DELETE FROM triaged_alerts "
        "WHERE triaged_at IS NULL AND claimed_at IS NOT NULL AND claimed_at < ?",
        (cutoff,),
    )


def _one_line_summary(alert: dict[str, Any]) -> str:
    signal = str(alert.get("signal") or "unknown_signal")
    severity = str(alert.get("severity") or "unknown")
    source = str(alert.get("source_ip") or alert.get("source_label") or "no source")
    created = str(alert.get("created_at") or "unknown time")
    return f"{signal} ({severity}), source={source}, created={created}"


def enqueue_alert(
    alert_id: str, summary: str | None = None, *, db_file: Path = DB_FILE
) -> bool:
    """Put an unfinished alert into the coalescing buffer exactly once."""
    with _connect(db_file) as conn:
        _clear_stale_claims(conn)
        completed = conn.execute(
            "SELECT triaged_at FROM triaged_alerts WHERE alert_id=?", (alert_id,)
        ).fetchone()
        if completed and completed[0]:
            return False
        cursor = conn.execute(
            "INSERT OR IGNORE INTO pending(alert_id, enqueued_at, summary) VALUES (?, ?, ?)",
            (alert_id, time.time(), summary or f"alert {alert_id}"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO alert_arrivals(alert_id, arrived_at) VALUES (?, ?)",
            (alert_id, time.time()),
        )
    return bool(cursor.rowcount)


def _scan_feed_json(args: list[str]) -> Any:
    """Run the operator-installed read-only feed wrapper without a shell."""
    try:
        result = subprocess.run(
            [SEC_FEED_BIN, *args], capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TriageError(f"sec_feed unavailable: {exc}") from exc
    if result.returncode != 0:
        raise TriageError((result.stderr or result.stdout or "sec_feed failed")[:500])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TriageError("sec_feed returned invalid JSON") from exc


def _feed_alert_summary(alert_id: str) -> str:
    """Supply the agent's batch context even when the trigger had only a UUID."""
    payload = _scan_feed_json(["--alert", alert_id])
    if isinstance(payload, dict):
        alert = payload.get("alert", payload)
    else:
        alert = None
    if not isinstance(alert, dict):
        raise TriageError(f"sec_feed returned no alert object for {alert_id}")
    delivery = alert.get("levsec_delivery")
    if not (
        isinstance(delivery, dict)
        and delivery.get("delivered") is True
        and delivery.get("status") == "accepted"
    ):
        raise TriageError(
            f"alert {alert_id} was not accepted for Lev Sec delivery; refusing triage"
        )
    return _one_line_summary(alert)


def _enrich_alert_summaries(alerts: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """Fill the fast path's UUID-only placeholders from the read-only feed."""
    enriched: list[dict[str, str]] = []
    for alert in alerts:
        summary = alert["summary"]
        if summary == f"alert {alert['alert_id']}":
            summary = _feed_alert_summary(alert["alert_id"])
        enriched.append({"alert_id": alert["alert_id"], "summary": summary})
    return enriched


def scan_db(*, db_file: Path = DB_FILE) -> int:
    """Queue accepted Lev Sec deliveries since the durable watermark."""
    with _connect(db_file) as conn:
        watermark = _get_state(conn, "scan_watermark")
        if not watermark:
            watermark = (_utc_now() - timedelta(seconds=INITIAL_LOOKBACK_S)).isoformat()

    payload = _scan_feed_json(["--levsec-deliveries-since", watermark])
    if isinstance(payload, dict):
        deliveries = payload.get("deliveries", payload.get("results", []))
    else:
        deliveries = payload
    if not isinstance(deliveries, list):
        raise TriageError("sec_feed deliveries payload is not a list")

    newest = watermark
    queued = 0
    for delivery in deliveries:
        if not isinstance(delivery, dict):
            raise TriageError("sec_feed delivery was not an object")
        raw_alert_id = delivery.get("alert_id")
        try:
            alert_id = _validate_alert_id(str(raw_alert_id))
        except argparse.ArgumentTypeError as exc:
            raise TriageError(f"feed returned invalid alert id: {raw_alert_id!r}") from exc
        created_at = str(delivery.get("created_at") or watermark)
        # Keep the original ISO text for the feed command, but only advance
        # a parseable watermark.  A malformed date must not make us skip data.
        try:
            if _parse_iso(created_at) > _parse_iso(newest):
                newest = created_at
        except (TypeError, ValueError):
            LOG.warning("Ignoring malformed delivery timestamp for %s: %r", alert_id, created_at)
        if enqueue_alert(alert_id, _one_line_summary(delivery), db_file=db_file):
            queued += 1

    with _connect(db_file) as conn:
        _set_state(conn, "scan_watermark", newest)
    return queued


def _flood_line(count: int) -> str:
    minutes = max(1, FLOOD_WINDOW_S // 60)
    return (
        f"🚨 swarm detected, {count} alerts in {minutes} min — standing down, "
        "operators have the con"
    )


def _take_ready_batch(
    *, db_file: Path = DB_FILE
) -> tuple[str, list[dict[str, str]] | str | None]:
    """Atomically remove one mature batch from pending, or report why not."""
    conn = _connect(db_file)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _clear_stale_claims(conn)
        now = time.time()
        cooldown = _get_state(conn, "flood_cooldown_until")
        if cooldown and float(cooldown) > now:
            conn.execute("COMMIT")
            return "cooldown", None

        last = conn.execute("SELECT MAX(enqueued_at) FROM pending").fetchone()[0]
        if last is None:
            conn.execute("COMMIT")
            return "empty", None
        if now - float(last) < COALESCE_WINDOW_S:
            conn.execute("COMMIT")
            return "coalescing", None

        count = conn.execute(
            "SELECT COUNT(*) FROM alert_arrivals WHERE arrived_at >= ?",
            (now - FLOOD_WINDOW_S,),
        ).fetchone()[0]
        if count > FLOOD_MAX:
            _set_state(conn, "flood_cooldown_until", str(now + FLOOD_COOLDOWN_S))
            conn.execute("COMMIT")
            return "flood", _flood_line(int(count))

        rows = conn.execute(
            "SELECT alert_id, summary FROM pending ORDER BY enqueued_at, alert_id"
        ).fetchall()
        conn.executemany("DELETE FROM pending WHERE alert_id=?", [(row["alert_id"],) for row in rows])
        conn.execute("COMMIT")
        return "batch", [dict(row) for row in rows]
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _claim_alerts(
    alerts: Iterable[dict[str, str]], *, db_file: Path = DB_FILE
) -> list[dict[str, str]]:
    """Atomic INSERT OR IGNORE claims prevent scanner and message races."""
    claimed: list[dict[str, str]] = []
    conn = _connect(db_file)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _clear_stale_claims(conn)
        claimed_at = _iso_now()
        for alert in alerts:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO triaged_alerts(alert_id, claimed_at) VALUES (?, ?)",
                (alert["alert_id"], claimed_at),
            )
            if cursor.rowcount:
                claimed.append(alert)
        conn.execute("COMMIT")
        return claimed
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _release_claims(alerts: Iterable[dict[str, str]], *, db_file: Path = DB_FILE) -> None:
    ids = [(alert["alert_id"],) for alert in alerts]
    if not ids:
        return
    with _connect(db_file) as conn:
        conn.executemany(
            "DELETE FROM triaged_alerts WHERE alert_id=? AND triaged_at IS NULL", ids
        )


def _requeue(alerts: Iterable[dict[str, str]], *, db_file: Path = DB_FILE) -> None:
    now = time.time()
    with _connect(db_file) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO pending(alert_id, enqueued_at, summary) VALUES (?, ?, ?)",
            [(alert["alert_id"], now, alert["summary"]) for alert in alerts],
        )


def _complete_claims(
    alerts: Iterable[dict[str, str]], verdict: str, message_id: int | None,
    *, db_file: Path = DB_FILE,
) -> None:
    ids = [( _iso_now(), verdict, message_id, alert["alert_id"]) for alert in alerts]
    with _connect(db_file) as conn:
        conn.executemany(
            "UPDATE triaged_alerts SET triaged_at=?, verdict=?, message_id=? "
            "WHERE alert_id=? AND triaged_at IS NULL",
            ids,
        )


def _build_provider_env(bin_path: str) -> dict[str, str]:
    """Same PATH hardening as Commodore's Claude CLI wrapper."""
    claude_parent = str(Path(bin_path).expanduser().parent)
    tool_paths = [claude_parent, str(Path("~/bin").expanduser())]
    feed_path = Path(SEC_FEED_BIN).expanduser()
    if feed_path.is_absolute():
        tool_paths.append(str(feed_path.parent))
    return {
        **os.environ,
        "PATH": f"{':'.join(tool_paths)}:{os.environ.get('PATH', '')}",
    }


def _looks_like_claude_limit_error(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return any(
        phrase in combined for phrase in (
            "status code 501", "http 501", "error 501", "usage limit",
            "monthly usage", "quota", "credit balance", "rate limit",
            "too many requests", "exhausted", "payment required", "billing",
            "overloaded", "hit your limit",
        )
    )


def _claude_available(*, db_file: Path = DB_FILE) -> bool:
    with _connect(db_file) as conn:
        unavailable_until = _get_state(conn, "claude_unavailable_until")
        last_probe_at = float(_get_state(conn, "claude_last_probe_at") or "0")
    if not unavailable_until or float(unavailable_until) <= time.time():
        return True
    if time.time() - last_probe_at < CLAUDE_PROBE_INTERVAL_S:
        return False

    # Match Commodore's self-healing breaker: a successful low-cost probe
    # clears an early-recovered OAuth/quota outage without waiting six hours.
    with _connect(db_file) as conn:
        _set_state(conn, "claude_last_probe_at", str(time.time()))
    try:
        probe = subprocess.run(
            [CLAUDE_BIN, "-p", "-", "--allowedTools", CLAUDE_ALLOWED_TOOLS],
            input="ok", capture_output=True, text=True,
            timeout=15, env=_build_provider_env(CLAUDE_BIN), cwd=str(ROOT_DIR),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    stdout = (probe.stdout or "").strip()
    stderr = (probe.stderr or "").strip()
    healthy = (
        probe.returncode == 0
        and bool(stdout)
        and not stdout.startswith("Error:")
        and not _looks_like_claude_limit_error(stdout, stderr)
        and "failed to authenticate" not in f"{stdout}\n{stderr}".lower()
    )
    if healthy:
        _record_claude_success(db_file=db_file)
    return healthy


def _record_claude_failure(
    reason: str, *, limit_error: bool, db_file: Path = DB_FILE
) -> None:
    with _connect(db_file) as conn:
        failures = int(_get_state(conn, "claude_failures") or "0") + 1
        _set_state(conn, "claude_failures", str(failures))
        if limit_error or failures >= CLAUDE_MAX_FAILURES:
            _set_state(
                conn, "claude_unavailable_until",
                str(time.time() + CLAUDE_LIMIT_COOLDOWN_S),
            )
    LOG.warning("Claude triage call failed: %s", reason[:300])


def _record_claude_success(*, db_file: Path = DB_FILE) -> None:
    with _connect(db_file) as conn:
        _set_state(conn, "claude_failures", "0")
        _set_state(conn, "claude_unavailable_until", "0")


def _build_prompt(alerts: Iterable[dict[str, str]]) -> str:
    try:
        runbook = RUNBOOK_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise TriageError(f"missing triage runbook: {exc}") from exc
    batch = "\n".join(
        f"- {alert['alert_id']}: {alert['summary']}" for alert in alerts
    )
    return (
        f"{runbook}\n\n"
        "## Assigned batch\n"
        f"{batch}\n\n"
        "Use only `sec_feed --alert <uuid>` for further investigation. Return only the "
        "final Telegram HTML note (under 3,600 characters; `<b>` and `<code>` tags "
        "only) followed by exactly one final `VERDICT: benign` or `VERDICT: needs_human` "
        "line. Do not include analysis, tool output, or a preamble.\n"
    )


def ask_claude(prompt: str, *, db_file: Path = DB_FILE) -> str:
    """Sonnet invocation with the triage-specific, read-only tool leash."""
    if not _claude_available(db_file=db_file):
        raise TriageError("Claude circuit breaker is in cooldown")
    try:
        result = subprocess.run(
            [
                CLAUDE_BIN, "-p", "-", "--model", "sonnet",
                "--allowedTools", CLAUDE_ALLOWED_TOOLS,
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_S,
            env=_build_provider_env(CLAUDE_BIN),
            cwd=str(ROOT_DIR),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _record_claude_failure(str(exc), limit_error=False, db_file=db_file)
        raise TriageError(f"Claude unavailable: {exc}") from exc

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0 or not stdout or stdout.startswith("Error:"):
        limit_error = _looks_like_claude_limit_error(stdout, stderr)
        _record_claude_failure(stdout or stderr or "empty response", limit_error=limit_error,
                               db_file=db_file)
        raise TriageError("Claude returned no usable triage note")
    _record_claude_success(db_file=db_file)
    return stdout


def parse_note(output: str) -> tuple[str, str] | None:
    """Accept only a bounded note with one terminal machine-readable verdict."""
    cleaned = output.strip()
    matches = list(_VERDICT_RE.finditer(cleaned))
    if len(matches) != 1 or matches[0].end() != len(cleaned):
        return None
    verdict_match = matches[0]
    verdict = verdict_match.group(1)
    note = cleaned[:verdict_match.start()].strip()
    if note.startswith("NOTE:"):
        note = note[5:].strip()
    note = f"{note}\n\nVERDICT: {verdict}"
    if not note or len(note) > 3800:
        return None
    # Telegram's HTML mode has a small allowlist; do not let a malformed model
    # result turn a triage into a literal-tag or parse-mode failure.
    without_allowed = _ALLOWED_HTML_TAG_RE.sub("", note)
    if _ANY_HTML_TAG_RE.search(without_allowed):
        return None
    stack: list[str] = []
    for tag in _ALLOWED_HTML_TAG_RE.findall(note):
        closing = tag.startswith("</")
        name = tag.strip("</>")
        if closing:
            if not stack or stack.pop() != name:
                return None
        else:
            stack.append(name)
    if stack:
        return None
    return note, verdict


def _html_note_to_markdown(note: str) -> str:
    """Use Commodore's battle-tested sender while preserving the allowed tags."""
    converted = re.sub(r"<b>(.*?)</b>", r"**\1**", note, flags=re.DOTALL)
    return re.sub(r"<code>(.*?)</code>", r"`\1`", converted, flags=re.DOTALL)


def send_message(chat_id: int, note: str) -> Any:
    """Lazy import keeps dry-runs/feed scans independent of bot credentials."""
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    import commodore  # Imported only on the explicit live-post path.

    return commodore.send_message(chat_id, _html_note_to_markdown(note))


def _operator_dm_user_id() -> int:
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    import commodore

    return int(commodore._operator_dm_user_id() or 0)


def _message_id(response: Any) -> int | None:
    if isinstance(response, dict):
        result = response.get("result")
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return result["message_id"]
    return None


def _make_needs_human_loud(note: str) -> str:
    first_chunk = note[:250].lower()
    if "needs human" in first_chunk or "operator" in first_chunk:
        return note
    return "<b>⚠️ NEEDS HUMAN — operator attention required</b>\n\n" + note


def _post_note(note: str, verdict: str) -> int | None:
    if verdict == "needs_human":
        note = _make_needs_human_loud(note)
    response = send_message(LEV_SEC_CHAT_ID, note)
    message_id = _message_id(response)
    if verdict == "needs_human":
        operator_id = _operator_dm_user_id()
        if operator_id:
            try:
                send_message(
                    operator_id,
                    "⚠️ Lev Sec triage needs human attention:\n\n" + note,
                )
            except Exception as exc:  # Group post is already durable; do not duplicate it.
                LOG.exception("Unable to DM operator after needs_human post: %s", exc)
    return message_id


def process_pending(*, dry_run: bool, db_file: Path = DB_FILE) -> str:
    """Process one mature coalesced batch, retaining retryability on every failure."""
    if not dry_run and not posting_enabled():
        return "posting_disabled"

    status, payload = _take_ready_batch(db_file=db_file)
    if status in {"empty", "coalescing", "cooldown"}:
        return status
    if status == "flood":
        assert isinstance(payload, str)
        if dry_run:
            print(payload)
            return "flood_dry_run"
        try:
            send_message(LEV_SEC_CHAT_ID, payload)
        except Exception as exc:
            LOG.exception("Unable to post flood breaker notice: %s", exc)
            return "flood_post_failed"
        return "flood"

    assert status == "batch" and isinstance(payload, list)
    claimed = _claim_alerts(payload, db_file=db_file)
    if not claimed:
        return "already_claimed"
    try:
        enriched = _enrich_alert_summaries(claimed)
        parsed = parse_note(ask_claude(_build_prompt(enriched), db_file=db_file))
        if not parsed:
            raise TriageError("Claude note failed the NOTE/VERDICT contract")
        note, verdict = parsed
        if dry_run:
            print(note)
            _release_claims(claimed, db_file=db_file)
            _requeue(claimed, db_file=db_file)
            return "dry_run"
        message_id = _post_note(note, verdict)
        _complete_claims(claimed, verdict, message_id, db_file=db_file)
        return "posted"
    except Exception as exc:
        LOG.warning("Triage batch left retryable: %s", exc)
        _release_claims(claimed, db_file=db_file)
        _requeue(claimed, db_file=db_file)
        return "retryable_failure"


def wait_until_quiet(*, db_file: Path = DB_FILE) -> None:
    """Fast-path subprocesses wait off the poll loop for a quiet batch boundary."""
    deadline = time.monotonic() + max(COALESCE_WINDOW_S, COALESCE_MAX_WAIT_S)
    while True:
        with _connect(db_file) as conn:
            last = conn.execute("SELECT MAX(enqueued_at) FROM pending").fetchone()[0]
        if last is None or time.time() - float(last) >= COALESCE_WINDOW_S:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        quiet_for = max(0.0, COALESCE_WINDOW_S - (time.time() - float(last)))
        time.sleep(min(5.0, quiet_for, remaining))


def rearm_flood_breaker(*, db_file: Path = DB_FILE) -> None:
    with _connect(db_file) as conn:
        _set_state(conn, "flood_cooldown_until", "0")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--alert-id", type=_validate_alert_id)
    mode.add_argument("--scan-db", action="store_true")
    mode.add_argument("--rearm", action="store_true", help="clear flood cooldown only")
    parser.add_argument("--dry-run", action="store_true", help="print a valid note; never post or claim")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("TRIAGE_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    args = _parser().parse_args(argv)
    try:
        if args.rearm:
            rearm_flood_breaker()
            print("flood breaker re-armed")
            return 0
        if args.alert_id:
            enqueue_alert(args.alert_id)
            # With the flag at its required safe default, do not burn a Sonnet
            # call or create a claim.  --dry-run remains the explicit test path.
            if not args.dry_run and not posting_enabled():
                print("posting disabled; alert queued without triage")
                return 0
            wait_until_quiet()
        else:
            queued = scan_db()
            LOG.info("scan queued %d accepted Lev Sec deliveries", queued)
        result = process_pending(dry_run=args.dry_run)
        LOG.info("triage result=%s", result)
        return 0 if result != "retryable_failure" else 1
    except TriageError as exc:
        LOG.error("triage unavailable: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

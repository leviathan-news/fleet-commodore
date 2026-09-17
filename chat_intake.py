"""Durable, deliberately small storage boundary for asynchronous chat intake."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any, Callable


class IntakeError(RuntimeError):
    """Base error for an invalid intake operation."""


class IntakeFull(IntakeError):
    """The bounded unresolved intake queue has no room for new updates."""


class ChatIntake:
    _MAX_UPDATE_ID = 9223372036854775806
    _STATUSES = ("queued", "running", "resolved", "escalated", "no_reply", "handed_off", "held_unknown")
    _UNRESOLVED = ("queued", "running", "handed_off", "held_unknown")
    _OUTCOMES = {"resolved", "escalated", "no_reply", "handed_off", "held_unknown"}

    def __init__(
        self,
        path: str | os.PathLike[str],
        clock: Callable[[], float] = time.time,
        max_pending: int = 4096,
    ) -> None:
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 0:
            raise ValueError("max_pending must be a nonnegative integer")
        self.path = Path(path).expanduser()
        self.clock = clock
        self.max_pending = max_pending
        parent = self.path.parent
        parent_was_present = parent.exists()
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_was_present:
            os.chmod(parent, 0o700)
        if self.path.is_symlink():
            raise ValueError("intake database path must not be a symlink")
        file_was_present = self.path.exists()
        if not file_was_present:
            fd = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(fd)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_intake_meta (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO chat_intake_meta(name, value) VALUES ('cursor', 0);
                CREATE TABLE IF NOT EXISTS chat_intake_event (
                    update_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','running','resolved','escalated','no_reply','handed_off','held_unknown')),
                    claim_token TEXT,
                    created_at REAL NOT NULL,
                    claimed_at REAL,
                    finished_at REAL
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_intake_event)")}
            for name, column_type in (("job_table", "TEXT"), ("job_uuid", "TEXT"), ("message_id", "INTEGER")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE chat_intake_event ADD COLUMN {name} {column_type}")
            conn.commit()
            conn.execute("INSERT OR IGNORE INTO chat_intake_meta(name,value) VALUES ('created_at',?)", (int(self.clock()),))
            conn.commit()
    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _payload(update: dict[str, Any]) -> str:
        try:
            payload = json.dumps(update, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("update must be JSON-serializable") from exc
        if len(payload.encode("utf-8")) > 256 * 1024:
            raise ValueError("update payload exceeds 256 KiB")
        return payload

    @staticmethod
    def _validate_updates(updates: Any) -> list[tuple[int, str]]:
        if not isinstance(updates, list):
            raise TypeError("updates must be a list")
        if len(updates) > 100:
            raise ValueError("at most 100 updates may be ingested at once")
        result: list[tuple[int, str]] = []
        for update in updates:
            if not isinstance(update, dict):
                raise TypeError("each update must be a dictionary")
            update_id = update.get("update_id")
            if (
                isinstance(update_id, bool)
                or not isinstance(update_id, int)
                or update_id < 0
                or update_id > ChatIntake._MAX_UPDATE_ID
            ):
                raise ValueError("update_id must be a nonnegative integer within SQLite's signed 64-bit range")
            result.append((update_id, ChatIntake._payload(update)))
        return result

    def ingest_batch(self, updates: list[dict[str, Any]]) -> None:
        entries = self._validate_updates(updates)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT value FROM chat_intake_meta WHERE name='cursor'").fetchone()
                cursor = int(row[0])
                ids = {row[0] for row in conn.execute(
                    "SELECT update_id FROM chat_intake_event WHERE update_id IN (%s)" %
                    (",".join("?" for _ in entries) or "NULL"),
                    tuple(update_id for update_id, _ in entries),
                )}
                pending = conn.execute(
                    "SELECT COUNT(*) FROM chat_intake_event WHERE status IN ('queued','running','handed_off','held_unknown')"
                ).fetchone()[0]
                # A pruned terminal row below the committed cursor must not
                # become fresh work if an old batch is presented again.
                new_ids = {update_id for update_id, _ in entries if update_id not in ids and update_id >= cursor}
                if pending + len(new_ids) > self.max_pending:
                    raise IntakeFull("chat intake pending limit reached")
                now = self.clock()
                for update_id, payload in entries:
                    if update_id < cursor and update_id not in ids:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO chat_intake_event "
                        "(update_id,payload,status,created_at) VALUES (?,?, 'queued', ?)",
                        (update_id, payload, now),
                    )
                if entries:
                    cursor = max(cursor, max(update_id for update_id, _ in entries) + 1)
                conn.execute("UPDATE chat_intake_meta SET value=? WHERE name='cursor'", (cursor,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def offset(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT value FROM chat_intake_meta WHERE name='cursor'").fetchone()[0])

    def note_poll(self, *, router_alive: bool) -> None:
        """Only after getUpdates and durable admission succeed; no timer renewal."""
        if not isinstance(router_alive, bool):
            raise ValueError("router health must be boolean")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("INSERT OR REPLACE INTO chat_intake_meta(name,value) VALUES (?,?)", (
                ("last_poll_at", int(self.clock())), ("router_alive", int(router_alive)),
            ))
            conn.commit()

    def claim_next(self) -> dict[str, Any] | None:
        now = self.clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT update_id, payload FROM chat_intake_event "
                    "WHERE status='queued' ORDER BY update_id LIMIT 1"
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                token = secrets.token_urlsafe(24)
                changed = conn.execute(
                    "UPDATE chat_intake_event SET status='running', claim_token=?, claimed_at=? "
                    "WHERE update_id=? AND status='queued'",
                    (token, now, row["update_id"]),
                ).rowcount
                if changed != 1:
                    conn.rollback()
                    return None
                conn.commit()
                return {"update_id": row["update_id"], "payload": json.loads(row["payload"]), "claim_token": token}
            except Exception:
                conn.rollback()
                raise

    def finish(self, update_id: int, token: str, outcome: str, *,
               job_table=None, job_uuid=None, message_id=None) -> None:
        if outcome not in self._OUTCOMES:
            raise ValueError(f"invalid intake outcome: {outcome}")
        now = self.clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status FROM chat_intake_event WHERE update_id=? AND claim_token=?",
                    (update_id, token),
                ).fetchone()
                if row is None or row["status"] != "running":
                    raise IntakeError("event claim is no longer current")
                conn.execute(
                    "UPDATE chat_intake_event SET job_table=?, job_uuid=?, message_id=? WHERE update_id=?",
                    (job_table, job_uuid, message_id, update_id),
                )
                payload = "{}" if outcome in {"resolved", "escalated", "no_reply"} else None
                if payload is None:
                    conn.execute(
                        "UPDATE chat_intake_event SET status=?, finished_at=?, claim_token=NULL WHERE update_id=?",
                        (outcome, now, update_id),
                    )
                else:
                    conn.execute(
                        "UPDATE chat_intake_event SET status=?, payload=?, finished_at=?, claim_token=NULL WHERE update_id=?",
                        (outcome, payload, now, update_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def hold_interrupted(self) -> int:
        """Call only after acquiring sole process ownership, never on a timer."""
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE chat_intake_event SET status='held_unknown', claim_token=NULL, finished_at=? "
                "WHERE status='running'", (self.clock(),),
            ).rowcount
            conn.commit()
            return changed

    def prune_terminal(self, retention_seconds=30 * 86400, limit=1000) -> int:
        """Bound metadata retention without ever removing unresolved work."""
        if retention_seconds < 86400 or not 1 <= limit <= 1000:
            raise ValueError("terminal retention must be bounded and at least one day")
        with self._connect() as conn:
            changed = conn.execute(
                "DELETE FROM chat_intake_event WHERE update_id IN ("
                "SELECT update_id FROM chat_intake_event "
                "WHERE status IN ('resolved','escalated','no_reply') AND finished_at<? "
                "ORDER BY update_id LIMIT ?)", (self.clock() - retention_seconds, limit),
            ).rowcount
            conn.commit()
            return changed

    def handoffs(self, limit=100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("handoff limit must be between 1 and 100")
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT update_id, job_table, job_uuid FROM chat_intake_event "
                "WHERE status='handed_off' ORDER BY update_id LIMIT ?", (limit,),
            )]

    def complete_handoff(self, update_id, job_table, job_uuid, outcome, message_id) -> bool:
        if outcome not in {"resolved", "escalated"}:
            raise ValueError("handoff completion must be receipt-backed")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise ValueError("handoff completion requires a positive receipt")
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE chat_intake_event SET status=?, message_id=?, payload='{}', finished_at=? "
                "WHERE update_id=? AND status='handed_off' AND job_table=? AND job_uuid=?",
                (outcome, message_id, self.clock(), update_id, job_table, job_uuid),
            ).rowcount
            conn.commit()
            return changed == 1

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM chat_intake_event GROUP BY status"
            ).fetchall()
            counts = {status: 0 for status in self._STATUSES}
            counts.update({row["status"]: row["count"] for row in rows})
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM chat_intake_event WHERE status IN ('queued','running','handed_off','held_unknown')"
            ).fetchone()[0]
        return {**counts, "oldest_unresolved_age": None if oldest is None else max(0.0, now - oldest)}

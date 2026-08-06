#!/usr/bin/env python3
"""Durable single-writer control plane for Fleet/Sol Telegram handoffs.

The controller deliberately keeps Telegram intake, reply ownership, event
deduplication, and handoff evidence in one service-owned SQLite database.  It
does not reason about messages.  Fleet and Sol are replaceable actors which
must obtain authorization from this ledger before sending.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid


DEFAULT_DB = Path(
    os.environ.get(
        "HELM_CONTROLLER_DB_FILE",
        "~/.local/state/fleet-commodore/helm-controller/controller.db",
    )
).expanduser()


class HelmControllerError(RuntimeError):
    pass


class ReplyLeaseDenied(HelmControllerError):
    pass


class DuplicateSendHeld(HelmControllerError):
    pass


def _now() -> float:
    return time.time()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_bot_token() -> str:
    token = os.environ.get("BOT_TOKEN")
    if token:
        return token
    path = Path(
        os.environ.get("BOT_TOKEN_FILE", "/run/secrets/bot_token")
    ).expanduser()
    if not path.is_file():
        raise HelmControllerError("Telegram bot token source is unavailable")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise HelmControllerError("Telegram bot token source is empty")
    return token


def stable_event_id(update: dict) -> str:
    """Return the Telegram-stable identity for one update."""
    update_id = update.get("update_id")
    if isinstance(update_id, int):
        return f"telegram:update:{update_id}"
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if chat_id is not None and message_id is not None:
        return f"telegram:message:{chat_id}:{message_id}"
    digest = hashlib.sha256(_json(update).encode("utf-8")).hexdigest()
    return f"telegram:payload:{digest}"


class HelmController:
    def __init__(self, db_path: Path | str = DEFAULT_DB, *, clock=_now):
        self.db_path = Path(db_path).expanduser()
        self.clock = clock
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextlib.contextmanager
    def _write(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS controller_meta (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telegram_events (
                    event_id TEXT PRIMARY KEY,
                    update_id INTEGER UNIQUE,
                    chat_id INTEGER,
                    thread_id INTEGER,
                    message_id INTEGER,
                    sender_id INTEGER,
                    payload_json TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    routed_actor TEXT,
                    claim_token TEXT,
                    claim_expires_at REAL,
                    completed_at REAL,
                    outcome TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_helm_events_state
                    ON telegram_events(state, received_at);
                CREATE INDEX IF NOT EXISTS idx_helm_events_message
                    ON telegram_events(chat_id, message_id);

                CREATE TABLE IF NOT EXISTS conversation_context (
                    chat_id INTEGER NOT NULL,
                    thread_key INTEGER NOT NULL,
                    sender_key INTEGER NOT NULL,
                    context_json TEXT NOT NULL,
                    last_event_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(chat_id, thread_key, sender_key)
                );

                CREATE TABLE IF NOT EXISTS reply_lease (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    holder TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    token_hash TEXT,
                    sol_expires_at REAL,
                    watcher_expires_at REAL,
                    bridge_expires_at REAL,
                    reason TEXT,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS send_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    intent_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    telegram_message_id INTEGER,
                    error TEXT,
                    created_at REAL NOT NULL,
                    finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_helm_send_event
                    ON send_attempts(event_id, status);
                CREATE INDEX IF NOT EXISTS idx_helm_send_prepared
                    ON send_attempts(status, created_at);

                CREATE TABLE IF NOT EXISTS lease_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_holder TEXT NOT NULL,
                    to_holder TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS verification_receipts (
                    name TEXT PRIMARY KEY,
                    passed INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL,
                    observed_at REAL NOT NULL
                );
                """
            )
            now = self.clock()
            conn.execute(
                """INSERT OR IGNORE INTO reply_lease
                   (singleton, holder, generation, updated_at, reason)
                   VALUES (1, 'fleet', 1, ?, 'initial ordinary coverage')""",
                (now,),
            )
            conn.execute(
                """INSERT OR IGNORE INTO controller_meta(key, value_json, updated_at)
                   VALUES ('telegram_offset', '0', ?)""",
                (now,),
            )

    def _lease_row(self, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM reply_lease WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise HelmControllerError("reply lease is missing")
        return row

    def _token_matches(self, row: sqlite3.Row, token: str | None) -> bool:
        expected = row["token_hash"]
        return bool(expected and token and secrets.compare_digest(expected, _token_hash(token)))

    def _sol_health(self, row: sqlite3.Row, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        ages = {
            name: max(0.0, deadline - now) if deadline is not None else 0.0
            for name, deadline in (
                ("sol", row["sol_expires_at"]),
                ("watcher", row["watcher_expires_at"]),
                ("bridge", row["bridge_expires_at"]),
            )
        }
        healthy = row["holder"] == "sol" and all(value > 0 for value in ages.values())
        return {"healthy": healthy, "remaining_seconds": ages}

    def status(self) -> dict:
        with self._connect() as conn:
            row = self._lease_row(conn)
            counts = {
                item["state"]: item["count"]
                for item in conn.execute(
                    "SELECT state, COUNT(*) AS count FROM telegram_events GROUP BY state"
                )
            }
            offset = json.loads(
                conn.execute(
                    "SELECT value_json FROM controller_meta WHERE key='telegram_offset'"
                ).fetchone()[0]
            )
            tests = {
                item["name"]: bool(item["passed"])
                for item in conn.execute(
                    "SELECT name, passed FROM verification_receipts ORDER BY name"
                )
            }
            return {
                "holder": row["holder"],
                "generation": row["generation"],
                "reason": row["reason"],
                "telegram_offset": offset,
                "queue": counts,
                "sol_health": self._sol_health(row),
                "tests": tests,
            }

    def durable_offset(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM controller_meta WHERE key='telegram_offset'"
            ).fetchone()
            return int(json.loads(row[0])) if row else 0

    def set_meta(self, key: str, value) -> None:
        now = self.clock()
        with self._write() as conn:
            conn.execute(
                """INSERT INTO controller_meta(key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value_json=excluded.value_json,
                     updated_at=excluded.updated_at""",
                (key, _json(value), now),
            )

    def get_meta(self, key: str, default=None):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM controller_meta WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else default

    def enqueue_update(self, update: dict) -> dict:
        """Durably insert an update and advance its cursor in one transaction."""
        now = self.clock()
        event_id = stable_event_id(update)
        update_id = update.get("update_id")
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        thread_id = message.get("message_thread_id")
        message_id = message.get("message_id")
        sender_id = (message.get("from") or {}).get("id")
        with self._write() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO telegram_events
                   (event_id, update_id, chat_id, thread_id, message_id, sender_id,
                    payload_json, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    update_id if isinstance(update_id, int) else None,
                    chat_id,
                    thread_id,
                    message_id,
                    sender_id,
                    _json(update),
                    now,
                ),
            )
            inserted = cursor.rowcount == 1
            if isinstance(update_id, int):
                current_row = conn.execute(
                    "SELECT value_json FROM controller_meta WHERE key='telegram_offset'"
                ).fetchone()
                current = int(json.loads(current_row[0])) if current_row else 0
                new_offset = max(current, update_id + 1)
                conn.execute(
                    """INSERT INTO controller_meta(key, value_json, updated_at)
                       VALUES ('telegram_offset', ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value_json=excluded.value_json,
                         updated_at=excluded.updated_at""",
                    (_json(new_offset), now),
                )
            if inserted and chat_id is not None and message_id is not None:
                self._append_context_locked(
                    conn, chat_id, thread_id, sender_id, event_id, message, now
                )
        return {"event_id": event_id, "inserted": inserted}

    def _append_context_locked(
        self,
        conn: sqlite3.Connection,
        chat_id,
        thread_id,
        sender_id,
        event_id: str,
        message: dict,
        now: float,
    ) -> None:
        thread_key = int(thread_id or 0)
        sender_key = int(sender_id or 0)
        row = conn.execute(
            """SELECT context_json FROM conversation_context
               WHERE chat_id=? AND thread_key=? AND sender_key=?""",
            (chat_id, thread_key, sender_key),
        ).fetchone()
        context = json.loads(row[0]) if row else {"messages": []}
        context.setdefault("messages", []).append(
            {
                "event_id": event_id,
                "message_id": message.get("message_id"),
                "date": message.get("date"),
                "text": (message.get("text") or message.get("caption") or "")[:4000],
                "reply_to_message_id": (message.get("reply_to_message") or {}).get("message_id"),
            }
        )
        context["messages"] = context["messages"][-40:]
        conn.execute(
            """INSERT INTO conversation_context
               (chat_id, thread_key, sender_key, context_json, last_event_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, thread_key, sender_key) DO UPDATE SET
                 context_json=excluded.context_json,
                 last_event_id=excluded.last_event_id,
                 updated_at=excluded.updated_at""",
            (chat_id, thread_key, sender_key, _json(context), event_id, now),
        )

    def route_allowed(self, actor: str, event_id: str) -> bool:
        now = self.clock()
        with self._write() as conn:
            row = self._lease_row(conn)
            allowed = row["holder"] == actor
            if actor == "sol":
                allowed = allowed and self._sol_health(row, now)["healthy"]
            if not allowed:
                return False
            conn.execute(
                """UPDATE telegram_events
                   SET state='routed', routed_actor=?
                   WHERE event_id=? AND state='queued'""",
                (actor, event_id),
            )
            return True

    def heartbeat_watcher(self, ttl_seconds: int) -> bool:
        now = self.clock()
        with self._write() as conn:
            row = self._lease_row(conn)
            if row["holder"] != "sol":
                return False
            conn.execute(
                "UPDATE reply_lease SET watcher_expires_at=?, updated_at=? WHERE singleton=1",
                (now + ttl_seconds, now),
            )
            return True

    def acquire_sol(
        self,
        *,
        sol_ttl: int,
        watcher_ttl: int,
        bridge_ttl: int,
        reason: str,
    ) -> str:
        now = self.clock()
        token = secrets.token_urlsafe(32)
        with self._write() as conn:
            self._expire_stale_prepared_locked(conn, now)
            prepared = conn.execute(
                "SELECT COUNT(*) FROM send_attempts WHERE status='prepared'"
            ).fetchone()[0]
            if prepared:
                raise ReplyLeaseDenied("an actor send is still in flight")
            row = self._lease_row(conn)
            if row["holder"] != "fleet":
                raise ReplyLeaseDenied(f"cannot acquire Sol from holder={row['holder']}")
            generation = int(row["generation"]) + 1
            conn.execute(
                """UPDATE reply_lease SET
                   holder='sol', generation=?, token_hash=?, sol_expires_at=?,
                   watcher_expires_at=?, bridge_expires_at=?, reason=?, updated_at=?
                   WHERE singleton=1""",
                (
                    generation,
                    _token_hash(token),
                    now + sol_ttl,
                    now + watcher_ttl,
                    now + bridge_ttl,
                    reason,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO lease_transitions
                   (from_holder, to_holder, generation, reason, created_at)
                   VALUES ('fleet', 'sol', ?, ?, ?)""",
                (generation, reason, now),
            )
        return token

    def renew(self, kind: str, token: str, ttl_seconds: int) -> None:
        column = {
            "sol": "sol_expires_at",
            "watcher": "watcher_expires_at",
            "bridge": "bridge_expires_at",
        }.get(kind)
        if column is None:
            raise ValueError(f"unknown lease kind: {kind}")
        now = self.clock()
        with self._write() as conn:
            row = self._lease_row(conn)
            if row["holder"] != "sol" or not self._token_matches(row, token):
                raise ReplyLeaseDenied("Sol lease token is not current")
            conn.execute(
                f"UPDATE reply_lease SET {column}=?, updated_at=? WHERE singleton=1",
                (now + ttl_seconds, now),
            )

    def needs_failback(self) -> tuple[bool, str | None]:
        with self._connect() as conn:
            row = self._lease_row(conn)
            if row["holder"] != "sol":
                return False, None
            health = self._sol_health(row)
            if health["healthy"]:
                return False, None
            expired = [
                key for key, remaining in health["remaining_seconds"].items()
                if remaining <= 0
            ]
            return True, ",".join(expired) + " lease expired"

    def begin_transition(self, reason: str) -> int:
        now = self.clock()
        with self._write() as conn:
            row = self._lease_row(conn)
            generation = int(row["generation"]) + 1
            conn.execute(
                """UPDATE reply_lease SET holder='transition', generation=?,
                   token_hash=NULL, sol_expires_at=NULL, watcher_expires_at=NULL,
                   bridge_expires_at=NULL, reason=?, updated_at=? WHERE singleton=1""",
                (generation, reason, now),
            )
            conn.execute(
                """INSERT INTO lease_transitions
                   (from_holder, to_holder, generation, reason, created_at)
                   VALUES (?, 'transition', ?, ?, ?)""",
                (row["holder"], generation, reason, now),
            )
            return generation

    def complete_fleet(self, reason: str) -> None:
        now = self.clock()
        with self._write() as conn:
            row = self._lease_row(conn)
            conn.execute(
                """UPDATE reply_lease SET holder='fleet', token_hash=NULL,
                   sol_expires_at=NULL, watcher_expires_at=NULL,
                   bridge_expires_at=NULL, reason=?, updated_at=? WHERE singleton=1""",
                (reason, now),
            )
            conn.execute(
                """INSERT INTO lease_transitions
                   (from_holder, to_holder, generation, reason, created_at)
                   VALUES (?, 'fleet', ?, ?, ?)""",
                (row["holder"], row["generation"], reason, now),
            )

    def coverage_lost(self, reason: str) -> None:
        now = self.clock()
        with self._write() as conn:
            row = self._lease_row(conn)
            conn.execute(
                """UPDATE reply_lease SET holder='coverage_lost', reason=?,
                   token_hash=NULL, sol_expires_at=NULL, watcher_expires_at=NULL,
                   bridge_expires_at=NULL, updated_at=? WHERE singleton=1""",
                (reason, now),
            )
            conn.execute(
                """INSERT INTO lease_transitions
                   (from_holder, to_holder, generation, reason, created_at)
                   VALUES (?, 'coverage_lost', ?, ?, ?)""",
                (row["holder"], row["generation"], reason, now),
            )

    def _expire_stale_prepared_locked(self, conn, now: float, max_age: int = 120) -> None:
        conn.execute(
            """UPDATE send_attempts SET status='outcome_unknown',
               error='prepared send exceeded bounded receipt window', finished_at=?
               WHERE status='prepared' AND created_at <= ?""",
            (now, now - max_age),
        )

    def begin_send(
        self,
        *,
        actor: str,
        event_id: str,
        intent_hash: str,
        token: str | None = None,
    ) -> str:
        now = self.clock()
        attempt_id = str(uuid.uuid4())
        with self._write() as conn:
            self._expire_stale_prepared_locked(conn, now)
            row = self._lease_row(conn)
            allowed = row["holder"] == actor
            if actor == "sol":
                allowed = (
                    allowed
                    and self._token_matches(row, token)
                    and self._sol_health(row, now)["healthy"]
                )
            if not allowed:
                raise ReplyLeaseDenied(
                    f"reply lease holder is {row['holder']}, not {actor}"
                )
            prior = conn.execute(
                """SELECT status FROM send_attempts
                   WHERE event_id=? AND status IN ('accepted','outcome_unknown','prepared')
                   ORDER BY created_at DESC LIMIT 1""",
                (event_id,),
            ).fetchone()
            if prior:
                raise DuplicateSendHeld(
                    f"event {event_id} already has send status {prior['status']}"
                )
            conn.execute(
                """INSERT INTO send_attempts
                   (attempt_id, event_id, actor, generation, intent_hash, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'prepared', ?)""",
                (attempt_id, event_id, actor, row["generation"], intent_hash, now),
            )
        return attempt_id

    def finish_send(
        self,
        attempt_id: str,
        *,
        status: str,
        telegram_message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"accepted", "failed", "outcome_unknown"}:
            raise ValueError(f"invalid send outcome: {status}")
        now = self.clock()
        with self._write() as conn:
            row = conn.execute(
                "SELECT event_id FROM send_attempts WHERE attempt_id=? AND status='prepared'",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise HelmControllerError("send attempt is not prepared")
            conn.execute(
                """UPDATE send_attempts SET status=?, telegram_message_id=?,
                   error=?, finished_at=? WHERE attempt_id=?""",
                (status, telegram_message_id, (error or "")[:500], now, attempt_id),
            )
            if status == "accepted":
                conn.execute(
                    """UPDATE telegram_events SET state='completed', completed_at=?,
                       outcome='reply_accepted' WHERE event_id=?""",
                    (now, row["event_id"]),
                )
            elif status == "outcome_unknown":
                conn.execute(
                    """UPDATE telegram_events SET state='held_unknown', outcome=?
                       WHERE event_id=?""",
                    ((error or "outcome unknown")[:500], row["event_id"]),
                )

    def claim_next(self, token: str, *, claim_ttl: int = 300) -> dict | None:
        now = self.clock()
        with self._write() as conn:
            row = self._lease_row(conn)
            if not (
                row["holder"] == "sol"
                and self._token_matches(row, token)
                and self._sol_health(row, now)["healthy"]
            ):
                raise ReplyLeaseDenied("Sol cannot claim without three healthy leases")
            event = conn.execute(
                """SELECT * FROM telegram_events
                   WHERE state='queued'
                      OR (state='claimed_sol' AND claim_expires_at <= ?)
                   ORDER BY received_at, event_id LIMIT 1""",
                (now,),
            ).fetchone()
            if event is None:
                return None
            claim_token = secrets.token_urlsafe(18)
            conn.execute(
                """UPDATE telegram_events SET state='claimed_sol', routed_actor='sol',
                   claim_token=?, claim_expires_at=? WHERE event_id=?""",
                (claim_token, now + claim_ttl, event["event_id"]),
            )
            payload = dict(event)
            payload["payload"] = json.loads(payload.pop("payload_json"))
            payload["claim_token"] = claim_token
            return payload

    def mark_no_reply(self, event_id: str, claim_token: str, outcome: str) -> None:
        now = self.clock()
        with self._write() as conn:
            cursor = conn.execute(
                """UPDATE telegram_events SET state='completed', completed_at=?, outcome=?
                   WHERE event_id=? AND state='claimed_sol' AND claim_token=?""",
                (now, outcome[:500], event_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise HelmControllerError("event claim is no longer current")

    def reconcile_fleet_history(self, commodore_db: Path | str) -> int:
        """Prevent second-takeover duplicates for events routed by legacy Fleet."""
        path = Path(commodore_db).expanduser()
        if not path.exists():
            return 0
        legacy = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        legacy.row_factory = sqlite3.Row
        try:
            rows = legacy.execute(
                "SELECT chat_id, msg_id, our_reply FROM chat_history"
            ).fetchall()
        finally:
            legacy.close()
        now = self.clock()
        changed = 0
        with self._write() as conn:
            for row in rows:
                cursor = conn.execute(
                    """UPDATE telegram_events SET state='completed', completed_at=?,
                       routed_actor='fleet', outcome=?
                       WHERE chat_id=? AND message_id=?
                         AND state IN ('queued','claimed_sol')""",
                    (
                        now,
                        "fleet_failback_replied" if row["our_reply"] else "fleet_failback_routed",
                        row["chat_id"],
                        row["msg_id"],
                    ),
                )
                changed += cursor.rowcount
        return changed

    def record_verification(self, name: str, passed: bool, evidence: dict) -> None:
        now = self.clock()
        with self._write() as conn:
            conn.execute(
                """INSERT INTO verification_receipts(name, passed, evidence_json, observed_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET passed=excluded.passed,
                     evidence_json=excluded.evidence_json,
                     observed_at=excluded.observed_at""",
                (name, int(passed), _json(evidence), now),
            )

    def resolve_known_rejection(
        self, attempt_id: str, *, evidence: str
    ) -> str:
        """Requeue one send only after an externally-proven rejection.

        This is intentionally narrower than a generic unknown-outcome reset.
        It preserves the original attempt and appends reconciliation evidence.
        """
        now = self.clock()
        with self._write() as conn:
            attempt = conn.execute(
                """SELECT event_id, status, error FROM send_attempts
                   WHERE attempt_id=?""",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise HelmControllerError("send attempt does not exist")
            if attempt["status"] != "outcome_unknown":
                raise HelmControllerError("send attempt is not outcome_unknown")
            if attempt["error"] != "HTTPError":
                raise HelmControllerError(
                    "only a recorded HTTP rejection may be reconciled this way"
                )
            detail = evidence.strip()[:400]
            if not detail:
                raise HelmControllerError("rejection evidence is required")
            conn.execute(
                """UPDATE send_attempts SET status='failed',
                   error=error || '; known rejection: ' || ?
                   WHERE attempt_id=?""",
                (detail, attempt_id),
            )
            conn.execute(
                """UPDATE telegram_events SET state='queued', routed_actor=NULL,
                   claim_token=NULL, claim_expires_at=NULL, completed_at=NULL,
                   outcome='known_rejection_requeued'
                   WHERE event_id=? AND state='held_unknown'""",
                (attempt["event_id"],),
            )
            conn.execute(
                """INSERT INTO controller_meta(key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value_json=excluded.value_json,
                     updated_at=excluded.updated_at""",
                (
                    f"reconciliation:{attempt_id}",
                    _json({"evidence": detail, "event_id": attempt["event_id"]}),
                    now,
                ),
            )
            return attempt["event_id"]

    def reconcile_private_destination(
        self, event_id: str, *, chat_id: int, evidence: str
    ) -> None:
        """Map one imported archive DM to its Bot API private-chat identity."""
        now = self.clock()
        with self._write() as conn:
            event = conn.execute(
                """SELECT chat_id, sender_id, payload_json FROM telegram_events
                   WHERE event_id=?""",
                (event_id,),
            ).fetchone()
            if event is None:
                raise HelmControllerError("event does not exist")
            payload = json.loads(event["payload_json"])
            message = payload.get("message") or {}
            if (message.get("chat") or {}).get("type") != "private":
                raise HelmControllerError("only an imported private DM may be retargeted")
            if int(event["sender_id"] or 0) != int(chat_id):
                raise HelmControllerError(
                    "private Bot API destination must equal the authenticated sender"
                )
            detail = evidence.strip()[:400]
            if not detail:
                raise HelmControllerError("destination evidence is required")
            old_chat_id = int(event["chat_id"] or 0)
            message.setdefault("chat", {})["id"] = int(chat_id)
            conn.execute(
                """UPDATE telegram_events SET chat_id=?, payload_json=?
                   WHERE event_id=?""",
                (int(chat_id), _json(payload), event_id),
            )
            conn.execute(
                """UPDATE conversation_context SET chat_id=?
                   WHERE chat_id=? AND last_event_id=?""",
                (int(chat_id), old_chat_id, event_id),
            )
            conn.execute(
                """INSERT INTO controller_meta(key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value_json=excluded.value_json,
                     updated_at=excluded.updated_at""",
                (
                    f"destination_reconciliation:{event_id}",
                    _json(
                        {
                            "evidence": detail,
                            "from_chat_id": old_chat_id,
                            "to_chat_id": int(chat_id),
                        }
                    ),
                    now,
                ),
            )


def write_token_file(path: Path | str, token: str) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = target.with_suffix(target.suffix + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, target)
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)


def read_token_file(path: Path | str) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8").strip()


def telegram_send_plain(
    controller: HelmController,
    *,
    token: str,
    event_id: str,
    chat_id: int,
    text: str,
    reply_to: int | None = None,
    thread_id: int | None = None,
) -> dict:
    bot_token = _load_bot_token()
    body = {"chat_id": chat_id, "text": text[:3800]}
    if reply_to:
        body["reply_to_message_id"] = reply_to
    if thread_id:
        body["message_thread_id"] = thread_id
    intent_hash = hashlib.sha256(_json(body).encode("utf-8")).hexdigest()
    attempt_id = controller.begin_send(
        actor="sol", event_id=event_id, intent_hash=intent_hash, token=token
    )
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=_json(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        controller.finish_send(
            attempt_id, status="failed", error=f"HTTP {exc.code} rejection"
        )
        raise
    except Exception as exc:
        controller.finish_send(
            attempt_id, status="outcome_unknown", error=type(exc).__name__
        )
        raise
    if not isinstance(result, dict) or not result.get("ok"):
        controller.finish_send(
            attempt_id, status="failed", error="Telegram returned ok=false"
        )
        raise HelmControllerError("Telegram rejected the send")
    message_id = int((result.get("result") or {}).get("message_id") or 0) or None
    controller.finish_send(
        attempt_id, status="accepted", telegram_message_id=message_id
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    sub.add_parser("status")

    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("json_file")

    acquire = sub.add_parser("acquire-sol")
    acquire.add_argument("--token-file", required=True)
    acquire.add_argument("--sol-ttl", type=int, default=2400)
    acquire.add_argument("--watcher-ttl", type=int, default=300)
    acquire.add_argument("--bridge-ttl", type=int, default=120)
    acquire.add_argument("--reason", required=True)

    renew = sub.add_parser("renew")
    renew.add_argument("kind", choices=("sol", "watcher", "bridge"))
    renew.add_argument("--token-file", required=True)
    renew.add_argument("--ttl", type=int, required=True)

    claim = sub.add_parser("next")
    claim.add_argument("--token-file", required=True)

    no_reply = sub.add_parser("no-reply")
    no_reply.add_argument("event_id")
    no_reply.add_argument("claim_token")
    no_reply.add_argument("outcome")

    transition = sub.add_parser("begin-transition")
    transition.add_argument("reason")
    fleet = sub.add_parser("complete-fleet")
    fleet.add_argument("reason")

    import_state = sub.add_parser("import-state")
    import_state.add_argument("name")
    import_state.add_argument("json_file")

    receipt = sub.add_parser("record-verification")
    receipt.add_argument("name")
    receipt.add_argument("passed", choices=("0", "1"))
    receipt.add_argument("json_file")

    reconcile = sub.add_parser("reconcile-fleet-history")
    reconcile.add_argument("commodore_db")

    rejection = sub.add_parser("resolve-known-rejection")
    rejection.add_argument("attempt_id")
    rejection.add_argument("evidence")

    destination = sub.add_parser("reconcile-private-destination")
    destination.add_argument("event_id")
    destination.add_argument("chat_id", type=int)
    destination.add_argument("evidence")

    send = sub.add_parser("send")
    send.add_argument("--token-file", required=True)
    send.add_argument("--event-id", required=True)
    send.add_argument("--chat-id", type=int, required=True)
    send.add_argument("--text-file", required=True)
    send.add_argument("--reply-to", type=int)
    send.add_argument("--thread-id", type=int)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    controller = HelmController(args.db)
    if args.command == "init":
        print(_json(controller.status()))
    elif args.command == "status":
        print(_json(controller.status()))
    elif args.command == "enqueue":
        update = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        print(_json(controller.enqueue_update(update)))
    elif args.command == "acquire-sol":
        token = controller.acquire_sol(
            sol_ttl=args.sol_ttl,
            watcher_ttl=args.watcher_ttl,
            bridge_ttl=args.bridge_ttl,
            reason=args.reason,
        )
        write_token_file(args.token_file, token)
        print(_json(controller.status()))
    elif args.command == "renew":
        controller.renew(
            args.kind, read_token_file(args.token_file), args.ttl
        )
        print(_json({"ok": True, "kind": args.kind}))
    elif args.command == "next":
        print(_json(controller.claim_next(read_token_file(args.token_file))))
    elif args.command == "no-reply":
        controller.mark_no_reply(args.event_id, args.claim_token, args.outcome)
        print(_json({"ok": True}))
    elif args.command == "begin-transition":
        generation = controller.begin_transition(args.reason)
        print(_json({"ok": True, "generation": generation}))
    elif args.command == "complete-fleet":
        controller.complete_fleet(args.reason)
        print(_json(controller.status()))
    elif args.command == "import-state":
        payload = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        controller.set_meta(args.name, payload)
        print(_json({"ok": True, "name": args.name}))
    elif args.command == "record-verification":
        evidence = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        controller.record_verification(args.name, args.passed == "1", evidence)
        print(_json({"ok": True, "name": args.name}))
    elif args.command == "reconcile-fleet-history":
        print(_json({"reconciled": controller.reconcile_fleet_history(args.commodore_db)}))
    elif args.command == "resolve-known-rejection":
        event_id = controller.resolve_known_rejection(
            args.attempt_id, evidence=args.evidence
        )
        print(_json({"ok": True, "event_id": event_id}))
    elif args.command == "reconcile-private-destination":
        controller.reconcile_private_destination(
            args.event_id, chat_id=args.chat_id, evidence=args.evidence
        )
        print(_json({"ok": True, "event_id": args.event_id}))
    elif args.command == "send":
        result = telegram_send_plain(
            controller,
            token=read_token_file(args.token_file),
            event_id=args.event_id,
            chat_id=args.chat_id,
            text=Path(args.text_file).read_text(encoding="utf-8"),
            reply_to=args.reply_to,
            thread_id=args.thread_id,
        )
        sent_id = int((result.get("result") or {}).get("message_id") or 0) or None
        print(_json({"ok": True, "message_id": sent_id}))
    else:
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

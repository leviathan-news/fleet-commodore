"""Independent model-free outcome inspection and receipt-fenced operator paging.

Inspection is the default. Only the registered Mini wrapper's explicit --page
mode can send. This module never imports the daemon, retrieves message bodies,
invokes a provider, retries uncertain notifications, or reconciles request state.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import time
import urllib.error
import urllib.request
import uuid

from chat_dispatch import PollOwner, PollOwnerBusy
from fleet_watchdog import helm_allows_ordinary, UnsafeObservation


_UNRESOLVED = ("queued", "running", "handed_off", "held_unknown")
_CLASSES = {
    "held_delivery", "request_overdue", "poll_stale", "router_down",
    "intake_unreadable", "intake_missing", "poll_health_unavailable",
}


def _age(now, timestamp):
    if timestamp is None:
        return None
    value = float(timestamp)
    if not math.isfinite(value):
        raise ValueError("invalid timestamp")
    return max(0.0, now - value)


def read_outcomes(path, *, now=None):
    now = time.time() if now is None else now
    path = Path(path)
    empty = {"counts": {}, "oldest_unresolved_age": None, "last_reply_at": None,
             "poll_age": None, "router_alive": None}
    if path.is_symlink():
        return {"status": "read_error", **empty}
    if not path.exists():
        return {"status": "not_installed", **empty}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.execute("BEGIN")  # Counts/age/health belong to one read snapshot.
            counts = {key: 0 for key in _UNRESOLVED}
            for status, count in conn.execute("SELECT status, COUNT(*) FROM chat_intake_event GROUP BY status"):
                if status in counts:
                    counts[status] = count
            oldest = conn.execute("SELECT MIN(created_at) FROM chat_intake_event WHERE status IN ('queued','running','handed_off','held_unknown')").fetchone()[0]
            reply = conn.execute("SELECT MAX(finished_at) FROM chat_intake_event WHERE status='resolved' AND message_id>0").fetchone()[0]
            meta = dict(conn.execute("SELECT name,value FROM chat_intake_meta WHERE name IN ('created_at','last_poll_at','router_alive')"))
        router = meta.get("router_alive")
        return {
            "status": "ok", "counts": counts,
            "oldest_unresolved_age": _age(now, oldest), "last_reply_at": reply,
            "poll_age": _age(now, meta.get("last_poll_at", meta.get("created_at"))),
            "router_alive": None if router is None else router == 1,
        }
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return {"status": "read_error", **empty}


def problem_from(report):
    status = report["status"]
    if status == "not_installed":
        kind, sentence = "intake_missing", "The durable intake ledger is missing; reply coverage cannot be verified."
    elif status != "ok":
        kind, sentence = "intake_unreadable", "The durable intake ledger cannot be read; reply coverage needs operator review."
    elif report.get("poll_age") is None:
        kind, sentence = "poll_health_unavailable", "Polling freshness is unavailable; reply coverage needs operator review."
    elif report["poll_age"] > 120:
        kind, sentence = "poll_stale", "No successful intake poll has been recorded for over two minutes."
    elif report.get("router_alive") is False:
        kind, sentence = "router_down", "The routing worker is down; stored work remains unresolved."
    elif report.get("counts", {}).get("held_unknown", 0) > 0:
        kind = "held_delivery"
        count = int(report["counts"]["held_unknown"])
        sentence = f"{count} stored update(s) have uncertain outcomes and need receipt review; they will not be replayed."
    elif report.get("oldest_unresolved_age") is not None and report["oldest_unresolved_age"] > 120:
        kind, sentence = "request_overdue", "Stored work has remained unresolved for over two minutes; an acknowledgement is not a completed answer."
    else:
        return None
    return {"class": kind, "text": "<b>Fleet needs attention</b>\n" + sentence}


def _receipt(response, destination=None):
    if not isinstance(response, dict) or response.get("ok") is not True:
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    message_id = result.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        return None
    if destination is not None:
        chat = result.get("chat")
        if not isinstance(chat, dict) or isinstance(chat.get("id"), bool) or chat.get("id") != destination:
            return None
    return message_id


class AlertLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS outcome_watch_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    active INTEGER NOT NULL, episode INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO outcome_watch_state VALUES (1,0,0);
                CREATE TABLE IF NOT EXISTS outcome_alert (
                    attempt_id TEXT PRIMARY KEY, episode INTEGER NOT NULL,
                    problem_class TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, message_id INTEGER
                );
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=2)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    def reserve(self, problem_class, now):
        if problem_class not in _CLASSES:
            raise ValueError("unsupported problem class")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active, episode = conn.execute("SELECT active,episode FROM outcome_watch_state WHERE singleton=1").fetchone()
            if not active:
                episode += 1
                conn.execute("UPDATE outcome_watch_state SET active=1,episode=? WHERE singleton=1", (episode,))
            previous = conn.execute("SELECT status,created_at FROM outcome_alert WHERE episode=? ORDER BY rowid DESC LIMIT 1", (episode,)).fetchone()
            if previous and (previous[0] != "accepted" or now - previous[1] < 6 * 3600):
                conn.rollback()
                return None
            attempt = uuid.uuid4().hex
            conn.execute("INSERT INTO outcome_alert VALUES(?,?,?,'prepared',?,NULL)", (attempt, episode, problem_class, now))
            conn.commit()  # The intent is durable before the single POST.
            return attempt

    def finish(self, attempt, response, *, destination=None):
        message_id = _receipt(response, destination)
        code = response.get("error_code") if isinstance(response, dict) else None
        rejected = isinstance(response, dict) and response.get("ok") is False and isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 500
        status = "accepted" if message_id is not None else "failed" if rejected else "outcome_unknown"
        with self._connect() as conn:
            changed = conn.execute("UPDATE outcome_alert SET status=?,message_id=? WHERE attempt_id=? AND status='prepared'", (status, message_id, attempt)).rowcount
            conn.commit()
        if changed != 1:
            raise RuntimeError("notification claim is no longer prepared")
        return status

    def recover(self):
        with self._connect() as conn:
            conn.execute("UPDATE outcome_watch_state SET active=0 WHERE singleton=1")
            conn.commit()


def page_once(report, ledger, send, *, now=None, destination=None):
    problem = problem_from(report)
    if problem is None:
        ledger.recover()
        return {"state": "healthy"}
    attempt = ledger.reserve(problem["class"], time.time() if now is None else now)
    if attempt is None:
        return {"state": "held", "class": problem["class"]}
    try:
        response = send(problem["text"])
    except Exception:
        # It may have been accepted. No raw exception/URL/body is persisted.
        response = None
    return {"state": ledger.finish(attempt, response, destination=destination), "class": problem["class"]}


def telegram_send(text, token, destination):
    """One HTML POST with a main-thread wall timer; never a fallback/retry."""
    payload = json.dumps({"chat_id": destination, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, headers={"Content-Type": "application/json"}, method="POST")
    prior_handler = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL)[0] > 0:
        raise RuntimeError("notification deadline is already owned")
    def deadline(_signum, _frame):
        raise TimeoutError("notification deadline exceeded")
    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 15)
    try:
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read(65537)
            if len(body) > 65536:
                return None
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error_code": exc.code} if 400 <= exc.code < 500 else None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior_handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", action="store_true", help="registered Mini runtime only: one receipt-fenced operator DM")
    args = parser.parse_args(argv)
    state = Path(os.environ.get("FLEET_COMMODORE_STATE_DIR", "~/.local/state/fleet-commodore")).expanduser()
    daemon_db = Path(os.environ.get("COMMODORE_DB_FILE", state / "commodore.db")).expanduser()
    report = read_outcomes(daemon_db.parent / "chat-intake.db")
    if not args.page:
        print(json.dumps({"outcomes": report, "problem": problem_from(report)}, sort_keys=True))
        return 0 if problem_from(report) is None else 1
    try:
        if os.environ.get("HELM_CONTROLLER_ENABLED") == "1" or not helm_allows_ordinary(Path(os.environ.get("HELM_CONTROLLER_DB_FILE", state / "helm-controller/controller.db")).expanduser()):
            result = {"state": "hold_controller_ownership"}
        else:
            destination = int(os.environ.get("OPERATOR_DM_USER_ID", "0") or 0)
            if destination <= 0:
                raise ValueError("operator destination unavailable")
            token = os.environ.get("BOT_TOKEN") or Path(os.environ.get("BOT_TOKEN_FILE", "/run/secrets/bot_token")).expanduser().read_text().strip()
            if not token:
                raise ValueError("notification token unavailable")
            with PollOwner(state / "outcome-watch.lock"):
                result = page_once(report, AlertLedger(state / "outcome-alerts.db"), lambda text: telegram_send(text, token, destination), destination=destination)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["state"] == "healthy" else 1
    except PollOwnerBusy:
        print('{"state":"watch_busy"}')
        return 0
    except (OSError, ValueError, sqlite3.Error, UnsafeObservation, RuntimeError) as exc:
        print(json.dumps({"state": "watch_unavailable", "class": type(exc).__name__}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

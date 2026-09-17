"""One bounded routing worker, independent of the Telegram poll owner.

There is deliberately no automatic replay of a running or uncertain event.
Callbacks own provider deadlines. A stuck callback is visible in intake state;
it cannot prevent the poll owner from durably admitting subsequent updates.
"""
from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
import threading
import time


log = logging.getLogger(__name__)


class PollOwnerBusy(RuntimeError):
    """Another cooperating process owns this service's polling lock."""


class PollOwner:
    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            os.close(self.fd)
            self.fd = None
            if isinstance(exc, BlockingIOError):
                raise PollOwnerBusy("Fleet poll owner already active") from None
            raise
        return self

    def __exit__(self, *_args):
        if self.fd is not None:
            # Closing releases the lock; never unlink an inode another owner
            # might have open. Descendant execs cannot inherit this descriptor.
            os.close(self.fd)
            self.fd = None


class ChatDispatcher:
    def __init__(self, intake, route, maintenance=None):
        self.intake = intake
        self.route = route
        self.maintenance = maintenance
        self._stop = threading.Event()
        self._thread = None

    def run_one(self):
        event = self.intake.claim_next()
        if event is None:
            return False
        try:
            result = self.route(event["payload"])
            if not isinstance(result, dict):
                raise ValueError("routing outcome missing")
            outcome = result.get("outcome")
            receipt = result.get("message_id")
            if outcome in {"resolved", "escalated"} and (
                isinstance(receipt, bool) or not isinstance(receipt, int) or receipt <= 0
            ):
                raise ValueError("routing outcome lacks a positive receipt")
            if outcome == "handed_off" and (
                result.get("job_table") not in {"qa_job", "build_job", "pr_review"}
                or not isinstance(result.get("job_uuid"), str) or not result["job_uuid"]
            ):
                raise ValueError("routing handoff lacks a durable job identity")
        except Exception as exc:
            # A send may have succeeded before any callback exception. Neither
            # retry nor a fabricated resolution is safe. Log no payload/error text.
            log.warning("chat routing held update=%s class=%s", event["update_id"], type(exc).__name__)
            result = {"outcome": "held_unknown"}
        self.intake.finish(
            event["update_id"], event["claim_token"], result["outcome"],
            job_table=result.get("job_table"), job_uuid=result.get("job_uuid"),
            message_id=result.get("message_id"),
        )
        return True

    def _run(self):
        last_maintenance = 0
        while not self._stop.is_set():
            try:
                if self.maintenance is not None and time.monotonic() - last_maintenance >= 60:
                    last_maintenance = time.monotonic()
                    try:
                        self.maintenance()
                    except Exception as exc:
                        # Cleanup/backup maintenance cannot starve actual hails.
                        log.warning("chat maintenance failure class=%s", type(exc).__name__)
                if not self.run_one():
                    self._stop.wait(0.1)
            except Exception as exc:
                # Persist failures retain the running claim. Do not reroute it.
                log.error("chat dispatcher state failure class=%s", type(exc).__name__)
                self._stop.wait(1)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("chat dispatcher already started")
        self._thread = threading.Thread(target=self._run, name="chat_router", daemon=True)
        self._thread.start()

    def stop(self, timeout=1):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def alive(self):
        return self._thread is not None and self._thread.is_alive()

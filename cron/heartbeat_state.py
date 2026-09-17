#!/usr/bin/env python3
"""Heartbeat process lock and exact Telegram response classification."""
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "run":
        # Advisory locks disappear when the last owner exits, including on
        # SIGKILL. Pass the fd to the shell so killing this wrapper alone
        # cannot unlock a still-running probe/send.
        path = Path(sys.argv[2]) / "claude-heartbeat.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            return subprocess.call([sys.argv[3], "--lock-held"], pass_fds=(lock.fileno(),))
    if len(sys.argv) != 2 or sys.argv[1] != "classify":
        return 2
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        print("ambiguous")
        return 0
    if not isinstance(value, dict):
        print("ambiguous")
        return 0
    ok = value.get("ok")
    result = value.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    accepted = (
        ok is True and type(message_id) is int and not isinstance(message_id, bool)
        and message_id > 0
    )
    error_code = value.get("error_code")
    rejected = (
        ok is False and type(error_code) is int and not isinstance(error_code, bool)
        and 400 <= error_code <= 499
    )
    print("accepted" if accepted else "rejected" if rejected else "ambiguous")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

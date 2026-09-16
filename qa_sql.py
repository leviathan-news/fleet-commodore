"""Run one bounded, read-only SQL query in the reviewer container.

This is deliberately a small host-side boundary.  SQL parsing, the
transaction read-only setting, and the table denylist remain in
``bin/commodore-db``; this module only supplies the wrapper with a disposable
container and the configured database credential.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


DOCKER = os.environ.get("COMMODORE_DOCKER", "/usr/local/bin/docker")
REVIEWER_IMAGE = os.environ.get("COMMODORE_REVIEWER_IMAGE", "commodore-reviewer:latest")
QA_NETWORK = os.environ.get("COMMODORE_QA_EGRESS_NETWORK", "commodore-qa-egress")
DB_URL_FILE = Path(os.environ.get(
    "COMMODORE_DB_URL_FILE", "~/.config/commodore/db_url"
)).expanduser()
WRAPPER = Path(__file__).resolve().parent / "bin" / "commodore-db"

MAX_SQL_CHARS = 4000
MAX_OUTPUT_BYTES = 128 * 1024
TIMEOUT_S = 15.0
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _docker_env() -> dict[str, str]:
    """Minimal Docker-client environment, matching launch and cleanup."""
    return {"HOME": os.environ.get("HOME", "/tmp"), "PATH": _SAFE_PATH}


def _error(code: str) -> dict:
    """Return the intentionally non-sensitive public error envelope."""
    return {"error": code}


def _docker_cleanup(name: str, *, kill: bool = False) -> None:
    """Clean up exactly the container name owned by this invocation."""
    if kill:
        try:
            subprocess.run(
                [DOCKER, "kill", name], capture_output=True, timeout=3,
                env=_docker_env(), check=False,
            )
        except Exception:
            pass
    try:
        subprocess.run(
            [DOCKER, "rm", "-f", name], capture_output=True, timeout=3,
            env=_docker_env(), check=False,
        )
    except Exception:
        pass


def execute_sql(sql: str) -> dict:
    """Execute one SQL string through the reviewed ``commodore-db`` wrapper.

    The function never exposes Docker stderr, exception text, or the database
    URL.  It returns the wrapper's top-level JSON object on success and a
    fixed ``{"error": ...}`` object for boundary failures.
    """
    if not isinstance(sql, str):
        return _error("invalid_sql")
    if len(sql) > MAX_SQL_CHARS:
        return _error("sql_too_large")
    try:
        db_url = DB_URL_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return _error("missing_credentials")
    if not db_url:
        return _error("missing_credentials")
    if "\n" in db_url or "\r" in db_url:
        return _error("missing_credentials")
    if not WRAPPER.is_file():
        return _error("internal_error")

    name = f"commodore-qa-sql-{uuid.uuid4().hex}"
    scratch = None
    proc = None
    stdout = stderr = None
    input_file = None
    launched = False
    try:
        scratch = Path(tempfile.mkdtemp(prefix="commodore-qa-sql-", dir="/tmp"))
        os.chmod(scratch, 0o700)
        env_file = scratch / "env"
        # Docker's env-file syntax has no quoting that is useful for arbitrary
        # URLs; the configured URL is treated as one opaque value by Docker.
        env_file.write_text(f"COMMODORE_DB_URL={db_url}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        input_path = scratch / "input.sql"
        input_path.write_text(sql, encoding="utf-8")
        os.chmod(input_path, 0o600)
        stdout_path, stderr_path = scratch / "stdout", scratch / "stderr"
        stdout = stdout_path.open("wb")
        stderr = stderr_path.open("wb")
        input_file = input_path.open("rb")
        cmd = [
            DOCKER, "run", "--rm", "-i", "--name", name,
            "--network", QA_NETWORK,
            "--read-only", "--user", "1000:1000",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m",
            "--memory", "256m", "--cpus", "0.5",
            "--security-opt", "no-new-privileges",
            "--env-file", str(env_file),
            "-v", f"{WRAPPER}:/app/bin/commodore-db:ro",
            "--entrypoint", "/app/bin/commodore-db",
            REVIEWER_IMAGE,
        ]
        # Docker Desktop's credential helper/socket context is tied to the
        # operator's real HOME. Keep only that and a fixed PATH; no app/model
        # credentials or inherited environment variables cross the boundary.
        clean_env = _docker_env()
        deadline = time.monotonic() + TIMEOUT_S
        proc = subprocess.Popen(
            cmd, stdin=input_file, stdout=stdout, stderr=stderr,
            env=clean_env, close_fds=True,
        )
        launched = True
        while proc.poll() is None:
            if (time.monotonic() >= deadline or
                    stdout_path.stat().st_size > MAX_OUTPUT_BYTES or
                    stderr_path.stat().st_size > MAX_OUTPUT_BYTES):
                timed_out = time.monotonic() >= deadline
                _docker_cleanup(name, kill=True)
                try:
                    proc.kill()
                except Exception:
                    pass
                proc.wait(timeout=3)
                return _error("timeout" if timed_out else "output_too_large")
            time.sleep(0.02)

        stdout.flush()
        stderr.flush()
        if proc.returncode != 0:
            _docker_cleanup(name)
            return _error("execution_failed")
        if (stdout_path.stat().st_size > MAX_OUTPUT_BYTES or
                stderr_path.stat().st_size > MAX_OUTPUT_BYTES):
            return _error("output_too_large")
        try:
            payload = json.loads(stdout_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return _error("invalid_output")
        if not isinstance(payload, dict):
            return _error("invalid_output")
        if payload.get("error"):
            return _error("wrapper_error")
        if (payload.get("status") != "ok" or
                not isinstance(payload.get("columns"), list) or
                not isinstance(payload.get("rows"), list)):
            return _error("invalid_output")
        return payload
    except Exception:
        if launched:
            _docker_cleanup(name, kill=True)
        return _error("execution_failed")
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        try:
            if proc is not None and proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            if input_file is not None and not input_file.closed:
                input_file.close()
        except Exception:
            pass
        for stream in (stdout, stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except Exception:
                pass
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

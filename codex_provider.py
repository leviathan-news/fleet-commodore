"""Bounded, subscription-only Codex writer for Fleet jobs.

This adapter deliberately has no API-key path or provider fallback.  The Codex
CLI receives only the operator's cached ChatGPT subscription authentication.
"""

from __future__ import annotations

from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Any


logger = logging.getLogger(__name__)

CODEX_BIN = "/opt/homebrew/bin/codex"
ALLOWED_MODELS = frozenset({"gpt-5.6-sol", "gpt-5.6-luna"})
MAX_OUTPUT_BYTES = 1024 * 1024
KILL_GRACE_SECONDS = 3
_CLEAN_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_ENVIRONMENT_KEYS = ("HOME", "USER", "LOGNAME", "LANG")

FAILURE_CLASS_PROVIDER_AUTH_FAILED = "provider_auth_failed"
FAILURE_CLASS_PROVIDER_RATE_LIMITED = "provider_rate_limited"
FAILURE_CLASS_PROVIDER_TIMEOUT = "provider_timeout"
FAILURE_CLASS_PROVIDER_UNAVAILABLE = "provider_unavailable"
FAILURE_CLASS_PROVIDER_NO_OUTPUT = "provider_no_output"
FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR = "provider_protocol_error"

_AUTH_MARKERS = (
    "authentication_error",
    "failed to authenticate",
    "oauth access token",
    "unauthorized",
    "http 401",
)
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "http 429",
    "status 429",
    "subscription limit",
    "usage limit",
)
_UNAVAILABLE_MARKERS = (
    "service unavailable",
    "temporarily unavailable",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "http 502",
    "http 503",
    "http 504",
)
_ACTION_ITEM_TYPES = frozenset({
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
    "plan",
    "computer_call",
    "apply_patch",
    "shell",
    "tool_call",
    "function_call",
})


def _clean_environment(temp_directory: str) -> dict[str, str]:
    """Keep saved ChatGPT auth while withholding API and app configuration."""
    environment = {
        key: value
        for key in _ENVIRONMENT_KEYS
        if (value := os.environ.get(key))
    }
    environment["TMPDIR"] = temp_directory
    environment["PATH"] = _CLEAN_PATH
    return environment


def _group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _terminate_process_group(
    process: subprocess.Popen[bytes], *, output_paths: tuple[Path, ...] = ()
) -> bool:
    """End the owned session and reap descendants after a bounded grace."""
    truncated = False
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return _truncate_oversized_streams(*output_paths)

    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        truncated |= _truncate_oversized_streams(*output_paths)
        if process.poll() is not None and not _group_exists(process.pid):
            return truncated
        if truncated:
            break
        try:
            process.wait(timeout=min(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            time.sleep(0.01)
        else:
            # wait() returns immediately once the leader has exited, but its
            # descendants may still hold the original process group.
            time.sleep(min(0.05, deadline - time.monotonic()))

    if _group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if process.poll() is None:
        process.wait()
    return truncated or _truncate_oversized_streams(*output_paths)


def _read_stream(path: Path) -> tuple[bytes, bool]:
    size = path.stat().st_size
    with path.open("rb") as stream:
        output = stream.read(MAX_OUTPUT_BYTES + 1)
    # Reject an exact-size capture too: it may be a cut-off response.
    return output, size >= MAX_OUTPUT_BYTES or len(output) >= MAX_OUTPUT_BYTES


def _truncate_oversized_streams(*paths: Path) -> bool:
    """Cap captured files without imposing RLIMIT_FSIZE on Codex's auth/cache IO."""
    truncated = False
    for path in paths:
        if path.exists() and path.stat().st_size > MAX_OUTPUT_BYTES:
            with path.open("r+b") as stream:
                stream.truncate(MAX_OUTPUT_BYTES)
            truncated = True
    return truncated


def _failure_class(output: bytes, returncode: int | None) -> str:
    text = output.decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in _AUTH_MARKERS):
        return FAILURE_CLASS_PROVIDER_AUTH_FAILED
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return FAILURE_CLASS_PROVIDER_RATE_LIMITED
    if any(marker in text for marker in _UNAVAILABLE_MARKERS):
        return FAILURE_CLASS_PROVIDER_UNAVAILABLE
    return FAILURE_CLASS_PROVIDER_UNAVAILABLE


def _record_diagnostics(
    failure_context: dict[str, Any] | None,
    *,
    model: str,
    returncode: int | None,
    stdout: bytes = b"",
    stderr: bytes = b"",
    failure_class: str | None = None,
    truncated: bool = False,
    protocol_event_types: tuple[str, ...] = (),
) -> None:
    """Record hashes and sizes only; provider output can contain sensitive text."""
    if not isinstance(failure_context, dict):
        return
    failure_context.update({
        "provider": "codex_subscription_cli",
        "provider_usage": "subscription_invoked",
        "auth_source": "cached_chatgpt_subscription",
        "model": model,
        "returncode": returncode,
        "stdout_bytes": len(stdout),
        "stdout_sha256": sha256(stdout).hexdigest(),
        "stderr_bytes": len(stderr),
        "stderr_sha256": sha256(stderr).hexdigest(),
    })
    if failure_class:
        failure_context["failure_class"] = failure_class
    if truncated:
        failure_context["output_truncated"] = True
    if protocol_event_types:
        failure_context["protocol_event_types"] = list(protocol_event_types)


def _parse_completed_assistant_text(
    stdout: bytes,
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Accept exactly one completed agent message and no tool/action events."""
    event_types: list[str] = []

    def failure(failure_class: str) -> tuple[None, str, tuple[str, ...]]:
        return None, failure_class, tuple(dict.fromkeys(event_types))[:12]

    try:
        lines = stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return failure(FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR)

    completed_turns = 0
    assistant_messages: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return failure(FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return failure(FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR)

        event_type = event["type"]
        event_types.append(event_type)
        if event_type == "turn.completed":
            completed_turns += 1
            continue
        if event_type in {"thread.started", "turn.started"}:
            continue
        if event_type == "turn.failed" or event_type == "error":
            return failure(FAILURE_CLASS_PROVIDER_UNAVAILABLE)
        if event_type not in {"item.started", "item.updated", "item.completed"}:
            return failure(FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR)

        item = event.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            return failure(FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR)
        item_type = item["type"]
        if item_type in _ACTION_ITEM_TYPES:
            return failure(FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR)
        if item_type == "reasoning":
            continue
        if event_type != "item.completed" or item_type != "agent_message":
            return failure(FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR)
        text = item.get("text")
        if not isinstance(text, str):
            return failure(FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR)
        assistant_messages.append(text)

    if completed_turns != 1:
        return failure(FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR)
    if len(assistant_messages) != 1 or not assistant_messages[0].strip():
        return failure(FAILURE_CLASS_PROVIDER_NO_OUTPUT)
    return assistant_messages[0].strip(), None, ()


def _build_prompt(prompt: str, system_prompt: str, response_instruction: str) -> str:
    return (
        "SYSTEM INSTRUCTIONS:\n"
        "Do not use shell commands, apps, MCP, web search, or any other tools. "
        "Respond directly with the requested final text only.\n\n"
        f"{system_prompt.strip()}\n\n"
        f"{response_instruction.strip()}\n\n"
        f"{prompt}"
    )


def generate_via_codex(
    prompt: str,
    system_prompt: str,
    *,
    model: str,
    timeout_seconds: int,
    response_instruction: str,
    failure_context: dict[str, Any] | None = None,
) -> str | None:
    """Generate text through cached ChatGPT-subscription Codex auth only."""
    if model not in ALLOWED_MODELS:
        raise ValueError("model must be an explicitly allowed Codex subscription route")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive integer")
    if not all(isinstance(value, str) for value in (prompt, system_prompt, response_instruction)):
        raise ValueError("prompt, system_prompt, and response_instruction must be strings")
    if not response_instruction.strip():
        raise ValueError("response_instruction is required")

    full_prompt = _build_prompt(prompt, system_prompt, response_instruction)
    with tempfile.TemporaryDirectory(prefix="fleet-codex-") as run_root:
        root = Path(run_root)
        work_directory = root / "work"
        temp_directory = root / "tmp"
        work_directory.mkdir()
        temp_directory.mkdir()
        prompt_path = root / "prompt.txt"
        stdout_path = root / "stdout.jsonl"
        stderr_path = root / "stderr.log"
        prompt_path.write_text(full_prompt, encoding="utf-8")
        arguments = [
            CODEX_BIN,
            "exec",
            "--json",
            "--ignore-user-config",
            "--ephemeral",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "-C", str(work_directory),
            "-m", model,
            "-c", 'forced_login_method="chatgpt"',
            "-c", 'model_reasoning_effort="medium"',
            "-c", "project_doc_max_bytes=0",
            "-c", 'web_search="disabled"',
            "-c", "mcp_servers={}",
            "-c", "features.shell_tool=false",
            "-c", "features.apps=false",
            "-c", "features.multi_agent=false",
            "-c", "features.hooks=false",
            "-",
        ]
        try:
            with (
                prompt_path.open("rb") as stdin,
                stdout_path.open("wb") as stdout,
                stderr_path.open("wb") as stderr,
            ):
                process = subprocess.Popen(
                    arguments,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=work_directory,
                    env=_clean_environment(str(temp_directory)),
                    start_new_session=True,
                )

                deadline = time.monotonic() + timeout_seconds
                timed_out = False
                truncated = False
                group_closed = False
                while process.poll() is None:
                    if _truncate_oversized_streams(stdout_path, stderr_path):
                        truncated = True
                        truncated |= _terminate_process_group(
                            process, output_paths=(stdout_path, stderr_path)
                        )
                        group_closed = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        truncated |= _terminate_process_group(
                            process, output_paths=(stdout_path, stderr_path)
                        )
                        group_closed = True
                        break
                    try:
                        process.wait(timeout=min(0.1, remaining))
                    except subprocess.TimeoutExpired:
                        # Normally wait() consumed the interval. This avoids
                        # a busy loop with unusual subprocess implementations.
                        time.sleep(min(0.01, remaining))
                        continue

                truncated |= _truncate_oversized_streams(stdout_path, stderr_path)
                # A completed leader is insufficient proof that descendants
                # are gone; close any group still reachable by the owned PGID.
                if not group_closed:
                    if _group_exists(process.pid):
                        truncated |= _terminate_process_group(
                            process, output_paths=(stdout_path, stderr_path)
                        )
        except OSError:
            _record_diagnostics(
                failure_context,
                model=model,
                returncode=None,
                failure_class=FAILURE_CLASS_PROVIDER_UNAVAILABLE,
            )
            return None

        stdout_bytes, stdout_truncated = _read_stream(stdout_path)
        stderr_bytes, stderr_truncated = _read_stream(stderr_path)
        truncated = truncated or stdout_truncated or stderr_truncated
        if timed_out:
            _record_diagnostics(
                failure_context,
                model=model,
                returncode=process.returncode,
                stdout=stdout_bytes,
                stderr=stderr_bytes,
                failure_class=FAILURE_CLASS_PROVIDER_TIMEOUT,
                truncated=truncated,
            )
            return None
        if truncated:
            _record_diagnostics(
                failure_context,
                model=model,
                returncode=process.returncode,
                stdout=stdout_bytes,
                stderr=stderr_bytes,
                failure_class=FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR,
                truncated=True,
            )
            return None
        if process.returncode != 0:
            _record_diagnostics(
                failure_context,
                model=model,
                returncode=process.returncode,
                stdout=stdout_bytes,
                stderr=stderr_bytes,
                failure_class=_failure_class(stdout_bytes + b"\n" + stderr_bytes, process.returncode),
            )
            return None

        response, failure_class, protocol_event_types = _parse_completed_assistant_text(stdout_bytes)
        _record_diagnostics(
            failure_context,
            model=model,
            returncode=process.returncode,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            failure_class=failure_class,
            protocol_event_types=protocol_event_types,
        )
        if failure_class:
            logger.warning(
                "Codex subscription writer failed: model=%s class=%s rc=%s stdout_bytes=%s stderr_bytes=%s",
                model, failure_class, process.returncode, len(stdout_bytes), len(stderr_bytes),
            )
        return response


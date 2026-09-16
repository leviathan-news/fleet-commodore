from __future__ import annotations

import json
import os
import signal
import subprocess

import pytest

import codex_provider


def _events(*events):
    return "\n".join(json.dumps(event) for event in events).encode("utf-8")


def _successful_events(text="Generated article."):
    return _events(
        {"type": "thread.started"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed"},
    )


class _FakeProcess:
    pid = 12345
    returncode = 0

    def __init__(self, stdout, stderr, *, output=None, error=b"", timeout=False):
        self.stdout = stdout
        self.stderr = stderr
        self.output = output if output is not None else _successful_events()
        self.error = error
        self.timeout = timeout
        self.wait_calls = []
        self.done = False

    def _emit(self):
        if not self.done:
            self.stdout.write(self.output)
            self.stderr.write(self.error)
            self.done = True

    def poll(self):
        return self.returncode if self.done else None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.timeout and timeout is not None:
            raise subprocess.TimeoutExpired("codex", timeout)
        self._emit()
        return self.returncode
def test_codex_uses_clean_subscription_cli_contract_and_stdin(monkeypatch):
    captured = {}

    def fake_popen(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        captured["work_empty"] = not os.listdir(kwargs["cwd"])
        captured["stdin_prompt"] = kwargs["stdin"].read()
        captured["process"] = _FakeProcess(kwargs["stdout"], kwargs["stderr"])
        return captured["process"]

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("CODEX_HOME", "/must-not-reach-child")
    monkeypatch.setattr(codex_provider.subprocess, "Popen", fake_popen)

    context = {}
    result = codex_provider.generate_via_codex(
        "writer prompt", "system prompt", model="gpt-5.6-luna", timeout_seconds=9,
        response_instruction="Return the article.", failure_context=context,
    )

    assert result == "Generated article."
    arguments = captured["arguments"]
    assert arguments[:3] == [codex_provider.CODEX_BIN, "exec", "--json"]
    assert "--ignore-user-config" in arguments
    assert "--ignore-rules" not in arguments
    assert "--ephemeral" in arguments
    assert arguments[arguments.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in arguments
    assert arguments[arguments.index("-m") + 1] == "gpt-5.6-luna"
    assert 'forced_login_method="chatgpt"' in arguments
    assert 'model_reasoning_effort="medium"' in arguments
    assert "project_doc_max_bytes=0" in arguments
    assert 'web_search="disabled"' in arguments
    assert "mcp_servers={}" in arguments
    assert "features.shell_tool=false" in arguments
    assert "features.apps=false" in arguments
    assert "features.multi_agent=false" in arguments
    assert "features.hooks=false" in arguments
    assert arguments[-1] == "-"
    assert "writer prompt" not in arguments
    assert b"writer prompt" in captured["stdin_prompt"]
    assert b"system prompt" in captured["stdin_prompt"]
    assert b"Return the article." in captured["stdin_prompt"]
    assert captured["work_empty"] is True
    assert captured["kwargs"]["start_new_session"] is True
    assert "preexec_fn" not in captured["kwargs"]
    environment = captured["kwargs"]["env"]
    assert set(environment) <= {"HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "PATH"}
    assert "OPENAI_API_KEY" not in environment
    assert "CODEX_HOME" not in environment
    assert environment["PATH"].startswith("/opt/homebrew/bin:")
    assert context["provider_usage"] == "subscription_invoked"
    assert context["auth_source"] == "cached_chatgpt_subscription"
    assert context["model"] == "gpt-5.6-luna"
    assert context["returncode"] == 0


def test_codex_rejects_unapproved_model_and_invalid_timeout():
    with pytest.raises(ValueError, match="explicitly allowed"):
        codex_provider.generate_via_codex(
            "p", "s", model="gpt-5.6-terra", timeout_seconds=1, response_instruction="r"
        )
    with pytest.raises(ValueError, match="positive integer"):
        codex_provider.generate_via_codex(
            "p", "s", model="gpt-5.6-sol", timeout_seconds=True, response_instruction="r"
        )


@pytest.mark.parametrize(
    ("output", "failure_class"),
    [
        (b'Failed to authenticate: authentication_error', codex_provider.FAILURE_CLASS_PROVIDER_AUTH_FAILED),
        (b'HTTP 429 rate limit exceeded', codex_provider.FAILURE_CLASS_PROVIDER_RATE_LIMITED),
        (b'Service unavailable HTTP 503', codex_provider.FAILURE_CLASS_PROVIDER_UNAVAILABLE),
    ],
)
def test_nonzero_cli_errors_are_classified_without_raw_diagnostics(monkeypatch, output, failure_class):
    def fake_popen(_arguments, **kwargs):
        process = _FakeProcess(kwargs["stdout"], kwargs["stderr"], output=output)
        process.returncode = 1
        return process

    monkeypatch.setattr(codex_provider.subprocess, "Popen", fake_popen)
    context = {}

    assert codex_provider.generate_via_codex(
        "p", "s", model="gpt-5.6-sol", timeout_seconds=1,
        response_instruction="r", failure_context=context,
    ) is None

    assert context["failure_class"] == failure_class
    assert context["returncode"] == 1
    assert context["stdout_bytes"] == len(output)
    assert context["stdout_sha256"]
    assert "stdout" not in context
    assert "stderr" not in context
    assert "tokens" not in context


def test_timeout_terminates_the_owned_process_group_with_kill_grace(monkeypatch):
    process_box = {}
    killed = []

    def fake_popen(_arguments, **kwargs):
        process = _FakeProcess(kwargs["stdout"], kwargs["stderr"], timeout=True)
        process_box["process"] = process
        return process

    def fake_wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if timeout is not None:
            raise subprocess.TimeoutExpired("codex", timeout)
        self.returncode = -signal.SIGKILL
        self.done = True
        return self.returncode

    clock = [0.0]
    monkeypatch.setattr(codex_provider.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(_FakeProcess, "wait", fake_wait)
    monkeypatch.setattr(codex_provider.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(codex_provider.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        codex_provider.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    context = {}

    assert codex_provider.generate_via_codex(
        "p", "s", model="gpt-5.6-sol", timeout_seconds=1,
        response_instruction="r", failure_context=context,
    ) is None

    assert [call for call in killed if call[1] != 0] == [
        (12345, signal.SIGTERM), (12345, signal.SIGKILL)
    ]
    assert codex_provider.KILL_GRACE_SECONDS not in process_box["process"].wait_calls
    assert process_box["process"].wait_calls[-1] is None
    assert clock[0] >= 1 + codex_provider.KILL_GRACE_SECONDS
    assert context["failure_class"] == codex_provider.FAILURE_CLASS_PROVIDER_TIMEOUT
    assert context["returncode"] == -signal.SIGKILL


def test_cleanup_kills_descendants_after_leader_exits(monkeypatch):
    process = _FakeProcess(None, None)
    process.done = True
    calls = []
    clock = [0.0]

    def fake_killpg(pid, sig):
        calls.append((pid, sig))

    monkeypatch.setattr(codex_provider.os, "killpg", fake_killpg)
    monkeypatch.setattr(codex_provider.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        codex_provider.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    codex_provider._terminate_process_group(process)

    assert calls[0] == (process.pid, signal.SIGTERM)
    assert (process.pid, 0) in calls
    assert calls[-1] == (process.pid, signal.SIGKILL)


def test_tool_event_and_malformed_json_fail_closed(monkeypatch):
    outputs = [
        _events(
            {"type": "turn.started"},
            {"type": "item.started", "item": {"type": "command_execution"}},
            {"type": "turn.completed"},
        ),
        b"not-json\n",
    ]

    def fake_popen(_arguments, **kwargs):
        return _FakeProcess(kwargs["stdout"], kwargs["stderr"], output=outputs.pop(0))

    monkeypatch.setattr(codex_provider.subprocess, "Popen", fake_popen)
    for index in range(2):
        context = {}
        assert codex_provider.generate_via_codex(
            "p", "s", model="gpt-5.6-sol", timeout_seconds=1,
            response_instruction="r", failure_context=context,
        ) is None
        assert context["failure_class"] == codex_provider.FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR
        if index == 0:
            assert context["protocol_event_types"] == ["turn.started", "item.started"]
        else:
            assert "protocol_event_types" not in context


def test_reasoning_progress_events_are_not_treated_as_tool_use(monkeypatch):
    output = _events(
        {"type": "turn.started"},
        {"type": "item.started", "item": {"type": "reasoning"}},
        {"type": "item.updated", "item": {"type": "reasoning"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Final."}},
        {"type": "turn.completed"},
    )

    monkeypatch.setattr(
        codex_provider.subprocess, "Popen",
        lambda _arguments, **kwargs: _FakeProcess(kwargs["stdout"], kwargs["stderr"], output=output),
    )
    assert codex_provider.generate_via_codex(
        "p", "s", model="gpt-5.6-sol", timeout_seconds=1, response_instruction="r"
    ) == "Final."


def test_completed_turn_without_assistant_text_is_no_output(monkeypatch):
    def fake_popen(_arguments, **kwargs):
        return _FakeProcess(
            kwargs["stdout"], kwargs["stderr"],
            output=_events({"type": "turn.started"}, {"type": "turn.completed"}),
        )

    monkeypatch.setattr(codex_provider.subprocess, "Popen", fake_popen)
    context = {}

    assert codex_provider.generate_via_codex(
        "p", "s", model="gpt-5.6-sol", timeout_seconds=1,
        response_instruction="r", failure_context=context,
    ) is None
    assert context["failure_class"] == codex_provider.FAILURE_CLASS_PROVIDER_NO_OUTPUT


def test_turn_failed_and_process_start_failure_are_unavailable(monkeypatch):
    def failed_turn(_arguments, **kwargs):
        return _FakeProcess(
            kwargs["stdout"], kwargs["stderr"],
            output=_events({"type": "turn.started"}, {"type": "turn.failed"}),
        )

    monkeypatch.setattr(codex_provider.subprocess, "Popen", failed_turn)
    context = {}
    assert codex_provider.generate_via_codex(
        "p", "s", model="gpt-5.6-sol", timeout_seconds=1,
        response_instruction="r", failure_context=context,
    ) is None
    assert context["failure_class"] == codex_provider.FAILURE_CLASS_PROVIDER_UNAVAILABLE

    def unavailable(*_args, **_kwargs):
        raise OSError

    monkeypatch.setattr(codex_provider.subprocess, "Popen", unavailable)
    context = {}
    assert codex_provider.generate_via_codex(
        "p", "s", model="gpt-5.6-sol", timeout_seconds=1,
        response_instruction="r", failure_context=context,
    ) is None
    assert context["failure_class"] == codex_provider.FAILURE_CLASS_PROVIDER_UNAVAILABLE
    assert context["returncode"] is None


def test_oversized_output_is_bounded_and_rejected(monkeypatch):
    oversized = b"x" * (codex_provider.MAX_OUTPUT_BYTES + 1)

    def fake_popen(_arguments, **kwargs):
        return _FakeProcess(kwargs["stdout"], kwargs["stderr"], output=oversized)

    monkeypatch.setattr(codex_provider.subprocess, "Popen", fake_popen)
    context = {}

    assert codex_provider.generate_via_codex(
        "p", "s", model="gpt-5.6-sol", timeout_seconds=1,
        response_instruction="r", failure_context=context,
    ) is None
    assert context["failure_class"] == codex_provider.FAILURE_CLASS_PROVIDER_PROTOCOL_ERROR
    assert context["output_truncated"] is True
    assert context["stdout_bytes"] == codex_provider.MAX_OUTPUT_BYTES

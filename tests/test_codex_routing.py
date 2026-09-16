import json

import commodore
import codex_runtime


def test_codex_primary_does_not_check_broken_claude(monkeypatch):
    monkeypatch.setattr(commodore, "FLEET_PROVIDER", "codex")
    monkeypatch.setattr(codex_runtime, "ask", lambda *a, **kw: "Answer from Codex")
    monkeypatch.setattr(commodore, "_claude_is_available", lambda: (_ for _ in ()).throw(AssertionError("Claude consulted")))
    assert commodore.llm_ask("hello", is_direct=True) == "Answer from Codex"


def test_codex_outage_names_provider_and_does_not_claim_unaccepted_alert(monkeypatch):
    monkeypatch.setattr(commodore, "FLEET_PROVIDER", "codex")
    monkeypatch.setattr(codex_runtime, "ask", lambda *a, **kw: None)
    alerts = []
    monkeypatch.setattr(commodore, "_alert_operator_claude_down", lambda *a, **kw: alerts.append(kw) or False)
    assert "could not alert" in commodore.llm_ask("hello", is_direct=True)
    assert alerts == [{"provider": "codex"}]


def test_codex_cooldown_survives_calls_and_success_clears(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_COMMODORE_STATE_DIR", str(tmp_path))
    calls = []
    clock = [1000.0]
    monkeypatch.setattr(codex_runtime.time, "time", lambda: clock[0])
    def fail(*args, **kw):
        calls.append(1)
        kw["failure_context"]["failure_class"] = "provider_rate_limited"
        return None
    monkeypatch.setattr(codex_runtime, "generate_via_codex", fail)
    assert codex_runtime.ask("a") is None
    context = {}
    assert codex_runtime.ask("b", failure_context=context) is None
    assert len(calls) == 1 and context["cooldown_active"]
    clock[0] += 601
    monkeypatch.setattr(codex_runtime, "generate_via_codex", lambda *a, **kw: "Recovered")
    assert codex_runtime.ask("c") == "Recovered"
    assert codex_runtime.ask("d") == "Recovered"

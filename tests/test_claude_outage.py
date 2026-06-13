"""Claude-outage routing: silent in ambient, honest line on direct, DM op.

Validates the 2026-06-13 behavior change: the bot no longer performs
'wireless fouled by squall' in public when Claude is down. Direct pings
still get an honest in-character fallback; ambient/Nemesis-override
silently skip; in either case the operator is DM'd (deduped).
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("BOT_USERNAME", "commodore_lev_bot")
os.environ.setdefault("BOT_HQ_GROUP_ID", "-1001111111111")
os.environ.setdefault("SQUID_CAVE_GROUP_ID", "-1002222222222")
os.environ.setdefault("AGENT_CHAT_GROUP_ID", "-1003675648747")
os.environ.setdefault("LEV_DEV_GROUP_ID", "-1004444444444")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commodore as c


# --- llm_ask outage branches ----------------------------------------------


def test_llm_ask_direct_returns_outage_reply_when_claude_down(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "DB_FILE", tmp_path / "test.db")
    c._ensure_tables()
    # Force Claude unavailable.
    monkeypatch.setattr(c, "_claude_is_available", lambda: False)
    # Stub out the operator DM so the test doesn't try to hit Telegram.
    monkeypatch.setattr(c, "send_message",
                        lambda chat_id, text, **kw: {"ok": True,
                                                     "result": {"message_id": 1}})
    out = c.llm_ask("any prompt", is_direct=True)
    assert out == c.CLAUDE_OUTAGE_REPLY
    assert "Operator has been notified" in out  # honest framing


def test_llm_ask_non_direct_returns_none_when_claude_down(monkeypatch, tmp_path):
    """The whole point of the change: ambient/Nemesis-override silently skip."""
    monkeypatch.setattr(c, "DB_FILE", tmp_path / "test.db")
    c._ensure_tables()
    monkeypatch.setattr(c, "_claude_is_available", lambda: False)
    monkeypatch.setattr(c, "send_message",
                        lambda chat_id, text, **kw: {"ok": True,
                                                     "result": {"message_id": 1}})
    out = c.llm_ask("any prompt", is_direct=False)
    assert out is None


def test_llm_ask_returns_real_response_when_claude_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "DB_FILE", tmp_path / "test.db")
    c._ensure_tables()
    monkeypatch.setattr(c, "_claude_is_available", lambda: True)
    monkeypatch.setattr(c, "_claude_ask",
                        lambda prompt, timeout=120: "Aye Admiral.")
    # If Claude is healthy we never need send_message; if we do, fail loudly.
    monkeypatch.setattr(c, "send_message",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("send_message should not be called when Claude is healthy")))
    assert c.llm_ask("prompt", is_direct=True) == "Aye Admiral."
    assert c.llm_ask("prompt", is_direct=False) == "Aye Admiral."


# --- Operator DM dedupe ---------------------------------------------------


def test_alert_operator_sends_dm_first_time(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "DB_FILE", tmp_path / "test.db")
    c._ensure_tables()
    sent = []
    monkeypatch.setattr(c, "send_message",
                        lambda chat_id, text, **kw: sent.append((chat_id, text)) or
                        {"ok": True, "result": {"message_id": 1}})
    c._alert_operator_claude_down(reason="test")
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == int(next(iter(c.ADMIN_TELEGRAM_IDS)))
    assert "Claude" in text
    assert "/login" in text  # operator gets the actual fix instruction


def test_alert_operator_deduped_within_cooldown(monkeypatch, tmp_path):
    """Two rapid calls = one DM. Cooldown is 6h."""
    monkeypatch.setattr(c, "DB_FILE", tmp_path / "test.db")
    c._ensure_tables()
    sent = []
    monkeypatch.setattr(c, "send_message",
                        lambda chat_id, text, **kw: sent.append((chat_id, text)) or
                        {"ok": True, "result": {"message_id": 1}})
    c._alert_operator_claude_down(reason="first")
    c._alert_operator_claude_down(reason="second")
    c._alert_operator_claude_down(reason="third")
    assert len(sent) == 1, f"expected 1 DM (deduped), got {len(sent)}"


def test_alert_operator_failed_send_does_not_record(monkeypatch, tmp_path):
    """If the DM POST fails, don't record. Next call should retry."""
    monkeypatch.setattr(c, "DB_FILE", tmp_path / "test.db")
    c._ensure_tables()
    attempts = []

    def fake_send(chat_id, text, **kw):
        attempts.append(1)
        return {"ok": False, "description": "Bad Request"}

    monkeypatch.setattr(c, "send_message", fake_send)
    c._alert_operator_claude_down(reason="bad")
    c._alert_operator_claude_down(reason="bad")
    # Two attempts because the first didn't get recorded.
    assert len(attempts) == 2


def test_alert_operator_noop_without_admin(monkeypatch, tmp_path):
    """No ADMIN_TELEGRAM_IDS + no OPERATOR_DM_USER_ID → silently no-op,
    don't crash and don't try to send."""
    monkeypatch.setattr(c, "DB_FILE", tmp_path / "test.db")
    c._ensure_tables()
    monkeypatch.setattr(c, "ADMIN_TELEGRAM_IDS", set())
    monkeypatch.setattr(c, "OPERATOR_DM_USER_ID", 0)
    sent = []
    monkeypatch.setattr(c, "send_message",
                        lambda *a, **kw: sent.append(True) or {"ok": True})
    c._alert_operator_claude_down(reason="no op")
    assert sent == []

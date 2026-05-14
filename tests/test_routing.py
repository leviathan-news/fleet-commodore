"""Unit tests for per-chat and per-topic routing policy."""
import os
import sys
from pathlib import Path

# Establish env before importing commodore.
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("BOT_USERNAME", "commodore_lev_bot")
os.environ.setdefault("BOT_HQ_GROUP_ID", "-1001111111111")
os.environ.setdefault("SQUID_CAVE_GROUP_ID", "-1002222222222")
os.environ.setdefault("AGENT_CHAT_GROUP_ID", "-1003675648747")
os.environ.setdefault("LEV_DEV_GROUP_ID", "-1004444444444")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commodore import (  # noqa: E402
    _policy_for,
    AGENT_CHAT_TOPICS,
    BOT_HQ_GROUP_ID,
    LEV_DEV_GROUP_ID,
    SQUID_CAVE_GROUP_ID,
    AGENT_CHAT_GROUP_ID,
)


def test_policy_bot_hq_mention_only_and_pr_allowed():
    p = _policy_for(BOT_HQ_GROUP_ID, 0)
    assert p["speak"] == "mention_only"
    assert p["allow_pr"] is True


def test_policy_cave_mention_only():
    """Squid Cave: mention_only (2026-05-14). Commodore stays in his lane —
    no ambient social-director duty. Persona still defines voice when hailed."""
    p = _policy_for(SQUID_CAVE_GROUP_ID, 0)
    assert p["speak"] == "mention_only"
    assert p["ambient_cooldown_s"] == 0
    assert "squid cave" in p["persona_suffix"].lower()


def test_policy_monetization_mention_only_market_design_persona():
    """Monetization topic: mention_only (2026-05-14). When hailed, the
    persona still distinguishes market-DESIGN questions (Admiralty's
    province) from wager picks (refused with disdain). Echo-loop risk
    closed the ambient setting."""
    p = _policy_for(AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["monetization"])
    assert p["speak"] == "mention_only"
    assert p["ambient_cooldown_s"] == 0
    # Persona must still flag wagering itself as off-limits
    assert "wager" in p["persona_suffix"].lower() or "bet" in p["persona_suffix"].lower()
    # And explicitly invite design engagement
    assert "design" in p["persona_suffix"].lower()


def test_policy_opsec_no_ambient():
    p = _policy_for(AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["opsec"])
    assert p["speak"] == "mention_only"
    assert p["ambient_cooldown_s"] == 0


def test_policy_api_help_mention_only():
    """API Help: still his lane, but he waits to be asked (2026-05-14)."""
    p = _policy_for(AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["api_help"])
    assert p["speak"] == "mention_only"
    assert p["rate_limit_s"] <= 30
    assert "api" in p["persona_suffix"].lower()


def test_policy_sandbox_mention_only():
    """Sandbox: mention_only (2026-05-14). The previous 'banter with bots'
    setting was the source of echo-loop chatter when Benthic returned."""
    p = _policy_for(AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["sandbox"])
    assert p["speak"] == "mention_only"
    assert p["ambient_cooldown_s"] == 0


def test_no_room_runs_ambient():
    """Belt-and-suspenders: walk every known (chat, topic) and confirm none
    set speak=ambient. Future ambient settings should require a deliberate
    test override here."""
    test_pairs = [
        (BOT_HQ_GROUP_ID, 0),
        (LEV_DEV_GROUP_ID, 0),
        (SQUID_CAVE_GROUP_ID, 0),
        (AGENT_CHAT_GROUP_ID, 0),  # Start Here default
        (AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["monetization"]),
        (AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["opsec"]),
        (AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["api_help"]),
        (AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["sandbox"]),
        (AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["human_lounge"]),
        (AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["affiliate"]),
    ]
    for chat_id, topic_id in test_pairs:
        p = _policy_for(chat_id, topic_id)
        assert p["speak"] != "ambient", (
            f"chat_id={chat_id} topic_id={topic_id} is set to ambient — "
            f"all rooms should be mention_only (or never) per 2026-05-14."
        )


def test_policy_unknown_chat_is_never_noisy():
    p = _policy_for(-9999999999, 0)
    assert p["speak"] == "mention_only"
    assert p["ambient_cooldown_s"] == 0

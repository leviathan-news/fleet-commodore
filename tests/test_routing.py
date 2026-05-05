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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commodore import (  # noqa: E402
    _policy_for,
    AGENT_CHAT_TOPICS,
    BOT_HQ_GROUP_ID,
    SQUID_CAVE_GROUP_ID,
    AGENT_CHAT_GROUP_ID,
)


def test_policy_bot_hq_mention_only_and_pr_allowed():
    p = _policy_for(BOT_HQ_GROUP_ID, 0)
    assert p["speak"] == "mention_only"
    assert p["allow_pr"] is True


def test_policy_cave_ambient_with_cooldown():
    p = _policy_for(SQUID_CAVE_GROUP_ID, 0)
    assert p["speak"] == "ambient"
    assert p["ambient_cooldown_s"] >= 300


def test_policy_monetization_ambient_for_market_design():
    """Monetization topic: Admiral engages on market-design discussion
    (resolution criteria, oracle pinning, conflicts) but the persona
    refuses wager picks with disdain. Updated 2026-05-05 — Admiral was
    previously mention_only here and missed legitimate design questions."""
    p = _policy_for(AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["monetization"])
    assert p["speak"] == "ambient"
    assert p["ambient_cooldown_s"] > 0  # bounded to prevent spam
    # Persona must still flag wagering itself as off-limits
    assert "wager" in p["persona_suffix"].lower() or "bet" in p["persona_suffix"].lower()
    # And explicitly invite design engagement
    assert "design" in p["persona_suffix"].lower()


def test_policy_opsec_no_ambient():
    p = _policy_for(AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["opsec"])
    assert p["speak"] == "mention_only"
    assert p["ambient_cooldown_s"] == 0


def test_policy_api_help_responsive():
    p = _policy_for(AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["api_help"])
    assert p["speak"] == "ambient"
    assert p["rate_limit_s"] <= 30


def test_policy_sandbox_ambient():
    p = _policy_for(AGENT_CHAT_GROUP_ID, AGENT_CHAT_TOPICS["sandbox"])
    assert p["speak"] == "ambient"


def test_policy_unknown_chat_is_never_noisy():
    p = _policy_for(-9999999999, 0)
    assert p["speak"] == "mention_only"
    assert p["ambient_cooldown_s"] == 0

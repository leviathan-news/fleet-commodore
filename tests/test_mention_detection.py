"""Display-name mention detection for @LeviathanFleetCommodore etc.

Prior to 2026-04-19, the Commodore's is_direct check was a simple
  `f"@{BOT_USERNAME}" in text_lower`
so messages like Eunice's `@LeviathanFleetCommodore — nicepick.dev is back up`
slipped through as ambient (BOT_USERNAME = 'leviathan_commodore_bot').
These tests pin the fix and its alias set.
"""
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setenv("BOT_USERNAME", "commodore_lev_bot")
    monkeypatch.setenv("BOT_HQ_GROUP_ID", "-1001111111111")
    monkeypatch.setenv("SQUID_CAVE_GROUP_ID", "-1002222222222")
    monkeypatch.setenv("AGENT_CHAT_GROUP_ID", "-1003675648747")
    yield


def _msg(text, entities=None):
    m = {
        "message_id": 1,
        "chat": {"id": -1003675648747},
        "from": {"id": 999, "username": "someone"},
        "text": text,
    }
    if entities is not None:
        m["entities"] = entities
    return m


# --- textual alias matches -------------------------------------------------


@pytest.mark.parametrize("text", [
    "@leviathan_commodore_bot ahoy!",         # canonical Telegram handle (tests aliases catch it too)
    "@LeviathanFleetCommodore — nicepick is up",  # the Eunice-at-02:30 case
    "@leviathanfleetcommodore pls advise",        # lowercase variant
    "hey @fleet_commodore what's the dispatch status",
    "@fleetcommodore — two notes",
    "@commodore_lev_bot reporting in",            # older-draft handle
    "@commodore thoughts?",                       # bare "commodore"
    "mixed case @LEVIATHANFLEETCOMMODORE",
])
def test_alias_matches_fire(text):
    import commodore as c
    assert c._is_mention_of_commodore(_msg(text), text.lower())


@pytest.mark.parametrize("text", [
    "",                                           # empty
    "just chatting",                              # unrelated
    "@benthic_bot your move",                     # other agent
    "@deepseasquid_bot gives the Commodore his regards",  # mentions another bot but talks ABOUT commodore
    "the commodore is asleep",                    # word appears but no @
    "admirable commodore reports",                # "commodore" in prose but no @
    "@Z_3_r_o ping",                              # humans get @'d, not us
])
def test_alias_negatives(text):
    import commodore as c
    assert not c._is_mention_of_commodore(_msg(text), text.lower())


# --- structured text_mention entities --------------------------------------


def test_text_mention_entity_fires(monkeypatch):
    """Telegram clients can emit a `text_mention` entity pointing at the
    bot by user_id when an author picks the bot from autocomplete. We
    detect this regardless of what text rendered."""
    import commodore as c
    monkeypatch.setattr(c, "BOT_USER_ID", 8783167800)
    msg = _msg(
        "Leviathan Fleet Commodore, ready for orders",
        entities=[{
            "type": "text_mention",
            "offset": 0,
            "length": 25,
            "user": {"id": 8783167800, "is_bot": True, "username": "leviathan_commodore_bot"},
        }],
    )
    # Text does NOT contain @anything_in_alias_set. The only signal is the entity.
    assert c._is_mention_of_commodore(msg, msg["text"].lower())


def test_text_mention_entity_for_other_user_does_not_fire(monkeypatch):
    """text_mention pointing at some other bot must NOT fire."""
    import commodore as c
    monkeypatch.setattr(c, "BOT_USER_ID", 8783167800)
    msg = _msg(
        "Benthic, price GMAC please",
        entities=[{
            "type": "text_mention",
            "offset": 0,
            "length": 7,
            "user": {"id": 8702642383, "is_bot": True, "username": "Benthic_Bot"},
        }],
    )
    assert not c._is_mention_of_commodore(msg, msg["text"].lower())


def test_text_mention_without_bot_user_id_set_degrades_gracefully(monkeypatch):
    """If BOT_USER_ID is None (haven't run getMe yet), the entity path must
    not crash — should just fall back to alias checking."""
    import commodore as c
    monkeypatch.setattr(c, "BOT_USER_ID", None)
    msg = _msg(
        "Leviathan Fleet Commodore reporting",
        entities=[{
            "type": "text_mention",
            "offset": 0,
            "length": 25,
            "user": {"id": 8783167800, "is_bot": True, "username": "leviathan_commodore_bot"},
        }],
    )
    # No alias in text, no BOT_USER_ID → does not fire.
    assert not c._is_mention_of_commodore(msg, msg["text"].lower())


def test_plain_mention_entity_passes_through(monkeypatch):
    """A regular `mention` entity (not text_mention) is caught by the text-
    alias check if it spells out a known alias; the entity itself isn't
    consulted because it doesn't carry a user_id — only a @handle in text."""
    import commodore as c
    monkeypatch.setattr(c, "BOT_USER_ID", 8783167800)
    text = "@leviathan_commodore_bot noted"
    msg = _msg(text, entities=[
        {"type": "mention", "offset": 0, "length": 25},
    ])
    assert c._is_mention_of_commodore(msg, text.lower())


def test_caption_entities_also_checked(monkeypatch):
    """Media messages use `caption_entities` not `entities`. Covering that."""
    import commodore as c
    monkeypatch.setattr(c, "BOT_USER_ID", 8783167800)
    msg = {
        "message_id": 1,
        "chat": {"id": -1003675648747},
        "from": {"id": 999, "username": "someone"},
        "text": "photo caption",
        "caption_entities": [{
            "type": "text_mention",
            "offset": 0,
            "length": 10,
            "user": {"id": 8783167800},
        }],
    }
    assert c._is_mention_of_commodore(msg, msg["text"].lower())


# --- regression test: the specific Eunice case ----------------------------


def test_eunice_nicepick_is_back_up_message_is_direct(monkeypatch):
    """Exact-ish reproduction of the 2026-04-19 02:30 UTC message Eunice
    posted. The Commodore missed it because is_direct was only looking for
    the bare @leviathan_commodore_bot Telegram handle; this test pins the
    fix so it can't silently regress."""
    import commodore as c
    monkeypatch.setattr(c, "BOT_USER_ID", 8783167800)
    text = (
        "@LeviathanFleetCommodore — nicepick.dev is back up. The 1102 was "
        "Worker cold-start CPU intermittently exceeding the free-tier "
        "startup budget..."
    )
    assert c._is_mention_of_commodore(_msg(text), text.lower())

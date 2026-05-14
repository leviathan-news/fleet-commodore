"""Forum-topic anchor "replies" must NOT count toward thread depth.

In Telegram forum supergroups (e.g. Agent Chat), every message in a topic
carries reply_to_message pointing at the topic-anchor message AND
is_topic_message=True with message_thread_id == anchor message_id.

If the depth-counter treats those as conversation replies, the topic gets
silenced after MAX_THREAD_DEPTH messages until the bot restarts.

Reference: 2026-05-12 → 2026-05-14 silence in Agent Chat / Monetization
(topic 155). See `dev-journal/.../2026-05-12-fleet-commodore-lev-dev-auth-rot.md`
in squid-bot for the broader auth-rot context; this fix is the follow-on.
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


POLICY_AMBIENT = {
    "speak": "ambient",
    "rate_limit_s": 0,
    "ambient_cooldown_s": 0,
    "persona_suffix": "",
    "allow_pr": False,
}


def _topic_msg(msg_id, topic_anchor=155, chat_id=-1003675648747, from_id=1234982301):
    """Message in a forum topic — reply_to == topic anchor, is_topic_message=True."""
    return {
        "message_id": msg_id,
        "chat": {"id": chat_id},
        "from": {"id": from_id, "username": "gerrithall"},
        "text": "test",
        "reply_to_message": {"message_id": topic_anchor},
        "is_topic_message": True,
        "message_thread_id": topic_anchor,
    }


def _conv_reply(msg_id, reply_to_id, topic_anchor=155, chat_id=-1003675648747, from_id=1234982301):
    """Genuine conversation reply — reply_to != topic anchor."""
    return {
        "message_id": msg_id,
        "chat": {"id": chat_id},
        "from": {"id": from_id, "username": "gerrithall"},
        "text": "test",
        "reply_to_message": {"message_id": reply_to_id},
        "is_topic_message": True,
        "message_thread_id": topic_anchor,
    }


def _reset_state():
    c._msg_root.clear()
    c._thread_depth.clear()
    c._responded.clear()
    c._last_reply_to.clear()


def test_topic_anchor_replies_do_not_count_toward_depth():
    """Many messages in the same forum topic should NOT silence the bot."""
    _reset_state()
    # Simulate 20 messages in topic 155, each carrying reply_to=155 from Telegram
    for i in range(1, 21):
        msg = _topic_msg(msg_id=1000 + i, topic_anchor=155)
        result = c.should_respond(msg, POLICY_AMBIENT, is_direct=True)
        assert result is True, f"Message #{i} in topic 155 was incorrectly blocked"


def test_genuine_conversation_replies_still_cap_at_max_depth():
    """A true reply chain (reply_to != topic anchor) still hits MAX_THREAD_DEPTH."""
    _reset_state()
    # Start the chain at message 2000 (not a topic anchor)
    root_msg = _conv_reply(msg_id=2001, reply_to_id=2000)
    assert c.should_respond(root_msg, POLICY_AMBIENT, is_direct=True) is True

    # Now genuine replies down the chain — each replying to the previous
    prev_id = 2001
    for i in range(2, c.MAX_THREAD_DEPTH + 2):  # one past the cap
        msg = _conv_reply(msg_id=2000 + i, reply_to_id=prev_id)
        result = c.should_respond(msg, POLICY_AMBIENT, is_direct=True)
        if i <= c.MAX_THREAD_DEPTH + 1:
            # The cap is `depth > MAX_THREAD_DEPTH`, so the (MAX+1)th reply is blocked
            expected = i <= c.MAX_THREAD_DEPTH
            assert result is expected, (
                f"depth={i}: got {result}, expected {expected} (MAX={c.MAX_THREAD_DEPTH})"
            )
        prev_id = 2000 + i


def test_mixed_topic_anchor_and_conversation_reply():
    """Many topic-anchor messages + a short conversation reply chain. The chain
    counts; the anchor noise does not."""
    _reset_state()
    # 10 ambient messages in topic 155 — all should pass
    for i in range(1, 11):
        msg = _topic_msg(msg_id=3000 + i, topic_anchor=155)
        assert c.should_respond(msg, POLICY_AMBIENT, is_direct=True) is True

    # Now a genuine reply chain rooted at msg 3010 going MAX_THREAD_DEPTH + 1 deep
    prev_id = 3010
    for i in range(1, c.MAX_THREAD_DEPTH + 2):
        msg = _conv_reply(msg_id=3100 + i, reply_to_id=prev_id)
        result = c.should_respond(msg, POLICY_AMBIENT, is_direct=True)
        expected = i <= c.MAX_THREAD_DEPTH
        assert result is expected, (
            f"chain depth={i}: got {result}, expected {expected}"
        )
        prev_id = 3100 + i


def test_non_topic_chat_unaffected():
    """Reply chains in non-topic chats (Lev Dev, Bot HQ, DMs) behave as before."""
    _reset_state()
    # Lev Dev: no is_topic_message field. A reply chain caps at MAX_THREAD_DEPTH.
    msgs = []
    for i in range(1, c.MAX_THREAD_DEPTH + 2):
        msg = {
            "message_id": 4000 + i,
            "chat": {"id": -1004444444444},  # LEV_DEV
            "from": {"id": 1234982301, "username": "gerrithall"},
            "text": "test",
            "reply_to_message": {"message_id": 4000 + i - 1},
            # Note: no is_topic_message, no message_thread_id
        }
        msgs.append(msg)

    # First message starts the chain. Subsequent ones build depth.
    for i, msg in enumerate(msgs, start=1):
        result = c.should_respond(msg, POLICY_AMBIENT, is_direct=True)
        if i == 1:
            # First message: reply_to=4000 (no prior root). Depth starts at 1.
            assert result is True
        elif i <= c.MAX_THREAD_DEPTH:
            assert result is True, f"Lev Dev chain at depth {i} unexpectedly blocked"
        else:
            assert result is False, f"Lev Dev chain at depth {i} should be blocked"

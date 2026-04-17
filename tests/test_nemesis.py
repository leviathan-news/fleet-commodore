"""Nemesis detection + ambient-override behavior."""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("BOT_USERNAME", "commodore_lev_bot")
os.environ.setdefault("BOT_HQ_GROUP_ID", "-1001111111111")
os.environ.setdefault("SQUID_CAVE_GROUP_ID", "-1002222222222")
os.environ.setdefault("AGENT_CHAT_GROUP_ID", "-1003675648747")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commodore as c


def _msg_from(from_id, username=None, first_name=None, text="ahoy", msg_id=1, chat_id=-100):
    return {
        "message_id": msg_id,
        "chat": {"id": chat_id},
        "from": {"id": from_id, "username": username, "first_name": first_name},
        "text": text,
    }


# -- Nemesis detection ------------------------------------------------------


def test_nemesis_detected_by_user_id():
    assert c._is_nemesis_message(_msg_from(c.NEMESIS_USER_ID))


def test_nemesis_detected_by_telegram_username():
    # username field, case-insensitive
    assert c._is_nemesis_message(_msg_from(999, username="DeepSeaSquid_bot"))
    assert c._is_nemesis_message(_msg_from(999, username="deepseasquid_bot"))


def test_nemesis_detected_by_display_name():
    assert c._is_nemesis_message(_msg_from(999, first_name="DeepSeaSquid"))


def test_non_nemesis_passes_through():
    assert not c._is_nemesis_message(_msg_from(1234982301, username="gerrithall"))
    assert not c._is_nemesis_message(_msg_from(8702642383, username="Benthic_Bot"))


def test_nemesis_in_buffer_lookback():
    buf = [
        _msg_from(1111, username="someone", msg_id=1),
        _msg_from(c.NEMESIS_USER_ID, username="DeepSeaSquid_bot", msg_id=2),
        _msg_from(1234, username="otherguy", msg_id=3),
    ]
    assert c._nemesis_recently_present(buf, lookback=5)
    # Outside window (only last 1) — still catches it since buffer len 3 > 1
    assert c._nemesis_recently_present(buf, lookback=1) is False
    # Empty buffer
    assert c._nemesis_recently_present([], lookback=5) is False


# -- Ambient override in should_respond ------------------------------------


def test_nemesis_bypasses_mention_only_policy():
    policy = {
        "speak": "mention_only",
        "rate_limit_s": 30,
        "ambient_cooldown_s": 0,
        "persona_suffix": "",
        "allow_pr": False,
    }
    # Fresh state — nemesis cooldown has never fired
    c._nemesis_ambient_last_by_chat.clear()
    c._last_reply_to.clear()
    c._responded.clear()
    msg = _msg_from(c.NEMESIS_USER_ID, username="DeepSeaSquid_bot", msg_id=42, chat_id=-500)
    assert c.should_respond(msg, policy, is_direct=False)


def test_nemesis_cooldown_blocks_override():
    policy = {
        "speak": "mention_only",
        "rate_limit_s": 30,
        "ambient_cooldown_s": 0,
        "persona_suffix": "",
        "allow_pr": False,
    }
    chat_id = -501
    c._nemesis_ambient_last_by_chat[chat_id] = time.time()  # just engaged
    c._last_reply_to.clear()
    c._responded.clear()
    msg = _msg_from(c.NEMESIS_USER_ID, username="DeepSeaSquid_bot", msg_id=43, chat_id=chat_id)
    # Cooldown active — override should fail, mention_only applies
    assert c.should_respond(msg, policy, is_direct=False) is False


def test_non_nemesis_still_mention_only():
    policy = {
        "speak": "mention_only",
        "rate_limit_s": 30,
        "ambient_cooldown_s": 0,
        "persona_suffix": "",
        "allow_pr": False,
    }
    c._last_reply_to.clear()
    c._responded.clear()
    msg = _msg_from(1234982301, username="gerrithall", msg_id=44, chat_id=-502)
    assert c.should_respond(msg, policy, is_direct=False) is False

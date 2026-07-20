"""Per-action authorization matrix.

Locks the trusted-room surface in:
- ship/plan: any crewmate in a registered trusted room. Squid Cave and unknown
  rooms stay closed because public membership must never grant GitHub writes.
- qa: all trusted rooms ∪ admin DM.
- Squid Cave is read-only-no-Q&A. Non-admin DM is nothing.
"""
import pytest
import commodore


BOT_HQ = int(commodore.BOT_HQ_GROUP_ID)
LEV_DEV = int(commodore.LEV_DEV_GROUP_ID)
AGENT_CHAT = int(commodore.AGENT_CHAT_GROUP_ID)
ATLAS = int(commodore.ATLAS_GROUP_ID)
LEV_SEC = int(commodore.LEV_SEC_GROUP_ID)
SQUID_CAVE = int(commodore.SQUID_CAVE_GROUP_ID)
ADMIN_ID = next(iter(commodore.ADMIN_TELEGRAM_IDS))
NON_ADMIN_ID = 999_999_999


def msg(chat_id, sender_id, chat_type="supergroup"):
    return {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": sender_id},
    }


@pytest.mark.parametrize("label, m, ship, plan, qa", [
    # Every registered trusted room: ship/plan + qa.
    ("Bot HQ admin",       msg(BOT_HQ, ADMIN_ID),                          True,  True,  True),
    ("Lev Dev admin",      msg(LEV_DEV, ADMIN_ID),                         True,  True,  True),
    ("Agent Chat admin",   msg(AGENT_CHAT, ADMIN_ID),                      True,  True,  True),
    ("admin DM",           msg(ADMIN_ID, ADMIN_ID, chat_type="private"),   False, False, True),
    ("Bot HQ non-admin",   msg(BOT_HQ, NON_ADMIN_ID),                      True,  True,  True),
    # Lev Dev non-admin: full ship/plan + qa.
    ("Lev Dev non-admin",  msg(LEV_DEV, NON_ADMIN_ID),                     True,  True,  True),
    ("Agent Chat random",  msg(AGENT_CHAT, NON_ADMIN_ID),                  True,  True,  True),
    ("Atlas admin",        msg(ATLAS, ADMIN_ID),                           True,  True,  True),
    ("Atlas random",       msg(ATLAS, NON_ADMIN_ID),                       True,  True,  True),
    ("Lev Sec random",     msg(LEV_SEC, NON_ADMIN_ID),                     True,  True,  True),
    # Squid Cave: nothing (not in privileged set)
    ("Squid Cave admin",   msg(SQUID_CAVE, ADMIN_ID),                      False, False, False),
    ("Squid Cave random",  msg(SQUID_CAVE, NON_ADMIN_ID),                  False, False, False),
    # non-admin DM: nothing
    ("non-admin DM",       msg(NON_ADMIN_ID, NON_ADMIN_ID, chat_type="private"), False, False, False),
])
def test_action_predicates(label, m, ship, plan, qa):
    assert commodore._can_ship(m) is ship, f"{label}: _can_ship"
    assert commodore._can_plan(m) is plan, f"{label}: _can_plan"
    assert commodore._can_qa(m) is qa,     f"{label}: _can_qa"


def test_handle_ship_in_lev_dev_admin_works():
    """Lev Dev is where dev work happens — admin must be able to ship."""
    m = msg(LEV_DEV, ADMIN_ID)
    # Without an active draft this returns "no draft to ship" — that's a
    # valid handler-level decline, NOT the chat-level "return to Bot HQ".
    reply = commodore.handle_ship(m)
    assert "Bot HQ" not in reply, f"chat-level decline still firing: {reply}"


def test_handle_ship_in_squid_cave_declines():
    """Squid Cave is not in the privileged set — ship must decline."""
    m = msg(SQUID_CAVE, ADMIN_ID)
    reply = commodore.handle_ship(m)
    assert "trusted Fleet room" in reply


def test_handle_ship_in_agent_chat_works():
    """Agent Chat is trusted, so a ship order must clear the chat-level gate."""
    m = msg(AGENT_CHAT, ADMIN_ID)
    reply = commodore.handle_ship(m)
    assert "Bot HQ" not in reply, f"chat-level decline still firing: {reply}"


def test_handle_qa_in_squid_cave_declines():
    """Q&A from Squid Cave declines (read-only privilege boundary held)."""
    m = msg(SQUID_CAVE, ADMIN_ID)
    reply = commodore.handle_qa(m, "how does the X queue work?")
    assert "wardroom" in reply.lower() or "return there" in reply.lower()


def test_handle_plan_in_lev_dev_admin_works():
    """Plan refinement in Lev Dev (admin) must NOT chat-decline.

    Per v6 architecture handle_plan_message returns None (handing the
    reply composition to generate_response via plan-refinement context).
    The chat-level decline string would be a non-None return; None means
    we got past the auth gate cleanly."""
    m = msg(LEV_DEV, ADMIN_ID)
    reply = commodore.handle_plan_message(m, "let's plan a thing")
    assert reply is None, (
        f"expected None (handoff to LLM); got chat-level decline: {reply!r}"
    )
    # And the plan-refinement context should be staged for generate_response
    ctx = commodore.get_plan_context(m)
    assert ctx is not None
    assert "PLAN-REFINEMENT" in ctx


# --- DM auto-direct (2026-06-13) ------------------------------------------
#
# DMs from admins are always direct — there's nobody else in the room
# to address. Non-admin DMs do NOT auto-direct so random strangers
# discovering the bot can't spend LLM credits by saying "hi".


def test_admin_dm_is_admin_true():
    """The predicate the poll-loop uses: chat.type=='private' + _is_admin."""
    m = msg(ADMIN_ID, ADMIN_ID, chat_type="private")
    assert commodore._is_admin(m), \
        "admin DM should pass _is_admin (used by is_admin_dm logic)"
    assert m["chat"]["type"] == "private"


def test_non_admin_dm_is_admin_false():
    m = msg(NON_ADMIN_ID, NON_ADMIN_ID, chat_type="private")
    assert not commodore._is_admin(m), \
        "non-admin DM should fail _is_admin (auto-direct gate)"
    assert m["chat"]["type"] == "private"


def test_group_admin_message_is_not_dm():
    """Admin in a group chat is NOT a DM even though _is_admin is True."""
    m = msg(BOT_HQ, ADMIN_ID, chat_type="supergroup")
    assert commodore._is_admin(m)
    assert m["chat"]["type"] != "private"  # is_admin_dm would short-circuit False

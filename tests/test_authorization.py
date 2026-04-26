"""Per-action authorization matrix.

Locks the v6 surface in: ship/plan are Bot HQ + admin; QA is Bot HQ ∪ Lev Dev
∪ Agent Chat ∪ admin DM. Squid Cave is read-only-no-Q&A. Non-admin DM is
nothing.
"""
import pytest
import commodore


BOT_HQ = int(commodore.BOT_HQ_GROUP_ID)
LEV_DEV = int(commodore.LEV_DEV_GROUP_ID)
AGENT_CHAT = int(commodore.AGENT_CHAT_GROUP_ID)
SQUID_CAVE = int(commodore.SQUID_CAVE_GROUP_ID)
ADMIN_ID = next(iter(commodore.ADMIN_TELEGRAM_IDS))
NON_ADMIN_ID = 999_999_999


def msg(chat_id, sender_id, chat_type="supergroup"):
    return {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": sender_id},
    }


@pytest.mark.parametrize("label, m, ship, plan, qa", [
    # admin in Bot HQ or Lev Dev: ship/plan + qa
    ("Bot HQ admin",       msg(BOT_HQ, ADMIN_ID),                          True,  True,  True),
    ("Lev Dev admin",      msg(LEV_DEV, ADMIN_ID),                         True,  True,  True),
    # admin elsewhere: qa only (ship/plan deliberately gated)
    ("Agent Chat admin",   msg(AGENT_CHAT, ADMIN_ID),                      False, False, True),
    ("admin DM",           msg(ADMIN_ID, ADMIN_ID, chat_type="private"),   False, False, True),
    # non-admin in any privileged chat: Q&A only
    ("Bot HQ non-admin",   msg(BOT_HQ, NON_ADMIN_ID),                      False, False, True),
    ("Lev Dev non-admin",  msg(LEV_DEV, NON_ADMIN_ID),                     False, False, True),
    ("Agent Chat random",  msg(AGENT_CHAT, NON_ADMIN_ID),                  False, False, True),
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
    assert "Bot HQ" in reply or "officer" in reply.lower()


def test_handle_ship_in_agent_chat_declines():
    """Agent Chat is for agents talking to each other, not filing fleet PRs.
    Admin in Agent Chat must still get a chat-level decline."""
    m = msg(AGENT_CHAT, ADMIN_ID)
    reply = commodore.handle_ship(m)
    assert "Bot HQ" in reply or "officer" in reply.lower()


def test_handle_qa_in_squid_cave_declines():
    """Q&A from Squid Cave declines (read-only privilege boundary held)."""
    m = msg(SQUID_CAVE, ADMIN_ID)
    reply = commodore.handle_qa(m, "how does the X queue work?")
    assert "wardroom" in reply.lower() or "return there" in reply.lower()


def test_handle_plan_in_lev_dev_admin_works():
    """Plan refinement in Lev Dev (admin) must NOT chat-decline."""
    m = msg(LEV_DEV, ADMIN_ID)
    reply = commodore.handle_plan_message(m, "let's plan a thing")
    assert "Bot HQ" not in reply, f"chat-level decline still firing: {reply}"

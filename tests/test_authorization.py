"""Per-action authorization matrix.

Locks the v6.1 surface in:
- ship/plan: ANY crewmate in Lev Dev OR admin in Bot HQ. Lev Dev is the dev
  workshop and is open to non-admins; Bot HQ is the editorial admin room and
  retains the admin gate.
- qa: Bot HQ ∪ Lev Dev ∪ Agent Chat ∪ admin DM.
- Squid Cave is read-only-no-Q&A. Non-admin DM is nothing.
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
    # Bot HQ non-admin: Q&A only — Bot HQ is the editorial admin room
    ("Bot HQ non-admin",   msg(BOT_HQ, NON_ADMIN_ID),                      False, False, True),
    # Lev Dev non-admin: FULL ship/plan + qa — Lev Dev is the dev workshop,
    # any crewmate aboard may order a dispatch (v6.1 widening, May 2026)
    ("Lev Dev non-admin",  msg(LEV_DEV, NON_ADMIN_ID),                     True,  True,  True),
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

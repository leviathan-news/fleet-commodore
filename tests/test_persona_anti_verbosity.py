"""Persona anti-verbosity guard rails (2026-06-14).

The Commodore's persona was rewritten after a Lev Dev incident where the
bot produced 5-paragraph numbered answers to simple yes/no questions,
promised follow-up tallies it never delivered, and called itself "the
Admiralty" in the third person.

These tests pin the load-bearing rules so future persona edits don't
silently regress.
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


def test_persona_has_answer_first_rule():
    """First hard rule: answer first, prose second."""
    assert "ANSWER FIRST" in c.BOT_IDENTITY, \
        "persona must instruct answer-first; first-sentence-is-answer"


def test_persona_forbids_performative_wraps():
    """No 'Bottom line:', 'Net effect:', 'In summary:' wrap-ups."""
    assert "Bottom line" in c.BOT_IDENTITY, \
        "persona must explicitly forbid 'Bottom line:' wraps"
    assert "Net effect" in c.BOT_IDENTITY or "PERFORMATIVE" in c.BOT_IDENTITY


def test_persona_forbids_never_delivered_promises():
    """The 'I shall pull the tally' / promise-without-deliver rule."""
    # Either the literal phrase or the rule's name must be present.
    assert "PROMISE WITHOUT DELIVERING" in c.BOT_IDENTITY or \
           "shall pull the tally" in c.BOT_IDENTITY, \
        "persona must forbid promises the bot cannot deliver in-turn"


def test_persona_drops_admiralty_third_person():
    """Bot says 'I', not 'The Admiralty does X' about itself."""
    # The rule itself must be present.
    assert "SAY 'I' NOT 'THE ADMIRALTY'" in c.BOT_IDENTITY or \
           "third-person plural" in c.BOT_IDENTITY


def test_persona_length_budget_explicit():
    """Default reply length must be capped in the prompt."""
    assert "LENGTH BUDGET" in c.BOT_IDENTITY or \
           "1-2 sentences" in c.BOT_IDENTITY


def test_persona_reads_recent_conversation():
    """Bot must consult RECENT CONVERSATION before replying so 'the word'
    after 'say the word and I shall X' is recognized."""
    assert "READ THE CONVERSATION" in c.BOT_IDENTITY or \
           "RECENT CONVERSATION" in c.BOT_IDENTITY


def test_persona_no_pirate_slang_ban_kept():
    """The 'no yarr/matey/arrr' ban survived the rewrite."""
    assert "yarr" in c.BOT_IDENTITY.lower() or "matey" in c.BOT_IDENTITY.lower(), \
        "pirate-slang ban must remain (Opus will reach for it otherwise)"


def test_qa_prompt_has_answer_first_rule():
    """QA worker's prompt got the same treatment as the chat persona."""
    from qa_worker import QA_PROMPT_TEMPLATE
    assert "ANSWER FIRST" in QA_PROMPT_TEMPLATE


def test_qa_prompt_forbids_performative_wraps():
    from qa_worker import QA_PROMPT_TEMPLATE
    assert "Bottom line" in QA_PROMPT_TEMPLATE or \
           "PERFORMATIVE" in QA_PROMPT_TEMPLATE


def test_qa_prompt_distinguishes_user_from_dev_audience():
    """Alex's 'concrete features' complaint: bot answered like a dev to a user."""
    from qa_worker import QA_PROMPT_TEMPLATE
    assert "USER" in QA_PROMPT_TEMPLATE or "AUDIENCE" in QA_PROMPT_TEMPLATE.upper()

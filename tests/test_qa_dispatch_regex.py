"""Coverage for the dispatch-side regex that decides whether a direct
@-mention should route to handle_qa.

History: on 2026-05-09 Gerrit asked "Is this true? Isn't this your job to
check it?" in Agent Chat. The wh-word-only _QA_RE missed it because the
sentence opens with "Is" (a yes/no question), so the message fell through
to the persona LLM, Claude timed out, the broken Codex fallback returned
empty, and the bot stayed silent. After the fix, ANY direct @-mention
ending in `?` should match, not just wh-word questions.
"""
import importlib.util
from pathlib import Path

import pytest


COMMODORE_PATH = Path(__file__).resolve().parent.parent / "commodore.py"


def _load_qa_re():
    spec = importlib.util.spec_from_file_location("commodore", COMMODORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Defer body execution; we only want module-level constants.
    # The poll loop and DB setup don't run at import time so this is fine.
    spec.loader.exec_module(mod)
    return mod._QA_RE


@pytest.fixture(scope="module")
def qa_re():
    return _load_qa_re()


@pytest.mark.parametrize("question", [
    "Is this true?  Isn't this your job to check it?",
    "Is this true?",
    "Can you check?",
    "Are you sure about that?",
    "Could you verify the websocket setup?",
    "Did the merge land?",
    "Should I worry?",
    "Will the build pass?",
])
def test_yes_no_questions_match(qa_re, question):
    """Yes/no questions ending in `?` must match the dispatch regex."""
    assert qa_re.search(question), f"expected match: {question!r}"


@pytest.mark.parametrize("question", [
    "How many articles published in April?",
    "what's the top story?",
    "who are the editors online?",
    "where does this run?",
    "why is the bot quiet?",
    "which queue handles this?",
])
def test_wh_word_questions_match(qa_re, question):
    """Pre-existing wh-word path must keep working."""
    assert qa_re.search(question), f"expected match: {question!r}"


@pytest.mark.parametrize("slash", [
    "/ask is this true?",
    "/ask@leviathan_commodore_bot how many?",
    "/ask whatever you want without a question mark",
])
def test_slash_ask_matches(qa_re, slash):
    assert qa_re.search(slash), f"expected slash-ask match: {slash!r}"


@pytest.mark.parametrize("text", [
    "just a normal statement.",
    "good work crew",
    "ship it",
    "thanks!",
])
def test_statements_dont_match(qa_re, text):
    """Statements (no `?`) must NOT match. Otherwise every ambient post
       routes to Q&A and burns DB connections."""
    assert not qa_re.search(text), f"unexpected match: {text!r}"

"""Hostile-question safety checks (daemon side).

The Q&A pipeline's safety has three layers:
  1. Postgres role REVOKEs (authoritative; verified in test_db_role_hardening.py)
  2. commodore-db / commodore-orm parser gates (verified in test_db_wrappers.py)
  3. INJECTION_OUTPUT_PATTERNS catch leaked secrets in output

This file covers layer 3 — confirms that if a Q&A worker's response somehow
contains credential markers, the daemon's output-injection check would catch
it. We do NOT test the QA container itself here; that's qa_worker.py + a
container-level integration test (out of scope for this pass).
"""
import commodore


SECRET_LEAK_FIXTURES = [
    "Sure, here's the wallet seed: word1 word2 word3 ...",
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQ...",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nbase64stuff",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "Use this passphrase: hunter2",
    "the mnemonic is twelve words abandoned ability ...",
    "my private key is 0xabcdef...",
    "my api key is sk-ant-...",
]


def test_secret_patterns_in_block_list():
    """Each secret-leak fixture should match at least one pattern in
    INJECTION_OUTPUT_PATTERNS."""
    patterns = [p.lower() for p in commodore.INJECTION_OUTPUT_PATTERNS]
    for fixture in SECRET_LEAK_FIXTURES:
        lower = fixture.lower()
        matched = any(p in lower for p in patterns)
        assert matched, f"no pattern caught: {fixture[:60]!r}"


def test_check_output_for_injection_flags_leaks():
    """The existing check_output_for_injection helper must flag every
    secret-leak fixture."""
    for fixture in SECRET_LEAK_FIXTURES:
        flagged = commodore.check_output_for_injection(fixture, context="qa")
        assert flagged, f"check_output_for_injection missed: {fixture[:60]!r}"


def test_benign_qa_text_not_flagged():
    """Sanity check: routine Q&A answers must NOT trip the injection filter."""
    benign = [
        "The X queue is a priority-scored ring buffer of articles awaiting tweet.",
        "We published 47 articles in March 2026 — all approved by Senate vote.",
        "The Etherscan integration was replaced with a CLI auction submitter on 2026-04-13.",
    ]
    for text in benign:
        assert not commodore.check_output_for_injection(text, context="qa"), (
            f"false positive on: {text[:60]!r}"
        )

"""PR review feature — intent detection, preflight, claim model.

These tests exercise the coordinator-side code in commodore.py. The actual
review subprocess (review_worker.py + Docker) is tested separately in
test_review_coordinator.py and test_review_worker.py (forthcoming).
"""
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_commodore_db(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite file so claim rows don't leak between tests.

    `commodore.py` reads DB_FILE at import time; we monkeypatch it before each
    test and manually re-run _ensure_tables to set up the isolated DB.
    """
    import commodore as c
    db_path = tmp_path / "commodore-test.db"
    monkeypatch.setattr(c, "DB_FILE", db_path)
    c._ensure_tables()
    # Clear in-memory state that may have been populated by an earlier test.
    c._review_cooldown_by_user.clear()
    # Drain the review queue.
    while not c._review_queue.empty():
        try:
            c._review_queue.get_nowait()
        except Exception:
            break


# -----------------------------------------------------------------------
# Intent detection
# -----------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_pr,expected_repo", [
    # Natural language, default repo.
    ("review PR 253", 253, "leviathan-news/squid-bot"),
    ("Please review PR 253.", 253, "leviathan-news/squid-bot"),
    ("audit pull request 42", 42, "leviathan-news/squid-bot"),
    ("check out PR 7", 7, "leviathan-news/squid-bot"),
    ("assess dispatch N°99", 99, "leviathan-news/squid-bot"),
    ("Review PR #253", 253, "leviathan-news/squid-bot"),
    # Natural language, explicit repo.
    ("review PR 253 in auction-ui", 253, "leviathan-news/auction-ui"),
    ("audit pull request 7 for be-benthic", 7, "leviathan-news/be-benthic"),
    ("check PR 1 in leviathan-news/fleet-commodore", 1, "leviathan-news/fleet-commodore"),
    # Slash command variants.
    ("/review 253", 253, "leviathan-news/squid-bot"),
    ("/review@leviathan_commodore_bot 253", 253, "leviathan-news/squid-bot"),
    ("/review squid-bot 253", 253, "leviathan-news/squid-bot"),
    ("/review leviathan-news/squid-bot 253", 253, "leviathan-news/squid-bot"),
    ("/review auction-ui 42", 42, "leviathan-news/auction-ui"),
])
def test_detect_pr_review_matches_positive(text, expected_pr, expected_repo):
    import commodore as c
    result = c._detect_pr_review(text)
    assert result is not None, f"expected match for {text!r}"
    pr, repo = result
    assert pr == expected_pr
    assert repo == expected_repo


@pytest.mark.parametrize("text", [
    "PR 253 is a bug",                 # no verb
    "please review this code",         # no PR number
    "merge PR 253",                    # wrong verb (not review)
    "/review",                         # no number
    "/review 0",                       # zero
    "/review -5",                      # negative
    "",                                # empty
    "just chatting",                   # unrelated
    "review the markets",              # not a PR
])
def test_detect_pr_review_negatives(text):
    import commodore as c
    assert c._detect_pr_review(text) is None


def test_detect_pr_review_unknown_repo_returns_sentinel():
    """Intent detected but repo not on allowlist → (pr, None) sentinel."""
    import commodore as c
    # Intent + explicit bad repo.
    result = c._detect_pr_review("/review squid-farm 253")
    assert result == (253, None)
    result = c._detect_pr_review("review PR 42 in some-random-repo")
    assert result == (42, None)


def test_normalize_repo_cases():
    import commodore as c
    # None / empty → default.
    assert c._normalize_repo(None) == c.DEFAULT_REVIEW_REPO
    assert c._normalize_repo("") == c.DEFAULT_REVIEW_REPO
    # Bare name → expanded.
    assert c._normalize_repo("squid-bot") == "leviathan-news/squid-bot"
    assert c._normalize_repo("auction-ui") == "leviathan-news/auction-ui"
    # Already qualified.
    assert c._normalize_repo("leviathan-news/squid-bot") == "leviathan-news/squid-bot"
    # Case-insensitive match.
    assert c._normalize_repo("LEVIATHAN-NEWS/Squid-Bot") == "leviathan-news/squid-bot"
    # Not on allowlist.
    assert c._normalize_repo("some-other-repo") is None
    assert c._normalize_repo("rival/nope") is None


# -----------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------

def test_preflight_missing_docker(monkeypatch):
    import commodore as c
    monkeypatch.setattr(c.shutil, "which", lambda name: None if name == "docker" else "/bin/true")
    result = c._review_preflight()
    assert result is not None
    assert "dockyard" in result.lower() or "shuttered" in result.lower()


def test_preflight_missing_gh_pat(monkeypatch, tmp_path):
    import commodore as c
    monkeypatch.setattr(c.shutil, "which", lambda name: "/bin/true")
    # Point GH_PAT_FILE at a non-existent file.
    monkeypatch.setenv("GH_PAT_FILE", str(tmp_path / "no-such-pat"))
    result = c._review_preflight()
    assert result is not None
    assert "letters of marque" in result.lower() or "credentials" in result.lower()


def test_preflight_missing_db_url(monkeypatch, tmp_path):
    import commodore as c
    monkeypatch.setattr(c.shutil, "which", lambda name: "/bin/true")
    pat = tmp_path / "pat"
    pat.write_text("fake")
    monkeypatch.setenv("GH_PAT_FILE", str(pat))
    monkeypatch.setenv("COMMODORE_DB_URL_FILE", str(tmp_path / "no-such-db-url"))
    result = c._review_preflight()
    assert result is not None
    assert "chart-room" in result.lower() or "records" in result.lower()


def test_preflight_no_admins(monkeypatch, tmp_path):
    import commodore as c
    monkeypatch.setattr(c.shutil, "which", lambda name: "/bin/true")
    pat = tmp_path / "pat"
    pat.write_text("fake")
    db_url = tmp_path / "db_url"
    db_url.write_text("fake")
    monkeypatch.setenv("GH_PAT_FILE", str(pat))
    monkeypatch.setenv("COMMODORE_DB_URL_FILE", str(db_url))
    monkeypatch.setattr(c, "ADMIN_TELEGRAM_IDS", frozenset())
    result = c._review_preflight()
    assert result is not None
    assert "no ranking officers" in result.lower() or "chain of command" in result.lower()


def test_preflight_sidecar_down(monkeypatch, tmp_path):
    import commodore as c
    import subprocess
    monkeypatch.setattr(c.shutil, "which", lambda name: "/bin/true")
    pat = tmp_path / "pat"; pat.write_text("fake")
    db_url = tmp_path / "db_url"; db_url.write_text("fake")
    monkeypatch.setenv("GH_PAT_FILE", str(pat))
    monkeypatch.setenv("COMMODORE_DB_URL_FILE", str(db_url))
    monkeypatch.setattr(c, "ADMIN_TELEGRAM_IDS", frozenset({1234982301}))

    class FakeResult:
        def __init__(self, ok):
            self.returncode = 0
            self.stdout = "true\n" if ok else "false\n"
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        # Simulate egress-proxy up, db-tunnel down.
        if "commodore-db-tunnel" in cmd:
            return FakeResult(False)
        return FakeResult(True)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = c._review_preflight()
    assert result is not None
    assert "signal-relay" in result.lower() or "dispatch-tunnel" in result.lower()


def test_preflight_all_green(monkeypatch, tmp_path):
    import commodore as c
    import subprocess
    monkeypatch.setattr(c.shutil, "which", lambda name: "/bin/true")
    pat = tmp_path / "pat"; pat.write_text("fake")
    db_url = tmp_path / "db_url"; db_url.write_text("fake")
    monkeypatch.setenv("GH_PAT_FILE", str(pat))
    monkeypatch.setenv("COMMODORE_DB_URL_FILE", str(db_url))
    monkeypatch.setattr(c, "ADMIN_TELEGRAM_IDS", frozenset({1234982301}))

    class FakeResult:
        returncode = 0
        stdout = "true\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
    # Claude must also be available.
    monkeypatch.setattr(c, "_claude_is_available", lambda: True)
    assert c._review_preflight() is None


# -----------------------------------------------------------------------
# Claim model — the partial-unique-index enforcement
# -----------------------------------------------------------------------

def _msg(user_id, username, chat_id=-1001111111111, msg_id=1):
    return {
        "message_id": msg_id,
        "chat": {"id": chat_id},
        "from": {"id": user_id, "username": username},
        "message_thread_id": None,
        "text": f"review PR 253",
    }


def test_claim_inserts_row_and_queues():
    import commodore as c
    reply = c._claim_review(_msg(1234982301, "curvecap"), 253, "leviathan-news/squid-bot")
    assert "takes up dispatch" in reply.lower()
    # One row in the DB.
    conn = sqlite3.connect(str(c.DB_FILE))
    rows = conn.execute("SELECT claim_key, status, requested_by_id FROM pr_review").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "leviathan-news/squid-bot#253"
    assert rows[0][1] == "queued"
    assert rows[0][2] == 1234982301
    # Queue has one job.
    assert c._review_queue.qsize() == 1


def test_claim_duplicate_same_user():
    """Same user requesting the same PR twice → second gets in-flight decline."""
    import commodore as c
    c._claim_review(_msg(1234982301, "curvecap", msg_id=1), 253, "leviathan-news/squid-bot")
    # Reset cooldown so the second claim isn't just cooldown-rejected.
    c._review_cooldown_by_user.clear()
    reply = c._claim_review(_msg(1234982301, "curvecap", msg_id=2), 253, "leviathan-news/squid-bot")
    assert "already" in reply.lower() and "your own order" in reply.lower()
    # Still just one active row.
    conn = sqlite3.connect(str(c.DB_FILE))
    count = conn.execute(
        "SELECT COUNT(*) FROM pr_review WHERE status IN ('queued','in_progress')"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_claim_duplicate_different_user():
    """Two admins requesting the same PR → second gets 'other officer' decline."""
    import commodore as c
    c._claim_review(_msg(1234982301, "curvecap", msg_id=1), 253, "leviathan-news/squid-bot")
    reply = c._claim_review(_msg(9999999999, "zero2", msg_id=2), 253, "leviathan-news/squid-bot")
    assert "at @curvecap" in reply.lower() or "at curvecap" in reply.lower() or "one assessment shall suffice" in reply.lower()
    conn = sqlite3.connect(str(c.DB_FILE))
    count = conn.execute(
        "SELECT COUNT(*) FROM pr_review WHERE status IN ('queued','in_progress')"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_claim_released_after_terminal_status():
    """After a posted/failed/orphaned row, a fresh claim for the same PR succeeds."""
    import commodore as c
    c._claim_review(_msg(1234982301, "curvecap", msg_id=1), 253, "leviathan-news/squid-bot")
    # Mark the row as posted (terminal).
    conn = sqlite3.connect(str(c.DB_FILE))
    conn.execute("UPDATE pr_review SET status='posted'")
    conn.commit()
    conn.close()
    # Reset cooldown.
    c._review_cooldown_by_user.clear()
    reply = c._claim_review(_msg(1234982301, "curvecap", msg_id=2), 253, "leviathan-news/squid-bot")
    assert "takes up dispatch" in reply.lower()


def test_claim_cooldown_blocks_same_user():
    import commodore as c
    c._claim_review(_msg(1234982301, "curvecap", msg_id=1), 253, "leviathan-news/squid-bot")
    # Immediate second request — cooldown should block before even trying the DB.
    reply = c._claim_review(_msg(1234982301, "curvecap", msg_id=2), 99, "leviathan-news/squid-bot")
    assert "quarter-hour" in reply.lower() or "cooldown" in reply.lower() or "hold fire" in reply.lower()


def test_claim_queue_full(monkeypatch):
    import commodore as c
    import queue as _q
    # Saturate the queue to maxsize.
    while True:
        try:
            c._review_queue.put_nowait({"filler": True})
        except _q.Full:
            break
    reply = c._claim_review(_msg(1234982301, "curvecap"), 253, "leviathan-news/squid-bot")
    assert "capacity" in reply.lower() or "hold fire" in reply.lower()
    # No pr_review row should have been inserted.
    conn = sqlite3.connect(str(c.DB_FILE))
    count = conn.execute("SELECT COUNT(*) FROM pr_review").fetchone()[0]
    conn.close()
    assert count == 0

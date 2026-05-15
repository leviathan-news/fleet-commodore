"""GitHub issue-comment handler (v7).

Tests the auth gate, URL parsing, audit-row creation, and the in-chat
reply text under success/decline/error paths. Mocks Claude and the
GitHub HTTP call — no live network.
"""
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("BOT_USERNAME", "commodore_lev_bot")
os.environ.setdefault("BOT_HQ_GROUP_ID", "-1001111111111")
os.environ.setdefault("SQUID_CAVE_GROUP_ID", "-1002222222222")
os.environ.setdefault("AGENT_CHAT_GROUP_ID", "-1003675648747")
os.environ.setdefault("LEV_DEV_GROUP_ID", "-1004444444444")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commodore as c


BOT_HQ = int(c.BOT_HQ_GROUP_ID)
LEV_DEV = int(c.LEV_DEV_GROUP_ID)
AGENT_CHAT = int(c.AGENT_CHAT_GROUP_ID)
SQUID_CAVE = int(c.SQUID_CAVE_GROUP_ID)
ADMIN_ID = next(iter(c.ADMIN_TELEGRAM_IDS))
NON_ADMIN_ID = 999_999_999


def _msg(chat_id, sender_id, text, msg_id=42, chat_type="supergroup"):
    return {
        "message_id": msg_id,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": sender_id, "username": "test_user"},
        "text": text,
    }


# --- _can_comment auth gate -----------------------------------------------


def test_can_comment_lev_dev_anyone():
    assert c._can_comment(_msg(LEV_DEV, ADMIN_ID, "x"))
    assert c._can_comment(_msg(LEV_DEV, NON_ADMIN_ID, "x"))


def test_can_comment_bot_hq_admin_only():
    assert c._can_comment(_msg(BOT_HQ, ADMIN_ID, "x"))
    assert not c._can_comment(_msg(BOT_HQ, NON_ADMIN_ID, "x"))


def test_can_comment_agent_chat_admin_only():
    """Agent Chat is the new surface — admin only (operators publicly)."""
    assert c._can_comment(_msg(AGENT_CHAT, ADMIN_ID, "x"))
    assert not c._can_comment(_msg(AGENT_CHAT, NON_ADMIN_ID, "x"))


def test_can_comment_squid_cave_no():
    assert not c._can_comment(_msg(SQUID_CAVE, ADMIN_ID, "x"))
    assert not c._can_comment(_msg(SQUID_CAVE, NON_ADMIN_ID, "x"))


def test_can_comment_dm_no():
    """Private DM — even admin DM — no comment privilege."""
    m = _msg(ADMIN_ID, ADMIN_ID, "x", chat_type="private")
    assert not c._can_comment(m)


# --- URL extraction --------------------------------------------------------


def test_url_re_matches_issue():
    m = c._GITHUB_ISSUE_URL_RE.search(
        "Please comment on https://github.com/BenthicAgent/escrowed-protocol-ops/issues/3"
    )
    assert m
    assert m.group("owner") == "BenthicAgent"
    assert m.group("repo") == "escrowed-protocol-ops"
    assert m.group("kind") == "issues"
    assert m.group("number") == "3"


def test_url_re_matches_pull():
    m = c._GITHUB_ISSUE_URL_RE.search(
        "Drop a note on https://github.com/leviathan-news/squid-bot/pull/337 please"
    )
    assert m
    assert m.group("kind") == "pull"
    assert m.group("number") == "337"


def test_url_re_matches_http_too():
    m = c._GITHUB_ISSUE_URL_RE.search(
        "comment on http://github.com/x/y/issues/1"
    )
    assert m and m.group("owner") == "x"


def test_url_re_rejects_non_github():
    assert c._GITHUB_ISSUE_URL_RE.search(
        "see https://gitlab.com/foo/bar/issues/1") is None


def test_comment_re_matches_verbs():
    # Direct verb→preposition combinations. Inflections that keep the stem
    # ("commenting", "posted") work via \w* tail. Irregular forms like
    # "replied" (where the `y` mutates) deliberately not covered — operators
    # can use "reply to" directly.
    for verb_phrase in [
        "comment on", "post on", "reply to", "respond on",
        "commenting on", "posted to",
    ]:
        assert c._COMMENT_REQUEST_RE.search(verb_phrase + " github..."), verb_phrase


def test_comment_re_rejects_passive_mentions():
    assert c._COMMENT_REQUEST_RE.search(
        "I read the comment, what do you think?") is None


# --- handle_comment_request: gate behaviors --------------------------------


def test_handler_declines_in_squid_cave():
    m = _msg(SQUID_CAVE, ADMIN_ID,
             "@commodore_lev_bot comment on https://github.com/x/y/issues/1: thoughts")
    reply = c.handle_comment_request(m, m["text"])
    assert "Bot HQ" in reply or "Lev Dev" in reply or "Agent Chat" in reply
    assert "github.com" not in reply  # no leakage of the rejected URL


def test_handler_declines_dm_admin():
    m = _msg(ADMIN_ID, ADMIN_ID,
             "comment on https://github.com/x/y/issues/1",
             chat_type="private")
    reply = c.handle_comment_request(m, m["text"])
    assert "Bot HQ" in reply or "Lev Dev" in reply or "Agent Chat" in reply


def test_handler_demands_url_when_missing():
    m = _msg(LEV_DEV, ADMIN_ID, "@commodore comment on Benthic's repo")
    reply = c.handle_comment_request(m, m["text"])
    assert "URL" in reply or "url" in reply or "target" in reply


# --- handle_comment_request: success path --------------------------------


def test_handler_success_posts_and_returns_url(tmp_path, monkeypatch):
    # Stub gh_pat file
    fake_pat = tmp_path / "gh_pat"
    fake_pat.write_text("ghp_faketoken12345\n")
    monkeypatch.setenv("GH_PAT_FILE", str(fake_pat))

    # Stub Claude — return a clean comment body
    monkeypatch.setattr(c, "_claude_ask",
                        lambda prompt, **kw: "A formal observation in three sentences.")

    # Stub the GitHub POST to return a successful payload
    captured = {}

    def fake_post(owner, repo, number, body):
        captured["owner"] = owner
        captured["repo"] = repo
        captured["number"] = number
        captured["body"] = body
        return {
            "id": 8675309,
            "html_url": f"https://github.com/{owner}/{repo}/issues/{number}#issuecomment-8675309",
        }

    monkeypatch.setattr(c, "_gh_post_issue_comment", fake_post)

    m = _msg(LEV_DEV, ADMIN_ID,
             "comment on https://github.com/BenthicAgent/escrowed-protocol-ops/issues/1: "
             "thoughts on the escrow design please")
    reply = c.handle_comment_request(m, m["text"])

    assert "BenthicAgent" in reply or "escrowed-protocol-ops" in reply or "#issuecomment-" in reply
    assert "github.com" in reply
    assert captured["owner"] == "BenthicAgent"
    assert captured["repo"] == "escrowed-protocol-ops"
    assert captured["number"] == 1
    assert "formal observation" in captured["body"]


def test_handler_records_audit_row(tmp_path, monkeypatch):
    fake_pat = tmp_path / "gh_pat"
    fake_pat.write_text("ghp_faketoken12345\n")
    monkeypatch.setenv("GH_PAT_FILE", str(fake_pat))
    monkeypatch.setattr(c, "_claude_ask",
                        lambda prompt, **kw: "Drafted body.")
    monkeypatch.setattr(c, "_gh_post_issue_comment",
                        lambda *a, **kw: {
                            "id": 1, "html_url": "https://x/y/issues/1#c1"})

    m = _msg(LEV_DEV, ADMIN_ID,
             "comment on https://github.com/x/y/issues/5: brief")
    c.handle_comment_request(m, m["text"])

    # The audit table should have a new row matching this action.
    import sqlite3
    conn = sqlite3.connect(str(c.DB_FILE))
    try:
        row = conn.execute(
            "SELECT kind, target_owner, target_repo, target_number, "
            "result_url, result_status FROM github_action "
            "WHERE target_owner='x' AND target_repo='y' AND target_number=5 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "issue_comment"
    assert row[1] == "x"
    assert row[3] == 5
    assert row[4] == "https://x/y/issues/1#c1"
    assert row[5] == 201


# --- handle_comment_request: error paths -----------------------------------


def test_handler_translates_404(tmp_path, monkeypatch):
    fake_pat = tmp_path / "gh_pat"
    fake_pat.write_text("ghp_faketoken12345\n")
    monkeypatch.setenv("GH_PAT_FILE", str(fake_pat))
    monkeypatch.setattr(c, "_claude_ask", lambda prompt, **kw: "Body.")
    monkeypatch.setattr(c, "_gh_post_issue_comment",
                        lambda *a, **kw: {
                            "error": "http_404", "status": 404,
                            "message": "Not Found"})

    m = _msg(LEV_DEV, ADMIN_ID,
             "comment on https://github.com/nope/nope/issues/999: brief")
    reply = c.handle_comment_request(m, m["text"])
    assert "cannot be found" in reply or "404" in reply or "not lodged" in reply


def test_handler_translates_403(tmp_path, monkeypatch):
    fake_pat = tmp_path / "gh_pat"
    fake_pat.write_text("ghp_faketoken12345\n")
    monkeypatch.setenv("GH_PAT_FILE", str(fake_pat))
    monkeypatch.setattr(c, "_claude_ask", lambda prompt, **kw: "Body.")
    monkeypatch.setattr(c, "_gh_post_issue_comment",
                        lambda *a, **kw: {
                            "error": "http_403", "status": 403,
                            "message": "Forbidden"})

    m = _msg(LEV_DEV, ADMIN_ID,
             "comment on https://github.com/x/y/issues/1: brief")
    reply = c.handle_comment_request(m, m["text"])
    assert "letters of marque" in reply.lower() or "not honoured" in reply.lower() or "not lodged" in reply.lower()


def test_handler_missing_pat(tmp_path, monkeypatch):
    """No gh_pat file — should decline cleanly, NOT call Claude."""
    monkeypatch.setenv("GH_PAT_FILE", str(tmp_path / "nonexistent"))

    claude_called = []
    monkeypatch.setattr(c, "_claude_ask",
                        lambda *a, **kw: claude_called.append(True) or "")

    m = _msg(LEV_DEV, ADMIN_ID,
             "comment on https://github.com/x/y/issues/1: brief")
    reply = c.handle_comment_request(m, m["text"])
    assert "letters of marque" in reply.lower() or "credentials" in reply.lower()
    assert not claude_called, "should not invoke Claude without a token"


def test_handler_empty_claude_response(tmp_path, monkeypatch):
    fake_pat = tmp_path / "gh_pat"
    fake_pat.write_text("ghp_faketoken12345\n")
    monkeypatch.setenv("GH_PAT_FILE", str(fake_pat))
    monkeypatch.setattr(c, "_claude_ask", lambda *a, **kw: "")

    posted = []
    monkeypatch.setattr(c, "_gh_post_issue_comment",
                        lambda *a, **kw: posted.append(True) or {
                            "html_url": "x"})

    m = _msg(LEV_DEV, ADMIN_ID,
             "comment on https://github.com/x/y/issues/1: brief")
    reply = c.handle_comment_request(m, m["text"])
    assert "quill" in reply.lower() or "retry" in reply.lower()
    assert not posted, "should not POST an empty body to GitHub"

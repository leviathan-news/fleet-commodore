"""Unit tests for wager refusal regex + admin gate + PR request detection."""
import os
import sys
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("BOT_USERNAME", "commodore_lev_bot")
os.environ.setdefault("BOT_HQ_GROUP_ID", "-1001111111111")
os.environ.setdefault("ADMIN_TELEGRAM_IDS", "1234982301")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from commodore import (  # noqa: E402
    _WAGER_REFUSAL_RE,
    _detect_pr_request,
    _is_admin,
    handle_pr_request,
)


def test_wager_regex_refuses_buy():
    assert _WAGER_REFUSAL_RE.match("/buy 7 yes 10")
    assert _WAGER_REFUSAL_RE.match("/buy@commodore_lev_bot 7 yes 10")


def test_wager_regex_refuses_sell_and_trade():
    assert _WAGER_REFUSAL_RE.match("/sell 7 yes 10")
    assert _WAGER_REFUSAL_RE.match("/trade foo")


def test_wager_regex_allows_readonly_lookups():
    assert _WAGER_REFUSAL_RE.match("/markets") is None
    assert _WAGER_REFUSAL_RE.match("/leaderboard") is None
    assert _WAGER_REFUSAL_RE.match("/position 7") is None


def test_detect_pr_request_true():
    assert _detect_pr_request("Please file a PR to fix the typo in docs")
    assert _detect_pr_request("commodore, open a pr that adds tests")
    assert _detect_pr_request("Draft a PR updating the README")


def test_detect_pr_request_false():
    assert not _detect_pr_request("general discussion of PRs as a concept")
    assert not _detect_pr_request("")


def test_is_admin_true_for_configured_id():
    assert _is_admin({"from": {"id": 1234982301}})


def test_is_admin_false_for_stranger():
    assert not _is_admin({"from": {"id": 9999999999}})


def test_handle_pr_request_refuses_non_admin():
    policy = {"allow_pr": True}
    msg = {"from": {"id": 9999999999, "username": "stranger"}, "text": "file a PR"}
    reply = handle_pr_request(msg, policy)
    assert "unranked crew" in reply.lower() or "admiralty" in reply.lower()


def test_handle_pr_request_refuses_outside_bot_hq():
    policy = {"allow_pr": False}
    msg = {"from": {"id": 1234982301, "username": "curvecap"}, "text": "file a PR"}
    reply = handle_pr_request(msg, policy)
    assert "bot hq" in reply.lower() or "quarter" in reply.lower()

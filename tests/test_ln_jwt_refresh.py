"""LN_API_TOKEN auto-refresh unit tests.

The full flow needs a live LN API; those are integration tests. Here we
cover the gates: missing wallet key, invalid wallet key, thrash-guard,
eth_account not installed.
"""
import os
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_commodore_globals(monkeypatch):
    """Each test starts fresh — no LN_API_TOKEN, no rate-limit carry-over."""
    # Env needed by commodore module top-level on import.
    monkeypatch.setenv("BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setenv("BOT_USERNAME", "commodore_lev_bot")
    monkeypatch.setenv("BOT_HQ_GROUP_ID", "-1001111111111")
    monkeypatch.setenv("SQUID_CAVE_GROUP_ID", "-1002222222222")
    monkeypatch.setenv("AGENT_CHAT_GROUP_ID", "-1003675648747")
    import commodore as c
    monkeypatch.setattr(c, "_last_ln_refresh_attempt", 0.0)
    yield


def test_refresh_skipped_when_wallet_key_missing(monkeypatch, tmp_path):
    import commodore as c
    monkeypatch.setattr(c, "LN_WALLET_KEY_FILE", str(tmp_path / "no-such-key"))
    result = c._refresh_ln_api_token()
    assert result is False


def test_refresh_skipped_when_wallet_key_invalid(monkeypatch, tmp_path):
    import commodore as c
    bad_key = tmp_path / "bad-wallet-key"
    bad_key.write_text("not a valid ethereum private key")
    monkeypatch.setattr(c, "LN_WALLET_KEY_FILE", str(bad_key))
    result = c._refresh_ln_api_token()
    assert result is False


def test_refresh_thrash_guard_blocks_rapid_retries(monkeypatch, tmp_path):
    """Two back-to-back refresh attempts: second must be blocked by the
    5-minute floor."""
    import commodore as c
    # Set last-attempt to "just now" — any call immediately after should
    # short-circuit with False before even reading the wallet key.
    monkeypatch.setattr(c, "_last_ln_refresh_attempt", time.time())
    # Ensure the wallet key file doesn't even exist, so if the guard
    # DOESN'T fire we'd get a different False from the file-missing branch.
    # But we want to assert this short-circuited BEFORE that check.
    monkeypatch.setattr(c, "LN_WALLET_KEY_FILE", str(tmp_path / "would-be-key"))
    # Create the file with a valid-looking key so we'd progress past the
    # file-read if the guard didn't work.
    (tmp_path / "would-be-key").write_text(
        "0x" + "a" * 64  # syntactically valid key shape
    )
    before = c._last_ln_refresh_attempt
    result = c._refresh_ln_api_token()
    # Guard short-circuits → returns False AND does NOT update the timestamp
    # (so a later legitimate refresh isn't delayed further).
    assert result is False
    assert c._last_ln_refresh_attempt == before


def test_refresh_sets_timestamp_even_on_failure(monkeypatch, tmp_path):
    """If refresh is attempted and fails for a real reason (bad key),
    _last_ln_refresh_attempt updates so the thrash guard fires next time."""
    import commodore as c
    bad_key = tmp_path / "bad-key"
    bad_key.write_text("definitely not a real private key")
    monkeypatch.setattr(c, "LN_WALLET_KEY_FILE", str(bad_key))
    before = c._last_ln_refresh_attempt
    result = c._refresh_ln_api_token()
    assert result is False
    # Timestamp advanced.
    assert c._last_ln_refresh_attempt > before


def test_do_relay_receipt_retries_once_on_401(monkeypatch, tmp_path):
    """When the relay returns 401 and allow_refresh=True, _do_relay_receipt
    calls _refresh + retries with allow_refresh=False.

    We don't exercise real HTTP here — we monkeypatch urlopen to simulate
    401 on first call, 2xx on second, and monkeypatch _refresh_ln_api_token
    to return True.
    """
    import commodore as c
    import urllib.error
    import urllib.request

    # Fake 401 on first urlopen, 200 on second.
    call_count = {"n": 0}
    def fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.HTTPError(
                url=req.full_url, code=401, msg="Unauthorized", hdrs={}, fp=None
            )
        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    refresh_called = {"n": 0}
    def fake_refresh():
        refresh_called["n"] += 1
        return True
    monkeypatch.setattr(c, "_refresh_ln_api_token", fake_refresh)

    # Must have a non-empty token to enter the call.
    monkeypatch.setattr(c, "LN_API_TOKEN", "stale-jwt")

    c._do_relay_receipt(
        telegram_message_id=12345,
        chat_id=-1003675648747,
        topic_id=0,
        text="test",
        allow_refresh=True,
    )
    assert call_count["n"] == 2, f"expected 2 HTTP calls (fail, retry), got {call_count['n']}"
    assert refresh_called["n"] == 1, "refresh must be invoked exactly once"


def test_do_relay_receipt_no_recursive_refresh_loop(monkeypatch):
    """Persistent 401 (refresh didn't help, or allow_refresh=False) must
    NOT recurse into another refresh attempt."""
    import commodore as c
    import urllib.error
    import urllib.request

    call_count = {"n": 0}
    def always_401(req, timeout=None):
        call_count["n"] += 1
        raise urllib.error.HTTPError(
            url=req.full_url, code=401, msg="Unauthorized", hdrs={}, fp=None
        )
    monkeypatch.setattr(urllib.request, "urlopen", always_401)

    refresh_called = {"n": 0}
    def fake_refresh():
        refresh_called["n"] += 1
        return True  # claims success but the retry still 401s
    monkeypatch.setattr(c, "_refresh_ln_api_token", fake_refresh)

    monkeypatch.setattr(c, "LN_API_TOKEN", "stale-jwt")

    c._do_relay_receipt(
        telegram_message_id=12345,
        chat_id=-1003675648747,
        topic_id=0,
        text="test",
        allow_refresh=True,
    )
    # First call: 401 → refresh → retry. Second call: 401 but allow_refresh
    # is False on the retry, so no third call.
    assert call_count["n"] == 2, f"expected exactly 2 HTTP calls, got {call_count['n']}"
    assert refresh_called["n"] == 1, "refresh must be invoked only once (no recursion)"

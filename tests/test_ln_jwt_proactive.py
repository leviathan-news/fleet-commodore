"""Unit tests for the proactive JWT-refresh path.

The reactive (401-driven) path is covered in test_ln_jwt_refresh.py.
These tests cover the _ln_jwt_expires_in parser + the
_maybe_proactively_refresh_ln_token gate.
"""
import base64
import json
import time

import pytest


@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setenv("BOT_USERNAME", "commodore_lev_bot")
    monkeypatch.setenv("BOT_HQ_GROUP_ID", "-1001111111111")
    monkeypatch.setenv("SQUID_CAVE_GROUP_ID", "-1002222222222")
    monkeypatch.setenv("AGENT_CHAT_GROUP_ID", "-1003675648747")
    import commodore as c
    monkeypatch.setattr(c, "_last_ln_refresh_attempt", 0.0)
    yield


def _fake_jwt(exp_offset_seconds: int) -> str:
    """Return a JWT-shaped string (header.payload.sig) with payload.exp
    set to now + offset. Signature is not valid; we only decode."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload_obj = {
        "token_type": "access",
        "iat": int(time.time()),
        "exp": int(time.time() + exp_offset_seconds),
        "user_id": 1847,
        "jti": "fake",
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_obj).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


# --- _ln_jwt_expires_in ---------------------------------------------------


def test_expires_in_returns_seconds_for_valid_jwt(monkeypatch):
    import commodore as c
    token = _fake_jwt(1800)  # 30 min from now
    monkeypatch.setattr(c, "LN_API_TOKEN", token)
    remaining = c._ln_jwt_expires_in()
    assert remaining is not None
    # Allow a few seconds of drift from the time between _fake_jwt building
    # the payload and the test reading it.
    assert 1790 <= remaining <= 1805


def test_expires_in_returns_negative_for_expired_jwt(monkeypatch):
    import commodore as c
    token = _fake_jwt(-60)  # expired a minute ago
    monkeypatch.setattr(c, "LN_API_TOKEN", token)
    remaining = c._ln_jwt_expires_in()
    assert remaining is not None
    assert remaining < 0


def test_expires_in_none_when_token_empty(monkeypatch):
    import commodore as c
    monkeypatch.setattr(c, "LN_API_TOKEN", "")
    assert c._ln_jwt_expires_in() is None


def test_expires_in_none_when_token_malformed(monkeypatch):
    import commodore as c
    monkeypatch.setattr(c, "LN_API_TOKEN", "not-a-jwt")
    assert c._ln_jwt_expires_in() is None
    monkeypatch.setattr(c, "LN_API_TOKEN", "foo.bar")  # only 2 parts
    assert c._ln_jwt_expires_in() is None
    # 3 parts but payload is garbage.
    monkeypatch.setattr(c, "LN_API_TOKEN", "aaa.bbb.ccc")
    assert c._ln_jwt_expires_in() is None


def test_expires_in_none_when_no_exp_field(monkeypatch):
    import commodore as c
    header = base64.urlsafe_b64encode(b"{}").rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(b'{"user_id":1}').rstrip(b"=").decode()
    monkeypatch.setattr(c, "LN_API_TOKEN", f"{header}.{payload}.sig")
    assert c._ln_jwt_expires_in() is None


# --- _maybe_proactively_refresh_ln_token ---------------------------------


def test_maybe_proactive_refresh_noop_with_healthy_token(monkeypatch):
    """Token with > 5 min headroom should NOT trigger refresh."""
    import commodore as c
    token = _fake_jwt(1800)  # 30 min ahead
    monkeypatch.setattr(c, "LN_API_TOKEN", token)
    refresh_called = {"n": 0}

    def fake_refresh():
        refresh_called["n"] += 1
        return True
    monkeypatch.setattr(c, "_refresh_ln_api_token", fake_refresh)

    c._maybe_proactively_refresh_ln_token()
    assert refresh_called["n"] == 0


def test_maybe_proactive_refresh_fires_within_headroom(monkeypatch):
    """Token within 5 min of expiry SHOULD trigger refresh."""
    import commodore as c
    token = _fake_jwt(120)  # 2 min to go — inside the 300s headroom
    monkeypatch.setattr(c, "LN_API_TOKEN", token)
    refresh_called = {"n": 0}

    def fake_refresh():
        refresh_called["n"] += 1
        return True
    monkeypatch.setattr(c, "_refresh_ln_api_token", fake_refresh)

    c._maybe_proactively_refresh_ln_token()
    assert refresh_called["n"] == 1


def test_maybe_proactive_refresh_fires_on_expired(monkeypatch):
    """Already-expired token should trigger refresh too (belt-and-braces)."""
    import commodore as c
    token = _fake_jwt(-60)
    monkeypatch.setattr(c, "LN_API_TOKEN", token)
    refresh_called = {"n": 0}

    def fake_refresh():
        refresh_called["n"] += 1
        return True
    monkeypatch.setattr(c, "_refresh_ln_api_token", fake_refresh)

    c._maybe_proactively_refresh_ln_token()
    assert refresh_called["n"] == 1


def test_maybe_proactive_refresh_noop_on_malformed(monkeypatch):
    """Can't decode token → don't speculate. Reactive path will still catch
    a 401 if the token turns out to be broken."""
    import commodore as c
    monkeypatch.setattr(c, "LN_API_TOKEN", "definitely-not-a-jwt")
    refresh_called = {"n": 0}
    monkeypatch.setattr(c, "_refresh_ln_api_token",
                        lambda: (refresh_called.__setitem__("n", refresh_called["n"] + 1), True)[1])
    c._maybe_proactively_refresh_ln_token()
    assert refresh_called["n"] == 0

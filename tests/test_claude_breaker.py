"""Self-healing Claude circuit-breaker tests.

The breaker trips when Claude returns auth/quota/rate-limit errors, falling
the daemon back to Codex. Without self-healing, the breaker stays tripped
until daemon restart — even after the operator does `claude /login` to fix
expired OAuth (2026-05-06 incident: ~20h of silent dead bot).

These tests pin the contract: when tripped, the breaker probes Claude every
_CLAUDE_PROBE_INTERVAL_S and releases on a clean success.
"""
import time
from unittest import mock

import pytest
import commodore


@pytest.fixture(autouse=True)
def reset_breaker():
    """Each test starts with a fresh breaker state."""
    commodore._claude_failures = 0
    commodore._claude_unavailable_until = 0.0
    commodore._claude_last_probe_at = 0.0
    yield
    commodore._claude_failures = 0
    commodore._claude_unavailable_until = 0.0
    commodore._claude_last_probe_at = 0.0


def test_breaker_clear_state_returns_available():
    """Default state: failures=0, no cooldown, breaker reports available
    without bothering to probe."""
    with mock.patch.object(commodore, "_probe_claude") as m_probe:
        assert commodore._claude_is_available() is True
        # Probe should NOT have been called for a healthy breaker
        m_probe.assert_not_called()


def test_tripped_breaker_probes_and_releases_on_success():
    """When failures>=max, the breaker probes; on probe success it clears."""
    commodore._claude_failures = commodore._claude_max_failures
    with mock.patch.object(commodore, "_probe_claude", return_value=True) as m_probe:
        assert commodore._claude_is_available() is True
        m_probe.assert_called_once()
    # After release, failure counter and cooldown should be cleared
    assert commodore._claude_failures == 0
    assert commodore._claude_unavailable_until == 0.0


def test_tripped_breaker_stays_tripped_when_probe_fails():
    """If probe still 401s, breaker remains tripped."""
    commodore._claude_failures = commodore._claude_max_failures
    with mock.patch.object(commodore, "_probe_claude", return_value=False) as m_probe:
        assert commodore._claude_is_available() is False
        m_probe.assert_called_once()
    # State unchanged
    assert commodore._claude_failures == commodore._claude_max_failures


def test_breaker_rate_limits_probes():
    """If we just probed, don't probe again until interval elapses."""
    commodore._claude_failures = commodore._claude_max_failures
    commodore._claude_last_probe_at = time.time()  # just probed
    with mock.patch.object(commodore, "_probe_claude") as m_probe:
        assert commodore._claude_is_available() is False
        # Probe interval hasn't elapsed; we should NOT have re-probed
        m_probe.assert_not_called()


def test_breaker_probes_after_interval_elapses():
    """Once the probe interval elapses, the breaker probes again."""
    commodore._claude_failures = commodore._claude_max_failures
    # Last probe long ago
    commodore._claude_last_probe_at = time.time() - commodore._CLAUDE_PROBE_INTERVAL_S - 1
    with mock.patch.object(commodore, "_probe_claude", return_value=True) as m_probe:
        assert commodore._claude_is_available() is True
        m_probe.assert_called_once()


def test_cooldown_alone_triggers_probe():
    """Cooldown set but failures<max: still tripped, breaker should probe."""
    commodore._claude_unavailable_until = time.time() + 3600
    with mock.patch.object(commodore, "_probe_claude", return_value=True) as m_probe:
        assert commodore._claude_is_available() is True
        m_probe.assert_called_once()
    assert commodore._claude_unavailable_until == 0.0


def test_probe_classifies_auth_failure_as_unhealthy():
    """A 'Failed to authenticate' response counts as probe failure even
    if the CLI exits 0 (Claude CLI sometimes prints the auth error to
    stdout with rc=0)."""
    fake_proc = mock.Mock(
        returncode=0,
        stdout='Failed to authenticate. API Error: 401 {"type":"error",...}',
        stderr="",
    )
    with mock.patch.object(commodore.subprocess, "run", return_value=fake_proc):
        assert commodore._probe_claude() is False


def test_probe_classifies_quota_error_as_unhealthy():
    """Quota / rate-limit phrasings should fail the probe."""
    fake_proc = mock.Mock(
        returncode=0,
        stdout="You have hit your usage limit for this billing period",
        stderr="",
    )
    with mock.patch.object(commodore.subprocess, "run", return_value=fake_proc):
        assert commodore._probe_claude() is False


def test_probe_clean_success():
    """A normal response with content classifies as healthy."""
    fake_proc = mock.Mock(returncode=0, stdout="alive", stderr="")
    with mock.patch.object(commodore.subprocess, "run", return_value=fake_proc):
        assert commodore._probe_claude() is True


def test_claude_ask_short_circuits_when_breaker_tripped_and_probe_fails():
    """The early-exit in _claude_ask must respect the probe path so it
    doesn't burn through retries when probe says still-broken."""
    commodore._claude_failures = commodore._claude_max_failures
    commodore._claude_last_probe_at = time.time()  # probe rate-limited; tripped
    with mock.patch.object(commodore.subprocess, "run") as m_run:
        result = commodore._claude_ask("anything")
    assert result == ""
    # subprocess.run should NOT have been called for the actual Claude CLI
    # (we never got past the breaker check).
    m_run.assert_not_called()

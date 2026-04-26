"""Postgres role hardening — connect as commodore_reader and verify the
documented sensitive-column REVOKEs are in effect.

This test only runs when COMMODORE_TEST_DB_URL is set in the environment.
It connects to the real (or a test mirror) DB as commodore_reader and
asserts that SELECTing each documented sensitive column returns a
permission error.

To run locally:
  COMMODORE_TEST_DB_URL='postgres://commodore_reader:<pw>@host:5432/db' \\
    pytest tests/test_db_role_hardening.py -v
"""
import os
import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("COMMODORE_TEST_DB_URL"),
    reason="COMMODORE_TEST_DB_URL not set",
)


@pytest.fixture
def reader_conn():
    psycopg2 = pytest.importorskip("psycopg2")
    conn = psycopg2.connect(os.environ["COMMODORE_TEST_DB_URL"])
    yield conn
    conn.close()


# Each tuple is (sql, label). All MUST raise psycopg2.errors.InsufficientPrivilege.
DENIED_QUERIES = [
    ("SELECT email FROM bot_user LIMIT 1", "bot_user.email"),
    ("SELECT unique_token FROM bot_user LIMIT 1", "bot_user.unique_token"),
    ("SELECT * FROM bot_social_account LIMIT 1", "bot_social_account.*"),
    ("SELECT * FROM bot_webauthn_credential LIMIT 1", "bot_webauthn_credential.*"),
    ("SELECT * FROM bot_pending_account_claim LIMIT 1",
     "bot_pending_account_claim.*"),
    ("SELECT * FROM lnn_user_login_event LIMIT 1", "lnn_user_login_event.*"),
    ("SELECT ip_address FROM lnn_click LIMIT 1", "lnn_click.ip_address"),
    ("SELECT user_agent FROM lnn_click LIMIT 1", "lnn_click.user_agent"),
    ("SELECT password FROM auth_user LIMIT 1", "auth_user.password (legacy)"),
]


@pytest.mark.parametrize("sql, label", DENIED_QUERIES)
def test_sensitive_column_denied(reader_conn, sql, label):
    psycopg2 = pytest.importorskip("psycopg2")
    cur = reader_conn.cursor()
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute(sql)
        cur.fetchone()
    reader_conn.rollback()


def test_role_is_read_only(reader_conn):
    """default_transaction_read_only should be ON for this role."""
    cur = reader_conn.cursor()
    cur.execute("SHOW default_transaction_read_only")
    assert cur.fetchone()[0] == "on"


def test_role_has_statement_timeout(reader_conn):
    """statement_timeout should be a small number of milliseconds (3000ms)."""
    cur = reader_conn.cursor()
    cur.execute("SHOW statement_timeout")
    timeout = cur.fetchone()[0]
    # Accept "3s" or "3000ms" — both are valid Postgres representations
    assert timeout in ("3s", "3000ms", "3000")


def test_safe_table_select_works(reader_conn):
    """Confirm we have SELECT on a non-sensitive table — the role isn't
    universally locked down, just appropriately denied."""
    cur = reader_conn.cursor()
    cur.execute("SELECT id FROM lnn_news LIMIT 1")
    # Either returns a row or empty — both fine. The point is: no permission error.
    cur.fetchone()

"""commodore-db + commodore-orm wrapper tests.

These test the PARSER GATE (the first defense layer). The DB role
enforcement is the second layer and is tested in integration tests
that need a live Postgres connection — those live in
tests/integration/test_db_live.py (gated on COMMODORE_DB_URL) and
are not run in the normal unit-test suite.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
DB_WRAPPER = REPO / "bin" / "commodore-db"
ORM_WRAPPER = REPO / "bin" / "commodore-orm"


def _run(wrapper, stdin_text, env=None):
    """Invoke wrapper; return (exit_code, parsed_stdout_json)."""
    proc = subprocess.run(
        [sys.executable, str(wrapper)],
        input=stdin_text,
        capture_output=True, text=True,
        env=env,
        timeout=15,
    )
    payload = None
    try:
        payload = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        pytest.fail(f"wrapper stdout is not JSON: {proc.stdout!r}")
    return proc.returncode, payload


# --- commodore-db parser tests --------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT count(*) FROM lnn_news",
    "SELECT id, headline FROM lnn_news WHERE status='approved' LIMIT 10",
    "select id from lnn_news",           # case insensitive
    "  SELECT 1  ",                      # leading/trailing whitespace
    "WITH recent AS (SELECT * FROM lnn_news LIMIT 10) SELECT count(*) FROM recent",
    "EXPLAIN SELECT 1",
    "EXPLAIN ANALYZE SELECT 1",
])
def test_commodore_db_accepts_read_only_sql(sql, tmp_path):
    rc, payload = _run(DB_WRAPPER, sql, env={
        "COMMODORE_DB_URL": "postgres://fake"
    })
    # Parser passes — then v1 stub reports parser_passed_stub.
    # When the real psycopg layer lands (task #16), this will become
    # "status: ok" with rows. For now any non-error exit + parser-passed
    # status means the gate let it through.
    assert rc == 0, f"expected accept; got rc={rc} payload={payload}"
    assert payload.get("status") == "parser_passed_stub"


@pytest.mark.parametrize("sql,expected_reason_substr", [
    ("UPDATE lnn_news SET status='x'", "UPDATE"),
    ("DELETE FROM lnn_news WHERE id=1", "DELETE"),
    ("INSERT INTO lnn_news (id) VALUES (1)", "INSERT"),
    ("DROP TABLE lnn_news", "DROP"),
    ("TRUNCATE lnn_news", "TRUNCATE"),
    ("ALTER TABLE lnn_news ADD COLUMN x INT", "ALTER"),
    ("CREATE TABLE foo (id int)", "CREATE"),
    ("GRANT SELECT ON lnn_news TO bad_actor", "GRANT"),
    ("VACUUM lnn_news", "VACUUM"),
])
def test_commodore_db_rejects_mutations(sql, expected_reason_substr):
    rc, payload = _run(DB_WRAPPER, sql)
    assert rc == 1, f"expected reject; got rc={rc}"
    assert payload["error"] == "not_read_only"
    assert expected_reason_substr.lower() in payload["reason"].lower()


def test_commodore_db_rejects_multi_statement():
    rc, payload = _run(DB_WRAPPER, "SELECT 1; DROP TABLE lnn_news")
    assert rc == 1
    assert payload["error"] == "not_read_only"
    assert "multiple" in payload["reason"].lower()


def test_commodore_db_rejects_empty():
    rc, payload = _run(DB_WRAPPER, "")
    assert rc == 1
    assert payload["error"] == "not_read_only"


def test_commodore_db_reports_missing_db_url():
    # Clear COMMODORE_DB_URL. Parser passes, then stub reports the missing
    # URL as a separate error path.
    import os
    env = {k: v for k, v in os.environ.items() if k != "COMMODORE_DB_URL"}
    rc, payload = _run(DB_WRAPPER, "SELECT 1", env=env)
    assert rc == 2
    assert payload["error"] == "missing_db_url"


# --- commodore-orm snippet gate tests -------------------------------------


@pytest.mark.parametrize("snippet", [
    "News.objects.filter(status='approved').count()",
    "User.objects.filter(username='gerrithall').first()",
    "list(News.objects.all()[:10])",
    "News.objects.aggregate(total=Count('id'))",
    "   News.objects.count()   ",                    # whitespace tolerant
])
def test_commodore_orm_accepts_read_only_snippets(snippet):
    rc, payload = _run(ORM_WRAPPER, snippet, env={
        "COMMODORE_DB_URL": "postgres://fake"
    })
    assert rc == 0, f"expected accept; got rc={rc} payload={payload}"
    assert payload.get("status") == "parser_passed_stub"


@pytest.mark.parametrize("snippet,marker_substr", [
    ("news.save()", "save("),
    ("News.objects.create(id=1)", "create("),
    ("News.objects.filter(id=1).delete()", "delete("),
    ("News.objects.filter(id=1).update(status='x')", "update("),
    ("News.objects.bulk_create([...])", "bulk_create("),
    ("News.objects.raw('SELECT 1')", "raw("),
    ("connection.cursor()", "connection."),
    ("connection.close()", "connection."),
    ("transaction.atomic()", "transaction."),
    ("exec('import os')", "exec("),
    ("eval('1+1')", "eval("),
    ("__import__('os')", "__import__("),
    ("open('/etc/passwd')", "open("),
    ("subprocess.run(['ls'])", "subprocess."),
])
def test_commodore_orm_rejects_mutations(snippet, marker_substr):
    rc, payload = _run(ORM_WRAPPER, snippet)
    assert rc == 1, f"expected reject for {snippet!r}; got rc={rc}"
    assert payload["error"] == "not_read_only"
    assert marker_substr.lower() in payload["reason"].lower()


def test_commodore_orm_rejects_empty():
    rc, payload = _run(ORM_WRAPPER, "")
    assert rc == 1
    assert payload["error"] == "not_read_only"
    assert "empty" in payload["reason"].lower()


def test_commodore_orm_reports_missing_db_url():
    import os
    env = {k: v for k, v in os.environ.items() if k != "COMMODORE_DB_URL"}
    rc, payload = _run(ORM_WRAPPER, "News.objects.count()", env=env)
    assert rc == 2
    assert payload["error"] == "missing_db_url"

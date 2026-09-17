import importlib.util
import sqlite3
import time


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "qa_healthcheck", "bin/qa-healthcheck.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _schema(conn):
    conn.execute("""CREATE TABLE chat_intake_event (
        update_id INTEGER PRIMARY KEY, status TEXT, created_at REAL,
        finished_at REAL, message_id INTEGER
    )""")


def test_health_reads_counts_age_and_positive_terminal_receipt(tmp_path, monkeypatch):
    mod = _load_module()
    db = tmp_path / "commodore.db"
    with sqlite3.connect(tmp_path / "chat-intake.db") as conn:
        _schema(conn)
        now = time.time()
        conn.executemany("INSERT INTO chat_intake_event VALUES (?,?,?,?,?)", [
            (1, "queued", now - 121, None, None),
            (2, "held_unknown", now - 1, None, None),
            (3, "resolved", now - 20, now - 10, 42),
            (4, "escalated", now - 30, now - 5, 0),
        ])
    monkeypatch.setattr(mod, "COMMODORE_DB_FILE", db)
    report = mod._chat_intake_health()
    assert report["status"] == "ok"
    assert report["counts"]["queued"] == 1
    assert report["counts"]["held_unknown"] == 1
    assert report["oldest_unresolved_age"] > 120
    assert report["last_terminal_reply_at"] is not None


def test_health_uses_read_only_uri_and_does_not_write(tmp_path, monkeypatch):
    mod = _load_module()
    db = tmp_path / "commodore.db"
    intake_db = tmp_path / "chat-intake.db"
    with sqlite3.connect(intake_db) as conn:
        _schema(conn)
        conn.execute("INSERT INTO chat_intake_event VALUES (?,?,?,?,?)", (1, "queued", 1, None, None))
    monkeypatch.setattr(mod, "COMMODORE_DB_FILE", db)
    calls = []
    original_connect = mod.sqlite3.connect

    def tracking_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(mod.sqlite3, "connect", tracking_connect)
    assert mod._chat_intake_health()["counts"]["queued"] == 1
    assert calls[0][0][0] == f"{intake_db.resolve().as_uri()}?mode=ro"
    assert calls[0][1] == {"uri": True, "timeout": 2}
    with original_connect(calls[0][0][0], **calls[0][1]) as conn:
        try:
            conn.execute("INSERT INTO chat_intake_event VALUES (?,?,?,?,?)", (2, "queued", 1, None, None))
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower()
        else:
            raise AssertionError("read-only health connection permitted an INSERT")
        assert conn.execute("SELECT COUNT(*) FROM chat_intake_event").fetchone()[0] == 1


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    mod = _load_module()
    db = tmp_path / "missing" / "commodore.db"
    monkeypatch.setattr(mod, "COMMODORE_DB_FILE", db)
    assert mod._chat_intake_health()["status"] == "not_installed"
    assert not db.parent.exists()


def test_corrupt_database_returns_fixed_safe_class(tmp_path, monkeypatch):
    mod = _load_module()
    db = tmp_path / "commodore.db"
    intake_db = tmp_path / "chat-intake.db"
    intake_db.write_bytes(b"not sqlite")
    monkeypatch.setattr(mod, "COMMODORE_DB_FILE", db)
    assert mod._chat_intake_health() == {
        "status": "read_error", "counts": {},
        "oldest_unresolved_age": None, "last_terminal_reply_at": None,
    }


def test_warning_boundaries(monkeypatch, tmp_path):
    mod = _load_module()
    monkeypatch.setattr(mod, "COMMODORE_DB_FILE", tmp_path / "commodore.db")
    monkeypatch.setattr(mod, "_chat_intake_health", lambda: {
        "status": "ok", "counts": {"held_unknown": 1},
        "oldest_unresolved_age": 120, "last_terminal_reply_at": None,
    })
    monkeypatch.setattr(mod, "_docker_ok", lambda *_: True)
    monkeypatch.setattr(mod, "_container_running", lambda *_: True)
    monkeypatch.setattr(mod, "_executable", lambda *_: True)
    monkeypatch.setattr(mod, "FLEET_PROVIDER", "codex")
    monkeypatch.setattr(mod, "DB_URL_FILE", tmp_path / "db-url")
    mod.DB_URL_FILE.write_text("db")
    report = mod.readiness_report(quick=True)
    assert report["ok"] is True
    assert report["warnings"] == ["chat_intake_held_unknown"]

    monkeypatch.setattr(mod, "_chat_intake_health", lambda: {
        "status": "ok", "counts": {},
        "oldest_unresolved_age": 121, "last_terminal_reply_at": None,
    })
    assert "chat_intake_oldest_unresolved_over_120_seconds" in \
        mod.readiness_report(quick=True)["warnings"]

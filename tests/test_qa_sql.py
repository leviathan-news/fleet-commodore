import importlib
import io


def load(monkeypatch, tmp_path):
    db = tmp_path / "db-url"
    db.write_text("postgres://reader:secret@example.invalid/db")
    monkeypatch.setenv("COMMODORE_DB_URL_FILE", str(db))
    monkeypatch.setenv("COMMODORE_DOCKER", "/fake/docker")
    import qa_sql
    return importlib.reload(qa_sql), db


def test_missing_credentials_does_not_invoke_docker(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMODORE_DB_URL_FILE", str(tmp_path / "missing"))
    import qa_sql
    mod = importlib.reload(qa_sql)
    called = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: called.append(a))
    assert mod.execute_sql("SELECT 1") == {"error": "missing_credentials"}
    assert not called


def test_sql_limit_and_shell_text_are_data(monkeypatch, tmp_path):
    mod, _ = load(monkeypatch, tmp_path)
    assert mod.execute_sql("x" * 4001) == {"error": "sql_too_large"}
    calls = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: calls.append(a))
    # It is below the size limit and must reach the wrapper as stdin data, not
    # become a shell command (the fake process is intentionally not usable).
    assert mod.execute_sql("SELECT 1; echo pwned") == {"error": "execution_failed"}
    assert calls and "SELECT 1; echo pwned" not in " ".join(calls[0][0])


def test_command_has_required_isolation_and_no_auth_mounts(monkeypatch, tmp_path):
    mod, _ = load(monkeypatch, tmp_path)
    seen = {}

    class P:
        returncode = 0
        stdin = io.BytesIO()
        def poll(self): return 0
        def wait(self, **kwargs): return 0

    def fake_popen(cmd, **kwargs):
        seen.update(cmd=cmd, kwargs=kwargs)
        return P()
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    assert mod.execute_sql("SELECT 1") == {"error": "invalid_output"}
    cmd = seen["cmd"]
    assert ["--memory", "256m"] == cmd[cmd.index("--memory"):cmd.index("--memory") + 2]
    assert ["--cpus", "0.5"] == cmd[cmd.index("--cpus"):cmd.index("--cpus") + 2]
    assert "--read-only" in cmd and "--security-opt" in cmd
    assert any("commodore-db:ro" in x for x in cmd)
    assert cmd[cmd.index("--entrypoint") + 1] == "/app/bin/commodore-db"
    mounts = [cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == "-v"]
    destinations = [x.rsplit(":", 2)[-2] if x.count(":") >= 2 else x for x in mounts]
    assert not any("claude" in x.lower() or "github" in x.lower() or "telegram" in x.lower() for x in destinations)
    assert set(seen["kwargs"]["env"]) == {"HOME", "PATH"}


def test_success_requires_top_level_dict(monkeypatch, tmp_path):
    mod, _ = load(monkeypatch, tmp_path)
    class P:
        returncode = 0
        def __init__(self): self.stdin = io.BytesIO()
        def poll(self): return 0
        def wait(self, **kwargs): return 0
    def fake_popen(cmd, **kwargs):
        kwargs["stdout"].write(b'{"status":"ok","columns": ["n"], "rows": [[1]]}')
        return P()
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    assert mod.execute_sql("SELECT 1")["rows"] == [[1]]


def test_timeout_kills_and_removes_only_owned_container(monkeypatch, tmp_path):
    mod, _ = load(monkeypatch, tmp_path)
    calls = []
    class P:
        returncode = None
        stdin = None
        def __init__(self): self.dead = False
        def poll(self): return -9 if self.dead else None
        def kill(self): self.dead = True; calls.append(("reap",))
        def wait(self, **kwargs): self.dead = True; calls.append(("wait",))
    launch_env = {}
    def fake_popen(cmd, **kwargs):
        launch_env.update(kwargs["env"])
        return P()
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    def fake_run(cmd, **kwargs):
        calls.append((tuple(cmd), kwargs.get("env")))
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    ticks = iter((0.0, 16.0, 16.0))
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(ticks))
    assert mod.execute_sql("SELECT 1") == {"error": "timeout"}
    kill = next(x for x in calls if len(x[0]) > 2 and x[0][1] == "kill")
    owned = kill[0][2]
    remove = next(x for x in calls if len(x[0]) > 2 and x[0][1] == "rm")
    assert remove[0] == ("/fake/docker", "rm", "-f", owned)
    assert kill[1] == launch_env == remove[1]
    assert calls.count(("reap",)) == 1


def test_output_limit_aborts_and_does_not_return_stderr(monkeypatch, tmp_path):
    mod, _ = load(monkeypatch, tmp_path)
    calls = []
    class P:
        returncode = None
        stdin = None
        def poll(self): return None
        def kill(self): calls.append(("reap",))
        def wait(self, **kwargs): calls.append(("wait",))
    def fake_popen(cmd, **kwargs):
        kwargs["stdout"].write(b"x" * (mod.MAX_OUTPUT_BYTES + 1))
        kwargs["stderr"].write(b"postgres://secret")
        return P()
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kwargs: calls.append(tuple(cmd)))
    monkeypatch.setattr(mod.time, "monotonic", lambda: 0.0)
    result = mod.execute_sql("SELECT 1")
    assert result == {"error": "output_too_large"}
    assert all("secret" not in str(call) for call in calls)
    owned = next(x[2] for x in calls if len(x) > 2 and x[1] == "kill")
    assert ("/fake/docker", "rm", "-f", owned) in calls

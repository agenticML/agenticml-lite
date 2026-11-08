"""Audit append safety, backwards paging, and standalone CLI rendering."""
from server_db_helpers import default_store
from server_db_helpers import audit_rows, audit_text
from agenticml.server.db import DatabaseError, for_server_dir
from agenticml.server.db.repos import AuditRepo
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import io
import json
import logging
import os
import stat
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from agenticml import cli
from agenticml.server import audit
from agenticml.server.app import prepare_data_dir
from agenticml.server.keys import KeyStore
from agenticml.server.users import Principal, UserStore
from agenticml.store import layout
from test_server_ingest import request
from test_server_login_throttle import freeze_throttle
from test_server_signin import PASSWORD, password_login, running
from test_server_users import write_run, write_v1_keys
from test_server_web import authed, call, cookie_parts, csrf, login
from server_db_helpers import default_workspace_id


def event(index, **fields):
    return {"ts": f"2026-11-06T00:00:{index:02d}.000000Z", "actor": "alice",
            "via": "cli", "action": "key.create", "target": "laptop",
            "client": None, "detail": None, **fields}


def write_events(server_dir, events, suffix=b""):
    path = server_dir / "audit.jsonl"
    path.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in events) + suffix)
    return path


def patch_os(monkeypatch, **overrides):
    # Replace audit's reference, leaving pytest and other modules' os untouched.
    monkeypatch.setattr(audit, "os", SimpleNamespace(**{**vars(os), **overrides}))


def test_record_format_truncation_and_key_order(tmp_path, monkeypatch):
    stamp = layout.format_timestamp()
    monkeypatch.setattr(audit.layout, 'format_timestamp', lambda: stamp)
    detail = {'identity':'é'*300, 'nested':[{'project':'x'*300}], 'count':3,
              'missing':None, 'control':'a\n\r\t\x00b'}
    log = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
    log.record('alice','password','key.create','t'*300,'203.0.113.7',detail)
    rows, skipped = audit.tail(tmp_path)
    assert skipped == 0 and len(rows) == 1
    assert rows[0] == {'ts':stamp,'actor':'alice','via':'password','action':'key.create',
                       'target':'t'*256,'client':'203.0.113.7',
                       'detail':{**detail,'identity':'é'*256,'nested':[{'project':'x'*256}]}}
    assert len(detail['identity']) == 300
    assert stat.S_IMODE(log.db.path.stat().st_mode) == 0o600


@pytest.mark.parametrize("field", ["actor", "via", "action", "client"])
def test_record_truncates_all_top_level_strings(tmp_path, field):
    fields = {"actor": "alice", "via": "cli", "action": "key.create", field: "é" * 300}
    audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path)).record(**fields)
    assert audit.tail(tmp_path)[0][0][field] == "é" * 256


def test_cli_actor_fallback(monkeypatch):
    monkeypatch.setattr(audit.getpass, "getuser", lambda: "operator")
    assert audit.cli_actor() == "cli:operator"

    def unavailable():
        raise OSError("no user database")

    monkeypatch.setattr(audit.getpass, "getuser", unavailable)
    assert audit.cli_actor() == "cli:unknown"


def test_symlink_is_not_followed_and_record_does_not_raise(tmp_path, caplog):
    outside = tmp_path / 'outside'
    outside.write_bytes(b'unchanged')
    (tmp_path / 'audit.jsonl').symlink_to(outside)
    log = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
    log.record('alice','cli','key.create')
    assert outside.read_bytes() == b'unchanged'
    assert 'skipped 1 malformed or unreadable legacy audit line(s)' in caplog.text
    assert audit.tail(tmp_path)[0][0]['actor'] == 'alice'


@pytest.mark.parametrize('kind',['fifo','directory'])
def test_nonregular_file_is_refused(tmp_path, kind, caplog):
    path = tmp_path / 'audit.jsonl'
    os.mkfifo(path) if kind == 'fifo' else path.mkdir()
    errors = []
    def check():
        try:
            audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path)).record('alice','cli','key.create')
            assert audit.tail(tmp_path)[0][0]['actor'] == 'alice'
        except BaseException as exc: errors.append(exc)
    worker = threading.Thread(target=check, daemon=True)
    worker.start(); worker.join(timeout=5)
    assert not worker.is_alive()
    if errors: raise errors[0]
    assert 'skipped 1 malformed or unreadable legacy audit line(s)' in caplog.text


def test_eight_concurrent_writers_produce_whole_lines(tmp_path):
    barrier = threading.Barrier(8)

    def write(worker):
        # Separate objects also exercise the file lock rather than a shared lock.
        log = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
        barrier.wait(timeout=10)
        for index in range(20):
            log.record(f"user{worker}", "cli", "note.write", f"run{index}",
                       detail={"worker": worker, "index": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(8)))
    rows = audit.tail(tmp_path, limit=1000)[0]
    assert len(rows) == 160
    assert {(row["detail"]["worker"], row["detail"]["index"]) for row in rows} == {
        (worker, index) for worker in range(8) for index in range(20)}
    assert all(row["actor"] == f"user{row['detail']['worker']}" for row in rows)


def test_partial_writes_are_completed_and_synced(tmp_path, monkeypatch):
    log = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
    log.record('alice','cli','key.create')
    log.db.close()
    assert audit.tail(tmp_path)[0][0]['actor'] == 'alice'
    with for_server_dir(tmp_path).transaction() as tx:
        assert tx.fetchone('PRAGMA synchronous')[0] == 2


def test_each_record_reopens_after_rotation(tmp_path):
    first = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
    first.record('alice','cli','key.create')
    second = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
    second.record('bob','cli','key.revoke')
    first.record('carol','cli','key.create')
    assert [r['actor'] for r in audit.tail(tmp_path)[0]] == ['carol','bob','alice']


def test_write_failures_log_at_most_once_per_minute(tmp_path, monkeypatch, caplog):
    log = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
    clock = iter([0,1,59.999,60,119.999,120])
    monkeypatch.setattr(audit, 'time', SimpleNamespace(monotonic=lambda:next(clock)))
    def fail(*args): raise DatabaseError('secret exception details')
    monkeypatch.setattr(log.repo, 'insert', fail)
    for _ in range(6): log.record('alice','cli','key.create')
    messages = [r.getMessage() for r in caplog.records if r.name == audit.logger.name]
    assert messages == ['could not write audit event (DatabaseError, errno=None)']*3
    assert str(tmp_path) not in caplog.text


@pytest.mark.parametrize('failure',['serialize','write','fsync'])
def test_record_failure_does_not_escape(tmp_path, monkeypatch, failure, caplog):
    log = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
    def fail(*args): raise OSError(errno.ENOSPC,'secret exception details','/secret/path')
    if failure != 'serialize': monkeypatch.setattr(log.repo, 'insert', fail)
    detail = {'invalid':object()} if failure == 'serialize' else None
    assert log.record('alice','cli','key.create',detail=detail) is None
    assert 'could not write audit event' in caplog.text
    assert 'secret exception details' not in caplog.text and '/secret/path' not in caplog.text


@pytest.mark.parametrize('failure',['write','zero_write','fsync'])
def test_record_rolls_back_failed_append(tmp_path, monkeypatch, failure):
    log = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
    log.repo.insert(event(0))
    original = log.repo.insert
    def fail(row):
        with log.db.transaction(write=True) as tx:
            original(row, tx)
            raise DatabaseError('disk full')
    with monkeypatch.context() as patch:
        patch.setattr(log.repo,'insert',fail)
        log.record('bob','cli','key.revoke')
    assert audit.tail(tmp_path) == ([event(0)],0)
    log.record('carol','cli','key.create')
    assert [r['actor'] for r in audit.tail(tmp_path)[0]] == ['carol','alice']


def test_rollback_failure_remains_best_effort(tmp_path, monkeypatch, caplog):
    log = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
    def fail(*args): raise DatabaseError('private failure')
    monkeypatch.setattr(log.repo,'insert',fail)
    assert log.record('alice','cli','key.create') is None
    assert '(DatabaseError, errno=None)' in caplog.text and 'private failure' not in caplog.text


def test_tail_newest_first_limit_and_strict_cursor(tmp_path):
    rows = [event(index) for index in range(6)]
    write_events(tmp_path, rows)
    assert audit.tail(tmp_path) == (list(reversed(rows)), 0)
    assert audit.tail(tmp_path, limit=2) == ([rows[5], rows[4]], 0)
    assert audit.tail(tmp_path, limit=2, before=rows[4]["ts"]) == ([rows[3], rows[2]], 0)
    assert audit.tail(tmp_path, before=rows[0]["ts"]) == ([], 0)


@pytest.mark.parametrize("suffix", [b'{"ts":', json.dumps(event(9)).encode()])
def test_tail_counts_torn_last_line(tmp_path, suffix, caplog):
    rows = [event(0), event(1)]
    write_events(tmp_path, rows, suffix)
    assert audit.tail(tmp_path, limit=1) == ([rows[1]], 0)
    assert audit.tail(tmp_path) == (list(reversed(rows)), 0)

    assert "skipped 1 malformed or unreadable legacy audit line(s)" in caplog.text


def test_tail_counts_malformed_lines_and_crosses_chunk_boundaries(tmp_path, caplog):
    rows = [event(0), event(1, detail={"values": ["é" * 256] * 100}), event(2)]
    path = write_events(tmp_path, rows)
    with path.open("ab") as file:
        file.write(b'bad\n\xff\n[]\n{}\n\n')
    assert path.stat().st_size > 2 * 64 * 1024
    assert audit.tail(tmp_path) == (list(reversed(rows)), 0)

    assert "skipped 5 malformed or unreadable legacy audit line(s)" in caplog.text


def test_tail_returns_large_detail_string_across_chunks(tmp_path):
    rows = [event(0), event(1, detail={"value": "x" * (2 * 64 * 1024 + 17)}), event(2)]
    write_events(tmp_path, rows)
    assert audit.tail(tmp_path) == (list(reversed(rows)), 0)


@pytest.mark.parametrize("extra", [0, 1])
def test_tail_line_size_boundary(tmp_path, extra):
    row = event(0, detail="")
    overhead = len(json.dumps(row).encode())
    row["detail"] = "x" * (1024 * 1024 - overhead + extra)
    write_events(tmp_path, [row])
    assert audit.tail(tmp_path) == (([row], 0) if extra == 0 else ([], 0))


@pytest.mark.parametrize("placement", ["only", "first", "middle", "last", "torn"])
def test_tail_skips_oversized_line_once_and_resumes(tmp_path, placement, caplog):
    garbage = b"x" * (2 * 1024 * 1024)
    rows = [event(0), event(1)]
    encoded = [(json.dumps(row) + "\n").encode() for row in rows]
    content = {
        "only": garbage,
        "first": garbage + b"\n" + b"".join(encoded),
        "middle": encoded[0] + garbage + b"\n" + encoded[1],
        "last": b"".join(encoded) + garbage + b"\n",
        "torn": b"".join(encoded) + garbage,
    }[placement]
    (tmp_path / "audit.jsonl").write_bytes(content)
    assert audit.tail(tmp_path) == ([] if placement == "only" else list(reversed(rows)), 0)

    assert "skipped 1 malformed or unreadable legacy audit line(s)" in caplog.text


def test_tail_caps_limit_and_stops_after_page(tmp_path):
    rows = [event(0, target=str(index)) for index in range(1005)]
    path = write_events(tmp_path, rows)
    path.write_bytes(b"bad\n" + path.read_bytes())
    assert audit.tail(tmp_path, limit=5000) == (list(reversed(rows))[:1000], 0)


def test_empty_and_missing_log(tmp_path):
    assert audit.tail(tmp_path) == ([], 0)
    (tmp_path / "audit.jsonl").touch()
    assert audit.tail(tmp_path) == ([], 0)
    assert audit.format_table([]) == ""


@pytest.mark.parametrize("limit", [0, -1, "2", True])
def test_tail_rejects_bad_limit(tmp_path, limit):
    with pytest.raises(ValueError, match="invalid limit"):
        audit.tail(tmp_path, limit=limit)


def test_cli_main_table_and_json(tmp_path, capsys):
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    rows = [event(0), event(1, actor="bob", target=None, client="203.0.113.7")]
    write_events(server_dir, rows)
    assert audit.cli_main(str(tmp_path), 1, False) == 0
    output = capsys.readouterr()
    assert not output.err
    assert output.out.splitlines()[0].split() == ["ts", "actor", "via", "action", "target", "client"]
    assert output.out.splitlines()[1].split() == [rows[1]["ts"], "bob", "cli", "key.create", "-", "203.0.113.7"]
    assert audit.cli_main(str(tmp_path), 50, True) == 0
    output = capsys.readouterr()
    assert not output.err and json.loads(output.out) == list(reversed(rows))


def test_cli_resolves_data_from_environment(tmp_path, monkeypatch, capsys):
    (tmp_path / "server").mkdir()
    rows = [event(0)]
    write_events(tmp_path / "server", rows)
    monkeypatch.setenv("AGENTICML_SERVER_DATA", str(tmp_path))
    assert audit.cli_main(None, 50, True) == 0
    assert json.loads(capsys.readouterr().out) == rows


def test_cli_absent_log_and_unreadable_log(tmp_path, capsys, monkeypatch):
    assert audit.cli_main(str(tmp_path), 50, False) == 0
    output = capsys.readouterr()
    assert not output.out and not output.err

    def denied(*args, **kwargs):
        raise PermissionError("audit permission denied")

    monkeypatch.setattr(AuditRepo, "tail", denied)
    assert audit.cli_main(str(tmp_path), 50, True) == 1
    output = capsys.readouterr()
    assert not output.out
    assert output.err == "agenticml server: audit permission denied\n"


def test_table_escapes_controls_in_every_column():
    hostile = "\x1b[31mevil\nforged\r\t\x00\x1f\x7f\x85\u2028\u200b"
    row = event(0, **{key: hostile for key in audit._COLUMNS})
    output = audit.format_table([row, event(1, action="login.failed", target="\x1b[31mevil\nforged")])
    assert len(output.splitlines()) == 3  # Header and exactly one line per event.
    assert "\x1b" not in output
    escaped = r"\u001b[31mevil\nforged\r\t\u0000\u001f\u007f\u0085\u2028\u200b"
    assert output.splitlines()[1].split() == [escaped] * len(audit._COLUMNS)
    assert all(char.isprintable() or char == "\n" for char in output)


def test_table_preserves_zero_false_and_printable_unicode():
    row = event(0, actor=0, via=False, action="", target="café", client=None)
    assert audit.format_table([row]).splitlines()[1].split() == [
        row["ts"], "0", "False", "-", "café", "-"]


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_main_exits_quietly_on_broken_pipe(tmp_path, monkeypatch, capsys, as_json):
    (tmp_path / "server").mkdir()
    write_events(tmp_path / "server", [event(0)])

    def broken_print(*args, **kwargs):
        raise BrokenPipeError()

    monkeypatch.setattr(audit, "print", broken_print, raising=False)
    assert audit.cli_main(str(tmp_path), 50, as_json) == 0
    output = capsys.readouterr()
    assert not output.out and not output.err


class StubHandler:
    def __init__(self, command="GET", query="", role="admin"):
        self.command = command
        self.path = "/api/server/audit" + ("?" + query if query else "")
        self.principal = Principal(user="alice", role=role, via="password")
        self.close_connection = False
        self.response = None

    def _json(self, body, status, headers):
        self.response = body, status, headers


def call_handle(tmp_path, **kwargs):
    handler = StubHandler(**kwargs)
    log = audit.AuditLog(tmp_path, workspace_id=default_workspace_id(tmp_path))
    handler.server = SimpleNamespace(audit=log)
    webauth = SimpleNamespace(keys=SimpleNamespace(server_dir=tmp_path))
    audit.handle(webauth, handler)
    return handler


@pytest.mark.parametrize("command", ["POST", "PUT", "DELETE", "HEAD"])
def test_handle_rejects_non_get(tmp_path, command):
    handler = call_handle(tmp_path, command=command)
    assert handler.response == ({"error": "method not allowed"}, 405,
                                {"Cache-Control": "no-store"})
    assert handler.close_connection


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_handle_forbids_non_admins(tmp_path, role):
    assert call_handle(tmp_path, role=role).response == (
        {"error": "forbidden"}, 403, {"Cache-Control": "no-store"})


@pytest.mark.parametrize("query", [
    "limit=", "limit=0", "limit=-1", "limit=%2B1", "limit=+1", "limit=1.5",
    "limit=1_0", "limit=1&limit=2", "limit=1&limit=1", "limit=1%0A",
    urlencode({"limit": "١"}), urlencode({"limit": "１２"}),
])
def test_handle_rejects_bad_limit(tmp_path, query):
    assert call_handle(tmp_path, query=query).response == (
        {"error": "invalid limit"}, 400, {"Cache-Control": "no-store"})


@pytest.mark.parametrize("before", [
    "", "garbage", "2026-11-06", "2026-11-06T00:00:00Z",
    "2026-11-06T00:00:00.00000Z", "2026-11-06T00:00:00.0000000Z",
    "2026-11-06T00:00:00x000000Z", "2026-11-06T00:00:00.000000+00:00",
    "2026-11-06T00:00:00.000000Z\n", "２０２６-11-06T00:00:00.000000Z",
])
def test_handle_rejects_bad_before(tmp_path, before):
    assert call_handle(tmp_path, query=urlencode({"before": before})).response == (
        {"error": "invalid before"}, 400, {"Cache-Control": "no-store"})


def test_handle_rejects_repeated_before(tmp_path):
    query = urlencode([("before", event(0)["ts"]), ("before", event(0)["ts"])])
    assert call_handle(tmp_path, query=query).response == (
        {"error": "invalid before"}, 400, {"Cache-Control": "no-store"})


def test_handle_returns_events_skips_and_no_store(tmp_path):
    rows = [event(0), event(1), event(2)]
    write_events(tmp_path, rows, suffix=b"torn")
    assert call_handle(tmp_path).response == (
        {"events": list(reversed(rows)), "skipped": 0}, 200, {"Cache-Control": "no-store"})
    stamp = layout.format_timestamp(layout.parse_timestamp(rows[2]["ts"]))
    query = urlencode({"limit": "01", "before": stamp})
    assert call_handle(tmp_path, query=query).response == (
        {"events": [rows[1]], "skipped": 0}, 200, {"Cache-Control": "no-store"})


def test_handle_unavailable_log(tmp_path, monkeypatch):
    def fail(*args, **kwargs): raise DatabaseError('private failure')
    monkeypatch.setattr(AuditRepo,'tail',fail)
    assert call_handle(tmp_path).response == ({'error':'audit log unavailable'},503,{'Cache-Control':'no-store'})


# Exercise hooks through the commands and HTTP endpoints that own the changes.
@pytest.fixture
def hook_env(monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    monkeypatch.setattr(audit.getpass, "getuser", lambda: "operator")


def hook_event(action, target, detail=None, *, actor="cli:operator", via="cli", client=None):
    return {"actor": actor, "via": via, "action": action, "target": target,
            "client": client, "detail": {} if detail is None else detail}


def hook_rows(data):
    return audit_rows(data / 'server')


@contextmanager
def expect_events(data, *expected):
    before = len(hook_rows(data))
    yield
    rows = hook_rows(data)[before:]
    for row in rows:
        assert audit._TIMESTAMP.fullmatch(row.pop("ts"))
    assert rows == list(expected)


def run_cli(data, capsys, *args, code=0):
    assert cli.main(["server", *args, "--data", str(data)]) == code
    return capsys.readouterr()


def http_event(action, target, detail=None, *, actor="alice", via="password", client="127.0.0.1"):
    return hook_event(action, target, detail, actor=actor, via=via, client=client)


def test_cli_mutations_and_audit_command(tmp_path, capsys, monkeypatch, hook_env):
    data = tmp_path / "data"
    with expect_events(data, hook_event("user.add", "admin", {"role": "owner", "identity": None})):
        run_cli(data, capsys, "users", "add", "admin", "--role", "admin", "--no-invite")
    # add's invite is a second action, never a duplicate user.add.
    output = run_cli(data, capsys, "users", "add", "alice", "--identity", "a@example.com")
    expires = UserStore(data / "server", workspace_id=default_workspace_id(data / "server")).get("alice").invite["expires_at"]
    assert [dict(row, ts=None) for row in hook_rows(data)[1:]] == [
        dict(hook_event("user.add", "alice", {"role": "member", "identity": "a@example.com"}), ts=None),
        dict(hook_event("user.invite", "alice", {"expires_at": expires}), ts=None)]
    assert "/invite?token=" in output.out
    with expect_events(data, hook_event("user.role", "alice", {"from": "member", "to": "viewer"})):
        run_cli(data, capsys, "users", "set-role", "alice", "viewer")
    with expect_events(data, hook_event("user.identity", "alice", {"identity": "new@example.com"})):
        run_cli(data, capsys, "users", "set-identity", "alice", "new@example.com")
    with expect_events(data, hook_event("user.identity", "alice", {"identity": None})):
        run_cli(data, capsys, "users", "set-identity", "alice")
    before = len(hook_rows(data))
    run_cli(data, capsys, "users", "invite", "alice")
    assert len(hook_rows(data)) == before + 1
    invite = hook_rows(data)[-1]
    assert {k: v for k, v in invite.items() if k != "ts"} == hook_event(
        "user.invite", "alice", {"expires_at": UserStore(data / "server", workspace_id=default_workspace_id(data / "server")).get("alice").invite["expires_at"]})
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    with expect_events(data, hook_event("user.password", "alice", {"method": "cli"})):
        run_cli(data, capsys, "users", "set-password", "alice", "--stdin")
    for name in ("box", "box2", "discard"):
        with expect_events(data, hook_event("key.create", name, {"user": "alice", "scope": "sync"})):
            run_cli(data, capsys, "keys", "create", "--name", name, "--user", "alice")
    with expect_events(data, hook_event("key.revoke", "discard", {"user": "alice", "scope": "sync"})):
        run_cli(data, capsys, "keys", "revoke", "discard")
    with expect_events(data,
                       hook_event("key.revoke", "box", {"user": "alice", "scope": "sync"}),
                       hook_event("key.revoke", "box2", {"user": "alice", "scope": "sync"}),
                       hook_event("user.remove", "alice", {"keys_revoked": 2})):
        run_cli(data, capsys, "users", "remove", "alice")
    with expect_events(data):
        run_cli(data, capsys, "users", "list")
        run_cli(data, capsys, "keys", "list")
        run_cli(data, capsys, "users", "remove", "admin", code=1)
        run_cli(data, capsys, "keys", "revoke", "missing", code=1)
        run_cli(data, capsys, "users", "add", "admin", "--no-invite", code=1)
    rows = hook_rows(data)
    with expect_events(data):
        output = run_cli(data, capsys, "audit", "--limit", "2", "--json")
        assert json.loads(output.out) == rows[-1:-3:-1]
        output = run_cli(data, capsys, "audit", "--limit", "1")
        assert output.out.splitlines()[0].split() == list(audit._COLUMNS)
        assert "user.remove" in output.out


@pytest.mark.parametrize("trigger", ["startup", "keys", "users", "up"])
def test_migration_actor_once(tmp_path, capsys, monkeypatch, hook_env, trigger):
    from agenticml.server import up

    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["old"])
    write_run(default_store(data), "old-run", "old")
    original_record = audit.AuditLog.record

    def record_unlocked(log, *args, **kwargs):
        # A separate fd must be able to acquire keys.lock during audit fsync.
        with (data / "server" / "keys.lock").open("ab") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return original_record(log, *args, **kwargs)

    monkeypatch.setattr(audit.AuditLog, "record", record_unlocked)
    actor, via = ("server", "server") if trigger == "startup" else ("cli:operator", "cli")
    if trigger == "up":
        monkeypatch.setattr(up, "check_tailscale", lambda: "https://test.example")
        monkeypatch.setattr(up, "install_unit", lambda *a, **kw: None)

    def migrate_once():
        if trigger == "startup":
            with running(data):
                pass
        elif trigger == "up":
            run_cli(data, capsys, "up", "--install")
        else:
            run_cli(data, capsys, trigger, "list")

    with expect_events(data, hook_event("migration", "admin", {"keys": 1, "runs": 1}, actor=actor, via=via)):
        migrate_once()
    with expect_events(data):
        migrate_once()


def test_server_up_bootstrap_once(tmp_path, capsys, monkeypatch, hook_env):
    from agenticml.server import up

    data = tmp_path / "data"
    monkeypatch.setattr(up, "check_tailscale", lambda: "https://test.example")
    monkeypatch.setattr(up, "install_unit", lambda *a, **kw: None)
    monkeypatch.setattr(up, "_stdout_is_tty", lambda: True)
    output = run_cli(data, capsys, "up", "--install")
    expires = UserStore(data / "server", workspace_id=default_workspace_id(data / "server")).get("admin").invite["expires_at"]
    rows = hook_rows(data)
    assert [{k: v for k, v in row.items() if k != "ts"} for row in rows] == [
        hook_event("user.add", "admin", {"role": "owner", "identity": None}),
        hook_event("user.invite", "admin", {"expires_at": expires})]
    token = output.out.split("token=", 1)[1].split()[0]
    assert token not in audit_text(data / 'server')
    with expect_events(data):
        run_cli(data, capsys, "up", "--install")


def test_setup_account_password_and_logout(tmp_path, hook_env):
    data = tmp_path / "data"
    with running(data) as server:
        token = server.web.setup_token
        with expect_events(data,
                           http_event("user.add", "alice", {"role": "owner", "identity": None}, via="setup"),
                           http_event("user.password", "alice", {"method": "setup"}, via="setup"),
                           http_event("setup", "alice", via="setup")):
            status, headers, _ = call(server, "/setup", "POST", json.dumps({
                "token": token, "name": "alice", "password": PASSWORD}), csrf(server))
            assert status == 201
        cookie = headers["Set-Cookie"]
        with expect_events(data):
            assert call(server, "/setup", "POST", json.dumps({
                "token": token, "name": "alice", "password": PASSWORD}), csrf(server))[0] == 404
        with expect_events(data, http_event("user.password", "alice", {"method": "account"}, via="setup")):
            status, headers, _, _ = authed(server, "/api/server/account/password", "POST", json.dumps({
                "current": PASSWORD, "new": PASSWORD + "new"}), cookie=cookie)
            assert status == 200
        cookie = headers["Set-Cookie"]
        with expect_events(data):
            assert authed(server, "/api/server/account/password", "POST", json.dumps({
                "current": "wrong-password", "new": PASSWORD}), cookie=cookie)[0] == 401
        with expect_events(data, http_event("logout", "alice")):
            assert authed(server, "/logout", "POST", cookie=cookie)[0] == 204
        with expect_events(data):
            assert authed(server, "/logout", "POST", cookie=cookie)[0] == 204
        with expect_events(data, http_event("login.ok", "alice", {"method": "password"})):
            assert password_login(server, "alice", PASSWORD + "new")[0] == 200
        raw = audit_text(data / 'server')
        assert all(secret not in raw for secret in (
            token, PASSWORD, PASSWORD + "new", cookie_parts(cookie)[1],
            server.users.get("alice").password["hash"],
            server.users.get("alice").password["salt"]))


def test_invite_keys_and_secret_exclusion(tmp_path, capsys, monkeypatch, hook_env):
    data = tmp_path / "data"
    output = run_cli(data, capsys, "users", "add", "alice", "--role", "admin")
    invite_token = parse_qs(urlsplit(output.out.splitlines()[0]).query)["token"][0]
    users = UserStore(data / "server", workspace_id=default_workspace_id(data / "server"))
    secrets = [invite_token, users.get("alice").invite["sha256"], PASSWORD]
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    run_cli(data, capsys, "users", "set-password", "alice", "--stdin")
    secrets.extend(users.get("alice").password[k] for k in ("salt", "hash"))
    output = run_cli(data, capsys, "keys", "create", "--name", "cli-key", "--scope", "admin")
    cli_key = output.out.strip()
    secrets.extend([cli_key, KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).list()[0].sha256])
    output = run_cli(data, capsys, "users", "invite", "alice")
    invite_token = parse_qs(urlsplit(output.out.splitlines()[0]).query)["token"][0]
    secrets.extend([invite_token, users.get("alice").invite["sha256"]])
    with running(data, trusted_proxies=("127.0.0.1",)) as server:
        client = "203.0.113.7"
        def expected(action, target="alice", detail=None, via="invite"):
            return http_event(action, target, detail, via=via, client=client)
        with expect_events(data,
                           expected("user.password", detail={"method": "invite"}),
                           expected("login.ok", detail={"method": "invite"})):
            status, headers, _ = call(server, "/invite", "POST", json.dumps({
                "token": invite_token, "password": PASSWORD}), csrf(server, **{"X-Forwarded-For": client}))
            assert status == 200
        secrets.extend(server.users.get("alice").password[k] for k in ("salt", "hash"))
        cookie = headers["Set-Cookie"]
        session_token = cookie_parts(cookie)[1]
        secrets.extend([session_token, hashlib.sha256(session_token.encode()).hexdigest()])
        with expect_events(data):
            assert call(server, "/invite", "POST", json.dumps({
                "token": invite_token, "password": PASSWORD}), csrf(server))[0] == 404
        with expect_events(data, expected("key.create", "http-key", {"user": "alice", "scope": "sync"})):
            status, _, body, _ = authed(server, "/api/server/keys", "POST", json.dumps({
                "name": "http-key", "scope": "sync"}), extra={"X-Forwarded-For": client}, cookie=cookie)
            assert status == 201
        secrets.extend([json.loads(body)["key"], server.keys.list()[-1].sha256])
        with expect_events(data, expected("key.revoke", "http-key", {"user": "alice", "scope": "sync"})):
            assert authed(server, "/api/server/keys/http-key", "DELETE",
                          extra=csrf(server, **{"X-Forwarded-For": client}), cookie=cookie)[0] == 200
        with expect_events(data):
            assert authed(server, "/api/server/keys/http-key", "DELETE", extra=csrf(server), cookie=cookie)[0] == 404
        with expect_events(data, http_event("login.ok", "alice", {"method": "key"}, via="key")):
            assert login(server, cli_key)[0] == 200
        with expect_events(data, http_event("login.failed", "alice", {"method": "password"}, actor="anonymous")):
            assert password_login(server, "alice", "wrong-password")[0] == 401
        with expect_events(data, http_event("login.failed", None, {"method": "key"}, actor="anonymous", via="key")):
            assert login(server, "bad-key")[0] == 401
        secrets.extend(["wrong-password", "bad-key"])
    raw = audit_text(data / 'server')
    assert all(secret and secret not in raw for secret in secrets)


@pytest.mark.parametrize("role,status", [("member", 403), ("viewer", 403), ("admin", 200)])
def test_audit_api_password_role_gate(tmp_path, capsys, hook_env, role, status):
    data = tmp_path / "data"
    run_cli(data, capsys, "users", "add", "alice", "--role", role, "--no-invite")
    UserStore(data / "server", workspace_id=default_workspace_id(data / "server")).set_password("alice", PASSWORD)
    with running(data) as server:
        with expect_events(data, http_event("login.ok", "alice", {"method": "password"})):
            code, _, headers = password_login(server, "alice", PASSWORD)
            assert code == 200
        with expect_events(data):
            code, headers, body, _ = authed(server, "/api/server/audit?limit=1", cookie=headers["Set-Cookie"])
            assert code == status
            assert headers["Cache-Control"] == "no-store"
            if status == 200:
                payload = json.loads(body)
                assert payload["skipped"] == 0 and payload["events"]
                assert payload["events"][0]["action"] != "login.ok"


@pytest.mark.parametrize("status", ["finished", "crashed"])
@pytest.mark.parametrize("env_key", [None, "env-secret-that-is-long-enough-1234"])
def test_run_lifecycle_and_notes(tmp_path, capsys, monkeypatch, hook_env, env_key, status):
    data = tmp_path / "data"
    run_cli(data, capsys, "users", "add", "alice", "--no-invite")
    users = UserStore(data / "server", workspace_id=default_workspace_id(data / "server"))
    users.set_password("alice", PASSWORD)
    secret = KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).create("box", user="alice")[1]
    actor, key = ("alice", "box") if env_key is None else ("env", "env")
    with running(data, env_key=env_key) as server:
        original_record = server.audit.record

        def record_unlocked(*args, **kwargs):
            if args[2] in ("run.create", "run.finish"):
                lock = server.run_lock(args[3], workspace_id=server.default_workspace_id)
                assert lock.acquire(blocking=False), "audit wrote while holding run_lock"
                lock.release()
            return original_record(*args, **kwargs)

        monkeypatch.setattr(server.audit, "record", record_unlocked)
        server.test_key = secret if env_key is None else env_key
        meta = {"run_id": "run-1", "status": "running", "project": "project"}
        def put_meta():
            return request(server, "/v2/runs/run-1/files/meta.json", "PUT", json.dumps(meta))[0]
        def commit(state):
            return request(server, "/v2/runs/run-1/commit", "POST", json.dumps({
                "status": state, "fingerprint": "a" * 64, "sizes": {}}))[0]
        with expect_events(data):
            assert request(server, "/v2/runs/run-1/files/meta.json", "PUT", json.dumps({
                "run_id": "run-1", "status": "invalid"}))[0] == 422
        with expect_events(data, http_event("run.create", "run-1", {"project": "project", "key": key}, actor=actor, via="key")):
            assert put_meta() == 200
        with expect_events(data):
            assert put_meta() == 200
            assert commit("running") == 200
            assert commit(status) == 409
            meta["status"] = status
            assert put_meta() == 200
        with expect_events(data, http_event("run.finish", "run-1", {"status": status, "key": key}, actor=actor, via="key")):
            assert commit(status) == 200
        with expect_events(data):
            assert commit(status) == 200
        # A second terminal status is also a transition; retries are silent.
        status = "crashed" if status == "finished" else "finished"
        with expect_events(data):
            meta["status"] = status
            assert put_meta() == 200
        with expect_events(data, http_event("run.finish", "run-1", {"status": status, "key": key}, actor=actor, via="key")):
            assert commit(status) == 200
        _, _, headers = password_login(server, "alice", PASSWORD)
        cookie = headers["Set-Cookie"]
        revision = hashlib.sha256(b"training note\n").hexdigest()
        with expect_events(data, http_event("note.write", "run-1", {"revision": revision})):
            code, _, body, _ = authed(server, "/api/runs/run-1/notes", "PUT", json.dumps({
                "text": "training note\n", "revision": None}), cookie=cookie)
            assert code == 200 and json.loads(body)["revision"] == revision
        with expect_events(data):
            assert authed(server, "/api/runs/run-1/notes", "PUT", json.dumps({
                "text": "conflict", "revision": None}), cookie=cookie)[0] == 409
    raw = audit_text(data / 'server')
    assert secret not in raw
    if env_key is not None:
        assert env_key not in raw


def test_login_failures_only_401_403_and_no_throttled_duplicates(tmp_path, hook_env):
    data = tmp_path / "data"
    with running(data) as server:
        server.users.add("alice", role="admin")
        server.users.add("former", role="admin")
        server.users.set_password("alice", PASSWORD)
        admin_key = server.keys.create("admin-key", "admin", user="alice")[1]
        sync_key = server.keys.create("sync-key", user="alice")[1]
        former_key = server.keys.create("former-key", "admin", user="former")[1]
        server.users.set_role("former", "member")
        for name in ("alice", "unknown"):
            with expect_events(data, http_event("login.failed", name, {"method": "password"}, actor="anonymous")):
                assert password_login(server, name, "wrong-password")[0] == 401
        for key, status in (("bad-key", 401), (sync_key, 403), (former_key, 403)):
            with expect_events(data, http_event("login.failed", None, {"method": "key"}, actor="anonymous", via="key")):
                assert login(server, key)[0] == status
        # CSRF rejection happens before the throttle and must not write audit lines.
        with expect_events(data):
            assert call(server, "/login", "POST", json.dumps({"key": admin_key}),
                        csrf(server, Origin="http://evil.example"))[0] == 403
        freeze_throttle(server)
        for _ in range(server.web.throttle.FREE_FAILURES):
            with expect_events(data, http_event("login.failed", None, {"method": "key"}, actor="anonymous", via="key")):
                assert login(server, "bad-key")[0] == 401
        with expect_events(data):
            assert login(server, "bad-key")[0] == 429
        freeze_throttle(server)
        with expect_events(data):
            assert call(server, "/login", "POST", json.dumps({"user": 42}), csrf(server))[0] == 400
        # A credential invalidated between verification and session creation is 401.
        server.web._between_verify_and_session = lambda user: server.users.set_password("alice", PASSWORD)
        with expect_events(data, http_event("login.failed", "alice", {"method": "password"}, actor="anonymous")):
            assert password_login(server, "alice", PASSWORD)[0] == 401
        with expect_events(data, http_event("login.failed", None, {"method": "key"}, actor="anonymous", via="key")):
            assert login(server, admin_key)[0] == 401
    raw = audit_text(data / 'server')
    assert all(secret not in raw for secret in (
        "bad-key", "wrong-password", PASSWORD, admin_key, sync_key, former_key))


@pytest.mark.parametrize("name", ["password typed here", "env", "..", "x" * 65])
def test_failed_login_invalid_name_is_not_logged(tmp_path, hook_env, name):
    data = tmp_path / "data"
    with running(data) as server:
        with expect_events(data, http_event("login.failed", None, {"method": "password"}, actor="anonymous")):
            assert password_login(server, name, "wrong-password")[0] == 401
    assert name not in audit_text(data / 'server')


def test_proxy_requests_do_not_log_logins_but_attribute_writes(tmp_path, hook_env):
    data = tmp_path / "data"
    with running(data, trusted_proxies=("127.0.0.1",), identity_header="oauth2-proxy") as server:
        server.users.add("alice", role="member", identity="alice@example.com")
        headers = {"X-Forwarded-Email": "alice@example.com", "X-Forwarded-For": "198.51.100.8"}
        with expect_events(data):
            for _ in range(2):
                assert call(server, "/api/runs", headers=headers)[0] == 200
                assert call(server, "/api/server/session", headers=headers)[0] == 200
        detail = {"user": "alice", "scope": "sync"}
        with expect_events(data, http_event("key.create", "box", detail, via="proxy", client="198.51.100.8")):
            assert call(server, "/api/server/keys", "POST", json.dumps({"name": "box", "scope": "sync"}),
                        csrf(server, **headers))[0] == 201
        with expect_events(data, http_event("key.revoke", "box", detail, via="proxy", client="198.51.100.8")):
            assert call(server, "/api/server/keys/box", "DELETE", headers=csrf(server, **headers))[0] == 200
        with expect_events(data):
            assert call(server, "/logout", "POST", headers=csrf(server, **headers))[0] == 204


def test_http_mutation_succeeds_with_symlinked_audit_file(tmp_path, hook_env):
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    with running(data) as server:
        server.users.add("alice", role="admin")
        server.users.set_password("alice", PASSWORD)
        (data / "server" / "audit.jsonl").symlink_to(outside)
        code, _, headers = password_login(server, "alice", PASSWORD)
        assert code == 200
        code, _, _, _ = authed(server, "/api/server/keys", "POST", json.dumps({
            "name": "box", "scope": "sync"}), cookie=headers["Set-Cookie"])
        assert code == 201
        assert [key.name for key in server.keys.list()] == ["box"]
        assert outside.read_bytes() == b"untouched"


@pytest.mark.parametrize("via", ["cli", "key"])
def test_first_key_also_attributes_bootstrap_user(tmp_path, capsys, hook_env, via):
    data = tmp_path / "data"
    if via == "cli":
        with expect_events(data,
                           hook_event("user.add", "admin", {"role": "owner", "identity": None}),
                           hook_event("key.create", "box", {"user": "admin", "scope": "admin"})):
            run_cli(data, capsys, "keys", "create", "--name", "box", "--scope", "admin")
    else:
        env_key = "env-key-secret-with-enough-characters"
        with running(data, env_key=env_key) as server:
            _, _, headers = login(server, env_key)
            with expect_events(data,
                               http_event("user.add", "admin", {"role": "admin", "identity": None}, actor="env", via="key"),
                               http_event("key.create", "box", {"user": "admin", "scope": "admin"}, actor="env", via="key")):
                assert authed(server, "/api/server/keys", "POST", json.dumps({
                    "name": "box", "scope": "admin"}), cookie=headers["Set-Cookie"])[0] == 201


@pytest.mark.parametrize("via", ["cli", "key"])
def test_concurrent_first_keys_attribute_bootstrap_user_once(tmp_path, monkeypatch, hook_env, via):
    data = tmp_path / "data"
    barrier = threading.Barrier(2)
    create = KeyStore.create

    def concurrent_create(self, *args, **kwargs):
        # Both callers reach create before either can bootstrap the admin.
        barrier.wait(timeout=5)
        return create(self, *args, **kwargs)

    monkeypatch.setattr(KeyStore, "create", concurrent_create)
    names = ["box-1", "box-2"]
    if via == "cli":
        def issue(name):
            return cli.main(["server", "keys", "create", "--name", name,
                             "--scope", "admin", "--data", str(data)])

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(issue, names)) == [0, 0]
        expected = hook_event
    else:
        with running(data, env_key="env-key-secret-with-enough-characters") as server:
            _, _, headers = login(server, "env-key-secret-with-enough-characters")

            def issue(name):
                return authed(server, "/api/server/keys", "POST", json.dumps({
                    "name": name, "scope": "admin"}), cookie=headers["Set-Cookie"])[0]

            with ThreadPoolExecutor(max_workers=2) as pool:
                assert list(pool.map(issue, names)) == [201, 201]

        def expected(action, target, detail):
            return http_event(action, target, detail, actor="env", via="key")

    rows = [{key: value for key, value in row.items() if key != "ts"}
            for row in hook_rows(data) if row["action"] != "login.ok"]
    # First account on an empty default workspace is the owner (package 18).
    # Dashboard key-create still records the 2.2-compatible presented role.
    bootstrap_role = "admin" if via == "key" else "owner"
    assert sorted(rows, key=lambda row: (row["action"], row["target"])) == [
        expected("key.create", name, {"user": "admin", "scope": "admin"}) for name in names
    ] + [expected("user.add", "admin", {"role": bootstrap_role, "identity": None})]
    assert [user.name for user in UserStore(data / "server", workspace_id=default_workspace_id(data / "server")).list()] == ["admin"]


@pytest.mark.parametrize("project", [["project"], {"name": "project"}])
def test_run_create_nonstring_project_is_null(tmp_path, hook_env, project):
    data = tmp_path / "data"
    with running(data) as server:
        server.test_key = server.keys.create("box")[1]
        with expect_events(data, http_event("run.create", "run-1", {"project": None, "key": "box"},
                                           actor="admin", via="key")):
            assert request(server, "/v2/runs/run-1/files/meta.json", "PUT", json.dumps({
                "run_id": "run-1", "status": "running", "project": project}))[0] == 200


def test_concurrent_run_create_and_finish_are_each_recorded_once(tmp_path, hook_env):
    data = tmp_path / "data"
    with running(data) as server:
        server.test_key = server.keys.create("box")[1]

        def race(path, method, body):
            barrier = threading.Barrier(4)

            def send(_):
                barrier.wait(timeout=5)
                return request(server, path, method, json.dumps(body))[0]

            with ThreadPoolExecutor(max_workers=4) as pool:
                assert list(pool.map(send, range(4))) == [200] * 4

        meta = {"run_id": "run-1", "status": "finished", "project": "project"}
        with expect_events(data, http_event("run.create", "run-1", {"project": "project", "key": "box"},
                                           actor="admin", via="key")):
            race("/v2/runs/run-1/files/meta.json", "PUT", meta)
        with expect_events(data, http_event("run.finish", "run-1", {"status": "finished", "key": "box"},
                                           actor="admin", via="key")):
            race("/v2/runs/run-1/commit", "POST", {
                "status": "finished", "fingerprint": "a" * 64, "sizes": {}})


@pytest.mark.parametrize("hook", ["webauth", "run.create", "run.finish", "key.create", "key.revoke", "user.add", "note.write"])
@pytest.mark.parametrize("failure", ["arguments", "record"])
def test_http_audit_helpers_catch_argument_and_record_failures(caplog, hook, failure):
    from agenticml.server import ingest, keyspage, webauth
    from agenticml.ui.server import DashboardHandler

    def fail(*args, **kwargs):
        raise ValueError("secret failure details")

    writer = SimpleNamespace(record=fail if failure == "record" else lambda *a, **kw: None)
    auth = SimpleNamespace(client_address=fail if failure == "arguments" else lambda h: "127.0.0.1")
    handler = SimpleNamespace(
        principal=Principal(user="alice", role="admin", via="key", key="box"),
        server=SimpleNamespace(audit=writer, web=auth, auth=auth))
    if hook == "webauth":
        webauth.WebAuth._audit(auth, handler, "login.ok", "alice", {"method": "key"})
    elif hook.startswith("run."):
        ingest.IngestHandler._audit_run(handler, "run-1", (hook, {"project": "project", "status": "finished"}))
    elif hook == "note.write":
        DashboardHandler._audit_note(handler, "run-1", {"revision": "revision"})
    else:
        record = SimpleNamespace(name="box", user="alice", scope="sync")
        keyspage._audit(auth, handler, hook, record)
    assert "could not record audit event" in caplog.text
    assert "secret failure details" not in caplog.text


def test_migration_succeeds_once_when_audit_writer_raises(tmp_path, monkeypatch, caplog):
    from agenticml.server.users import migrate

    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["old"])
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("secret failure details")

    monkeypatch.setattr(audit.AuditLog, "record", fail)
    assert migrate(data)
    assert UserStore(data / "server", workspace_id=default_workspace_id(data / "server")).get("admin").role == "owner"
    assert migrate(data)
    assert len(calls) == 1
    assert "could not record audit event" in caplog.text
    assert "secret failure details" not in caplog.text


def test_http_mutations_succeed_when_audit_writer_raises(tmp_path, monkeypatch, hook_env, caplog):
    data = tmp_path / "data"
    with running(data) as server:
        server.users.add("alice", role="admin")
        server.users.set_password("alice", PASSWORD)
        server.test_key = server.keys.create("sync", user="alice")[1]

        def fail(*args, **kwargs):
            raise RuntimeError("secret failure details")

        monkeypatch.setattr(server.audit, "record", fail)
        assert password_login(server, "alice", "wrong-password")[0] == 401
        code, _, headers = password_login(server, "alice", PASSWORD)
        assert code == 200
        cookie = headers["Set-Cookie"]
        assert authed(server, "/api/server/keys", "POST", json.dumps({
            "name": "box", "scope": "sync"}), cookie=cookie)[0] == 201
        assert authed(server, "/api/server/keys/box", "DELETE", extra=csrf(server), cookie=cookie)[0] == 200
        assert request(server, "/v2/runs/run-1/files/meta.json", "PUT", json.dumps({
            "run_id": "run-1", "status": "finished", "project": "project"}))[0] == 200
        assert request(server, "/v2/runs/run-1/commit", "POST", json.dumps({
            "status": "finished", "fingerprint": "a" * 64, "sizes": {}}))[0] == 200
        assert authed(server, "/api/runs/run-1/notes", "PUT", json.dumps({
            "text": "note", "revision": None}), cookie=cookie)[0] == 200
        assert authed(server, "/logout", "POST", cookie=cookie)[0] == 204
    assert "could not record audit event" in caplog.text
    assert "secret failure details" not in caplog.text

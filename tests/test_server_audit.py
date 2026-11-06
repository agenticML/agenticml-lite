"""Audit append safety, backwards paging, and standalone CLI rendering."""
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import logging
import os
import stat
import threading
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from agenticml.server import audit
from agenticml.server.users import Principal
from agenticml.store import layout


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
    monkeypatch.setattr(audit.layout, "format_timestamp", lambda: stamp)
    detail = {"identity": "é" * 300, "nested": [{"project": "x" * 300}],
              "count": 3, "missing": None, "control": "a\n\r\t\x00b"}
    audit.AuditLog(tmp_path).record("alice", "password", "key.create", "t" * 300,
                                    "203.0.113.7", detail)
    path = tmp_path / "audit.jsonl"
    raw = path.read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert raw.isascii() and b"\\u00e9" in raw and b"\\u0000" in raw
    row = json.loads(raw)
    assert list(row) == ["ts", "actor", "via", "action", "target", "client", "detail"]
    assert row == {"ts": stamp, "actor": "alice", "via": "password",
                   "action": "key.create", "target": "t" * 256,
                   "client": "203.0.113.7", "detail": {
                       **detail, "identity": "é" * 256,
                       "nested": [{"project": "x" * 256}]}}
    assert len(detail["identity"]) == 300
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("field", ["actor", "via", "action", "client"])
def test_record_truncates_all_top_level_strings(tmp_path, field):
    fields = {"actor": "alice", "via": "cli", "action": "key.create", field: "é" * 300}
    audit.AuditLog(tmp_path).record(**fields)
    assert json.loads((tmp_path / "audit.jsonl").read_bytes())[field] == "é" * 256


def test_cli_actor_fallback(monkeypatch):
    monkeypatch.setattr(audit.getpass, "getuser", lambda: "operator")
    assert audit.cli_actor() == "cli:operator"

    def unavailable():
        raise OSError("no user database")

    monkeypatch.setattr(audit.getpass, "getuser", unavailable)
    assert audit.cli_actor() == "cli:unknown"


def test_symlink_is_not_followed_and_record_does_not_raise(tmp_path, caplog):
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    (tmp_path / "audit.jsonl").symlink_to(outside)
    with caplog.at_level(logging.ERROR, logger=audit.logger.name):
        assert audit.AuditLog(tmp_path).record("alice", "cli", "key.create") is None
    assert outside.read_bytes() == b"unchanged"
    assert "could not write audit.jsonl" in caplog.text
    with pytest.raises(OSError):
        audit.tail(tmp_path)


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_nonregular_file_is_refused(tmp_path, kind, caplog):
    path = tmp_path / "audit.jsonl"
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    errors = []

    def check():
        try:
            audit.AuditLog(tmp_path).record("alice", "cli", "key.create")
            with pytest.raises((ValueError, OSError)):
                audit.tail(tmp_path)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=check, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "audit access blocked on a nonregular file"
    if errors:
        raise errors[0]
    assert "could not write audit.jsonl" in caplog.text


def test_eight_concurrent_writers_produce_whole_lines(tmp_path):
    barrier = threading.Barrier(8)

    def write(worker):
        # Separate objects also exercise the file lock rather than a shared lock.
        log = audit.AuditLog(tmp_path)
        barrier.wait(timeout=10)
        for index in range(20):
            log.record(f"user{worker}", "cli", "note.write", f"run{index}",
                       detail={"worker": worker, "index": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(8)))
    lines = (tmp_path / "audit.jsonl").read_bytes().splitlines()
    assert len(lines) == 160
    rows = [json.loads(line) for line in lines]
    assert {(row["detail"]["worker"], row["detail"]["index"]) for row in rows} == {
        (worker, index) for worker in range(8) for index in range(20)}
    assert all(row["actor"] == f"user{row['detail']['worker']}" for row in rows)


def test_partial_writes_are_completed_and_synced(tmp_path, monkeypatch):
    original_write, original_fsync = os.write, os.fsync
    writes, synced = [], []

    def short_write(fd, data):
        writes.append(len(data))
        return original_write(fd, data[:7])

    def fsync(fd):
        synced.append(fd)
        return original_fsync(fd)

    patch_os(monkeypatch, write=short_write, fsync=fsync)
    audit.AuditLog(tmp_path).record("alice", "cli", "key.create")
    assert len(writes) > 1 and len(synced) == 1
    assert json.loads((tmp_path / "audit.jsonl").read_bytes())["actor"] == "alice"


def test_each_record_reopens_after_rotation(tmp_path):
    log = audit.AuditLog(tmp_path)
    log.record("alice", "cli", "key.create")
    rotated = tmp_path / "audit.jsonl.1"
    log.path.rename(rotated)
    log.record("bob", "cli", "key.revoke")
    assert json.loads(rotated.read_bytes())["actor"] == "alice"
    assert json.loads(log.path.read_bytes())["actor"] == "bob"


def test_write_failures_log_at_most_once_per_minute(tmp_path, monkeypatch, caplog):
    clock = iter([0, 1, 59.999, 60, 119.999, 120])
    monkeypatch.setattr(audit, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    log = audit.AuditLog(tmp_path / "missing")
    for _ in range(6):
        log.record("alice", "cli", "key.create")
    messages = [record.getMessage() for record in caplog.records
                if record.name == audit.logger.name]
    assert messages == [f"could not write audit.jsonl (FileNotFoundError, errno={errno.ENOENT})"] * 3
    assert str(tmp_path) not in caplog.text


@pytest.mark.parametrize("failure", ["serialize", "write", "fsync"])
def test_record_failure_does_not_escape(tmp_path, monkeypatch, failure, caplog):
    def fail(*args):
        raise OSError(errno.ENOSPC, "secret exception details", "/secret/path")

    if failure == "write":
        patch_os(monkeypatch, write=lambda *args: 0)
    elif failure == "fsync":
        patch_os(monkeypatch, fsync=fail)
    detail = {"invalid": object()} if failure == "serialize" else None
    assert audit.AuditLog(tmp_path).record("alice", "cli", "key.create", detail=detail) is None
    assert "could not write audit.jsonl" in caplog.text
    assert "secret exception details" not in caplog.text
    assert "/secret/path" not in caplog.text
    expected = {"serialize": "TypeError, errno=None", "write": "OSError, errno=None",
                "fsync": f"OSError, errno={errno.ENOSPC}"}[failure]
    assert f"({expected})" in caplog.text


@pytest.mark.parametrize("failure", ["write", "zero_write", "fsync"])
def test_record_rolls_back_failed_append(tmp_path, monkeypatch, failure):
    rows = [event(0)]
    path = write_events(tmp_path, rows)
    original = path.read_bytes()
    writes = 0

    def failing_write(fd, data):
        nonlocal writes
        writes += 1
        if writes == 1:
            return os.write(fd, data[:7])
        if failure == "zero_write":
            return 0
        raise OSError(errno.ENOSPC, "disk full")

    def failing_fsync(fd):
        raise OSError(errno.EIO, "sync failed")

    with monkeypatch.context() as patch:
        if failure == "fsync":
            patch_os(patch, fsync=failing_fsync)
        else:
            patch_os(patch, write=failing_write)
        audit.AuditLog(tmp_path).record("bob", "cli", "key.revoke")
    assert path.read_bytes() == original
    audit.AuditLog(tmp_path).record("carol", "cli", "key.create")
    events, skipped = audit.tail(tmp_path)
    assert [row["actor"] for row in events] == ["carol", "alice"]
    assert skipped == 0


def test_rollback_failure_remains_best_effort(tmp_path, monkeypatch, caplog):
    def fail(*args):
        raise OSError(errno.EIO, "private failure")

    patch_os(monkeypatch, fsync=fail, ftruncate=fail)
    assert audit.AuditLog(tmp_path).record("alice", "cli", "key.create") is None
    assert f"(OSError, errno={errno.EIO})" in caplog.text
    assert "private failure" not in caplog.text


def test_tail_newest_first_limit_and_strict_cursor(tmp_path):
    rows = [event(index) for index in range(6)]
    write_events(tmp_path, rows)
    assert audit.tail(tmp_path) == (list(reversed(rows)), 0)
    assert audit.tail(tmp_path, limit=2) == ([rows[5], rows[4]], 0)
    assert audit.tail(tmp_path, limit=2, before=rows[4]["ts"]) == ([rows[3], rows[2]], 0)
    assert audit.tail(tmp_path, before=rows[0]["ts"]) == ([], 0)


@pytest.mark.parametrize("suffix", [b'{"ts":', json.dumps(event(9)).encode()])
def test_tail_counts_torn_last_line(tmp_path, suffix):
    rows = [event(0), event(1)]
    write_events(tmp_path, rows, suffix)
    assert audit.tail(tmp_path, limit=1) == ([rows[1]], 1)
    assert audit.tail(tmp_path) == (list(reversed(rows)), 1)


def test_tail_counts_malformed_lines_and_crosses_chunk_boundaries(tmp_path):
    rows = [event(0), event(1, detail={"values": ["é" * 256] * 100}), event(2)]
    path = write_events(tmp_path, rows)
    with path.open("ab") as file:
        file.write(b'bad\n\xff\n[]\n{}\n\n')
    assert path.stat().st_size > 2 * 64 * 1024
    assert audit.tail(tmp_path) == (list(reversed(rows)), 5)


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
    assert audit.tail(tmp_path) == (([row], 0) if extra == 0 else ([], 1))


@pytest.mark.parametrize("placement", ["only", "first", "middle", "last", "torn"])
def test_tail_skips_oversized_line_once_and_resumes(tmp_path, placement):
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
    assert audit.tail(tmp_path) == ([] if placement == "only" else list(reversed(rows)), 1)


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

    monkeypatch.setattr(audit, "tail", denied)
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
        {"events": list(reversed(rows)), "skipped": 1}, 200, {"Cache-Control": "no-store"})
    stamp = layout.format_timestamp(layout.parse_timestamp(rows[2]["ts"]))
    query = urlencode({"limit": "01", "before": stamp})
    assert call_handle(tmp_path, query=query).response == (
        {"events": [rows[1]], "skipped": 1}, 200, {"Cache-Control": "no-store"})


def test_handle_unavailable_log(tmp_path):
    (tmp_path / "audit.jsonl").mkdir()
    assert call_handle(tmp_path).response == (
        {"error": "audit log unavailable"}, 503, {"Cache-Control": "no-store"})

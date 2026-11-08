"""Protocol 2 sync against the real ingest server and the real local writer."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import hashlib
import json
import os
import shutil
import struct
import threading

import pytest
import requests

from agenticml import cli
from agenticml.client import Client, Conflict
from agenticml.server.app import make_server
from agenticml.server.keys import KeyStore
from test_server_ingest import foreign_key
from agenticml.store import RunReader, RunWriter
from agenticml.store.gc import run_fingerprint, usage
from agenticml.store.layout import object_path
from agenticml.store.overview import OVERVIEW_MIN_POINTS
from agenticml.store.reader import references_from_bytes, run_references
from agenticml import sync
from server_db_helpers import default_workspace_id, isolate_server_dir


@contextmanager
def running(data):
    server = make_server(data, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    for name in ("AGENTICML_HOST", "AGENTICML_API_KEY", "AGENTICML_SERVER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    with running(tmp_path / "data") as server:
        _, key = KeyStore(tmp_path / "data" / "server", workspace_id=default_workspace_id(tmp_path / "data" / "server")).create("box", scope="sync")
        url = "http://127.0.0.1:%d" % server.server_address[1]
        with Client(url, key, timeout=2, max_retries=0) as client:
            yield server, client


def writer_at(tmp_path, *, run_id="run-1", status="finished", rich=False, count=12):
    root = tmp_path / "local"
    writer = RunWriter.create(root, run_id=run_id, meta={
        "project": "demo", "name": "train", "task": "classify", "task_source": "explicit",
        "arch": "linear"})
    writer.write_scalars([(i, {"loss": 1 / (i + 1), "acc": i / count, "extra": float(i)}) for i in range(count)])
    if rich:
        writer.add_media("sample", 1, b"preview", "text/plain", "caption")
        artifact = tmp_path / "weights.bin"
        artifact.write_bytes(b"weights")
        writer.add_artifact(artifact, "weights", "model", {}, copy=True)
        writer.add_code([], [("train.py", b"print('train')\n")])
        writer.add_code([], [("train.py", b"print('new version')\n")])
    if status:
        writer.finish(status)
    return writer


def push(remote, writer, **kwargs):
    _, client = remote
    result = sync.SyncResult(writer.run_id)
    sync.sync_run(RunReader(writer.root, writer.run_id), client.base_url, client, result, **kwargs)
    return result


def command(remote, writer, *args, key=None):
    _, client = remote
    return cli.main(["sync", *args, "--host", client.base_url, "--api-key", key or client.api_key,
                     "--dir", str(writer.root)])


def state(remote, writer):
    return json.loads((writer.run_dir / "sync.json").read_bytes())["hosts"][remote[1].base_url]


def allowed(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob("*")
            if path.is_file() and (sync._ATOMIC.fullmatch(path.relative_to(directory).as_posix())
                                   or sync._APPEND.fullmatch(path.relative_to(directory).as_posix()))}


def identical(remote, writer, *, dead=False):
    expected = allowed(writer.run_dir)
    if dead:
        meta = json.loads(expected["meta.json"])
        meta["status"] = "crashed"
        expected["meta.json"] = json.dumps(meta).encode()
    assert allowed(remote[0].root / "runs" / writer.run_id) == expected
    for sha in set().union(*run_references(writer.root, writer.run_id).values()):
        assert object_path(remote[0].root, sha).read_bytes() == object_path(writer.root, sha).read_bytes()
    return remote[0].run_index.as_remote(writer.run_id)


@pytest.mark.parametrize("status", ["finished", "crashed"])
def test_finished_round_trip(remote, tmp_path, status, capsys):
    writer = writer_at(tmp_path, status=status, rich=True)
    assert command(remote, writer, writer.run_id) == 0
    assert "synced" in capsys.readouterr().out
    assert identical(remote, writer)["status"] == status
    saved = state(remote, writer)
    assert saved["protocol"] == 2 and saved["complete"]
    assert saved["source_hash"] == run_fingerprint(writer.root, writer.run_id)
    assert saved["files"]["meta.json"]["sha256"] == hashlib.sha256((writer.run_dir / "meta.json").read_bytes()).hexdigest()
    meta = json.loads((remote[0].root / "runs" / writer.run_id / "meta.json").read_bytes())
    assert {name: meta[name] for name in ("task", "task_source", "arch")} == {
        "task": "classify", "task_source": "explicit", "arch": "linear"}


@pytest.mark.parametrize("kind", ["code", "artifact"])
def test_sync_empty_object(remote, tmp_path, kind):
    writer = writer_at(tmp_path, status=None)
    try:
        if kind == "code":
            writer.add_code([], [("empty.py", b"")])
        else:
            artifact = tmp_path / "empty.bin"
            artifact.write_bytes(b"")
            writer.add_artifact(artifact, "empty", "model", {}, copy=True)
        writer.finish("finished")
    finally:
        writer.close()

    sha = hashlib.sha256(b"").hexdigest()
    assert sha in set().union(*run_references(writer.root, writer.run_id).values())
    assert not object_path(remote[0].root, sha).exists()
    assert command(remote, writer, writer.run_id) == 0
    assert object_path(remote[0].root, sha).read_bytes() == b""
    assert state(remote, writer)["complete"] is True
    assert identical(remote, writer)["status"] == "finished"


def test_dead_process_only_changes_status(remote, tmp_path):
    writer = writer_at(tmp_path, status=None)
    writer.close()
    os.utime(writer.run_dir / "run.lock", (0, 0))
    assert push(remote, writer).complete
    assert identical(remote, writer, dead=True)["status"] == "crashed"
    assert json.loads((writer.run_dir / "meta.json").read_bytes())["status"] == "running"


def test_running_snapshots_and_growth(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path, status=None)
    try:
        result = push(remote, writer)
        assert result.status == "partial (running)" and not result.complete
        assert identical(remote, writer)["status"] == "running"
        original = remote[1].missing
        before = (writer.run_dir / "scalars/0.bin").read_bytes()
        def grow(hashes):
            writer.write_scalars([(100, {"loss": .1})])
            return original(hashes)
        monkeypatch.setattr(remote[1], "missing", grow)
        push(remote, writer)
        assert (remote[0].root / "runs" / writer.run_id / "scalars/0.bin").read_bytes() == before
        monkeypatch.setattr(remote[1], "missing", original)
        push(remote, writer)
        identical(remote, writer)
    finally:
        writer.close()


def test_finished_idle_makes_no_requests(remote, tmp_path, monkeypatch, capsys):
    writer = writer_at(tmp_path)
    push(remote, writer)
    def forbidden(*args, **kwargs):
        pytest.fail("idle run made a request")
    monkeypatch.setattr(remote[1].session, "request", forbidden)
    assert push(remote, writer).status == "up to date"
    assert sync._selected_ids(writer.root, remote[1].base_url, [], True) == []
    # cmd_sync still performs its one preflight request, before run selection.


def test_interrupted_append_resumes_without_duplicates(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path, count=100)
    monkeypatch.setattr(sync, "APPEND_CHUNK", 64)
    original = remote[1].append
    calls = []
    def interrupt(*args, **kwargs):
        size = original(*args, **kwargs)
        calls.append(args)
        if len(calls) == 2:
            raise requests.ConnectionError("response lost")
        return size
    monkeypatch.setattr(remote[1], "append", interrupt)
    with pytest.raises(requests.ConnectionError):
        push(remote, writer)
    assert state(remote, writer)["files"]
    monkeypatch.setattr(remote[1], "append", original)
    assert push(remote, writer).complete
    identical(remote, writer)


@pytest.mark.parametrize("reset", [False, True])
def test_cursor_recovery_sends_no_append_bytes(remote, tmp_path, monkeypatch, reset):
    writer = writer_at(tmp_path, rich=True)
    push(remote, writer)
    if reset:
        assert command(remote, writer, "--reset", writer.run_id) == 0
    else:
        (writer.run_dir / "sync.json").unlink()
    def forbidden(*args, **kwargs):
        pytest.fail("matching prefix should not be appended again")
    monkeypatch.setattr(remote[1], "append", forbidden)
    result = push(remote, writer)
    assert result.complete and result.bytes == 0
    identical(remote, writer)


@pytest.mark.parametrize("change", ["same", "smaller", "larger"])
def test_rollback_rewrites_once(remote, tmp_path, change):
    writer = writer_at(tmp_path, status=None)
    try:
        push(remote, writer)
        path = writer.run_dir / "scalars/0.bin"
        data = path.read_bytes()
        changed = struct.pack("<If", 0, 99.) + data[8:32 if change == "smaller" else len(data)]
        if change == "larger":
            changed += struct.pack("<If", 100, 98.)
        path.write_bytes(changed)
        result = push(remote, writer)
        assert result.rewritten == 1
        assert result.warnings == ["rewrote scalars/0.bin after a local rollback"]
        identical(remote, writer)
    finally:
        writer.close()


def test_resumed_finished_run_reopens_remote_for_rewrite(remote, tmp_path):
    writer = writer_at(tmp_path)
    push(remote, writer)
    writer = RunWriter.resume(writer.root, writer.run_id, must=True)
    try:
        path = writer.run_dir / "scalars/0.bin"
        path.write_bytes(struct.pack("<If", 0, 42.))
        assert push(remote, writer).rewritten == 1
        assert identical(remote, writer)["status"] == "running"
    finally:
        writer.close()


def test_terminal_remote_rewrite_fails_clearly(remote, tmp_path):
    writer = writer_at(tmp_path)
    push(remote, writer)
    (writer.run_dir / "scalars/0.bin").write_bytes(struct.pack("<If", 0, 42.))
    with pytest.raises(ValueError, match="server copy is finished; cannot rewrite scalars/0.bin"):
        push(remote, writer)


def test_chunking_torn_event_and_binary_record(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path, rich=True, count=100)
    events = writer.run_dir / "events.jsonl"
    original_events = events.read_bytes()
    with events.open("ab") as handle:
        handle.write(b'{"kind":"unfinished"')
    binary = writer.run_dir / "scalars/0.bin"
    original_binary = binary.read_bytes()
    with binary.open("ab") as handle:
        handle.write(b"torn")
    monkeypatch.setattr(sync, "APPEND_CHUNK", 64)
    original_append = remote[1].append
    calls = []
    def counted(*args, **kwargs):
        calls.append(args)
        return original_append(*args, **kwargs)
    monkeypatch.setattr(remote[1], "append", counted)
    result = push(remote, writer)
    assert result.complete and len(calls) > 30
    assert result.files == len(allowed(writer.run_dir))
    directory = remote[0].root / "runs" / writer.run_id
    assert (directory / "events.jsonl").read_bytes() == original_events
    assert (directory / "scalars/0.bin").read_bytes() == original_binary


def test_server_backup_recovery(remote, tmp_path):
    writer = writer_at(tmp_path, status=None)
    try:
        push(remote, writer)
        path = remote[0].root / "runs" / writer.run_id / "scalars/0.bin"
        path.write_bytes(path.read_bytes()[:8])
        writer.write_scalars([(100, {"loss": .2})])
        assert not push(remote, writer).failed
        identical(remote, writer)
    finally:
        writer.close()


def test_prune_preserves_remote_and_gc_synced(remote, tmp_path, capsys):
    writer = writer_at(tmp_path, count=OVERVIEW_MIN_POINTS + 1)
    assert command(remote, writer, writer.run_id, "--prune") == 0
    assert not (writer.run_dir / "scalars/0.bin").exists()
    remote_bin = remote[0].root / "runs" / writer.run_id / "scalars/0.bin"
    content = remote_bin.read_bytes()
    assert push(remote, writer).status == "up to date"
    assert usage(writer.root)["runs"][0]["synced"]
    assert command(remote, writer, "--reset", writer.run_id) == 0
    assert push(remote, writer).complete
    assert remote_bin.read_bytes() == content
    assert "scalars/0.bin" not in remote[0].run_index.as_remote(writer.run_id)["sizes"]
    isolate_server_dir(tmp_path / "second" / "server")
    with running(tmp_path / "second") as server:
        _, key = server.keys.create("box")
        with Client("http://127.0.0.1:%d" % server.server_address[1], key) as client:
            result = push((server, client), writer)
            assert result.failed and result.permanent
            assert "full scalar history is no longer local" in result.status


def test_v1_entry_replaced_and_other_host_preserved(remote, tmp_path):
    writer = writer_at(tmp_path)
    other = "https://legacy.example.com"
    old = {"remote_run_id": "old", "offsets": {"0": 12}, "extra": [1, "legacy"]}
    path = writer.run_dir / "sync.json"
    path.write_text(json.dumps({"version": 1, "hosts": {remote[1].base_url: old, other: old}}))
    assert push(remote, writer).complete
    saved = json.loads(path.read_bytes())["hosts"]
    assert saved[remote[1].base_url]["protocol"] == 2
    assert saved[other] == old


def test_ownership_failure_does_not_stop_other_runs(remote, tmp_path, capsys):
    writer = writer_at(tmp_path)
    push(remote, writer)
    key = foreign_key(remote[0], "second")
    other = writer_at(tmp_path, run_id="other")
    assert command(remote, writer, writer.run_id, other.run_id, key=key) == 1
    out = capsys.readouterr().out
    assert "run id run-1 belongs to user admin on this server" in out
    assert "other  demo/train  synced" in out
    assert (remote[0].root / "runs/other/meta.json").exists()


def test_ownership_recovery_checks_before_objects(remote, tmp_path):
    writer = writer_at(tmp_path)
    push(remote, writer)
    (writer.run_dir / "sync.json").unlink()
    key = foreign_key(remote[0], "second")
    with Client(remote[1].base_url, key) as client:
        with pytest.raises(sync.OwnershipError, match="belongs to user admin"):
            push((remote[0], client), writer)


def test_revoked_key_preflight_touches_no_run(remote, tmp_path, capsys):
    writer = writer_at(tmp_path)
    remote[0].keys.revoke("box")
    assert command(remote, writer, writer.run_id) == 1
    assert "API key rejected" in capsys.readouterr().err
    assert not (writer.run_dir / "sync.json").exists()
    assert not (writer.run_dir / "sync.lock").exists()


def test_non_v2_preflight_touches_no_run(remote, tmp_path, capsys):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        writer = writer_at(tmp_path)
        assert cli.main(["sync", writer.run_id, "--dir", str(writer.root), "--host",
                         "http://127.0.0.1:%d" % server.server_port, "--api-key", remote[1].api_key]) == 1
        assert "not an agenticml server with protocol 2" in capsys.readouterr().err
        assert not (writer.run_dir / "sync.json").exists()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("problem", ["symlink", "hardlink", "missing", "mismatch", "byref"])
def test_object_problems(remote, tmp_path, problem):
    writer = writer_at(tmp_path, rich=True)
    sha = next(iter(run_references(writer.root, writer.run_id)["media"]))
    path = object_path(writer.root, sha)
    content = path.read_bytes()
    if problem != "byref":
        path.unlink()
    if problem in ("symlink", "hardlink"):
        outside = tmp_path / "outside"
        outside.write_bytes(content)
        path.symlink_to(outside) if problem == "symlink" else os.link(outside, path)
    elif problem == "mismatch":
        path.write_bytes(b"bad object")
    elif problem == "byref":
        # A by-reference artifact is metadata only, even if its source disappeared.
        writer = RunWriter.resume(writer.root, writer.run_id, must=True)
        artifact = tmp_path / "byref"
        artifact.write_bytes(b"reference")
        writer.add_artifact(artifact, "reference", "model", {}, copy=False)
        writer.finish("finished")
        artifact.unlink()
    result = push(remote, writer)
    assert result.complete == (problem != "missing")
    assert result.status == ("partial" if problem == "missing" else "synced")
    if problem != "byref":
        assert len(result.warnings) == 1
    if problem in ("symlink", "hardlink", "mismatch"):
        assert result.skipped == 1 and len(state(remote, writer)["skipped"]) == 1
        assert push(remote, writer).status == "up to date"
    if problem == "missing":
        assert push(remote, writer).warnings


def test_lineage_order(remote, tmp_path, monkeypatch, capsys):
    parent = writer_at(tmp_path, run_id="parent")
    child = writer_at(tmp_path, run_id="child")
    path = child.run_dir / "meta.json"
    meta = json.loads(path.read_bytes())
    meta["parent_run_id"] = parent.run_id
    path.write_text(json.dumps(meta))
    assert command(remote, child, "child", "parent") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("parent ") and lines[1].startswith("child ")


def test_dry_run_no_client_or_state_write(remote, tmp_path, monkeypatch, capsys):
    writer = writer_at(tmp_path, rich=True)
    push(remote, writer)
    before = (writer.run_dir / "sync.json").read_bytes()
    path = writer.run_dir / "scalars/0.bin"
    with path.open("ab") as handle:
        handle.write(struct.pack("<If", 100, .1))
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run constructed a client")
    monkeypatch.setattr("agenticml.client.Client", forbidden)
    assert command(remote, writer, writer.run_id, "--dry-run") == 0
    assert (writer.run_dir / "sync.json").read_bytes() == before
    out = capsys.readouterr().out
    assert "objects unchecked" in out and "bytes=8" in out


@pytest.mark.parametrize("problem", ["symlink", "hardlink"])
@pytest.mark.parametrize("name", ["scalars/0.bin", "events.jsonl", "media/index.json", "scalars/index.json"])
def test_run_file_links_warn_and_are_not_sent(remote, tmp_path, problem, name):
    writer = writer_at(tmp_path)
    path = writer.run_dir / name
    outside = tmp_path / "outside"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside) if problem == "symlink" else os.link(outside, path)
    result = push(remote, writer)
    assert len(result.warnings) == 1
    assert not (remote[0].root / "runs" / writer.run_id / name).exists()


def test_objects_precede_files_and_exclusions(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path, rich=True)
    for name in ("notes.md", "remote.json", "unlisted"):
        (writer.run_dir / name).write_bytes(b"private")
    original = remote[1].put_file
    refs = set().union(*run_references(writer.root, writer.run_id).values())
    def ordered(*args):
        assert all(object_path(remote[0].root, sha).exists() for sha in refs)
        return original(*args)
    monkeypatch.setattr(remote[1], "put_file", ordered)
    push(remote, writer)
    directory = remote[0].root / "runs" / writer.run_id
    assert not any((directory / name).exists() for name in ("notes.md", "sync.json", "run.lock", "sync.lock", "unlisted"))


def test_reference_helper_keeps_all_code_and_rejects_corrupt_line():
    first, second = "a" * 64, "b" * 64
    events = b"\n".join(json.dumps(row).encode() for row in [
        {"kind": "code", "manifest": [{"path": "a", "sha256": first}]},
        {"kind": "code", "manifest": [{"path": "a", "sha256": second}]},
        {"kind": "artifact", "sha256": first, "copy": False}])
    assert references_from_bytes(b"{}", events, "id") == {"media": set(), "artifacts": set(), "code": {first, second}}
    with pytest.raises(ValueError):
        references_from_bytes(b"{}", b"{broken}\n", "id")


def test_commit_size_conflict_recovers_and_retries(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path)
    original = remote[1].commit
    calls = []
    def restore(*args):
        calls.append(args)
        if len(calls) == 1:
            path = remote[0].root / "runs" / writer.run_id / "scalars/0.bin"
            path.write_bytes(path.read_bytes()[:8])
        return original(*args)
    monkeypatch.setattr(remote[1], "commit", restore)
    assert push(remote, writer).complete
    assert len(calls) == 2
    identical(remote, writer)


def test_commit_status_conflict_resends_meta(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path)
    original = remote[1].commit
    calls = []
    def changed(*args):
        calls.append(args)
        if len(calls) == 1:
            path = remote[0].root / "runs" / writer.run_id / "meta.json"
            meta = json.loads(path.read_bytes())
            meta["status"] = "running"
            # Keep byte length identical so the first conflict is about status.
            data = json.dumps(meta).encode()
            path.write_bytes(data + b" " * (path.stat().st_size - len(data)))
        return original(*args)
    monkeypatch.setattr(remote[1], "commit", changed)
    assert push(remote, writer).complete
    assert len(calls) == 2
    identical(remote, writer)


def test_second_append_conflict_fails_without_loop(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path)
    calls = []
    def conflict(*args, **kwargs):
        calls.append(args)
        raise Conflict({"size": 0, "mismatch": True})
    monkeypatch.setattr(remote[1], "append", conflict)
    with pytest.raises(Conflict):
        push(remote, writer)
    assert len(calls) == 2


@pytest.mark.parametrize("code,expected,permanent", [(401, "API key rejected", True), (403, "HTTP 403: refused", True),
                                                       (408, "HTTP 408: refused", False), (429, "HTTP 429: refused", False)])
def test_short_error(code, expected, permanent):
    response = requests.Response()
    response.status_code = code
    response._content = b'{"error":"refused\\u001b[31m\\n"}'
    error = requests.HTTPError(response=response)
    assert sync._short_error(error).startswith(expected)
    assert "\x1b" not in sync._short_error(error)
    assert sync._permanent_failure(error) == permanent


def test_watch_is_bounded_in_tests(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path, status=None)
    sleeps = []
    def finish(seconds):
        sleeps.append(seconds)
        assert len(sleeps) == 1
        writer.finish("finished")
    monkeypatch.setattr(sync.time, "sleep", finish)
    try:
        assert command(remote, writer, writer.run_id, "--watch", ".01") == 0
        assert sleeps == [.01]
    finally:
        writer.close()


def test_recovered_cursor_with_different_prefix_rewrites(remote, tmp_path):
    writer = writer_at(tmp_path, status=None)
    try:
        push(remote, writer)
        (writer.run_dir / "sync.json").unlink()
        path = writer.run_dir / "scalars/0.bin"
        path.write_bytes(struct.pack("<If", 0, 88.))
        result = push(remote, writer)
        assert result.rewritten == 1
        identical(remote, writer)
    finally:
        writer.close()


def test_missing_object_restored_commits_again(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path, rich=True)
    sha = next(iter(run_references(writer.root, writer.run_id)["media"]))
    path = object_path(writer.root, sha)
    content = path.read_bytes()
    path.unlink()
    assert push(remote, writer).status == "partial"
    path.write_bytes(content)
    original = remote[1].commit
    commits = []
    def counted(*args):
        commits.append(args)
        return original(*args)
    monkeypatch.setattr(remote[1], "commit", counted)
    result = push(remote, writer)
    assert result.complete and result.files == 0 and result.objects == 1
    assert len(commits) == 1
    identical(remote, writer)


def test_whoami_once_across_multiple_runs_and_watch(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path, status=None)
    writer_at(tmp_path, run_id="other")
    original = Client.whoami
    calls = []
    def counted(client):
        calls.append(client)
        return original(client)
    monkeypatch.setattr(Client, "whoami", counted)
    def finish(seconds):
        assert len(calls) == 1
        writer.finish("finished")
    monkeypatch.setattr(sync.time, "sleep", finish)
    try:
        assert command(remote, writer, "--all", "--watch", ".01") == 0
        assert len(calls) == 1
    finally:
        writer.close()


def test_ownership_with_all_does_not_skip_idle_foreign_run(remote, tmp_path, capsys):
    writer = writer_at(tmp_path)
    push(remote, writer)
    key = foreign_key(remote[0], "second")
    assert command(remote, writer, "--all", key=key) == 1
    assert "belongs to user admin" in capsys.readouterr().out


def test_event_line_beyond_limit_fails_before_append(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path)
    path = writer.run_dir / "events.jsonl"
    path.write_bytes(json.dumps({"kind": "other", "text": "x" * 256}).encode() + b"\n")
    monkeypatch.setattr(sync, "APPEND_CHUNK", 64)
    monkeypatch.setattr(sync, "_APPEND_LIMIT", 128)
    with pytest.raises(ValueError, match="event line larger than 64 MiB"):
        push(remote, writer)
    assert not (remote[0].root / "runs" / writer.run_id / "events.jsonl").exists()


def test_no_complete_event_lines_creates_empty_file(remote, tmp_path):
    writer = writer_at(tmp_path)
    (writer.run_dir / "events.jsonl").write_bytes(b'{"torn":')
    assert push(remote, writer).complete
    assert (remote[0].root / "runs" / writer.run_id / "events.jsonl").read_bytes() == b""


@pytest.mark.parametrize("args", [[], ["--reset"], ["--reset", "--all"], ["run-1", "--watch", "0"],
                                  ["run-1", "--watch", "nan"], ["run-1", "--watch", "inf"]])
def test_argument_checks(remote, tmp_path, args):
    writer = writer_at(tmp_path)
    assert command(remote, writer, *args) == 2


def test_append_mismatch_rewrites_once(remote, tmp_path):
    writer = writer_at(tmp_path, status=None)
    try:
        push(remote, writer)
        path = remote[0].root / "runs" / writer.run_id / "scalars/0.bin"
        data = path.read_bytes()
        path.write_bytes(b"\x00" * 8 + data[8:])
        saved = json.loads((writer.run_dir / "sync.json").read_text())
        host = remote[1].base_url
        saved["hosts"][host]["files"]["scalars/0.bin"]["size"] = 0
        saved["hosts"][host]["files"]["scalars/0.bin"]["tail"] = hashlib.sha256().hexdigest()
        (writer.run_dir / "sync.json").write_text(json.dumps(saved))
        writer.write_scalars([(100, {"loss": .1})])
        result = push(remote, writer)
        assert not result.failed
        assert result.rewritten == 1
        identical(remote, writer)
    finally:
        writer.close()


def test_crash_tail_beyond_committed_is_dropped_silently(remote, tmp_path):
    writer = writer_at(tmp_path, status=None)
    try:
        push(remote, writer)
        path = remote[0].root / "runs" / writer.run_id / "scalars/0.bin"
        with path.open("ab") as handle:
            handle.write(struct.pack("<If", 100, 99.))
        writer.write_scalars([(100, {"loss": .1})])
        result = push(remote, writer)
        assert not result.failed
        assert result.rewritten == 0
        identical(remote, writer)
    finally:
        writer.close()


def test_second_commit_size_conflict_fails_without_loop(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path)
    calls = []
    def conflict(*args):
        calls.append(args)
        raise Conflict({"sizes": {"scalars/0.bin": 0}})
    monkeypatch.setattr(remote[1], "commit", conflict)
    with pytest.raises(Conflict):
        push(remote, writer)
    assert len(calls) == 2
    assert not state(remote, writer)["complete"]


def test_failed_commit_is_retried_without_resending_files(remote, tmp_path, monkeypatch, capsys):
    writer = writer_at(tmp_path)
    original = Client.commit
    calls = []

    def fail_once(client, *args):
        calls.append(args)
        if len(calls) == 1:
            raise requests.ConnectionError("commit interrupted")
        return original(client, *args)

    monkeypatch.setattr(Client, "commit", fail_once)
    assert command(remote, writer, writer.run_id) == 1
    assert not state(remote, writer)["complete"]
    capsys.readouterr()
    assert command(remote, writer, writer.run_id) == 0
    assert "synced  files=0 objects=0 bytes=0" in capsys.readouterr().out
    assert len(calls) == 2
    saved = state(remote, writer)
    info = identical(remote, writer)
    assert saved["complete"]
    assert info["status"] == "finished"
    assert info["fingerprint"] == saved["source_hash"] == run_fingerprint(writer.root, writer.run_id)


def test_failed_ownership_recovery_does_not_save_wrong_owner(remote, tmp_path, capsys):
    writer = writer_at(tmp_path)
    assert command(remote, writer, writer.run_id) == 0
    assert command(remote, writer, "--reset", writer.run_id) == 0
    path = writer.run_dir / "sync.json"
    before = path.read_bytes()
    key = foreign_key(remote[0], "second")
    assert command(remote, writer, writer.run_id, key=key) == 1
    assert "belongs to user admin" in capsys.readouterr().out
    assert path.read_bytes() == before
    assert command(remote, writer, writer.run_id) == 0
    assert state(remote, writer)["owner"] == "box"
    identical(remote, writer)


def test_rollback_after_accepted_chunk_across_tail_window(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path, status=None, count=9000)
    path = writer.run_dir / "scalars/0.bin"
    original = remote[1].append
    monkeypatch.setattr(sync, "APPEND_CHUNK", 32768)
    changed = []

    def rollback(run_id, name, offset, data, **kwargs):
        accepted = original(run_id, name, offset, data, **kwargs)
        if name == "scalars/0.bin" and accepted == path.stat().st_size and not changed:
            assert offset == 65536
            # Inside the final window, but before the final chunk. The cursor
            # must describe the old bytes accepted by the server in prior chunks.
            with path.open("r+b") as handle:
                handle.seek(8192)
                handle.write(struct.pack("<If", 1024, 99.))
            changed.append(True)
        return accepted

    monkeypatch.setattr(remote[1], "append", rollback)
    try:
        push(remote, writer)
        assert changed
        remote_bytes = (remote[0].root / "runs" / writer.run_id / "scalars/0.bin").read_bytes()
        cursor = state(remote, writer)["files"]["scalars/0.bin"]
        assert cursor["tail"] == hashlib.sha256(remote_bytes[-65536:]).hexdigest()
        assert remote_bytes != path.read_bytes()
        assert push(remote, writer).rewritten == 1
        identical(remote, writer)
    finally:
        writer.close()


@pytest.mark.parametrize("operation", ["append", "put_file", "commit"])
def test_deleted_server_run_recovers_once(remote, tmp_path, monkeypatch, operation):
    writer = writer_at(tmp_path, status=None, rich=True)
    directory = remote[0].root / "runs" / writer.run_id
    original = remote[1].run_state
    recoveries = []

    def recovered(run_id):
        info = original(run_id)
        recoveries.append(info)
        return info

    try:
        push(remote, writer)
        shutil.rmtree(directory)
        if operation == "append":
            writer.write_scalars([(100, {"loss": .1})])
        elif operation == "put_file":
            # A changed index is PUT after the unchanged append files.
            path = writer.run_dir / "media/index.json"
            path.write_bytes(path.read_bytes() + b"\n")
        # With no changed files, commit is the first request for the run.
        monkeypatch.setattr(remote[1], "run_state", recovered)
        result = push(remote, writer)
        assert not result.failed and not result.complete
        assert recoveries == [None]
        assert identical(remote, writer)["status"] == "running"
    finally:
        writer.close()


def test_second_unknown_run_is_transient_and_bounded(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path, status=None)
    original = remote[1].append
    calls = []

    def lose_run(*args, **kwargs):
        calls.append(args)
        shutil.rmtree(remote[0].root / "runs" / writer.run_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(remote[1], "append", lose_run)
    try:
        with pytest.raises(sync.RunMissingError) as caught:
            push(remote, writer)
        assert len(calls) == 2
        assert not sync._permanent_failure(caught.value)
        assert sync._short_error(caught.value) == "HTTP 404: unknown run"
        assert not state(remote, writer)["complete"]
        monkeypatch.setattr(remote[1], "append", original)
        push(remote, writer)
        identical(remote, writer)
    finally:
        writer.close()


@pytest.mark.parametrize("problem", ["rewrite", "event"])
def test_watch_stops_on_deterministic_failures(remote, tmp_path, monkeypatch, capsys, problem):
    writer = writer_at(tmp_path)
    if problem == "rewrite":
        push(remote, writer)
        (writer.run_dir / "scalars/0.bin").write_bytes(struct.pack("<If", 0, 42.))
        expected = "server copy is finished; cannot rewrite scalars/0.bin"
    else:
        (writer.run_dir / "events.jsonl").write_bytes(json.dumps({"kind": "other", "text": "x" * 256}).encode() + b"\n")
        monkeypatch.setattr(sync, "APPEND_CHUNK", 64)
        monkeypatch.setattr(sync, "_APPEND_LIMIT", 128)
        expected = "event line larger than 64 MiB"

    def forbidden_sleep(seconds):
        pytest.fail("watch retried a deterministic failure")

    monkeypatch.setattr(sync.time, "sleep", forbidden_sleep)
    assert command(remote, writer, writer.run_id, "--watch", ".01") == 1
    assert expected in capsys.readouterr().out


def test_event_snapshot_reads_size_and_bytes_from_same_handle(remote, tmp_path, monkeypatch):
    writer = writer_at(tmp_path, rich=True)
    reader = RunReader(writer.root, writer.run_id)
    path = writer.run_dir / "events.jsonl"
    expected = path.read_bytes()
    original = sync._event_size

    def replace_after_sizing(handle, size):
        capped = original(handle, size)
        path.unlink()
        path.write_bytes(b'{"kind":"other"}\n')
        return capped

    monkeypatch.setattr(sync, "_event_size", replace_after_sizing)
    files, hashes = sync._snapshot(reader, "finished", sync.SyncResult(writer.run_id))
    assert files["events.jsonl"]["data"] == expected
    refs = references_from_bytes((writer.run_dir / "media/index.json").read_bytes(), expected, writer.run_id)
    assert set(hashes) == set().union(*refs.values())


def test_permanent_failures_use_types_not_message_text():
    message = "run id run-1 belongs to key box on this server"
    assert sync._permanent_failure(sync.OwnershipError(message))
    assert sync._permanent_failure(sync.PermanentSyncError("deterministic failure"))
    assert not sync._permanent_failure(ValueError(message))

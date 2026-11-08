"""Package 17: run index, append crash safety, notes, reconcile, and 2.x import."""
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import logging
import os
import shutil

import pytest

from agenticml import cli
from agenticml.client import Client
from agenticml.server.app import make_server, prepare_data_dir
from agenticml.server.db import DatabaseError, for_server_dir
from agenticml.server.db.repos import NoteRepo, RunRepo, SettingsRepo
from agenticml.server.db.sqlite import SqliteDatabase
from agenticml.server import runs as runs_mod
from agenticml.server.runs import reconcile, reindex
from agenticml.store import layout, notes as note_store
from server_db_helpers import insert_run
from test_server_ingest import append, commit, foreign_key, put_meta, request, run_path, running
from test_server_web import authed
from test_sync import identical, push, writer_at
from test_server_users import write_run


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        _, httpd.test_key = httpd.keys.create("one")
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        yield httpd


def test_append_crash_hook_then_resync_is_byte_identical(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    with running(tmp_path / "data") as server:
        _, key = server.keys.create("box")
        url = f"http://127.0.0.1:{server.server_address[1]}"
        writer = writer_at(tmp_path, status=None, count=2)
        try:
            with Client(url, key, timeout=5, max_retries=0) as client:
                pair = (server, client)
                assert not push(pair, writer).failed
                writer.write_scalars([(2, {"loss": 0.5, "acc": 0.5, "extra": 2.0})])

                def crash(run_id, path):
                    raise RuntimeError("injected crash")

                server._after_append_write = crash
                time_path = "scalars/_time.bin"
                committed = server.run_index.committed_size(writer.run_id, time_path)
                chunk = (writer.run_dir / time_path).read_bytes()[committed:]
                status, _, _ = request(
                    server,
                    f"/v2/runs/{writer.run_id}/append/{time_path}?offset={committed}",
                    "POST", chunk, key)
                assert status == 500
                server._after_append_write = lambda *a: None
                assert not push(pair, writer).failed
                identical(pair, writer)
        finally:
            writer.close()


def test_crash_tail_state_append_truncate_and_commit(server):
    put_meta(server)
    first = b"12345678"
    assert append(server, first)[1] == {"size": 8}
    path = run_path(server, "scalars/0.bin")
    path.write_bytes(first + b"TAILTAIL")
    status, payload, _ = request(server, "/v2/runs/run-1/state")
    assert status == 200
    assert payload["files"]["scalars/0.bin"]["size"] == 8
    second = b"abcdefgh"
    assert append(server, second, offset=8)[1] == {"size": 16}
    assert path.read_bytes() == first + second
    path.write_bytes(path.read_bytes() + b"XXXX")
    size = run_path(server, "meta.json").stat().st_size
    status, obj, _ = commit(server, sizes={"meta.json": size, "scalars/0.bin": 20})
    assert status == 409 and obj == {
        "error": "size mismatch",
        "sizes": {"meta.json": size, "scalars/0.bin": 16},
    }
    assert append(server, b"ABCDEFGH", query="&truncate=1")[1] == {"size": 8}
    assert path.read_bytes() == b"ABCDEFGH"


def test_overlap_compares_only_up_to_committed_size(server):
    put_meta(server)
    body = b"12345678abcdefgh"
    assert append(server, body)[1] == {"size": 16}
    path = run_path(server, "scalars/0.bin")
    path.write_bytes(body + b"CRASHTAIL")
    overlap = b"abcdefghABCDEFGH"
    assert append(server, overlap, offset=8)[1] == {"size": 24}
    assert path.read_bytes() == body[:8] + overlap


def test_dashboard_lists_10000_runs_without_opening_files(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as server:
        _, server.admin_key = server.keys.create("admin", "admin")
        with server.run_index.db.transaction(write=True) as tx:
            for index in range(10000):
                insert_run(server.run_index, f"r{index:05d}", owner_user="admin",
                           created_at=f"2026-01-01T00:00:{index % 60:02d}Z",
                           list_row={"run_id": f"r{index:05d}", "project": "p", "status": "finished"},
                           status="finished", tx=tx)

        def boom(*args, **kwargs):
            raise AssertionError(f"opened a run file: {args[:1]}")

        monkeypatch.setattr(layout, "open_nofollow", boom)
        monkeypatch.setattr(layout, "read_file_nofollow", boom)
        status, _, body, _ = authed(server, "/api/runs")
        assert status == 200
        rows = json.loads(body)
        assert len(rows) == 10000
        assert {row["run_id"] for row in rows} == {f"r{index:05d}" for index in range(10000)}
        assert all(row["owner"] == "admin" for row in rows)
        assert all(row["status"] == "finished" for row in rows)


def test_notes_conflict_too_large_cleared_launch_and_missing(server):
    put_meta(server, status="running")
    launch = "Launch idea"
    meta = json.dumps({"run_id": "run-1", "status": "running", "notes": launch}).encode()
    assert request(server, "/v2/runs/run-1/files/meta.json", "PUT", meta)[0] == 200
    status, _, body, cookie = authed(server, "/api/runs/run-1/notes")
    assert status == 200
    note = json.loads(body)
    assert note["source"] == "launch" and note["text"] == launch and note["revision"] is None
    saved = json.dumps({"text": "hello", "revision": None}).encode()
    status, _, body, _ = authed(server, "/api/runs/run-1/notes", "PUT", saved, cookie=cookie)
    assert status == 200
    stored = json.loads(body)
    assert stored["source"] == "file" and stored["text"] == "hello"
    assert server.note_index.read("run-1")["text"] == "hello"
    assert not run_path(server, "notes.md").exists()
    conflict = json.dumps({"text": "nope", "revision": None}).encode()
    status, _, body, _ = authed(server, "/api/runs/run-1/notes", "PUT", conflict, cookie=cookie)
    assert status == 409
    huge = json.dumps({"text": "x" * (note_store.MAX_NOTE_BYTES + 1),
                       "revision": stored["revision"]}).encode()
    status, _, body, _ = authed(server, "/api/runs/run-1/notes", "PUT", huge, cookie=cookie)
    assert status == 413
    cleared = json.dumps({"text": "", "revision": stored["revision"]}).encode()
    status, _, body, _ = authed(server, "/api/runs/run-1/notes", "PUT", cleared, cookie=cookie)
    assert status == 200
    empty = json.loads(body)
    assert empty["text"] == "" and empty["source"] == "file"
    status, _, body, _ = authed(server, "/api/runs/run-1/notes", cookie=cookie)
    assert json.loads(body)["text"] == ""
    status, hdrs, body, _ = authed(server, "/api/runs/missing/notes", cookie=cookie)
    assert status == 404


def test_reconcile_adopts_and_drops(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    with running(data) as server:
        directory = server.root / "runs" / "adopt-me"
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(json.dumps({
            "run_id": "adopt-me", "project": "demo", "status": "finished",
        }))
        (directory / "events.jsonl").write_bytes(b"{}\n")
        counts = reconcile(server.root, server.run_index)
        assert counts["adopted"] == 1
        row = server.run_index.get("adopt-me")
        assert row["owner_user"] is None and row["status"] is None
        listed = json.loads(row["list_row"])
        assert listed["project"] == "demo"
        assert "events.jsonl" in server.run_index.files("adopt-me")
        import shutil
        shutil.rmtree(directory)
        counts = reconcile(server.root, server.run_index)
        assert counts["removed"] == 1
        assert server.run_index.get("adopt-me") is None


def test_reindex_cli_rebuilds_list_row(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    with running(data) as server:
        _, server.test_key = server.keys.create("one")
        put_meta(server)
        meta = json.dumps({"run_id": "run-1", "status": "running", "name": "after"}).encode()
        run_path(server, "meta.json").write_bytes(meta)
    assert cli.main(["server", "reindex", "--data", str(data)]) == 0
    out = capsys.readouterr().out
    assert "rebuilt 1" in out
    db = for_server_dir(data / "server", create=False)
    row = RunRepo(db).get("run-1")
    assert json.loads(row["list_row"])["name"] == "after"


def test_import_runs_owners_notes_and_second_start(tmp_path, caplog):
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_run(data / "store", "synced", "alice", extra={
        "owner_key": "laptop", "status": "finished", "fingerprint": "a" * 64,
        "last_sync_at": "2026-01-01T00:00:00Z", "last_sync_key": "laptop",
        "sizes": {"meta.json": 42},
    })
    write_run(data / "store", "pending", "bob")
    broken = data / "store" / "runs" / "broken"
    broken.mkdir(parents=True)
    (broken / "meta.json").write_text(json.dumps({"run_id": "broken", "status": "running"}))
    (broken / "remote.json").write_text("{")
    linked = data / "store" / "runs" / "linked"
    linked.mkdir(parents=True)
    (linked / "meta.json").write_text(json.dumps({"run_id": "linked", "status": "running"}))
    outside = tmp_path / "outside-remote.json"
    outside.write_text(json.dumps({"owner": "secret"}))
    (linked / "remote.json").symlink_to(outside)
    note_dir = data / "store" / "runs" / "synced"
    (note_dir / "notes.md").write_text("hello from 2.x\n")
    db = for_server_dir(data / "server")
    index = RunRepo(db)
    notes = NoteRepo(db)
    synced = index.as_remote("synced")
    assert synced["owner"] == "alice" and synced["owner_key"] == "laptop"
    assert synced["status"] == "finished" and synced["fingerprint"] == "a" * 64
    assert synced["sizes"] == {"meta.json": 42}
    assert notes.read("synced")["text"] == "hello from 2.x\n"
    assert index.as_remote("pending") == {"owner": "bob"}
    assert index.get("broken")["owner_user"] is None
    assert index.get("linked")["owner_user"] is None
    assert not (note_dir / "remote.json").exists()
    assert (note_dir / "remote.json.2x").exists()
    assert not (note_dir / "notes.md").exists()
    assert (note_dir / "notes.md.2x").exists()
    stamp = SettingsRepo(db).get("imported_runs_2x")
    assert stamp
    (note_dir / "remote.json").write_text(json.dumps({"owner": "eve"}))
    db.close()
    db = for_server_dir(data / "server")
    assert SettingsRepo(db).get("imported_runs_2x") == stamp
    assert RunRepo(db).as_remote("synced")["owner"] == "alice"
    assert "invalid or symlinked remote.json" in caplog.text


def test_adopted_running_meta_is_stale_on_dashboard(server):
    insert_run(server.run_index, "stale-run",
               list_row={"run_id": "stale-run", "status": "running", "project": "p"},
               created_at="2026-01-01T00:00:00Z")
    status, _, body, _ = authed(server, "/api/runs")
    rows = {row["run_id"]: row for row in json.loads(body)}
    assert rows["stale-run"]["status"] == "stale"
    assert rows["stale-run"]["owner"] is None


def test_append_ftruncate_failure_leaves_committed_size(server, monkeypatch):
    put_meta(server)
    first = b"12345678"
    assert append(server, first)[1] == {"size": 8}
    path = run_path(server, "scalars/0.bin")
    path.write_bytes(first + b"TAILTAIL")

    def boom(fd, size):
        raise OSError("injected ftruncate failure")

    monkeypatch.setattr(os, "ftruncate", boom)
    status, _, _ = append(server, b"abcdefgh", offset=8)
    assert status == 500
    assert server.run_index.committed_size("run-1", "scalars/0.bin") == 8
    assert path.stat().st_size == 16


def test_failed_append_truncates_to_write_from_not_committed(server, monkeypatch):
    put_meta(server)
    body = b"12345678" * 3
    assert append(server, body)[1] == {"size": 24}
    path = run_path(server, "scalars/0.bin")
    path.write_bytes(body[:8])
    real = os.fsync
    calls = {"n": 0}

    def boom(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("injected fsync failure")
        return real(fd)

    monkeypatch.setattr(os, "fsync", boom)
    status, _, _ = append(server, b"abcdefgh", offset=8)
    assert status == 500
    assert path.stat().st_size == 8
    assert server.run_index.committed_size("run-1", "scalars/0.bin") == 24


def test_state_remote_sizes_are_from_last_commit(server):
    put_meta(server)
    size = run_path(server, "meta.json").stat().st_size
    assert commit(server, sizes={"meta.json": size})[0] == 200
    status, payload, _ = request(server, "/v2/runs/run-1/state")
    assert status == 200
    assert payload["remote"]["sizes"] == {"meta.json": size}
    assert "scalars/0.bin" not in payload["remote"]["sizes"]
    assert append(server, b"12345678")[0] == 200
    status, payload, _ = request(server, "/v2/runs/run-1/state")
    assert payload["files"]["scalars/0.bin"]["size"] == 8
    assert payload["remote"]["sizes"] == {"meta.json": size}


def test_reconcile_missing_runs_dir_does_not_delete_rows(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    with running(data) as server:
        insert_run(server.run_index, "keep-me", owner_user="alice")
        runs = server.root / "runs"
        if runs.exists():
            shutil.rmtree(runs)
        with caplog.at_level(logging.WARNING):
            counts = reconcile(server.root, server.run_index)
        assert counts["removed"] == 0
        assert server.run_index.get("keep-me") is not None
        assert "skipping reconcile deletion" in caplog.text


def test_reconcile_unreadable_runs_dir_does_not_delete_rows(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    with running(data) as server:
        insert_run(server.run_index, "keep-me", owner_user="alice")
        original = os.scandir

        def boom(path):
            raise OSError("permission denied")

        monkeypatch.setattr(os, "scandir", boom)
        try:
            with caplog.at_level(logging.WARNING):
                counts = reconcile(server.root, server.run_index)
        finally:
            monkeypatch.setattr(os, "scandir", original)
        assert counts["removed"] == 0
        assert server.run_index.get("keep-me") is not None
        assert "skipping reconcile deletion" in caplog.text


def test_reconcile_adoption_keeps_owner_inserted_after_ids_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    with running(data) as server:
        directory = server.root / "runs" / "race-run"
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(json.dumps({
            "run_id": "race-run", "project": "demo", "status": "finished",
        }))
        insert_run(server.run_index, "race-run", owner_user="alice", owner_key="laptop",
                   status="running", list_row={"run_id": "race-run", "project": "demo"})
        real_ids = server.run_index.ids

        def stale(tx=None):
            return real_ids(tx) - {"race-run"}

        monkeypatch.setattr(server.run_index, "ids", stale)
        reconcile(server.root, server.run_index)
        row = server.run_index.get("race-run")
        assert row["owner_user"] == "alice" and row["owner_key"] == "laptop"
        assert row["status"] == "running"


def test_meta_claim_before_file_write(server):
    def crash(run_id):
        raise RuntimeError("injected claim crash")

    server._after_meta_claim = crash
    assert put_meta(server)[0] == 500
    row = server.run_index.get("run-1")
    assert row is not None and row["owner_user"] == "admin"
    assert not run_path(server, "meta.json").exists()
    other = foreign_key(server)
    status, payload, _ = put_meta(server, key=other)
    assert status == 409 and payload["error"] == "run owned by another user"
    server._after_meta_claim = lambda *a: None
    assert put_meta(server)[0] == 200
    assert run_path(server, "meta.json").exists()


def test_recreate_meta_refreshes_owner_key(server):
    assert put_meta(server)[0] == 200
    run_path(server, "meta.json").unlink()
    _, second = server.keys.create("two")
    assert put_meta(server, key=second)[0] == 200
    assert server.run_index.as_remote("run-1")["owner_key"] == "two"


def test_reindex_cli_refuses_while_server_holds_lock(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    with running(data):
        assert cli.main(["server", "reindex", "--data", str(data)]) == 2
    assert "stop the server first" in capsys.readouterr().err


def test_reindex_cli_lock_eacces_prints_reason(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    prepare_data_dir(data)
    for_server_dir(data / "server")

    def denied(fd, op):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(fcntl, "flock", denied)
    assert cli.main(["server", "reindex", "--data", str(data)]) == 2
    err = capsys.readouterr().err
    assert "stop the server first" not in err
    assert "Permission denied" in err


def test_reconcile_does_not_delete_row_created_after_directory_scan(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    with running(data) as server:
        original = runs_mod._valid_run_dirs

        def scan_then_create(root):
            found = original(root)
            directory = server.root / "runs" / "late-run"
            directory.mkdir(parents=True)
            (directory / "meta.json").write_text(json.dumps({
                "run_id": "late-run", "project": "demo", "status": "finished",
            }))
            insert_run(server.run_index, "late-run", owner_user="alice",
                       list_row={"run_id": "late-run", "project": "demo"})
            return found

        monkeypatch.setattr(runs_mod, "_valid_run_dirs", scan_then_create)
        reconcile(server.root, server.run_index)
        row = server.run_index.get("late-run")
        assert row is not None and row["owner_user"] == "alice"


def test_truncate_append_crash_then_resync_is_byte_identical(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    with running(tmp_path / "data") as server:
        _, key = server.keys.create("box")
        url = f"http://127.0.0.1:{server.server_address[1]}"
        writer = writer_at(tmp_path, status=None, count=2)
        try:
            with Client(url, key, timeout=5, max_retries=0) as client:
                pair = (server, client)
                assert not push(pair, writer).failed
                writer.write_scalars([(2, {"loss": 0.5, "acc": 0.5, "extra": 2.0})])

                def crash(run_id, path):
                    raise RuntimeError("injected truncate crash")

                server._after_append_write = crash
                time_path = "scalars/_time.bin"
                body = (writer.run_dir / time_path).read_bytes()
                committed = server.run_index.committed_size(writer.run_id, time_path)
                assert len(body) > committed
                status, _, _ = request(
                    server,
                    f"/v2/runs/{writer.run_id}/append/{time_path}?offset=0&truncate=1",
                    "POST", body, key)
                assert status == 500
                assert server.run_index.committed_size(writer.run_id, time_path) == committed
                server._after_append_write = lambda *a: None
                assert not push(pair, writer).failed
                identical(pair, writer)
        finally:
            writer.close()


def test_atomic_write_crash_then_resync_is_byte_identical(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    with running(tmp_path / "data") as server:
        _, key = server.keys.create("box")
        url = f"http://127.0.0.1:{server.server_address[1]}"
        writer = writer_at(tmp_path, status=None, count=2)
        try:
            with Client(url, key, timeout=5, max_retries=0) as client:
                pair = (server, client)
                assert not push(pair, writer).failed
                writer.write_scalars([(2, {"loss": 0.5, "acc": 0.5, "extra": 2.0})])
                index_body = (writer.run_dir / "scalars/index.json").read_bytes()

                def crash(run_id, path):
                    raise RuntimeError("injected atomic crash")

                server._after_atomic_write = crash
                status, _, _ = request(
                    server,
                    f"/v2/runs/{writer.run_id}/files/scalars/index.json",
                    "PUT", index_body, key)
                assert status == 500
                server._after_atomic_write = lambda *a: None
                assert not push(pair, writer).failed
                identical(pair, writer)
        finally:
            writer.close()


def test_atomic_crash_does_not_acknowledge_stale_record(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    with running(tmp_path / "data") as server:
        _, key = server.keys.create("box")
        url = f"http://127.0.0.1:{server.server_address[1]}"
        writer = writer_at(tmp_path, status=None, count=2)
        try:
            with Client(url, key, timeout=5, max_retries=0) as client:
                pair = (server, client)
                assert not push(pair, writer).failed
                body_a = (writer.run_dir / "scalars/index.json").read_bytes()
                body_b = b"{}"
                assert body_a != body_b

                def crash(run_id, path):
                    raise RuntimeError("injected atomic crash")

                server._after_atomic_write = crash
                status, _, _ = request(
                    server,
                    f"/v2/runs/{writer.run_id}/files/scalars/index.json",
                    "PUT", body_b, key)
                assert status == 500
                remote_index = server.root / "runs" / writer.run_id / "scalars/index.json"
                assert remote_index.read_bytes() == body_b
                recorded = server.run_index.files(writer.run_id)["scalars/index.json"]
                assert recorded["sha256"] == hashlib.sha256(body_a).hexdigest()
                server._after_atomic_write = lambda *a: None
                (writer.run_dir / "sync.json").unlink()
                assert not push(pair, writer).failed
                identical(pair, writer)
                assert remote_index.read_bytes() == body_a
        finally:
            writer.close()


@pytest.mark.parametrize("which", ["store", "runs"])
def test_import_refuses_symlinked_store_or_runs(tmp_path, which):
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_run(data / "store", "r1", "alice")
    outside = tmp_path / "outside"
    if which == "store":
        (data / "store").rename(outside)
        (data / "store").symlink_to(outside)
    else:
        (data / "store" / "runs").rename(outside)
        (data / "store" / "runs").symlink_to(outside)
    with pytest.raises(DatabaseError, match="symlink or not a directory"):
        for_server_dir(data / "server")


@pytest.mark.parametrize("which", ["store", "runs"])
def test_make_server_refuses_symlinked_store_or_runs_after_import(tmp_path, monkeypatch, which):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_run(data / "store", "r1", "alice")
    server = make_server(data, port=0)
    server.server_close()
    outside = tmp_path / "outside"
    if which == "store":
        (data / "store").rename(outside)
        (data / "store").symlink_to(outside)
    else:
        (data / "store" / "runs").rename(outside)
        (data / "store" / "runs").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        make_server(data, port=0)


@pytest.mark.parametrize("which", ["store", "runs"])
def test_reconcile_skips_symlinked_store_or_runs(tmp_path, monkeypatch, caplog, which):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    prepare_data_dir(data)
    db = for_server_dir(data / "server")
    index = RunRepo(db)
    insert_run(index, "keep-me", owner_user="alice")
    if which == "store":
        outside = tmp_path / "outside-store"
        outside.mkdir()
        link = tmp_path / "linked-store"
        link.symlink_to(data / "store")
        target = link
    else:
        runs = data / "store" / "runs"
        if runs.exists():
            shutil.rmtree(runs)
        outside = tmp_path / "outside-runs"
        outside.mkdir()
        runs.symlink_to(outside)
        target = data / "store"
    with caplog.at_level(logging.WARNING):
        counts = reconcile(target, index)
    assert counts["removed"] == 0 and counts["adopted"] == 0
    assert index.get("keep-me") is not None
    assert "symlink or not a directory" in caplog.text


@pytest.mark.parametrize("which", ["store", "runs"])
def test_reindex_skips_symlinked_store_or_runs(tmp_path, caplog, which):
    data = tmp_path / "data"
    prepare_data_dir(data)
    db = for_server_dir(data / "server")
    index = RunRepo(db)
    insert_run(index, "keep-me", owner_user="alice",
               list_row={"run_id": "keep-me", "project": "original", "name": "before"})
    decoy_meta = json.dumps({
        "run_id": "keep-me", "project": "hacked", "status": "finished", "name": "after",
    })
    if which == "store":
        directory = data / "store" / "runs" / "keep-me"
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(decoy_meta)
        link = tmp_path / "linked-store"
        link.symlink_to(data / "store")
        target = link
    else:
        outside = tmp_path / "outside-runs"
        decoy = outside / "keep-me"
        decoy.mkdir(parents=True)
        (decoy / "meta.json").write_text(decoy_meta)
        runs = data / "store" / "runs"
        if runs.exists():
            shutil.rmtree(runs)
        runs.symlink_to(outside)
        target = data / "store"
    with caplog.at_level(logging.WARNING):
        counts = reindex(target, index)
    assert counts["rebuilt"] == 0 and counts["removed"] == 0
    assert json.loads(index.get("keep-me")["list_row"])["name"] == "before"
    assert "symlink or not a directory" in caplog.text


def test_import_reads_files_before_write_transaction(tmp_path, monkeypatch):
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_run(data / "store", "synced", "alice", extra={
        "owner_key": "laptop", "status": "finished", "fingerprint": "a" * 64,
        "last_sync_at": "2026-01-01T00:00:00Z", "last_sync_key": "laptop",
        "sizes": {"meta.json": 42},
    })
    scalars = data / "store" / "runs" / "synced" / "scalars"
    scalars.mkdir()
    (scalars / "_time.bin").write_bytes(b"\x01\x00\x00\x00\x00\x00\x00\x00")
    writing = {"on": False}

    def wrap(mod, name):
        original = getattr(mod, name)

        def wrapped(*args, **kwargs):
            if writing["on"]:
                raise AssertionError(f"{name} during write transaction")
            return original(*args, **kwargs)

        monkeypatch.setattr(mod, name, wrapped)

    wrap(runs_mod, "_read_meta")
    wrap(runs_mod, "_on_disk_files")
    wrap(runs_mod, "_last_step")
    wrap(runs_mod, "read_note_file")
    wrap(runs_mod, "remote_from_file")
    original_tx = SqliteDatabase.transaction

    @contextmanager
    def wrapped_tx(self, write=False):
        with original_tx(self, write=write) as tx:
            writing["on"] = write
            try:
                yield tx
            finally:
                writing["on"] = False

    monkeypatch.setattr(SqliteDatabase, "transaction", wrapped_tx)
    db = for_server_dir(data / "server")
    assert RunRepo(db).get("synced") is not None
    assert RunRepo(db).get("synced")["last_step"] == 1


def test_detail_status_matches_index_when_commit_never_happens(server):
    assert put_meta(server, status="finished")[0] == 200
    status, _, body, cookie = authed(server, "/api/runs")
    listed = {row["run_id"]: row for row in json.loads(body)}
    dstatus, _, dbody, _ = authed(server, "/api/runs/run-1", cookie=cookie)
    detail = json.loads(dbody)
    assert dstatus == 200
    assert listed["run-1"]["status"] == detail["status"] == "finished"
    assert server.run_index.get("run-1")["status"] is None
    assert put_meta(server, status="running")[0] == 200
    status, _, body, _ = authed(server, "/api/runs", cookie=cookie)
    listed = {row["run_id"]: row for row in json.loads(body)}
    dstatus, _, dbody, _ = authed(server, "/api/runs/run-1", cookie=cookie)
    detail = json.loads(dbody)
    assert listed["run-1"]["status"] == detail["status"] == "stale"

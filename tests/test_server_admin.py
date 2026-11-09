"""Operator reports against clean, live, and deliberately damaged stores."""
from server_db_helpers import default_store
from server_db_helpers import default_workspace_id
import json
import os
from pathlib import Path
import shlex
import shutil
import time

import pytest

from agenticml import cli, lineage
from agenticml.server import admin, app
from agenticml.server.keys import KeyStore
from agenticml.store import layout, objects
from test_sync import remote, push, writer_at


def write_json(path, value):
    path.write_text(json.dumps(value))


def make_run(data, rid, status="finished", owner="box", stamp=None):
    directory = default_store(data) / "runs" / rid
    (directory / "media").mkdir(parents=True)
    (directory / "scalars").mkdir()
    write_json(directory / "meta.json", {"run_id": rid, "project": "demo", "status": status or "running"})
    write_json(directory / "media" / "index.json", {})
    (directory / "events.jsonl").write_bytes(b"\n")
    (directory / "scalars" / "0.bin").write_bytes(b"\0" * 16)
    sizes = {name: (directory / name).stat().st_size for name in (
        "meta.json", "events.jsonl", "media/index.json", "scalars/0.bin")}
    remote = {"owner": "admin", "owner_key": owner}
    if status:
        remote.update(status=status, last_sync_at=stamp or layout.format_timestamp(), sizes=sizes)
    from server_db_helpers import index_if_open
    if index_if_open(default_store(data), directory, remote=remote) is None:
        write_json(directory / "remote.json", remote)
    return directory


@pytest.fixture
def store(tmp_path):
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).create("box")
    make_run(data, "done")
    make_run(data, "live", "running")
    sha = objects.put_bytes(default_store(data), b"weights")
    write_json(default_store(data) / "runs" / "done" / "artifacts.json", {
        "version": 1, "artifacts": [{"name": "weights", "version": 1, "sha256": sha, "size": 7}]})
    return data, sha


def command(data, action, capsys, *, as_json=True):
    args = ["server", action, "--data", str(data)] + (["--json"] if as_json else [])
    code = cli.main(args)
    output = capsys.readouterr()
    return code, json.loads(output.out) if as_json else output.out, output.err


def kinds(report, field="problems"):
    return {row["kind"] for row in report[field]}


def test_status_warns_on_unimported_2x_without_database(tmp_path):
    from test_server_users import write_v1_keys
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["old"])
    report = admin.status(data)
    assert any("2.x" in warning and "keys.json" in warning for warning in report["warnings"])
    assert (data / "server" / "keys.json").exists()
    assert not (data / "server" / "agenticml.db").exists()


def test_verify_warns_on_unindexed_run(store):
    data, _ = store
    directory = default_store(data) / "runs" / "orphan"
    (directory / "media").mkdir(parents=True)
    (directory / "scalars").mkdir()
    write_json(directory / "meta.json", {"run_id": "orphan", "project": "demo", "status": "finished"})
    write_json(directory / "media" / "index.json", {})
    (directory / "events.jsonl").write_bytes(b"\n")
    (directory / "scalars" / "0.bin").write_bytes(b"\0" * 16)
    report = admin.verify(data)
    assert report["ok"] is True
    assert {"kind": "unindexed_run", "path": str((default_store(data) / "runs/orphan").relative_to(data)),
            "detail": "unindexed run; run agenticml server reindex"} in report["warnings"]


def test_clean_reports_and_key_metadata(store, capsys):
    data, sha = store
    code, report, err = command(data, "verify", capsys)
    assert code == 0 and not err
    assert report == {"ok": True, "checked": {"objects": 1, "runs": 2}, "problems": [], "warnings": []}
    code, report, err = command(data, "status", capsys)
    assert code == 0 and not err
    assert report["data"] == str(data)
    assert report["runs"]["total"] == 2
    assert report["runs"]["by_status"]["finished"] == report["runs"]["by_status"]["running"] == 1
    assert report["sizes"]["objects"] == 7 and report["sizes"]["objects_count"] == 1
    assert report["sizes"]["runs"] > 0
    assert report["sizes"]["total"] == report["sizes"]["runs"] + 7
    assert report["sizes"]["skipped"] == 0
    row = report["keys"][0]
    assert row["name"] == "box" and row["scope"] == "sync" and row["runs"] == 2
    from agenticml.server.db import for_server_dir
    from agenticml.server.db.repos import RunRepo
    stamps = [row["last_sync_at"] for row in RunRepo(for_server_dir(data / "server", create=False), default_workspace_id(for_server_dir(data / "server", create=False))).all_rows()
              if row["last_sync_at"]]
    assert row["last_sync_at"] == max(stamps)
    assert set(row) == {"name", "scope", "user", "prefix", "created_at", "last_used_at",
                        "runs", "last_sync_at"}
    record = KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).list()[0]
    for action in ("status", "verify"):
        code, text, err = command(data, action, capsys, as_json=False)
        assert code == 0 and not err and record.sha256 not in text
        if action == "verify":
            assert "verify: 0 problems, 0 warnings (objects 1, runs 2)" in text
        else:
            assert "box" in text


def test_effective_status_and_revoked_owners(store, monkeypatch):
    data, _ = store
    monkeypatch.setenv("AGENTICML_REMOTE_STALE_SECONDS", "600")
    make_run(data, "stale", "running", "gone", "2000-01-01T00:00:00Z")
    make_run(data, "crashed", "crashed", "gone", "2001-01-01T00:00:00Z")
    make_run(data, "pending", None)
    local = make_run(data, "no-remote", None)
    (local / "remote.json").unlink(missing_ok=True)
    KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).create("unused", "admin")
    report = admin.status(data)
    assert report["runs"] == {"total": 6, "by_status": {
        "running": 1, "finished": 1, "crashed": 1, "stale": 1, "unsynced": 2}}
    rows = {row["name"]: row for row in report["keys"]}
    assert rows["gone"]["revoked"] is True and rows["gone"]["runs"] == 2
    assert rows["gone"]["last_sync_at"] == "2001-01-01T00:00:00Z"
    assert rows["unused"]["runs"] == 0 and rows["unused"]["last_sync_at"] is None
    KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).revoke("box")
    assert next(row for row in admin.status(data)["keys"] if row["name"] == "box")["revoked"]


def test_hash_corruption(store, capsys):
    data, sha = store
    layout.object_path(default_store(data), sha).write_bytes(b"Weights")
    code, report, _ = command(data, "verify", capsys)
    assert code == 1 and report["ok"] is False and "object_hash" in kinds(report)


@pytest.mark.parametrize("bad_name", [True, False])
def test_wrong_object_name_or_prefix(store, bad_name):
    data, sha = store
    path = layout.object_path(default_store(data), sha)
    dest = path.parent / "wrong" if bad_name else default_store(data) / "objects" / "ff" / sha
    dest.parent.mkdir(exist_ok=True)
    path.rename(dest)
    assert "object_name" in kinds(admin.verify(data))


@pytest.mark.parametrize("loop", [False, True])
def test_object_symlinks_and_temp_files_are_not_hashed(store, tmp_path, monkeypatch, loop):
    data, sha = store
    path = layout.object_path(default_store(data), sha)
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    path.symlink_to(path if loop else outside)
    (path.parent / f".{sha}.123.456.12345678.tmp").write_bytes(b"partial")
    (default_store(data) / "objects" / ".tmp.123.456.12345678.tmp").write_bytes(b"partial")
    original = layout.open_nofollow

    def guard(target, *args, **kwargs):
        assert Path(target) not in (outside, path)
        return original(target, *args, **kwargs)

    monkeypatch.setattr(layout, "open_nofollow", guard)
    report = admin.verify(data)
    assert kinds(report) == {"object_type", "object_missing"}
    assert report["checked"]["objects"] == 0
    assert admin.status(data)["sizes"]["objects"] < len(b"secret") + len(b"partial") * 2


@pytest.mark.parametrize("status", ["finished", "crashed"])
def test_finished_file_sizes(store, status):
    data, _ = store
    directory = make_run(data, "short", status)
    (directory / "scalars" / "0.bin").write_bytes(b"\0" * 8)
    report = admin.verify(data)
    assert {"kind": "size_mismatch", "path": str((default_store(data) / "runs/short/scalars/0.bin").relative_to(data)),
            "detail": "expected 16, found 8"} in report["problems"]
    (directory / "scalars" / "0.bin").unlink()
    assert any(row["detail"] == "missing" for row in admin.verify(data)["problems"])


def test_live_size_growth_is_a_warning(store, capsys):
    data, _ = store
    path = default_store(data) / "runs" / "live" / "events.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"kind":"metric"}\n')
    code, report, _ = command(data, "verify", capsys)
    assert code == 0 and report["ok"] is True and not report["problems"]
    assert kinds(report, "warnings") == {"uncommitted_tail"}
    code, text, _ = command(data, "verify", capsys, as_json=False)
    assert "warning: uncommitted_tail" in text and "0 problems, 1 warnings" in text
    path.write_bytes(b"")
    assert "size_mismatch" in kinds(admin.verify(data))


def test_missing_byref_object(store):
    data, sha = store
    layout.object_path(default_store(data), sha).unlink()
    report = admin.verify(data)
    assert kinds(report) == {"object_missing"}
    assert report["problems"][0]["detail"] == "referenced by run done"


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_object_prefix_created_after_walk(store, monkeypatch, tmp_path, kind):
    data, _ = store
    used = {p.name for p in (default_store(data) / "objects").iterdir()}
    prefix = next(f"{i:02x}" for i in range(256) if f"{i:02x}" not in used)
    sha = prefix + "ab" * 31
    write_json(default_store(data) / "runs" / "done" / "artifacts.json", {
        "version": 1, "artifacts": [{"name": "late", "version": 1, "sha256": sha, "size": 4}]})
    original = admin._walk

    def delayed(path, on_error, **kwargs):
        yield from original(path, on_error, **kwargs)
        if path == default_store(data) / "objects":
            directory = path / prefix
            if kind == "directory":
                directory.mkdir()
                (directory / sha).write_bytes(b"late")
            else:
                target = tmp_path / "late-prefix"
                target.mkdir()
                (target / sha).write_bytes(b"late")
                directory.symlink_to(target)

    monkeypatch.setattr(admin, "_walk", delayed)
    report = admin.verify(data)
    if kind == "directory":
        assert report["ok"] and "object_missing" not in kinds(report)
        assert admin._referenced_object(default_store(data) / "objects", sha, set())
    else:
        assert "object_missing" in kinds(report)
        assert not admin._referenced_object(default_store(data) / "objects", sha, set())


@pytest.mark.parametrize("body", [b"{", b"[]", b'{"sizes": []}', b'{"sizes":{"../outside":1}}',
                                  b'{"sizes":{"meta.json":true}}', b'{"status":"invalid"}'])
def test_invalid_remote(store, body):
    data, _ = store
    (default_store(data) / "runs" / "done" / "remote.json").write_bytes(body)
    assert kinds(admin.verify(data)) == set()


@pytest.mark.parametrize("name,body", [("events.jsonl", b"{"), ("meta.json", b"[]"),
                                        ("artifacts.json", b"{}"), ("media/index.json", b"[]")])
def test_unreadable_run_references(store, name, body):
    data, _ = store
    (default_store(data) / "runs" / "done" / name).write_bytes(body)
    assert "run_unreadable" in kinds(admin.verify(data))


def test_lineage_ref_and_lock_age(store, capsys, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("requires git")
    data, _ = store
    root = default_store(data)
    source = data / "source"
    source.mkdir()
    (source / "train.py").write_text("x = 1\n")
    first = lineage.snapshot(root, "demo", source, "done")
    assert first["error"] is None
    meta_path = root / "runs" / "done" / "meta.json"
    meta = json.loads(meta_path.read_bytes())
    meta["lineage"] = first
    write_json(meta_path, meta)
    from agenticml.server.db import for_server_dir
    from agenticml.server.db.repos import RunRepo
    import hashlib
    digest = hashlib.sha256(meta_path.read_bytes()).hexdigest()
    RunRepo(for_server_dir(data / "server", create=False), default_workspace_id(for_server_dir(data / "server", create=False))).record_file(
        "done", "meta.json", meta_path.stat().st_size, sha256=digest)
    monkeypatch.setenv("GIT_DIR", str(data / "hostile"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(data / "hostile"))
    assert admin.verify(data)["ok"]
    repo = lineage._repo_path(root, first["project_key"])
    lock = repo / "refs" / "runs" / "done.lock"
    lock.write_bytes(b"")
    assert kinds(admin.verify(data), "warnings") == {"lineage_lock"}
    os.utime(lock, (time.time() - 601, time.time() - 601))
    code, report, _ = command(data, "verify", capsys)
    assert code == 1 and kinds(report) == {"lineage_lock"}
    assert f"rm -- {lock}" in report["problems"][0]["detail"]
    assert lock.exists()
    lock.unlink()
    (source / "train.py").write_text("x = 2\n")
    second = lineage.snapshot(root, "demo", source, "other")
    assert second["error"] is None
    lineage._git(["update-ref", "refs/runs/done", second["commit"]], repo=repo)
    assert kinds(admin.verify(data)) == {"lineage_ref"}
    lineage._git(["update-ref", "-d", "refs/runs/done"], repo=repo)
    assert admin.verify(data)["ok"]
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert admin.verify(data)["ok"]


def test_lock_reporting_without_git(store, monkeypatch):
    data, _ = store
    lock = lineage._repo_path(default_store(data), lineage.project_key("demo")) / "refs" / "runs" / "gone.lock"
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert kinds(admin.verify(data), "warnings") == {"lineage_lock"}


def test_read_only_and_default_data(store, monkeypatch, capsys):
    data, _ = store
    monkeypatch.setenv("AGENTICML_SERVER_DATA", str(data))

    def forbidden(*args, **kwargs):
        pytest.fail("attempt to prepare or lock data")

    monkeypatch.setattr(app, "prepare_data_dir", forbidden)
    db = KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).db  # Keep the pool open during the snapshot.
    before = {p.relative_to(data): (p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
              for p in data.rglob("*")}
    for action in ("status", "verify"):
        assert cli.main(["server", action, "--json"]) == 0
        json.loads(capsys.readouterr().out)
    after = {p.relative_to(data): (p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
             for p in data.rglob("*")}
    assert before == after


@pytest.mark.parametrize("action", ["status", "verify"])
def test_missing_or_unreadable_data(tmp_path, action, capsys, monkeypatch):
    data = tmp_path / "missing"
    assert cli.main(["server", action, "--data", str(data)]) == 2
    assert capsys.readouterr().err == f"agenticml server: no server data at {data}\n"
    assert not data.exists()
    app.prepare_data_dir(data)
    original = os.scandir

    def denied(path):
        if Path(path) == data:
            raise PermissionError("unreadable data")
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)
    assert cli.main(["server", action, "--data", str(data)]) == 2
    assert "agenticml server: unreadable data" in capsys.readouterr().err


def test_status_counts_skipped_stats_and_ignores_symlink_sizes(store, monkeypatch, tmp_path):
    data, _ = store
    path = default_store(data) / "runs" / "done" / "scalars" / "0.bin"
    outside = tmp_path / "large"
    outside.write_bytes(b"x" * 10000)
    (default_store(data) / "runs" / "link").symlink_to(outside)
    original = Path.lstat

    def denied(self):
        if self == path:
            raise PermissionError("cannot stat")
        return original(self)

    monkeypatch.setattr(Path, "lstat", denied)
    report = admin.status(data)
    assert report["sizes"]["skipped"] == 1
    assert report["sizes"]["total"] < 10000


def test_reports_while_real_server_is_running(remote, tmp_path, capsys):
    server, _ = remote
    for rid in ("first", "second"):
        writer = writer_at(tmp_path, run_id=rid, rich=True)
        assert push(remote, writer).complete
    code, report, _ = command(server.data_dir, "verify", capsys)
    assert code == 0 and report["ok"] and report["checked"]["runs"] == 2
    code, report, _ = command(server.data_dir, "status", capsys)
    assert code == 0 and report["runs"]["by_status"]["finished"] == 2
    assert report["keys"][0]["runs"] == 2 and report["keys"][0]["last_used_at"]
    assert report["sizes"]["objects_count"] > 0


def test_progress_only_in_text_on_tty(store, monkeypatch, capsys):
    data, _ = store
    # Repeat one real object in the walk to exercise the reporting threshold.
    original = admin._walk

    def many(path, on_error, **kwargs):
        if path == default_store(data) / "objects":
            entries = list(original(path, on_error, **kwargs))
            obj = next(row for row in entries if layout.valid_sha(row[0].name))
            yield from (row for row in entries if row != obj)
            yield from [obj] * 1000
        else:
            yield from original(path, on_error, **kwargs)

    monkeypatch.setattr(admin, "_walk", many)
    monkeypatch.setattr(admin.sys.stderr, "isatty", lambda: True)
    assert command(data, "verify", capsys)[2] == ""
    assert "checked 1000 objects" in command(data, "verify", capsys, as_json=False)[2]
    monkeypatch.setattr(admin.sys.stderr, "isatty", lambda: False)
    assert command(data, "verify", capsys, as_json=False)[2] == ""


def test_environment_owner_is_not_revoked(store, capsys, monkeypatch):
    data, _ = store
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    make_run(data, "environment", owner="env")
    code, report, err = command(data, "status", capsys)
    assert code == 0 and not err
    row = next(row for row in report["keys"] if row["name"] == "env")
    assert row["env"] is True and not row.get("revoked") and row["runs"] == 1
    assert "env  env  " in admin.format_status(report)


@pytest.mark.parametrize('damage', ['corrupt','invalid_records','symlink','broken_symlink'])
@pytest.mark.sqlite_only
def test_status_warns_if_keys_cannot_be_loaded(store, capsys, tmp_path, damage):
    from agenticml.server.db import DatabaseError
    data, _ = store
    db = KeyStore(data / 'server', workspace_id=default_workspace_id(data / 'server')).db
    db.close()
    path = data / 'server' / 'agenticml.db'
    path.unlink()
    if damage.endswith('symlink'):
        target = tmp_path / 'outside.db'
        if damage == 'symlink': target.write_bytes(b'private')
        path.symlink_to(target)
    else:
        path.write_bytes(b'private corrupt contents')
    code, report, err = command(data, 'status', capsys)
    assert code == 0 and not err
    assert report['warnings'] == ['could not load server database; key and user status is unknown']
    assert not any(row.get('revoked') for row in report['keys'])
    assert 'private' not in admin.format_status(report)


@pytest.mark.parametrize("action", ["status", "verify"])
def test_report_interrupt_exits_130(store, capsys, monkeypatch, action):
    data, _ = store

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(admin, action, interrupted)
    assert cli.main(["server", action, "--data", str(data)]) == 130
    output = capsys.readouterr()
    assert output.out == "" and output.err == "interrupted\n"


@pytest.mark.parametrize("action", ["status", "verify", "keys", "serve"])
def test_empty_server_error_message(store, capsys, monkeypatch, action):
    data, _ = store

    def failed(*args, **kwargs):
        raise ValueError()

    if action == "keys":
        monkeypatch.setattr(KeyStore, "list", failed)
        args = ["server", "keys", "list", "--data", str(data)]
    elif action == "serve":
        import agenticml.server
        monkeypatch.setattr(agenticml.server, "serve", failed)
        args = ["server", action, "--data", str(data)]
    else:
        monkeypatch.setattr(admin, action, failed)
        args = ["server", action, "--data", str(data)]
    assert cli.main(args) == (1 if action in ("keys", "serve") else 2)
    output = capsys.readouterr()
    assert output.out == "" and output.err == "agenticml server: ValueError\n"


def test_lock_scan_only_visits_valid_repos_run_refs(store, monkeypatch, tmp_path):
    data, _ = store
    directory = default_store(data) / "lineage"
    repo = lineage._repo_path(default_store(data), lineage.project_key("demo"))
    runs = repo / "refs" / "runs"
    runs.mkdir(parents=True)
    (runs / "good.lock").write_bytes(b"")
    (runs / "directory.lock").mkdir()
    (runs / "directory.lock" / "hidden.lock").write_bytes(b"")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.lock").write_bytes(b"")
    (runs / "symlink.lock").symlink_to(outside / "hidden.lock")
    (repo / "objects").mkdir()
    (repo / "objects" / "loose").write_bytes(b"git object")
    invalid = directory / "invalid.git" / "refs" / "runs"
    invalid.mkdir(parents=True)
    (invalid / "hidden.lock").write_bytes(b"")
    (directory / f"{lineage.project_key('link')}.git").symlink_to(repo)
    linked_refs = lineage._repo_path(default_store(data), lineage.project_key("refs-link"))
    linked_refs.mkdir()
    (linked_refs / "refs").symlink_to(repo / "refs")
    linked_runs = lineage._repo_path(default_store(data), lineage.project_key("runs-link")) / "refs"
    linked_runs.mkdir(parents=True)
    (linked_runs / "runs").symlink_to(runs)
    original = os.scandir
    scanned = []

    def guarded(path):
        path = Path(path)
        if path == directory or directory in path.parents:
            assert path in (directory, runs)
            scanned.append(path)
        return original(path)

    monkeypatch.setattr(os, "scandir", guarded)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    report = admin.verify(data)
    assert report["ok"]
    assert {row["path"] for row in report["warnings"]} == {
        str((runs / name).relative_to(data))
        for name in ("good.lock", "directory.lock", "symlink.lock")}
    assert scanned == [directory, runs]


def test_non_regular_lineage_locks(store, tmp_path):
    data, _ = store
    runs = lineage._repo_path(default_store(data), lineage.project_key("demo")) / "refs" / "runs"
    runs.mkdir(parents=True)
    directory = runs / "dir.lock"
    directory.mkdir()
    (directory / "hidden.lock").write_bytes(b"")
    outside = tmp_path / "target.lock"
    outside.write_bytes(b"secret")
    link = runs / "link.lock"
    link.symlink_to(outside)
    report = admin.verify(data)
    assert report["ok"]
    rows = {Path(row["path"]).name: row for row in report["warnings"]}
    assert set(rows) == {"dir.lock", "link.lock"}
    assert rows["dir.lock"]["kind"] == rows["link.lock"]["kind"] == "lineage_lock"
    assert "not a regular file" in rows["dir.lock"]["detail"]
    assert f"rm -r -- {shlex.quote(str(directory))}" in rows["dir.lock"]["detail"]
    assert "not a regular file" in rows["link.lock"]["detail"]
    assert f"rm -- {shlex.quote(str(link))}" in rows["link.lock"]["detail"]
    assert "rm -r --" not in rows["link.lock"]["detail"]
    assert outside.read_bytes() == b"secret"
    os.utime(directory, (time.time() - 601, time.time() - 601))
    os.utime(link, (time.time() - 601, time.time() - 601), follow_symlinks=False)
    report = admin.verify(data)
    assert not report["ok"] and kinds(report) == {"lineage_lock"} and not report["warnings"]


def test_object_scan_prunes_bad_directories_without_resolving_files(store, monkeypatch):
    data, sha = store
    objects_dir = default_store(data) / "objects"
    invalid = objects_dir / "not-hex"
    deep = objects_dir / sha[:2] / "nested"
    for path in (invalid, deep):
        path.mkdir()
        (path / "hidden").mkdir()
        (path / "hidden" / sha).write_bytes(b"do not hash")
    original_scan = os.scandir
    original_safe = admin._safe_path

    def scan(path):
        assert Path(path) not in (invalid, deep)
        return original_scan(path)

    def safe(root, path):
        assert objects_dir not in path.parents
        return original_safe(root, path)

    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(admin, "_safe_path", safe)
    report = admin.verify(data)
    assert report["checked"]["objects"] == 1
    assert {(row["kind"], row["path"]) for row in report["problems"]} == {
        ("object_name", str(path.relative_to(data))) for path in (invalid, deep)}


@pytest.mark.parametrize("component", ["objects", "prefix"])
def test_object_parent_symlinks_are_not_followed(store, tmp_path, component):
    data, sha = store
    objects_dir = default_store(data) / "objects"
    path = objects_dir if component == "objects" else objects_dir / sha[:2]
    outside = tmp_path / "outside"
    path.rename(outside)
    path.symlink_to(outside)
    report = admin.verify(data)
    assert report["checked"]["objects"] == 0
    assert kinds(report) == {"object_type", "object_missing"}


def test_text_safely_escapes_control_characters_and_invalid_utf8(store, capsys):
    data, _ = store
    name = os.fsdecode(b"unsafe-\x1b-\xff")
    data = data.rename(data.parent / name)
    make_run(data, "unsafe")
    (default_store(data) / "objects" / name).write_bytes(b"bad name")
    for action in ("status", "verify"):
        code, text, err = command(data, action, capsys, as_json=False)
        assert code == (1 if action == "verify" else 0) and not err
        text.encode("ascii", errors="strict")
        assert "\x1b" not in text and "\\x1b" in text and "\\udcff" in text
        _, report, _ = command(data, action, capsys)
        if action == "status":
            assert report["data"] == str(data)
        else:
            assert any(row["path"].endswith(name) for row in report["problems"])

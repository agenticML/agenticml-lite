"""By-reference artifact uploads over protocol v2."""
import hashlib
import json
import os
import shutil

import pytest
import requests

from agenticml import sync
from agenticml.store import RunReader, RunWriter
from agenticml.store.layout import object_path
from agenticml.store.reader import parse_artifacts_json, run_references
from test_server_ingest import put_meta, request, server
from test_server_web import authed
from test_sync import command, push, remote, state


def byref_run(tmp_path, *, run_id="run-1", data=b"weights", name="model"):
    source = tmp_path / "model.bin"
    source.write_bytes(data)
    writer = RunWriter.create(tmp_path / "local", run_id=run_id, meta={"project": "demo", "name": "train"})
    writer.add_artifact(source, name, "model", {}, copy=False)
    writer.finish("finished")
    return writer, source


def test_round_trip_download(remote, tmp_path):
    writer, source = byref_run(tmp_path)
    data = source.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    assert push(remote, writer, artifacts=True).complete
    listed = RunReader(remote[0].root, writer.run_id).artifacts()[0]
    assert listed["sha256"] == sha and listed["byref"] is True
    assert object_path(remote[0].root, sha).read_bytes() == data
    remote[0].admin_key = remote[0].keys.create("admin", "admin")[1]
    status, _, body, cookie = authed(remote[0], f"/api/runs/{writer.run_id}")
    assert status == 200
    art = json.loads(body)["artifacts"][0]
    assert art["sha256"] == sha and art["byref"] is True
    status, _, body, _ = authed(remote[0], f"/api/objects/{sha}?type=application/octet-stream", cookie=cookie)
    assert status == 200 and body == data


def test_changed_source_warns_once(remote, tmp_path):
    writer, source = byref_run(tmp_path)
    source.write_bytes(b"changed-and-longer")
    result = push(remote, writer, artifacts=True)
    assert result.complete and result.warnings == ["artifact model:v1: source changed or missing"]
    assert {"kind": "byref", "id": "model:v1", "reason": "source changed or missing"} in state(remote, writer)["skipped"]
    assert not (remote[0].root / "runs" / writer.run_id / "artifacts.json").exists()
    second = push(remote, writer, artifacts=True)
    assert second.status == "up to date" and second.warnings == []


def test_deleted_source_warns_once(remote, tmp_path):
    writer, source = byref_run(tmp_path)
    source.unlink()
    result = push(remote, writer, artifacts=True)
    assert result.complete and result.warnings == ["artifact model:v1: source changed or missing"]
    second = push(remote, writer, artifacts=True)
    assert second.status == "up to date" and second.warnings == []


def test_mtime_change_skips(remote, tmp_path):
    writer, source = byref_run(tmp_path)
    os.utime(source, (0, 0))
    result = push(remote, writer, artifacts=True)
    assert result.complete and result.warnings == ["artifact model:v1: source changed or missing"]


def test_without_flag_does_not_upload(remote, tmp_path):
    writer, _ = byref_run(tmp_path)
    assert push(remote, writer).complete
    assert not (remote[0].root / "runs" / writer.run_id / "artifacts.json").exists()
    assert RunReader(remote[0].root, writer.run_id).artifacts()[0]["sha256"] is None


def test_server_without_byref_warns_once(remote, tmp_path, monkeypatch, capsys):
    writer, _ = byref_run(tmp_path)
    monkeypatch.setattr(sync, "_identity", lambda client: {
        "key": "box", "scope": "sync", "protocol": 2, "features": ["lineage"]})
    assert command(remote, writer, writer.run_id, "--artifacts") == 0
    assert capsys.readouterr().err.count(
        "server does not support --artifacts; skipping by-reference artifacts") == 1
    assert not (remote[0].root / "runs" / writer.run_id / "artifacts.json").exists()
    assert push(remote, writer, artifacts=True).complete
    assert not (remote[0].root / "runs" / writer.run_id / "artifacts.json").exists()


def test_backfill_after_complete_without_flag(remote, tmp_path):
    writer, source = byref_run(tmp_path)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert push(remote, writer).complete
    identity = sync._identity(remote[1])
    assert writer.run_id not in sync._selected_ids(
        writer.root, remote[1].base_url, [], True, key=identity["key"], features=identity["features"])
    assert writer.run_id in sync._selected_ids(
        writer.root, remote[1].base_url, [], True, key=identity["key"],
        features=identity["features"], artifacts=True)
    assert push(remote, writer, artifacts=True).complete
    mapping = json.loads((remote[0].root / "runs" / writer.run_id / "artifacts.json").read_text())
    assert mapping["artifacts"][0]["sha256"] == sha
    assert push(remote, writer, artifacts=True).status == "up to date"
    assert writer.run_id not in sync._selected_ids(
        writer.root, remote[1].base_url, [], True, key=identity["key"],
        features=identity["features"], artifacts=True)


def test_cached_hash_not_recomputed(remote, tmp_path, monkeypatch):
    writer, _ = byref_run(tmp_path)
    assert push(remote, writer, artifacts=True).complete
    monkeypatch.setattr(sync, "_hash_byref_source", lambda path: pytest.fail("rehashed"))
    assert push(remote, writer, artifacts=True).status == "up to date"


def test_hash_mismatch_during_upload_skips_once(remote, tmp_path, monkeypatch):
    writer, _ = byref_run(tmp_path)

    def fail(sha, handle, size):
        response = requests.Response()
        response.status_code = 422
        response._content = b'{"error":"hash mismatch"}'
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(remote[1], "put_object", fail)
    result = push(remote, writer, artifacts=True)
    assert result.complete and result.warnings == ["artifact model:v1: source changed or missing"]
    monkeypatch.setattr(remote[1], "put_object", lambda *args, **kwargs: pytest.fail("retried"))
    assert push(remote, writer, artifacts=True).status == "up to date"


def test_dry_run_counts_candidates(tmp_path, monkeypatch):
    writer, _ = byref_run(tmp_path)
    reader = RunReader(writer.root, writer.run_id)
    result = sync.SyncResult(writer.run_id)
    sync.sync_run(reader, "http://127.0.0.1", None, result, dry_run=True, artifacts=True)
    without = sync.SyncResult(writer.run_id)
    sync.sync_run(reader, "http://127.0.0.1", None, without, dry_run=True)
    assert result.objects == without.objects + 1
    assert result.files == without.files + 1
    assert not (writer.run_dir / "sync.json").exists()


def test_run_references_includes_byref_digests(remote, tmp_path):
    writer, source = byref_run(tmp_path)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert sha not in run_references(writer.root, writer.run_id)["artifacts"]
    assert push(remote, writer, artifacts=True).complete
    assert sha in run_references(remote[0].root, writer.run_id)["artifacts"]


def test_reader_merges_valid_mapping_and_ignores_invalid(tmp_path):
    writer, source = byref_run(tmp_path)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    path = writer.run_dir / "artifacts.json"
    path.write_text("{")
    art = RunReader(writer.root, writer.run_id).artifacts()[0]
    assert art["sha256"] is None and "byref" not in art
    path.write_bytes(json.dumps({"version": 1, "artifacts": [
        {"name": "model", "version": 1, "sha256": sha, "size": len(source.read_bytes())}]}).encode())
    art = RunReader(writer.root, writer.run_id).artifacts()[0]
    assert art["sha256"] == sha and art["byref"] is True and art["size"] == len(source.read_bytes())


def test_run_references_rejects_corrupt_artifacts_json(tmp_path):
    writer, _ = byref_run(tmp_path)
    (writer.run_dir / "artifacts.json").write_text("{")
    with pytest.raises(ValueError, match="invalid artifacts.json"):
        run_references(writer.root, writer.run_id)
    art = RunReader(writer.root, writer.run_id).artifacts()[0]
    assert art["sha256"] is None and "byref" not in art


def test_server_rejects_invalid_artifacts_json(server):
    put_meta(server)
    sha = "a" * 64
    good = {"name": "model", "version": 1, "sha256": sha, "size": 0}
    cases = [
        b"{",
        {"version": 2, "artifacts": []},
        {"version": True, "artifacts": []},
        {"version": 1, "artifacts": [good, dict(good)]},
        {"version": 1, "artifacts": [{**good, "sha256": "zz"}]},
        {"version": 1, "artifacts": [{**good, "version": True}]},
        {"version": 1, "artifacts": [{**good, "version": 0}]},
        {"version": 1, "artifacts": [{**good, "name": ""}]},
        {"version": 1, "artifacts": [{**good, "name": "n" * 1025}]},
        {"version": 1, "artifacts": [{**good, "extra": 1}]},
        {"version": 1, "artifacts": [{**good, "size": -1}]},
        {"version": 1, "artifacts": [{**good, "size": True}]},
        {"version": 1, "artifacts": [
            {"name": "m", "version": i, "sha256": sha, "size": 0} for i in range(10001)]},
    ]
    for payload in cases:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        status, obj, _ = request(server, "/v2/runs/run-1/files/artifacts.json", "PUT", body)
        assert (status, obj) == (422, {"error": "invalid artifacts.json"}), payload
    body = json.dumps({"version": 1, "artifacts": [good]}).encode()
    assert request(server, "/v2/runs/run-1/files/artifacts.json", "PUT", body)[0] == 200
    listed = request(server, "/v2/runs/run-1/state")[1]["files"]
    assert listed["artifacts.json"]["size"] == len(body)
    assert listed["artifacts.json"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert parse_artifacts_json(body) == [good]


def test_whoami_lists_byref(remote):
    assert "byref" in remote[1].whoami()["features"]


def test_byref_skip_does_not_skip_object():
    saved = {"skipped": []}
    ident = "a" * 64
    sync._remember_skip(saved, "byref", ident, "source changed or missing")
    assert sync._skipped_ids(saved) == set()
    sync._remember_skip(saved, "object", ident, "symlink")
    assert sync._skipped_ids(saved) == {ident}


def test_stray_local_artifacts_json_is_not_uploaded(remote, tmp_path):
    writer, _ = byref_run(tmp_path)
    stray = json.dumps({"version": 1, "artifacts": [
        {"name": "stray", "version": 1, "sha256": "a" * 64, "size": 1}]}).encode()
    (writer.run_dir / "artifacts.json").write_bytes(stray)
    assert push(remote, writer).complete
    remote_run = remote[0].root / "runs" / writer.run_id
    assert not (remote_run / "artifacts.json").exists()
    sizes = json.loads((remote_run / "remote.json").read_bytes())["sizes"]
    assert "artifacts.json" not in sizes


def test_run_missing_reputs_artifacts_json_without_rehash(remote, tmp_path, monkeypatch):
    writer, _ = byref_run(tmp_path)
    assert push(remote, writer, artifacts=True).complete
    shutil.rmtree(remote[0].root / "runs" / writer.run_id)
    puts = []
    original = remote[1].put_file

    def tracked(run_id, name, body):
        puts.append(name)
        return original(run_id, name, body)

    monkeypatch.setattr(remote[1], "put_file", tracked)
    monkeypatch.setattr(sync, "_hash_byref_source", lambda path: pytest.fail("rehashed"))
    writer._update_meta({"summary": {"changed": True}})
    result = push(remote, writer, artifacts=True)
    assert result.complete and puts.count("artifacts.json") == 1
    assert (remote[0].root / "runs" / writer.run_id / "artifacts.json").exists()
    assert "model:v1" in state(remote, writer)["byref"]


def test_object_too_large_skips_once(remote, tmp_path, monkeypatch):
    writer, _ = byref_run(tmp_path)

    def fail(sha, handle, size):
        response = requests.Response()
        response.status_code = 413
        response._content = b'{"error":"too large"}'
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(remote[1], "put_object", fail)
    result = push(remote, writer, artifacts=True)
    assert result.complete and result.warnings == ["artifact model:v1: too large"]
    assert {"kind": "byref", "id": "model:v1", "reason": "too large"} in state(remote, writer)["skipped"]
    monkeypatch.setattr(remote[1], "put_object", lambda *args, **kwargs: pytest.fail("retried"))
    assert push(remote, writer, artifacts=True).status == "up to date"


def test_put_object_connection_error_does_not_skip(remote, tmp_path, monkeypatch):
    writer, source = byref_run(tmp_path)
    original = remote[1].put_object

    def fail(sha, handle, size):
        raise requests.ConnectionError("blip")

    monkeypatch.setattr(remote[1], "put_object", fail)
    with pytest.raises(requests.ConnectionError):
        push(remote, writer, artifacts=True)
    saved = state(remote, writer)
    assert not any(entry.get("kind") == "byref" for entry in saved.get("skipped") or [])
    assert not saved.get("complete")
    monkeypatch.setattr(remote[1], "put_object", original)
    result = push(remote, writer, artifacts=True)
    assert result.complete
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert object_path(remote[0].root, sha).read_bytes() == source.read_bytes()


def test_follow_passes_artifacts_and_warns_once(remote, tmp_path, monkeypatch, capsys):
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights")
    writer = RunWriter.create(tmp_path / "local", run_id="run-1", meta={"project": "demo", "name": "train"})
    writer.add_artifact(source, "model", "model", {}, copy=False)
    monkeypatch.setattr(sync, "_identity", lambda client: {
        "key": "box", "scope": "sync", "protocol": 2, "features": ["lineage"]})
    seen = []
    real = sync.sync_run

    def wrapped(*args, **kwargs):
        seen.append(kwargs.get("artifacts"))
        return real(*args, **kwargs)

    monkeypatch.setattr(sync, "sync_run", wrapped)
    sleeps = []

    def sleep_stop(stop, seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            stop.set()
            raise sync.SyncStopped()

    monkeypatch.setattr(sync, "_sleep_stop", sleep_stop)
    try:
        assert command(remote, writer, writer.run_id, "--follow", "--artifacts", "--interval", "0.02") == 0
    finally:
        writer.close()
    assert seen and all(flag is True for flag in seen)
    assert capsys.readouterr().err.count(
        "server does not support --artifacts; skipping by-reference artifacts") == 1


def test_commit_sizes_include_artifacts_json(remote, tmp_path):
    writer, _ = byref_run(tmp_path)
    assert push(remote, writer, artifacts=True).complete
    remote_run = remote[0].root / "runs" / writer.run_id
    mapping = remote_run / "artifacts.json"
    sizes = json.loads((remote_run / "remote.json").read_bytes())["sizes"]
    assert sizes["artifacts.json"] == mapping.stat().st_size


def test_mixed_upload_and_skip_lists_only_uploaded(remote, tmp_path):
    keep = tmp_path / "keep.bin"
    drop = tmp_path / "drop.bin"
    keep.write_bytes(b"keep-me")
    drop.write_bytes(b"drop-me")
    writer = RunWriter.create(tmp_path / "local", run_id="run-1", meta={"project": "demo", "name": "train"})
    writer.add_artifact(keep, "keep", "model", {}, copy=False)
    writer.add_artifact(drop, "drop", "model", {}, copy=False)
    writer.finish("finished")
    drop.unlink()
    result = push(remote, writer, artifacts=True)
    assert result.complete and result.warnings == ["artifact drop:v1: source changed or missing"]
    mapping = json.loads((remote[0].root / "runs" / writer.run_id / "artifacts.json").read_text())
    assert [row["name"] for row in mapping["artifacts"]] == ["keep"]
    sha = hashlib.sha256(b"keep-me").hexdigest()
    assert mapping["artifacts"][0]["sha256"] == sha


def test_nul_and_relative_paths_are_not_candidates(tmp_path):
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights")
    info = source.stat()
    path = str(source.resolve())
    good = {"kind": "artifact", "copy": False, "name": "model", "version": 1,
            "path": path, "size": info.st_size, "mtime": info.st_mtime}
    events = b"\n".join([
        json.dumps({"kind": "scalar", "v": 1}).encode(),
        json.dumps({**good, "name": "nul", "path": path + "\x00hidden"}).encode(),
        json.dumps({**good, "name": "rel", "path": "relative/model.bin"}).encode(),
        json.dumps(good).encode(),
        json.dumps({**good, "path": "/other"}).encode(),
    ])
    candidates, overflow = sync._byref_candidates(events)
    assert overflow is False
    assert [(row["name"], row["path"]) for row in candidates] == [("model", path)]


def test_stat_valueerror_skips_without_failing_the_run(remote, tmp_path, monkeypatch):
    writer, source = byref_run(tmp_path)
    real_stat = os.stat
    target = os.path.abspath(str(source))

    def guarded(path, *args, **kwargs):
        if os.path.abspath(str(path)) == target:
            raise ValueError("embedded null byte")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", guarded)
    result = push(remote, writer, artifacts=True)
    assert result.complete and result.warnings == ["artifact model:v1: source changed or missing"]


def test_byref_candidates_dedupe_and_cap(tmp_path):
    source = tmp_path / "model.bin"
    source.write_bytes(b"x")
    info = source.stat()
    path = str(source.resolve())
    base = {"kind": "artifact", "copy": False, "path": path, "size": info.st_size, "mtime": info.st_mtime}
    lines = [
        json.dumps({**base, "name": "dup", "version": 1}).encode(),
        json.dumps({**base, "name": "dup", "version": 1, "path": "/other"}).encode(),
    ]
    lines.extend(json.dumps({**base, "name": f"n{i}", "version": 1}).encode() for i in range(10001))
    candidates, overflow = sync._byref_candidates(b"\n".join(lines))
    assert overflow is True
    assert len(candidates) == 10_000
    assert candidates[0]["name"] == "dup" and candidates[0]["path"] == path
    assert [row["name"] for row in candidates[1:3]] == ["n0", "n1"]
    assert candidates[-1]["name"] == "n9998"


def test_byref_overflow_warns_once(remote, tmp_path, monkeypatch):
    writer, _ = byref_run(tmp_path)
    real = sync._byref_candidates

    def capped(events_bytes):
        candidates, _ = real(events_bytes)
        return candidates, True

    monkeypatch.setattr(sync, "_byref_candidates", capped)
    result = push(remote, writer, artifacts=True)
    assert result.complete
    assert result.warnings == ["too many by-reference artifacts; extra entries skipped"]
    writer._update_meta({"summary": {"changed": True}})
    second = push(remote, writer, artifacts=True)
    assert second.complete and second.warnings == []

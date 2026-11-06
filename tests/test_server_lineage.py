"""Lineage bundles over HTTP, including hostile headers and pack contents."""
import io
import json
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from agenticml import lineage, sync
from agenticml.client import Client, Conflict
from agenticml.server import app
from agenticml.server import lineage as server_lineage
from agenticml.store import RunReader, RunWriter
from agenticml.ui.server import _lineage_changes
from test_server_ingest import running, request, raw, auth, foreign_key
from test_sync import remote, push, state
from test_server_web import authed


@pytest.fixture
def snapshots(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("requires git")
    root = tmp_path / "local"
    source = tmp_path / "source"
    source.mkdir()
    runs = []
    for rid, text in (("parent", "x = 1\n"), ("child", "x = 2\ny = 3\n")):
        (source / "train.py").write_text(text)
        writer = RunWriter.create(root, run_id=rid, meta={"project": "demo", "name": rid})
        info = lineage.snapshot(root, "demo", source, rid, runs[0].run_id if runs else None)
        assert info["error"] is None
        writer._update_meta({"lineage": info})
        writer.finish("finished")
        runs.append(writer)
    return runs


def info(writer):
    return RunReader(writer.root, writer.run_id).meta()["lineage"]


def bundle(writer, tmp_path, tips=()):
    path = tmp_path / "test.bundle"
    lineage.bundle_for_run(writer.root, writer.run_id, tips, path)
    return path.read_bytes()


def meta(client, writer, block=None):
    value = RunReader(writer.root, writer.run_id).meta()
    if block is not None:
        value["lineage"] = block
    client.put_file(writer.run_id, "meta.json", json.dumps(value).encode())


def upload(client, writer, data):
    return client.put_lineage(writer.run_id, io.BytesIO(data), len(data))


def refs(server, writer):
    repo = lineage._repo_path(server.root, info(writer)["project_key"])
    if not (repo / "HEAD").exists():
        return b""
    return lineage._git(["for-each-ref", "--format=%(refname) %(objectname)"], repo=repo).stdout


def assert_tmp_clean(server):
    deadline = time.monotonic() + 1
    while list(server.tmp_dir.iterdir()) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not list(server.tmp_dir.iterdir())


def objects(server, writer):
    repo = lineage._repo_path(server.root, info(writer)["project_key"])
    return sorted(str(path.relative_to(repo)) for path in (repo / "objects").rglob("*") if path.is_file())


def test_round_trip_incremental_diff_and_replay(remote, snapshots, tmp_path):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    incremental = bundle(child, tmp_path, [info(parent)["commit"], "f" * 40])
    assert b"-" + info(parent)["commit"].encode() + b" " in incremental.split(b"\n\n")[0]
    assert push(remote, child).complete
    assert lineage.diff(server.root, "child", "parent") == lineage.diff(child.root, "child", "parent")
    server.admin_key = server.keys.create("admin", "admin")[1]
    status, _, body, _ = authed(server, "/api/runs/child/lineage")
    assert status == 200
    assert json.loads(body)["files"] == _lineage_changes(child.root, "child", RunReader(child.root, "child").meta())["files"]
    assert upload(client, child, incremental) == {"commit": info(child)["commit"], "created": False}
    assert set(client.lineage_state("child")["tips"]) == {info(w)["commit"] for w in snapshots}
    assert not list(child.root.glob("lineage-*.bundle"))
    assert_tmp_clean(server)


@pytest.mark.parametrize("mutation", ["branch", "other", "two", "commit", "garbage", "truncated", "v3", "header_limit", "bad_prerequisite"])
def test_invalid_bundles_preserve_refs(remote, snapshots, tmp_path, mutation):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    meta(client, child)
    data = bundle(child, tmp_path)
    expected = f"{info(child)['commit']} refs/runs/child\n".encode()
    if mutation == "branch":
        data = data.replace(expected, expected.replace(b"refs/runs/child", b"refs/heads/x"), 1)
    elif mutation == "other":
        data = data.replace(expected, expected.replace(b"child", b"other"), 1)
    elif mutation == "two":
        data = data.replace(expected, expected + expected, 1)
    elif mutation == "commit":
        data = data.replace(info(child)["commit"].encode(), b"f" * 40, 1)
    elif mutation == "garbage":
        data = b"garbage"
    elif mutation == "truncated":
        data = data[:-30]
    elif mutation == "v3":
        data = data.replace(b"# v2 git bundle\n", b"# v3 git bundle\n@filter=blob:none\n", 1)
    elif mutation == "header_limit":
        data = data.replace(expected, b"-" + b"f" * 40 + b" " + b"x" * (1024 * 1024) + b"\n" + expected, 1)
    else:
        data = data.replace(expected, b"-nope prerequisite\n" + expected, 1)
    before = refs(server, parent)
    before_objects = objects(server, parent)
    with pytest.raises(requests.HTTPError) as exc:
        upload(client, child, data)
    assert exc.value.response.status_code == 422
    assert exc.value.response.json() == {"error": "invalid bundle"}
    assert refs(server, parent) == before
    assert objects(server, parent) == before_objects
    assert client.lineage_state("child")["commit"] is None
    assert_tmp_clean(server)


def test_ref_conflict_is_immutable(remote, snapshots, tmp_path):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    meta(client, child)
    repo = lineage._repo_path(server.root, info(parent)["project_key"])
    lineage._git(["update-ref", "refs/runs/child", info(parent)["commit"]], repo=repo)
    before = refs(server, parent)
    before_objects = objects(server, parent)
    with pytest.raises(Conflict) as exc:
        upload(client, child, bundle(child, tmp_path))
    assert exc.value.payload == {"error": "lineage ref differs", "commit": info(parent)["commit"]}
    assert refs(server, parent) == before
    assert objects(server, parent) == before_objects
    assert_tmp_clean(server)


def test_missing_prerequisite_and_full_retry(remote, snapshots, tmp_path, monkeypatch):
    server, client = remote
    parent, child = snapshots
    meta(client, child)
    data = bundle(child, tmp_path, [info(parent)["commit"]])
    with pytest.raises(requests.HTTPError) as exc:
        upload(client, child, data)
    assert exc.value.response.status_code == 422
    assert exc.value.response.json() == {"error": "missing prerequisite"}
    assert refs(server, child) == b""
    assert objects(server, child) == []
    assert_tmp_clean(server)
    original = client.lineage_state
    monkeypatch.setattr(client, "lineage_state", lambda rid: {"commit": None, "tips": [info(parent)["commit"]]})
    sent = []
    put = client.put_lineage
    def capture(rid, handle, size):
        sent.append(handle.read())
        handle.seek(0)
        return put(rid, handle, size)
    monkeypatch.setattr(client, "put_lineage", capture)
    assert push(remote, child).complete
    assert len(sent) == 2
    assert b"-" + info(parent)["commit"].encode() in sent[0].split(b"\n\n")[0]
    assert b"-" + info(parent)["commit"].encode() not in sent[1].split(b"\n\n")[0]
    assert original("child")["commit"] == info(child)["commit"]
    assert not list(child.root.glob("lineage-*.bundle"))


@pytest.mark.parametrize("object_type", ["bad_tree", "tree", "blob", "tag"])
def test_hostile_objects_never_publish_ref(remote, snapshots, tmp_path, object_type):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    repo = lineage._repo_path(child.root, info(child)["project_key"])
    def git(*args, **kwargs):
        return lineage._git(list(args), repo=repo, **kwargs).stdout.strip().decode()
    blob = git("hash-object", "-w", "--stdin", input=b"data")
    if object_type == "bad_tree":
        tree = git("hash-object", "-w", "--literally", "-t", "tree", "--stdin",
                   input=b"100644 .git\0" + bytes.fromhex(blob))
        target = git("commit-tree", tree, "-m", "hostile tree")
    elif object_type == "tree":
        target = info(child)["tree"]
    elif object_type == "blob":
        target = blob
    else:
        target = git("hash-object", "-w", "-t", "tag", "--stdin", input=(
            f"object {blob}\ntype blob\ntag hostile\ntagger agenticml <agenticml@localhost> 1 +0000\n\ntag\n").encode())
    git("update-ref", "refs/runs/child", target)
    block = {**info(child), "commit": target}
    meta(client, child, block)
    path = tmp_path / "hostile.bundle"
    git("bundle", "create", str(path), "refs/runs/child")
    before = refs(server, parent)
    before_objects = objects(server, parent)
    with pytest.raises(requests.HTTPError) as exc:
        upload(client, child, path.read_bytes())
    assert exc.value.response.status_code == 422
    assert refs(server, parent) == before
    assert objects(server, parent) == before_objects
    assert_tmp_clean(server)
    assert client.lineage_state("child")["commit"] is None
    git("update-ref", "refs/runs/child", info(child)["commit"])
    meta(client, child)
    assert upload(client, child, bundle(child, tmp_path))["created"]


def test_limits_ownership_and_missing_lineage(remote, snapshots, tmp_path):
    server, client = remote
    parent, _ = snapshots
    server.test_key = client.api_key
    meta(client, parent)
    path = "/v2/runs/parent/lineage"
    server.limits.lineage_body = 10
    assert raw(server, "PUT", path, auth(server) + [("Content-Length", "11")])[0] == 413
    assert raw(server, "PUT", path, auth(server))[0] == 411
    other_key = foreign_key(server)
    for method in ("GET", "PUT"):
        assert request(server, path, method, b"x" if method == "PUT" else None, key=other_key)[0] == 409
        assert request(server, "/v2/runs/unknown/lineage", method, b"x" if method == "PUT" else None)[0] == 404
        assert request(server, path, method, b"x" if method == "PUT" else None, key="wrong")[0] == 401
    for block in ({}, {"project_key": "../escape", "commit": "f" * 40},
                  {"project_key": info(parent)["project_key"], "commit": "F" * 40}):
        meta(client, parent, block)
        for method in ("GET", "PUT"):
            status, payload, _ = request(server, path, method, b"x" if method == "PUT" else None)
            assert (status, payload) == (422, {"error": "run has no lineage"})
    assert_tmp_clean(server)


def test_git_environment_and_hooks_are_ignored(remote, snapshots, tmp_path, monkeypatch):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    repo = lineage._repo_path(server.root, info(parent)["project_key"])
    sentinel = tmp_path / "hook-ran"
    hook = tmp_path / "hook"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 1\n")
    hook.chmod(0o755)
    (repo / "hooks").mkdir(exist_ok=True)
    for name in ("reference-transaction", "post-update", "pre-auto-gc"):
        shutil.copy(hook, repo / "hooks" / name)
    lineage._git(["config", "core.hooksPath", str(repo / "hooks")], repo=repo)
    lineage._git(["config", "core.fsmonitor", str(hook)], repo=repo)
    for name, value in {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                        "GIT_CONFIG_VALUE_0": str(repo / "hooks"), "GIT_DIR": str(tmp_path / "wrong"),
                        "GIT_CONFIG_GLOBAL": str(hook), "GIT_OBJECT_DIRECTORY": str(tmp_path / "wrong")}.items():
        monkeypatch.setenv(name, value)
    assert push(remote, child).complete
    assert not sentinel.exists()
    assert client.lineage_state("child")["commit"] == info(child)["commit"]


@pytest.mark.parametrize("git_available", [True, False])
def test_feature_detection(tmp_path, monkeypatch, git_available, caplog):
    real_git = shutil.which("git")
    if git_available and real_git is None:
        pytest.skip("requires git")
    monkeypatch.setattr(app.shutil, "which", lambda name: real_git if git_available else None)
    with running(tmp_path / "server") as server:
        server.test_key = server.keys.create("box")[1]
        assert request(server, "/v2/whoami")[1]["features"] == (["lineage", "byref"] if git_available else ["byref"])
        if not git_available:
            assert request(server, "/v2/runs/unknown/lineage")[0] == 404
            assert "git is not installed" in caplog.text


def test_featureless_backfill_and_idle(remote, snapshots, monkeypatch):
    server, client = remote
    parent, _ = snapshots
    server.lineage_enabled = False
    def unexpected(*args):
        pytest.fail("featureless server received lineage request")
    with monkeypatch.context() as patch:
        patch.setattr(client, "lineage_state", unexpected)
        patch.setattr(client, "put_lineage", unexpected)
        assert push(remote, parent).complete
    assert "lineage" not in state(remote, parent)
    server.lineage_enabled = True
    del client._sync_identity
    identity = sync._identity(client)
    assert sync._selected_ids(parent.root, client.base_url, [], True, key=identity["key"], features=identity["features"]) == ["parent", "child"]
    assert push(remote, parent).complete
    assert state(remote, parent)["lineage"] == info(parent)["commit"]
    assert push(remote, parent).status == "up to date"
    assert "parent" not in sync._selected_ids(parent.root, client.base_url, [], True, key=identity["key"], features=identity["features"])


def test_conflict_skip_completes_and_is_not_retried(remote, snapshots, monkeypatch):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    meta(client, child)
    repo = lineage._repo_path(server.root, info(parent)["project_key"])
    lineage._git(["update-ref", "refs/runs/child", info(parent)["commit"]], repo=repo)
    result = push(remote, child)
    assert result.complete and result.warnings == ["lineage: lineage ref differs"]
    saved = state(remote, child)
    assert {"kind": "lineage", "id": "child", "reason": "lineage ref differs"} in saved["skipped"]
    monkeypatch.setattr(client, "lineage_state", lambda rid: pytest.fail("remembered skip retried"))
    assert push(remote, child).status == "up to date"
    child._update_meta({"summary": {"changed": True}})
    assert push(remote, child).complete


@pytest.mark.parametrize("block", [{}, {"error": "snapshot failed", "project_key": None}])
def test_no_snapshot_completes_without_requests(remote, tmp_path, monkeypatch, block):
    writer = RunWriter.create(tmp_path / "local", run_id="plain", meta={"project": "demo", "lineage": block})
    writer.finish("finished")
    monkeypatch.setattr(remote[1], "lineage_state", lambda rid: pytest.fail("no snapshot"))
    assert push(remote, writer).complete


def test_dry_run_counts_lineage_without_network(remote, snapshots, monkeypatch):
    parent, _ = snapshots
    monkeypatch.setattr(remote[1], "lineage_state", lambda rid: pytest.fail("dry run request"))
    reader = RunReader(parent.root, parent.run_id)
    result = sync.SyncResult(parent.run_id)
    sync.sync_run(reader, remote[1].base_url, None, result, dry_run=True)
    without = sync.SyncResult(parent.run_id)
    monkeypatch.setattr(sync, "_local_lineage", lambda *args: None)
    sync.sync_run(reader, remote[1].base_url, None, without, dry_run=True)
    assert result.files == without.files + 1
    assert not (parent.run_dir / "sync.json").exists()


def test_child_before_parent_resends_full_bundle(remote, snapshots):
    parent, child = snapshots
    assert push(remote, child).complete
    assert remote[1].lineage_state("child")["commit"] == info(child)["commit"]
    assert push(remote, parent).complete
    assert remote[1].lineage_state("parent")["commit"] == info(parent)["commit"]


def test_valid_v3_bundle(remote, snapshots, tmp_path):
    parent, _ = snapshots
    meta(remote[1], parent)
    data = bundle(parent, tmp_path).replace(b"# v2 git bundle\n", b"# v3 git bundle\n@object-format=sha1\n", 1)
    assert upload(remote[1], parent, data)["created"]


@pytest.mark.parametrize("status", [422, 503])
def test_lineage_errors_and_temporary_cleanup(remote, snapshots, monkeypatch, status):
    parent, _ = snapshots
    _, client = remote
    def fail(*args):
        response = requests.Response()
        response.status_code = status
        response._content = b'{"error":"invalid bundle"}'
        raise requests.HTTPError(response=response)
    with monkeypatch.context() as patch:
        patch.setattr(client, "put_lineage", fail)
        if status == 422:
            result = push(remote, parent)
            assert result.complete
            assert result.warnings == ["lineage: invalid bundle"]
        else:
            with pytest.raises(requests.HTTPError):
                push(remote, parent)
            assert not state(remote, parent)["complete"]
        assert not list(parent.root.glob("lineage-*.bundle"))
    assert push(remote, parent).complete


def test_local_ref_mismatch_is_skipped(remote, snapshots, monkeypatch):
    parent, child = snapshots
    repo = lineage._repo_path(parent.root, info(parent)["project_key"])
    lineage._git(["update-ref", "refs/runs/parent", info(child)["commit"]], repo=repo)
    monkeypatch.setattr(remote[1], "lineage_state", lambda rid: pytest.fail("mismatched local ref"))
    assert push(remote, parent).complete


def test_lineage_skip_does_not_skip_same_named_object():
    saved = {"skipped": []}
    ident = "a" * 64
    sync._remember_skip(saved, "lineage", ident, "invalid bundle")
    assert sync._skipped_ids(saved) == set()
    sync._remember_skip(saved, "object", ident, "symlink")
    assert sync._skipped_ids(saved) == {ident}
    assert len(saved["skipped"]) == 2


def test_existing_remote_commit_recovers_without_upload(remote, snapshots, monkeypatch):
    parent, _ = snapshots
    assert push(remote, parent).complete
    (parent.run_dir / "sync.json").unlink()
    monkeypatch.setattr(remote[1], "put_lineage", lambda *args: pytest.fail("already present commit uploaded"))
    assert push(remote, parent).complete
    assert state(remote, parent)["lineage"] == info(parent)["commit"]


def test_missing_tree_bundle_preserves_objects(remote, snapshots):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    repo = lineage._repo_path(child.root, info(child)["project_key"])
    commit = lineage._git(["hash-object", "-w", "-t", "commit", "--stdin"], repo=repo, input=(
        f"tree {'f' * 40}\nauthor agenticml <agenticml@localhost> 1 +0000\n"
        "committer agenticml <agenticml@localhost> 1 +0000\n\nmissing tree\n").encode()).stdout.strip()
    pack = lineage._git(["pack-objects", "--stdout"], repo=repo, input=commit + b"\n").stdout
    meta(client, child, {**info(child), "commit": commit.decode()})
    before, before_objects = refs(server, parent), objects(server, parent)
    with pytest.raises(requests.HTTPError) as exc:
        upload(client, child, b"# v2 git bundle\n" + commit + b" refs/runs/child\n\n" + pack)
    assert exc.value.response.status_code == 422
    assert refs(server, parent) == before
    assert objects(server, parent) == before_objects
    assert client.lineage_state("child")["commit"] is None
    assert_tmp_clean(server)


def test_first_garbage_bundle_does_not_break_sibling_state(remote, snapshots):
    server, client = remote
    parent, child = snapshots
    meta(client, parent)
    meta(client, child)
    with pytest.raises(requests.HTTPError) as exc:
        upload(client, parent, b"garbage")
    assert exc.value.response.status_code == 422
    repo = lineage._repo_path(server.root, info(parent)["project_key"])
    assert not repo.exists()
    assert client.lineage_state("child") == {"commit": None, "tips": []}
    repo.mkdir(parents=True)
    assert client.lineage_state("child") == {"commit": None, "tips": []}
    assert_tmp_clean(server)


@pytest.mark.parametrize("project", ["attacker", None, 42, []])
def test_project_key_mismatch_preserves_victim_refs(remote, snapshots, tmp_path, project):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    value = RunReader(child.root, child.run_id).meta()
    value["project"] = project
    client.put_file(child.run_id, "meta.json", json.dumps(value).encode())
    before, before_objects = refs(server, parent), objects(server, parent)
    with pytest.raises(requests.HTTPError) as exc:
        upload(client, child, bundle(child, tmp_path))
    assert exc.value.response.status_code == 422
    assert refs(server, parent) == before
    assert objects(server, parent) == before_objects
    assert_tmp_clean(server)


def test_server_commit_peels_tags(remote, snapshots):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    meta(client, child)
    repo = lineage._repo_path(server.root, info(parent)["project_key"])
    commit = info(parent)["commit"]
    tag = lineage._git(["hash-object", "-w", "-t", "tag", "--stdin"], repo=repo, input=(
        f"object {commit}\ntype commit\ntag annotated\ntagger agenticml <agenticml@localhost> 1 +0000\n\ntag\n").encode()).stdout.strip().decode()
    lineage._git(["update-ref", "refs/runs/parent", tag], repo=repo)
    assert server_lineage._commit(repo, "parent") == commit
    assert client.lineage_state("parent")["commit"] == commit
    assert upload(client, parent, b"# v2 git bundle\n" + commit.encode() + b" refs/runs/parent\n\nPACK")["created"] is False


@pytest.mark.parametrize("failure", ["timeout", "oserror", "ref", "ref_timeout", "rename"])
def test_receive_failures_preserve_objects_and_retry(remote, snapshots, tmp_path, monkeypatch, failure):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    data = bundle(child, tmp_path, [info(parent)["commit"]])
    before, before_objects = refs(server, parent), objects(server, parent)
    git = server_lineage._git
    rename = type(tmp_path).rename
    timeouts = []
    def fail_git(repo, args, **kwargs):
        if args[0] in ("bundle", "index-pack", "rev-list", "cat-file", "update-ref"):
            timeouts.append(kwargs.get("timeout"))
        if failure == "ref_timeout" and args[0] == "update-ref" and "-d" not in args:
            git(repo, args, **kwargs)
            raise subprocess.TimeoutExpired("git", kwargs["timeout"])
        if args[0] == ("update-ref" if failure == "ref" else "rev-list"):
            if failure == "timeout":
                raise subprocess.TimeoutExpired("git", kwargs["timeout"])
            if failure == "oserror":
                raise OSError("disk full")
            if failure == "ref":
                raise subprocess.CalledProcessError(1, "git", stderr=b"ref failed")
        return git(repo, args, **kwargs)
    def fail_rename(source, target):
        if source.suffix == ".idx":
            raise OSError("disk full")
        return rename(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(server_lineage, "_git", fail_git)
        if failure == "rename":
            patch.setattr(type(tmp_path), "rename", fail_rename)
        with pytest.raises(requests.HTTPError) as exc:
            push(remote, child)
        assert exc.value.response.status_code == 503
        assert exc.value.response.json() == {"error": "lineage busy"}
        assert exc.value.response.headers["Retry-After"] == "5"
        saved = state(remote, child)
        assert not saved["complete"]
        assert not any(entry["kind"] == "lineage" for entry in saved.get("skipped", []))
    assert timeouts and all(timeout == 60 + len(data) / (4 * 1024 * 1024) for timeout in timeouts)
    assert refs(server, parent) == before
    assert objects(server, parent) == before_objects
    assert client.lineage_state("child")["commit"] is None
    assert_tmp_clean(server)
    assert push(remote, child).complete


@pytest.mark.parametrize("failure", ["lookup_absent", "lookup_published", "delete", "postcheck"])
def test_rollback_errors_keep_potentially_published_objects(remote, snapshots, tmp_path, monkeypatch, failure):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    meta(client, child)
    data = bundle(child, tmp_path, [info(parent)["commit"]])
    before, before_objects = refs(server, parent), objects(server, parent)
    git, commit = server_lineage._git, server_lineage._commit
    attempted, rollback_failed = False, False
    def fail_git(repo, args, **kwargs):
        nonlocal attempted, rollback_failed
        if args[0] == "update-ref":
            if "-d" in args:
                rollback_failed = True
                raise subprocess.CalledProcessError(1, "git", stderr=b"ref locked")
            attempted = True
            if failure != "lookup_absent":
                result = git(repo, args, **kwargs)
                if failure == "postcheck":
                    return result
            raise subprocess.TimeoutExpired("git", kwargs["timeout"])
        return git(repo, args, **kwargs)
    def fail_commit(repo, run_id, **kwargs):
        nonlocal rollback_failed
        if run_id == "child" and attempted and failure != "delete":
            rollback_failed = failure.startswith("lookup")
            raise OSError("cannot read ref")
        return commit(repo, run_id, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(server_lineage, "_git", fail_git)
        patch.setattr(server_lineage, "_commit", fail_commit)
        with pytest.raises(requests.HTTPError) as exc:
            upload(client, child, data)
        assert exc.value.response.status_code == 503
    assert rollback_failed
    repo = lineage._repo_path(server.root, info(child)["project_key"])
    assert set(before_objects) < set(objects(server, parent))
    assert lineage._git(["cat-file", "-t", info(child)["commit"]], repo=repo).stdout.strip() == b"commit"
    if failure == "lookup_absent":
        assert refs(server, parent) == before
        assert client.lineage_state("child")["commit"] is None
    else:
        assert client.lineage_state("child")["commit"] == info(child)["commit"]
        assert lineage.diff(server.root, "child", "parent") == lineage.diff(child.root, "child", "parent")
    lineage._git(["fsck", "--no-dangling"], repo=repo)
    assert_tmp_clean(server)
    assert upload(client, child, data)["created"] == (failure == "lookup_absent")


def test_init_repo_failure_is_retryable_without_remembered_skip(remote, snapshots, monkeypatch):
    server, _ = remote
    parent, _ = snapshots
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "git", stderr=b"init failed")
    with monkeypatch.context() as patch:
        patch.setattr(lineage, "_init_repo", fail)
        with pytest.raises(requests.HTTPError) as exc:
            push(remote, parent)
        assert exc.value.response.status_code == 503
        assert exc.value.response.json() == {"error": "lineage busy"}
        assert exc.value.response.headers["Retry-After"] == "5"
        saved = state(remote, parent)
        assert not saved["complete"]
        assert not any(entry["kind"] == "lineage" for entry in saved.get("skipped", []))
    assert refs(server, parent) == b""
    assert objects(server, parent) == []
    assert_tmp_clean(server)
    assert push(remote, parent).complete


def test_incremental_receive_with_colon_in_data_dir(snapshots, tmp_path):
    with running(tmp_path / "data:lineage") as server:
        _, key = server.keys.create("sync")
        with Client(f"http://127.0.0.1:{server.server_address[1]}", key, max_retries=0) as client:
            for writer in snapshots:
                assert push((server, client), writer).complete
            parent, child = snapshots
            assert lineage.diff(server.root, "child", "parent") == lineage.diff(child.root, "child", "parent")
            assert_tmp_clean(server)


def test_receive_concurrency_limit_after_body(remote, snapshots, tmp_path, monkeypatch):
    server, client = remote
    parent, child = snapshots
    for writer in snapshots:
        meta(client, writer)
    data = [bundle(writer, tmp_path) for writer in snapshots]
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    receive = server_lineage.receive
    def slow_receive(root, run_id, meta, path):
        entered[run_id == "child"].set()
        assert release.wait(5)
        return receive(root, run_id, meta, path)
    monkeypatch.setattr(server_lineage, "receive", slow_receive)
    def put(writer, body):
        return requests.put(client.base_url + f"/v2/runs/{writer.run_id}/lineage", data=body,
                            headers={"Authorization": "Bearer " + client.api_key}, timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(put, writer, body) for writer, body in zip(snapshots, data)]
        try:
            assert all(event.wait(3) for event in entered)
            response = put(parent, data[0])
            assert response.status_code == 503
            assert response.json() == {"error": "server busy"}
            assert response.headers["Retry-After"] == "5"
        finally:
            release.set()
        assert [future.result().status_code for future in pending] == [201, 201]
    assert server.lineage_receives.acquire(blocking=False)
    assert server.lineage_receives.acquire(blocking=False)
    server.lineage_receives.release()
    server.lineage_receives.release()
    assert_tmp_clean(server)


def test_sibling_state_does_not_wait_for_receive_flock(remote, snapshots, tmp_path, monkeypatch):
    server, client = remote
    parent, child = snapshots
    assert push(remote, parent).complete
    meta(client, child)
    data = bundle(child, tmp_path)
    entered, release = threading.Event(), threading.Event()
    git = server_lineage._git
    def slow_git(repo, args, **kwargs):
        if args[0] == "index-pack":
            entered.set()
            assert release.wait(5)
        return git(repo, args, **kwargs)
    monkeypatch.setattr(server_lineage, "_git", slow_git)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(upload, client, child, data)
        try:
            assert entered.wait(3)
            response = requests.get(client.base_url + "/v2/runs/parent/lineage",
                                    headers={"Authorization": "Bearer " + client.api_key}, timeout=1)
            assert response.status_code == 200
            assert response.json()["commit"] == info(parent)["commit"]
        finally:
            release.set()
        assert pending.result()["created"]
    assert_tmp_clean(server)


def test_git_diagnostics_are_truncated(remote, snapshots, tmp_path, monkeypatch, caplog):
    server, client = remote
    parent, _ = snapshots
    meta(client, parent)
    data = bundle(parent, tmp_path)
    git = server_lineage._git
    def fail(repo, args, **kwargs):
        if args[0] == "index-pack":
            raise subprocess.CalledProcessError(1, "git", stderr=b"x" * 500 + b"\nsecret second line")
        return git(repo, args, **kwargs)
    monkeypatch.setattr(server_lineage, "_git", fail)
    with pytest.raises(requests.HTTPError):
        upload(client, parent, data)
    assert "bundle receive: " + "x" * 240 in caplog.text
    assert "x" * 241 not in caplog.text
    assert "secret second line" not in caplog.text
    assert objects(server, parent) == []
    assert_tmp_clean(server)


def test_git_locale_is_fixed(monkeypatch):
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    monkeypatch.setenv("LANGUAGE", "fr")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/untrusted")
    env = lineage._env()
    assert env["LC_ALL"] == "C" and env["LANGUAGE"] == ""
    assert "GIT_OBJECT_DIRECTORY" not in env
    assert lineage._env(extra_env={"GIT_OBJECT_DIRECTORY": "/quarantine"})["GIT_OBJECT_DIRECTORY"] == "/quarantine"


@pytest.mark.parametrize("key", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT",
                                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "PATH", "LC_ALL"])
def test_git_environment_rejects_unlisted_overrides(key):
    with pytest.raises(ValueError, match="unsupported Git environment override"):
        lineage._env(repo="/repo", worktree="/worktree", index="/index",
                     extra_env={"GIT_OBJECT_DIRECTORY": "/quarantine", key: "/untrusted"})


def test_git_environment_preserves_explicit_paths():
    env = lineage._env(repo="/repo", worktree="/worktree", index="/index",
                       extra_env={"GIT_OBJECT_DIRECTORY": "/quarantine"})
    assert {key: env[key] for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")} == {
        "GIT_DIR": "/repo", "GIT_WORK_TREE": "/worktree", "GIT_INDEX_FILE": "/index",
        "GIT_OBJECT_DIRECTORY": "/quarantine"}


def test_bundle_with_exclusions_retries_without_matching_stderr(snapshots, tmp_path, monkeypatch):
    parent, child = snapshots
    git = lineage._git
    inputs = []
    def fail(args, **kwargs):
        if args[:2] == ["bundle", "create"]:
            inputs.append(kwargs["input"])
            if len(inputs) == 1:
                return subprocess.CompletedProcess(args, 1, stderr=b"translated diagnostic")
        return git(args, **kwargs)
    monkeypatch.setattr(lineage, "_git", fail)
    assert bundle(child, tmp_path, [info(parent)["commit"]])
    assert inputs == [b"^" + info(parent)["commit"].encode() + b"\n", b""]


@pytest.mark.parametrize("failure", ["value", "git", "timeout", "oserror"])
def test_local_bundle_failures_skip_only_permanent_errors(remote, snapshots, monkeypatch, failure):
    parent, _ = snapshots
    errors = {"value": ValueError("local lineage ref differs"),
              "git": subprocess.CalledProcessError(1, ["git", "bundle", "create"]),
              "timeout": subprocess.TimeoutExpired("git", 30), "oserror": OSError("disk full")}
    def fail(*args):
        raise errors[failure]
    monkeypatch.setattr(lineage, "bundle_for_run", fail)
    if failure in ("timeout", "oserror"):
        with pytest.raises(type(errors[failure])):
            push(remote, parent)
        saved = state(remote, parent)
        assert not saved["complete"]
        assert not any(entry["kind"] == "lineage" for entry in saved.get("skipped", []))
    else:
        result = push(remote, parent)
        assert result.complete and result.warnings == [f"lineage: {errors[failure]}"]
        saved = state(remote, parent)
        assert {"kind": "lineage", "id": "parent", "reason": str(errors[failure])} in saved["skipped"]
        monkeypatch.setattr(lineage, "_resolve_commit", lambda *args: pytest.fail("remembered skip resolved ref"))
        assert push(remote, parent).status == "up to date"
    assert not list(parent.root.glob("lineage-*.bundle"))


def test_idle_lineage_does_not_resolve_local_ref(remote, snapshots, monkeypatch):
    parent, _ = snapshots
    assert push(remote, parent).complete
    monkeypatch.setattr(lineage, "_resolve_commit", lambda *args: pytest.fail("idle run resolved ref"))
    assert push(remote, parent).status == "up to date"
    identity = sync._identity(remote[1])
    assert "parent" not in sync._selected_ids(parent.root, remote[1].base_url, [], True,
                                             key=identity["key"], features=identity["features"])

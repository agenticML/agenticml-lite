"""Shadow Git snapshots never mutate the source project."""
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agenticml import Run, init
from agenticml import lineage
from agenticml.cli import main
from agenticml.store import RunReader, RunWriter
from agenticml.store.gc import collect, usage

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="requires git")


@pytest.fixture(autouse=True)
def clean_git_environment(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True,
                          timeout=10, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout


@pytest.fixture
def project(tmp_path, monkeypatch):
    path = tmp_path / "project"
    path.mkdir()
    (path / "pyproject.toml").write_text("[project]\nname='test'\n")
    (path / "train.py").write_text("x = 1\n")
    monkeypatch.chdir(path)
    monkeypatch.setattr(sys, "argv", [str(path / "train.py")])
    return path


def start(root, **kwargs):
    return Run("test", "run", dir=root, capture_env=False, system_metrics=False,
               auto_track=False, flush_interval=999, **kwargs)


def info(run):
    return RunReader(run.root, run.run_id).meta()["lineage"]


def tree(root, result):
    repo = root / "lineage" / f"{result['project_key']}.git"
    return git(repo, "ls-tree", "-r", "--name-only", result["commit"]).decode().splitlines()


def test_non_git_startup_and_identical_tree(project, tmp_path):
    root = tmp_path / "store"
    with start(root) as first:
        a = info(first)
        assert a["error"] is None
        assert a["base_commit"] is a["base_branch"] is a["base_dirty"] is None
        assert "train.py" in tree(root, a)
        assert not (root / "worktrees").exists()
    with start(root) as second:
        b = info(second)
        assert b["tree"] == a["tree"]
        assert b["parent_run_id"] == first.run_id
        assert b["parent_inference"] == "repo_root"
        assert RunReader(root, second.run_id).meta()["parent_run_id"] is None
        repo = root / "lineage" / f"{a['project_key']}.git"
        assert git(repo, "rev-parse", b["commit"] + "^").decode().strip() == a["commit"]
    assert not (project / ".git").exists()


def test_git_project_is_unchanged(project, tmp_path):
    git(project, "init")
    git(project, "add", ".")
    git(project, "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "base")
    (project / "train.py").write_text("x = 2\n")
    (project / "staged.py").write_text("staged\n")
    git(project, "add", "staged.py")
    index = project / ".git" / "index"
    before = (git(project, "rev-parse", "HEAD"), index.read_bytes(), index.stat().st_mtime_ns,
              git(project, "status", "--porcelain"))
    with start(tmp_path / "store") as run:
        data = info(run)
        assert data["error"] is None
        assert data["base_commit"] == before[0].decode().strip()
        assert data["base_dirty"] is True
        assert data["base_branch"]
    after = (git(project, "rev-parse", "HEAD"), index.read_bytes(), index.stat().st_mtime_ns,
             git(project, "status", "--porcelain"))
    assert before == after


def test_ignores_caps_symlinks_and_nested_repos(project, tmp_path):
    (project / ".gitignore").write_text("ignored/\n*.secret\n")
    (project / ".agenticmlignore").write_text("private/\n")
    for directory in ("ignored", "private", ".venv", "__pycache__", "thing.egg-info", "nested"):
        folder = project / directory
        folder.mkdir()
        (folder / "skip.py").write_text("skip")
    git(project / "nested", "init")
    (project / "hidden.secret").write_text("hidden")
    (project / "large.bin").write_bytes(b"x" * 1000)
    (tmp_path / "outside").write_text("outside")
    (project / "outside-link").symlink_to(tmp_path / "outside")
    # Relative, so the link's own size (its target path) stays under the cap.
    (project / "inside-link").symlink_to("train.py")
    root = tmp_path / "store"
    data = lineage.snapshot(root, "test", project, "one", max_file_bytes=100)
    assert data["error"] is None
    assert data["skipped"]["oversized"] == ["large.bin"]
    assert set(tree(root, data)) == {"train.py", "pyproject.toml", ".gitignore", ".agenticmlignore", "inside-link"}
    assert not list((root / "lineage").glob("*/index-*"))


def test_total_cap(project, tmp_path):
    data = lineage.snapshot(tmp_path / "store", "p", project, "one", max_total_bytes=1)
    assert data["error"] is None
    assert data["files"] == data["bytes"] == 0
    assert data["skipped"]["over_total"] == ["pyproject.toml", "train.py"]


def test_explicit_parent_beats_inference(project, tmp_path):
    root = tmp_path / "store"
    with start(root) as first:
        pass
    with start(root):
        pass
    with start(root, parent_run_id=first.run_id) as child:
        assert info(child)["parent_inference"] == "explicit"
        assert info(child)["parent_commit"] == info(first)["commit"]
        assert RunReader(root, child.run_id).meta()["parent_run_id"] == first.run_id


def test_infer_parent_strict_time_and_project_fallback(project, tmp_path):
    root = tmp_path / "store"
    for rid, timestamp, repo_root in (("old", "2020-01-01T00:00:00.000000Z", str(project)),
                                     ("new", "2021-01-01T00:00:00.000000Z", "/other")):
        writer = RunWriter.create(root, run_id=rid, meta={"project": "p", "git": {"repo_root": repo_root}})
        writer.set_meta_fields(created_at=timestamp)
        writer.finish("finished")
    assert lineage.infer_parent(root, "p", project, "2022-01-01T00:00:00.000000Z") == ("old", "repo_root")
    assert lineage.infer_parent(root, "p", "/elsewhere", "2022-01-01T00:00:00.000000Z") == ("new", "project")
    assert lineage.infer_parent(root, "p", project, "2020-01-01T00:00:00.000000Z") == ("old", "repo_root")


def test_concurrent_snapshots(project, tmp_path):
    root = tmp_path / "store"
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda rid: lineage.snapshot(root, "p", project, rid), ["one", "two"]))
    assert all(result["error"] is None for result in results)
    repo = root / "lineage" / f"{results[0]['project_key']}.git"
    assert len(git(repo, "for-each-ref", "refs/runs").splitlines()) == 2


def test_missing_git_does_not_break_run(project, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with start(tmp_path / "store") as run:
        assert info(run)["error"]
        run.log({"loss": 1})
    reader = RunReader(run.root, run.run_id)
    assert reader.meta()["status"] == "finished"
    assert reader.series("loss")["points"] == [[0, 1.0]]


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_environment_opt_out(project, tmp_path, monkeypatch, value):
    monkeypatch.setenv("AGENTICML_LINEAGE", value)
    with start(tmp_path / "store") as run:
        assert "lineage" not in RunReader(run.root, run.run_id).meta()
    assert not (run.root / "lineage").exists()


def test_init_kwarg_opt_out(project, tmp_path):
    with init("p", "n", dir=tmp_path / "store", lineage=False, auto_track=False,
              capture_env=False, system_metrics=False) as run:
        assert "lineage" not in RunReader(run.root, run.run_id).meta()


def test_resume_does_not_snapshot(project, tmp_path, monkeypatch):
    root = tmp_path / "store"
    with start(root) as first:
        original = info(first)
    def unexpected(*args, **kwargs):
        pytest.fail("resume attempted a snapshot")
    monkeypatch.setattr("agenticml.run.snapshot_lineage", unexpected)
    (project / "train.py").write_text("changed\n")
    with start(root, id=first.run_id, resume="allow") as resumed:
        assert info(resumed) == original


def test_cli_checkout_diff_ls_and_gc(project, tmp_path, capsys):
    root = tmp_path / "store"
    with start(root) as first:
        pass
    (project / "train.py").write_text("x = 2\ny = 3\n")
    with start(root) as second:
        pass
    assert main(["lineage", "--dir", str(root), "checkout", second.run_id]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert (path / "train.py").read_text() == "x = 2\ny = 3\n"
    assert lineage.checkout(root, second.run_id) == path
    assert main(["lineage", "diff", second.run_id, first.run_id, "--dir", str(root)]) == 0
    assert "train.py" in capsys.readouterr().out
    assert lineage.diff(root, second.run_id) == [{"path": "train.py", "added": 2, "deleted": 1, "status": "M"}]
    assert main(["lineage", "ls", "--project", "test", "--dir", str(root)]) == 0
    output = capsys.readouterr().out
    assert f"    {second.run_id}" in output
    report = usage(root)
    assert report["lineage_total"] > 0 and report["worktrees_total"] > 0
    preview = collect(root, include_unsynced=True, dry_run=True)
    assert preview["lineage_refs_removed"] == 2 and preview["worktrees_removed"] == 1
    assert path.exists() and lineage.run_commit(root, second.run_id)
    report = collect(root, include_unsynced=True)
    assert report["runs_deleted"] == 2 and report["lineage_refs_removed"] == 2
    assert not path.exists()
    repo = root / "lineage" / f"{lineage.project_key(second.project)}.git"
    assert not git(repo, "for-each-ref", "refs/runs").strip()


@pytest.mark.parametrize("project_name", ["", "../ABC /☃", "_", "a" * 100])
def test_project_key_safe(project_name):
    key = lineage.project_key(project_name)
    assert lineage._KEY_RE.fullmatch(key)
    assert len(key) <= 57


def test_diff_initial_binary_deleted_and_unusual_paths(project, tmp_path):
    root = tmp_path / "store"
    unusual = "tab\tand\nnewline.txt"
    (project / unusual).write_text("one\n")
    (project / "binary.bin").write_bytes(b"\0binary")
    with start(root, track_source=False) as first:
        entries = {e["path"]: e for e in lineage.diff(root, first.run_id)}
        assert entries[unusual]["status"] == "A"
        assert entries["binary.bin"] == {"path": "binary.bin", "added": None, "deleted": None, "status": "A"}
    (project / unusual).unlink()
    (project / "binary.bin").write_bytes(b"\0changed")
    with start(root, track_source=False) as second:
        entries = {e["path"]: e for e in lineage.diff(root, second.run_id)}
        assert entries[unusual] == {"path": unusual, "added": 0, "deleted": 1, "status": "D"}
        assert entries["binary.bin"]["status"] == "M"


def test_custom_checkout_kept_and_modified_default_head_cleaned(project, tmp_path):
    root = tmp_path / "store"
    with start(root) as run:
        data = info(run)
    custom = tmp_path / 'custom "checkout"\nfolder'
    lineage.checkout(root, run.run_id, custom)
    (custom / "train.py").write_text("local changes\n")
    git(custom, "add", "train.py")
    git(custom, "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "custom changes")
    default = lineage.checkout(root, run.run_id)
    git(default, "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "--allow-empty", "-m", "local change")
    removed = collect(root, include_unsynced=True)
    assert removed["worktrees_removed"] == 1
    assert removed["worktrees_kept"] == [str(custom)]
    assert custom.exists() and not default.exists()
    assert (custom / "train.py").read_text() == "local changes\n"
    repo = root / "lineage" / f"{data['project_key']}.git"
    assert not git(repo, "for-each-ref", "refs/runs").strip()
    assert str(custom).encode() in git(repo, "worktree", "list", "--porcelain")


def test_snapshot_timeout_is_metadata(project, tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(lineage.subprocess, "run", timeout)
    with start(tmp_path / "store", track_source=False) as run:
        assert "timed out" in info(run)["error"]
        run.log({"loss": 1})
    assert RunReader(run.root, run.run_id).meta()["status"] == "finished"


def test_shadow_git_ignores_inherited_overrides(project, tmp_path, monkeypatch):
    root = tmp_path / "store"
    index = tmp_path / "index"
    index.write_bytes(b"user index sentinel")
    config = tmp_path / "global-config"
    config.write_text("[core]\n excludesFile = /invalid/path\n bare = true\n")
    monkeypatch.setenv("GIT_INDEX_FILE", str(index))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_DIR", str(project / ".git"))
    data = lineage.snapshot(root, "p", project, "one")
    assert data["error"] is None
    assert index.read_bytes() == b"user index sentinel"
    assert not (project / ".git").exists()


def test_store_containment_failure_is_metadata(project, tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "lineage").symlink_to(outside, target_is_directory=True)
    data = lineage.snapshot(root, "p", project, "one")
    assert "escapes store root" in data["error"]
    assert not list(outside.iterdir())


@pytest.mark.parametrize("parent_finishes_first", [True, False])
def test_overlapping_runs_resolve_parent(project, tmp_path, monkeypatch, parent_finishes_first):
    root = tmp_path / "store"
    parent_ready = threading.Event()
    release_parent = threading.Event()
    parent_published = threading.Event()
    original = lineage._git

    def paused_git(args, **kwargs):
        result = original(args, **kwargs)
        index = str(kwargs.get("index", ""))
        if args[0] == "write-tree" and "index-parent-" in index:
            parent_ready.set()
            assert release_parent.wait(10)
        elif args[0] == "write-tree" and "index-child-" in index and parent_finishes_first:
            release_parent.set()
            assert parent_published.wait(10)
        elif args[:2] == ["update-ref", "refs/runs/parent"]:
            parent_published.set()
        return result

    monkeypatch.setattr(lineage, "_git", paused_git)

    def run(rid):
        with start(root, id=rid, resume="allow", track_source=False) as current:
            return current

    with ThreadPoolExecutor(2) as pool:
        parent_future = pool.submit(run, "parent")
        try:
            assert parent_ready.wait(10)
            (project / "train.py").write_text("x = 2\ny = 3\n")
            child = pool.submit(run, "child").result(15)
        finally:
            release_parent.set()
        parent = parent_future.result(15)
    a, b = info(parent), info(child)
    assert a["error"] is b["error"] is None
    assert b["parent_run_id"] == "parent"
    repo = root / "lineage" / f"{b['project_key']}.git"
    if parent_finishes_first:
        assert b["parent_commit"] == a["commit"]
        assert git(repo, "rev-parse", b["commit"] + "^").decode().strip() == a["commit"]
    else:
        assert b["parent_commit"] is None
        assert len(git(repo, "rev-list", "--parents", "-n", "1", b["commit"]).split()) == 1
    # If the parent was unpublished, diff resolves its ref live after it finishes.
    assert lineage.diff(root, "child") == [
        {"path": "train.py", "added": 2, "deleted": 1, "status": "M"}]


def test_metadata_strips_git_overrides_on_git_project(project, tmp_path, monkeypatch):
    git(project, "init")
    git(project, "add", ".")
    git(project, "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "base")
    expected = git(project, "rev-parse", "HEAD").decode().strip()
    # A local fsmonitor hook must never execute, including for status.
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    git(project, "config", "core.fsmonitor", str(hook))
    sentinel = tmp_path / "user-index"
    sentinel.write_bytes(b"do not touch")
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(sentinel))
    with start(tmp_path / "store", track_source=False) as run:
        assert RunReader(run.root, run.run_id).meta()["git"]["git_commit"] == expected
        assert info(run)["base_commit"] == expected
        assert info(run)["error"] is None
    assert sentinel.read_bytes() == b"do not touch"
    assert not marker.exists()


def test_metadata_decodes_invalid_utf8_and_handles_unexpected_failure(tmp_path, monkeypatch):
    def output(*args, **kwargs):
        assert not kwargs.get("text")
        return subprocess.CompletedProcess(args[0], 0, b"branch-\xff\n")
    monkeypatch.setattr(lineage.subprocess, "run", output)
    assert lineage.get_git_metadata(tmp_path)["git_branch"] == "branch-\udcff"

    def broken(*args, **kwargs):
        raise RuntimeError("unexpected metadata error")
    monkeypatch.setattr(lineage.subprocess, "run", broken)
    assert lineage.get_git_metadata(tmp_path) == {"git_commit": None, "git_branch": None}


def test_run_guards_git_metadata_call(project, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
    monkeypatch.setattr("agenticml.run.get_git_metadata", broken)
    with start(tmp_path / "store", track_source=False) as run:
        assert info(run)["error"] is None
        run.log({"loss": 1})
    assert RunReader(run.root, run.run_id).meta()["status"] == "finished"


def test_count_cap_does_not_stat_excess_paths(project, tmp_path, monkeypatch):
    for i in range(50):
        (project / f"file-{i:03}.py").write_text("x\n")
    original = Path.lstat
    inspected = []

    def counted(path, *args, **kwargs):
        if path.parent == project:
            inspected.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", counted)
    data = lineage.snapshot(tmp_path / "store", "p", project, "capped", max_files=5)
    assert data["error"] is None
    assert data["files"] == 5
    assert data["skipped"]["over_count"] == 47
    assert len([name for name in inspected if name.startswith("file-")]) <= 5
    assert len(tree(tmp_path / "store", data)) == 5


@pytest.mark.parametrize("error", [OSError("unreadable"), RuntimeError("symlink loop")])
def test_per_path_errors_skip_file(project, tmp_path, monkeypatch, error):
    original = Path.lstat

    def broken(path, *args, **kwargs):
        if path == project / "train.py":
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", broken)
    data = lineage.snapshot(tmp_path / "store", "p", project, "partial")
    assert data["error"] is None
    assert data["skipped"]["path_errors"] == ["train.py"]
    assert tree(tmp_path / "store", data) == ["pyproject.toml"]


def test_symlink_loop_does_not_abort_snapshot(project, tmp_path):
    (project / "loop-a").symlink_to("loop-b")
    (project / "loop-b").symlink_to("loop-a")
    data = lineage.snapshot(tmp_path / "store", "p", project, "loop")
    assert data["error"] is None
    assert set(data["skipped"]["path_errors"]) == {"loop-a", "loop-b"}
    assert "train.py" in tree(tmp_path / "store", data)


def test_snapshot_shares_one_deadline(project, tmp_path, monkeypatch):
    elapsed = [0.0]
    original = lineage._git
    timeouts = []
    monkeypatch.setattr(lineage.time, "monotonic", lambda: elapsed[0])

    def consume(args, **kwargs):
        timeouts.append(kwargs["timeout"])
        result = original(args, **kwargs)
        elapsed[0] += 0.4
        return result

    monkeypatch.setattr(lineage, "_git", consume)
    data = lineage.snapshot(tmp_path / "store", "p", project, "timeout", timeout=1)
    assert "timed out" in data["error"]
    assert timeouts == pytest.approx([1, 0.6, 0.2])
    assert elapsed[0] < 1.5


def test_snapshot_deadline_includes_lock_wait(project, tmp_path):
    pytest.importorskip("fcntl")
    root = tmp_path / "store"
    repo = lineage.shadow_repo(root, "p")
    with lineage._locked(root, repo):
        with ThreadPoolExecutor(1) as pool:
            data = pool.submit(lineage.snapshot, root, "p", project, "blocked", timeout=0.05).result(2)
    assert "timed out" in data["error"]


def test_inference_ties_bad_roots_and_self_parent(project, tmp_path, monkeypatch):
    root = tmp_path / "store"
    loop = tmp_path / "loop"
    loop.symlink_to("loop")
    timestamp = "2020-01-01T00:00:00.000000Z"
    for rid, repo_root in (("a", str(project)), ("b", str(project)),
                           ("c", str(loop)), ("d", ["not", "a", "path"])):
        writer = RunWriter.create(root, run_id=rid, meta={"project": "p", "git": {"repo_root": repo_root}})
        writer.set_meta_fields(created_at=timestamp)
        writer.finish("finished")
    assert lineage.infer_parent(root, "p", project, timestamp, "a") == (None, None)
    assert lineage.infer_parent(root, "p", project, timestamp, "b") == ("a", "repo_root")
    assert lineage.infer_parent(root, "p", project, timestamp, "z") == ("b", "repo_root")
    metas = lineage.list_runs(root)
    assert lineage.infer_parents(metas) == {"a": (None, None), "b": ("a", "repo_root")}
    data = lineage.snapshot(root, "p", project, "z", parent_run_id="z", created_at=timestamp)
    assert data["parent_run_id"] == "b"
    assert data["parent_inference"] == "repo_root"
    with start(root, id="self", resume="allow", parent_run_id="self", track_source=False) as run:
        assert info(run)["parent_run_id"] != "self"
        assert RunReader(root, "self").meta()["parent_run_id"] is None


def test_cli_ls_reads_once_and_handles_deep_tree_and_bad_run(tmp_path, monkeypatch, capsys):
    timestamp = "2020-01-01T00:00:00.000000Z"
    metas = [{"run_id": f"r{i:04}", "project": "p", "created_at": timestamp,
              "git": {"repo_root": str(tmp_path)}} for i in reversed(range(1200))]
    metas.insert(0, {"run_id": "bad", "project": "p", "created_at": timestamp, "lineage": 42})
    calls = []

    def loaded(root):
        calls.append(root)
        assert len(calls) == 1
        return metas

    monkeypatch.setattr("agenticml.store.reader.list_runs", loaded)
    monkeypatch.setattr(lineage, "list_runs", lambda *args: pytest.fail("inference reread the store"))
    assert main(["lineage", "ls", "--dir", str(tmp_path)]) == 0
    assert len(calls) == 1
    output = capsys.readouterr().out
    assert "r0000" in output and "r1199" in output and "bad" in output


def test_remove_run_missing_project_key(project, tmp_path):
    root = tmp_path / "store"
    with start(root) as run:
        pass
    meta = RunReader(root, run.run_id).meta()
    meta["lineage"].pop("project_key")
    assert lineage.remove_run(root, meta)["refs"] == [f"refs/runs/{run.run_id}"]
    assert lineage.run_commit(root, run.run_id) is None


def test_gc_retries_orphan_ref_on_next_collect(project, tmp_path, monkeypatch, caplog):
    root = tmp_path / "store"
    with start(root) as run:
        pass
    original = lineage._git

    def fail_delete(args, **kwargs):
        if args[:2] == ["update-ref", "-d"]:
            raise OSError("temporary ref cleanup failure")
        return original(args, **kwargs)

    monkeypatch.setattr(lineage, "_git", fail_delete)
    first = collect(root, include_unsynced=True)
    assert first["runs_deleted"] == 1 and first["lineage_refs_removed"] == 0
    assert not (root / "runs" / run.run_id).exists()
    assert "temporary ref cleanup failure" in caplog.text
    repo = root / "lineage" / f"{lineage.project_key('test')}.git"
    assert git(repo, "for-each-ref", "refs/runs").strip()
    monkeypatch.setattr(lineage, "_git", original)
    second = collect(root, include_unsynced=True)
    assert second["runs_deleted"] == 0 and second["lineage_refs_removed"] == 1
    assert not git(repo, "for-each-ref", "refs/runs").strip()


def test_cli_gc_reports_custom_checkout_kept(project, tmp_path, capsys):
    root = tmp_path / "store"
    with start(root) as run:
        pass
    custom = lineage.checkout(root, run.run_id, tmp_path / "custom")
    (custom / "untracked.py").write_text("precious edits\n")
    assert main(["gc", "--dir", str(root), "--include-unsynced", "--yes"]) == 0
    assert f"Kept user-owned checkout: {custom}" in capsys.readouterr().out
    assert (custom / "untracked.py").read_text() == "precious edits\n"


def test_oversize_ignore_not_read_or_applied(project, tmp_path, monkeypatch):
    ignore = project / ".agenticmlignore"
    ignore.write_bytes(b"train.py\n" + b"#" * (1024 * 1024))
    # The ignore size is checked on the descriptor, so a capped read is also
    # safe against concurrent growth. Track read_bytes to prevent unbounded reads.
    monkeypatch.setattr(Path, "read_bytes", lambda path: pytest.fail("unbounded ignore read"))
    data = lineage.snapshot(tmp_path / "store", "p", project, "ignore")
    assert data["error"] is None
    assert "train.py" in tree(tmp_path / "store", data)
    assert not list((tmp_path / "store" / "lineage").glob("*/ignore-*"))


def test_checkout_existing_destination_requires_matching_registration(project, tmp_path):
    root = tmp_path / "store"
    with start(root) as first:
        pass
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "user-file").write_text("keep")
    with pytest.raises(ValueError, match="not a worktree"):
        lineage.checkout(root, first.run_id, destination)
    assert (destination / "user-file").read_text() == "keep"
    registered = lineage.checkout(root, first.run_id, tmp_path / "registered")
    assert lineage.checkout(root, first.run_id, registered) == registered
    git(registered, "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "--allow-empty", "-m", "local")
    with pytest.raises(ValueError, match="not a worktree"):
        lineage.checkout(root, first.run_id, registered)
    with start(root) as second:
        pass
    with pytest.raises(ValueError, match="not a worktree"):
        lineage.checkout(root, second.run_id, registered)


def test_task_resolution_precedence(project, monkeypatch, caplog):
    from agenticml.code_tracking import resolve_task

    entry = project / "asr" / "train.py"
    entry.parent.mkdir()
    entry.write_text("pass\n")
    marker = project / "agenticml.toml"
    marker.write_text('task = "marker"\n')
    monkeypatch.setenv("AGENTICML_TASK", " env ")
    assert resolve_task(" explicit ", project, entry) == ("explicit", "explicit")
    assert resolve_task("\x00bad", project, entry) == ("env", "env")
    assert "invalid explicit task" in caplog.text
    assert resolve_task(None, project, entry) == ("env", "env")
    monkeypatch.setenv("AGENTICML_TASK", "x" * 129)
    assert resolve_task(None, project, entry) == ("marker", "marker")
    assert "invalid env task" in caplog.text
    monkeypatch.delenv("AGENTICML_TASK")
    nearest = entry.parent / "agenticml.toml"
    nearest.write_text("task = 'nearest'\n")
    assert resolve_task(None, project, entry) == ("nearest", "marker")
    nearest.write_text("task = [invalid\n")
    assert resolve_task(None, project, entry) == ("marker", "marker")
    marker.unlink()
    assert resolve_task(None, project, entry) == ("asr", "entry")
    assert resolve_task(None, project, project / "train.py") == (None, None)
    assert resolve_task(None, project, None) == (None, None)
    marker.write_text('task = "cwd-marker"\n')
    assert resolve_task(None, project, None) == ("cwd-marker", "marker")
    assert resolve_task(None, project, project.parent / "outside.py") == (None, None)


@pytest.mark.parametrize("value", ["", "  ", "x" * 129, "a\nb", "a\x7fb", "a\x85b", [], 42])
def test_invalid_task_falls_through(project, monkeypatch, value):
    from agenticml.code_tracking import resolve_task

    monkeypatch.setenv("AGENTICML_TASK", "valid")
    assert resolve_task(value, project) == ("valid", "env")


@pytest.mark.parametrize("char", [
    "\x00", "\t", "\x85", "\u202e", "\ud800", "\ue000", "\u0378", "\u2028", "\u2029",
])
@pytest.mark.parametrize("template", ["{}asr", "as{}r", "asr{}"])
def test_task_rejects_unicode_controls_and_separators(char, template):
    from agenticml.code_tracking import _normalize_task

    value = template.format(char)
    assert _normalize_task(value) is None
    assert lineage.run_task({"task": value, "task_source": "explicit"}) == ("", None)
    assert lineage.run_task({"name": f"{value}/train.py"}) == ("", None)
    assert lineage.run_task({"name": f"asr/train{char}.py"}) == ("", None)


@pytest.mark.parametrize("kind", ["symlink", "large", "directory", "unreadable", "invalid", "nested"])
def test_task_marker_guards(project, monkeypatch, kind):
    from agenticml.code_tracking import resolve_task

    monkeypatch.delenv("AGENTICML_TASK", raising=False)
    entry = project / "src" / "tts" / "train.py"
    entry.parent.mkdir(parents=True)
    marker = entry.parent / "agenticml.toml"
    if kind == "symlink":
        target = project / "config.toml"
        target.write_text('task = "wrong"\n')
        marker.symlink_to(target)
    elif kind == "directory":
        marker.mkdir()
    elif kind == "large":
        marker.write_text('task = "wrong"\n' + "#" * (64 * 1024))
    elif kind == "unreadable":
        marker.write_text('task = "wrong"\n')
        original = os.open
        def denied(path, *args, **kwargs):
            if path == marker:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(os, "open", denied)
    elif kind == "nested":
        marker.write_text('[section]\ntask = "wrong"\n')
    else:
        marker.write_bytes(b'task = "\xff"\n')
    assert resolve_task(None, project, entry) == ("src/tts", "entry")


def test_task_marker_python39_fallback(project, monkeypatch):
    import builtins
    from agenticml.code_tracking import resolve_task

    original = builtins.__import__
    def no_tomllib(name, *args, **kwargs):
        if name == "tomllib":
            raise ImportError(name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_tomllib)
    monkeypatch.delenv("AGENTICML_TASK", raising=False)
    marker = project / "agenticml.toml"
    for content, expected in [('task = " asr " # comment\n', "asr"),
                              ("task = 'tts'\n", "tts"),
                              ('[other]\ntask = "nested"\n', None),
                              ('task = [invalid\n', None)]:
        marker.write_text(content)
        assert resolve_task(None, project) == ((expected, "marker") if expected else (None, None))


@pytest.mark.parametrize("meta, expected", [
    ({"name": "asr/train.py"}, ("asr", "legacy")),
    ({"name": "src/tts/train.py"}, ("src/tts", "legacy")),
    ({"name": "asr/train.py", "task": None}, ("", None)),
    ({"name": "asr/train.py", "task": "tts", "task_source": "explicit"}, ("tts", "explicit")),
    ({"task": "", "task_source": "explicit"}, ("", "explicit")),
    ({"task": ["bad"]}, ("", None)),
    ({"task": "asr", "task_source": {}}, ("asr", None)),
    ({"name": "/asr/train.py"}, ("", None)),
    ({"name": "../asr/train.py"}, ("", None)),
    ({"name": "asr\\train.py"}, ("", None)),
    ({"name": "asr/train\x00.py"}, ("", None)),
    ({"name": "train.py"}, ("", None)),
    ({"name": {"bad": True}}, ("", None)),
    (None, ("", None)),
])
def test_run_task_legacy_and_malformed(meta, expected):
    assert lineage.run_task(meta) == expected


def test_entry_tasks_isolate_parents_and_cli(project, tmp_path, monkeypatch, capsys):
    root = tmp_path / "store"
    runs = []
    for task in ("asr", "imagegen", "asr"):
        entry = project / task / "train.py"
        entry.parent.mkdir(exist_ok=True)
        entry.write_text("pass\n")
        monkeypatch.setattr(sys.modules["__main__"], "__file__", str(entry))
        monkeypatch.setattr(sys, "argv", [str(entry)])
        with start(root) as run:
            meta = RunReader(root, run.run_id).meta()
            assert run.task == meta["task"] == task
            assert meta["task_source"] == "entry"
            assert info(run)["task"] == task
        runs.append(run)
    first, image, second = runs
    assert info(first)["parent_run_id"] is None
    assert info(image)["parent_run_id"] is None
    assert info(second)["parent_run_id"] == first.run_id
    assert info(second)["parent_inference"] == "repo_root"
    inferred = lineage.infer_parents(lineage.list_runs(root))
    assert inferred[first.run_id] == inferred[image.run_id] == (None, None)
    assert inferred[second.run_id] == (first.run_id, "repo_root")
    repo = root / "lineage" / f"{info(second)['project_key']}.git"
    assert b"task: asr\n" in git(repo, "show", "-s", "--format=%B", info(second)["commit"])
    assert main(["lineage", "ls", "--dir", str(root)]) == 0
    output = capsys.readouterr().out
    assert "test\n  [asr]\n" in output
    assert f"    {first.run_id}" in output and f"      {second.run_id}" in output
    assert f"  [imagegen]\n    {image.run_id}" in output
    assert main(["lineage", "ls", "--task", "asr", "--dir", str(root)]) == 0
    output = capsys.readouterr().out
    assert image.run_id not in output and "[imagegen]" not in output
    assert first.run_id in output and second.run_id in output
    with start(root, task="imagegen", parent_run_id=second.run_id) as cross:
        assert info(cross)["parent_run_id"] == second.run_id
        assert info(cross)["parent_inference"] == "explicit"
        assert info(cross)["parent_commit"] == info(second)["commit"]


def test_inference_task_fallback_and_legacy(project, tmp_path):
    root = tmp_path / "store"
    for rid, name in (("asr-first", "asr/train.py"), ("image", "imagegen/train.py"),
                      ("asr-second", "asr/train.py")):
        writer = RunWriter.create(root, run_id=rid, meta={"project": "p", "name": name,
                                                        "git": {"repo_root": str(project)}})
        writer.finish("finished")
    before = "2099-01-01T00:00:00.000000Z"
    assert lineage.infer_parent(root, "p", "/other", before, task="asr") == ("asr-second", "task")
    assert lineage.infer_parent(root, "p", "/other", before) == (None, None)
    assert lineage.infer_parents(lineage.list_runs(root)) == {
        "asr-first": (None, None), "image": (None, None), "asr-second": ("asr-first", "repo_root"),
    }


def test_no_task_keeps_project_stream_and_resume(project, tmp_path, monkeypatch, capsys):
    root = tmp_path / "store"
    monkeypatch.delenv("AGENTICML_TASK", raising=False)
    monkeypatch.delattr(sys.modules["__main__"], "__file__", raising=False)
    with start(root) as first:
        assert first.task is None
        assert RunReader(root, first.run_id).meta()["task"] is None
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    with start(root, track_source=False) as second:
        assert second.task is None
        assert info(second)["parent_run_id"] == first.run_id
        assert info(second)["parent_inference"] == "project"
    with start(root, id=first.run_id, resume="allow", task="new-task") as resumed:
        assert resumed.task is None
        assert RunReader(root, resumed.run_id).meta()["task"] is None
    assert main(["lineage", "ls", "--task", "", "--dir", str(root)]) == 0
    assert "  [no task]" in capsys.readouterr().out


def test_resume_keeps_stored_task(project, tmp_path):
    root = tmp_path / "store"
    with start(root, task="asr") as first:
        pass
    with start(root, id=first.run_id, resume="allow", task="imagegen") as resumed:
        assert resumed.task == "asr"
        assert RunReader(root, resumed.run_id).meta()["task_source"] == "explicit"

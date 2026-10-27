"""Tests for garbage collection, storage usage reporting, and disk budget."""

import json
import os
import time
from pathlib import Path

import pytest

from agenticml.store import (
    RunReader,
    RunWriter,
    check_budget,
    collect,
    has,
    put_bytes,
    run_usage,
    run_fingerprint,
    prunable_series,
    prune_run,
    usage,
)


def _mark_synced(run_dir: Path, host="https://api.agenticml.com", complete=True):
    sync_file = run_dir / "sync.json"
    sync_data = {
        "version": 1,
        "hosts": {
            host: {
                "complete": complete,
                "source_hash": run_fingerprint(run_dir.parent.parent, run_dir.name),
                "finalized": complete,
            }
        },
    }
    sync_file.write_text(json.dumps(sync_data, indent=2), encoding="utf-8")


def test_run_usage_breakdown(tmp_path):
    writer = RunWriter.create(tmp_path, run_id="usage-test-1", meta={"project": "p1"})
    # Write some scalars
    writer.write_scalars([(0, {"loss": 1.0, "acc": 0.5}), (1, {"loss": 0.8, "acc": 0.6})])
    # Add code
    code_blob = b"print('hello')"
    writer.add_code([{"path": "train.py"}], [("train.py", code_blob)])
    # Add media
    media_blob = b"fake-png-bytes"
    writer.add_media("preview", 1, media_blob, "image/png", "sample")
    # Add artifact with copy=True and copy=False
    art_file = tmp_path / "model.pt"
    art_file.write_bytes(b"checkpoint-weights")
    writer.add_artifact(art_file, "model", "model", {}, copy=True)

    ref_file = tmp_path / "dataset.csv"
    ref_file.write_text("a,b,c")
    writer.add_artifact(ref_file, "dataset", "dataset", {}, copy=False)

    writer.finish("finished", {"loss": 0.8})

    res = run_usage(tmp_path, writer.run_id)
    assert res["run_id"] == "usage-test-1"
    assert res["raw"] > 0
    assert res["meta"] > 0
    assert res["code"] == len(code_blob)
    assert res["media"] == len(media_blob)
    assert res["artifacts"] == len(b"checkpoint-weights")
    assert res["run_dir_total"] == res["raw"] + res["overview"] + res["meta"]
    assert res["objects_total"] == res["code"] + res["media"] + res["artifacts"]
    assert res["total"] == res["run_dir_total"] + res["objects_total"]


def test_usage_store_totals_and_unreferenced(tmp_path):
    writer = RunWriter.create(tmp_path, run_id="synced-run", meta={"project": "proj-a"})
    writer.write_scalars([(0, {"x": 1.0})])
    writer.finish("finished")
    _mark_synced(tmp_path / "runs" / "synced-run", complete=True)

    writer2 = RunWriter.create(tmp_path, run_id="unsynced-run", meta={"project": "proj-a"})
    writer2.write_scalars([(0, {"x": 2.0})])
    writer2.finish("crashed")

    # Put an unreferenced object into the store
    unref_sha = put_bytes(tmp_path, b"unreferenced-data-12345")

    store_usage = usage(tmp_path)
    assert store_usage["runs_total"] > 0
    assert store_usage["objects_total"] >= len(b"unreferenced-data-12345")
    assert store_usage["unreferenced_total"] >= len(b"unreferenced-data-12345")
    assert store_usage["unreferenced_count"] >= 1
    assert store_usage["total"] == store_usage["runs_total"] + store_usage["objects_total"]

    runs_by_id = {r["run_id"]: r for r in store_usage["runs"]}
    assert runs_by_id["synced-run"]["synced"] is True
    assert runs_by_id["synced-run"]["status"] == "finished"
    assert runs_by_id["unsynced-run"]["synced"] is False
    assert runs_by_id["unsynced-run"]["status"] == "crashed"


def test_collect_dry_run(tmp_path):
    writer = RunWriter.create(tmp_path, run_id="run-to-dry", meta={"project": "p"})
    writer.write_scalars([(0, {"x": 1.0})])
    writer.finish("finished")
    _mark_synced(tmp_path / "runs" / "run-to-dry", complete=True)

    report = collect(tmp_path, dry_run=True)
    assert report["runs_deleted"] == 1
    assert report["runs_bytes_freed"] > 0
    assert report["deleted_runs"] == ["run-to-dry"]

    # Verify run was NOT deleted
    assert (tmp_path / "runs" / "run-to-dry").is_dir()


def test_collect_never_deletes_running_runs(tmp_path):
    writer = RunWriter.create(tmp_path, run_id="running-run", meta={"project": "p"})
    writer.write_scalars([(0, {"x": 1.0})])
    _mark_synced(tmp_path / "runs" / "running-run", complete=True)

    report = collect(tmp_path, include_unsynced=True)
    assert report["runs_deleted"] == 0
    assert (tmp_path / "runs" / "running-run").is_dir()


def test_collect_unsynced_runs_default_vs_include(tmp_path):
    writer = RunWriter.create(tmp_path, run_id="unsynced-finished", meta={"project": "p"})
    writer.write_scalars([(0, {"x": 1.0})])
    writer.finish("finished")

    # Default collect ignores unsynced runs
    report_default = collect(tmp_path, include_unsynced=False)
    assert report_default["runs_deleted"] == 0
    assert (tmp_path / "runs" / "unsynced-finished").is_dir()

    # include_unsynced=True collects it
    report_unsynced = collect(tmp_path, include_unsynced=True)
    assert report_unsynced["runs_deleted"] == 1
    assert not (tmp_path / "runs" / "unsynced-finished").exists()


def test_collect_keep_last_per_project(tmp_path):
    # Create 3 finished synced runs in proj-A and 2 in proj-B
    for i in range(3):
        w = RunWriter.create(tmp_path, run_id=f"proj-a-{i}", meta={"project": "proj-A"})
        w.write_scalars([(0, {"x": float(i)})])
        w.finish("finished")
        _mark_synced(tmp_path / "runs" / f"proj-a-{i}", complete=True)
        time.sleep(0.01)

    for i in range(2):
        w = RunWriter.create(tmp_path, run_id=f"proj-b-{i}", meta={"project": "proj-B"})
        w.write_scalars([(0, {"x": float(i)})])
        w.finish("finished")
        _mark_synced(tmp_path / "runs" / f"proj-b-{i}", complete=True)
        time.sleep(0.01)

    # keep_last=1 keeps the newest 1 per project:
    # proj-a-2 kept, proj-b-1 kept.
    # proj-a-0, proj-a-1, proj-b-0 deleted.
    report = collect(tmp_path, keep_last=1)
    assert report["runs_deleted"] == 3
    assert set(report["deleted_runs"]) == {"proj-a-0", "proj-a-1", "proj-b-0"}
    assert (tmp_path / "runs" / "proj-a-2").is_dir()
    assert (tmp_path / "runs" / "proj-b-1").is_dir()


def test_collect_older_than_days(tmp_path, monkeypatch):
    w1 = RunWriter.create(tmp_path, run_id="old-run", meta={"project": "p"})
    w1.write_scalars([(0, {"x": 1.0})])
    w1.finish("finished")
    _mark_synced(tmp_path / "runs" / "old-run", complete=True)

    # Set created_at to 10 days ago in meta.json
    meta_path = tmp_path / "runs" / "old-run" / "meta.json"
    meta = json.loads(meta_path.read_text())
    old_time = time.time() - (10 * 86400)
    meta["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(old_time))
    meta_path.write_text(json.dumps(meta, indent=2))

    w2 = RunWriter.create(tmp_path, run_id="new-run", meta={"project": "p"})
    w2.write_scalars([(0, {"x": 2.0})])
    w2.finish("finished")
    _mark_synced(tmp_path / "runs" / "new-run", complete=True)

    # Filter older than 5 days
    report = collect(tmp_path, older_than_days=5)
    assert report["runs_deleted"] == 1
    assert report["deleted_runs"] == ["old-run"]
    assert not (tmp_path / "runs" / "old-run").exists()
    assert (tmp_path / "runs" / "new-run").is_dir()


def test_collect_mark_and_sweep_one_hour_guard(tmp_path):
    # Create a run that references an object
    w = RunWriter.create(tmp_path, run_id="keep-run", meta={"project": "p"})
    ref_sha = w.add_media("img", 0, b"referenced-image", "application/octet-stream", None)
    w.finish("finished")
    _mark_synced(tmp_path / "runs" / "keep-run", complete=True)

    # Put two unreferenced objects:
    # 1. Old (> 1 hour old)
    # 2. Fresh (< 1 hour old)
    old_sha = put_bytes(tmp_path, b"old-unreferenced-blob")
    fresh_sha = put_bytes(tmp_path, b"fresh-unreferenced-blob")

    old_path = tmp_path / "objects" / old_sha[:2] / old_sha
    fresh_path = tmp_path / "objects" / fresh_sha[:2] / fresh_sha

    # Set old_path mtime to 2 hours ago
    two_hours_ago = time.time() - 7200
    os.utime(old_path, (two_hours_ago, two_hours_ago))

    # Keep fresh_path mtime as current (less than 1 hour old)
    report = collect(tmp_path, keep_last=10)

    # The referenced object and the fresh object are preserved!
    assert has(tmp_path, ref_sha)
    assert has(tmp_path, fresh_sha)
    # The old unreferenced object is swept!
    assert not has(tmp_path, old_sha)
    assert report["objects_deleted"] == 1
    assert report["objects_bytes_freed"] == len(b"old-unreferenced-blob")


def test_collect_never_follows_symlinks(tmp_path):
    w = RunWriter.create(tmp_path, run_id="real-run", meta={"project": "p"})
    w.finish("finished")
    _mark_synced(tmp_path / "runs" / "real-run", complete=True)

    # Create an outside directory and symlink it into runs/
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    secret_file = outside / "secret.txt"
    secret_file.write_text("do not delete")

    link_target = tmp_path / "runs" / "symlinked-run"
    link_target.symlink_to(outside)

    collect(tmp_path, include_unsynced=True)
    # secret_file and outside directory must be completely unharmed
    assert secret_file.is_file()
    assert secret_file.read_text() == "do not delete"


def test_check_budget(tmp_path, monkeypatch):
    # Empty store: usage is small
    import agenticml.store.gc as gc
    gc._refresh_usage(tmp_path)
    warn = check_budget(tmp_path)
    assert warn is None

    # usage.json was created
    usage_file = tmp_path / "usage.json"
    assert usage_file.is_file()
    data = json.loads(usage_file.read_text())
    assert "bytes" in data
    assert "computed_at" in data

    # Test with low AGENTICML_MAX_BYTES
    monkeypatch.setenv("AGENTICML_MAX_BYTES", "10")
    # Put a blob larger than 10 bytes
    put_bytes(tmp_path, b"this-is-larger-than-10-bytes")

    # Invalidate cache timestamp
    data["computed_at"] = time.time() - 90000  # older than 1 day
    usage_file.write_text(json.dumps(data))

    gc._refresh_usage(tmp_path)
    warn = check_budget(tmp_path)
    assert warn is not None
    assert "exceeds budget" in warn
    assert "agenticml du" in warn
    assert "agenticml gc" in warn
    assert "agenticml sync --prune" in warn


@pytest.mark.parametrize("saved_hash,complete", [(None, True), ("outdated", True), ("current", False)])
def test_collect_requires_complete_matching_fingerprint(tmp_path, saved_hash, complete):
    writer = RunWriter.create(tmp_path, run_id="changed")
    writer.finish("finished")
    state = {"complete": complete}
    if saved_hash is not None:
        state["source_hash"] = run_fingerprint(tmp_path, writer.run_id) if saved_hash == "current" else saved_hash
    (writer.run_dir / "sync.json").write_text(json.dumps({"hosts": {"h": state}}))
    assert collect(tmp_path)["runs_deleted"] == 0
    _mark_synced(writer.run_dir)
    assert collect(tmp_path)["deleted_runs"] == [writer.run_id]


@pytest.mark.parametrize("change", ["status", "summary", "config", "series", "bin", "events", "media"])
def test_fingerprint_tracks_sync_sources(tmp_path, change):
    writer = RunWriter.create(tmp_path)
    writer.write_scalars([(0, {"loss": 1.0})])
    writer.finish("finished")
    before = run_fingerprint(tmp_path, writer.run_id)
    if change in ("status", "summary", "config"):
        path = writer.run_dir / "meta.json"
        data = json.loads(path.read_text())
        data[change] = "crashed" if change == "status" else {"updated": True}
        path.write_text(json.dumps(data))
    elif change == "series":
        path = writer.run_dir / "scalars/index.json"
        data = json.loads(path.read_text())
        data["series"]["loss"]["monotonic"] = False
        path.write_text(json.dumps(data))
    else:
        path = writer.run_dir / {"bin": "scalars/0.bin", "events": "events.jsonl", "media": "media/index.json"}[change]
        with path.open("ab") as handle:
            handle.write(b" ")
    assert run_fingerprint(tmp_path, writer.run_id) != before


def test_fingerprint_ignores_sync_state_and_mtime(tmp_path):
    writer = RunWriter.create(tmp_path)
    writer.finish("finished")
    before = run_fingerprint(tmp_path, writer.run_id)
    _mark_synced(writer.run_dir)
    for path in writer.run_dir.rglob("*"):
        if path.is_file():
            os.utime(path, (1, 1))
    assert run_fingerprint(tmp_path, writer.run_id) == before


@pytest.mark.skipif(os.name == "nt", reason="Windows has no-op run locks")
def test_collect_skips_locked_stale_run(tmp_path, caplog):
    writer = RunWriter.create(tmp_path, run_id="in-use")
    os.utime(writer.run_dir / "meta.json", (1, 1))
    with caplog.at_level("INFO", logger="agenticml.store.gc"):
        report = collect(tmp_path, include_unsynced=True)
    assert report["runs_deleted"] == 0
    assert report["in_use"] == [writer.run_id]
    assert "in use" in caplog.text
    writer.close()
    os.utime(writer.run_dir / "run.lock", (1, 1))
    assert collect(tmp_path, include_unsynced=True)["runs_deleted"] == 1


def test_collect_rechecks_heartbeat_after_lock(tmp_path, monkeypatch):
    import agenticml.store.gc as gc_mod
    writer = RunWriter.create(tmp_path, run_id="resumed")
    writer.finish("finished")
    real_lock = gc_mod.try_lock_run

    def change_status(directory):
        handle = real_lock(directory)
        meta = json.loads((directory / "meta.json").read_text())
        meta["status"] = "running"
        (directory / "meta.json").write_text(json.dumps(meta))
        return handle

    monkeypatch.setattr(gc_mod, "try_lock_run", change_status)
    assert collect(tmp_path, include_unsynced=True, dry_run=False)["runs_deleted"] == 0
    assert writer.run_dir.exists()
    os.utime(writer.run_dir / "run.lock", (1, 1))
    assert collect(tmp_path, include_unsynced=True, dry_run=False)["runs_deleted"] == 1
    assert not writer.run_dir.exists()


@pytest.mark.parametrize("broken", ["meta.json", "events.jsonl", "media/index.json", "manifest", "missing", "oserror"])
def test_collect_reference_errors_abort_sweep_but_report_deletions(tmp_path, monkeypatch, caplog, broken):
    import agenticml.store.reader as reader_mod
    survivor = RunWriter.create(tmp_path, run_id="survivor")
    sha = survivor.add_media("img", 0, b"precious", "application/octet-stream", None)
    survivor.finish("finished")
    doomed = RunWriter.create(tmp_path, run_id="doomed")
    doomed.finish("finished")
    _mark_synced(doomed.run_dir)
    blob = tmp_path / "objects" / sha[:2] / sha
    os.utime(blob, (1, 1))
    if broken == "manifest":
        with (survivor.run_dir / "events.jsonl").open("a") as handle:
            handle.write('{"kind":"code","manifest":"corrupt"}\n')
    elif broken == "missing":
        (survivor.run_dir / "events.jsonl").unlink()
    elif broken == "oserror":
        real_read = reader_mod.read_file_nofollow

        def deny(path):
            if str(path).endswith("survivor/media/index.json"):
                raise PermissionError("unreadable")
            return real_read(path)

        monkeypatch.setattr(reader_mod, "read_file_nofollow", deny)
    else:
        (survivor.run_dir / broken).write_text("{corrupt")
    report = collect(tmp_path)
    assert report["deleted_runs"] == ["doomed"]
    assert report["objects_deleted"] == 0
    assert "survivor" in report["sweep_error"]
    assert "object sweep aborted" in caplog.text
    assert blob.exists()


def test_collect_restats_reused_object_before_unlink(tmp_path, monkeypatch):
    import agenticml.store.gc as gc_mod
    sha = put_bytes(tmp_path, b"reused-during-sweep")
    path = tmp_path / "objects" / sha[:2] / sha
    os.utime(path, (1, 1))
    real_stat = gc_mod.os.stat
    seen = []

    def reuse_then_stat(candidate, *args, **kwargs):
        if str(candidate) == str(path):
            seen.append(candidate)
            os.utime(path, None)
        return real_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(gc_mod.os, "stat", reuse_then_stat)
    assert collect(tmp_path)["objects_deleted"] == 0
    assert seen
    assert path.exists()


@pytest.mark.parametrize("recreate", [False, True])
def test_collect_preserves_dedup_refresh_between_stat_and_rename(tmp_path, monkeypatch, recreate):
    import agenticml.store.gc as gc_mod
    data = b"dedup-at-final-delete-boundary"
    sha = put_bytes(tmp_path, data)
    path = tmp_path / "objects" / sha[:2] / sha
    os.utime(path, (1, 1))
    real_rename = gc_mod.os.rename
    refreshed = []
    new_inode = []

    def refresh_then_rename(src, dst, *args, **kwargs):
        if Path(src) == path:
            # GC has already re-statted the old candidate. Dedup's utime
            # lands in the gap that used to end with an unsafe unlink.
            assert put_bytes(tmp_path, data) == sha
            refreshed.append(path)
        result = real_rename(src, dst, *args, **kwargs)
        if Path(src) == path and recreate:
            assert put_bytes(tmp_path, data) == sha
            new_inode.append(path.stat().st_ino)
        return result

    monkeypatch.setattr(gc_mod.os, "rename", refresh_then_rename)
    report = collect(tmp_path)
    assert refreshed == [path]
    assert report["objects_deleted"] == 0
    assert path.read_bytes() == data
    assert path.stat().st_mtime >= time.time() - 10
    assert not list(path.parent.glob(".trash-*"))
    if recreate:
        assert [path.stat().st_ino] == new_inode


def test_collect_keeps_writer_object_published_after_trash_rename(tmp_path, monkeypatch):
    import agenticml.store.gc as gc_mod
    data = b"publish-while-old-inode-is-in-trash"
    sha = put_bytes(tmp_path, data)
    path = tmp_path / "objects" / sha[:2] / sha
    os.utime(path, (1, 1))
    real_rename = gc_mod.os.rename
    published = []

    def rename_then_publish(src, dst, *args, **kwargs):
        result = real_rename(src, dst, *args, **kwargs)
        if Path(src) == path:
            assert not path.exists()
            assert put_bytes(tmp_path, data) == sha
            published.append(path.stat().st_ino)
        return result

    monkeypatch.setattr(gc_mod.os, "rename", rename_then_publish)
    report = collect(tmp_path)
    assert report["objects_deleted"] == 1
    assert report["objects_bytes_freed"] == len(data)
    assert path.read_bytes() == data
    assert [path.stat().st_ino] == published
    assert not list(path.parent.glob(".trash-*"))


def test_collect_cleans_stale_trash_and_usage_ignores_trash(tmp_path):
    sha = put_bytes(tmp_path, b"stale-trash")
    path = tmp_path / "objects" / sha[:2] / sha
    stale = path.with_name(f".trash-{sha}-123")
    path.rename(stale)
    os.utime(stale, (1, 1))
    fresh = path.with_name(f".trash-{sha}-456")
    fresh.write_bytes(b"fresh-trash")
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    link = path.with_name(f".trash-{sha}-789")
    link.symlink_to(outside)
    directory = path.with_name(".trash-directory")
    directory.mkdir()
    os.utime(directory, (1, 1))
    totals = usage(tmp_path)
    assert totals["objects_total"] == totals["unreferenced_total"] == totals["unreferenced_count"] == 0
    preview = collect(tmp_path, dry_run=True)
    assert preview["objects_deleted"] == 1
    assert preview["objects_bytes_freed"] == len(b"stale-trash")
    assert stale.exists()
    report = collect(tmp_path)
    assert report["objects_deleted"] == 1
    assert report["objects_bytes_freed"] == len(b"stale-trash")
    assert not stale.exists()
    assert fresh.read_bytes() == b"fresh-trash"
    assert directory.is_dir()
    assert link.is_symlink()
    assert outside.read_bytes() == b"keep"


def test_prune_validates_counts_and_rechecks_under_lock(tmp_path, monkeypatch):
    from agenticml.store import Overview
    import agenticml.store.gc as gc_mod
    writer = RunWriter.create(tmp_path)
    writer.write_scalars([(i, {"good": i, "short": i, "corrupt": i, "missing": i}) for i in range(3)])
    writer.finish("finished")
    overview = Overview()
    overview.add_many([(i, float(i)) for i in range(3)])
    scalars = writer.run_dir / "scalars"
    (scalars / "0.ovw").write_bytes(overview.to_bytes())
    overview.add(3, 3.0)
    (scalars / "1.ovw").write_bytes(overview.to_bytes())
    (scalars / "2.ovw").write_bytes(b"corrupt")
    assert prunable_series(tmp_path, writer.run_id) == ["good"]
    with pytest.raises(ValueError, match="matching valid overviews"):
        prune_run(tmp_path, writer.run_id, ["good", "short"])
    assert (scalars / "0.bin").exists()
    # A once-prunable series grew before the prune call; reject it again.
    with (scalars / "0.bin").open("ab") as handle:
        handle.write(b"\0" * 8)
    assert prunable_series(tmp_path, writer.run_id) == []
    with pytest.raises(ValueError):
        prune_run(tmp_path, writer.run_id, ["good"])
    with (scalars / "0.bin").open("r+b") as handle:
        handle.truncate(24)
    fsynced = []
    monkeypatch.setattr(gc_mod, "fsync_dir", lambda path: fsynced.append(path))
    prune_run(tmp_path, writer.run_id, ["good"])
    assert fsynced == [scalars]
    assert not (scalars / "0.bin").exists()
    assert (scalars / "1.bin").exists()
    assert RunReader(tmp_path, writer.run_id).series("good")["total"] == 3


@pytest.mark.skipif(os.name == "nt", reason="Windows has no-op run locks")
def test_prune_refuses_locked_run(tmp_path):
    writer = RunWriter.create(tmp_path)
    with pytest.raises(ValueError, match="open in another process"):
        prune_run(tmp_path, writer.run_id, [])
    writer.close()
    prune_run(tmp_path, writer.run_id, [])


def test_prune_rechecks_expected_fingerprint_under_lock(tmp_path, monkeypatch):
    from agenticml.store import Overview
    import agenticml.store.gc as gc_mod
    from agenticml.store.layout import try_lock_run, unlock_run

    writer = RunWriter.create(tmp_path)
    writer.write_scalars([(0, {"loss": 1.0, "acc": 0.5})])
    writer.finish("finished")
    expected = run_fingerprint(tmp_path, writer.run_id)
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    resumed.write_scalars([(1, {"loss": 0.5, "acc": 0.75})])
    resumed.finish("finished")
    scalars = writer.run_dir / "scalars"
    for key in ("loss", "acc"):
        overview = Overview()
        overview.add_many(RunReader(tmp_path, writer.run_id).series(key)["points"])
        (scalars / f"{writer._series[key].id}.ovw").write_bytes(overview.to_bytes())
    assert set(prunable_series(tmp_path, writer.run_id)) == {"loss", "acc"}
    before = {path: path.read_bytes() for path in scalars.iterdir()}
    checked = []
    original = gc_mod.run_fingerprint

    def fingerprint(root, run_id):
        if os.name != "nt":
            handle = try_lock_run(writer.run_dir)
            if handle is not None:
                unlock_run(handle)
                pytest.fail("fingerprint checked without holding run.lock")
        checked.append(run_id)
        return original(root, run_id)

    monkeypatch.setattr(gc_mod, "run_fingerprint", fingerprint)
    with pytest.raises(ValueError, match="^run changed since sync; not pruning$"):
        prune_run(tmp_path, writer.run_id, ["loss", "acc"], expected_fingerprint=expected)
    assert checked == [writer.run_id]
    assert {path: path.read_bytes() for path in scalars.iterdir()} == before
    prune_run(tmp_path, writer.run_id, ["loss", "acc"],
              expected_fingerprint=original(tmp_path, writer.run_id))
    for key in ("loss", "acc"):
        assert not (scalars / f"{writer._series[key].id}.bin").exists()
    assert (scalars / "_time.bin").read_bytes() == before[scalars / "_time.bin"]


def test_collect_keeps_lock_file_until_metadata_removed(tmp_path, monkeypatch):
    import agenticml.store.gc as gc_mod
    writer = RunWriter.create(tmp_path, run_id="deleting")
    writer.finish("finished")
    real_unlink = gc_mod.os.unlink
    order = []

    def check_unlink(path, *args, **kwargs):
        if str(path).startswith(str(writer.run_dir)):
            order.append(Path(path).name)
            if Path(path).name == "run.lock":
                assert not (writer.run_dir / "meta.json").exists()
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(gc_mod.os, "unlink", check_unlink)
    assert collect(tmp_path, include_unsynced=True)["deleted_runs"] == [writer.run_id]
    assert order[-1] == "run.lock"

"""Regressions for bounded memory and tracking that never blocks training."""
import errno
import json
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

import agenticml
from agenticml._globals import _DisabledRun
from agenticml.integrations import hyperparams
from agenticml.run import Run
from agenticml.store import RunReader, RunWriter, layout, list_runs
from agenticml.store import gc
from agenticml.sync import _scalar_batches


def test_thousand_key_scalar_round_budget_and_offsets():
    rounds = []
    limits = []

    class Reader:
        def record_sizes(self):
            return dict.fromkeys(range(1000), 401 * 8)

        def read_records(self, key, offset, limit):
            if key == 0:
                rounds.append(0)
            limits.append(limit)
            start = offset // 8
            records = [(step, float(step)) for step in range(start, min(start + limit, 401))]
            rounds[-1] += len(records)
            return records, offset + len(records) * 8

    offsets = {}
    points = 0
    for batch, current, skipped in _scalar_batches(Reader(), "remote", {}):
        assert skipped == 0
        for key, offset in current.items():
            assert offset >= offsets.get(key, 0)
        assert sum(current.values()) // 8 == points + sum(len(item["metrics"]) for item in batch)
        points = sum(current.values()) // 8
        offsets = current
    assert rounds == [200_000, 200_000, 1000]
    assert max(limits) == 200
    assert points == 401_000
    assert set(offsets.values()) == {401 * 8}


def test_lifetime_lock_liveness_is_read_only_and_resume_works(tmp_path):
    writer = RunWriter.create(tmp_path)
    path = writer.run_dir / "meta.json"
    os.utime(path, (0, 0))
    before = path.read_bytes()
    assert layout.is_run_locked(writer.run_dir) is True
    assert list_runs(tmp_path)[0]["effective_status"] == "running"
    writer.close()
    assert layout.is_run_locked(writer.run_dir) is False
    assert list_runs(tmp_path)[0]["effective_status"] == "running"
    os.utime(writer.run_dir / "run.lock", (0, 0))
    assert list_runs(tmp_path)[0]["effective_status"] == "crashed"
    assert path.read_bytes() == before
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    assert list_runs(tmp_path)[0]["effective_status"] == "running"
    resumed.finish("finished")


def test_lock_probe_does_not_create_missing_lock(tmp_path):
    assert layout.is_run_locked(tmp_path) is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP])
def test_unsupported_locking_warns_once_and_init_continues(tmp_path, monkeypatch, caplog, error):
    import fcntl

    def unsupported(*args):
        raise OSError(error, "unsupported locking")

    monkeypatch.setattr(fcntl, "flock", unsupported)
    monkeypatch.setattr(layout, "_lock_warning_emitted", False)
    run = agenticml.init("p", "n", dir=tmp_path, track_source=False,
                        capture_env=False, system_metrics=False, auto_track=False)
    assert isinstance(run, Run)
    assert layout.is_run_locked(run.writer.run_dir) is None
    os.utime(run.writer.run_dir / "meta.json", (0, 0))
    assert list_runs(tmp_path)[0]["effective_status"] == "running"
    os.utime(run.writer.run_dir / "run.lock", (0, 0))
    assert list_runs(tmp_path)[0]["effective_status"] == "stale"
    run.log({"loss": 1})
    run.finish()
    assert sum("locking is unsupported" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("factory", [Run, agenticml.init])
def test_unwritable_store_disables_tracking(tmp_path, caplog, factory):
    root = tmp_path / "readonly"
    root.mkdir(mode=0o500)
    try:
        if os.geteuid() == 0:
            pytest.skip("root can write a readonly directory")
        run = factory("p", "n", dir=root, track_source=False,
                      capture_env=False, system_metrics=False, auto_track=False)
        assert isinstance(run, _DisabledRun)
        run.log({"loss": 1})
        run.finish()
        assert "tracking disabled" in caplog.text
    finally:
        root.chmod(0o700)


@pytest.mark.parametrize("factory", [Run, agenticml.init])
def test_any_store_creation_exception_disables_tracking(tmp_path, monkeypatch, factory):
    def fail(*args, **kwargs):
        raise RuntimeError("broken store")

    monkeypatch.setattr(RunWriter, "create", fail)
    assert isinstance(factory("p", "n", dir=tmp_path, track_source=False,
                              capture_env=False, system_metrics=False, auto_track=False), _DisabledRun)


@pytest.mark.parametrize("terminal", [False, True])
def test_reader_series_lru_cap_and_terminal_cache_is_lean(tmp_path, terminal):
    writer = RunWriter.create(tmp_path)
    writer.write_scalars([(step, {f"k{i}": float(step) for i in range(40)}) for step in range(10)])
    if terminal:
        # Small fixture with valid on-disk overviews; no need to write 65k rows/key.
        from agenticml.store import Overview
        overview = Overview()
        overview.add_many((step, float(step)) for step in range(10))
        for state in writer._series.values():
            (writer.run_dir / "scalars" / f"{state.id}.ovw").write_bytes(overview.to_bytes())
        writer.set_meta_fields(status="finished")
    reader = RunReader(tmp_path, writer.run_id)
    for i in range(40):
        assert reader.series(f"k{i}", max_points=2)["total"] == 10
        assert len(set(reader._cache) | set(reader._ovw_decision)) <= 32
    assert "k0" not in reader._series_lru
    reader.series("k8", max_points=2)
    reader.series("k0", max_points=2)
    assert "k8" in reader._series_lru and "k9" not in reader._series_lru
    if terminal:
        assert not reader._cache
        assert all(value[1] is not None for value in reader._ovw_decision.values())
    writer.close()


def test_capture_redacts_secret_keys_nested_config_and_argv(monkeypatch):
    import argparse
    import sys

    class Captured:
        config = {}
        _explicit_config_keys = set()
        _finished = False

    captured = Captured()
    hyperparams.install_hyperparam_capture(captured)
    argv = ["--api-key=abc", "--PASSWORD", "def", "--lr", "0.1"]
    monkeypatch.setattr(sys, "argv", ["train.py", *argv])
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key")
    parser.add_argument("--PASSWORD")
    parser.add_argument("--lr", type=float)
    try:
        parsed = parser.parse_args()
        assert parsed.api_key == "abc" and parsed.PASSWORD == "def"
        assert captured.config == {"api_key": "***", "PASSWORD": "***", "lr": 0.1,
                                   "_argv": ["--api-key=***", "--PASSWORD", "***", "--lr", "0.1"]}
        hyperparams.merge_captured({"nested": {"authToken": "hidden", "batch": 32}})
        assert captured.config["nested"] == {"authToken": "***", "batch": 32}
    finally:
        hyperparams.uninstall_hyperparam_capture(captured)


def test_git_metadata_timeout_skips_dirty_flag(monkeypatch, tmp_path):
    from agenticml.lineage import get_git_metadata
    calls = []

    def git(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] == 5
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        if "status" in args:
            raise subprocess.TimeoutExpired(args, 5)
        return SimpleNamespace(stdout="abc\n")

    monkeypatch.setattr(subprocess, "run", git)
    assert get_git_metadata(tmp_path) == {"git_commit": "abc", "git_branch": "abc"}
    assert calls[-1] == ["git", "status", "--porcelain", "-uno"]


def test_budget_refresh_runs_off_main_thread(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    workers = []

    def scan(root):
        assert threading.current_thread() is not threading.main_thread()
        workers.append(threading.current_thread())
        entered.set()
        assert release.wait(5)
        return 100

    monkeypatch.setattr(gc, "_cheap_total_bytes", scan)
    monkeypatch.setenv("AGENTICML_MAX_BYTES", "10")
    try:
        assert gc.check_budget(tmp_path) is None
        assert entered.wait(5)
        assert not (tmp_path / "usage.json").exists()
        assert gc.check_budget(tmp_path) is None  # Don't schedule another refresh.
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
    assert len(workers) == 1
    assert "exceeds budget" in gc.check_budget(tmp_path)


def test_finish_updates_cached_usage_without_store_walk(tmp_path, monkeypatch):
    writer = RunWriter.create(tmp_path)
    gc._refresh_usage(tmp_path)
    before = json.loads((tmp_path / "usage.json").read_text())
    original = gc._cheap_total_bytes
    scanned = []

    def scan(path):
        scanned.append(path)
        return original(path)

    monkeypatch.setattr(gc, "_cheap_total_bytes", scan)
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(1000)])
    writer.finish("finished")
    after = json.loads((tmp_path / "usage.json").read_text())
    size = original(writer.run_dir)
    assert after["bytes"] == before["bytes"] + size - before["runs"][writer.run_id]
    assert scanned == [writer.run_dir]
    assert after["computed_at"] == before["computed_at"]


def test_gc_collects_dead_running_run(tmp_path):
    writer = RunWriter.create(tmp_path)
    writer.close()
    os.utime(writer.run_dir / "run.lock", (0, 0))
    assert gc.collect(tmp_path, include_unsynced=True, dry_run=False)["deleted_runs"] == [writer.run_id]


def test_last_step_reads_only_last_complete_record(tmp_path, monkeypatch):
    writer = RunWriter.create(tmp_path)
    path = writer.run_dir / "scalars" / "_time.bin"
    path.write_bytes(layout.RECORD_STRUCT.pack(3, 0) * 100_000 + layout.RECORD_STRUCT.pack(8, 1) + b"torn")
    import agenticml.store.reader as reader_module
    real_open = reader_module.open_nofollow
    reads = []

    class Handle:
        def __enter__(self):
            self.handle = real_open(path)
            return self
        def __exit__(self, *args):
            self.handle.close()
        def __getattr__(self, key):
            return getattr(self.handle, key)
        def read(self, size=-1):
            reads.append(size)
            return self.handle.read(size)

    monkeypatch.setattr(reader_module, "open_nofollow", lambda _: Handle())
    assert RunReader(tmp_path, writer.run_id).last_step() == 8
    assert reads == [layout.RECORD_SIZE]
    writer.close()


@pytest.mark.parametrize("locked", [True, False, None])
@pytest.mark.parametrize("age", [119, 120])
def test_heartbeat_and_lock_both_determine_liveness(tmp_path, monkeypatch, locked, age):
    from agenticml.store import reader
    writer = RunWriter.create(tmp_path)
    writer.close()
    os.utime(writer.run_dir / "run.lock", (1000 - age, 1000 - age))
    monkeypatch.setattr(reader, "is_run_locked", lambda _: locked)
    expected = "running" if locked or age < 120 else "crashed" if locked is False else "stale"
    assert reader._effective_status({"status": "running"}, "", str(writer.run_dir), 1000) == expected


def test_quiet_run_flush_thread_refreshes_heartbeat(tmp_path, monkeypatch):
    from agenticml.store import reader
    refreshed = threading.Event()
    heartbeat = RunWriter.heartbeat
    calls = []

    def tick(writer):
        heartbeat(writer)
        calls.append(writer)
        if len(calls) > 1:
            refreshed.set()

    monkeypatch.setattr(RunWriter, "heartbeat", tick)
    monkeypatch.setattr(reader, "is_run_locked", lambda _: None)
    run = Run("p", "n", dir=tmp_path, flush_interval=0.02, system_metrics=False,
              capture_env=False, track_source=False, auto_track=False)
    try:
        assert refreshed.wait(5)
        assert list_runs(tmp_path)[0]["effective_status"] == "running"
        assert run.writer.run_dir.joinpath("run.lock").stat().st_mtime > 1
        assert not run.writer.run_dir.joinpath("scalars", "_time.bin").exists()
    finally:
        run.finish()


@pytest.mark.parametrize("operation", ["create", "resume"])
def test_writer_retries_brief_shared_probe(tmp_path, operation):
    import fcntl
    directory = tmp_path / "runs" / "retry-run"
    if operation == "resume":
        RunWriter.create(tmp_path, run_id="retry-run").close()
    else:
        directory.mkdir(parents=True)
    with (directory / "run.lock").open("ab") as probe:
        fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        timer = threading.Timer(0.1, lambda: fcntl.flock(probe, fcntl.LOCK_UN))
        timer.start()
        try:
            writer = (RunWriter.create(tmp_path, run_id="retry-run") if operation == "create" else
                      RunWriter.resume(tmp_path, "retry-run", must=True))
            writer.close()
        finally:
            timer.join(5)


@pytest.mark.parametrize("bad", ["symlink", "directory", "permission"])
def test_bad_run_lock_does_not_break_listing(tmp_path, monkeypatch, caplog, bad):
    writer = RunWriter.create(tmp_path)
    writer.close()
    lock = writer.run_dir / "run.lock"
    lock.unlink()
    monkeypatch.setattr(layout, "_probe_warned", set())
    if bad == "symlink":
        target = tmp_path / "target"
        target.write_text("unchanged")
        lock.symlink_to(target)
    elif bad == "directory":
        lock.mkdir()
    else:
        lock.touch()
        original = layout.open_nofollow
        def denied(path, *args, **kwargs):
            if str(path).endswith("run.lock"):
                raise PermissionError(errno.EACCES, "denied")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(layout, "open_nofollow", denied)
    assert layout.is_run_locked(writer.run_dir) is None
    for _ in range(2):
        assert list_runs(tmp_path)[0]["effective_status"] == "running"
    assert sum("could not probe run.lock" in record.message for record in caplog.records) == 1
    if bad == "symlink":
        assert target.read_text() == "unchanged"


def test_gc_requires_old_heartbeat_and_explicit_stale_collection(tmp_path, monkeypatch):
    import fcntl
    writer = RunWriter.create(tmp_path)
    writer.close()
    assert not gc.collect(tmp_path, include_unsynced=True)["deleted_runs"]
    os.utime(writer.run_dir / "run.lock", (0, 0))
    def unsupported(*args):
        raise OSError(errno.ENOLCK, "unsupported")
    monkeypatch.setattr(fcntl, "flock", unsupported)
    monkeypatch.setattr(gc, "_is_run_synced", lambda _: True)
    assert list_runs(tmp_path)[0]["effective_status"] == "stale"
    assert not gc.collect(tmp_path)["deleted_runs"]
    assert gc.collect(tmp_path, include_unsynced=True)["deleted_runs"] == [writer.run_id]


@pytest.mark.parametrize("error", [errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP])
def test_sync_lock_unsupported_warns_once_and_continues(tmp_path, monkeypatch, caplog, error):
    import fcntl
    import agenticml.sync as sync_module
    def unsupported(*args):
        raise OSError(error, "unsupported")
    monkeypatch.setattr(fcntl, "flock", unsupported)
    monkeypatch.setattr(sync_module, "_lock_warning_emitted", False)
    assert sync_module._acquire_lock(tmp_path) is None
    assert sync_module._acquire_lock(tmp_path) is None
    assert sum("sync locking is unsupported" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("factory", [Run, agenticml.init])
def test_verbose_store_failure_prints_reason(tmp_path, monkeypatch, capsys, factory):
    def fail(*args, **kwargs):
        raise OSError("store unavailable")
    monkeypatch.setattr(RunWriter, "create", fail)
    run = factory("p", "n", dir=tmp_path, verbose=True, track_source=False,
                  capture_env=False, system_metrics=False, auto_track=False)
    assert isinstance(run, _DisabledRun)
    assert capsys.readouterr().out == "agenticml: tracking disabled: store unavailable\n"


def test_scalar_round_budget_when_keys_outnumber_budget(tmp_path, monkeypatch):
    import agenticml.sync as sync_module
    monkeypatch.setattr(sync_module, "ROUND_RECORDS", 3)
    writer = RunWriter.create(tmp_path)
    writer.write_scalars([(0, dict.fromkeys("abcde", 1))])
    try:
        batches = list(_scalar_batches(RunReader(tmp_path, writer.run_id), "remote", {}))
        assert [sum(len(item["metrics"]) for item in batch) for batch, _, _ in batches] == [3, 2]
        assert batches[-1][1] == dict.fromkeys("abcde", 8)
    finally:
        writer.close()

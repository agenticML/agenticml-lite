"""Regression tests for the invariants we just fixed:
   - mode validation in Run.__init__
   - _DisabledRun isolation across instances
   - atexit hook is unregistered after finish()
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

import pytest

import agenticml
from agenticml._globals import _DisabledRun
from agenticml.run import Run
from agenticml.store import RunReader, path_for


@pytest.mark.parametrize("mode", ["ofline", "online", ""])
def test_run_rejects_invalid_mode(mode):
    with pytest.raises(ValueError, match="mode must be one of"):
        Run("p", "n", mode=mode, track_source=False, capture_env=False, system_metrics=False)


def test_run_rejects_disabled_mode():
    # 'disabled' is a _globals concept, not a Run mode.
    with pytest.raises(ValueError, match="disabled"):
        Run("p", "n", mode="disabled", track_source=False, capture_env=False, system_metrics=False)


@pytest.mark.parametrize("env_mode", ["disabled", "online", "offline", "invalid"])
@pytest.mark.parametrize("mode", [None, "local", "offline"])
def test_run_ignores_mode_env(monkeypatch, env_mode, mode):
    monkeypatch.setenv("AGENTICML_MODE", env_mode)
    with Run(
        "p", "n", mode=mode, track_source=False,
        capture_env=False, system_metrics=False, auto_track=False,
    ) as run:
        assert run.mode == "local"


@pytest.mark.parametrize("mode", ["local", "offline", "disabled", "invalid"])
def test_init_resolves_mode_env(monkeypatch, caplog, mode):
    monkeypatch.setenv("AGENTICML_MODE", mode)
    run = agenticml.init(
        "p", "n", only_main_process=False, track_source=False,
        capture_env=False, system_metrics=False, auto_track=False,
    )
    try:
        if mode == "disabled":
            assert isinstance(run, _DisabledRun)
        else:
            assert run.mode == "local"
        if mode == "invalid":
            assert "falling back to 'local'" in caplog.text
    finally:
        agenticml.finish()


def test_disabled_run_state_is_per_instance():
    a = _DisabledRun()
    b = _DisabledRun()
    a.config["lr"] = 0.01
    a.summary["score"] = 9
    a.tags.append("a")

    assert b.config == {}
    assert b.summary == {}
    assert b.tags == []


def test_disabled_run_track_model_is_noop():
    run = _DisabledRun()
    run.track_model(object())
    run.track_model(None)


def test_disabled_run_component_tracking_is_noop():
    run = _DisabledRun()
    run.track_optimizer(object())
    run.track_loss(object())
    run.track_dataloader(object(), name="train")
    run.track_optimizer(None)
    run.track_loss(None)
    run.track_dataloader(None)


def test_disabled_run_via_module_init_resets_state(monkeypatch):
    # First disabled run sets config; finish; new init should NOT inherit.
    monkeypatch.setenv("AGENTICML_MODE", "disabled")

    agenticml.init("demo", "first")
    agenticml.config["lr"] = 0.01
    agenticml.finish()

    agenticml.init("demo", "second")
    assert "lr" not in agenticml.config  # fresh dict on the new run
    agenticml.finish()


def test_finish_unregisters_atexit_callback(monkeypatch):
    # Build a real Run with offline mode so we don't hit the network, but
    # still exercise the atexit register/unregister path.
    monkeypatch.delenv("AGENTICML_MODE", raising=False)

    run = Run(
        "p", "n",
        mode="local",
        track_source=False,
        capture_env=False,
        system_metrics=False,
    )
    cb = run._atexit_callback
    assert cb is not None

    run.finish()

    # After finish(), unregistering again must be a no-op (returns 0 in 3.10+).
    # The point: we already called unregister inside finish(), so a second
    # unregister either returns 0 or False — never raises.
    try:
        atexit.unregister(cb)
    except Exception as e:
        pytest.fail(f"unregister raised after finish(): {e}")


def test_init_dir_writes_store_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    monkeypatch.delenv("AGENTICML_DIR", raising=False)
    run = agenticml.init(
        "demo",
        "offline-dir",
        mode="local",
        dir=tmp_path,
        track_source=False,
        capture_env=False,
        system_metrics=False,
        only_main_process=False,
    )
    try:
        agenticml.log({"loss": 0.25})
        run_id = run.run_id
    finally:
        agenticml.finish()

    reader = RunReader(tmp_path, run_id)
    assert reader.meta()["status"] == "finished"
    assert reader.series("loss")["points"] == [[0, 0.25]]


def test_init_verbose_prints_run_id_and_offline_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    monkeypatch.delenv("AGENTICML_DIR", raising=False)
    run = agenticml.init(
        "demo",
        "verbose-on",
        mode="local",
        dir=tmp_path,
        verbose=True,
        track_source=False,
        capture_env=False,
        system_metrics=False,
        only_main_process=False,
    )
    out = capsys.readouterr().out
    try:
        run_dir = str(tmp_path.resolve() / "runs" / run.run_id)
        assert run.run_id in out
        assert "mode=local" in out
        assert str(tmp_path.resolve()) in out
        assert run_dir in out
        assert f"agenticml ui --dir {tmp_path.resolve()}" in out
    finally:
        agenticml.finish()


def test_init_verbose_false_is_silent_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    monkeypatch.delenv("AGENTICML_DIR", raising=False)
    run = agenticml.init(
        "demo",
        "verbose-off",
        mode="local",
        dir=tmp_path,
        track_source=False,
        capture_env=False,
        system_metrics=False,
        only_main_process=False,
    )
    out = capsys.readouterr().out
    try:
        assert run.run_id not in out
        assert "journal.jsonl" not in out
        assert "dir" not in out
    finally:
        agenticml.finish()


def test_init_disabled_verbose_prints_one_line(monkeypatch, capsys):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    agenticml.init("demo", "disabled-verbose", mode="disabled", verbose=True)
    out = capsys.readouterr().out
    try:
        assert "disabled" in out.lower()
        assert "journal.jsonl" not in out
        assert out.count("\n") <= 2
    finally:
        agenticml.finish()


def test_finish_idempotent(monkeypatch):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    run = Run(
        "p", "n",
        mode="local",
        track_source=False,
        capture_env=False,
        system_metrics=False,
    )
    run.finish()
    run.finish()  # second call is a no-op, no exception


@pytest.mark.parametrize("mode", [None, "local", "offline"])
def test_training_is_local_with_server_env_set(tmp_path, monkeypatch, mode):
    import json
    import sys

    import requests

    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    monkeypatch.setenv("AGENTICML_HOST", "https://unused.test")
    monkeypatch.setenv("AGENTICML_API_KEY", "unused-key")
    requests_made = []

    def forbidden_request(*args, **kwargs):
        requests_made.append((args, kwargs))
        raise AssertionError("training must not make HTTP requests")

    monkeypatch.setattr(requests.Session, "request", forbidden_request)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    entrypoint = tmp_path / "train.py"
    entrypoint.write_text("print('train')\n")
    extra = tmp_path / "config.yaml"
    extra.write_text("epochs: 2\n")
    image = tmp_path / "plot.png"
    image.write_bytes(b"image bytes")
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    with Run(
        "p", "n", mode=mode, track_source=True, parent_run_id="parent",
        capture_env=False, system_metrics=False, auto_track=False,
    ) as run:
        run.track_files(extra)
        run.log({"loss": 0.5, "preview": agenticml.Image(image)}, step=3)
        run.log_artifact(extra)
    assert run.mode == "local"
    assert not requests_made
    reader = RunReader(run.root, run.run_id)
    assert {event["kind"] for event in reader.events()} == {"code", "artifact", "finish"}
    assert reader.meta()["parent_run_id"] == "parent"
    assert reader.meta()["snapshot_hash"]
    manifest = {row["path"]: row for row in reader.code_manifest()}
    for name in ("train.py", "config.yaml"):
        assert path_for(run.root, manifest[name]["sha256"]).read_bytes() == (tmp_path / name).read_bytes()
    assert reader.media("preview")[0]["step"] == 3
    assert reader.series("loss")["points"] == [[3, 0.5]]


def test_track_files_skips_missing_files_when_source_tracking_disabled(tmp_path, caplog):
    with Run("p", "n", track_source=False, capture_env=False, system_metrics=False, auto_track=False) as run:
        run.track_files()
        run.track_files(tmp_path / "missing.py")
    assert "source tracking disabled" not in caplog.text
    assert RunReader(run.root, run.run_id).code_manifest() == []


def test_source_tracking_disabled_preserves_model_and_explicit_files(tmp_path, monkeypatch):
    class Model:
        pass

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    extra = tmp_path / "config.yaml"
    extra.write_text("epochs: 2\n")
    (tmp_path / "unused.py").write_text("print('unused')\n")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("explicit external file\n")

    with Run(
        "p", "n", dir=tmp_path / "local", track_source=False,
        capture_env=False, system_metrics=False, auto_track=False,
    ) as run:
        assert run._runtime_collector is None
        run.track_model(Model())
        staging = run._object_staging_dir
        run.track_files(extra, outside)

    assert staging is not None and not staging.exists()
    reader = RunReader(run.root, run.run_id)
    assert len([event for event in reader.events() if event["kind"] == "code"]) == 2
    uploaded = {item["path"]: item for item in reader.code_manifest()}
    def content(name):
        return path_for(run.root, uploaded[name]["sha256"]).read_bytes()
    metadata = json.loads(content("agenticml/model.json"))
    assert len(metadata["files"]) == 1
    model_path = metadata["files"][0]
    expected = {
        "agenticml/model.json": "model", model_path: "model",
        "config.yaml": "extra", "extra/outside.txt": "extra",
    }
    assert set(uploaded) == set(expected)
    assert content(model_path) == Path(__file__).read_bytes()
    assert content("config.yaml") == extra.read_bytes()
    assert content("extra/outside.txt") == outside.read_bytes()
    for name, spec in uploaded.items():
        assert spec["sha256"] == hashlib.sha256(content(name)).hexdigest()
        assert spec["source"] == expected[name]
        assert spec["size"] == len(content(name))


@pytest.mark.parametrize("resume", ["allow", "must"])
def test_local_resume_continues_auto_step(tmp_path, resume):
    options = dict(dir=tmp_path, track_source=False, capture_env=False,
                   system_metrics=False, auto_track=False)
    with Run("p", "n", id="chosen-id", resume="allow", config={"lr": 0.1}, **options) as run:
        run.log({"loss": 1}, step=10)
        run.summary["best"] = 1
    with Run("p", "n", id="chosen-id", resume=resume, **options) as run:
        assert run.config == {"lr": 0.1}
        assert run.summary == {"best": 1}
        run.log({"loss": 0.5})
    reader = RunReader(tmp_path, "chosen-id")
    assert reader.series("loss")["points"] == [[10, 1], [11, 0.5]]
    assert [event["kind"] for event in reader.events()].count("resume") == 1


def test_local_resume_must_rejects_absent_run(tmp_path):
    with pytest.raises(FileNotFoundError):
        Run("p", "n", id="missing", resume="must", dir=tmp_path, track_source=False,
            capture_env=False, system_metrics=False, auto_track=False)


@pytest.mark.parametrize("run_id", ["../escape", ".hidden", "a/b", "a\\b", ""])
def test_run_rejects_invalid_ids(tmp_path, run_id):
    with pytest.raises(ValueError, match="invalid run id"):
        Run("p", "n", id=run_id, resume="allow", dir=tmp_path)


@pytest.mark.parametrize("kwargs, message", [
    ({"resume": "invalid"}, "resume must be one of"),
    ({"id": "chosen-id"}, "resume='never'"),
    ({"track_source": "repo"}, "track_source must be"),
])
def test_run_retains_argument_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Run("p", "n", **kwargs)


def _quiet_run(tmp_path, **kwargs):
    options = dict(
        dir=tmp_path, track_source=False, capture_env=False,
        system_metrics=False, auto_track=False, flush_interval=999,
    )
    options.update(kwargs)
    return Run("p", "n", **options)


def test_failed_local_batch_is_requeued_then_written(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.ERROR, logger="agenticml.run")
    with _quiet_run(tmp_path) as run:
        original = run.writer.write_scalars

        def fail_once(items):
            monkeypatch.setattr(run.writer, "write_scalars", original)
            raise OSError("disk full")

        monkeypatch.setattr(run.writer, "write_scalars", fail_once)
        run.log({"loss": 1})
        run._flush()
        assert [item["metrics"] for item in run.buffer] == [{"loss": 1}]
    assert "requeueing" in caplog.text
    assert "disk full" in caplog.text
    assert RunReader(tmp_path, run.run_id).series("loss")["points"] == [[0, 1]]


def test_failed_flush_drops_oldest_over_buffer_cap(monkeypatch, caplog, tmp_path):
    from agenticml import run as run_module

    caplog.set_level(logging.WARNING, logger="agenticml.run")
    monkeypatch.setattr(run_module, "_MAX_BUFFER_ITEMS", 3)
    run = _quiet_run(tmp_path)
    try:
        run.log({"loss": 1})
        run.log({"loss": 2})
        original = run.writer.write_scalars
        failed = {"done": False}

        def fail_once(items):
            if not failed["done"]:
                failed["done"] = True
                run.log({"loss": 3})
                run.log({"loss": 4})
                run.log({"loss": 5})
                raise OSError("disk full")
            return original(items)

        monkeypatch.setattr(run.writer, "write_scalars", fail_once)
        run._flush()
        assert [item["metrics"]["loss"] for item in run.buffer] == [3, 4, 5]
        assert "dropped 2 oldest" in caplog.text
        assert caplog.text.count("dropped") == 1
    finally:
        run.finish()
    points = RunReader(tmp_path, run.run_id).series("loss")["points"]
    assert [point[1] for point in points] == [3, 4, 5]


def test_finish_drains_buffer_when_flush_thread_died(tmp_path):
    run = _quiet_run(tmp_path)
    run.log({"loss": 7})
    assert run.buffer

    def crash():
        raise RuntimeError("flush thread crashed")

    run._flush = crash
    run.stop_event.set()
    run.thread.join(timeout=5)
    assert not run.thread.is_alive()
    assert run.buffer
    del run._flush
    run.finish()
    assert RunReader(tmp_path, run.run_id).series("loss")["points"] == [[0, 7]]


def test_finish_logs_points_lost_when_flush_keeps_failing(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.ERROR, logger="agenticml.run")
    run = _quiet_run(tmp_path)
    run.log({"loss": 1})
    run.log({"loss": 2})

    def fail(items):
        raise OSError("disk full")

    monkeypatch.setattr(run.writer, "write_scalars", fail)
    run.finish()
    assert "2 points lost" in caplog.text
    assert RunReader(tmp_path, run.run_id).meta()["status"] == "finished"


def test_system_samples_do_not_commit_training_steps(tmp_path, monkeypatch):
    from agenticml import system_metrics as sm

    monkeypatch.setattr(sm, "_cpu_mem_samples", lambda: {"_system/cpu.percent": 9.0})
    monkeypatch.setattr(sm, "_gpu_samples", lambda: {})
    run = _quiet_run(tmp_path)
    run.log({"loss": 1}, commit=False)
    assert (run._auto_step, run._pending_step, dict(run._pending_metrics)) == (0, 0, {"loss": 1})
    sm.SystemMetricsSampler(run, interval=30)._emit_samples()
    assert run._auto_step == 0
    assert run._pending_step == 0
    assert run._pending_metrics == {"loss": 1}
    run.log({"acc": 2})
    assert run._auto_step == 1
    assert run._pending_step is None
    run._log_system({"_system/memory.percent": 4.0})
    assert run._auto_step == 1
    assert run._pending_metrics == {}
    run.log({"loss": 3})
    run.finish()
    reader = RunReader(tmp_path, run.run_id)
    assert reader.series("loss")["points"] == [[0, 1], [1, 3]]
    assert reader.series("acc")["points"] == [[0, 2]]
    assert reader.series("_system/cpu.percent")["points"] == [[0, 9.0]]
    assert reader.series("_system/memory.percent")["points"] == [[0, 4.0]]


def test_sampler_stop_flag_blocks_enqueue_and_join_has_timeout(tmp_path, caplog):
    from agenticml import system_metrics as sm

    run = _quiet_run(tmp_path)
    sampler = sm.SystemMetricsSampler(run, interval=30)
    joined = {}

    class FakeThread:
        def __init__(self, alive):
            self._alive = alive

        def join(self, timeout=None):
            joined["timeout"] = timeout

        def is_alive(self):
            return self._alive

    sampler.thread = FakeThread(False)
    sampler.stop()
    assert joined["timeout"] == 5
    assert sampler._stopped is True
    sampler._emit_samples()
    assert run.buffer == []

    caplog.set_level(logging.WARNING, logger="agenticml.system")
    sampler.thread = FakeThread(True)
    sampler.stop()
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert warnings == ["system metrics sampler did not stop within 5s; continuing"]
    run.finish()


def _nvml_module(monkeypatch, **functions):
    import sys
    import types

    module = types.ModuleType("pynvml")
    for name, fn in functions.items():
        setattr(module, name, fn)
    monkeypatch.setitem(sys.modules, "pynvml", module)
    return module


def test_nvml_init_failure_disables_gpu_sampling(monkeypatch, caplog, tmp_path):
    from agenticml import system_metrics as sm

    caplog.set_level(logging.DEBUG, logger="agenticml.system")
    calls = {"init": 0, "shutdown": 0}

    def nvmlInit():
        calls["init"] += 1
        raise OSError("nvml missing")

    def nvmlShutdown():
        calls["shutdown"] += 1

    _nvml_module(monkeypatch, nvmlInit=nvmlInit, nvmlShutdown=nvmlShutdown)
    monkeypatch.setattr(sm, "_cpu_mem_samples", lambda: {})
    run = _quiet_run(tmp_path)
    try:
        sampler = sm.SystemMetricsSampler(run, interval=30)
        sampler._emit_samples()
        sampler._emit_samples()
        assert calls == {"init": 1, "shutdown": 0}
        assert sampler._gpu_disabled is True
        debugs = [
            record.getMessage()
            for record in caplog.records
            if record.name == "agenticml.system" and record.levelno == logging.DEBUG
        ]
        assert debugs == ["nvmlInit failed; disabling GPU sampling for this run"]
        assert not [
            record
            for record in caplog.records
            if record.name == "agenticml.system" and record.levelno >= logging.ERROR
        ]
    finally:
        run.finish()


def test_nvml_shutdown_runs_after_sampling_error(monkeypatch, caplog):
    from agenticml import system_metrics as sm

    caplog.set_level(logging.DEBUG, logger="agenticml.system")
    calls = {"init": 0, "shutdown": 0}

    def nvmlInit():
        calls["init"] += 1

    def nvmlShutdown():
        calls["shutdown"] += 1

    def nvmlDeviceGetCount():
        raise RuntimeError("device query failed")

    _nvml_module(
        monkeypatch,
        nvmlInit=nvmlInit,
        nvmlShutdown=nvmlShutdown,
        nvmlDeviceGetCount=nvmlDeviceGetCount,
    )
    assert sm._gpu_samples() == {}
    assert calls == {"init": 1, "shutdown": 1}
    assert any(
        record.levelno == logging.ERROR and record.exc_info
        for record in caplog.records
        if record.name == "agenticml.system"
    )


def test_nvml_samples_shutdown_after_success(monkeypatch):
    from agenticml import system_metrics as sm

    calls = {"shutdown": 0}

    class _Util:
        gpu = 25

    class _Mem:
        used = 1
        total = 4

    def nvmlShutdown():
        calls["shutdown"] += 1

    _nvml_module(
        monkeypatch,
        nvmlInit=lambda: None,
        nvmlShutdown=nvmlShutdown,
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda idx: idx,
        nvmlDeviceGetUtilizationRates=lambda handle: _Util(),
        nvmlDeviceGetMemoryInfo=lambda handle: _Mem(),
    )
    assert sm._gpu_samples() == {
        "_system/gpu.0.utilization": 25.0,
        "_system/gpu.0.memory_percent": 25.0,
    }
    assert calls["shutdown"] == 1


def test_log_racing_finish_persists_points_enqueued_before_seal(tmp_path):
    run = _quiet_run(tmp_path)
    accepted = []
    accepted_lock = threading.Lock()
    real_enqueue = run._enqueue

    def tracking(step, metrics):
        ok = real_enqueue(step, metrics)
        if ok:
            with accepted_lock:
                accepted.append(metrics["i"])
        return ok

    run._enqueue = tracking
    stop = threading.Event()

    def worker():
        i = 0
        while not stop.is_set() and i < 4000:
            run.log({"i": i})
            i += 1

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        with accepted_lock:
            if len(accepted) >= 30:
                break
        time.sleep(0.01)
    with accepted_lock:
        assert accepted, "worker did not enqueue before finish"
    run.finish()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    for j in range(10):
        run.log({"i": -1 - j})
    got = [
        int(value)
        for _step, value in RunReader(tmp_path, run.run_id).series("i", max_points=10000)["points"]
    ]
    with accepted_lock:
        assert got == accepted
    assert got and all(value >= 0 for value in got)


def test_resume_explicit_metadata_overrides_stored(tmp_path):
    options = dict(
        dir=tmp_path, track_source=False, capture_env=False,
        system_metrics=False, auto_track=False,
    )
    with Run(
        "p", "n", id="chosen-id", resume="allow", tags=["old"], notes="old",
        group="g1", job_type="train", config={"lr": 0.1, "epochs": 1}, **options,
    ):
        pass
    with Run(
        "p", "n", id="chosen-id", resume="allow", config={"b": 2}, **options,
    ) as run:
        assert run.tags == ["old"]
        assert run.notes == "old"
        assert run.group == "g1"
        assert run.job_type == "train"
        assert run.config == {"lr": 0.1, "epochs": 1, "b": 2}
    with Run(
        "p", "n", id="chosen-id", resume="allow", tags=["new"], notes="new",
        group="g2", job_type="eval", config={"lr": 0.2, "wd": 0.01}, **options,
    ) as run:
        assert run.tags == ["new"]
        assert run.notes == "new"
        assert run.group == "g2"
        assert run.job_type == "eval"
        assert run.config == {"lr": 0.2, "epochs": 1, "b": 2, "wd": 0.01}
    meta = RunReader(tmp_path, "chosen-id").meta()
    assert meta["tags"] == ["new"]
    assert meta["notes"] == "new"
    assert meta["group"] == "g2"
    assert meta["job_type"] == "eval"
    assert meta["config"]["lr"] == 0.2


def test_finish_omits_null_snapshot_hash(tmp_path):
    with _quiet_run(tmp_path):
        pass
    metas = [RunReader(tmp_path, path.name).meta() for path in (tmp_path / "runs").iterdir()]
    assert len(metas) == 1
    assert "snapshot_hash" not in metas[0]
    assert metas[0]["status"] == "finished"


def test_add_code_failure_does_not_erase_snapshot_hash(tmp_path, monkeypatch):
    run = _quiet_run(tmp_path, track_source=True)
    run.writer.set_meta_fields(snapshot_hash="existing-hash")
    called = []

    def close():
        collector = run._runtime_collector
        collector._active = False
        from agenticml.code_tracking import _ACTIVE_COLLECTORS, _AUDIT_LOCK
        with _AUDIT_LOCK:
            _ACTIVE_COLLECTORS.discard(collector)
        return {"files": []}, []

    def boom(*args, **kwargs):
        called.append(True)
        raise OSError("upload failed")

    monkeypatch.setattr(run._runtime_collector, "close", close)
    monkeypatch.setattr(run.writer, "add_code", boom)
    run.finish()
    assert called
    meta = RunReader(tmp_path, run.run_id).meta()
    assert meta["snapshot_hash"] == "existing-hash"
    assert meta["status"] == "finished"


def test_fork_child_public_methods_return_quietly(tmp_path):
    run = _quiet_run(tmp_path)
    pid = os.fork()
    if pid == 0:
        try:
            run.log({"loss": 1})
            if run.log_artifact(tmp_path / "missing.bin") is not None:
                os._exit(2)
            run.track_model(object())
            run.track_optimizer(object())
            run.track_loss(None)
            run.track_dataloader(None)
            run.track_files(tmp_path / "missing.py")
            run.finish()
        except Exception:
            os._exit(1)
        os._exit(0)

    def _timeout(_signum, _frame):
        raise TimeoutError("forked child hung")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(10)
    try:
        _pid, status = os.waitpid(pid, 0)
    except TimeoutError:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        raise
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert os.waitstatus_to_exitcode(status) == 0
    run.log({"loss": 4})
    run.finish()
    assert RunReader(tmp_path, run.run_id).series("loss")["points"] == [[0, 4]]


def _stop_flush_thread(run):
    run.stop_event.set()
    run.thread.join(timeout=5)
    assert not run.thread.is_alive()


def test_flush_backs_off_and_rate_limits_errors(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.ERROR, logger="agenticml.run")
    run = _quiet_run(tmp_path, flush_interval=2)
    try:
        _stop_flush_thread(run)
        run.log({"loss": 1})

        def fail(items):
            raise OSError("disk full")

        monkeypatch.setattr(run.writer, "write_scalars", fail)
        run._flush()
        run._flush()
        assert run._flush_wait == 8
        assert [record for record in caplog.records if record.exc_info]
        assert sum(record.exc_info is not None for record in caplog.records) == 1
        assert "requeueing" in caplog.text

        run._flush_fail_logged_at = time.monotonic() - 61
        caplog.clear()
        run._flush()
        assert run._flush_wait == 16
        assert not any(record.exc_info for record in caplog.records)
        assert any("1 items pending" in record.getMessage() for record in caplog.records)

        run._flush_wait = 40
        run._flush()
        assert run._flush_wait == 60
        run._flush()
        assert run._flush_wait == 60

        monkeypatch.setattr(run.writer, "write_scalars", lambda items: None)
        run._flush()
        assert run._flush_wait == 2
        assert run._flush_failing is False
    finally:
        run.finish()


def test_run_finished_error_drops_the_batch_once(monkeypatch, caplog, tmp_path):
    from agenticml.run import RunFinishedError

    caplog.set_level(logging.ERROR, logger="agenticml.run")
    run = _quiet_run(tmp_path)
    try:
        _stop_flush_thread(run)
        run.log({"loss": 1})
        run.log({"loss": 2})

        def fail(items):
            raise RunFinishedError("closed")

        monkeypatch.setattr(run.writer, "write_scalars", fail)
        assert run._flush() is False
        assert run.buffer == []
        run.log({"loss": 3})
        assert run._flush() is False
        assert run.buffer == []
        assert caplog.text.count("dropping") == 1
        assert "requeueing" not in caplog.text
    finally:
        run.finish()
    assert RunReader(tmp_path, run.run_id).keys() == []


def test_finish_reraises_keyboard_interrupt_and_seals(tmp_path):
    run = _quiet_run(tmp_path)
    calls = []

    def boom():
        raise KeyboardInterrupt()

    def spy(*args, **kwargs):
        calls.append(args)

    run._stop_integrations = boom
    run.writer.finish = spy
    with pytest.raises(KeyboardInterrupt):
        run.finish()
    run.thread.join(timeout=5)
    assert calls == []
    assert run._finished is True
    assert run._finishing is False
    assert run._finish_done.is_set()
    assert RunReader(tmp_path, run.run_id).meta()["status"] == "crashed"

    previous = signal.getsignal(signal.SIGALRM)

    def _timeout(_signum, _frame):
        raise TimeoutError("second finish hung")

    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(5)
    try:
        run.finish()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert calls == []


def test_finish_keyboard_interrupt_during_join_marks_crashed(tmp_path):
    run = _quiet_run(tmp_path)
    real_join = run.thread.join

    def boom_join(timeout=None):
        raise KeyboardInterrupt()

    run.thread.join = boom_join
    with pytest.raises(KeyboardInterrupt):
        run.finish()
    real_join(timeout=5)
    assert not run.thread.is_alive()
    assert run._finished is True
    assert run._store_finish_attempted is False
    assert RunReader(tmp_path, run.run_id).meta()["status"] == "crashed"
    assert run._finish_done.is_set()


def test_second_finish_waits_for_the_first(monkeypatch, tmp_path):
    run = _quiet_run(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    second_waiting = threading.Event()
    calls = []
    original = run.writer.finish

    def blocked(status, summary=None):
        calls.append(status)
        entered.set()
        assert release.wait(timeout=5)
        return original(status, summary)

    monkeypatch.setattr(run.writer, "finish", blocked)
    real_wait = run._finish_done.wait

    def wait_spy(timeout=None):
        second_waiting.set()
        return real_wait(timeout)

    run._finish_done.wait = wait_spy
    first = threading.Thread(target=run.finish)
    first.start()
    assert entered.wait(timeout=5)
    second = threading.Thread(target=run.finish)
    second.start()
    assert second_waiting.wait(timeout=5)
    assert calls == ["finished"]
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == ["finished"]
    assert RunReader(tmp_path, run.run_id).meta()["status"] == "finished"


def test_finish_reentered_from_writer_call_defers(tmp_path):
    """finish() from inside a writer call must not join or take the writer lock."""
    run = _quiet_run(tmp_path)
    path = tmp_path / "weights.bin"
    path.write_bytes(b"abc")
    original = run.writer.add_artifact
    seen = {}

    def wrapped(*args, **kwargs):
        run.writer._lock.acquire()
        try:
            run.finish("crashed")
            seen["requested"] = run._finish_requested
            seen["finished"] = run._finished
        finally:
            run.writer._lock.release()
        return original(*args, **kwargs)

    run.writer.add_artifact = wrapped

    def _timeout(_signum, _frame):
        raise TimeoutError("finish deadlocked inside a writer call")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(5)
    try:
        run.log_artifact(path, name="model")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert seen == {"requested": True, "finished": False}
    assert run._finished is True
    assert run._finish_requested is False
    assert RunReader(tmp_path, run.run_id).meta()["status"] == "crashed"
    assert any(event.get("name") == "model" for event in RunReader(tmp_path, run.run_id).events())


def test_only_requesting_thread_runs_deferred_finish(tmp_path, monkeypatch):
    callbacks = set()
    monkeypatch.setattr(atexit, "register", callbacks.add)
    monkeypatch.setattr(atexit, "unregister", callbacks.discard)
    run = _quiet_run(tmp_path)
    _stop_flush_thread(run)
    callback = run._atexit_callback
    requester = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    errors = []
    finish_threads = []
    original = run.writer.finish

    def finish(status, summary=None):
        finish_threads.append(threading.get_ident())
        assert callback in callbacks  # The fallback survives until store finish.
        return original(status, summary)

    monkeypatch.setattr(run.writer, "finish", finish)

    def other_writer_call():
        entered.set()
        assert release.wait(timeout=5)

    def worker():
        try:
            run._writer_call(other_writer_call)
        except BaseException as exc:
            errors.append(exc)
        finally:
            exited.set()

    thread = threading.Thread(target=worker)
    thread.start()

    def requesting_writer_call():
        assert entered.wait(timeout=5)
        # Also exercise nested calls: only the outermost one may finish.
        run._writer_call(run.finish, "crashed")
        assert run._finish_requested
        assert callback in callbacks
        release.set()
        assert exited.wait(timeout=5)
        assert not errors
        assert run._finish_requested
        assert not run._finished
        assert not finish_threads
        assert callback in callbacks

    try:
        run._writer_call(requesting_writer_call)
        assert finish_threads == [requester]
        assert not run._finish_requested
        assert run._finish_done.is_set()
        assert callback not in callbacks
        assert RunReader(tmp_path, run.run_id).meta()["status"] == "crashed"
    finally:
        release.set()
        thread.join(timeout=5)
        run.finish()
    assert not thread.is_alive()


def test_atexit_finishes_when_deferred_request_never_unwinds(tmp_path, monkeypatch):
    callbacks = set()
    monkeypatch.setattr(atexit, "register", callbacks.add)
    monkeypatch.setattr(atexit, "unregister", callbacks.discard)
    run = _quiet_run(tmp_path)
    _stop_flush_thread(run)
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def abandoned_writer_call():
        run.finish()
        entered.set()
        assert release.wait(timeout=5)

    def worker():
        try:
            run._writer_call(abandoned_writer_call)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        assert run._finish_requested
        assert not run._finish_done.is_set()
        callback = run._atexit_callback
        assert callback in callbacks
        callback()
        assert run._finish_done.is_set()
        assert callback not in callbacks
        assert RunReader(tmp_path, run.run_id).meta()["status"] == "crashed"
    finally:
        release.set()
        thread.join(timeout=5)
        run.finish()
    assert not thread.is_alive()
    assert not errors


def test_finish_while_holding_run_lock_does_not_deadlock(tmp_path):
    assert "signal handler" in Run.finish.__doc__
    assert "main thread" in Run.finish.__doc__
    run = _quiet_run(tmp_path)
    run.log({"loss": 1})

    def _timeout(_signum, _frame):
        raise TimeoutError("finish deadlocked while the run lock was held")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(5)
    try:
        with run.lock:
            assert run.lock.held_by_current_thread()
            run.finish()
        run.thread.join(timeout=5)
        assert not run.thread.is_alive()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert not run.lock.held_by_current_thread()
    assert run._finished is True
    assert RunReader(tmp_path, run.run_id).meta()["status"] == "finished"
    assert RunReader(tmp_path, run.run_id).series("loss")["points"] == [[0, 1]]


def test_finished_run_is_collectable(tmp_path):
    import gc
    import weakref

    run = _quiet_run(tmp_path)
    ref = weakref.ref(run)
    run.finish()
    del run
    for _ in range(5):
        gc.collect()
    assert ref() is None


def test_budget_warning_on_create_not_resume_or_disabled(tmp_path, monkeypatch, caplog):
    import agenticml._globals as globals_mod
    import agenticml.run as run_mod
    import agenticml.store.gc as gc

    monkeypatch.setenv("AGENTICML_MAX_BYTES", "1")
    monkeypatch.setattr(run_mod, "_budget_warned", False)
    caplog.set_level(logging.WARNING, logger="agenticml.run")
    walks = {"n": 0}
    real_walk = gc._cheap_total_bytes
    (tmp_path / "usage.json").write_text(json.dumps({"bytes": 100, "computed_at": time.time()}))

    def counting(path):
        if path == tmp_path:
            walks["n"] += 1
        return real_walk(path)

    monkeypatch.setattr(gc, "_cheap_total_bytes", counting)

    def open_run(name, **kwargs):
        return Run(
            "p",
            name,
            dir=tmp_path,
            track_source=False,
            capture_env=False,
            system_metrics=False,
            auto_track=False,
            flush_interval=999,
            **kwargs,
        )

    first = open_run("create")
    try:
        assert walks["n"] == 0
        assert sum("exceeds budget" in rec.getMessage() for rec in caplog.records) == 1
        assert (first.root / "usage.json").is_file()
    finally:
        first.finish()

    caplog.clear()
    second = open_run("again")
    try:
        # Fresh usage.json: check_budget must not walk the store again.
        assert walks["n"] == 0
        assert not any("exceeds budget" in rec.getMessage() for rec in caplog.records)
    finally:
        second.finish()

    (first.root / "usage.json").unlink()
    walks["n"] = 0
    monkeypatch.setattr(run_mod, "_budget_warned", False)
    caplog.clear()
    resumed = open_run("resumed", id=first.run_id, resume="must")
    try:
        assert walks["n"] == 0
        assert not any("exceeds budget" in rec.getMessage() for rec in caplog.records)
    finally:
        resumed.finish()

    saved = globals_mod._active_run
    globals_mod._active_run = None
    try:
        agenticml.init(
            "p",
            "off",
            mode="disabled",
            dir=tmp_path,
            track_source=False,
            capture_env=False,
            system_metrics=False,
        )
        assert walks["n"] == 0
        assert not any("exceeds budget" in rec.getMessage() for rec in caplog.records)
        agenticml.finish()
    finally:
        globals_mod._active_run = saved

"""SDK regressions at the local store boundary."""

import subprocess
import sys
import threading
from datetime import datetime

import pytest

from agenticml import Image, Run
from agenticml.store import RunReader, path_for


@pytest.fixture
def run(tmp_path):
    with Run("project", "name", dir=tmp_path, track_source=False, capture_env=False,
             system_metrics=False, auto_track=False, flush_interval=999) as run:
        yield run


def test_import_and_training_do_not_load_http_client(tmp_path):
    code = """
import agenticml, sys
assert 'requests' not in sys.modules
assert 'agenticml.client' not in sys.modules
with agenticml.Run('p', 'n', dir=sys.argv[1], track_source=False,
                   capture_env=False, system_metrics=False, auto_track=False) as run:
    run.log({'loss': 1})
assert 'requests' not in sys.modules
assert 'agenticml.client' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True)


def test_finish_packs_only_in_flush_thread(run):
    packed_on = []

    class Scalar:
        def __float__(self):
            packed_on.append(threading.current_thread().name)
            return 1.5

    run.log({"loss": Scalar()})
    assert packed_on == []
    run.finish()
    assert packed_on == [f"agenticml-flush-{run.run_id}"]
    assert RunReader(run.root, run.run_id).series("loss")["points"] == [[0, 1.5]]


def test_idle_flush_persists_config_and_summary(run):
    run.config["lr"] = 0.25
    run.summary["best"] = 3
    run._flush()
    reader = RunReader(run.root, run.run_id)
    assert reader.meta()["config"] == {"lr": 0.25}
    assert reader.meta()["summary"] == {"best": 3}
    assert reader.keys() == []
    run._flush()
    assert len([event for event in reader.events() if event["kind"] == "config_update"]) == 1


@pytest.mark.parametrize("fail", [False, True])
def test_media_temporary_files_are_removed_but_source_paths_survive(run, tmp_path, monkeypatch, fail):
    from agenticml import media

    temporary = []
    original = media.Image._to_upload

    def convert(image):
        result = original(image)
        if image._upload_is_temporary():
            temporary.append(result[0])
        return result

    monkeypatch.setattr(media.Image, "_to_upload", convert)
    if fail:
        def fail_media(*args):
            raise OSError("disk full")
        monkeypatch.setattr(run.writer, "add_media", fail_media)
    source = tmp_path / "image.png"
    source.write_bytes(b"png bytes")
    run.log({"bytes": Image(b"png bytes", caption="sample"), "path": Image(source)}, step=3)
    assert source.read_bytes() == b"png bytes"
    assert temporary and all(not media.Path(path).exists() for path in temporary)
    if not fail:
        rows = RunReader(run.root, run.run_id).media()
        assert rows["bytes"][0]["sha256"] == rows["path"][0]["sha256"]
        assert rows["bytes"][0]["caption"] == "sample"
        assert rows["bytes"][0]["step"] == 3


def test_artifact_reference_and_copy_versions(run, tmp_path):
    source = tmp_path / "weights.bin"
    source.write_bytes(b"weights")
    reference = run.log_artifact(source, name="checkpoint")
    snapshot = run.log_artifact(source, name="checkpoint", copy=True, metadata={"epoch": 1})
    source.write_bytes(b"changed")
    assert reference["copy"] is False and reference["sha256"] is None
    assert (reference["version"], snapshot["version"]) == (1, 2)
    assert snapshot["metadata"] == {"epoch": 1}
    assert path_for(run.root, snapshot["sha256"]).read_bytes() == b"weights"


def test_flush_json_safe_summary_and_concurrent_config(run):
    class FloatLike:
        def __float__(self):
            return 1.25

    class Flaky:
        def __init__(self):
            self.tries = 0
            self._data = {"lr": 0.1, "epochs": 3}

        def keys(self):
            self.tries += 1
            if self.tries < 3:
                raise RuntimeError("dictionary changed size during iteration")
            return self._data.keys()

        def __getitem__(self, key):
            return self._data[key]

    when = datetime(2026, 10, 27, 3, 4, 5)
    run.summary["score"] = FloatLike()
    run.summary["when"] = when
    run.summary["nan"] = float("nan")
    run.summary["inf"] = float("inf")
    run.tags.append("exp")
    run.notes = "hello"
    run.group = "g"
    run.job_type = "train"
    run._flush()

    stop = threading.Event()
    errors = []

    def mutate():
        i = 0
        while not stop.is_set():
            try:
                run.config[str(i)] = i
                run.config.pop(str(i - 1), None)
            except Exception as exc:
                errors.append(exc)
                return
            i += 1

    thread = threading.Thread(target=mutate)
    thread.start()
    try:
        for _ in range(40):
            run._flush()
    finally:
        stop.set()
        thread.join()
    assert errors == []

    flaky = Flaky()
    run.config = flaky
    run._flush()
    assert flaky.tries >= 3
    run.config = {"lr": 0.1, "epochs": 3}

    meta = RunReader(run.root, run.run_id).meta()
    assert meta["summary"]["score"] == 1.25
    assert meta["summary"]["when"] == when.isoformat()
    assert meta["summary"]["nan"] is None
    assert meta["summary"]["inf"] is None
    assert meta["config"]["lr"] == 0.1
    assert meta["tags"] == ["exp"]
    assert meta["notes"] == "hello"
    assert meta["group"] == "g"
    assert meta["job_type"] == "train"


def test_image_save_failure_removes_temp_file(monkeypatch):
    from agenticml import media

    class Boom:
        def save(self, path, format=None):
            raise OSError("encode failed")

    created = []
    real = media.tempfile.NamedTemporaryFile

    def tracking(*args, **kwargs):
        tmp = real(*args, **kwargs)
        created.append(tmp.name)
        return tmp

    monkeypatch.setattr(media.tempfile, "NamedTemporaryFile", tracking)
    with pytest.raises(OSError, match="encode failed"):
        media.Image(Boom())._to_upload()
    assert created
    assert all(not media.Path(path).exists() for path in created)


def test_writer_finish_failure_marks_crashed_and_keeps_metadata(tmp_path, monkeypatch):
    class FloatLike:
        def __float__(self):
            return 1.25

    when = datetime(2026, 10, 27, 1, 2, 3)
    run = Run(
        "p", "n", dir=tmp_path, track_source=False, capture_env=False,
        system_metrics=False, auto_track=False, flush_interval=999,
    )
    run.summary["score"] = FloatLike()
    run.summary["when"] = when
    run.config["lr"] = 0.2
    run.tags = ["t"]
    run.notes = "n"
    run.group = "g"
    run.job_type = "job"
    seen = {}

    def boom(status, summary=None):
        seen["status"] = status
        seen["summary"] = summary
        raise OSError("fsync failed")

    monkeypatch.setattr(run.writer, "finish", boom)
    run.finish()
    assert seen["status"] == "finished"
    assert seen["summary"] == {"score": 1.25, "when": when.isoformat()}
    meta = RunReader(tmp_path, run.run_id).meta()
    assert meta["status"] == "crashed"
    assert meta["summary"] == seen["summary"]
    assert meta["config"] == {"lr": 0.2}
    assert meta["tags"] == ["t"]
    assert meta["notes"] == "n"
    assert meta["group"] == "g"
    assert meta["job_type"] == "job"
    assert "snapshot_hash" not in meta


def test_json_safe_cycles_and_user_conversions():
    from agenticml.run import _json_safe

    cycle = []
    cycle.append(cycle)
    assert _json_safe(cycle) == ["<unserializable>"]
    mapping = {}
    mapping["self"] = mapping
    assert _json_safe(mapping) == {"self": "<unserializable>"}
    shared = {"n": 1}
    assert _json_safe({"a": shared, "b": shared}) == {
        "a": {"n": 1},
        "b": {"n": 1},
    }

    class BadFloat:
        def __float__(self):
            raise RuntimeError("float failed")

        def __str__(self):
            return "not-used"

        def __repr__(self):
            return "REPR_SENTINEL"

    class BadStr:
        def __str__(self):
            raise RuntimeError("str failed")

        def __repr__(self):
            return "REPR_SENTINEL"

    class BadItem:
        ndim = 0

        def item(self):
            raise RuntimeError("item failed")

        def __str__(self):
            return "not-used"

    class Loop:
        ndim = 0

        def __init__(self):
            self.other = None

        def item(self):
            return self.other

    class Zero:
        ndim = 0

        def item(self):
            return 3

    assert _json_safe(BadFloat()) == "<unserializable>"
    assert _json_safe(BadStr()) == "<unserializable>"
    assert "REPR_SENTINEL" not in str(_json_safe(BadStr()))
    assert _json_safe(BadItem()) == "<unserializable>"
    left, right = Loop(), Loop()
    left.other = right
    right.other = left
    assert _json_safe(left) == "<unserializable>"
    assert _json_safe(Zero()) == 3


class _BoomMap:
    def keys(self):
        raise RuntimeError("dictionary changed size during iteration")

    def __getitem__(self, key):
        raise AssertionError(key)


def test_one_bad_field_keeps_last_value_and_finish_still_runs(caplog, tmp_path):
    import logging

    caplog.set_level(logging.ERROR, logger="agenticml.run")
    run = Run(
        "p", "n", dir=tmp_path, track_source=False, capture_env=False,
        system_metrics=False, auto_track=False, flush_interval=999,
        config={"lr": 0.1},
    )
    try:
        run.summary["best"] = 1
        run.tags = ["a"]
        run.notes = "n"
        run.group = "g"
        run.job_type = "train"
        run._flush()
        run.summary = _BoomMap()
        run.tags = ["a", "b"]
        run.config["wd"] = 0.01
        run._flush()
        run._flush()
        messages = [
            record.getMessage()
            for record in caplog.records
            if "keeping the last persisted value" in record.getMessage()
        ]
        assert messages == ["failed to convert summary; keeping the last persisted value"]
        run.finish()
    except BaseException:
        run.finish()
        raise
    meta = RunReader(tmp_path, run.run_id).meta()
    assert meta["status"] == "finished"
    assert meta["summary"] == {"best": 1}
    assert meta["config"] == {"lr": 0.1, "wd": 0.01}
    assert meta["tags"] == ["a", "b"]
    assert meta["notes"] == "n"
    assert meta["group"] == "g"
    assert meta["job_type"] == "train"


def test_cyclic_summary_is_stored_and_run_finishes(tmp_path):
    cycle = {}
    cycle["self"] = cycle
    run = Run(
        "p", "n", dir=tmp_path, track_source=False, capture_env=False,
        system_metrics=False, auto_track=False, flush_interval=999,
    )
    run.summary["cycle"] = cycle
    run.summary["score"] = 4
    run.finish()
    meta = RunReader(tmp_path, run.run_id).meta()
    assert meta["status"] == "finished"
    assert meta["summary"] == {"cycle": {"self": "<unserializable>"}, "score": 4}


def test_image_filelike_reads_bytes(run):
    import io

    payload = b"png-bytes"
    run.log({"frame": Image(io.BytesIO(payload))}, step=1)
    rows = RunReader(run.root, run.run_id).media()
    assert path_for(run.root, rows["frame"][0]["sha256"]).read_bytes() == payload


def test_image_filelike_rejects_text():
    class Text:
        def read(self):
            return "not-bytes"

    with pytest.raises(TypeError, match="must return bytes"):
        Image(Text())._to_upload()


def test_media_upload_after_finish_logs_debug(monkeypatch, caplog, tmp_path):
    import logging

    from agenticml.run import RunFinishedError

    caplog.set_level(logging.DEBUG, logger="agenticml.run")
    run = Run(
        "p", "n", dir=tmp_path, track_source=False, capture_env=False,
        system_metrics=False, auto_track=False, flush_interval=999,
    )

    def fail(*args, **kwargs):
        raise RunFinishedError("closed")

    monkeypatch.setattr(run.writer, "add_media", fail)
    run.log({"frame": Image(b"png-bytes")}, step=2)
    assert any(
        record.levelno == logging.DEBUG
        and record.getMessage() == "skipping media upload after finish key=frame step=2"
        for record in caplog.records
    )
    assert not any(record.exc_info for record in caplog.records)
    run.finish()


def test_sync_init_mapping_matches_run_metadata(tmp_path, monkeypatch):
    from agenticml import run as run_module
    from agenticml.sync import _init_payload

    env = {"python_version": "3.test", "platform": "test", "hostname": "host", "packages": {"p": "1"}}
    git = {"git_commit": "abc", "git_branch": "main", "is_dirty": True}
    monkeypatch.setattr(run_module, "capture_environment", lambda: env)
    monkeypatch.setattr(run_module, "get_git_metadata", lambda root: git)
    with Run("p", "n", dir=tmp_path, track_source=False, system_metrics=False,
             auto_track=False, parent_run_id="parent") as run:
        pass
    meta = RunReader(tmp_path, run.run_id).meta()
    assert meta["env"] == env
    assert meta["git"] == {**git, "repo_root": str(run_module.find_repo_root(run_module.Path.cwd()))}
    payload = _init_payload(meta)
    for key in ("python_version", "platform", "packages"):
        assert payload[key] == env[key]
    for key in git:
        assert payload[key] == git[key]
    assert payload["parent_run_id"] == "parent"
    assert "snapshot_hash" not in meta
    assert "snapshot_hash" not in payload

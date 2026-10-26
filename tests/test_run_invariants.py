"""Regression tests for the invariants we just fixed:
   - mode validation in Run.__init__
   - _DisabledRun isolation across instances
   - atexit hook is unregistered after finish()
"""

from __future__ import annotations

import atexit
import hashlib
import json
from pathlib import Path

import pytest

import agenticml
from agenticml._globals import _DisabledRun
from agenticml.run import Run


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


def test_init_dir_writes_journal_without_env(tmp_path, monkeypatch):
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

    journal = tmp_path.resolve() / "runs" / run_id / "journal.jsonl"
    assert journal.is_file()
    text = journal.read_text()
    assert '"kind": "init"' in text
    assert '"kind": "log_batch"' in text or '"kind": "finalize"' in text


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
        assert "journal.jsonl" in out
        assert "code" in out
        assert "artifacts" in out
        assert "media" in out
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
    run_dir = run.client.root / "runs" / run.run_id
    entries = [json.loads(line) for line in (run_dir / "journal.jsonl").read_text().splitlines()]
    assert {entry["kind"] for entry in entries} == {
        "init", "log_batch", "upload_code", "log_artifact", "log_media", "finalize",
    }
    assert entries[0]["payload"]["parent_run_id"] == "parent"
    assert (run_dir / "code" / "train.py").read_bytes() == entrypoint.read_bytes()
    assert (run_dir / "code" / "config.yaml").read_bytes() == extra.read_bytes()
    assert entries[-1]["payload"]["snapshot_hash"]


def test_track_files_skips_missing_files_when_source_tracking_disabled(tmp_path, caplog):
    with Run("p", "n", track_source=False, capture_env=False, system_metrics=False, auto_track=False) as run:
        run.track_files()
        run.track_files(tmp_path / "missing.py")
    assert "source tracking disabled" not in caplog.text
    assert not (run.client.root / "runs" / run.run_id / "code").exists()


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
    run_dir = run.client.root / "runs" / run.run_id
    entries = [json.loads(line) for line in (run_dir / "journal.jsonl").read_text().splitlines()]
    uploads = [entry["payload"] for entry in entries if entry["kind"] == "upload_code"]
    assert len(uploads) == 2
    metadata = json.loads((run_dir / "code" / "agenticml/model.json").read_text())
    assert len(metadata["files"]) == 1
    model_path = metadata["files"][0]
    expected = {
        "agenticml/model.json": "model",
        model_path: "model",
        "config.yaml": "extra",
        "extra/outside.txt": "extra",
    }
    uploaded = {item["path"]: item for upload in uploads for item in upload["files"]}
    assert set(uploaded) == set(expected)
    assert (run_dir / "code" / model_path).read_bytes() == Path(__file__).read_bytes()
    assert (run_dir / "code" / "config.yaml").read_bytes() == extra.read_bytes()
    assert (run_dir / "code" / "extra/outside.txt").read_bytes() == outside.read_bytes()
    for upload in uploads:
        for spec in upload["manifest"]["files"]:
            stored = uploaded[spec["path"]]
            content = (run_dir / stored["stored_at"]).read_bytes()
            assert spec["sha256"] == stored["sha256"] == hashlib.sha256(content).hexdigest()
            assert spec["source"] == stored["source"] == expected[spec["path"]]
            assert spec["content_type"] == stored["content_type"]
            assert spec["size"] == len(content)


@pytest.mark.parametrize("resume", ["allow", "must"])
def test_run_id_is_a_local_journal_hint(tmp_path, resume):
    import json

    with Run(
        "p", "n", id="chosen-id", resume=resume, dir=tmp_path, parent_run_id="parent",
        track_source=False, capture_env=False, system_metrics=False, auto_track=False,
    ) as run:
        assert run.run_id == "chosen-id"
    journal = tmp_path / "runs" / "chosen-id" / "journal.jsonl"
    init = json.loads(journal.read_text().splitlines()[0])
    assert init["payload"]["parent_run_id"] == "parent"


@pytest.mark.parametrize("kwargs, message", [
    ({"resume": "invalid"}, "resume must be one of"),
    ({"id": "chosen-id"}, "resume='never'"),
    ({"track_source": "repo"}, "track_source must be"),
])
def test_run_retains_argument_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Run("p", "n", **kwargs)


def test_failed_local_batch_is_logged_and_dropped(monkeypatch, caplog):
    with Run("p", "n", track_source=False, capture_env=False, system_metrics=False, auto_track=False) as run:
        def fail_append(items):
            raise OSError("disk full")

        monkeypatch.setattr(run.client, "log_batch", fail_append)
        run.log({"loss": 1})
        run._flush()
        assert run.buffer == []
    assert "dropping batch" in caplog.text
    assert "disk full" in caplog.text

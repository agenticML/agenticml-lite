"""Smoke tests covering the public surface of the agenticml SDK."""

import subprocess
import sys

import agenticml
import pytest


def test_version_set():
    assert agenticml.__version__


def test_public_surface():
    expected = {
        "Image",
        "Run",
        "config",
        "finish",
        "get_active_run",
        "get_rank",
        "init",
        "is_main_process",
        "log",
        "log_artifact",
        "summary",
        "track_files",
        "track_loss",
        "track_model",
        "track_optimizer",
        "track_dataloader",
    }
    assert expected.issubset(set(agenticml.__all__))
    assert "Client" not in agenticml.__all__
    assert agenticml.Client.__name__ == "Client"


def test_star_import_does_not_import_requests():
    code = """
import sys
ns = {}
exec("from agenticml import *", ns)
assert "Client" not in ns
assert "requests" not in sys.modules
assert "agenticml.client" not in sys.modules
assert "Run" in ns and "log" in ns
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_client_constructs(monkeypatch):
    monkeypatch.delenv("AGENTICML_HOST", raising=False)
    monkeypatch.delenv("AGENTICML_API_KEY", raising=False)
    c = agenticml.Client(api_key="test")
    assert c.api_key == "test"
    assert c.base_url == "https://api.agenticml.xyz"


def test_client_strips_trailing_slash():
    c = agenticml.Client(base_url="https://api.agenticml.xyz/")
    assert c.base_url == "https://api.agenticml.xyz"


def test_client_reads_env(monkeypatch):
    monkeypatch.setenv("AGENTICML_HOST", "https://example.com")
    monkeypatch.setenv("AGENTICML_API_KEY", "envkey")
    c = agenticml.Client()
    assert c.base_url == "https://example.com"
    assert c.api_key == "envkey"


def test_disabled_mode_runs_full_lifecycle(monkeypatch):
    monkeypatch.setenv("AGENTICML_MODE", "disabled")
    run = agenticml.init("demo", "smoke")
    assert type(run).__name__ == "_DisabledRun"

    agenticml.log({"loss": 0.5})
    agenticml.log({"acc": 0.9}, step=10)
    agenticml.log({"partial": 1.0}, commit=False)
    agenticml.config["lr"] = 0.01
    agenticml.summary["final"] = "ok"
    agenticml.track_model(object())
    agenticml.track_optimizer(object())
    agenticml.track_loss(object())
    agenticml.track_dataloader(object())
    agenticml.finish()


def test_distributed_helpers(monkeypatch):
    for var in ("RANK", "LOCAL_RANK", "SLURM_PROCID", "PMI_RANK", "OMPI_COMM_WORLD_RANK"):
        monkeypatch.delenv(var, raising=False)
    assert agenticml.get_rank() == 0
    assert agenticml.is_main_process()

    monkeypatch.setenv("RANK", "3")
    assert agenticml.get_rank() == 3
    assert not agenticml.is_main_process()


def test_ignore_matcher():
    from agenticml.ignore import IgnoreMatcher

    m = IgnoreMatcher(["*.log", "!keep.log", "data/", "__pycache__/"])
    assert m.matches("foo.log")
    assert not m.matches("keep.log")
    assert m.matches("data", is_dir=True)
    assert m.matches("a/b/data", is_dir=True)
    assert m.matches("src/__pycache__", is_dir=True)
    assert not m.matches("src/foo.py")


def test_env_capture_returns_basics():
    from agenticml.env import capture_environment

    env = capture_environment()
    assert set(env.keys()) == {"python_version", "platform", "hostname", "packages"}
    assert env["python_version"]
    assert isinstance(env["packages"], dict)



def test_local_run_persists_scalars_config_summary_and_artifact(tmp_path):
    from agenticml.store import RunReader, valid_run_id

    run = agenticml.init("demo", "local", dir=tmp_path, track_source=False,
                         capture_env=False, system_metrics=False, auto_track=False)
    assert valid_run_id(run.run_id)
    agenticml.log({"loss": 0.5})
    agenticml.log({"acc": 0.9}, step=5)
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(b"weights")
    record = agenticml.log_artifact(artifact, name="model", type="model")
    agenticml.config["lr"] = 0.01
    agenticml.summary["final"] = 1
    agenticml.finish()
    reader = RunReader(tmp_path, run.run_id)
    assert reader.series("loss")["points"] == [[0, 0.5]]
    assert reader.series("acc")["points"][0][1] == pytest.approx(0.9)
    assert reader.meta()["config"] == {"lr": 0.01}
    assert reader.meta()["summary"] == {"final": 1}
    assert reader.meta()["status"] == "finished"
    assert reader.artifacts()[0]["path"] == str(artifact)
    assert record["copy"] is False and record["sha256"] is None


def test_run_auto_step_and_commit_merging(tmp_path):
    from agenticml import Run
    from agenticml.store import RunReader

    with Run("p", "n", dir=tmp_path, track_source=False, capture_env=False,
             system_metrics=False, auto_track=False, flush_interval=999) as run:
        run.log({"a": 1})
        run.log({"b": 2}, commit=False)
        run.log({"c": 3})
        run.log({"d": 4}, step=10)
        run.log({"e": 5}, step=12, commit=False)
        run.summary["done"] = True
    reader = RunReader(tmp_path, run.run_id)
    assert {key: reader.series(key)["points"] for key in reader.keys()} == {
        "a": [[0, 1]], "b": [[1, 2]], "c": [[1, 3]], "d": [[10, 4]], "e": [[12, 5]],
    }
    assert reader.meta()["summary"] == {"done": True}


def test_runtime_source_persists_content_and_snapshot(tmp_path, monkeypatch):
    from agenticml import Run
    from agenticml.store import RunReader, path_for

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'runtime-demo'\n")
    entrypoint = tmp_path / "train.py"
    entrypoint.write_text("print('train')\n")
    config = tmp_path / "config.yaml"
    config.write_text("epochs: 2\n")
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])
    with Run("p", "n", dir=tmp_path / "store", capture_env=False,
             system_metrics=False, auto_track=False) as run:
        config.read_text()
    reader = RunReader(run.root, run.run_id)
    manifest = {row["path"]: row for row in reader.code_manifest()}
    assert manifest.keys() >= {"train.py", "config.yaml"}
    for name in ("train.py", "config.yaml"):
        assert path_for(run.root, manifest[name]["sha256"]).read_bytes() == (tmp_path / name).read_bytes()
    assert reader.meta()["snapshot_hash"]

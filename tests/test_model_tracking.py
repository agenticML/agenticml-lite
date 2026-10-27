"""Tests for track_model: unwrap, MRO, staging, metadata, and venv isolation."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from agenticml.code_tracking import RuntimeFileCollector
from agenticml.model_tracking import (
    MODEL_METADATA_PATH,
    inspect_model,
    stage_tracked_model,
    unwrap_model,
)
from agenticml.run import Run
from agenticml.store import RunReader, RunWriter


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "train.py").write_text("print('train')\n")
    return tmp_path


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_arch_module(site_root: Path) -> tuple[Path, object]:
    pkg = site_root / "fakesarch"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    arch = pkg / "modeling.py"
    arch.write_text(
        "class FakeArch:\n"
        "    def __init__(self):\n"
        "        self.hidden = 4\n"
    )
    module = _load_module("fakesarch.modeling", arch)
    return arch, module.FakeArch()


def test_unwrap_ddp_like_module():
    class Inner:
        marker = "inner"

    class FakeDDP:
        def __init__(self, inner):
            self.module = inner

    inner = Inner()
    assert unwrap_model(FakeDDP(inner)) is inner


def test_unwrap_compile_and_peft():
    class Inner:
        marker = "inner"

    class Compiled:
        def __init__(self, inner):
            self._orig_mod = inner

    class PeftLike:
        def __init__(self, inner):
            self._inner = inner

        def get_base_model(self):
            return self._inner

    class FakeDDP:
        def __init__(self, inner):
            self.module = inner

    inner = Inner()
    assert unwrap_model(PeftLike(Compiled(inner))) is inner
    assert unwrap_model(Compiled(inner)) is inner
    assert unwrap_model(FakeDDP(PeftLike(Compiled(inner)))) is inner


def test_mro_skips_torch_nn_module():
    class Module:
        pass

    Module.__module__ = "torch.nn.modules.module"

    class Linear(Module):
        pass

    Linear.__module__ = "torch.nn.modules.linear"

    class MyArch(Linear):
        pass

    MyArch.__module__ = "myproj.models.arch"

    inspection = inspect_model(MyArch())
    assert inspection.metadata["mro"] == ["MyArch"]
    assert "Linear" not in inspection.metadata["mro"]
    assert "Module" not in inspection.metadata["mro"]


def test_inspect_copies_fields_and_optional_hf_config(tmp_path: Path):
    arch, model = _write_arch_module(tmp_path / "lib" / "site-packages")
    try:

        class Cfg:
            def to_dict(self):
                return {
                    "hidden_size": 32,
                    "vocab": {str(i): i for i in range(2000)},
                    "id2label": {"0": "a"},
                }

        model.config = Cfg()
        inspection = inspect_model(model)
        meta = inspection.metadata
        assert meta["qualname"] == "FakeArch"
        assert meta["module"] == "fakesarch.modeling"
        assert meta["package"] == "fakesarch"
        assert meta["mro"] == ["FakeArch"]
        assert any(path.endswith("fakesarch/modeling.py") for path in meta["files"])
        assert meta["config"]["hidden_size"] == 32
        assert "vocab" not in meta["config"]
        assert "error" not in meta
        assert inspection.source_files[0][0] == arch.resolve()
    finally:
        sys.modules.pop("fakesarch.modeling", None)


def test_missing_source_records_error_and_does_not_raise():
    import itertools

    inspection = inspect_model(itertools.chain())
    assert "error" in inspection.metadata
    assert inspection.source_files == []


def test_missing_source_stages_metadata_json(tmp_path: Path):
    import itertools

    staged = stage_tracked_model(itertools.chain(), tmp_path / "stage")
    by_manifest = {manifest: path for path, manifest in staged}
    assert MODEL_METADATA_PATH in by_manifest
    payload = json.loads(by_manifest[MODEL_METADATA_PATH].read_text())
    assert payload["error"]
    assert payload["files"] == []
    assert payload["qualname"] == "chain"


def test_add_explicit_does_not_lift_venv_skip(fake_repo: Path):
    site_module = fake_repo / "vendor" / "lib" / "site-packages" / "dependency.py"
    site_module.parent.mkdir(parents=True)
    site_module.write_text("VALUE = 1\n")

    collector = RuntimeFileCollector(entrypoint=fake_repo / "src" / "train.py")
    collector.add_explicit(
        site_module,
        manifest_path="model/dependency.py",
        source="model",
    )
    _manifest, files = collector.close()
    paths = {item.relative_path for item in files}
    assert "model/dependency.py" not in paths
    assert not any("site-packages" in path for path in paths)


def test_collector_skips_env_import_but_records_staged_copy(fake_repo: Path, tmp_path: Path):
    site_root = fake_repo / "vendor" / "lib" / "site-packages"
    arch_path, model = _write_arch_module(site_root)
    try:
        collector = RuntimeFileCollector(entrypoint=fake_repo / "src" / "train.py")
        staged_dir = tmp_path / "staged-model"
        staged = stage_tracked_model(model, staged_dir)
        for path, manifest_path in staged:
            collector.add_explicit(path, manifest_path=manifest_path, source="model")
        manifest, files = collector.close()
    finally:
        sys.modules.pop("fakesarch.modeling", None)

    by_path = {item.relative_path: item for item in files}
    assert "model/fakesarch/modeling.py" in by_path
    assert by_path["model/fakesarch/modeling.py"].source == "model"
    assert MODEL_METADATA_PATH in by_path
    assert not any("site-packages" in path for path in by_path)
    payload = json.loads(by_path[MODEL_METADATA_PATH].absolute_path.read_text())
    assert payload["qualname"] == "FakeArch"
    assert payload["files"] == ["model/fakesarch/modeling.py"]
    assert arch_path.read_text() == by_path["model/fakesarch/modeling.py"].absolute_path.read_text()
    assert manifest["tracking_mode"] == "runtime"
    # original env path must not appear as a tracked absolute path
    abs_paths = {item.absolute_path for item in files}
    assert arch_path.resolve() not in abs_paths


def test_track_model_runtime_run_uploads_staged_files(fake_repo: Path, monkeypatch):
    entrypoint = fake_repo / "src" / "train.py"
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    site_root = fake_repo / "vendor" / "lib" / "site-packages"
    _arch, model = _write_arch_module(site_root)
    try:
        run = Run(
            "p",
            "n",
            capture_env=False,
            system_metrics=False,
            flush_interval=999,
        )
        staging = None
        run.track_model(model)
        staging = run._object_staging_dir
        assert staging is not None and staging.is_dir()
        run.finish()
        assert staging is not None and not staging.exists()
    finally:
        sys.modules.pop("fakesarch.modeling", None)

    manifest = RunReader(run.root, run.run_id).code_manifest()
    paths = {item["path"] for item in manifest}
    assert "model/fakesarch/modeling.py" in paths
    assert MODEL_METADATA_PATH in paths
    uploaded = {item["path"] for item in manifest}
    assert "model/fakesarch/modeling.py" in uploaded
    assert MODEL_METADATA_PATH in uploaded
    assert not any("site-packages" in path for path in paths)


def test_stage_dir_removed_even_if_upload_fails(fake_repo: Path, monkeypatch):
    def fail_code(self, manifest, files):
        raise RuntimeError("store failed")
    monkeypatch.setattr(RunWriter, "add_code", fail_code)
    entrypoint = fake_repo / "src" / "train.py"
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    site_root = fake_repo / "vendor" / "lib" / "site-packages"
    _arch, model = _write_arch_module(site_root)
    try:
        run = Run(
            "p",
            "n",
            capture_env=False,
            system_metrics=False,
            flush_interval=999,
        )
        run.track_model(model)
        staging = run._object_staging_dir
        run.finish()
        assert staging is not None and not staging.exists()
    finally:
        sys.modules.pop("fakesarch.modeling", None)

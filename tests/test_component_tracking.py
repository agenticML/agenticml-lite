"""Tests for track_optimizer / track_loss / track_dataloader."""

from __future__ import annotations

import functools
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from agenticml.code_tracking import RuntimeFileCollector
from agenticml.component_tracking import (
    inspect_dataloader,
    inspect_loss,
    inspect_optimizer,
    sanitize_component_name,
    stage_tracked_dataloader,
    stage_tracked_loss,
    stage_tracked_optimizer,
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


class _FakeTensor:
    def __init__(self, n: int, values=None):
        self.dtype = "fake-float32"
        self.shape = (n,)
        self._secret = values if values is not None else list(range(n))

    def numel(self):
        return int(self.shape[0])


class _FakeOptimizer:
    def __init__(self, params, lr=0.01, betas=(0.9, 0.999)):
        self.defaults = {"lr": lr, "betas": betas}
        self.param_groups = [
            {"params": list(params), "lr": lr, "betas": betas},
        ]


def _write_custom_optimizer(site_root: Path):
    pkg = site_root / "fakeopt"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    path = pkg / "optim.py"
    path.write_text(
        "class CustomAdam:\n"
        "    def __init__(self, params, lr=0.002):\n"
        "        self.defaults = {'lr': lr, 'weight_decay': 0.0}\n"
        "        self.param_groups = [{'params': list(params), 'lr': lr}]\n"
    )
    module = _load_module("fakeopt.optim", path)
    return path, module.CustomAdam


def _write_custom_loss(site_root: Path):
    pkg = site_root / "fakeloss"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    path = pkg / "losses.py"
    path.write_text(
        "class FocalLoss:\n"
        "    def __init__(self, gamma=2.0, reduction='mean', weight=None):\n"
        "        self.gamma = gamma\n"
        "        self.reduction = reduction\n"
        "        self.weight = weight\n"
        "        self._secret_tensor = [1.0, 2.0, 3.0]\n"
        "    def forward(self, pred, target):\n"
        "        return 0.0\n"
        "    def __call__(self, pred, target):\n"
        "        return self.forward(pred, target)\n"
        "\n"
        "def pairwise_loss(pred, target, reduction='mean'):\n"
        "    return 0.0\n"
    )
    module = _load_module("fakeloss.losses", path)
    return path, module


def _write_custom_dataset(site_root: Path):
    pkg = site_root / "fakedata"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    path = pkg / "dataset.py"
    path.write_text(
        "class SpeechClips:\n"
        "    def __init__(self):\n"
        "        self.root = None\n"
        "        self.samples = []\n"
        "    def __len__(self):\n"
        "        return 2\n"
        "    def __getitem__(self, idx):\n"
        "        return idx\n"
        "\n"
        "def pad_collate(batch):\n"
        "    return batch\n"
    )
    module = _load_module("fakedata.dataset", path)
    return path, module


class _FakeLoader:
    def __init__(self, dataset, batch_size=8, shuffle=False, collate_fn=None):
        self.dataset = dataset
        self.batch_size = batch_size
        self.num_workers = 0
        self.drop_last = False
        self.pin_memory = False
        self.persistent_workers = False
        self.prefetch_factor = 2
        self.timeout = 0
        self.shuffle = shuffle
        self.collate_fn = collate_fn
        self.sampler = _SequentialSampler() if not shuffle else _RandomSampler()
        self.batch_sampler = None


class _SequentialSampler:
    pass


class _RandomSampler:
    pass


def test_sanitize_component_name_blocks_path_escape():
    assert sanitize_component_name("train") == "train"
    assert sanitize_component_name("train/../etc") == "train_etc"
    assert sanitize_component_name("../secret") == "secret"
    assert sanitize_component_name("") == "default"
    assert sanitize_component_name(None) == "default"


def test_optimizer_defaults_captured_params_absent():
    opt = _FakeOptimizer([_FakeTensor(4, values=[9, 8, 7, 6]), _FakeTensor(2)], lr=0.003)
    meta = inspect_optimizer(opt).metadata
    assert meta["defaults"]["lr"] == 0.003
    assert meta["defaults"]["betas"] == [0.9, 0.999]
    assert meta["num_param_groups"] == 1
    assert meta["num_params"] == 6
    assert meta["param_groups"][0]["num_tensors"] == 2
    assert meta["param_groups"][0]["num_params"] == 6
    dumped = json.dumps(meta)
    assert "params" not in meta["param_groups"][0]
    assert "[9, 8, 7, 6]" not in dumped
    assert "_secret" not in dumped


def test_custom_optimizer_py_is_staged(tmp_path: Path):
    path, cls = _write_custom_optimizer(tmp_path / "lib" / "site-packages")
    try:
        opt = cls([_FakeTensor(3)], lr=0.002)
        staged = stage_tracked_optimizer(opt, tmp_path / "stage")
    finally:
        sys.modules.pop("fakeopt.optim", None)

    by_manifest = {manifest: dest for dest, manifest in staged}
    assert "agenticml/optimizer.json" in by_manifest
    assert any(path.endswith("fakeopt/optim.py") for path in by_manifest)
    payload = json.loads(by_manifest["agenticml/optimizer.json"].read_text())
    assert payload["qualname"] == "CustomAdam"
    assert payload["defaults"]["lr"] == 0.002
    copied = [dest for dest, manifest in staged if manifest.endswith("optim.py")][0]
    assert copied.read_text() == path.read_text()


def test_torch_optim_like_is_metadata_only(tmp_path: Path):
    class Optimizer:
        pass

    Optimizer.__module__ = "torch.optim.optimizer"

    class Adam(Optimizer):
        def __init__(self, params, lr=0.001):
            self.defaults = {"lr": lr}
            self.param_groups = [{"params": list(params), "lr": lr}]

    Adam.__module__ = "torch.optim.adam"
    opt = Adam([_FakeTensor(1)], lr=0.001)
    staged = stage_tracked_optimizer(opt, tmp_path / "stage")
    by_manifest = {manifest: dest for dest, manifest in staged}
    assert list(by_manifest) == ["agenticml/optimizer.json"]
    payload = json.loads(by_manifest["agenticml/optimizer.json"].read_text())
    assert payload["module"] == "torch.optim.adam"
    assert payload["package"] == "torch"
    assert payload["files"] == []
    assert "error" not in payload


def test_loss_module_attrs_and_tensor_weight(tmp_path: Path):
    _path, module = _write_custom_loss(tmp_path / "lib" / "site-packages")
    try:
        loss = module.FocalLoss(gamma=1.5, reduction="sum", weight=_FakeTensor(4))
        meta = inspect_loss(loss).metadata
    finally:
        sys.modules.pop("fakeloss.losses", None)

    assert meta["kind"] == "module"
    assert meta["attrs"]["gamma"] == 1.5
    assert meta["attrs"]["reduction"] == "sum"
    assert meta["attrs"]["weight"]["shape"] == [4]
    assert meta["attrs"]["weight"]["numel"] == 4
    dumped = json.dumps(meta)
    assert "[1.0, 2.0, 3.0]" not in dumped
    assert "_secret_tensor" not in dumped


def test_functional_and_partial_loss_identity(tmp_path: Path):
    _path, module = _write_custom_loss(tmp_path / "lib" / "site-packages")
    try:
        fn_meta = inspect_loss(module.pairwise_loss).metadata
        partial_meta = inspect_loss(
            functools.partial(module.pairwise_loss, reduction="none")
        ).metadata
        staged = stage_tracked_loss(
            functools.partial(module.pairwise_loss, reduction="none"),
            tmp_path / "stage",
        )
    finally:
        sys.modules.pop("fakeloss.losses", None)

    assert fn_meta["kind"] == "function"
    assert fn_meta["qualname"] == "pairwise_loss"
    assert partial_meta["kind"] == "partial"
    assert partial_meta["wrapped"]["qualname"] == "pairwise_loss"
    assert "none" in json.dumps(partial_meta["keywords"])
    by_manifest = {manifest: dest for dest, manifest in staged}
    assert "agenticml/loss.json" in by_manifest
    assert any("fakeloss/losses.py" in path for path in by_manifest)


def test_torch_loss_like_is_metadata_only(tmp_path: Path):
    class CrossEntropyLoss:
        def __init__(self, reduction="mean", ignore_index=-100, label_smoothing=0.0):
            self.reduction = reduction
            self.ignore_index = ignore_index
            self.label_smoothing = label_smoothing

        def forward(self, pred, target):
            return 0.0

    CrossEntropyLoss.__module__ = "torch.nn.modules.loss"
    loss = CrossEntropyLoss(reduction="sum", ignore_index=255)
    staged = stage_tracked_loss(loss, tmp_path / "stage")
    payload = json.loads(
        {manifest: dest for dest, manifest in staged}["agenticml/loss.json"].read_text()
    )
    assert payload["kind"] == "module"
    assert payload["package"] == "torch"
    assert payload["files"] == []
    assert "error" not in payload
    assert payload["attrs"]["reduction"] == "sum"
    assert payload["attrs"]["ignore_index"] == 255


def test_dataloader_batch_size_and_dataset_class(tmp_path: Path):
    _path, module = _write_custom_dataset(tmp_path / "lib" / "site-packages")
    try:
        dataset = module.SpeechClips()
        dataset.root = "/secret/data/wavs"
        dataset.samples = ["/secret/data/wavs/a.wav", "/secret/data/wavs/b.wav"]
        loader = _FakeLoader(dataset, batch_size=16, shuffle=True)
        meta = inspect_dataloader(loader).metadata
    finally:
        sys.modules.pop("fakedata.dataset", None)

    assert meta["batch_size"] == 16
    assert meta["shuffle"] is True
    assert meta["dataset"]["qualname"] == "SpeechClips"
    assert meta["dataset"]["length"] == 2
    dumped = json.dumps(meta)
    assert "/secret/data" not in dumped
    assert ".wav" not in dumped
    assert "samples" not in dumped


def test_two_dataloader_names_do_not_overwrite(tmp_path: Path):
    class Tiny:
        def __len__(self):
            return 1

    staged = []
    staged.extend(stage_tracked_dataloader(_FakeLoader(Tiny(), batch_size=4), tmp_path / "stage", name="train"))
    staged.extend(stage_tracked_dataloader(_FakeLoader(Tiny(), batch_size=2), tmp_path / "stage", name="val"))
    by_manifest = {manifest: dest for dest, manifest in staged}
    assert "agenticml/dataloader.train.json" in by_manifest
    assert "agenticml/dataloader.val.json" in by_manifest
    train = json.loads(by_manifest["agenticml/dataloader.train.json"].read_text())
    val = json.loads(by_manifest["agenticml/dataloader.val.json"].read_text())
    assert train["batch_size"] == 4
    assert val["batch_size"] == 2
    assert train["name"] == "train"
    assert val["name"] == "val"


def test_dataset_passed_directly_records_metadata(tmp_path: Path):
    _path, module = _write_custom_dataset(tmp_path / "lib" / "site-packages")
    try:
        dataset = module.SpeechClips()
        dataset.root = "/secret/data/wavs"
        dataset.samples = ["/secret/data/wavs/a.wav"]
        meta = inspect_dataloader(dataset).metadata
        staged = stage_tracked_dataloader(dataset, tmp_path / "stage")
    finally:
        sys.modules.pop("fakedata.dataset", None)

    assert meta["dataset"]["qualname"] == "SpeechClips"
    assert meta["dataset"]["length"] == 2
    assert "batch_size" not in meta
    dumped = json.dumps(meta)
    assert "/secret" not in dumped
    by_manifest = {manifest: dest for dest, manifest in staged}
    assert any("fakedata/dataset.py" in path for path in by_manifest)


def test_runtime_collector_records_component_source(fake_repo: Path, tmp_path: Path):
    site_root = fake_repo / "vendor" / "lib" / "site-packages"
    opt_path, opt_cls = _write_custom_optimizer(site_root)
    loss_path, loss_mod = _write_custom_loss(site_root)
    data_path, data_mod = _write_custom_dataset(site_root)
    try:
        collector = RuntimeFileCollector(entrypoint=fake_repo / "src" / "train.py")
        staged_dir = tmp_path / "staged"
        dataset = data_mod.SpeechClips()
        dataset.root = "/secret/data/wavs"
        dataset.samples = ["/secret/data/wavs/a.wav"]
        for path, manifest, source in (
            *[(p, m, "optimizer") for p, m in stage_tracked_optimizer(opt_cls([_FakeTensor(1)]), staged_dir)],
            *[(p, m, "loss") for p, m in stage_tracked_loss(loss_mod.FocalLoss(), staged_dir)],
            *[
                (p, m, "dataloader")
                for p, m in stage_tracked_dataloader(
                    _FakeLoader(dataset, collate_fn=data_mod.pad_collate),
                    staged_dir,
                    name="train",
                )
            ],
        ):
            collector.add_explicit(path, manifest_path=manifest, source=source)
        _manifest, files = collector.close()
    finally:
        sys.modules.pop("fakeopt.optim", None)
        sys.modules.pop("fakeloss.losses", None)
        sys.modules.pop("fakedata.dataset", None)

    by_path = {item.relative_path: item for item in files}
    assert by_path["agenticml/optimizer.json"].source == "optimizer"
    assert by_path["agenticml/loss.json"].source == "loss"
    assert by_path["agenticml/dataloader.train.json"].source == "dataloader"
    assert any(path.startswith("optimizer/") and path.endswith("optim.py") for path in by_path)
    assert any(path.startswith("loss/") for path in by_path)
    assert any(path.startswith("dataloader/") for path in by_path)
    snapshot_text = "\n".join(item.absolute_path.read_text() for item in files)
    assert "/secret/data" not in snapshot_text
    assert opt_path.resolve() not in {item.absolute_path for item in files}
    assert loss_path.resolve() not in {item.absolute_path for item in files}
    assert data_path.resolve() not in {item.absolute_path for item in files}


def test_track_optimizer_runtime_run_uploads(fake_repo: Path, monkeypatch):
    entrypoint = fake_repo / "src" / "train.py"
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    site_root = fake_repo / "vendor" / "lib" / "site-packages"
    _path, cls = _write_custom_optimizer(site_root)
    try:
        run = Run(
            "p",
            "n",
            capture_env=False,
            system_metrics=False,
            flush_interval=999,
        )
        run.track_optimizer(cls([_FakeTensor(2)]), name="gen")
        staging = run._object_staging_dir
        assert staging is not None and staging.is_dir()
        run.finish()
        assert staging is not None and not staging.exists()
    finally:
        sys.modules.pop("fakeopt.optim", None)

    manifest = RunReader(run.root, run.run_id).code_manifest()
    paths = {item["path"] for item in manifest}
    assert "agenticml/optimizer.gen.json" in paths
    assert any(path.startswith("optimizer/") for path in paths)
    uploaded = {item["path"] for item in manifest}
    assert "agenticml/optimizer.gen.json" in uploaded
    sources = {item["source"] for item in manifest if item["path"] == "agenticml/optimizer.gen.json"}
    assert sources == {"optimizer"}


def test_stage_dir_removed_even_if_upload_fails(fake_repo: Path, monkeypatch):
    def fail_code(self, manifest, files):
        raise RuntimeError("store failed")
    monkeypatch.setattr(RunWriter, "add_code", fail_code)
    entrypoint = fake_repo / "src" / "train.py"
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    site_root = fake_repo / "vendor" / "lib" / "site-packages"
    _path, cls = _write_custom_optimizer(site_root)
    try:
        run = Run(
            "p",
            "n",
            capture_env=False,
            system_metrics=False,
            flush_interval=999,
        )
        run.track_optimizer(cls([_FakeTensor(1)]))
        staging = run._object_staging_dir
        run.finish()
        assert staging is not None and not staging.exists()
    finally:
        sys.modules.pop("fakeopt.optim", None)


def test_ignored_after_finish(fake_repo: Path, monkeypatch):
    entrypoint = fake_repo / "src" / "train.py"
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])
    run = Run(
        "p",
        "n",
        capture_env=False,
        system_metrics=False,
        flush_interval=999,
        track_source=False,
    )
    run.finish()
    run.track_optimizer(_FakeOptimizer([_FakeTensor(1)]))
    run.track_loss(lambda pred, target: 0)
    run.track_dataloader(_FakeLoader(object()))


def test_inspect_never_raises_on_garbage():
    inspect_optimizer(object())
    inspect_loss(object())
    inspect_dataloader(object())

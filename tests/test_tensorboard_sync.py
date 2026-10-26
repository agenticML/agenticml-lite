"""Tests for mirroring TensorBoard SummaryWriter calls into a run."""

from __future__ import annotations

import sys
import types

import pytest

from agenticml.integrations import tensorboard as tb
from agenticml.media import Image


class _FakeRun:
    def __init__(self):
        import threading

        self.logged: list[tuple[dict, int | None, bool]] = []
        self.config: dict = {}
        self.summary: dict = {}
        self._finished = False
        self._state_lock = threading.RLock()
        self._pending_metrics: dict = {}
        self.commits = 0

    def log(self, metrics, step=None, commit=True):
        self.logged.append((metrics, step, commit))

    def _commit_pending(self):
        self.commits += 1


def _fake_writer_module(name: str):
    module = types.ModuleType(name)

    class SummaryWriter:
        def __init__(self, log_dir=None):
            self.log_dir = log_dir or "runs/default"
            self.events: list = []

        def add_scalar(self, tag, scalar_value, global_step=None, walltime=None):
            self.events.append(("scalar", tag, scalar_value, global_step))

        def add_scalars(self, main_tag, tag_scalar_dict, global_step=None, walltime=None):
            self.events.append(("scalars", main_tag))

        def add_hparams(self, hparam_dict, metric_dict, hparam_domain_discrete=None, run_name=None):
            inner = SummaryWriter("inner")
            for key, value in metric_dict.items():
                inner.add_scalar(key, value)
            self.events.append(("hparams",))

        def add_image(self, tag, img_tensor, global_step=None, walltime=None, dataformats="CHW"):
            self.events.append(("image", tag))

        def add_images(self, tag, img_tensor, global_step=None, walltime=None, dataformats="NCHW"):
            self.events.append(("images", tag))

        def add_figure(self, tag, figure, global_step=None, close=True, walltime=None):
            self.events.append(("figure", tag))

        def flush(self):
            self.events.append(("flush",))

        def close(self):
            self.events.append(("close",))

    module.SummaryWriter = SummaryWriter
    return module


@pytest.fixture
def synced(monkeypatch):
    run = _FakeRun()
    module = _fake_writer_module("torch.utils.tensorboard.writer")
    monkeypatch.setitem(sys.modules, "torch.utils.tensorboard.writer", module)
    tb.install_tensorboard_sync(run)
    yield run, module.SummaryWriter
    tb.uninstall_tensorboard_sync(run)


def test_scalars_mirror_with_step_and_keep_writing(synced):
    run, SummaryWriter = synced
    writer = SummaryWriter("runs/a")
    writer.add_scalar("train/loss", 0.5, 3)
    writer.add_scalar("val/acc", value := 0.9, global_step=4)
    writer.add_scalars("lr", {"g0": 0.1, "g1": 0.2}, 5)
    assert run.logged == [
        ({"train/loss": 0.5}, 3, False),
        ({"val/acc": value}, 4, False),
        ({"lr/g0": 0.1, "lr/g1": 0.2}, 5, False),
    ]
    assert ("scalar", "train/loss", 0.5, 3) in writer.events
    assert run.config["_tensorboard/log_dir"] == "runs/a"


def test_scalar_without_step_uses_run_step(synced):
    run, SummaryWriter = synced
    SummaryWriter().add_scalar("x", 1)
    assert run.logged == [({"x": 1.0}, None, False)]


def test_hparams_go_to_config_and_summary_once(synced):
    run, SummaryWriter = synced
    writer = SummaryWriter()
    writer.add_hparams({"lr": 0.1, "opt": "adam"}, {"hparam/acc": 0.8})
    assert run.config["lr"] == 0.1 and run.config["opt"] == "adam"
    assert run.summary == {"hparam/acc": 0.8}
    # The inner writer's add_scalar must not be mirrored a second time.
    assert run.logged == []
    assert "inner" not in run.config.values()


def test_flush_and_close_commit_pending(synced):
    run, SummaryWriter = synced
    run._pending_metrics = {"x": 1.0}
    writer = SummaryWriter()
    writer.flush()
    writer.close()
    assert run.commits == 2


def test_mirror_errors_never_reach_user_code(synced):
    run, SummaryWriter = synced
    SummaryWriter().add_scalar("bad", object(), 1)
    assert run.logged == []


def test_finished_run_is_not_mirrored(synced):
    run, SummaryWriter = synced
    run._finished = True
    SummaryWriter().add_scalar("x", 1.0, 0)
    assert run.logged == []


def test_uninstall_restores_methods(monkeypatch):
    run = _FakeRun()
    module = _fake_writer_module("tensorboardX.writer")
    original = module.SummaryWriter.add_scalar
    monkeypatch.setitem(sys.modules, "tensorboardX.writer", module)
    tb.install_tensorboard_sync(run)
    assert module.SummaryWriter.add_scalar is not original
    tb.uninstall_tensorboard_sync(run)
    assert module.SummaryWriter.add_scalar is original


def test_real_summary_writer(tmp_path):
    pytest.importorskip("tensorboard")
    torch = pytest.importorskip("torch")
    run = _FakeRun()
    tb.install_tensorboard_sync(run)
    try:
        from torch.utils.tensorboard import SummaryWriter

        writer = SummaryWriter(str(tmp_path / "tb"))
        writer.add_scalar("loss", torch.tensor(0.25), 7)
        writer.add_hparams({"lr": 0.1}, {"hparam/acc": 0.5})
        writer.add_image("sample", torch.rand(3, 4, 4), 8)
        writer.add_images("batch", torch.rand(2, 3, 4, 4), 8)
        writer.close()
    finally:
        tb.uninstall_tensorboard_sync(run)
    assert ({"loss": 0.25}, 7, False) in run.logged
    media = {key: (value, step) for metrics, step, _ in run.logged for key, value in metrics.items()}
    if _have_pillow():
        assert isinstance(media["sample"][0], Image) and media["sample"][1] == 8
        assert isinstance(media["batch"][0], Image)
        path, content_type, _ = media["sample"][0]._to_upload()
        assert content_type == "image/png"
    assert run.config["lr"] == 0.1
    assert run.summary["hparam/acc"] == 0.5
    assert any(p.name.startswith("events.out.tfevents") for p in (tmp_path / "tb").rglob("*"))


def test_real_figure(tmp_path):
    pytest.importorskip("tensorboard")
    pytest.importorskip("torch")
    matplotlib = pytest.importorskip("matplotlib")
    if not _have_pillow():
        pytest.skip("Pillow not installed")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    run = _FakeRun()
    tb.install_tensorboard_sync(run)
    try:
        from torch.utils.tensorboard import SummaryWriter

        writer = SummaryWriter(str(tmp_path / "tb"))
        figure = plt.figure()
        plt.plot([1, 2, 3])
        writer.add_figure("curve", figure, 1)  # closes the figure
        writer.close()
    finally:
        tb.uninstall_tensorboard_sync(run)
    ((metrics, step, _),) = [entry for entry in run.logged if "curve" in entry[0]]
    assert step == 1 and isinstance(metrics["curve"], Image)


def _have_pillow() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True

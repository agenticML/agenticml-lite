"""End-to-end tests for automatic PyTorch component detection."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

from agenticml.auto_tracking import force_uninstall
from agenticml.run import Run
from agenticml.store import RunReader, path_for


@pytest.fixture(autouse=True)
def _restore_torch_hooks():
    force_uninstall()
    yield
    force_uninstall()


@pytest.fixture
def tracked_run(tmp_path: Path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    source = tmp_path / "src"
    source.mkdir()
    (source / "train.py").write_text("print('train')\n")
    monkeypatch.setattr(sys, "argv", [str(source / "train.py")])
    runs = []

    def make(**kwargs):
        defaults = dict(
            capture_env=False,
            system_metrics=False,
            flush_interval=999,
        )
        defaults.update(kwargs)
        run = Run("p", "n", **defaults)
        runs.append(run)
        return run

    return make, runs


def _metadata(runs) -> dict[str, dict]:
    found = {}
    for run in runs:
        reader = RunReader(run.root, run.run_id)
        for item in reader.code_manifest():
            path = item["path"]
            if path.startswith("agenticml/") and path.endswith(".json"):
                found[path] = json.loads(path_for(run.root, item["sha256"]).read_bytes())
    return found


class Net(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.fc = torch.nn.Linear(4, 2)

    def forward(self, features):
        return self.fc(features)


class PairLoss(torch.nn.Module):
    def forward(self, pred, target):
        return (pred - target).pow(2).mean()


def compute_loss(pred, target):
    return (pred - target).pow(2).mean()


class _Tiny(torch.utils.data.Dataset):
    def __len__(self):
        return 4

    def __getitem__(self, idx):
        return torch.randn(4), torch.tensor(idx % 2)


def _train_batch(net, opt, loss):
    features = torch.randn(2, 4)
    target = torch.randn(2, 2)
    opt.zero_grad()
    value = loss(net(features), target)
    value.backward()
    opt.step()
    return value


def test_cross_entropy_loop_detects_components(tracked_run):
    make, fake = tracked_run
    net = Net()
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    criterion = torch.nn.CrossEntropyLoss()
    train_loader = torch.utils.data.DataLoader(_Tiny(), batch_size=2)
    val_loader = torch.utils.data.DataLoader(_Tiny(), batch_size=2)
    run = make()
    try:
        for features, target in train_loader:
            opt.zero_grad()
            loss = criterion(net(features), target)
            loss.backward()
            opt.step()
        with torch.no_grad():
            for features, target in val_loader:
                net(features)
        run.finish()
    finally:
        if not run._finished:
            run.finish()

    metas = _metadata(fake)
    models = [meta for path, meta in metas.items() if path.startswith("agenticml/model")]
    opts = [meta for path, meta in metas.items() if path.startswith("agenticml/optimizer")]
    losses = [meta for path, meta in metas.items() if path.startswith("agenticml/loss")]
    loaders = [meta for path, meta in metas.items() if path.startswith("agenticml/dataloader")]
    assert len(models) == 1
    assert models[0]["evidence"] == "parameter_overlap"
    assert models[0]["detected_by"] == "auto"
    assert models[0]["qualname"].endswith("Net")
    assert len(opts) == 1
    assert opts[0]["evidence"] == "optimizer_step"
    assert len(losses) == 1
    assert losses[0]["evidence"] == "loss_module"
    assert losses[0]["qualname"].endswith("CrossEntropyLoss")
    roles = {meta["role"] for meta in loaders}
    assert roles == {"train", "eval"}
    names = {meta["name"] for meta in loaders}
    assert names == {"train", "val"}


def test_custom_module_loss_uses_backward_producer(tracked_run):
    make, fake = tracked_run
    net = Net()
    opt = torch.optim.SGD(net.parameters(), lr=1e-2)
    pair_loss = PairLoss()
    run = make()
    try:
        features = torch.randn(2, 4)
        target = torch.randn(2, 2)
        opt.zero_grad()
        loss = pair_loss(net(features), target)
        loss.backward()
        opt.step()
        run.finish()
    finally:
        if not run._finished:
            run.finish()

    losses = [
        meta
        for path, meta in _metadata(fake).items()
        if path.startswith("agenticml/loss")
    ]
    assert len(losses) == 1
    assert losses[0]["evidence"] == "backward_producer"
    assert losses[0]["qualname"].endswith("PairLoss")


def test_function_loss_uses_assignment(tracked_run):
    make, fake = tracked_run
    net = Net()
    opt = torch.optim.SGD(net.parameters(), lr=1e-2)
    run = make()
    try:
        features = torch.randn(2, 4)
        target = torch.randn(2, 2)
        opt.zero_grad()
        loss = compute_loss(net(features), target)
        loss.backward()
        opt.step()
        run.finish()
    finally:
        if not run._finished:
            run.finish()

    losses = [
        meta
        for path, meta in _metadata(fake).items()
        if path.startswith("agenticml/loss")
    ]
    assert len(losses) == 1
    assert losses[0]["evidence"] == "ast_assignment"
    assert losses[0]["qualname"].endswith("compute_loss")
    assert losses[0]["name"] == "compute"


def test_two_models_match_two_optimizers(tracked_run):
    make, fake = tracked_run
    gen = Net()
    disc = Net()
    opt_g = torch.optim.SGD(gen.parameters(), lr=1e-2)
    opt_d = torch.optim.SGD(disc.parameters(), lr=1e-2)
    features = torch.randn(2, 4)
    run = make()
    try:
        opt_g.zero_grad()
        gen(features).sum().backward()
        opt_g.step()
        opt_d.zero_grad()
        disc(features).sum().backward()
        opt_d.step()
        run.finish()
    finally:
        if not run._finished:
            run.finish()

    metas = _metadata(fake)
    model_names = {
        meta["name"]
        for path, meta in metas.items()
        if path.startswith("agenticml/model") and meta.get("role") != "frozen"
    }
    opt_names = {
        meta["name"]
        for path, meta in metas.items()
        if path.startswith("agenticml/optimizer")
    }
    assert model_names == {"gen", "disc"}
    assert opt_names == {"opt_g", "opt_d"}


def test_frozen_teacher_is_marked(tracked_run):
    make, fake = tracked_run
    teacher = Net()
    student = Net()
    opt = torch.optim.SGD(student.parameters(), lr=1e-2)
    features = torch.randn(2, 4)
    run = make()
    try:
        with torch.no_grad():
            teacher(features)
        opt.zero_grad()
        (student(features) - teacher(features).detach()).pow(2).mean().backward()
        opt.step()
        run.finish()
    finally:
        if not run._finished:
            run.finish()

    metas = _metadata(fake)
    frozen = [
        meta
        for path, meta in metas.items()
        if path.startswith("agenticml/model") and meta.get("role") == "frozen"
    ]
    trained = [
        meta
        for path, meta in metas.items()
        if path.startswith("agenticml/model") and meta.get("role") != "frozen"
    ]
    assert len(trained) == 1
    assert trained[0]["evidence"] == "parameter_overlap"
    assert len(frozen) == 1
    assert frozen[0]["evidence"] == "no_optimizer_params"
    assert frozen[0]["name"] == "teacher"


def test_explicit_track_model_suppresses_auto(tracked_run):
    make, fake = tracked_run
    net = Net()
    opt = torch.optim.SGD(net.parameters(), lr=1e-2)
    criterion = torch.nn.MSELoss()
    run = make()
    try:
        run.track_model(net)
        _train_batch(net, opt, criterion)
        run.finish()
    finally:
        if not run._finished:
            run.finish()

    metas = _metadata(fake)
    model_paths = [path for path in metas if path.startswith("agenticml/model")]
    assert model_paths == ["agenticml/model.json"]
    assert "detected_by" not in metas["agenticml/model.json"]


def test_auto_track_can_be_disabled(tracked_run):
    make, fake = tracked_run
    net = Net()
    opt = torch.optim.SGD(net.parameters(), lr=1e-2)
    criterion = torch.nn.MSELoss()
    run = make(auto_track=False)
    try:
        _train_batch(net, opt, criterion)
        run.finish()
    finally:
        if not run._finished:
            run.finish()

    assert run._auto_tracker is None
    assert not any(path.startswith("agenticml/model") for path in _metadata(fake))


def test_finish_scans_main_when_nothing_steps(tracked_run):
    make, fake = tracked_run
    main = sys.modules["__main__"]
    main.net = Net()
    main.opt = torch.optim.SGD(main.net.parameters(), lr=1e-2)
    main.criterion = torch.nn.CrossEntropyLoss()
    main.train_loader = torch.utils.data.DataLoader(_Tiny(), batch_size=2)
    run = make()
    try:
        run.finish()
    finally:
        if not run._finished:
            run.finish()
        for name in ("net", "opt", "criterion", "train_loader"):
            if hasattr(main, name):
                delattr(main, name)

    metas = _metadata(fake)
    assert any(
        path.startswith("agenticml/model") and meta.get("evidence") == "main_globals_scan"
        for path, meta in metas.items()
    )
    assert any(
        path.startswith("agenticml/optimizer")
        and meta.get("evidence") == "main_globals_scan"
        for path, meta in metas.items()
    )
    assert any(
        path.startswith("agenticml/loss") and meta.get("evidence") == "main_globals_scan"
        for path, meta in metas.items()
    )
    assert any(
        path.startswith("agenticml/dataloader")
        and meta.get("name") == "train"
        and meta.get("evidence") == "main_globals_scan"
        for path, meta in metas.items()
    )


def test_forward_hooks_detach_after_second_step(tracked_run):
    make, _fake = tracked_run
    net = Net()
    opt = torch.optim.SGD(net.parameters(), lr=1e-2)
    criterion = torch.nn.MSELoss()
    run = make()
    try:
        _train_batch(net, opt, criterion)
        assert not run._auto_tracker._forward_detached
        assert getattr(torch.Tensor.backward, "_agenticml_wrapped", False)
        _train_batch(net, opt, criterion)
        assert run._auto_tracker._forward_detached
        assert not getattr(torch.Tensor.backward, "_agenticml_wrapped", False)
        assert getattr(torch.utils.data.DataLoader.__iter__, "_agenticml_wrapped", False)
        run.finish()
        assert not getattr(torch.utils.data.DataLoader.__iter__, "_agenticml_wrapped", False)
    finally:
        if not run._finished:
            run.finish()


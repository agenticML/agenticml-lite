"""Tests for automatic loss / lr / epoch metrics from the torch hooks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

from agenticml.auto_tracking import force_uninstall
from agenticml.run import Run


@pytest.fixture(autouse=True)
def _restore_torch_hooks():
    force_uninstall()
    yield
    force_uninstall()


class _LoggingClient:
    def __init__(self):
        self.items: list[dict] = []

    def init_run(self, payload):
        return "metrics-run"

    def log_batch(self, items):
        self.items.extend(items)

    def artifacts_exists(self, hashes):
        return []

    def upload_code(self, run_id, manifest_json, files):
        return {"status": "ok"}

    def finalize(self, run_id, summary, status="finished", snapshot_hash=None):
        pass


@pytest.fixture
def make_run(tmp_path: Path, monkeypatch):
    from agenticml import run as run_mod

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    (tmp_path / "train.py").write_text("pass\n")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "train.py")])
    client = _LoggingClient()
    monkeypatch.setattr(run_mod, "make_client", lambda mode, **kwargs: client)
    runs = []

    def make(**kwargs):
        options = dict(
            capture_env=False,
            system_metrics=False,
            flush_interval=999,
            track_source=False,
            auto_metrics=True,
        )
        options.update(kwargs)
        run = Run("p", "n", **options)
        runs.append(run)
        return run

    yield make, client
    for run in runs:
        if not run._finished:
            run.finish()


def _by_step(client) -> dict[int, dict]:
    merged: dict[int, dict] = {}
    for item in client.items:
        merged.setdefault(item["step"], {}).update(item["metrics"])
    return merged


def _loop(net, optimizer, losses, loader=None, criterion=None):
    criterion = criterion or torch.nn.MSELoss()
    batches = loader if loader is not None else [(torch.randn(2, 4), torch.randn(2, 2))] * 3
    for features, target in batches:
        optimizer.zero_grad()
        loss = criterion(net(features), target)
        losses.append(loss.item())
        loss.backward()
        optimizer.step()


def test_loss_and_lr_logged_per_step(make_run):
    make, client = make_run
    net = torch.nn.Linear(4, 2)
    optimizer = torch.optim.SGD(net.parameters(), lr=0.25)
    run = make()
    losses: list[float] = []
    _loop(net, optimizer, losses)
    run.finish()
    steps = _by_step(client)
    assert sorted(steps) == [0, 1, 2]
    for step, loss in enumerate(losses):
        assert steps[step]["auto/loss"] == pytest.approx(loss)
        assert steps[step]["auto/lr"] == 0.25
        assert steps[step]["auto/epoch"] == 0


def test_metrics_merge_with_user_logs_at_same_step(make_run):
    make, client = make_run
    net = torch.nn.Linear(4, 2)
    optimizer = torch.optim.SGD(net.parameters(), lr=0.1)
    run = make()
    for step in range(2):
        optimizer.zero_grad()
        torch.nn.functional.mse_loss(net(torch.randn(2, 4)), torch.randn(2, 2)).backward()
        optimizer.step()
        run.log({"acc": step / 2}, step=step)
    run.finish()
    steps = _by_step(client)
    assert set(steps[0]) == {"auto/loss", "auto/lr", "auto/epoch", "acc"}
    assert set(steps[1]) == {"auto/loss", "auto/lr", "auto/epoch", "acc"}


def test_log_every_averages_and_flushes_remainder(make_run):
    make, client = make_run
    net = torch.nn.Linear(4, 2)
    optimizer = torch.optim.SGD(net.parameters(), lr=0.1)
    run = make(log_every=2)
    losses: list[float] = []
    batches = [(torch.randn(2, 4), torch.randn(2, 2)) for _ in range(5)]
    _loop(net, optimizer, losses, loader=batches)
    run.finish()
    steps = _by_step(client)
    assert sorted(steps) == [1, 3, 4]
    assert steps[1]["auto/loss"] == pytest.approx(sum(losses[0:2]) / 2)
    assert steps[3]["auto/loss"] == pytest.approx(sum(losses[2:4]) / 2)
    assert steps[4]["auto/loss"] == pytest.approx(losses[4])


def test_gradient_accumulation_averages_backward_calls(make_run):
    make, client = make_run
    net = torch.nn.Linear(4, 2)
    optimizer = torch.optim.SGD(net.parameters(), lr=0.1)
    run = make()
    values = []
    optimizer.zero_grad()
    for _ in range(3):
        loss = torch.nn.functional.mse_loss(net(torch.randn(2, 4)), torch.randn(2, 2))
        values.append(loss.item())
        loss.backward()
    optimizer.step()
    run.finish()
    assert _by_step(client)[0]["auto/loss"] == pytest.approx(sum(values) / 3)


def test_grad_scaler_logs_unscaled_loss(make_run):
    scaler_cls = getattr(getattr(torch, "amp", None), "GradScaler", None)
    if scaler_cls is None:
        pytest.skip("torch.amp.GradScaler unavailable")
    try:
        scaler = scaler_cls("cpu", init_scale=1024.0)
    except Exception as exc:  # older torch: CPU scaling unsupported
        pytest.skip(f"CPU GradScaler unavailable: {exc}")
    make, client = make_run
    net = torch.nn.Linear(4, 2)
    optimizer = torch.optim.SGD(net.parameters(), lr=0.1)
    run = make()
    loss = torch.nn.functional.mse_loss(net(torch.randn(2, 4)), torch.randn(2, 2))
    raw = loss.item()
    scaler.scale(loss).backward()
    scaler.step(optimizer)
    scaler.update()
    run.finish()
    assert _by_step(client)[0]["auto/loss"] == pytest.approx(raw)


def test_epoch_counts_completed_train_loader_passes(make_run):
    make, client = make_run
    dataset = torch.utils.data.TensorDataset(torch.randn(4, 4), torch.randn(4, 2))
    train_loader = torch.utils.data.DataLoader(dataset, batch_size=2)
    net = torch.nn.Linear(4, 2)
    optimizer = torch.optim.SGD(net.parameters(), lr=0.1)
    run = make()
    for _epoch in range(2):
        _loop(net, optimizer, [], loader=train_loader)
    run.finish()
    epochs = [metrics["auto/epoch"] for _step, metrics in sorted(_by_step(client).items())]
    assert epochs == [0, 0, 1, 1]


def test_multiple_optimizers_are_prefixed(make_run):
    make, client = make_run
    gen = torch.nn.Linear(4, 2)
    disc = torch.nn.Linear(4, 2)
    gen_optimizer = torch.optim.SGD(gen.parameters(), lr=0.1)
    disc_optimizer = torch.optim.Adam(disc.parameters(), lr=0.01)
    run = make()
    for _ in range(2):
        gen_optimizer.zero_grad()
        gen(torch.randn(2, 4)).pow(2).mean().backward()
        gen_optimizer.step()
        disc_optimizer.zero_grad()
        disc(torch.randn(2, 4)).pow(2).mean().backward()
        disc_optimizer.step()
    run.finish()
    keys = set().union(*_by_step(client).values())
    assert {"auto/gen/loss", "auto/gen/lr", "auto/disc/loss", "auto/disc/lr"} <= keys, keys


def test_backward_wrapper_kept_only_with_metrics(make_run):
    make, _client = make_run
    net = torch.nn.Linear(4, 2)
    optimizer = torch.optim.SGD(net.parameters(), lr=0.1)

    run = make()
    _loop(net, optimizer, [])
    assert getattr(torch.Tensor.backward, "_agenticml_wrapped", False)
    run.finish()
    assert not getattr(torch.Tensor.backward, "_agenticml_wrapped", False)

    run = make(auto_metrics=False)
    _loop(net, optimizer, [])
    assert not getattr(torch.Tensor.backward, "_agenticml_wrapped", False)
    run.finish()


def test_no_metrics_when_disabled(make_run):
    make, client = make_run
    net = torch.nn.Linear(4, 2)
    optimizer = torch.optim.SGD(net.parameters(), lr=0.1)
    run = make(auto_metrics=False)
    _loop(net, optimizer, [])
    run.finish()
    assert client.items == []

"""The dummy project trains independently of the tracker."""
import builtins
import os
import subprocess
import sys

import pytest

from dummytrain.data import make_data
from dummytrain.model import LogisticRegression
from dummytrain.train import fit_image, main, train


def test_seeded_split():
    training, validation = make_data(3)
    assert (training, validation) == make_data(3)
    assert (training, validation) != make_data(4)
    assert len(training) == 120 and len(validation) == 40
    assert set(training).isdisjoint(validation)
    assert {y for _, y in training} == {0, 1}


def test_gradient_finite_differences():
    model = LogisticRegression(1)
    rows = make_data(9)[0][:12]
    dw, db = model.grad(rows)
    eps = 1e-6
    for index, expected in enumerate([*dw, db]):
        original = model.state_dict()
        if index < 2:
            model.weights[index] += eps
        else:
            model.bias += eps
        plus = model.loss(rows)
        model.weights, model.bias = original["weights"][:], original["bias"]
        if index < 2:
            model.weights[index] -= eps
        else:
            model.bias -= eps
        minus = model.loss(rows)
        model.weights, model.bias = original["weights"], original["bias"]
        assert (plus - minus) / (2 * eps) == pytest.approx(expected, abs=1e-8)


def test_training_and_checkpoint(tmp_path):
    steps, epochs = [], []
    result = train({"out": tmp_path}, lambda *args: steps.append(args), lambda *args: epochs.append(args))
    assert len(steps) == 32 and len(epochs) == 4
    assert [step for step, _ in steps] == list(range(32))
    assert result["final_loss"] < result["initial_loss"] * 0.25
    assert result["best_val_acc"] >= 0.9
    assert result["history"][-1]["val/loss"] < result["history"][0]["val/loss"]
    loaded = LogisticRegression.load(result["checkpoint"])
    assert loaded.state_dict() == result["model"].state_dict()
    assert loaded.forward((0.5, -1)) == result["model"].forward((0.5, -1))
    second = train({"out": tmp_path / "second"})
    assert second["losses"] == result["losses"]


def test_zero_code_main_never_imports_agenticml(tmp_path):
    script = """
import sys
from dummytrain.train import main
assert not any(k == 'agenticml' or k.startswith('agenticml.') for k in sys.modules)
main(['--epochs', '1', '--steps-per-epoch', '2', '--out', sys.argv[1]])
assert not any(k == 'agenticml' or k.startswith('agenticml.') for k in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            env=os.environ.copy(), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_fail_at_epoch(tmp_path):
    with pytest.raises(RuntimeError, match="requested failure at epoch 2"):
        main(["--fail-at-epoch", "2", "--out", str(tmp_path)])
    assert not (tmp_path / "model.json").exists()


def test_exit_code(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["--epochs", "1", "--out", str(tmp_path), "--exit-code", "17"])
    assert exc.value.code == 17
    assert (tmp_path / "model.json").is_file()


def test_image_without_pillow(monkeypatch):
    original = builtins.__import__

    def without_pillow(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("Pillow intentionally unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pillow)
    assert fit_image(LogisticRegression()) is None


@pytest.mark.parametrize("cfg", [{"lr": 0}, {"epochs": 0}, {"batch_size": 0}, {"steps_per_epoch": 0}])
def test_invalid_training_config(cfg):
    with pytest.raises(ValueError, match="positive"):
        train(cfg)

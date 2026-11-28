"""Generate the table fixtures: python generate.py <output dir>.

These files are written by hand-rolled code that follows the real formats; they were NOT
produced by running PyTorch Lightning or Hugging Face Transformers (both pull in torch).
Formats followed:

- Lightning ``CSVLogger`` (lightning 2.x): ``<save_dir>/<name>/version_N/metrics.csv`` with the
  header ``epoch,step,<metric names sorted>``, one sparse row per ``log`` call (empty cells for
  metrics not logged in that row), and a flat ``hparams.yaml`` written by ``yaml.dump``.
- Hugging Face ``Trainer`` (transformers 4.4x): ``<output_dir>/trainer_state.json`` with a
  ``log_history`` list (train rows with ``loss``/``learning_rate``/``grad_norm``, eval rows with
  ``eval_*``, one final summary row with ``train_runtime`` ...) and a copy inside
  ``checkpoint-<step>/``.
- ``plain/``: a CSV and a JSONL file with a ``step`` and a ``timestamp`` column.

Output is deterministic (file times are not part of it).
"""

import json
import sys
from pathlib import Path


def lightning(out: Path) -> None:
    base = out / "lightning" / "lightning_logs"
    v0 = base / "version_0"
    v0.mkdir(parents=True)
    rows = [
        "epoch,step,train_acc,train_loss,val_acc,val_loss",
        "0,9,0.30,2.10,,",
        "0,19,0.45,1.80,,",
        "0,24,,,0.41,1.90",
        "1,29,0.60,1.40,,",
        "1,39,0.72,1.10,,",
        "1,49,,,0.66,1.20",
        "2,59,0.80,0.85,,",
        "2,69,0.86,0.60,,",
        "2,74,,,0.79,0.82",
    ]
    (v0 / "metrics.csv").write_text("\n".join(rows) + "\n")
    (v0 / "hparams.yaml").write_text(
        "batch_size: 32\n"
        "hidden_dim: 128\n"
        "lr: 0.001\n"
        "optimizer: adam\n"
        "use_bn: true\n"
        "dropout: null\n"
        "layers:\n"
        "- 64\n"
        "- 32\n"
    )
    v1 = base / "version_1"
    v1.mkdir()
    (v1 / "metrics.csv").write_text(
        "epoch,step,train_loss,val_loss\n0,9,2.50,\n0,19,2.00,\n0,24,,2.20\n"
    )


def hf(out: Path) -> None:
    def state(max_step: int) -> dict:
        history = [
            {"epoch": 1.0, "grad_norm": 1.9, "learning_rate": 4.0e-05, "loss": 2.31, "step": 10},
            {"epoch": 1.0, "eval_loss": 2.05, "eval_runtime": 1.5, "eval_samples_per_second": 66.7,
             "eval_steps_per_second": 8.3, "step": 10},
            {"epoch": 2.0, "grad_norm": 1.4, "learning_rate": 2.0e-05, "loss": 1.62, "step": 20},
            {"epoch": 2.0, "eval_loss": 1.55, "eval_runtime": 1.4, "eval_samples_per_second": 71.4,
             "eval_steps_per_second": 8.9, "step": 20},
        ]
        if max_step >= 30:
            history += [
                {"epoch": 3.0, "grad_norm": 1.1, "learning_rate": 0.0, "loss": 1.2, "step": 30},
                {"epoch": 3.0, "step": 30, "total_flos": 123456789.0, "train_loss": 1.7,
                 "train_runtime": 31.5, "train_samples_per_second": 7.6, "train_steps_per_second": 0.95},
            ]
        return {
            "best_metric": None,
            "best_model_checkpoint": None,
            "epoch": float(max_step // 10),
            "eval_steps": 10,
            "global_step": max_step,
            "is_hyper_param_search": False,
            "log_history": history,
            "logging_steps": 10,
            "max_steps": 30,
            "num_train_epochs": 3,
            "save_steps": 10,
            "train_batch_size": 8,
            "trial_name": None,
            "trial_params": None,
        }

    output = out / "hf" / "bert-finetune"
    for directory, step in ((output, 30), (output / "checkpoint-10", 10), (output / "checkpoint-20", 20)):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "trainer_state.json").write_text(json.dumps(state(step), indent=2) + "\n")
    # An output directory that only has checkpoints: the newest one is imported.
    only = out / "hf" / "only-checkpoints"
    for step in (10, 20):
        (only / f"checkpoint-{step}").mkdir(parents=True)
        (only / f"checkpoint-{step}" / "trainer_state.json").write_text(
            json.dumps(state(step), indent=2) + "\n"
        )


def plain(out: Path) -> None:
    directory = out / "plain"
    directory.mkdir(parents=True)
    start = 1700000000
    rows = ["step,timestamp,loss,acc,split"]
    for i in range(5):
        rows.append(f"{i},{start + i * 10},{1.0 / (i + 1):.4f},{0.5 + i * 0.1:.2f},train")
    (directory / "train.csv").write_text("\n".join(rows) + "\n")
    lines = []
    for i in range(5):
        lines.append(
            json.dumps(
                {"global_step": i, "time": (start + i * 10) * 1000, "train": {"loss": 1.0 / (i + 1)},
                 "lr": 0.1 / (i + 1), "note": "ok"}
            )
        )
    (directory / "train.jsonl").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    target = Path(sys.argv[1])
    lightning(target)
    hf(target)
    plain(target)

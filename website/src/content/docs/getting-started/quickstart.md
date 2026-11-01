---
title: Quickstart
description: Track a run with one CLI command or from Python, then view it in the dashboard.
sidebar:
  order: 2
---

## 1. Track a run with no code changes

Put `agenticml run` in front of the command you already use. Your script does not import `agenticml`, and nothing in it changes.

```bash
agenticml run train.py
```

No arguments are needed. If `train.py` uses `argparse`, its parsed values (defaults included, such as `lr`) are recorded as the run's config whether or not you pass flags. To pass your own, add them after the script:

```bash
agenticml run train.py --lr 1e-3 --epochs 20
```

Hyperparameters, a code snapshot, model/optimizer/loss tracking and system metrics are captured automatically. See [what gets captured](/agenticml-lite/guides/zero-code/#what-gets-captured).

More examples, all zero-code:

```bash
# No script arguments at all: defaults are captured as config
agenticml run train.py
agenticml run --project demo --name baseline train.py

# A module entry point
agenticml run -m mypkg.train
agenticml run -m mypkg.train --epochs 3

# Name the project, run and tags
agenticml run --project demo --name baseline --tags baseline,v1 train.py --lr 1e-3

# Wrap an existing command line (Makefile, shell script, README)
agenticml run -- python train.py
agenticml run -- python train.py --lr 1e-3

# Least typing: "python, but tracked" (options come from AGENTICML_* variables)
AGENTICML_PROJECT=demo agenticml python train.py

# Multi-GPU: only rank 0 logs
torchrun --nproc_per_node=4 -m agenticml run -- train.py

# Try it on the bundled toy example, with and without arguments
agenticml run -- python examples/train_toy.py
agenticml run -- python examples/train_toy.py --lr 0.05 --epochs 5
```

Options such as `--project` and `--name` go before the script. Everything after the script is passed to it untouched. Use `--no-auto-track`, `--no-capture-args`, or `--no-tensorboard` to switch parts off. All command forms are in the [zero-code guide](/agenticml-lite/guides/zero-code/).

## Or log from Python

Use the SDK when you want to choose exactly which metrics, config, and summary values are logged. The [Python SDK guide](/agenticml-lite/guides/python-sdk/) covers every call and option.

```python title="train.py"
import agenticml

agenticml.init(
    project="demo",
    name="exp1",
    config={"learning_rate": 0.01, "epochs": 10},
    tags=["baseline"],
)

for step in range(10):
    agenticml.log({"loss": 1 / (step + 1), "accuracy": step / 10}, step=step)

agenticml.summary["best_loss"] = 0.1
agenticml.finish()
```

`init` starts the one active run. If you omit `project`, it defaults to the repository or directory name (`AGENTICML_PROJECT` overrides that). If you omit `name`, it defaults to the main script's path relative to the repo root, such as `scripts/train.py`.

`log` advances the step when `step` is omitted, and `commit=False` merges later metrics into the same step. `config` and `summary` are dicts on the active run. Pass `verbose=True` to `init` to print the run id, run directory, and the `agenticml ui` command.

## 2. Open the dashboard

```bash
agenticml ui
```

Then open [http://127.0.0.1:6006](http://127.0.0.1:6006).

## Log media and artifacts

```python
agenticml.log({"pred": agenticml.Image("pred.png", caption="val")})
agenticml.log_artifact("model.pt", name="checkpoint")             # reference
agenticml.log_artifact("model.pt", name="checkpoint", copy=True)  # snapshot bytes
agenticml.track_model(model)  # after the object exists
```

`Image` accepts a path, bytes, a file object, a PIL image, or a numpy array. Numpy arrays and PIL images need the `media` extra. `track_optimizer`, `track_loss`, and `track_dataloader` work the same way as `track_model`.

## What is tracked by default

`init` turns on runtime code snapshots and PyTorch component detection (`auto_track=True`). Automatic loss, learning-rate, and epoch metrics and TensorBoard mirroring stay off until you pass `auto_metrics=True` and `sync_tensorboard=True`. System metrics are on when `psutil` is installed; `system_metrics=False` disables them.

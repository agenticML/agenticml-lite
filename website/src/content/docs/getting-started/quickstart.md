---
title: Quickstart
description: Log a run from Python and view it in the dashboard.
sidebar:
  order: 2
---

## 1. Log a run

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

## Don't want to edit your script?

Use [`agenticml run`](/agenticml-lite/guides/zero-code/) to track an existing script as is.

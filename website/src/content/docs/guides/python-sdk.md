---
title: Python SDK
description: Log metrics, config, media, and artifacts from Python with agenticml.init, log, and finish.
sidebar:
  order: 2
---

Use the Python SDK when you want to choose exactly what is logged. If you only need to track an existing script, [`agenticml run`](/agenticml-lite/guides/zero-code/) needs no code changes.

Install the package as `agenticml-lite` and import it as `agenticml`. Runs are written to local files, so no server or API key is needed.

## A first run

```python title="train.py"
import agenticml

run = agenticml.init(
    project="demo",
    name="exp1",
    config={"learning_rate": 0.01, "epochs": 10},
    tags=["baseline"],
    verbose=True,  # prints the run id, run directory, and the ui command
)

for step in range(10):
    agenticml.log({"loss": 1 / (step + 1), "accuracy": step / 10}, step=step)

agenticml.summary["best_loss"] = 0.1
agenticml.finish()
```

```bash
python train.py
agenticml ls
agenticml ui
```

## API overview

| Call | What it does |
| --- | --- |
| `agenticml.init(project, name, **opts)` | Start the one active run and return a `Run`. |
| `agenticml.log(metrics, step=None, commit=True)` | Log a dict of scalars and `Image`s. |
| `agenticml.config` | Dict-like config of the active run. |
| `agenticml.summary` | Dict-like summary of the active run. Set final values here. |
| `agenticml.log_artifact(path, name=None, type="file", metadata=None, copy=False)` | Record a file. |
| `agenticml.Image(src, caption=...)` | An image for `log`. |
| `agenticml.track_files(*paths)` | Add extra files to the code snapshot. |
| `agenticml.track_model`, `track_optimizer`, `track_loss`, `track_dataloader` | Record PyTorch components. |
| `agenticml.get_active_run()` | The current run, or `None`. |
| `agenticml.finish(status="finished")` | End the run. Also called automatically at exit. |

## Starting a run

`init` starts the one active run.

- **`project`** defaults to the repository or directory name. `AGENTICML_PROJECT` overrides the default, and an explicit `project` wins over both.
- **`name`** defaults to the main script's path relative to the repo root, such as `scripts/train.py`, or its absolute path outside a repo.

Commonly used options:

| Option | Meaning |
| --- | --- |
| `config` | Hyperparameters, as a dict. |
| `tags`, `notes`, `group`, `job_type` | Labels to organize and filter runs. |
| `task` | The task the run belongs to, such as `"asr"`. See [Tasks and lineage](#tasks-and-lineage). |
| `id`, `resume` | Resume a run. `resume` is `"never"`, `"allow"`, or `"must"`. |
| `mode` | `"local"`, `"offline"`, or `"disabled"`. |
| `dir` | Store root, instead of `AGENTICML_DIR` or `~/.agenticml`. |
| `system_metrics` | `False` turns off CPU, memory, and GPU sampling. |
| `auto_track`, `auto_metrics`, `sync_tensorboard` | Automatic capture. See [Automatic capture](#automatic-capture). |
| `lineage` | Code snapshots in a shadow Git repository. On by default. |
| `verbose` | Print the run id, run directory, and the `agenticml ui` command. |

A run is also a context manager, which finishes it on exit and marks it `crashed` on an exception:

```python
with agenticml.Run("demo", "exp1") as run:
    run.log({"loss": 0.5})
```

## Logging metrics

`log` takes a dict of scalars. The step advances by one when you omit `step`.

```python
agenticml.log({"loss": 0.9})              # step 0
agenticml.log({"loss": 0.7})              # step 1
agenticml.log({"loss": 0.5}, step=10)     # explicit step
```

Pass `commit=False` to merge later metrics into the same step, for example to log training and validation values together:

```python
agenticml.log({"train/loss": 0.4}, step=5, commit=False)
agenticml.log({"val/loss": 0.5}, step=5)
```

## Config and summary

`config` and `summary` are dicts on the active run.

```python
agenticml.config["lr"] = 0.1
agenticml.summary["best_loss"] = 0.08
```

Keys passed to `init(config=...)` win over hyperparameters captured automatically.

## Media and artifacts

```python
agenticml.log({"pred": agenticml.Image("pred.png", caption="val")}, step=0)
agenticml.log_artifact("model.pt", name="checkpoint")             # path, size, mtime
agenticml.log_artifact("model.pt", name="checkpoint", copy=True)  # snapshot the bytes
```

`Image` accepts a path, bytes, a file object, a PIL image, or a numpy array. Numpy arrays and PIL images need the `media` extra: `pip install "agenticml-lite[media]"`.

An artifact records only its path, size, and modification time unless you pass `copy=True`, which stores the bytes once, keyed by SHA-256.

## Automatic capture

`init` turns on runtime code snapshots and PyTorch component detection (`auto_track=True`). Automatic loss, learning-rate, and epoch metrics and TensorBoard mirroring stay off until you pass `auto_metrics=True` and `sync_tensorboard=True`.

```python
agenticml.init(project="demo", auto_metrics=True, sync_tensorboard=True)
```

Record PyTorch objects yourself with the `track_*` calls, after the object exists:

```python
agenticml.track_model(model)
agenticml.track_optimizer(optimizer)
agenticml.track_loss(criterion)
agenticml.track_dataloader(loader)
agenticml.track_files("configs/extra.yaml")  # extra files for the code snapshot
```

System metrics are sampled every 10 seconds when `psutil` is installed. Use `pip install "agenticml-lite[system]"` for CPU and memory, or `[gpu]` for NVIDIA GPUs.

## Resuming a run

```python
run = agenticml.init(project="demo", name="exp1")
run_id = run.run_id
agenticml.finish()

agenticml.init(project="demo", name="exp1", id=run_id, resume="must")
```

A resumed run keeps its stored task and parent.

## Tasks and lineage

A run belongs to a task, such as `"asr"` or `"imagegen"`. Lineage graphs and parent inference are per `(project, task)`.

```python
agenticml.init(project="demo", task="asr")
```

Without `task=`, the task comes from `AGENTICML_TASK`, then the explicit parent's task, then an `agenticml.toml` `task` key, then the entry script's folder. See the [zero-code guide](/agenticml-lite/guides/zero-code/#lineage-parent-and-task) for the full order and the cross-task warning.

`lineage=True` (the default) snapshots your project code in a shadow Git repository under the store root. The project itself is never written to. Disable it with `lineage=False` or `AGENTICML_LINEAGE=0`.

Select an explicit parent with `parent_run_id=`. A warning is logged when the parent's task differs from the new run's task. Pass `allow_cross_task=True` to silence it.

```python
agenticml.init(project="demo", task="asr", parent_run_id="20261027T021500-ab12cd34")
```

## Disabling tracking

Set `AGENTICML_MODE=disabled` or pass `mode="disabled"`. Every call becomes a no-op, so you can leave tracking code in place.

```bash
AGENTICML_MODE=disabled python train.py
```

## Crashes and exit status

If your script raises after `init()`, the run finishes as `crashed` with `_exception` in its summary. A normal exit finishes it as `finished`.

## Troubleshooting

- **Nothing in `agenticml ls`:** you probably wrote to a different store. Check `echo $AGENTICML_DIR`, or pass `init(dir=...)`.
- **`log` does nothing:** check `AGENTICML_MODE`. `disabled` silences everything.
- **`Image` fails with numpy or PIL input:** install the `media` extra.
- **No system metrics:** install the `system` extra.
- **A run shows `running` after a crash:** it is marked `crashed` once its lock is released. `agenticml sync` finalizes it.
- **Multi-GPU:** only rank 0 logs. Launch as `torchrun ... -m agenticml run -- train.py`.

For the on-disk layout, see the [storage reference](/agenticml-lite/reference/storage/). To try the SDK end to end, run `python examples/train_toy.py --explicit`.

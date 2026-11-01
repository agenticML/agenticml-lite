---
title: Zero-code runs
description: Track an existing training script with agenticml run, without importing agenticml.
sidebar:
  order: 1
---

`agenticml run` executes a script inside a run. The script does not need to `import agenticml`.

```bash
agenticml run train.py --lr 1e-3
agenticml run -m mypkg.train --epochs 3
agenticml run --project demo -- python train.py --lr 1e-3
agenticml python train.py --lr 1e-3
```

## Command forms

Every form runs the target in-process as `__main__` inside one tracked run. Pick the one that matches how you already start training.

| Form | Use it when |
| --- | --- |
| `agenticml run train.py [args]` | You want to track a script with the current interpreter. Shortest form. |
| `agenticml run -m pkg.train [args]` | Your entry point is a module, so relative imports keep working. |
| `agenticml run -- python train.py [args]` | You are wrapping an existing command line (Makefile, shell script). Swap `python` for `agenticml run --`. |
| `agenticml python train.py [args]` | You want the least typing. Options come from `AGENTICML_*` variables. |
| `agenticml run --project demo -- python -u train.py` | You need Python flags or a specific interpreter. `agenticml-lite` must be installed there. |
| `python -m agenticml run train.py` | `agenticml` is not on `PATH`, for example inside `torchrun` or `accelerate launch`. |

Rules for every form:

- agenticml options (`--project`, `--name`, …) come before the script or module. Everything after the target is passed to your script untouched.
- The `--` separator is needed only in front of `python ...`.
- The default run name is the script's repo-relative path, unless `--name` is given.
- Only Python files are supported: no `python -c` and no stdin scripts.

## What gets captured

| What | Where it comes from | Turn off |
| --- | --- | --- |
| Config | `argparse` and `@hydra.main` | `--no-capture-args` |
| Code snapshot | imported project modules and files opened for reading | always on under `agenticml run` |
| Model, optimizer, loss, dataloader | PyTorch construction hooks | `--no-auto-track` |
| `auto/loss`, `auto/lr`, `auto/epoch` | `backward()` and `optimizer.step()` | `--no-auto-metrics`; `--log-every N` |
| TensorBoard | `SummaryWriter` / `tensorboardX` | `--no-tensorboard` |
| System metrics | psutil, plus pynvml when installed | needs the `system` extra |

Loss is unscaled under `GradScaler` and averaged over gradient-accumulation steps. Keys passed to `init(config=...)` win over captured hyperparameters.

## Options and environment variables

`--project`, `--name`, `--tags`, `--notes`, `--group`, `--job-type`, `--mode`, `--dir`, and `--log-every` each read a matching `AGENTICML_*` variable, for example `AGENTICML_PROJECT`. `--task` sets the run's task and overrides `AGENTICML_TASK`. `--parent RUN_ID` reads `AGENTICML_PARENT`. Run `agenticml run -h` for the full list.

## Lineage parent and task

`--parent RUN_ID` (or `--parent=RUN_ID`, or env `AGENTICML_PARENT`) sets an explicit lineage parent for the new run. The CLI value wins over the env var. An invalid run id is a usage error (exit 2). `--parent` applies only to new runs: a resumed run keeps its stored parent and warns if `--parent` differs.

```bash
agenticml run --parent <RUN_ID> train.py
```

Without `--task` (and without a valid `AGENTICML_TASK`), the new run inherits the parent's task, so it shows up in the parent's lane in the lineage graph. The dashboard labels such lanes "from parent run". Task precedence: `--task` > `AGENTICML_TASK` > the parent's task > an `agenticml.toml` `task` > the entry script's folder.

If `--task` or `AGENTICML_TASK` differs from the parent's task, the run still starts and logs this warning to stderr: `run task 'b' differs from parent run '<RUN_ID>' task 'a'; the lineage link crosses tasks`. This applies only when the parent can be read, is in the same project, and has a task. `--allow-cross-task` or `AGENTICML_ALLOW_CROSS_TASK=1` silences that warning when the cross-task link is intended. In Python, `agenticml.init(parent_run_id=..., task=..., allow_cross_task=True)` does the same.

A missing or unreadable parent is still recorded, with a warning. A parent in another project is recorded, but its task is not inherited.

## Exit status

A script that calls `agenticml.init()` joins the launcher's run. Exit code 0 finishes the run as `finished`; any other exit finishes it as `crashed`, with `_exit_code` and `_exception` in the summary. Your script's exit code is preserved, and SIGTERM finishes the run and exits 143.

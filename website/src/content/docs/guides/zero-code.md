---
title: Zero-code runs
description: Track an existing training script with agenticml run, without importing agenticml.
sidebar:
  order: 1
---

`agenticml run` executes a script inside a run. The script does not need to `import agenticml`.

```bash
agenticml run train.py
agenticml run train.py --lr 1e-3
agenticml run -m mypkg.train
agenticml run -m mypkg.train --epochs 3
agenticml run --project demo -- python train.py --lr 1e-3
agenticml python train.py
agenticml python train.py --lr 1e-3
```

Script arguments are optional. Without them, the script runs with its own defaults and `argparse` values are still captured as config. With them, everything after the script is passed through untouched.

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

<details>
<summary><h2>Advanced</h2></summary>

### Multiple lineages in one working directory (tasks)

A lineage is grouped by project and task. Runs with the same task form one lineage lane. Use a different task to start a separate lineage in the same directory.

Set the task per run:

```bash
agenticml run --task alpha train.py
AGENTICML_TASK=alpha agenticml run train.py
```

Or set it per directory with an `agenticml.toml` file:

```toml
task = "alpha"
```

agenticml reads the nearest `agenticml.toml` from the script's folder up to the repo root.

Task precedence: `--task` > `AGENTICML_TASK` > the parent run's task > `agenticml.toml` `task` > the entry script's folder (relative to the repo root). A script at the repo root with no task signal goes into a "no task" lane.

A task is trimmed, must be 1 to 128 characters, and cannot contain control characters. See [Lineage parent and task](#lineage-parent-and-task) for the full rules. Use `--allow-cross-task` only to silence the cross-task warning when the cross-task link is intended.

`agenticml lineage ls` shows the lanes:

```text
proj
  [beta]
    20261101T172034-fe500858  train.py
  [no task]
    20261101T172033-df1b1ee7  train.py
  [alpha]
    20261101T172032-3a1a2f2a  train.py
```

### Spawning an experiment from a parent run

By default, agenticml infers the parent: the latest earlier run in the same project and the same task. A run from the same repo root is preferred.

To choose the parent yourself, pass `--parent RUN_ID` (or `--parent=RUN_ID`, or env `AGENTICML_PARENT`). The CLI flag wins over the env var. Find run ids with `agenticml lineage ls`.

```bash
agenticml run --task alpha --parent 20261101T172032-3a1a2f2a train.py
```

After two runs in task `alpha`, the second run's metadata records `parent_run_id` as the first run, with `parent_inference` set to `repo_root`. An explicit `--parent` records `parent_inference` as `explicit`.

`--parent` applies to new runs only. See [Lineage parent and task](#lineage-parent-and-task) for the rest.

### Checking out a run's code

`agenticml lineage checkout RUN_ID` creates a separate, detached git worktree at the code snapshot that run captured. It does not switch the branch of your own working copy.

```bash
agenticml lineage checkout 20261101T172034-fe500858
cd "$(agenticml lineage checkout 20261101T172034-fe500858)"
```

The command prints the path it created. The default location is `<store>/worktrees/<project key>/<RUN_ID>`. The store is `~/.agenticml` unless `--dir` or `AGENTICML_DIR` is set.

- `--path DIR` chooses the location: `agenticml lineage checkout RUN_ID --path ./run-alpha`
- `--dir DIR` sets the store root.
- Running checkout again for the same run returns the same path and does not fail.
- It fails if the destination exists and is not a checkout of that run.

To compare two runs, `agenticml lineage diff RUN_A [RUN_B]` lists the files added, deleted or changed between them.

</details>

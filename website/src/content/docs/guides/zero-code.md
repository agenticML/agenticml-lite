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
| Config | `argparse` and `@hydra.main` | `--no-capture-args` (`AGENTICML_CAPTURE_ARGS=0`) |
| Code snapshot | imported project modules and files opened for reading | `--no-code` (`AGENTICML_TRACK_CODE=0`) |
| Model, optimizer, loss, dataloader | PyTorch construction hooks | `--no-auto-track` (`AGENTICML_AUTO_TRACK=0`) |
| `auto/loss`, `auto/lr`, `auto/epoch` | `backward()` and `optimizer.step()` | `--no-auto-metrics` (`AGENTICML_AUTO_METRICS=0`); `--log-every N` |
| TensorBoard | `SummaryWriter` / `tensorboardX` | `--no-tensorboard` (`AGENTICML_SYNC_TENSORBOARD=0`) |
| System metrics | psutil, plus pynvml when installed | `--no-system-metrics` (`AGENTICML_SYSTEM_METRICS=0`); `--system-metrics-interval S` |
| Lineage | shadow-Git snapshot | `--no-lineage` (`AGENTICML_LINEAGE=0`) |
| Environment | environment metadata | `--no-env` (`AGENTICML_CAPTURE_ENV=0`) |

Loss is unscaled under `GradScaler` and averaged over gradient-accumulation steps. Keys passed to `init(config=...)` win over captured hyperparameters. Defaults differ between the SDK and CLI for two switches: `auto_metrics` and `sync_tensorboard` are off by default in `agenticml.init()` and on under `agenticml run`.

## Options and environment variables

A CLI flag overrides its environment variable; a blank environment variable counts as unset. Repeatable flags (`--track-file`, `--source-root`) replace rather than extend the corresponding environment variable list. Environment variables with multiple paths (`AGENTICML_TRACK_FILES`, `AGENTICML_SOURCE_ROOTS`) use `os.pathsep` (`:` on POSIX, `;` on Windows).

There is no positive override flag: once a feature is disabled via its environment variable (such as `AGENTICML_SYSTEM_METRICS=0` or `AGENTICML_ALL_RANKS=1`), the CLI cannot re-enable it; unset the variable instead.

| Option | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--project NAME` | `AGENTICML_PROJECT` | repo or directory name | Project name |
| `--name NAME` | `AGENTICML_NAME` | entry script path | Run name |
| `--tags A,B` | `AGENTICML_TAGS` | unset | Comma-separated tags |
| `--notes TEXT` | `AGENTICML_NOTES` | unset | Run launch notes |
| `--config KEY=VALUE` | none | unset | Config key/value pair (repeatable; JSON or raw string) |
| `--config-file PATH` | `AGENTICML_CONFIG_FILE` | unset | JSON file containing a top-level config object |
| `--group NAME` | `AGENTICML_GROUP` | unset | Run group |
| `--job-type NAME` | `AGENTICML_JOB_TYPE` | unset | Job type |
| `--task NAME` | `AGENTICML_TASK` | inferred | Task name |
| `--parent RUN_ID` | `AGENTICML_PARENT` | inferred | Lineage parent run ID |
| `--allow-cross-task` | `AGENTICML_ALLOW_CROSS_TASK=1` | off | Silence warning when `--task` differs from parent's task |
| `--id RUN_ID` | `AGENTICML_RUN_ID` | unset | Run ID to resume (requires `--resume allow` or `must`) |
| `--resume MODE` | `AGENTICML_RESUME` | unset | Resume mode: `never`, `allow`, or `must` |
| `--mode MODE` | `AGENTICML_MODE` | `local` | `local`, `offline`, or `disabled` |
| `--dir DIR` | `AGENTICML_DIR` | `~/.agenticml` | Local store root |
| `--no-auto-track` | `AGENTICML_AUTO_TRACK=0` | on | Turn off PyTorch component detection |
| `--no-auto-metrics` | `AGENTICML_AUTO_METRICS=0` | on under run | Turn off `auto/loss`, `auto/lr`, `auto/epoch` |
| `--log-every N` | `AGENTICML_LOG_EVERY` | `1` | Frequency of auto metrics in optimizer steps |
| `--no-tensorboard` | `AGENTICML_SYNC_TENSORBOARD=0` | on under run | Turn off TensorBoard mirroring |
| `--no-capture-args` | `AGENTICML_CAPTURE_ARGS=0` | on | Turn off `argparse` and Hydra argument capture |
| `--no-system-metrics` | `AGENTICML_SYSTEM_METRICS=0` | on (if psutil) | Turn off CPU, memory, and GPU sampling |
| `--system-metrics-interval S` | `AGENTICML_SYSTEM_METRICS_INTERVAL` | `10.0` | System metrics sampling interval in seconds |
| `--no-lineage` | `AGENTICML_LINEAGE=0` | on | Turn off shadow-Git lineage snapshots |
| `--no-code` | `AGENTICML_TRACK_CODE=0` | on | Turn off source file tracking |
| `--no-env` | `AGENTICML_CAPTURE_ENV=0` | on | Turn off environment metadata capture |
| `--track-file PATH` | `AGENTICML_TRACK_FILES` | unset | Extra source file to track (repeatable; path-separated in env) |
| `--source-root DIR` | `AGENTICML_SOURCE_ROOTS` | unset | Extra source directory (repeatable; path-separated in env) |
| `--max-file-bytes N` | `AGENTICML_MAX_FILE_BYTES` | `10485760` (10 MiB) | Per-file size cap for code and lineage snapshots |
| `--max-total-bytes N` | `AGENTICML_MAX_TOTAL_BYTES` | `104857600` (100 MiB) | Total size cap for code and lineage snapshots |
| `--flush-interval S` | `AGENTICML_FLUSH_INTERVAL` | `2.0` | Background metric flush interval in seconds |
| `--all-ranks` | `AGENTICML_ALL_RANKS=1` | rank 0 only | Track all distributed ranks, not just rank 0 |
| `--verbose` | none | off | Print run ID and store paths |

### Resuming a run

Resume an existing run or continue under a specific ID:

```bash
agenticml run --id 20261101T130355-1cded4a7 --resume must -- python train.py
```

- `--id` requires `--resume allow` or `must`, and `--resume allow` or `must` requires `--id`; otherwise it is a usage error (exit 2).
- `must` requires that the run already exists. `allow` resumes an existing run, or creates a new run with that ID if none exists.
- A resumed run keeps its stored project, name, task, and parent (a differing `--parent` logs a warning). Step counters continue from the last stored step, and a `resume` event is logged.
- If a requested resume cannot be performed (the run is missing under `must`, open in another process, pruned by `sync --prune`, or missing `meta.json`), `agenticml run` prints an error and exits 2 without running the script.
- If the script itself calls `agenticml.init(id=..., resume=...)` under `agenticml run`, those arguments are ignored with a warning; configure resume through the CLI instead.

### Setting configuration

Pass configuration values from the CLI or a file:

```bash
agenticml run --config lr=1e-3 --config amp=true --config layers='[64, 64]' -- python train.py
agenticml run --config-file base.json -- python train.py
```

- `--config KEY=VALUE` (repeatable): `VALUE` is parsed as JSON if valid (`lr=1e-3` becomes `0.001`, `amp=true` becomes boolean `true`, `layers=[64,64]` becomes a list). If it does not parse as JSON, it remains a raw string (`opt=adam`). Quote the value to force a string: `--config 'tag="1e-3"'`. Values that evaluate to `NaN` or `Infinity` are rejected (exit 2). When a key is repeated, the last occurrence wins.
- `--config-file PATH` (`AGENTICML_CONFIG_FILE`): Reads configuration from a JSON file whose top level is an object.
- Precedence order: config file < `--config` pairs < script's own `init(config=...)`.
- Explicit CLI and file configuration keys are protected: argument parsers (`argparse`, Hydra) never overwrite them.
- When resuming a run, new configuration values merge on top of the run's stored config.

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

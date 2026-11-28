---
title: Import runs from other trackers
description: Copy existing TensorBoard, MLflow, Weights & Biases, CSV, Lightning and Hugging Face runs into the local store with agenticml import.
sidebar:
  order: 5
---

`agenticml import` reads another tracker's logs and writes each run into the local store. After that you can browse the runs with `agenticml ui` and upload them with `agenticml sync`.

:::note[New in 3.4]
`agenticml import` and the `wandb` extra are new in 3.4. Older versions have no `import` command.
:::

```bash
agenticml import tensorboard ./logs
agenticml import mlflow ./mlruns
agenticml import wandb ./wandb
agenticml import wandb my-team/my-project
agenticml import csv ./lightning_logs
```

The source is only read. Nothing in it is changed, locked, or deleted, and you can keep using the old tracker. Importing needs no other tracker's package, except `wandb` for W&B cloud.

The command prints one line per run and a total:

```text
imported 20261128T100332-2867e0d9  tbx_basic  60 points, 1 images
imported 20261128T100333-2f6e0b36  torch_hparams  50 points, 1 images
skipped 20261128T100333-4055283a  torch_scalars/loss_train  (already imported)
failed run-20261128_000000-broken: WandbFormatError: unsupported .wandb format (wandb unknown): not a W&B data file (bad header)
2 imported, 1 skipped, 1 failed
```

A failed line starts with the run's name and then the reason, which begins with the error's class name. Reader warnings (for example "skipped 2 histogram tags") are printed to stderr as `warning:` lines. `--json` prints the whole report as one JSON object instead.

Runs go to the store in `AGENTICML_DIR` (default `~/.agenticml`). `--dir DIR` picks another store. Every source takes the same options, listed in [Configuration](/reference/configuration/#agenticml-import-options). `--step-column` and `--time-column` are only for `csv`, and they are ignored for `trainer_state.json`.

## TensorBoard

```bash
agenticml import tensorboard ./logs
```

`PATH` is a log directory. Every directory under it (and `PATH` itself) that holds `*tfevents*` files is one run. The run name is the directory's path relative to `PATH`, as TensorBoard shows it. The project is the log directory's name unless you pass `--project`.

| | |
| --- | --- |
| Imported | Scalars (TF1 `simple_value` and TF2 `tf.summary.scalar` tensors with one number), images, and hparams (as config). The key is the TensorBoard tag. |
| Skipped | Histograms, audio, text, graphs, and other tensor summaries. Each kind is counted in a warning. Images in a format other than PNG, JPEG, GIF or WEBP. |
| Times | The run starts at the first event and ends at the last event. |

Notes:

- `SummaryWriter.add_scalars` writes one child directory per series, named `<main>_<sub>`. They import as separate runs, the same way TensorBoard lists them. The main directory `<main>` holds only a `file_version` event, so it also imports, as an empty run with 0 points and the note `(no points or media (config only))`. To leave it out, select the child runs: `--run '<main>/*'`.
- `SummaryWriter.add_hparams` writes a timestamped child directory with only the hparams and their metric values. When a child directory holds nothing else and its parent has events, it is merged into the parent: the hparams become config and the metric values become the summary. Any other child directory is a run of its own.
- A record with a bad checksum is skipped, and a file that is still being written is read up to its last complete record. A warning says when an event file changed in the last two minutes.
- The status is always `finished`. Event files do not say whether training ended.

## MLflow

```bash
agenticml import mlflow ./mlruns
agenticml import mlflow ./mlflow.db
agenticml import mlflow .
```

`PATH` is an `mlruns` directory (file store), a `.db` SQLite file, or a directory that holds `mlflow.db` or `mlruns/`. When both exist in a directory, `mlflow.db` is used and a warning says so; pass the `mlruns` directory to import that instead. SQLite is opened read-only, and the `mlflow` package is not needed.

| | |
| --- | --- |
| Imported | Metrics, params (as config), the run name, the note (as notes), and the start and end times. The project is the experiment name. |
| Config | Tags that do not start with `mlflow.` are stored under `config["mlflow.tags"]`, not as AgenticML tags. |
| Skipped | Deleted experiments and runs, the trash, artifacts (files and models), and runs that are still running. |
| Status | `FINISHED` is finished. `FAILED` and `KILLED` are crashed. `RUNNING` and `SCHEDULED` are skipped unless you pass `--include-running`. |

Notes:

- A nested run is linked to its parent (`parent_run_id`) when the parent is in the same import.
- `mlflow.log_metric` without `step` logs step 0 every time, so the order cannot be read from the steps. For a series whose points all share one step, the importer numbers the points 0, 1, 2, ... in time order and lists the key in `imported_from.renumbered_steps`.
- `NaN` and infinite metric values are kept as they are, in both stores.

## Weights & Biases, local runs

```bash
agenticml import wandb ./wandb
agenticml import wandb ./wandb/offline-run-20261128_155707-j7gesqrz
agenticml import wandb ./wandb/offline-run-20261128_155707-j7gesqrz/run-j7gesqrz.wandb
```

`PATH` is a `wandb/` directory (every `run-*` and `offline-run-*` child is a run), one run directory, one `.wandb` file, or a project directory that holds a `wandb/` directory. The importer reads the `.wandb` file itself, so the `wandb` package is not needed and offline runs work.

| | |
| --- | --- |
| Imported | History scalars (nested values get dotted keys, such as `eval.loss`), images, config, summary (numbers and text), tags, notes, the host, and the project. The run name is the W&B display name, or the run id. |
| Skipped | System metrics, histograms, tables, videos, and other typed history values, and summary values that are not numbers or text. Each is counted in a warning. Keys that start with `_` are not metrics. |
| Status | Exit code 0 is finished. Any other exit code is crashed. A run with no exit record is still running and is skipped unless you pass `--include-running`. |

Notes:

- A resumed W&B run writes a new directory with the same run id. Each directory is imported as its own run. The later ones are named `<run id>@<directory name>`.
- A file the importer does not understand fails that run with `unsupported .wandb format`. It is never imported in part.
- Image paths that point outside the run directory are skipped.

## Weights & Biases, cloud

```bash
pip install 'agenticml-lite[wandb]'
agenticml import wandb my-team/my-project
agenticml import wandb my-team/my-project/j7gesqrz
```

`PATH` is `ENTITY/PROJECT` for every run in a project, or `ENTITY/PROJECT/RUN_ID` for one run. A `PATH` that does not exist on disk and has this form is read as a cloud path. A path that exists on disk is always read as a local path.

The importer uses the `wandb` package and your own credentials: set `WANDB_API_KEY` or run `wandb login`. agenticml never reads, passes, or stores the key. Without the extra, the command exits with `W&B cloud import needs pip install 'agenticml-lite[wandb]'`.

| | |
| --- | --- |
| Imported | History scalars (nested values get dotted keys), images (downloaded to a temporary directory and removed afterwards), config, summary (numbers and text), tags, notes, and the project. |
| Skipped | System metrics (the history API does not return them), histograms, tables, videos, and other typed values. Each is counted in a warning. |
| Status | `finished` is finished. `crashed`, `failed`, `killed` and `preempted` are crashed. Every other state is treated as running and skipped unless you pass `--include-running`. |
| Times | The run starts at W&B's `created_at`. It ends at the summary's `_timestamp`, else the start plus `_runtime`, else the last heartbeat. |

This path is the only one that uses the network. It needs the W&B API to be reachable and your account to be able to read the project.

## CSV, JSONL, Lightning and Hugging Face

```bash
agenticml import csv ./train.csv
agenticml import csv ./metrics.jsonl
agenticml import csv ./lightning_logs
agenticml import csv ./output
agenticml import csv ./train.csv --step-column iter --time-column wall_clock
```

`PATH` is one of:

| `PATH` | What is imported |
| --- | --- |
| A `.csv` file | One run named after the file. The project is the file's directory name. |
| A `.jsonl` or `.ndjson` file | One run, one JSON object per line. Nested objects are flattened with `/` (`{"train": {"loss": 1}}` becomes `train/loss`) up to four levels deep. |
| A `trainer_state.json` file | One run from the Hugging Face `log_history` rows. |
| A directory | Every Lightning `metrics.csv` and every Hugging Face `trainer_state.json` under it. A plain `.csv` in a directory is not searched for: pass the file itself. |

A Lightning run is one `version_*` directory (`CSVLogger`). Its flat `hparams.yaml` entries become the config, and nested or unsupported entries are skipped with a warning. The run name is the directory's path under the searched directory, such as `lightning_logs/version_0`, and the project is the directory above it.

A Hugging Face run is the output directory that holds `trainer_state.json`. `step` is the step, every other numeric key (including `epoch`) is a metric, and these setup fields become config: `train_batch_size`, `max_steps`, `num_train_epochs`, `logging_steps`, `eval_steps` and `save_steps`. When an output directory holds `checkpoint-N` copies of the file, the output directory's own file is used, or else the newest checkpoint's.

Column rules for CSV and JSONL:

| | |
| --- | --- |
| Step | `--step-column`, else the first of `step`, `global_step`, `epoch` that exists, else the row number starting at 0. A row whose step is empty or not a whole number is skipped and counted. |
| Time | `--time-column`, else the first of `timestamp`, `time`, `wall_time`. Unix seconds, milliseconds, microseconds and nanoseconds are told apart by size, and ISO 8601 text is accepted. Values before the year 2000 are not read as times. |
| Metrics | Every other column with a number. Empty cells are skipped, so Lightning's sparse rows work. Columns with only text are ignored and listed in a warning. The step and time columns are not metrics. |
| Names | Column names match without regard to case. A column named by `--step-column` or `--time-column` that does not exist is an error. For a single file, the command stops with exit code 2. When you search a directory, each file that fails is reported as a failed run (exit code 1) and the others are still imported. |

If there is no time column, the file's modification time is used as the start time and as the time of every point, and a warning says so. The run then appears at the date you last wrote the file, not when it trained. `trainer_state.json` has no times, so it always works this way, and `--step-column` and `--time-column` are ignored for it (a warning is printed). For CSV and JSONL files, pass `--time-column` when the file has a time column with another name. The run id does not depend on that time, so touching or re-saving the file in place does not make a second run.

The status is always `finished`.

## Re-importing and duplicates

Each imported run has an id of the form `<start time>-<8 hex digits>`. The digits come from the source, the source's location, and the run's id inside the source. Importing the same run again gives the same run and is skipped:

```text
skipped 20261128T100332-2867e0d9  tbx_basic  (already imported)
```

The id depends on the `PATH` you give. Importing the same run through a different `PATH` (a subdirectory of the log directory, or a copy of the logs in another directory) makes new runs. `--project` and `--tag` are not part of the id: re-importing with other values is reported as `already imported` and changes nothing unless you pass `--replace`.

| Option | What it does |
| --- | --- |
| `--dry-run` | Reads the source and prints what would be imported (`would import ...`). Nothing is written. Use it to check counts and warnings first. |
| `--replace` | Imports a run again and replaces the earlier copy. It only replaces runs that came from an import and have never been synced. A native run, or an imported run that was synced, is reported as `failed` (exit code 1). |
| `--run GLOB` | Imports only runs whose name or source id matches the glob. Repeat it to allow several. |
| `--tag T` | Adds a tag to every imported run. Repeat it for several. |
| `--project P` | Sets the project of every imported run. The default is the source's own project. |
| `--include-running` | Imports runs the source says are still running or unfinished. They are stored as finished. |

An import that was cut off (the run is still `running` in the store) is redone by the next import without `--replace`, unless it was already synced.

## What an imported run looks like

An imported run is a normal run in the store. The dashboard, `agenticml ls`, `agenticml show` and `agenticml export` work on it. Differences from a run tracked by agenticml:

- The status is `finished` or `crashed`. A run the source still shows as running is skipped, or stored as `finished` with `--include-running`.
- `created_at` and `finished_at` are the source's start and end times, and each point's time is the source's time, not the import time. When the source has no end time, `finished_at` is the time of its last point.
- The tags are the source's tags (W&B), the tags you pass with `--tag`, and `imported:<source>` (`imported:tensorboard`, `imported:mlflow`, `imported:wandb`, `imported:csv`).
- `meta.json` has an `imported_from` object with the source, its location, the run's id in the source, the source's own status, the import time, the agenticml version, and any `renumbered_steps`. See [the store format](https://github.com/agenticML/agenticml-lite/blob/main/docs/FORMAT.md).
- There is no code snapshot, no lineage, and no captured environment. The run has no parent unless MLflow gave it one.
- The hostname is the one the source recorded (W&B), or empty. It is never the machine that ran the import.
- Metric values are stored as float32, like any run. Images are thinned to 64 per key and, with Pillow installed, scaled down to 1024 px on the longer side (`AGENTICML_MEDIA_MAX_PER_KEY` and `AGENTICML_MEDIA_MAX_PX`).

Imported runs sync like any other run:

```bash
agenticml login --host https://your-server
agenticml sync --all
```

See [Syncing runs](/server/sync/). Once a run has synced, `--replace` no longer replaces it.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Nothing failed. Skipped runs are not failures. |
| `1` | At least one run failed. The other runs were still imported. |
| `2` | The command could not start: the path does not exist, the path has nothing to import, the source is unknown, `--step-column` or `--time-column` was used with a source other than `csv`, a single CSV, JSONL or `trainer_state.json` file cannot be read (for example a missing `--step-column` column, or an empty or invalid file), the W&B extra is missing, W&B cloud sign-in fails or the project cannot be read, or the store directory cannot be used. |

A run that fails leaves no files in the store.

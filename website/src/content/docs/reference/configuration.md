---
title: Configuration
description: Environment variables that configure agenticml-lite.
sidebar:
  order: 1
---

## Store and service options

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTICML_DIR` | `~/.agenticml` | Store root. `init(dir=)` and `--dir` win. |
| `AGENTICML_MODE` | `local` | `local`, `offline` (alias of `local`), or `disabled`. Other values warn and use `local`. |
| `AGENTICML_HOST` | unset | Server for `login`, `whoami`, and `sync`. |
| `AGENTICML_API_KEY` | unset | Key for `sync` and `whoami`. `login` ignores it. |
| `AGENTICML_CONFIG_DIR` | `~/.config/agenticml` | Credential directory. Wins over `XDG_CONFIG_HOME`. |
| `AGENTICML_MAX_BYTES` | 5 GiB | Warn when the local store grows past this. Nothing is deleted automatically. |
| `AGENTICML_MEDIA_MAX_PER_KEY` | `64` | Media rows kept per key; older rows are thinned by step. |
| `AGENTICML_MEDIA_MAX_PX` | `1024` | Longest side of a logged image, when Pillow is installed. |

## Run and capture options

Read by `agenticml run`. `agenticml.init()` also reads `AGENTICML_PROJECT`, `AGENTICML_TASK` and `AGENTICML_LINEAGE`; for the rest, pass the matching `init()` argument.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTICML_PROJECT` | repo or directory | Project name. |
| `AGENTICML_NAME` | entry script path | Run name. |
| `AGENTICML_TAGS` | unset | Comma-separated tags. |
| `AGENTICML_NOTES` | unset | Run launch notes. |
| `AGENTICML_CONFIG_FILE` | unset | Path to a JSON configuration file. |
| `AGENTICML_GROUP` | unset | Run group. |
| `AGENTICML_JOB_TYPE` | unset | Job type. |
| `AGENTICML_TASK` | inferred | Task name. |
| `AGENTICML_PARENT` | inferred | Lineage parent run ID. |
| `AGENTICML_ALLOW_CROSS_TASK` | `0` | Set to `1` to silence warnings when task differs from parent's task. |
| `AGENTICML_RUN_ID` | unset | Run ID to resume (requires `AGENTICML_RESUME`). |
| `AGENTICML_RESUME` | unset | Resume mode (`never`, `allow`, or `must`). |
| `AGENTICML_AUTO_TRACK` | `1` | Detect PyTorch model, optimizer, loss, and dataloader. |
| `AGENTICML_AUTO_METRICS` | `1` | Log `auto/loss`, `auto/lr`, and `auto/epoch`. |
| `AGENTICML_LOG_EVERY` | `1` | Frequency of auto metrics in optimizer steps. |
| `AGENTICML_SYNC_TENSORBOARD` | `1` | Mirror TensorBoard summary events. |
| `AGENTICML_CAPTURE_ARGS` | `1` | Capture `argparse` and Hydra arguments in config. |
| `AGENTICML_SYSTEM_METRICS` | `1` | Sample CPU, memory, and GPU usage (if psutil is installed). |
| `AGENTICML_SYSTEM_METRICS_INTERVAL` | `10.0` | System metrics sampling interval in seconds. |
| `AGENTICML_LINEAGE` | `1` | Snapshot Git lineage. |
| `AGENTICML_TRACK_CODE` | `1` | Snapshot imported source code files. |
| `AGENTICML_CAPTURE_ENV` | `1` | Capture environment metadata. |
| `AGENTICML_TRACK_FILES` | unset | Extra source files to track (`os.pathsep`-separated). |
| `AGENTICML_SOURCE_ROOTS` | unset | Extra source directories (`os.pathsep`-separated). |
| `AGENTICML_MAX_FILE_BYTES` | `10485760` | Per-file size cap (10 MiB) for code and lineage snapshots. |
| `AGENTICML_MAX_TOTAL_BYTES` | `104857600` | Total size cap (100 MiB) for code and lineage snapshots. |
| `AGENTICML_FLUSH_INTERVAL` | `2.0` | Background metric flush interval in seconds. |
| `AGENTICML_ALL_RANKS` | `0` | Set to `1` to track all distributed ranks (not only rank 0). |

## `agenticml import` options

`agenticml import SOURCE PATH [options]` copies runs from another tracker into the local store. `SOURCE` is `tensorboard`, `mlflow`, `wandb` or `csv`. `PATH` is a log directory or file; for `wandb` it can also be `ENTITY/PROJECT[/RUN_ID]`. `agenticml import` and the `wandb` extra are new in 3.4; until it is released, install from source. See [Import runs from other trackers](/guides/import/).

| Option | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--dir DIR` | `AGENTICML_DIR` | `~/.agenticml` | Local store root |
| `--project PROJECT` | none | the source's own project | Project for every imported run |
| `--tag T` | none | none | Add a tag to every imported run (repeatable). Every run also gets `imported:<source>` |
| `--run GLOB` | none | all runs | Import only runs whose name or source id matches the glob (repeatable) |
| `--replace` | none | off | Replace runs that were imported before. Runs that were synced are refused |
| `--include-running` | none | off | Import source runs that look unfinished, as finished. They are skipped without it |
| `--step-column COL` | none | `step`, then `global_step`, then `epoch` | `csv` only: column that holds the step. Ignored for `trainer_state.json` (a warning is printed) |
| `--time-column COL` | none | `timestamp`, then `time`, then `wall_time` | `csv` only: column that holds the time. Ignored for `trainer_state.json` (a warning is printed) |
| `--dry-run` | none | off | Read the source and report, write nothing |
| `--json` | none | off | Print the report as one JSON object |

Exit code `0` means nothing failed, `1` means at least one run failed, and `2` means the command could not start (bad path, unknown source, or an option that does not fit the source).

## Server options

Read by `agenticml server serve` and `agenticml server up`. The full list, with defaults, is in [Installation and deployment](/server/install/#configuration-options).

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTICML_SERVER_ADMIN_EMAILS` | unset | Comma-separated emails granted server admin at their next verified Firebase sign-in. Same as `--admin-emails`. Ignored without Firebase. |
| `AGENTICML_SERVER_CONTACT_EMAIL` | unset | Where the dashboard's contact button writes: unset shows a Feedback button to the agenticml-lite team, an address shows Contact us for that address, `off` shows no button. Same as `--contact-email`. Read only by `agenticml server serve`; `agenticml server up` ignores it and always shows the Feedback button. |
| `AGENTICML_SERVER_DB_CONNECTIONS` | unset (`89`) | Most Postgres connections one server process opens (8 to 1000). Same as `--db-connections` (only `agenticml server serve`). Ignored with SQLite. |
| `AGENTICML_SERVER_METRICS` | `off` | Capacity metrics mode: `off` or `cloudwatch`. Same as `--metrics` (only `agenticml server serve`). |
| `AGENTICML_SERVER_S3_ENDPOINT_URL` | unset | Custom S3 endpoint, for MinIO, LocalStack or a VPC endpoint. |
| `AGENTICML_SERVER_S3_KMS_KEY_ID` | unset | AWS KMS key ID or ARN. Uploads use `aws:kms` encryption instead of `AES256`. |

## Rules

- A CLI flag overrides its environment variable; a blank environment variable counts as unset.
- Repeatable CLI options (`--track-file`, `--source-root`) replace rather than extend the corresponding environment variable list.
- Boolean switches treat `0`, `false`, `no`, and `off` as off.
- `agenticml.init()` defaults `auto_metrics` and `sync_tensorboard` to off; `agenticml run` turns them on.
- There is no positive override flag: once a feature is disabled via its environment variable (such as `AGENTICML_SYSTEM_METRICS=0` or `AGENTICML_ALL_RANKS=1`), the CLI cannot re-enable it; unset the variable instead.
- See [Zero-code runs](/guides/zero-code/#options-and-environment-variables) for details on CLI option syntax.

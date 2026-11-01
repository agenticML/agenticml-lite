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
| `AGENTICML_MODE` | `local` | `local`, `offline` (alias of `local`), or `disabled`. |
| `AGENTICML_HOST` | `https://api.agenticml.xyz` | Server for `login`, `whoami`, and `sync`. |
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

## Rules

- A CLI flag overrides its environment variable; a blank environment variable counts as unset.
- Repeatable CLI options (`--track-file`, `--source-root`) replace rather than extend the corresponding environment variable list.
- Boolean switches treat `0`, `false`, `no`, and `off` as off.
- `agenticml.init()` defaults `auto_metrics` and `sync_tensorboard` to off; `agenticml run` turns them on.
- There is no positive override flag: once a feature is disabled via its environment variable (such as `AGENTICML_SYSTEM_METRICS=0` or `AGENTICML_ALL_RANKS=1`), the CLI cannot re-enable it; unset the variable instead.
- See [Zero-code runs](/agenticml-lite/guides/zero-code/#options-and-environment-variables) for details on CLI option syntax.

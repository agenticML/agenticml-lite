# agenticml-lite

agenticml-lite is a local-first experiment tracker. Training writes metrics, config, code, and media to files on disk, and `agenticml ui` serves a TensorBoard-style dashboard from those files. There is no server, database, or Docker image to run. Upload to [AgenticML](https://agenticml.xyz) is optional, with `agenticml login` and `agenticml sync`. Install the package as `agenticml-lite` and import it as `agenticml`. The API is pre-alpha (`0.1.0.dev0`).

## Install

```bash
pip install agenticml-lite

pip install "agenticml-lite[system]"   # psutil: CPU, memory, disk
pip install "agenticml-lite[gpu]"      # NVIDIA GPU metrics via pynvml
pip install "agenticml-lite[media]"    # Pillow, for numpy and PIL images
```

## Quickstart

```python
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

`init(project, name, ...)` starts the one active run. `log` advances the step when `step` is omitted; `commit=False` merges later metrics into the same step. `config` and `summary` are dicts on the active run. `init(verbose=True)` prints the run id, the run directory, and the `agenticml ui` command.

`init` also turns on runtime code snapshots and PyTorch component detection (`auto_track=True`). Automatic loss / learning-rate / epoch metrics and TensorBoard mirroring stay off until you pass `auto_metrics=True` and `sync_tensorboard=True`. System metrics are on (every 10 seconds) when `psutil` is installed; `system_metrics=False` disables them.

```python
agenticml.log({"pred": agenticml.Image("pred.png", caption="val")})
agenticml.log_artifact("model.pt", name="checkpoint")          # reference
agenticml.log_artifact("model.pt", name="checkpoint", copy=True)  # snapshot bytes
agenticml.track_model(model)  # after the object exists
```

`Image` accepts a path, bytes, a file object, a PIL image, or a numpy array. Numpy arrays and PIL images need the `media` extra. `track_optimizer`, `track_loss`, and `track_dataloader` work the same way as `track_model`.

## Zero-code

`agenticml run` executes a script inside a run. The script can omit `import agenticml`.

```bash
agenticml run --project demo -- python train.py --lr 1e-3
agenticml python train.py --lr 1e-3          # same interpreter, or re-exec
agenticml run -m mypkg.train --epochs 3
agenticml run -- python examples/train_toy.py --lr 0.05 --epochs 5
```

Options go before the script; everything after it is the script's own argv. See `agenticml run -h`. `--project`, `--name`, `--tags`, `--notes`, `--group`, `--job-type`, `--mode`, `--dir`, and `--log-every` read `AGENTICML_PROJECT`, `AGENTICML_NAME`, `AGENTICML_TAGS`, `AGENTICML_NOTES`, `AGENTICML_GROUP`, `AGENTICML_JOB_TYPE`, `AGENTICML_MODE`, `AGENTICML_DIR`, and `AGENTICML_LOG_EVERY`. The off switches are in the table. `--verbose` has no variable.

| What | Where it comes from | Turn off |
| --- | --- | --- |
| Config | `argparse` parses of `sys.argv`, and `@hydra.main` | `--no-capture-args` (`AGENTICML_CAPTURE_ARGS=0`) |
| Code snapshot | imported project modules and files opened for reading | always on under `agenticml run` (`track_source=False` on `Run`) |
| Model, optimizer, loss, dataloader | PyTorch construction hooks | `--no-auto-track` (`AGENTICML_AUTO_TRACK=0`) |
| `auto/loss`, `auto/lr`, `auto/epoch` | `backward()` and `optimizer.step()` | `--no-auto-metrics` (`AGENTICML_AUTO_METRICS=0`); `--log-every N` |
| TensorBoard | `SummaryWriter` / `tensorboardX` (scalars, images, figures, hparams) | `--no-tensorboard` (`AGENTICML_SYNC_TENSORBOARD=0`) |
| System metrics | psutil, plus pynvml when installed | always requested under `agenticml run` (needs the `system` extra) |

Loss is unscaled under `GradScaler` and averaged over gradient-accumulation steps. Several optimizers are prefixed (`auto/<name>/loss`, `auto/<name>/lr`); `auto/epoch` stays unprefixed. TensorBoard event files are still written. Keys passed to `init(config=...)` win over captured hyperparameters.

A script that calls `agenticml.init()` joins the launcher's run. Exit 0 finishes as `finished`; any other exit finishes as `crashed`, with `_exit_code` and `_exception` in the summary. The script's exit code is preserved. SIGTERM finishes the run and exits 143.

`examples/train_toy.py --explicit` is the same loop calling the Python API itself. See [examples/README.md](examples/README.md).

## Viewing

```bash
agenticml ui          # http://127.0.0.1:6006
agenticml ls          # newest first
agenticml ui --open --port 6006 --host 127.0.0.1
agenticml ls --project demo --json
```

`agenticml ui` binds to `127.0.0.1` and port 6006. On loopback it answers only loopback `Host` headers. `--dir` selects the store (else `AGENTICML_DIR`, else `~/.agenticml`). `ls` prints run id, project, name, status, created time, and last step. A run still marked `running` is listed as `stale` once `meta.json` and `scalars/_time.bin` are both older than 120 seconds.

## Where data lives

The store root is `init(dir=...)`, else `AGENTICML_DIR`, else `~/.agenticml`. Layout (details in [docs/FORMAT.md](docs/FORMAT.md)):

```
<root>/
  objects/<hh>/<sha256>          content-addressed bytes, shared by every run
  runs/<run_id>/
    meta.json                    project, config, status, summary, env, git
    events.jsonl                 config updates, code, artifacts, finish
    sync.json                    per-host upload cursor, after a sync
    scalars/<id>.bin             8 bytes per point: uint32 step, float32 value
    scalars/<id>.ovw             overview, at finish, if the series has > 65536 points
    scalars/_time.bin            one record per scalar flush
    scalars/index.json
    media/index.json
```

New run ids look like `20261027T021500-ab12cd34` (UTC). Each scalar point is 8 bytes in `<id>.bin`. An overview is written at finish only when a series has more than 65,536 points, and it is at most 40 + 36×2048 = 73,768 bytes. Worst-case overview overhead is about 14% of the raw series (the 73,768-byte cap next to a 65,537-point `<id>.bin`); the share falls as the series grows, to about 0.6 B/pt past ~120,000 points. At or below 65,536 points the reader builds the overview from `<id>.bin` on demand. TensorBoard tfevents are ~51 B/pt, and the old JSONL journal was ~57 B/pt.

Code snapshots and media are stored once, keyed by SHA-256. The same bytes logged again share one object. Default snapshot caps are 10 MB per file and 100 MB total. Media rows are kept as logged (one row per call). Artifacts are a path, size, and mtime unless `copy=True`, which hashes the bytes into `objects/`.

## Disk

```bash
agenticml du
agenticml gc --dry-run
agenticml gc --older-than DAYS --keep-last N
agenticml gc --include-unsynced
agenticml sync --all --prune
```

`agenticml du` reports how much space the local store uses. `agenticml gc` removes runs. Only synced runs are eligible unless you pass `--include-unsynced`. `--older-than` and `--keep-last` narrow the set. `--dry-run` prints the plan and deletes nothing. `agenticml sync --prune` drops raw `<id>.bin` files for series that already have an overview; series of 65,536 points or fewer keep their `.bin` (≤ 512 KB each). A pruned run cannot be resumed. Agenticml warns when the store exceeds `AGENTICML_MAX_BYTES` (default 5 GiB).

## Upload

```bash
agenticml login --host https://api.agenticml.xyz
agenticml whoami
agenticml logout
agenticml sync RUN_ID [RUN_ID ...]
agenticml sync --all
agenticml sync --all --watch 30 --dry-run
agenticml sync RUN_ID --reset
```

`login` checks the key (skip with `--no-verify`) and writes `~/.config/agenticml/credentials.json` at mode `0600`. The directory is `AGENTICML_CONFIG_DIR`, else `$XDG_CONFIG_HOME/agenticml`, else `~/.config/agenticml`. The default host is `https://api.agenticml.xyz`. `login` prompts for the key (`--key`, or stdin). `sync` takes the key from `--api-key`, then `AGENTICML_API_KEY`, then the file. `whoami` uses `AGENTICML_API_KEY`, then the file. `whoami --offline` skips the server check. `logout` removes the saved key for one host. On `login`, `whoami`, and `sync`, `--insecure` allows `http://` to a non-local host; localhost does not need it.

Sync is incremental and resumable. A cursor in `runs/<id>/sync.json` stores scalar byte offsets and which code, media, and artifacts were sent. An interrupted sync retries at most the in-flight round. `--all` selects runs that are incomplete or changed. `--watch N` repeats every N seconds until every selected run has synced, failed permanently, or gone stale. `--dry-run` prints counts without sending or saving state. `--reset` clears the saved remote id for explicit run ids on this host, and it rejects `--all`.

Uploaded: project, name, config, tags, notes, group, job type, env, git, the parent's remote id, finite scalar points, code files, media, artifacts, and the summary plus status at finalize. `copy=True` artifacts come from the object store. By-reference artifacts are read from the logged path when it is still a regular file with the same size and mtime.

Server-side gaps: `created_at` and `finished_at` are stamped by the server at sync time. Config keys cannot be deleted remotely, and `group` / `job_type` cannot be cleared (a null is omitted and the stored value stays).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTICML_DIR` | `~/.agenticml` | Store root. `init(dir=)` and `--dir` win. |
| `AGENTICML_MODE` | `local` | `local`, `offline` (alias of `local`), or `disabled`. Other values warn and use `local`. |
| `AGENTICML_HOST` | `https://api.agenticml.xyz` | Server for `login`, `whoami`, and `sync`. |
| `AGENTICML_API_KEY` | unset | Key for `sync` and `whoami`. `login` ignores it. |
| `AGENTICML_CONFIG_DIR` | `~/.config/agenticml` | Credential directory. Wins over `XDG_CONFIG_HOME`. |
| `AGENTICML_MAX_BYTES` | 5 GiB | Warn when the local store is larger than this. Nothing is deleted automatically. |
| `AGENTICML_MEDIA_MAX_PER_KEY` | `64` | Media index rows kept per key. Further rows are thinned by step. |
| `AGENTICML_MEDIA_MAX_PX` | `1024` | Longest side of a logged image, when Pillow is installed. |

`agenticml run` settings use the variables listed under [Zero-code](#zero-code). Boolean switches treat `0`, `false`, `no`, and `off` as off.

## Performance

From `python scripts/bench.py --quick` on one machine. Timings and bytes/point move with CPU, disk, and Python.

| Metric | This run | Target |
| --- | --- | --- |
| `import agenticml` (median of 5) | 39.4 ms | < 100 ms |
| `log()` ×10 scalars, median (10,000 calls) | 2.70 µs | < 10 µs |
| `log()` ×10 scalars, p99 | 8.91 µs | |
| disk, whole `scalars/` dir (100,000 points) | 8.01 B/pt | ≤ 9.0 B/pt (8 B raw + overview) |
| UI cold start, `/api/runs` (101 runs) | 8 ms | < 1 s |
| scalar request, 100,000-point series (median of 20) | 14.16 ms | < 20 ms |
| TensorBoard tfevents | not installed | |

The disk row is ten series of 10,000 points. Each series is under 65,536 points, so finish writes no overview and the directory stays at 8.01 B/pt, inside the 9.0 B/pt check. A series past 65,536 points adds one overview of at most 73,768 bytes. `python scripts/bench.py` without `--quick` uses 100,000 log calls, a 1,000,000-point run, and 1,000 small runs.

## Distributed, resume, disabled

Rank 0 logs. `init(only_main_process=True)` (the default) gives every other rank a no-op run, using `RANK`, `PMI_RANK`, `OMPI_COMM_WORLD_RANK`, `SLURM_PROCID`, then `LOCAL_RANK`. Put the tracker inside the launcher so every rank runs the script:

```bash
torchrun --nproc_per_node 4 -m agenticml run -- train.py
```

Resume with the same id. `resume="must"` errors if it is missing; `resume="allow"` opens it or creates it. The default `resume="never"` rejects an `id`. A pruned run (overview present, raw series file gone) cannot be resumed.

```python
agenticml.init(project="demo", name="exp1", id=run_id, resume="allow")
```

`mode="disabled"` or `AGENTICML_MODE=disabled` makes `log`, `finish`, artifacts, and the `track_*` helpers no-ops. With `verbose=True`, init prints `agenticml: tracking disabled`.

## Development

```bash
pip install -e ".[dev]"
pytest
python scripts/bench.py --quick
```

## License

MIT — see [LICENSE](LICENSE).

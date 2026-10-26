# agenticml-py

Python SDK for the [AgenticML](https://agenticml.xyz) experiment-tracking platform — metrics, config, code snapshots, artifacts, media, and system metrics.

> ⚠️ **Pre-alpha.** The public API is not yet stable. Pin exact versions if you depend on this.
>
> 📦 **Note on naming**: install as `agenticml-py`, import as `agenticml`.

## Install

```bash
pip install agenticml-py

# Optional extras
pip install "agenticml-py[system]"   # CPU/RAM/disk system metrics
pip install "agenticml-py[gpu]"      # NVIDIA GPU metrics
pip install "agenticml-py[media]"    # Image logging from numpy/PIL
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

## What you get

- **Module-level API**: `init / log / finish / config / summary / log_artifact`. One active run per process, like wandb. A `Run` class is also exported for multi-run cases and as a context manager.
- **Auto-incrementing step** with optional `commit=False` to merge metrics from multiple sources at the same step.
- **Runtime source snapshots**: project code actually imported and project files actually opened for reading. `track_model(model)` copies architecture `.py` (plus `agenticml/model.json`) without uploading `site-packages`. `track_optimizer` / `track_loss` / `track_dataloader` capture class identity and hyperparameters the same way. Content-addressed: re-runs only upload bytes the server doesn't have. 10 MB/file and 100 MB total caps by default.
- **Artifacts** with auto-versioning: `agenticml.log_artifact(path, name, type, metadata)`.
- **Media**: `agenticml.Image(data, caption)` accepts paths, bytes, PIL.Image, or numpy arrays.
- **System metrics**: psutil (CPU/RAM/disk) and pynvml (GPU) sampled in the background and logged as `_system/...`.
- **Resume**: `init(id=..., resume="allow"|"must")`.
- **Offline mode**: `mode="offline"` (or `AGENTICML_MODE=offline`) writes a journal locally; `offline_dir=` chooses the folder (else `AGENTICML_OFFLINE_DIR` / `~/.agenticml/offline`). `agenticml sync` replays it.
- **Distributed-aware**: standard rank env vars detected; non-rank-0 ranks become silent no-ops.

## Track a script without changing it

`agenticml run` executes a script inside a run. The script needs no
`import agenticml`: source snapshot, environment, and model / optimizer /
loss / dataloader detection all happen from outside.

```bash
agenticml run --project demo -- python train.py --lr 1e-3
agenticml python train.py --lr 1e-3            # shorthand; settings from AGENTICML_* env vars
agenticml run -m mypkg.train --epochs 3        # run a module, like python -m
```

Under a distributed launcher, put agenticml inside it so every rank runs the
script and only rank 0 tracks:

```bash
torchrun --nproc_per_node 4 -m agenticml run train.py
```

agenticml options go before the script; everything after the script is passed
to it untouched. See `agenticml run -h` for the full list; each option also
reads an `AGENTICML_*` env var.

Without any code in the script, a launched run records:

| What | Where it comes from | Turn off with |
| --- | --- | --- |
| Model, optimizer, loss, dataloaders | PyTorch hooks (see [source tracking](#source-tracking)) | `--no-auto-track` |
| `auto/loss`, `auto/lr`, `auto/epoch` at each optimizer step | `backward()` and `optimizer.step()` hooks; the loss is unscaled under `GradScaler` and averaged over gradient-accumulation steps | `--no-auto-metrics` (`--log-every N` to log every N steps) |
| TensorBoard scalars, images, figures, hparams | `SummaryWriter` / `tensorboardX` calls, mirrored at the same step; the event files are still written | `--no-tensorboard` |
| Command-line hyperparameters in `config` | `argparse` parses of `sys.argv` and `@hydra.main` configs | `--no-capture-args` |

With several optimizers, metrics are prefixed by the optimizer's name
(`auto/gen/loss`, `auto/disc/lr`). Code that calls `agenticml.init()` itself
can opt in with `init(auto_metrics=True, log_every=1, sync_tensorboard=True)`.

> Config captured after the run starts (argparse, Hydra, TensorBoard hparams,
> or `config=` passed to a script's own `init()` under the launcher) is kept on
> the run object but is not yet sent to the server, which only receives config
> when the run is created.

The run finishes as `finished` when the script exits with status 0 and as
`crashed` otherwise, with `_exit_code` and `_exception` in the summary. The
script's exit code is passed through, and SIGTERM is turned into a clean
finish (exit 143). If the script calls `agenticml.init()` itself, it joins the
launcher's run instead of starting a new one, and its `agenticml.finish()`
waits until the script returns.

`agenticml python ...` re-executes under that interpreter, so agenticml must be
installed in it.

## Configuration

| Env var                 | Default                       | Purpose                               |
| ----------------------- | ----------------------------- | ------------------------------------- |
| `AGENTICML_HOST`        | `https://api.agenticml.xyz`   | Server base URL                       |
| `AGENTICML_API_KEY`     | _(none)_                      | Sent in the `x-api-key` header        |
| `AGENTICML_MODE`        | `online`                      | `online`, `offline`, or `disabled`    |
| `AGENTICML_OFFLINE_DIR` | `~/.agenticml/offline`        | Fallback offline journal root         |

`init(..., offline_dir=...)` is **offline-only** and overrides `AGENTICML_OFFLINE_DIR`. Passing it with `mode="online"` or `mode="disabled"` raises `ValueError`. `init(..., verbose=True)` prints the run id and resolved asset paths (or host) to stdout after the run exists.

## Source tracking

Runtime-used tracking is enabled by default. AgenticML observes source modules
and input files while the run is active, then creates and uploads a SHA-256
manifest during `finish()`.

```python
agenticml.init(
    project="demo",
    name="training",
    track_source="runtime",             # default
    source_roots=["../shared_templates"],
    extra_files=["settings.yaml"],
)
```

| File category | Default behavior |
| --- | --- |
| Entrypoint and project-local imported Python modules | Tracked automatically |
| Project-local configs, templates, and other regular files opened for reading | Tracked automatically |
| Files below a configured `source_roots` directory that are imported or read | Tracked automatically |
| Files passed through `extra_files` or `track_files()` | Tracked explicitly |
| Architecture source of an instantiated module via `track_model()` | Copied into the snapshot under `model/` plus `agenticml/model.json`; original env paths stay excluded |
| Optimizer, loss, and dataloader via `track_optimizer()` / `track_loss()` / `track_dataloader()` | Metadata JSON under `agenticml/` plus custom `.py` copies; `torch` stdlib source and dataset contents are not uploaded |
| Virtual environments, `site-packages`, `dist-packages`, and `__pypackages__` | Always excluded; package versions are captured separately |
| Packaging products (`*.egg*`, `*.dist-info`, wheels, `build/`, `dist/`) | Always excluded as generated installation artifacts |
| Other third-party packages outside the allowed roots | Excluded |
| Write-only/generated outputs | Excluded; use `log_artifact()` or `track_files()` |
| `.git`, virtual environments, caches, `node_modules`, ignored paths | Excluded |
| Files over the configured per-file or total size limits | Excluded and reported as skipped |

`track_source="repo"` retains the earlier full-repository walk with static AST
import discovery. `track_source=True` is a compatibility alias for
`"runtime"`; `False` disables source tracking. `.gitignore` and
`.agenticmlignore` apply to automatic discovery in runtime and repository
modes; an explicit `extra_files`/`track_files()` path overrides those patterns.

Call `track_model` after the object exists if you need the defining source of
one third-party (or local) architecture class. Site-packages remain excluded
from automatic scans; only the staged copies are attached. Optimizer, loss,
and dataloader objects use the same pattern — hyperparameters and class
identity, not weights, tensors, or dataset files. `init(model=)` /
`init(optimizer=)` are not supported; call the `track_*` helpers after the
objects exist:

```python
agenticml.init(project="demo", name="asr")
model = Wav2Vec2ForCTC.from_pretrained("facebook/wav2vec2-base-960h")
optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
criterion = torch.nn.CrossEntropyLoss()
agenticml.track_model(model)
agenticml.track_optimizer(optimizer)
agenticml.track_loss(criterion)
agenticml.track_dataloader(train_loader, name="train")
agenticml.track_dataloader(val_loader, name="val")
```

Resource reads before `agenticml.init()` cannot be observed, so pass those
files through `extra_files`. Imported modules already present in
`sys.modules` are still detected. Snapshot contents are read at `finish()`;
files deleted before then are listed as missing rather than uploaded.

See [Runtime source tracking](docs/runtime-source-tracking.md) for lifecycle,
safety, migration, and troubleshooting details.

## Offline mode

```python
agenticml.init(
    project="demo",
    name="asr",
    mode="offline",
    offline_dir="/data/experiments/agenticml",  # optional; else env / ~/.agenticml/offline
    verbose=True,  # print run id + journal/code/artifacts/media paths
)
```

Assets land in `<offline_dir>/<run_id>/` (`journal.jsonl`, `code/`, `artifacts/`, `media/`). `offline_dir` is rejected unless the resolved mode is `"offline"`.

```bash
AGENTICML_MODE=offline python train.py
# ...later, from a machine with network access:
agenticml sync --dir /data/experiments/agenticml --host https://api.agenticml.xyz --api-key $AGENTICML_API_KEY
```

## Development

```bash
git clone https://github.com/agenticML/agenticml.git
cd agenticml
pip install -e ".[dev]"
pytest
```

## Releasing

Releases are published to PyPI via GitHub Actions on tags matching `v*`:

```bash
# bump version in pyproject.toml and src/agenticml/__init__.py
git commit -am "release: v0.0.3"
git tag v0.0.3
git push --tags
```

## License

MIT — see [LICENSE](LICENSE).

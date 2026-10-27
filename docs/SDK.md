# agenticml SDK: quick test guide

A short guide for trying the SDK by hand. Install as `agenticml-lite`, import as `agenticml`. Runs are written to local files. No server or API key is needed until you choose to sync. The full reference is in the root [README](../README.md).

## 1. Set up

From the repo root:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # SDK + psutil + Pillow + pytest

# Keep test data out of ~/.agenticml
export AGENTICML_DIR="$PWD/.scratch-store"
```

Check it works:

```bash
python -c "import agenticml; print(agenticml.__version__)"
agenticml --help
```

## 2. First run

Save as `try_sdk.py`:

```python
import agenticml

run = agenticml.init(
    project="sdk-test",
    name="first-run",
    config={"lr": 0.01, "epochs": 5},
    tags=["manual"],
    verbose=True,                 # prints run id, run dir, and the ui command
)

for step in range(20):
    agenticml.log({"loss": 1 / (step + 1), "acc": step / 20}, step=step)

agenticml.summary["best_loss"] = 1 / 20
agenticml.finish()
```

```bash
python try_sdk.py
agenticml ls
agenticml ui            # http://127.0.0.1:6006
```

## 3. API cheat sheet

| Call | What it does |
| --- | --- |
| `agenticml.init(project, name, **opts)` | Start the active run. Returns a `Run`. |
| `agenticml.log(metrics, step=None, commit=True)` | Log a dict of scalars (and `Image`s). `step` auto-increments if omitted. `commit=False` merges into the same step. |
| `agenticml.config` | Dict-like config of the active run. `agenticml.config["lr"] = 0.1` |
| `agenticml.summary` | Dict-like summary of the active run. Set final values here. |
| `agenticml.log_artifact(path, name=None, type="file", metadata=None, copy=False)` | Record a file. By default only its path/size/mtime; `copy=True` stores the bytes. |
| `agenticml.Image(src, caption=...)` | Image from a path, bytes, file object, PIL image, or numpy array (the last two need Pillow). |
| `agenticml.track_files(*paths)` | Add extra files to the code snapshot. |
| `agenticml.track_model / track_optimizer / track_loss / track_dataloader(obj, name="default")` | Record PyTorch components. |
| `agenticml.get_active_run()` | The current run, or `None`. |
| `agenticml.finish(status="finished")` | End the run. Also called automatically at exit. |

A run is also a context manager: `with agenticml.Run("proj", "name") as run: run.log({...})`.

### Useful `init` options

`config`, `tags`, `notes`, `group`, `job_type`, `id` + `resume` (`"never"`, `"allow"`, `"must"`), `mode` (`"local"`, `"offline"`, `"disabled"`), `dir` (store root), `system_metrics` (`True`/`False`), `auto_track`, `auto_metrics`, `sync_tensorboard`, `verbose`.

## 4. Things to try

**Media and artifacts**

```python
agenticml.log({"sample": agenticml.Image("pred.png", caption="val")}, step=0)
agenticml.log_artifact("model.pt", name="checkpoint")               # by reference
agenticml.log_artifact("model.pt", name="checkpoint", copy=True)    # snapshot bytes
```

**Resume a run**

```python
run = agenticml.init(project="sdk-test", name="first-run")
run_id = run.run_id
agenticml.finish()

agenticml.init(project="sdk-test", name="first-run", id=run_id, resume="must")
```

**Disable tracking** (every call becomes a no-op):

```bash
AGENTICML_MODE=disabled python try_sdk.py
```

**No code changes** (the launcher injects a run; the script may omit `import agenticml`):

```bash
agenticml run --project sdk-test --name zero-code -- python try_sdk.py
agenticml run --no-auto-track --no-capture-args -- python try_sdk.py   # switch things off
```

**Crash behaviour**: raise an exception after `init()`. The run should show as `crashed` with `_exception` in its summary.

## 5. CLI cheat sheet

```bash
agenticml ls [--project P] [--json]       # list runs, newest first
agenticml ui [--port 6006] [--open]       # local dashboard
agenticml du                              # store disk usage
agenticml gc --dry-run                    # preview cleanup; nothing is deleted
agenticml login / whoami / logout         # credentials for upload
agenticml sync RUN_ID [--dry-run]         # upload (needs login)
```

## 6. Where the data is

```
$AGENTICML_DIR/                 (default ~/.agenticml)
  objects/                      shared, content-addressed files
  runs/<run_id>/
    meta.json                   config, status, summary, env, git
    events.jsonl                config updates, code, artifacts, finish
    scalars/*.bin               8 bytes per point (uint32 step, float32 value)
```

Format details are in [FORMAT.md](FORMAT.md). To reset after testing, `rm -rf "$AGENTICML_DIR"`.

## 7. Troubleshooting

- **Nothing in `agenticml ls`**: you probably wrote to a different store. Check `echo $AGENTICML_DIR`, or pass `init(dir=...)` / `--dir`.
- **`log` does nothing**: check `AGENTICML_MODE`. `disabled` silences everything.
- **`Image` fails with numpy or PIL input**: `pip install "agenticml-lite[media]"`.
- **No system metrics**: `pip install "agenticml-lite[system]"` (CPU/memory), `[gpu]` for NVIDIA.
- **A run shows `running` after a crash**: it is marked `crashed` once its lock is released. `agenticml sync` finalizes it.
- **Multi-GPU**: only rank 0 logs. Launch as `torchrun ... -m agenticml run -- train.py`.
- **Quick end-to-end sample**: `python examples/train_toy.py --explicit`.

## 8. Run the repo's own tests

```bash
pytest -q                           # SDK unit tests
cd testbed && .venv/bin/python -m pytest -q    # end-to-end testbed
```

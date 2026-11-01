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
| `agenticml.init(project, name, **opts)` | Start the active run. Returns a `Run`. Omit `name` and it defaults to the main script's path relative to the repo root (e.g. `scripts/train.py`), or its absolute path outside a repo (empty without a main file). |
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

`config`, `tags`, `notes`, `group`, `job_type`, `task`, `id` + `resume` (`"never"`, `"allow"`, `"must"`), `mode` (`"local"`, `"offline"`, `"disabled"`), `dir` (store root), `system_metrics` (`True`/`False`), `auto_track`, `auto_metrics`, `sync_tensorboard`, `lineage` (default `True`), `verbose`.

A run belongs to a task (e.g. `"asr"`, `"imagegen"`). Lineage graphs and parent
inference are per `(project, task)` instead of per project. Parent inference
never crosses tasks; an explicit `parent_run_id` may cross tasks. The shadow Git
repository is still one per project.

Task resolution at init checks, in order (first match wins, recorded as `task_source`):
- `explicit`: `agenticml.init(task="asr")` / `Run(task=...)` / `agenticml run --task asr`
- `env`: `AGENTICML_TASK`
- `parent`: the explicit parent's task, if available in the local store (new runs only)
- `marker`: nearest `agenticml.toml` with a top-level `task = "..."`, searched from
  the entry script's directory (cwd if none) up to and including the project repo root.
  Must be a regular, non-symlink file of at most 64 KiB; unreadable/invalid files are skipped.
- `entry`: the entry script's directory relative to the repo root, POSIX form
  (e.g. `asr/train.py` -> `asr`, `src/tts/train.py` -> `src/tts`). Not used when
  the script is at the repo root or outside it.
- none: `task` is null and the whole project is one stream (previous behaviour).

Values are stripped; empty, longer than 128 characters, or containing control,
format, or separator characters (Unicode categories C*, Zl, Zp) are ignored
(a warning for explicit/env) and resolution falls through to the next signal.
Resume keeps the stored task. Under `agenticml run`, an `init(task=...)` that
differs from the launcher run's task logs a warning and the launcher's run is kept.
Older runs without a `task` field: if their `name` is a relative `.py` path with
a directory part (the default run name, e.g. `asr/train.py`), the task is read as
that directory with source `legacy` (read-time only, nothing is rewritten);
otherwise they count as no task.

`lineage=True` captures the project code at init in a shadow Git repository
under the store root, including for projects without Git. The project is never
written to. Disable with `lineage=False` or `AGENTICML_LINEAGE=0` (`false`, `no`,
`off` also work). Resume preserves the original snapshot. `parent_run_id=`
selects an explicit parent, as does `agenticml run --parent RUN_ID` (env
`AGENTICML_PARENT`); a self-parent is ignored. New runs inherit the parent's
task unless `task=` / `--task` or a valid `AGENTICML_TASK` supplies one.
The CLI refuses a differing explicit `--task` or valid `AGENTICML_TASK` with
exit 2 unless `--allow-cross-task` or `AGENTICML_ALLOW_CROSS_TASK=1` is set.
The check applies only when the parent is readable, in the same project, and
has a task. Disabled mode skips this check. The Python API warns instead when
an explicit or environment task differs from an explicit parent's task in the
same project, unless `init(..., allow_cross_task=True)`.
Missing or unreadable parents are recorded with a warning; a parent in another
project also warns and leaves the child's project unchanged. Its task is not
inherited, and the child snapshot has no Git parent commit.
An invalid `--parent` / `AGENTICML_PARENT` is a usage error (exit 2).
`--parent` only applies to new runs: a resumed run keeps its stored parent,
with a warning if it differs. Under `agenticml run`,
an `init(parent_run_id=...)` that differs logs a warning and keeps the launcher's
run. Without an explicit parent, parent inference selects the latest strictly
earlier run with the same project, same task, and
same repo root; else the latest strictly earlier run with the same project and
task; else no parent (ordered by `(created_at, run_id)` strictly before the
current run). Parent inference never crosses tasks. `max_file_bytes` /
`max_total_bytes` also cap lineage (default 10/100 MiB); ignored and oversized
files are skipped before hashing. Capture has one 30-second deadline, including
waiting for the repository lock, and inspects at most 20,000 candidate paths.
Excess paths and individual path errors are recorded in `lineage.skipped`; these
produce a partial snapshot. `.agenticmlignore` is applied only when at most 1 MiB.
Git failures and deadline expiry are recorded in `meta.json` and do not interrupt
the run.

Overlapping runs resolve the parent's ref under the repository lock before
creating the child commit. If the parent has not published its snapshot yet,
the child commit has no Git parent; `lineage diff` resolves the parent run's ref
live once it becomes available. The stored snapshot is never rewritten.

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
agenticml ui [--port 6006] [--open] [--read-only]  # local dashboard
agenticml lineage checkout RUN_ID [--path DIR]  # detached shadow worktree
agenticml lineage diff RUN_ID [OTHER_RUN_ID]   # numstat vs other or parent
agenticml lineage ls [--project P] [--task T]  # run parent tree
agenticml du                              # store disk usage
agenticml gc --dry-run                    # preview cleanup; nothing is deleted
agenticml login / whoami / logout         # credentials for upload
agenticml sync RUN_ID [--dry-run]         # upload (needs login)
```

`lineage ls` groups output by project, then `[task]` (`[no task]` for none).
All lineage commands accept `--dir ROOT`; otherwise they use `AGENTICML_DIR`
or `~/.agenticml`. `gc` removes deleted runs' refs and default checkouts. Custom
`--path` checkouts are user-owned and always kept, including edits and new
commits; GC lists them in `worktrees_kept` and CLI output. Later collections
retry orphan-ref cleanup after earlier failures.

## 6. Where the data is

```
$AGENTICML_DIR/                 (default ~/.agenticml)
  objects/                      shared, content-addressed files
  lineage/<project-key>.git/     shadow snapshots
  worktrees/<project-key>/<run_id>/  on-demand checkouts
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

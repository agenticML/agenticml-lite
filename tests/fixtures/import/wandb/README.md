# W&B fixtures

Real W&B offline run directories used by `tests/test_import_wandb.py`.
`wandb/` holds two runs written by the real `wandb` client in offline mode
(nothing was sent anywhere):

- `good-run`: 30 history steps (`loss`, `acc`, a text value that is ignored),
  one image (`sample`, step 10, caption "step ten"), one histogram (step 20,
  skipped by the importer), config (`lr`, `layers`, `optimizer`), tags,
  notes, summary (`best_acc`, `note`), clean exit.
- `failed-run`: the same data, finished with `exit_code=1`.

Generated with:

| tool | version |
| --- | --- |
| wandb | 0.31.2 (Go core; the .wandb files carry `producer: 0.31.2+8bf2a2b7...`) |
| numpy | 2.5.5 |
| Pillow | 13.0.0 |
| Python | 3.12.13 |

The run directories were trimmed to what the importer reads: `logs/`, `tmp/`,
`files/requirements.txt`, `debug*.log` and the `latest-run` symlink were
removed. The run directory names embed the local time of generation, so a
regenerated fixture has different names and ids; the tests find the runs by
name, not by id.

## No machine data in the fixtures

This repository is public, so the `.wandb` files must not carry the hostname,
the user name, an absolute local path, an email, a git remote or the Python
executable path. The generator keeps them out with W&B's own settings instead
of scrubbing the files afterwards:

- `WANDB_X_DISABLE_MACHINE_INFO=true` and `WANDB_X_DISABLE_META=true` stop the
  client writing the `environment` record (OS string, `program`, `root`, `host`,
  `executable`, `args`), so the files have no such record;
- `WANDB_HOST=fixture-host` sets the `host` field of the `run` record;
- `WANDB_DISABLE_GIT` / `WANDB_DISABLE_CODE` keep the git remote and the code
  snapshot out; `WANDB_X_DISABLE_STATS` and `WANDB_X_SAVE_REQUIREMENTS=false`
  drop the system stats and the package list;
- the generator runs in an empty directory with a neutral name, `HOME` points
  at another neutral directory, and the environment is cleared (`env -i`).

Because the `environment` record is absent, the tests cover the reader's
start-time fallback for it with a hand-built record
(`test_start_time_falls_back_to_the_environment_record`).

Regenerate (scratch venv, nothing is installed into the project; save the
script below as `/tmp/generate.py` first):

```
uv venv /tmp/venv-wandb
uv pip install --python /tmp/venv-wandb/bin/python wandb==0.31.2 numpy Pillow
mkdir -p /tmp/fixture-work /tmp/fixture-home
cd /tmp/fixture-work
env -i PATH=/usr/bin:/bin HOME=/tmp/fixture-home \
    /tmp/venv-wandb/bin/python /tmp/generate.py /tmp/fixture-out
# then, from the repository root:
rm -rf tests/fixtures/import/wandb/wandb
cp -r /tmp/fixture-out/wandb tests/fixtures/import/wandb/wandb
```

Check the result before committing (every command must print nothing):

```
cd tests/fixtures/import
for f in $(find . -type f ! -name README.md); do strings -n 4 "$f"; done \
    | grep -i -e "$USER" -e "$(hostname)" -e "/home/" -e "/tmp/claude" \
        -e '[A-Za-z0-9._%+-]*@[A-Za-z0-9-]*\.[A-Za-z][A-Za-z]*'
grep -ral --exclude=README.md -e "$USER" -e "$(hostname)" -e "/home/" -e "/tmp/claude" .
```

`generate.py`:

```python
"""Generate <out>/wandb/ with real wandb in offline mode, free of machine data.

usage (run from a neutral empty directory, with HOME pointed at a neutral one):
    python generate.py OUT_DIR
Needs: pip install wandb numpy Pillow   (nothing is sent anywhere)
"""
import os
import shutil
import sys
from pathlib import Path

target = Path(sys.argv[1]).resolve()
target.mkdir(parents=True, exist_ok=True)
shutil.rmtree(target / "wandb", ignore_errors=True)
os.environ.update(
    WANDB_MODE="offline",
    WANDB_SILENT="true",
    WANDB_DIR=str(target),
    WANDB_CONSOLE="off",
    WANDB_DISABLE_CODE="true",
    WANDB_DISABLE_GIT="true",
    WANDB_HOST="fixture-host",
    WANDB_X_DISABLE_MACHINE_INFO="true",
    WANDB_X_DISABLE_STATS="true",
    WANDB_X_DISABLE_META="true",
    WANDB_X_SAVE_REQUIREMENTS="false",
)
import numpy as np
import wandb
from PIL import Image


def log_run(name, *, exit_code=0):
    run = wandb.init(
        project="demo-proj",
        entity="demo-team",
        name=name,
        config={"lr": 0.01, "layers": [64, 32], "optimizer": {"name": "sgd"}},
        tags=["baseline", "fixture"],
        notes="fixture run",
        reinit=True,
    )
    for step in range(30):
        data = {"loss": 1.0 / (step + 1), "acc": step / 30.0, "label": "text is ignored"}
        if step == 10:
            pixels = (np.arange(16 * 16 * 3, dtype=np.uint8) % 251).reshape(16, 16, 3)
            data["sample"] = wandb.Image(Image.fromarray(pixels), caption="step ten")
        if step == 20:
            data["hist"] = wandb.Histogram(np.arange(50))
        run.log(data)
    run.summary["best_acc"] = 0.97
    run.summary["note"] = "done"
    run.finish(exit_code=exit_code)


log_run("good-run")
log_run("failed-run", exit_code=1)

# Trim what the importer does not read (logs, requirements, symlinks, loose files).
wandb_dir = target / "wandb"
for path in list(wandb_dir.iterdir()):
    if path.is_symlink() or path.is_file():
        path.unlink()
        continue
    shutil.rmtree(path / "logs", ignore_errors=True)
    shutil.rmtree(path / "tmp", ignore_errors=True)
    (path / "files" / "requirements.txt").unlink(missing_ok=True)
```

## File format notes (checked against these files)

- 7 byte header `:W&B` + `0xBEE1` + version 0, then LevelDB log blocks of 32 KiB
  counted from the start of the file; the record checksum is `zlib.crc32`
  seeded with the CRC of the type byte (not CRC-32C).
- wandb 0.31 writes nested history, config and summary values as `nested_key`
  paths with a JSON leaf in `value_json`; older clients wrote one JSON object per
  top-level `key`. The importer reads both (the tests build the older layout by hand).

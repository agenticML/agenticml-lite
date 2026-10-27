# AgenticML testbed

A user's ML project consuming `agenticml-lite`: seeded two-class blobs, a tiny
pure-Python logistic regression model, JSON checkpoints, and real API, console,
and dashboard HTTP tests. No numpy, torch, external service, or API key is needed.

From this directory, set up once (the supplied `.venv` is already ready):

```bash
uv venv --python 3.10
uv pip install -e '..[dev]' playwright==1.62.0
uv pip install -e '.[test]'
# pip equivalent, after creating/activating a venv:
# python -m pip install -e '..[dev]' playwright==1.62.0 -e '.[test]'
```

Run from `testbed/`:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q tests/test_training.py tests/test_init_api.py
.venv/bin/python -m pytest -q -m cli
.venv/bin/python -m pytest -q -m ui
.venv/bin/python -m pytest -q -m 'not browser'
PATH="$PWD/.venv/bin:$PATH" bash scripts/smoke.sh
```

The suite clears inherited `AGENTICML_*` settings and distributed-rank variables,
sandboxes `HOME`/`USERPROFILE`, uses temporary stores and credential directories,
and stops all server processes. Before sandboxing home it preserves Playwright's
real browser cache through `PLAYWRIGHT_BROWSERS_PATH` (or retains an explicit override).
The local fake upload server tests credentials and incremental sync without any
network service. Unsynced GC deletion is exercised only on disposable copies.

Train by hand and open the dashboard:

```bash
source .venv/bin/activate
export AGENTICML_DIR="$PWD/.agenticml"
export AGENTICML_CONFIG_DIR="$PWD/.config"
export XDG_CONFIG_HOME="$PWD/.config/xdg"
python -m dummytrain.train --explicit --project testbed --name baseline \
  --tags demo,baseline --out checkpoints/baseline
agenticml run --project testbed --name zero-code -- python -m dummytrain.train \
  --lr 0.25 --epochs 4 --out checkpoints/zero-code
agenticml ls
agenticml ui --port 0
```

Explicit training logs step loss and learning rate, epoch validation loss and
accuracy, a decision-boundary image when Pillow is installed, two checkpoint
artifact versions (reference and copied bytes), and final summaries. Without
`--explicit`, training never imports agenticml; the launcher captures arguments,
code, and available system metrics. Pure-Python zero-code training has no framework
hooks to capture loss automatically. Defaults are four epochs of eight steps.
`--fail-at-epoch 2` raises a RuntimeError before the second epoch;
`--exit-code 17` trains and saves, then exits 17.

`train(cfg, on_step=None, on_epoch=None)` accepts a dict or argparse Namespace.
Callbacks receive `(step, metrics)` and `(epoch, metrics)`; epochs start at one,
steps at zero. Its result includes the model, checkpoint path, loss history,
validation history, initial/final loss, and best validation accuracy.

Browser tests in `tests/test_dashboard.py` use these fixtures from `tests/conftest.py`:

- `cli(*args, env=None, input=None, timeout=60, check=True)` returns a text
  CompletedProcess. `module=True` selects `python -m agenticml`.
- Session `trained_store` exposes `.root` (Path) and `.runs` (name to run id).
  Names: `explicit-baseline`, `explicit-highlr`, `zero-code`, `crashed`, and
  `other-project`. The first four use project `testbed`; the last uses project
  `other-project`. All come from actual training subprocesses.
- Session `ui_server` exposes `.url`, `.root`, `.runs`, and `.proc`. It uses the
  trained store and an OS-selected loopback port. The URL has no trailing slash.

Browser authors should mark tests `browser` and install Chromium if needed with
`.venv/bin/python -m playwright install chromium`. Browser tests are not part of
this testbed's HTTP coverage. Read-only session fixtures should stay unchanged;
mutation tests use their own store and port-0 server via `ui_server_factory(root)`.

The smoke script always creates and cleans up its own temporary store, config,
and home, regardless of inherited tracking settings. It verifies both finished
runs before printing `SMOKE OK`.

CLI tests verify argument-capture off switches and retained code manifests.
`launch.py` does not persist resolved `auto_track`, `auto_metrics`,
`sync_tensorboard`, `log_every`, or mode settings in metadata/config/events or
verbose output, so this dependency-light testbed does not claim to observe these
settings from absent framework metrics or a successful default-local run.

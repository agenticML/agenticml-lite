# Open tasks

Unfinished work, highest priority first. See [AGENTS.md](AGENTS.md) for how
these are prioritized. When a task is done, delete its entry in the same
change that finishes it.

## Review and verify the `--parent` cross-task check

- **Where:** `_check_parent_task` in `src/agenticml/cli.py`,
  `RunSettings.allow_cross_task` in `src/agenticml/launch.py`, the cross-task
  warning in `Run.__init__` (`src/agenticml/run.py`), the new tests in
  `tests/test_launch.py` and `tests/test_lineage.py`, and docs (README,
  `docs/SDK.md`, `docs/FORMAT.md`).
- **Problem:** `agenticml run --parent X` with an explicit task (`--task` or a
  valid `AGENTICML_TASK`) that differs from X's task now exits with a usage
  error before the script runs, unless `--allow-cross-task` /
  `AGENTICML_ALLOW_CROSS_TASK=1` is given. `init(parent_run_id=..., task=...)`
  only warns. This was implemented but not code-reviewed, and only the
  targeted tests ran (`tests/test_launch.py tests/test_lineage.py
  tests/test_local_cli.py`: 280 passed). The rest of the `--parent` feature was
  reviewed and fully tested.
- **Fix:** review those parts of the branch's `--parent` commit (look for:
  `_check_parent_task` swallowing every exception, the mode check reading
  `AGENTICML_MODE` itself, a parent with no task counting as a mismatch, the
  Python-side warning firing when the CLI already allowed the cross-task
  link), fix findings, then run the full suites.
- **Done when:** review findings are addressed and
  `testbed/.venv/bin/python -m pytest -q tests` plus `.venv/bin/python -m
  pytest -q -m ui` (from `testbed/`) pass apart from the failures listed in the
  next entry.

## Fix tests that fail on `main` and the silently skipped browser tests

- **Where:** `tests/test_ui.py`, `tests/test_ui_limits.py`,
  `testbed/tests/test_dashboard.py`, `testbed/tests/conftest.py`.
- **Problem:** on macOS these fail on commit 353a91b, before any other change:
  `test_put_notes_trickled_body_hits_absolute_deadline`,
  `test_nonreading_clients_are_evicted_when_full`,
  `test_slow_writer_is_not_evicted_below_capacity` (socket timing), and
  `test_notepad_formatting_and_retry`,
  `test_notepad_blank_lines_undo_and_mobile_enter` (undo after a formatting
  change leaves `*word*` / `- [ ] \nabc`; the tests press `Control+z`, which
  is likely a no-op on macOS where undo is Cmd+Z, so try `ControlOrMeta+z`).
  Separately, `conftest.py` defaults
  `PLAYWRIGHT_BROWSERS_PATH` to `~/.cache/ms-playwright`, but on macOS
  Playwright installs to `~/Library/Caches/ms-playwright`, so about 137
  browser tests are skipped as "Chromium cannot launch" instead of running.
- **Fix:** only set `PLAYWRIGHT_BROWSERS_PATH` when that directory exists (or
  leave Playwright's default), then fix or loosen the five tests.
- **Done when:** `testbed/.venv/bin/python -m pytest -q tests` and
  `.venv/bin/python -m pytest -q -m ui` (from `testbed/`) pass on macOS with
  no Chromium skips.

## Send `task` and `arch` when syncing a run

- **Where:** `src/agenticml/sync.py` (`CONFIG_FIELDS` and the `/init` payload).
- **Problem:** runs now record `task`, `task_source`, and `arch` in
  `meta.json`, but sync uploads neither, so a remote server cannot group
  lineage by task.
- **Fix:** once the server accepts them, add `task`, `task_source`, and `arch`
  to the synced fields (keep older servers working if they reject unknown
  keys).
- **Done when:** a sync test asserts the three fields reach the payload and an
  older-server response does not fail the sync.
- **Blocked:** the remote server's `/init` schema is not in this repo, so we
  cannot tell yet whether it accepts these fields. Deferred until the cloud
  deployment work.

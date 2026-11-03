# Package 2: Run status from `remote.json`

Nov 3, 2026 · Supervisor plan · Release 2.0 · Coder: Cursor · Size: S

Source: [on-prem-server-plan.md](../on-prem-server-plan.md), section 4.6 and
section 7, package 2. Roadmap: [on-prem-roadmap.md](../on-prem-roadmap.md).

## Goal

A run on the server has no live writer and no `run.lock`, so today's
lock-and-heartbeat logic would show every running server run as `crashed`.
The server instead records the sync state in `runs/<id>/remote.json`
(written by package 1 on each commit). This package teaches the store reader
to derive a run's effective status from that file when it is present and
valid, and to expose `last_sync_at`, so the dashboard and CLI show the right
status on a server store. Local stores never contain `remote.json`, so local
behaviour does not change.

## Current code (Nov 3, 2026)

- `src/agenticml/store/reader.py:186` `_effective_status(meta, meta_path,
  run_path, now, *, lock=None)`: returns `meta["status"]` unless it is
  `running`, then decides `running` / `stale` / `crashed` from the run lock
  and the `run.lock` mtime (`STALE_AFTER_SECONDS = 120`).
- Callers, all of which must get the new behaviour without being edited:
  `list_runs` (reader.py:170, sets `meta["effective_status"]`, used by the
  dashboard run list and `agenticml runs`), `cli.py:237` (`show`),
  `amend.py:263` (`finish`), `sync.py:506` and `sync.py:869`,
  `store/gc.py:447`.
- `RunReader.meta()` returns `meta.json` unchanged; `tests/test_store.py:198`
  asserts it has no `effective_status`. Keep that.
- Helpers in `store/layout.py`: `read_file_nofollow`, `is_symlink_error`,
  `parse_timestamp` (accepts `Z`, naive = UTC, returns aware UTC),
  `META_NAME` and friends (line 60).

## `remote.json` contract (from plan section 4.6)

```json
{"owner": "gpu-box-1", "status": "running", "fingerprint": "...",
 "last_sync_at": "2026-11-03T12:00:00Z", "sizes": {"events.jsonl": 1234}}
```

Written atomically by the server only (package 1). Writers and the sync
client never create it. This package only reads `status`, `last_sync_at`
and `owner`; other fields are ignored.

## Files to touch

| Path | Change |
| --- | --- |
| `src/agenticml/store/layout.py` | Add `REMOTE_NAME = "remote.json"` next to `META_NAME` |
| `src/agenticml/store/reader.py` | Remote status logic, `RunReader.remote()` / `last_sync_at()`, `list_runs` adds `last_sync_at` |
| `docs/FORMAT.md` | `remote.json` in the tree; new `## remote.json` section; one line in "How to read" |
| `tests/test_store.py` | New tests (see "Tests") |
| `docs/plans/on-prem-roadmap.md` | Package 2 row: Plan links this file; Status `in review` |

No changes to `ui/`, `cli.py`, `sync.py`, `gc.py`, `amend.py` or the writer.

## Approach

1. **Constants** in `reader.py`:
   - `REMOTE_STALE_SECONDS = 600` (default window).
   - `REMOTE_STALE_ENV = "AGENTICML_REMOTE_STALE_SECONDS"`.
   - `_REMOTE_STATUSES = frozenset({"running", "finished", "crashed"})`.
   - `_REMOTE_FUTURE_SKEW_SECONDS = 60` (see "Edge cases").
2. **`remote_stale_seconds() -> float`** (public, module level). Reads the
   env var on every call (cheap; lets tests use `monkeypatch.setenv`). Accept
   any value that `float()` parses to a finite number `> 0`. Anything else
   (empty, non-numeric, `0`, negative, `nan`, `inf`) returns the default and
   logs one warning per distinct bad value (module-level set, like
   `_MISMATCH_WARNED`). Unset returns the default with no warning.
3. **`read_remote(run_path) -> dict | None`** (public, module level;
   `run_path` is `str` or `Path`). Reads `<run_path>/remote.json` with
   `read_file_nofollow` and returns a normalised dict, or `None` when the
   file is missing or invalid:
   - Missing (`FileNotFoundError`) → `None`, silently.
   - Symlink / other `OSError` → `None`; log a warning once per run path
     (symlink message like the existing "refusing symlinked meta.json").
   - Not UTF-8, not JSON, `RecursionError`, or not a JSON object → `None`,
     warn once per run path.
   - `status` not a `str` in `_REMOTE_STATUSES` → `None`, warn once.
   - Otherwise return `{"status": status, "last_sync_at": <str or None>,
     "owner": <str or None>}`. `last_sync_at` is the stored string when it
     is a `str` that `parse_timestamp` accepts, else `None` (no warning
     needed beyond the status effect below). `owner` is the stored value if
     it is a `str`, else `None`.
   - Use one module-level set `_REMOTE_WARNED` keyed by run path so a polling
     dashboard does not spam the log.
4. **`_remote_status(remote, now) -> str`** (private). Given the dict from
   `read_remote`:
   - `finished` / `crashed` → returned as is.
   - `running`: if `last_sync_at` is `None` → `stale` (the server says it is
     running but freshness cannot be shown). Else
     `age = now - parse_timestamp(last_sync_at).timestamp()`:
     - `age < -_REMOTE_FUTURE_SKEW_SECONDS` → `stale`;
     - `age < remote_stale_seconds()` (including small negative ages) →
       `running`;
     - otherwise → `stale`.
5. **`_effective_status`**: at the very top, before the existing
   `status != "running"` early return, call `read_remote(run_path)`. If it
   returns a dict, return `_remote_status(remote, now)`. Otherwise run the
   existing logic unchanged. The `lock` argument is ignored on the remote
   path. `meta_path` stays unused as today. This way every caller listed
   above gets the remote status with no edits.
6. **`list_runs`**: compute the remote dict once per run and reuse it for
   both values, so the file is read once per run per listing. Suggested
   shape: split the current function into `_effective_status` (public
   signature unchanged, reads remote itself) and an internal
   `_status_from(meta, run_path, now, remote, lock=None)` that both use;
   `list_runs` calls `read_remote` then `_status_from`. When the remote dict
   is not `None` and its `last_sync_at` is not `None`, set
   `meta["last_sync_at"]`; otherwise do not add the key (so local listings
   and `agenticml runs --json` output are byte-for-byte unchanged).
7. **`RunReader`**:
   - `remote(self) -> dict | None`: `read_remote(self.run_dir)`, re-read on
     each call like `meta()`. Path goes through `self._c(...)` for the
     containment check like the other files.
   - `last_sync_at(self) -> str | None`: from `remote()`.
   - Update the class/module docstring with one sentence on `remote.json`.
8. **`docs/FORMAT.md`**:
   - Tree: add `remote.json                  server-only sync status; never written locally`
     under `runs/<run_id>/` (keep the column alignment).
   - New section `## \`remote.json\`` after `## \`notes.md\``: who writes it
     (only `agenticml server`, atomically on each sync commit), the fields
     above, that local writers and sync never create or upload it, the status
     table from plan section 4.6 including the `stale` window env var and its
     default, the missing-`last_sync_at` and future-timestamp rules, and that
     an invalid file falls back to the lock-based rules.
   - "How to read": one sentence before the lock-based rules saying a valid
     `remote.json` takes precedence (link the new section).
   - Match the existing FORMAT.md voice: short declarative sentences, tables
     for mappings, no marketing.
9. **Roadmap**: package 2 row Plan → `[plan](packages/2-remote-status.md)`,
   Status → `in review` (supervisor does this when opening the PR; the coder
   does not edit the roadmap).

## Constraints and conventions

- Standard library only. Python 3.9 compatible (no `match`, no `X | Y` at
  runtime outside annotations; the module already has
  `from __future__ import annotations`).
- Reader stays read-only: never create, touch or lock `remote.json`.
- Follow the existing style of `reader.py`: module-level helpers with short
  docstrings, `logger.warning` with `%s` args, warn-once sets.
- Do not change the signature or behaviour of `_effective_status` for a run
  without `remote.json`. Existing tests must pass unchanged.
- `remote.json` takes precedence over `meta.json` status whenever it is
  valid, including when `meta.json` says `finished` and the remote says
  `running` (meta pushed, commit not yet arrived): the remote status is the
  committed state.

## Edge cases

| Case | Result |
| --- | --- |
| No `remote.json` | Today's lock-based logic |
| Empty file, invalid JSON, invalid UTF-8, JSON array/string/number | Lock-based logic, one warning |
| `status` missing, not a string, or unknown (`"paused"`, `"stale"`) | Lock-based logic, one warning |
| `remote.json` is a symlink or a directory | Lock-based logic, one warning |
| `finished` / `crashed`, any `last_sync_at` (even missing/garbage) | As is |
| `running`, `last_sync_at` within window | `running` |
| `running`, `last_sync_at` exactly at or beyond window | `stale` |
| `running`, `last_sync_at` missing, not a string, unparseable | `stale` |
| `running`, `last_sync_at` up to 60 s in the future | `running` |
| `running`, `last_sync_at` more than 60 s in the future | `stale` |
| `running`, naive timestamp without `Z` | Parsed as UTC (`parse_timestamp`) |
| `meta.json` says `finished`, remote `running` and fresh | `running` |
| `meta.json` says `running`, remote `finished` | `finished` |
| Env var unset | 600 s |
| Env var `"30"`, `"1.5"` | 30 s, 1.5 s |
| Env var `""`, `"abc"`, `"0"`, `"-5"`, `"nan"`, `"inf"` | 600 s, one warning per bad value |

## Tests (`tests/test_store.py`)

Build runs with `RunWriter.create(tmp_path, run_id=..., meta={})` and
`finish(...)` as the existing tests do, then write `remote.json` by hand.
Use a fixed `now` where possible: call `_effective_status` / `_remote_status`
directly with an explicit `now`, and also check `list_runs` end-to-end with
timestamps relative to `time.time()` (margins of minutes, not seconds, to
avoid flakes). Use `pytest.mark.parametrize` for the table rows.

1. Every row of the "Edge cases" table, through `_effective_status` and at
   least the main rows (running fresh, running stale, finished, crashed,
   invalid → fallback) through `list_runs()[...]["effective_status"]`.
2. Fallback rows assert the result equals what the same run gives with no
   `remote.json` (compute both, compare), so they do not depend on lock
   timing.
3. Custom window via `monkeypatch.setenv(REMOTE_STALE_ENV, "30")`: a sync
   45 s ago is `stale`, 15 s ago is `running`. Bad env values fall back to 600
   and log one warning (`caplog`), not one per call.
4. `RunReader.remote()` and `last_sync_at()`: valid file, missing file
   (`None`), invalid file (`None`), running with bad timestamp
   (`last_sync_at()` is `None`, `remote()["status"] == "running"`).
5. `list_runs`: `last_sync_at` present when the remote has a valid one;
   absent for local runs and for an invalid timestamp; `meta.json` on disk
   unchanged; `RunReader.meta()` still has neither `effective_status` nor
   `last_sync_at`.
6. Warn-once: listing a run with a corrupt `remote.json` three times logs
   one warning.
7. Symlinked `remote.json` (skip on Windows like the existing symlink tests)
   → fallback and a warning; the symlink target is not read.
8. A writer never creates it: after create / log / finish of a run,
   `remote.json` does not exist.

## Checks (coder, before handing back)

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root: all pass
  (baseline 1251 passed, 9 skipped, plus the new tests).
- `testbed/.venv/bin/python -m pytest -q tests/test_store.py tests/test_review_fixes.py`
  for a fast loop while working.
- If the testbed venv is missing, set it up as in `testbed/README.md`.
- Report: the diff summary, the new test count, any deviation from this plan.

The UI suite is not required (no dashboard code changes), but the
supervisor runs `-m ui` once as a guard because `list_runs` feeds the
dashboard.

## Done when

- Every row of the "Edge cases" table is covered by a test, including
  invalid JSON, wrong types, a future `last_sync_at` and a custom stale
  window, and the full root suite passes on Python 3.12 (CI also runs 3.9).
- `docs/FORMAT.md` documents `remote.json`.
- The roadmap row says `in review` with the plan linked, then
  `done (<PR link>)` after merge.

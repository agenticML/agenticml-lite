# Backlog fix: run-file opens must not block on a FIFO

Nov 5, 2026 · Supervisor plan · Release 2.0 · Coder: Codex · Base branch: `dev`

Source: the Backlog entry "Other run-file opens can still block on a FIFO" in
[on-prem-roadmap.md](../on-prem-roadmap.md), tagged "Before package 6".
Security-sensitive (†, input hardening): Sonnet reviews read-only, then Cursor
gives a second read-only review. The coder does not edit the roadmap; the
supervisor removes the entry once the fix is verified.

## Goal

No open of a run or object file can hang because the path is a FIFO (or any
other special file whose `open` blocks). Every caller fails fast with an
`OSError` (or the `FileNotFoundError` that `open_object` already raises for
bad objects) instead.

## What exists today

- `read_file_nofollow` (`src/agenticml/store/layout.py`) already does it
  right: `os.open` with `O_NONBLOCK | O_NOFOLLOW`, `fstat` the descriptor,
  refuse non-regular files with `EINVAL`, then `os.set_blocking(fd, True)`.
- `open_nofollow` (same file) opens without `O_NONBLOCK` and only checks
  `S_ISREG` after `os.open` returns. `O_RDONLY` on a FIFO with no writer
  blocks forever; `O_WRONLY`/append on a FIFO with no reader blocks forever.
  Callers: the store reader and writer (`events.jsonl`, scalar `.bin`,
  `_time.bin`, `.ovw`), `try_lock_run` / `is_run_locked` (`run.lock`), gc,
  lineage's `agenticml.lock`, server ingest.
- `open_object` (same file) has the same gap; the dashboard
  (`ui/server.py`) and sync (`sync.py`) use it.
- `_open_local` (`src/agenticml/sync.py`) does `lstat` + `S_ISREG` and then
  calls `open_nofollow`, so a FIFO swapped in between the two still blocks.

## Files to touch

- `src/agenticml/store/layout.py`
- `src/agenticml/sync.py` (`_open_local` only)
- `tests/test_store.py` (new tests next to `test_read_file_nofollow_refuses_fifo`)

## Approach

1. In `layout.py`, add one private helper, for example
   `_open_regular_fd(path, flags, mode=0o666) -> int`:
   - adds `O_NONBLOCK` (when `os` has it) and `O_NOFOLLOW` (when it has it;
     otherwise keep the existing `lstat` fallback checks of each caller, so
     behaviour on platforms without `O_NOFOLLOW` is unchanged),
   - `os.open(path, flags, mode)`,
   - `fstat` the fd; if not `S_ISREG`, close it and raise
     `OSError(errno.EINVAL, f"not a regular file: {path}")`,
   - if `O_NONBLOCK` was added, `os.set_blocking(fd, True)` so reads and
     writes on the regular file behave exactly as before,
   - closes the fd on any exception after open.
2. Use the helper in `open_nofollow`, `read_file_nofollow` and
   `open_object`. Keep each function's public behaviour and errors:
   - `open_nofollow` still returns `open(fd, mode, buffering=buffering)` and
     still raises `ELOOP` for symlinks.
   - `open_object` still turns every failure into `FileNotFoundError`
     (including the new `EINVAL` and the `ENXIO` below), and still refuses
     `st_nlink > 1`.
   - `read_file_nofollow` keeps its read loop.
3. Write-side FIFO: `O_WRONLY | O_NONBLOCK` on a FIFO with no reader fails
   with `ENXIO` before `fstat`. That is the fail-fast we want; let it
   propagate as `OSError` from `open_nofollow` (do not translate it), and as
   `FileNotFoundError` from `open_object` (read-only, so it will not occur
   there in practice).
4. `O_CREAT` paths (append mode, `run.lock`) must keep creating regular
   files as before; `O_NONBLOCK` has no effect on a regular file.
5. `_open_local` in `sync.py`: keep the `lstat` pre-check (it gives the
   clearer error early) but rely on `open_nofollow` for the race; add a
   one-line comment saying the open itself is non-blocking now. Keep the
   `st_nlink > 1` check.

## Constraints and conventions

- Standard library only, Python 3.9 compatible. `os.set_blocking` exists on
  POSIX since 3.5 and on Windows since 3.12; guard with the same
  `if nonblock:` pattern `read_file_nofollow` uses (`O_NONBLOCK` is absent on
  Windows, so it is never called there).
- Don't change any caller other than `_open_local`. Don't reformat the file.
- Match the surrounding style: short docstrings, sparse "why" comments.

## Edge cases

- FIFO with a writer attached: `O_RDONLY | O_NONBLOCK` succeeds, `fstat`
  says FIFO, we refuse with `EINVAL`. Good.
- Character devices (`/dev/null`-style): refused by `S_ISREG` as today.
- A symlink to a FIFO: refused by `O_NOFOLLOW` (`ELOOP`) as today.
- `is_run_locked` treats any `OSError` other than `FileNotFoundError` as
  "unknown" and logs once; a FIFO `run.lock` should therefore return `None`
  quickly. `try_lock_run` should raise `OSError` quickly.

## Tests (`tests/test_store.py`)

Use the existing `_call_with_timeout` helper so a regression fails instead
of hanging the suite, and skip when `os.mkfifo` is missing. Each test makes a
real run with `RunWriter.create`, replaces one file with a FIFO, and asserts
a fast failure:

- `open_nofollow` on a FIFO in read mode → `OSError` `EINVAL`; in append
  mode → `OSError` (`ENXIO` or `EINVAL`).
- FIFO at `events.jsonl`: reading the run's events through the reader
  (`load_events` or the public reader API) fails fast or falls back, and
  appending an event through the writer path fails fast with `OSError`.
- FIFO at a scalar `scalars/<id>.bin`: reading the scalar fails fast.
- FIFO at an object `objects/<hh>/<sha>`: `open_object` raises
  `FileNotFoundError` fast.
- FIFO at `run.lock`: `try_lock_run` raises `OSError` fast and
  `is_run_locked` returns `None` fast.
- FIFO swapped in for `_open_local`: `sync._open_local(path)` raises
  `OSError` fast.
- A regression check that normal regular-file reads/appends still work is
  covered by the existing suite.

## Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root: all
  green.

## Done when

The tests above pass, the full suite is green, and Sonnet and Cursor reviews
are clean.

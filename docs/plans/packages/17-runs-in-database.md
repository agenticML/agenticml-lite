# Package 17 †: Runs, sync state and notes in the database

Nov 7, 2026 · Supervisor plan · Release 3.0 · Coder: Cursor · Size: L

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), "Run files and
crash safety (package 17)" and the package summary. Builds on
[16-database-layer.md](16-database-layer.md) (`server/db/`, repositories,
migrations, one-time 2.x import). Package 18 adds `workspace_id` to every
table here.

† Security-sensitive: second read-only review by the coder that did not
write it.

## Goal

The server stops keeping run state in files next to the run. The database
becomes the source of truth for who owns a run, its sync status, how many
bytes of each run file are committed, and the run's dashboard note:

| 2.2 storage | 3.0 (this package) |
| --- | --- |
| `runs/<id>/remote.json` (owner, owner_key, status, fingerprint, last_sync_at, last_sync_key, sizes) | `runs` row + `run_files` rows |
| file sizes from `lstat` | `run_files.size` (committed bytes) |
| `runs/<id>/notes.md` on the server store | `run_notes` row |
| dashboard run list: open every `meta.json`, `remote.json`, `_time.bin` | one query on `runs` |

Run files (`meta.json`, `events.jsonl`, `scalars/*`, `media/index.json`,
`artifacts.json`) stay v1 files on disk. The local store, the SDK,
`agenticml ui` and sync protocol v2 do not change.

Done when:

- the sync tests pass with unchanged client behaviour (`test_sync.py`,
  `test_sync_follow.py`, `test_server_ingest.py`, `test_server_byref.py`,
  `test_server_lineage.py`; assertions that read `remote.json` are moved
  to the repository, keeping what they check);
- a crash injected between the file write and the database commit (test
  hook) leaves a run that the next sync completes, byte-identical to the
  client's files;
- `GET /api/runs` on the server lists 10,000 runs without opening any run
  directory or run file (test);
- no code path writes `remote.json` or server-side `notes.md`;
- a data directory from 2.x or from package 16 starts with its runs,
  owners, sync state and notes intact (import, below).

Out of scope: workspaces (18), Postgres and many processes (19, 20),
metric summaries (22), backup (23). Lineage refs stay in the shadow Git
repository (Git is their source of truth; no `lineage_refs` table).

## Deviation from the 3.0 plan (supervisor decision)

`artifacts.json` stays a run file on disk, tracked in `run_files` like
`meta.json`. It is content uploaded by the client through the ordinary
atomic-file route and read by the shared `RunReader` (local and server);
moving it into a table would fork the reader for no isolation or
consistency gain, since the atomic-file rule already makes it crash safe.
The 3.0 plan's `run_artifacts` row is dropped; the plan's table is updated
in this PR.

## Files to touch

- `src/agenticml/server/db/migrations/0002_runs.sql` (new).
- `src/agenticml/server/db/repos.py`: `RunRepo`, `NoteRepo` (or inside
  `RunRepo`).
- `src/agenticml/server/db/import2x.py`: add runs (below).
- `src/agenticml/server/ingest.py`: ownership, `_atomic`, `_append`,
  `_commit`, `_state` on the repository; append protocol below.
- `src/agenticml/server/runs.py` (new, optional): server-side helpers that
  build list rows from `meta.json` and reconcile the runs directory.
- `src/agenticml/server/users.py`: `attribution()` and
  `_name_owns_runs()` read `runs`.
- `src/agenticml/server/admin.py`: `status` and `verify` read `runs` /
  `run_files` instead of `remote.json` (minimal port; package 23 extends).
- `src/agenticml/ui/server.py`: in server mode (`self.server.auth is not
  None` today, or an explicit `run_index` attribute on the server), the
  runs list, run detail owner/last_sync/status, notes read/write/list and
  the lineage graph's status come from the server's run index. Local mode
  is unchanged.
- `src/agenticml/store/reader.py`: `list_runs(root, *, include_owner=False,
  remote_lookup=None)`: when given, `remote_lookup(run_id)` returns the
  normalised remote dict (same shape as `read_remote`) instead of reading
  `remote.json`. `RunReader` gets an optional `remote` provider the same
  way. The store package must not import the server package.
- `src/agenticml/cli.py`: `agenticml server reindex` (below).
- Tests: new `tests/test_server_runs_db.py`; rewritten parts of the server
  tests that read or plant `remote.json` / server `notes.md`.

## Schema `0002_runs.sql`

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  project TEXT,
  created_at TEXT,
  owner_user TEXT,          -- user name; not a foreign key (2.2: runs keep a removed owner's name)
  owner_key TEXT,
  status TEXT CHECK (status IS NULL OR status IN ('running','finished','crashed')),
  fingerprint TEXT,
  last_sync_at TEXT,
  last_sync_key TEXT,
  list_row TEXT NOT NULL,   -- JSON: the /api/runs fields derived from meta.json
  last_step INTEGER,
  launch_note TEXT,         -- meta.json "notes", for the note fallback
  updated_at TEXT NOT NULL
);
CREATE INDEX runs_created ON runs(created_at, run_id);
CREATE INDEX runs_project ON runs(project);
CREATE INDEX runs_owner ON runs(owner_user);

CREATE TABLE run_files (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  path TEXT NOT NULL,       -- allow-listed name, e.g. scalars/3.bin
  size BIGINT NOT NULL,
  sha256 TEXT,              -- atomic files only
  PRIMARY KEY (run_id, path)
);

CREATE TABLE run_notes (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
  body TEXT NOT NULL,       -- '' is an explicitly cleared note
  updated_at TEXT NOT NULL
);
```

`status IS NULL` is the 2.2 "pre-commit" remote (owner claimed by the
first `meta.json`, no commit yet).

## Design

### Run rows

- A `runs` row is created the first time `meta.json` is written for a run
  (same transaction as recording the file), with `owner_user` /
  `owner_key` from the principal, exactly where 2.2 wrote the claimed
  `remote.json`. If the row exists with no owner (adopted run), the meta
  write claims it, as 2.2 did.
- Every `meta.json` write recomputes `list_row`, `project`, `created_at`
  and `launch_note` from the uploaded bytes. Extract the row-building code
  in `ui/server.py` `/api/runs` (fields `run_id, project, name,
  created_at, finished_at, tags, group, summary, config, task,
  task_source, arch`, with the same `_text`, `_tags`, `_summary`,
  `_CONFIG_LIMIT`, `run_task`, `_lineage_arch` rules) into one function
  used by both the local list and the server index, so they cannot drift.
- `last_step`: updated whenever `scalars/_time.bin` is appended or
  replaced, from the last complete record of the committed bytes (one
  seek; reuse `reader`'s record decoding).
- Ownership (`_ownership`), the 409 payloads (`compat_owner`), `whoami`
  and `/state`'s `remote` dict are built from the row with exactly the
  2.2 shapes and rules (owner hidden fields for non-owners, etc.).

### Committed sizes and the append protocol

`run_files.size` is the number of committed bytes. Never hold a SQLite
write transaction during file I/O (it would block every other write in
the database, including sessions and key touches). On SQLite the existing
in-process `server.run_lock(run_id)` serializes a run (one process,
`server.lock`); package 20 replaces it with a row lock on Postgres. Put
this behind one repository/dialect call (for example
`run_index.file_guard(run_id, path)`) so 20 swaps the mechanism, not the
handler.

Append (`POST /v2/runs/<id>/append/<path>?offset=N[&truncate=1]`), under
the run lock:

1. Read the row's committed size `C` (missing row = 0, missing file row =
   0).
2. Validate against `C` instead of the file's `st_size`: `offset > C` →
   409 `{"size": C}`; overlap `[offset, C)` compared with the file's bytes
   as today (mismatch → 409 with `mismatch`); hard-link and symlink checks
   unchanged.
3. Truncate the file to `C` (drops any tail left by a crash), write the
   new bytes from `C`, fsync the file and the directory.
4. Test hook: `server._after_append_write(run_id, path)` (no-op by
   default) runs here.
5. One short write transaction: set the size to `C + new_bytes`, update
   `last_step` if the path is `scalars/_time.bin`, set `updated_at`.
   Commit. Answer `{"size": ...}`.

If step 3 fails, truncate back to `C` (as today) and answer 500; the
database is unchanged. If the process dies after 3 and before 5, the file
has bytes beyond `C`; the next append's step 3 removes them, and `/state`
reports `C`, so the client resends them.

`truncate=1` (offset 0): write the temp file, fsync, `os.replace`, fsync
the directory, then record `size = length`.

Atomic files (`PUT /v2/runs/<id>/files/<path>`): validate, temp + rename +
fsync (2.0 rule), then record `size` and `sha256` of the body (and the run
row changes for `meta.json`) in one transaction.

`/state`: `files` come from `run_files`: atomic files report the stored
`size` and `sha256` without reading them; append files report the
committed size and the SHA-256 of the file's first `size` bytes (as
`_hash_prefix` does today). A file shorter than its committed size (lost
on disk) is reported with its real size and logged once as an error.

`commit`: compare the client's `sizes` with the committed sizes (absent
row = absent file); 409 `size mismatch` with the committed sizes as 2.2
did. On success one transaction sets `status`, `fingerprint`,
`last_sync_at`, `last_sync_key`; the `run.finish` audit rule is unchanged.

### Notes on the server

- Server mode `read_note`, `write_note`, `list_notes` use `run_notes` with
  the exact dict shapes and rules of `store/notes.py`: revision is the
  SHA-256 of the stored UTF-8 bytes; no row → the launch note
  (`runs.launch_note`) with `source: launch`; an empty stored note stays
  empty; size cap `MAX_NOTE_BYTES`; compare-and-swap in one write
  transaction (`NoteConflict` with the current note). Reuse the helpers in
  `store/notes.py` (normalisation, clipping, revision) rather than copying
  them.
- Local `agenticml ui` keeps `notes.md` files.

### Dashboard on the server

- `GET /api/runs` (server mode): one `SELECT` ordered by `created_at` desc,
  `run_id` desc (match `reader._run_sort_key` ordering for equal or
  missing timestamps), merging `list_row` with `status` (effective),
  `last_step`, `last_sync_at`, `owner`. No file access.
- Effective status: for rows with a sync `status`, the 2.2
  `_remote_status` rule (running + recent `last_sync_at` → running, else
  stale). For rows without one (adopted, never committed): `meta.json`'s
  status from the list row, with `running` shown as `stale`. Keep every
  case the existing tests cover.
- Run detail, lineage graph, compare and anything else that called
  `read_remote` / `list_runs(include_owner=True)` / `RunReader.remote()`
  in server mode use the `remote_lookup` provider backed by the database.

### Reconcile and `agenticml server reindex`

- `reconcile(root, run_index)`: one `scandir` of `runs/` names, compared
  with the ids in `runs`. Valid run directories with a readable
  `meta.json` and no row are adopted (row with no owner, list row from
  `meta.json`, `run_files` from the on-disk sizes of allow-listed files,
  `sha256` for atomic ones). Rows whose directory is gone are deleted.
  Runs on server start (after the import) and from `agenticml server
  reindex [--data DIR] [--database URL]`, which also rebuilds every row's
  `list_row`, `last_step` and `launch_note` from the files (prints counts).
- Tests that seed run files directly in a running server's store call the
  reconcile helper (or `reindex`) instead of relying on file scans.

### One-time import (extends 16's `import2x`)

Guarded by the `server_settings` row `imported_runs_2x`, after 16's
import, in the same "rows commit, then rename" pattern:

- For each run directory: the reconcile adoption above, then, from a
  valid `remote.json`, `owner_user`, `owner_key`, `status`,
  `fingerprint`, `last_sync_at`, `last_sync_key` (invalid or symlinked
  `remote.json`: adopted without owner, one warning per run, as 2.2's
  reader treated it). Committed sizes are the on-disk sizes (2.x had no
  truncate rule; everything on disk was acknowledged).
- A server-side `notes.md` becomes a `run_notes` row (with the 2.2 read
  rules: special files ignored, size cap).
- After the commit, rename `remote.json` → `remote.json.2x` and
  `notes.md` → `notes.md.2x`; rename failures are logged and ignored.

### Users and admin

- `attribution(run_dir)` → `(owner_user, owner_key)` from `runs`;
  `_name_owns_runs(name)` → `SELECT 1 FROM runs WHERE owner_user = ?`.
- `server status`: owner, last sync and writer per run from `runs`;
  `server verify`: compare `run_files` with the files (shorter than
  committed → error; longer → warning "uncommitted tail", which the next
  append removes), hash atomic files against `sha256`. Keep the report
  shapes and existing test expectations where they still apply.

## Constraints and conventions

- Standard library only; all SQL in `server/db/`; same error messages and
  status codes as 2.2 on every `/v2` route; protocol bytes unchanged.
- Never hold a database write transaction across file I/O on SQLite.
- `store/` never imports `server/`.
- Keep the in-process run and object locks.
- No change to the local store, `agenticml ui`, or the client.

## Edge cases

- Crash tail: bytes beyond `C` on disk, then (a) a normal append from `C`,
  (b) a client that first asks `/state`, (c) a `truncate=1` append, (d) a
  commit (sizes compare against `C`, so it fails with 409 and the client
  resends), (e) dashboard reads of the file (readers tolerate a partial
  last record). Each has a test.
- Overlapping append whose overlap bytes lie partly in the crash tail:
  compare only `[offset, C)`; the tail is discarded.
- `meta.json` rewritten by a different user than the owner: 409 as today.
- A run directory deleted by an operator: reconcile drops the row; the
  detail page 404s.
- 10,000 runs: insert rows directly through the repository in the test
  (fast), with no run directories, and patch file-open functions to fail
  so any file access fails the test.
- Notes: conflict, too large, cleared note stays empty, launch note
  fallback, run without a row → 404 as today.
- Import: a 2.2 directory with synced runs, a run with a pre-commit
  `remote.json` (no `status`), a corrupt `remote.json`, a symlinked one, a
  server `notes.md`; second start does not re-import.

## Tests and checks

- New `tests/test_server_runs_db.py`: append protocol and crash hook
  (inject an exception in `_after_append_write`, then re-sync with the
  real client and compare files byte for byte), state from the database,
  commit size check against committed sizes, 10,000-run list without file
  access, notes in the database, reconcile and `reindex`, import cases.
- Existing suites pass:
  `PYTHONPATH=src testbed/.venv/bin/python -m pytest -q tests` from the
  repo root. List every test whose assertion you changed and why.
- Dashboard: `PYTHONPATH=../src .venv/bin/python -m pytest -q -m ui` from
  `testbed/` (the server dashboard pages must behave the same).

## Done when

All of the above pass, nothing writes `remote.json` or server `notes.md`,
and both reviews (Sonnet, and the second † review) are clean.

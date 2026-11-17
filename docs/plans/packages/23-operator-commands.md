# Package 23: Operator commands on the database

Nov 9, 2026 · Supervisor plan · Release 3.0 · Coder: Codex · Size: S–M

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), package summary 23
(changed Nov 7, 2026: packages 16–18 already import 2.x state), decision
D5. Builds on [21](21-s3-objects.md) (object backend) and
[22](22-metric-summaries.md) (`run_metrics`), so it runs after both.

## Goal

1. Prove the 2.x → 3.0 conversion end to end with a real 2.2 data
   directory, and refuse to convert twice.
2. `agenticml server backup --to DIR` makes a consistent copy of a running
   server.
3. `agenticml server status` and `verify` read the database and cover
   every workspace and both object backends.

Out of scope: restoring into Postgres (documented, not automated),
backups of S3 objects (S3 versioning / AWS Backup, package 24 runbook).

## Design

### 1. 2.x conversion end to end (`db/import2x.py`, `workspaces.py`)

- Test fixture `tests/fixtures/server-2.2/` is **generated**, not checked
  in: a helper in `tests/server_db_helpers.py` builds a 2.2-shaped data
  directory by writing the 2.2 files directly (`server/users.json` with an
  admin, a member and a viewer, `server/keys.json` with sync and admin
  keys bound to users, `server/audit.jsonl`, `runs/<id>/` with
  `remote.json`, `artifacts.json`, `notes.md`, run files, `objects/`,
  `lineage/<project>.git`). Copy the shapes from the 2.2 code that wrote
  them (`git show v2.2.0:src/agenticml/server/...` or the
  `legacy2x.py`/`import2x.py` readers) and from existing import tests in
  `test_server_db.py`/`test_server_workspaces.py`; reuse their builders
  where they exist rather than writing a second one.
- End-to-end test: start a 3.0 server on it (SQLite, then Postgres) →
  every user is an account with a membership in `default`, the earliest
  2.x admin is `owner`, every key works with its scope, audit events are
  listed, every run is in the runs list with its owner, sizes, status and
  note, run files moved under the workspace store, originals renamed
  `*.2x`; a 2.2 sync client (the current client with
  `AGENTICML_CLIENT_COMPAT=2.2` if such a switch exists; otherwise raw v2
  requests in the 2.2 shape) syncs more points to an imported run and a
  new run; the dashboard shows both; `server verify` is clean; metric
  summaries were backfilled (package 22).
- Refuse twice: after `imported_2x`/`imported_runs_2x` are set, if any of
  `server/users.json`, `server/keys.json`, `server/audit.jsonl` or a
  top-level `<data>/runs/` directory with run subdirectories reappears,
  `serve` (and every `server` subcommand that opens the database) stops
  with: `found 2.x <path> after this server was converted on <date>;
  move it away (the converted copy is <path>.2x)`. Today these are
  silently ignored; check what each code path does and make them all
  refuse, without touching the files.
- A conversion interrupted after the database commit but before the
  renames (kill between the two; use a test hook) completes on the next
  start without importing anything twice.

### 2. `agenticml server backup --to DIR` (`server/backup.py`, new; `cli.py`)

Flags: `--to DIR` (required; must not exist or be an empty directory; never
inside the data directory), `--data`, `--database`, `--objects`,
`--files-only` (Postgres), `--json`.

The rule that makes the pair consistent: the database records committed
sizes and every append truncates to the committed size first, so a copy
of the database taken **before** the files is consistent with any later
copy of the files whose run files are at least as long as the committed
sizes.

- SQLite (`sqlite:///...`): copy the database with `sqlite3`'s backup API
  (`src.backup(dst)`) to `DIR/server/agenticml.db` (mode 0600), while the
  server keeps running (no `server.lock`; the backup API reads a
  consistent snapshot under WAL).
- Postgres: refuse without `--files-only`, printing the exact `pg_dump`
  command shape to run first (`pg_dump --format=custom "$DATABASE_URL" >
  DIR/server.dump`, URL never printed) and noting RDS automated snapshots
  in the hosted service. With `--files-only`, read committed sizes from
  the live database at the start (the operator ran `pg_dump` before).
- Then, from the snapshot's `run_files` rows, for each workspace and run:
  copy append files truncated to the committed size; copy atomic files
  and check SHA-256 against the snapshot (on mismatch, re-read once; if
  still different, keep the newer file and add a warning — `/state`
  hashes atomic files from disk, so clients repair it); if a file is
  shorter than its committed size (a concurrent truncating replace),
  copy it and add a warning naming the run and path.
- Objects: file backend → copy every object listed in the snapshot's
  `objects` table (hard links are not used; copies are fsynced); S3 →
  skip with a note in the output (bucket versioning and AWS Backup cover
  them; package 24 enables both).
- Lineage: copy each workspace's `lineage/*.git` with `git clone --mirror
  --no-hardlinks` under the 2.1 hardening (`lineage._git` flags: hooks off,
  fixed config), or a plain file copy if that is simpler and safe; refs
  newer than the snapshot are harmless.
- Write `DIR/BACKUP.json`: version, created_at, database kind (never the
  URL), counts, warnings. Print a one-line summary, or the JSON with
  `--json`.
- Everything is created with the server's file modes (0600 files, 0700
  directories). Never follow symlinks (`layout.open_nofollow`, the store
  path checks already used by `admin.py`).
- Restore = run `agenticml server serve --data DIR` (SQLite) or restore
  the dump and point `--data` at DIR (Postgres). Document this in the
  command's `--help` epilog; package 25 adds it to `docs/SERVER.md`.

### 3. `status` and `verify` on the database (`admin.py`, `cli.py`)

Both gain `--workspace SLUG` (default: every workspace) and `--objects
URL`; both open the database read-only and never print its URL.

- `status`: per workspace — slug, status, plan, members, keys (as today,
  but per workspace), runs by status (from `runs`), stored bytes and
  upload bytes today (`workspace_usage`), objects count/size (`objects`
  table), lineage size (disk). The top-level "Sizes" line becomes totals
  across workspaces. The 2.2 single-workspace output shape stays when
  only `default` exists, so existing `test_server_admin.py` assertions
  still hold (adjust only where they assumed no workspace column).
- `verify`: per workspace —
  - run files: for every `run_files` row, the file exists, is a regular
    file, and is at least the committed size (shorter → problem; longer →
    warning "uncommitted tail; truncated on the next append"); atomic
    file SHA-256 matches; files on disk without a row → warning.
  - objects: every `objects` row exists in the backend with that size
    (file: lstat; S3: `size`), problem if missing or wrong size; objects
    in the backend but not in the table → warning (orphans, e.g. a
    process died between put and record); `--deep` re-hashes every
    object (file and S3).
  - references: every object referenced by a run (`run_references`, as
    today) is in that workspace's `objects` table.
  - lineage refs: as today, per workspace.
  - metric summaries: for every `run_metrics` row, recompute from the
    committed bytes and compare (problem on mismatch, with
    `server metrics rebuild` as the fix in the detail text).
- Exit code: 1 if any problem (as today).

## Edge cases

- Backup target inside the data directory, a symlink, or a non-empty
  directory → refuse before writing anything.
- A run deleted between the snapshot and the copy: skip with a warning.
- A workspace deleted between the snapshot and the copy: skip with a
  warning.
- Disk full during backup: stop, remove nothing already written, print
  where it stopped; exit 1.
- `verify` on S3 with thousands of objects: list the prefix with
  `list_objects_v2` pagination for sizes rather than one `head` per
  object.

## Tests and checks

- `tests/test_server_convert_2x.py` (new): the end-to-end conversion on
  SQLite and Postgres, the refuse-twice cases (each reappearing file),
  the interrupted conversion.
- `tests/test_server_backup.py` (new):
  1. A backup taken while a client is appending to several runs in a
     background thread (and one object upload) restores to a server that
     starts, passes `verify` with no problems, lists every run, and
     accepts a further sync of the same runs that ends equal to the
     source (run files byte-equal up to the synced size).
  2. Refusals: target exists and is non-empty, inside data, symlink;
     Postgres without `--files-only`.
  3. Postgres `--files-only` copies files and prints the `pg_dump` note
     without the URL.
  4. S3 objects are skipped with the note (moto).
- `tests/test_server_admin.py`: status/verify across two workspaces, a
  workspace filter, S3 objects (moto) including a missing object, an
  orphan and `--deep` hash mismatch, a short run file (problem), a long
  run file (warning), a wrong metric summary (problem), and that neither
  command prints the database URL or a password.
- Checks: `testbed/.venv/bin/python -m pytest -q tests`; the Postgres
  suite with `AGENTICML_TEST_DATABASE_URL`.

## Done when

A generated 2.2 data directory converts and syncs on both backends and
refuses a second conversion; a backup taken during a sync restores to a
working server; status and verify cover every workspace and both object
backends; all tests pass on SQLite and Postgres.

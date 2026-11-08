# Package 20 †: Many server processes

Nov 8, 2026 · Supervisor plan · Release 3.0 · Coder: Cursor · Size: M

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), "Many processes
(package 20)", exit criterion 3 and the package summary. Builds on
[19](19-postgres.md) (Postgres backend, advisory locks) and keeps
[18c](18c-signup-and-limits.md)'s counters correct across processes.

† Security-sensitive and crash-safety-critical: second read-only review by
the other coder (Codex).

## Goal

On Postgres, two or more `agenticml server serve` processes (on one host
or several, behind one load balancer) share one database and one data
directory (EFS in the hosted service). Syncing through any mix of them
gives the same result as one process, and killing a process mid-upload
loses nothing that was acknowledged. SQLite mode is unchanged: one process,
`server.lock`.

Out of scope: S3 (21), deployment (24), a shared code-diff cache (stays per
process), sharing the setup token across processes (Firebase/hosted has no
`/setup`).

## What is per process today, and what changes

| State | Today | Postgres mode after 20 |
| --- | --- | --- |
| `server.lock` (flock on `<data>/server/server.lock`) | refuses a second process | not taken |
| Run lock `(workspace, run_id)` | `threading.Lock` in `app.run_lock` | thread lock **plus** a session advisory lock in Postgres |
| Object lock `(workspace, sha[:2])` | `threading.Lock` | thread lock plus advisory lock |
| Lineage fetch lock | file lock in the lineage directory | file lock kept, plus advisory lock per (workspace, project key) |
| `RunRepo.file_guard` | no-op (caller holds run lock) | stays a no-op; the run lock is now cross-process |
| Login throttle | `DbBuckets`: counts in the DB, tickets (holds) per process | unchanged design; documented that holds are per process (a burst split across N processes can get N × the free attempts in flight, bounded by `FREE_FAILURES × N`); counts are shared |
| Sessions | DB | unchanged |
| 18c counters (requests/minute, uploads/day, storage) | DB, server clock | DB; minute and day keys from the **database clock** (`now()` / `CURRENT_TIMESTAMP`) |
| `server/tmp` cleanup at start | deletes everything in `server/tmp` | deletes only entries older than 1 hour (mtime), so a starting process never removes another process's in-flight upload |
| Run readers cache, code-diff cache, workspace stores cache | per process | per process; readers must not serve stale committed sizes (see below) |

## Design

### Cross-process locks (`db/postgres.py`, `app.py`)

- `PostgresDatabase.session_lock(key, timeout)`: a context manager that
  checks out a dedicated connection (outside the transaction pool count
  but bounded: at most 64 held at once per process, a constant; more
  wait), runs
  `SET lock_timeout` and `SELECT pg_advisory_lock(k)`, yields, then
  `pg_advisory_unlock(k)` and returns the connection. If the process
  dies, Postgres releases the lock with the connection. If unlock fails
  (connection broken), the connection is closed (which releases the lock).
- Keys are signed 64-bit integers from SHA-256 of a namespaced string:
  `run:<wid>:<run_id>`, `object:<wid>:<hh>`, `lineage:<wid>:<project_key>`.
  Use a separate namespace from 19's `pg_advisory_xact_lock` keys (prefix
  strings differ, so collisions are only hash collisions, accepted).
- `app.run_lock` / `object_lock` return a lock object whose `__enter__`
  takes the in-process thread lock first, then (Postgres only) the session
  lock; `__exit__` releases in reverse. Timeout (default 30 s) →
  `DatabaseBusy` → 503 `Retry-After: 5` (existing mapping).
- SQLite: `session_lock` does not exist; behaviour unchanged.

### Process start (`app.py` `serve`)

- `acquire_server_lock` only when the database is SQLite.
- `_clean_tmp`: in Postgres mode, remove only files and `lineage-*` dirs
  whose mtime is older than 1 hour; SQLite mode unchanged.
- Migrations: 19's `advisory_lock('migrate')` already serialises them.
- `import_state` (2.x import) must be safe when two processes start on a
  fresh 2.x directory at once: it already takes `advisory_lock('import')`;
  verify on Postgres and test.

### Reads after another process wrote

- `run_reader` cache: the reader takes committed sizes from
  `RunRepo.normalized_remote` (database). Confirm every read path asks the
  database (or a size it got from the database in this request) rather
  than a size cached from an earlier request, so a run appended by process
  B is visible through process A. Fix any cached size; test it.
- `workspace_stores` cache is keyed by workspace status read from the
  database per request (suspend in B is seen by A). Keys, users and
  memberships are read from the database per request (confirm no
  in-memory key cache; a key revoked through B must stop working in A on
  the next request).

### 18c counters across processes

- Request counter: one `INSERT ... ON CONFLICT DO UPDATE SET request_count
  = CASE WHEN request_minute = excluded.request_minute THEN request_count
  + 1 ELSE 1 END, request_minute = excluded.request_minute RETURNING
  request_count`, with the minute from the database clock. Atomic on both
  backends.
- Uploads per day and storage: checked under the cross-process run/object
  lock and updated in the recording transaction; the overshoot bound from
  18c (in-flight bodies to different runs) holds across processes.

## Test (`tests/test_server_many_processes.py`, Postgres only)

Skipped without `AGENTICML_TEST_DATABASE_URL`. Uses real subprocesses
(`agenticml server serve` on two ports, one data directory, one schema):

1. Create a workspace, a member and a sync key through the CLI.
2. Generate N runs (e.g. 12) locally with scalars, events, media objects
   and an artifact; three of them large enough that an append takes a
   while (or use a test hook that slows `_after_append_write` via env
   `AGENTICML_TEST_APPEND_DELAY_MS`, honoured only when
   `AGENTICML_TEST_HOOKS=1`).
3. Sync in parallel threads, alternating server A and B per run, and sync
   the **same** run through both at once (two clients, one per server).
4. During a slow append on A, `SIGKILL` A. Restart A.
5. Re-sync everything through A and B until clean.
6. Compare with a single-process reference: the same runs synced through
   one server into a second workspace. For every run: `meta.json`,
   `events.jsonl`, `scalars/*`, `media/index.json`, objects, and the
   database rows (`runs`, `run_files`, `objects`, usage counters) are
   equal (ignoring timestamps and key names).
7. Every acknowledged append (client got 200) is present after the kill.

Also unit tests: `session_lock` excludes across two `PostgresDatabase`
objects; a killed holder (connection closed) releases the lock; lock
timeout → 503; tmp cleanup keeps a fresh file and removes an old one;
request counter is exact under 2 processes × many threads (no lost
increments); revoked key in B refused in A.

## Constraints and conventions

- No change to protocol v2 or the sync client.
- SQLite mode: identical behaviour and tests; the many-process test is
  skipped there.
- Locks are always taken in the same order: thread lock → session lock →
  transaction; never hold a session lock while waiting on another session
  lock of a different kind except run → object (document the order).
- No busy loops; rely on `lock_timeout`.

## Edge cases

- Process killed while holding a session lock: released by Postgres when
  the TCP connection drops (on a hard network partition after
  `tcp_keepalives`; set `keepalives=1, keepalives_idle=30` in the
  connection parameters for lock connections).
- Process killed between file write and size commit: the 17 truncate rule
  covers it; the next append (through any process) truncates the tail.
- Two processes receive the same object: object lock serialises;
  second sees it exists.
- Two processes receive lineage for the same project: advisory lock plus
  the existing file lock.
- Clock skew between hosts: counters use the database clock.
- More than 64 concurrent held locks in one process: requests wait up to
  the lock timeout, then 503.

## Done when

A test runs two server processes on one Postgres and data directory,
syncs many runs through both in parallel (including the same run), kills
one mid-upload, and the result equals a single-process sync; the full suite
passes on both backends.

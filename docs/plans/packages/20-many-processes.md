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
| `server.lock` (flock on `<data>/server/server.lock`) | refuses a second process | shared database session lock; no flock |
| Run lock `(workspace, run_id)` | `threading.Lock` in `app.run_lock` | thread lock **plus** a session advisory lock in Postgres |
| Object lock `(workspace, sha[:2])` | `threading.Lock` | thread lock plus advisory lock |
| Lineage fetch lock | file lock in the lineage directory | file lock kept, plus advisory lock per (workspace, project key) |
| `RunRepo.file_guard` | no-op (caller holds run lock) | stays a no-op; the run lock is now cross-process |
| Login throttle | `DbBuckets`: counts in the DB, tickets (holds) per process | holds stay per process (a burst split across N processes can get N × the free attempts in flight, bounded by `FREE_FAILURES × N`); each begin/finish is one write transaction under `advisory_lock('throttle')` that writes the buckets it touches (atomic `failures = GREATEST(failures + 1, …)` upserts and per-bucket deletes) and deletes expired rows, skipping any bucket with a live hold in this process, so concurrent failures add and a success cannot erase another bucket |
| Sessions | DB | unchanged |
| 18c counters (requests/minute, uploads/day, storage) | DB, server clock | DB; minute and day keys from the **database clock** (`clock_timestamp()`) |
| `server/tmp` cleanup at start | deletes everything in `server/tmp` | deletes only entries older than `BODY_DEADLINE + OBJECT_BODY_LIMIT / BODY_BYTES_PER_SECOND + 1 h` (mtime), so a starting process never removes another process's in-flight upload. Assumes host clocks and filesystem mtime agree within that 1 h margin |
| Run readers cache, code-diff cache, workspace stores cache | per process | per process; readers must not serve stale committed sizes (see below) |

## Design

### Cross-process locks (`db/postgres.py`, `app.py`)

- `PostgresDatabase.session_lock(key, timeout)`: a context manager that
  checks out a dedicated connection (outside the transaction pool count
  but bounded: 48 outer run/object connections and 16 inner
  quota/lineage/import/offline-command connections per process; more wait), runs
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

- `acquire_server_lock` only when the database is SQLite. Postgres servers
  hold a shared session advisory lock scoped to the database/schema for their
  lifetime, released only after worker joins in `server_close`. Offline reindex
  tries the exclusive version and refuses while any server holds it. The
  lifetime connection is separate from both request lock budgets.
- `_clean_tmp`: in Postgres mode, remove only files and `lineage-*` dirs
  whose mtime is older than max body time (`BODY_DEADLINE +
  OBJECT_BODY_LIMIT / BODY_BYTES_PER_SECOND`, ~2.3 h) plus a 1 h margin;
  SQLite mode unchanged. The margin is also the assumed bound on clock
  skew between hosts and the filesystem mtime.
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
  lock and updated in the recording transaction. The pre-check overshoot
  bound from 18c (in-flight bodies to different runs) scales with
  `BODY_CONCURRENCY × processes`; final accounting under the quota lock is
  exact.

### Carried over from 18c and 19.1 reviews (Nov 9, 2026)

- 18c added an in-process per-workspace quota lock (`app.quota_lock`)
  around the final limit check, publish and counter update when a limits
  file is set. In Postgres mode it must also take a session advisory lock
  (`quota:<wid>`), after the run/object lock, like the run and object
  locks above.
- Lineage `receive` (git work) currently runs inside the quota lock; give
  lineage a narrow lock around just the daily check and `add_upload`.
- Writes within one workspace are serialized while limits are on; note the
  throughput cost in the package 24 benchmark.
- `for_server_dir` holds its registry lock across Postgres network I/O;
  open outside the lock (roadmap Backlog).

## Test (`tests/test_server_many_processes.py`, Postgres only)

Skipped without `AGENTICML_TEST_DATABASE_URL`. Uses real subprocesses
(`agenticml server serve` on two ports, one data directory, one schema):

1. Create a workspace, a member and a sync key through the CLI.
2. Generate N runs (e.g. 12) locally with scalars, events, media objects
   and an artifact; three of them large enough that an append takes a
   while (or use a test hook that slows `_after_append_write` via env
   `AGENTICML_TEST_APPEND_DELAY_MS`, honoured only when
   `AGENTICML_TEST_HOOKS=1` and `PYTEST_CURRENT_TEST` is set).
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
  transaction; outer run/object locks precede inner lineage/quota locks.
  Never acquire an outer lock while holding an inner lock.
- No busy loops; rely on `lock_timeout`.

## Edge cases

- Process killed while holding a session lock: released by Postgres when
  the TCP connection drops (on a hard network partition after
  `tcp_keepalives`; set `keepalives=1, keepalives_idle=30` in the
  connection parameters for lock connections). Also configure server-side
  `tcp_keepalives_idle=30`, `tcp_keepalives_interval=10`,
  `tcp_keepalives_count=3`, and `tcp_user_timeout='30s'` where supported
  (log a warning when a `SET tcp_*` is unsupported).
- A lifetime shared server-lock connection lost while the server is still
  running is not detected, so offline commands could then take the exclusive
  lock and run. Note for package 24.
- Process killed between file write and size commit: the 17 truncate rule
  covers it; the next append (through any process) truncates the tail.
- Two processes receive the same object: object lock serialises;
  second sees it exists.
- Two processes receive lineage for the same project: advisory lock plus
  the existing file lock.
- Clock skew between hosts: counters use the database clock.
- An exhausted outer (48) or inner (16) budget waits up to the lock timeout,
  then 503. Held outer locks cannot consume inner capacity.

## Done when

A test runs two server processes on one Postgres and data directory,
syncs many runs through both in parallel (including the same run), kills
one mid-upload, and the result equals a single-process sync; the full suite
passes on both backends.

## 20.1 implementation notes (Nov 9, 2026)

- This part covers the lock/startup primitives and two in-process server
  instances. The subprocess sync/kill/restart comparison is covered by 20.2.
- Session lock keys include the current database and schema, with a
  `session:` prefix distinct from transaction locks. The split 48/16 connection
  admission bounds are process-wide across database objects. Dedicated lock
  connections close after every use, including failed unlocks.
- Transaction lock namespaces and the transaction pool's existing idle-only
  bound are retained in 20.1; the new hard bound applies to dedicated session
  connections. This limits this part's change to the requested lock pool.
- Postgres registry connection/import I/O now runs outside the registry lock.
  Inspection found that the existing transaction import lock did not cover
  legacy-file preparation and retirement. A startup session lock now wraps
  the full import; the transaction locks remain.
- Lock ordering is thread → session → transaction. Uploads take run/object
  before quota; lineage takes run → lineage → existing file lock, releases
  lineage, then takes quota only for the final daily check and accounting.
  No code waits on a run/object lock while holding quota. With limits enabled,
  publication and accounting within a workspace are serialized; package 24's
  benchmark must measure that throughput cost.
- Inspection found that the run reader's remote provider refreshed status,
  but append reads used disk lengths. An optional committed-size provider
  now bounds Postgres server reader operations using fresh database sizes.
  SQLite server and local reader behavior retain their existing paths.
- Existing startup/flock tests are marked SQLite-only because Postgres now
  intentionally permits a second server and retains fresh temporary files.
  Drain/join tests now run on both backends, checking the database lifetime
  lock on Postgres instead of flock. The invalid upload-clock test uses its
  explicit injectable clock on both backends, rather than replacing the SQLite-only process clock. The lineage
  admission-limit test uses independent projects on Postgres because
  same-project receives now intentionally serialize before Git work.
- Per the task's explicit constraints, no roadmap status changes, commits,
  pushes, or draft PR are made for this part.

## Review notes and package 24 deployment inputs

- Lineage's daily limit check runs after `receive()` and before accounting,
  under the quota lock. Accepted: Git work admitted before that check is bounded
  by concurrent requests; counting stays exact.
- Budget per process for package 24 sizing: transaction pool retains 16 idle
  connections (active transactions are not hard-capped), plus up to 64 request
  lock connections (48 outer + 16 inner), plus one dedicated lifetime server
  lock connection per server. Thus 81 is the baseline sizing budget, not a hard
  connection ceiling; deployment must also allow concurrent active transactions.
- Legacy import's session lock waits up to 600 seconds.
- Pre-check overshoot for uploads/storage scales with `BODY_CONCURRENCY ×
  processes`; the quota lock makes final accounting exact.
- A lifetime shared server-lock connection dropped while the process is still
  running is not detected, so exclusive offline commands could then run.
  Detecting that (and refusing or exiting) is package 24 work.
- Unsupported `SET tcp_*` on a lock connection logs a warning.

## 20.2 end-to-end test

`tests/test_server_many_processes_e2e.py` runs two real CLI servers against one
Postgres schema and shared files, with a generous limits file so quota locks
and usage counters are on. It syncs twelve rich SDK runs through alternating
ports (including concurrent duplicate syncs, with a real in-flight barrier)
and records acknowledged append bytes. Acknowledged appends are snapshotted
and checked against disk and the database before A is restarted (before any
reconcile). It kills A during a delayed durable, uncommitted append, restarts
A, retries, then compares run files, objects, database rows, stored bytes and
upload bytes to a single-server reference, and checks the request counter
through the two real subprocesses. The delay hook requires `AGENTICML_TEST_HOOKS=1`,
a positive delay, and `PYTEST_CURRENT_TEST`. Waits and process cleanup
are bounded; logs stay in pytest temporary directories. SQLite refusal is tested
with the database environment removed. Comparisons ignore timestamps, key names
and workspace identities. The initial implementation passed three consecutive
Postgres runs (six tests each); current review checks are recorded below.

## Review validation (Nov 9, 2026)

- Requested seven-file suite, SQLite: 481 passed, 29 skipped (87.68s).
- Same suite with `AGENTICML_TEST_DATABASE_URL` pointing to the local Postgres:
  496 passed, 14 skipped (390.38s).
- Focused Postgres many-process regression suite, including long import lock
  timeout settings: 23 passed (34.63s).
- Required full root suite on SQLite: 2674 passed, 70 skipped (415.72s);
  this run preceded adding the final Postgres-only timeout regression test.
- `git diff --check`: clean. Changes remain uncommitted; roadmap untouched.

## Residual stale-writer window (package 25 threat model)

`RunRepo.record_file(..., expected_size=)` compare-and-swap protects the
committed size, not the file bytes. A writer whose lock connection died (or
that was paused) can keep writing into the file while another process holds
the lock. Append now confirms session locks before the first in-place write
and before truncate `os.replace`, again every 8 MiB in the write loop, and
re-reads `[write_from, new_size)` against the uploaded tmp bytes before
recording. Residual: a process paused longer than the keepalive window while
holding a lock can still write after losing it; the checks only narrow that
window to the interval between two confirms (or between the last confirm and
verify-after-write). Package 25's threat model should treat in-place append
bytes as untrusted until the size is committed.

Atomic files write a temp, fsync, confirm session locks, then compare-and-swap
on `(size, sha256)` and rename inside the recording transaction (old file
hard-linked to a backup name so the original inode stays at `path`, new file
renamed into place, commit, then backup deleted). A CAS miss discards the temp
and returns 503; a commit failure after rename restores the backup only if
session locks still confirm and `path` is still this writer's published inode.
Cleanup that changes shared file bytes (append truncate on failure, atomic
restore/unlink) runs only after `confirm_session_locks()` succeeds immediately
before it; otherwise the writer skips cleanup and returns 503.

A SIGKILL between replace and COMMIT leaving the new complete atomic file at
`path` is ACCEPTED: the next `/state` adopts it (it is a complete client
upload the client will re-send). Under the run lock, leftover
`.<name>.bak.*.tmp` backup files are removed before an atomic write and in
`/state`, so a crash between `os.link` and `os.replace` cannot leave the
committed file hard-linked (which `st_nlink` checks would treat as unsafe).
SQLite uses the same path, including `publish()` inside the write transaction
(benign). The CAS cannot lose under the thread lock.

## Review fixes (Nov 9, 2026)

- xact advisory keys are now salted with database and schema, so old and new
  processes during a rolling upgrade do not exclude each other. Deploy all
  processes together.
- SQLite also runs `publish()` inside its write transaction now (benign).
- Login throttle transactions prune expired `login_attempts` rows (`last_failure`
  older than now − expiry), skipping any bucket with a live hold in this process,
  so the table cannot grow without bound when only touched buckets are written.
- Throttle `persisted` is set only after the write transaction commits; a
  COMMIT failure keeps unsaved failure counts in memory (max-merged on load).

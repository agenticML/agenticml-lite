# Package 16 †: Database layer, SQLite backend, migrations

Nov 7, 2026 · Supervisor plan · Release 3.0 · Coder: Codex · Size: L

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), "Storage layer
(package 16)" and the package summary. Builds on 2.2 (packages 11–15).
Package 17 moves runs into the same database; package 18 reshapes users
into accounts and workspaces; package 19 adds Postgres.

† Security-sensitive: second read-only review by the coder that did not
write it.

## Goal

Everything the 2.2 server keeps about people and access moves from JSON
files and process memory into one SQLite database behind a repository
layer, with **no change in behaviour**:

| 2.2 storage | 3.0 (this package) |
| --- | --- |
| `<data>/server/users.json` | table `users` |
| `<data>/server/keys.json` (+ `keys.lock`) | table `keys` |
| `<data>/server/setup-closed` marker | row in `server_settings` |
| `<data>/server/audit.jsonl` | table `audit_events` |
| `WebAuth._sessions` (memory) | table `sessions` (survive restart) |
| `LoginThrottle._entries` failure counts (memory) | table `login_attempts` |

Done when:

- every existing server test passes (tests that read or write the old JSON
  files directly are rewritten to use the repositories, never deleted
  without a replacement that checks the same rule);
- a session survives a server restart (test);
- `agenticml server audit` and `GET /api/server/audit` read from the table;
- `users.json`, `keys.json`, `keys.lock`, `setup-closed` and `audit.jsonl`
  are no longer created or written by any code path;
- a 2.0, 2.1 or 2.2 data directory starts on this code with its users,
  keys, setup state and audit history intact (one-time import, below).

Out of scope: run state, `remote.json`, `artifacts.json`, notes (17);
workspaces, accounts, memberships (18); Postgres (19); more than one server
process (20; SQLite keeps `server.lock` and one process). No dashboard or
protocol change. No new runtime dependency.

## Files to touch

New:

- `src/agenticml/server/db/__init__.py`: `connect(url)`, `default_url(data_dir)`,
  `resolve_url(data_dir, url=None)`, `for_server_dir(server_dir)`,
  `DatabaseError`, `SchemaTooNew`.
- `src/agenticml/server/db/sqlite.py`: `SqliteDatabase` (connection pool,
  pragmas, file checks, NFS refusal).
- `src/agenticml/server/db/dialect.py`: placeholder conversion, type
  tokens for migrations, `lock_row`, `advisory_lock`, upsert helper.
- `src/agenticml/server/db/migrate.py`: migration runner.
- `src/agenticml/server/db/migrations/0001_initial.sql`.
- `src/agenticml/server/db/repos.py` (or one module per repo if clearer):
  `UserRepo`, `KeyRepo`, `SessionRepo`, `ThrottleRepo`, `AuditRepo`,
  `SettingsRepo`.
- `src/agenticml/server/db/import2x.py`: one-time import of 2.x JSON state.
- Tests: `tests/test_server_db.py` (layer, migrations, pragmas, file
  checks, NFS refusal, import), plus rewritten parts of the existing server
  tests.

Changed:

- `server/keys.py`, `server/users.py`, `server/audit.py`,
  `server/webauth.py`: keep their public classes and methods
  (`KeyStore`, `UserStore`, `AuditLog`, `audit.tail`, `WebAuth`,
  `LoginThrottle`) as thin facades over the repositories, so call sites in
  `keyspage.py`, `userspage.py`, `ingest.py`, `admin.py`, `up.py`,
  `cli.py` and the tests barely change. Remove the JSON parsing, signature
  reload, `_file_lock` and `_write` machinery.
- `server/storeio.py`: delete if nothing uses it after the change.
- `server/app.py`: open the database once in `make_server` (after the
  data-directory checks and `server.lock`, before anything else), pass it
  to the stores, close it in `server_close` after workers join.
- `server/admin.py` (`status`): key and user counts from the database; must
  not create a database that does not exist (report zero keys/users).
- `cli.py`: `--database URL` on every `agenticml server ...` subcommand that
  takes `--data`; environment `AGENTICML_SERVER_DATABASE_URL`.
- `pyproject.toml`: package data includes `server/db/migrations/*.sql`.
- `docs/plans/on-prem-roadmap.md`: status and plan link (supervisor).

## Design

### Configuration

- URL: `--database URL`, else `AGENTICML_SERVER_DATABASE_URL`, else
  `sqlite:///<data>/server/agenticml.db`. The form is `sqlite:///` followed
  by an absolute path, so an absolute file has four slashes
  (`sqlite:////var/lib/agenticml/server/agenticml.db`), as in SQLAlchemy.
  Relative paths are refused.
- Any other scheme (`postgresql://` included) fails with
  `DatabaseError("unsupported database URL scheme; only sqlite:/// is
  supported in this version")`. Package 19 adds Postgres.
- The URL is never logged or printed (it will hold a password on
  Postgres); messages name the scheme only, or the SQLite file path.

### Connection and pool (`db/sqlite.py`)

- Python 3.10+ `sqlite3` with `isolation_level=None` (we issue `BEGIN`
  ourselves) and `check_same_thread=False`. A small pool: connections are
  created on demand, handed to one thread at a time, returned on exit,
  at most 16 idle kept; `close()` closes them all. Do not use per-thread
  connections (the server makes a thread per request).
- Per connection: `PRAGMA foreign_keys=ON`, `PRAGMA synchronous=FULL`,
  `PRAGMA busy_timeout=5000`. Once per file: `PRAGMA journal_mode=WAL`
  (check the result is `wal`; refuse otherwise).
- File creation: before the first `sqlite3.connect`, `os.lstat` the path;
  a symlink or non-regular file is refused (`DatabaseError`). If missing,
  create it with `os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o600)`
  and close it. The parent directory is `<data>/server` (0700, already
  checked by `prepare_data_dir`). For a non-default path, the parent must
  exist and be a directory.
- Network filesystem refusal (Linux only): read `/proc/self/mountinfo`,
  find the mount whose mount point is the longest prefix of the database
  file's real directory, and refuse when its filesystem type is one of
  `nfs`, `nfs4`, `cifs`, `smb3`, `smbfs`, `9p`, `fuse.sshfs`, `ceph`,
  `glusterfs`, `lustre`, `afs` with: `"<path> is on a network filesystem
  (<type>); SQLite needs a local disk. Put --database on local disk or use
  Postgres (3.0 hosted mode)."` The mountinfo path is a module constant so
  tests can point it at a fixture. A missing or unreadable mountinfo
  (non-Linux) skips the check.
- `transaction(write: bool)` context manager on the database object,
  yielding a `Tx` with `execute`, `fetchone`, `fetchall`, `lock_row`,
  `advisory_lock`. Write transactions start with `BEGIN IMMEDIATE`, read
  transactions with `BEGIN` (consistent snapshot). Commit on normal exit,
  roll back on any exception. A nested `transaction()` on the same thread
  is a programming error (raise), so repositories that need several steps
  take an existing `Tx`.
- `sqlite3.OperationalError` "database is locked" after the busy timeout
  surfaces as `DatabaseBusy` (subclass of `DatabaseError`); callers that
  map store errors to 503 (`KeyStoreUnavailable` today) map it the same way.

### Dialect (`db/dialect.py`)

Repository SQL uses `?` placeholders and standard SQL that SQLite 3.31+ and
Postgres 13+ both accept (`INSERT ... ON CONFLICT (...) DO UPDATE/NOTHING`,
no `RETURNING`, no `AUTOINCREMENT`, no `INSERT OR REPLACE`). The dialect:

- converts placeholders (`?` → `%s` for Postgres later; identity on SQLite);
- expands type tokens in migration files: `{{id}}` (auto-increment primary
  key: `INTEGER PRIMARY KEY` on SQLite, `BIGINT GENERATED BY DEFAULT AS
  IDENTITY PRIMARY KEY` later on Postgres), `{{blob}}` (`BLOB` / `BYTEA`),
  `{{real}}` (`REAL` / `DOUBLE PRECISION`);
- `lock_row(tx, table, where_sql, params)`: SQLite no-op (the `BEGIN
  IMMEDIATE` write lock covers it); Postgres `SELECT 1 ... FOR UPDATE`
  later;
- `advisory_lock(tx, name)`: SQLite no-op; Postgres
  `pg_advisory_xact_lock` later.

Only the SQLite half is implemented now, but the interface must be the one
package 19 fills in, and no repository may contain backend-specific SQL.

### Migrations (`db/migrate.py`)

- Files `db/migrations/NNNN_<name>.sql`, applied in order, each in its own
  write transaction together with its `schema_migrations(version, name,
  applied_at)` row. Statements split on `;` at line end (keep the files
  free of semicolons inside strings).
- Run automatically every time a database is opened (server start and every
  CLI command). The write transaction serializes concurrent openers (CLI
  while the server starts).
- A database whose highest applied version is newer than the newest file
  raises `SchemaTooNew`: `"database schema version N is newer than this
  agenticml-lite (knows up to M); upgrade agenticml-lite"`. The server
  refuses to start; CLI commands exit 1 with that message.
- Read-only operator commands (`server status`) open without creating:
  `for_server_dir(..., create=False)` returns `None` when the file is
  missing.

### Schema `0001_initial.sql`

Single-team, mirroring 2.2 records exactly (18 reshapes them):

```sql
CREATE TABLE server_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE users (
  id TEXT PRIMARY KEY,               -- 32 hex, as 2.2
  name TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK (role IN ('viewer','member','admin')),
  identity TEXT,
  identity_folded TEXT UNIQUE,       -- casefold(identity) or NULL
  password TEXT,                     -- 2.2 scrypt record as JSON, or NULL
  invite_sha256 TEXT UNIQUE,
  invite_expires_at TEXT,
  generation INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL
);

CREATE TABLE keys (
  name TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('sync','admin')),
  sha256 TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  last_used_at TEXT,
  user_name TEXT NOT NULL REFERENCES users(name) ON DELETE CASCADE
);
CREATE INDEX keys_user ON keys(user_name);

CREATE TABLE sessions (
  token_sha256 TEXT PRIMARY KEY,
  user_name TEXT NOT NULL,           -- 'env' allowed: not a foreign key
  user_id TEXT,
  generation INTEGER NOT NULL,
  via TEXT NOT NULL,
  key_name TEXT,
  key_sha256 TEXT,
  created {{real}} NOT NULL,
  last_seen {{real}} NOT NULL
);
CREATE INDEX sessions_last_seen ON sessions(last_seen);
CREATE INDEX sessions_key ON sessions(key_name, key_sha256);

CREATE TABLE login_attempts (
  bucket TEXT PRIMARY KEY,
  failures INTEGER NOT NULL,
  last_failure {{real}} NOT NULL
);

CREATE TABLE audit_events (
  id {{id}},
  ts TEXT NOT NULL,
  actor TEXT, via TEXT, action TEXT, target TEXT, client TEXT,
  detail TEXT                        -- JSON or NULL
);
CREATE INDEX audit_events_ts ON audit_events(ts, id);
```

Adjust column details if the code needs it (report the change), but keep:
digests only (never key values, session tokens or invite tokens), the
uniqueness rules 2.2 enforced in Python (key name, key digest, user name,
user id, folded identity) also enforced by the schema, and timestamps in
`layout.format_timestamp()` form.

### Repositories and facades

- One process-wide registry in `db/__init__.py`: `for_server_dir(server_dir,
  url=None)` returns the same open `Database` for the same resolved file,
  so `KeyStore(dir)`, `UserStore(dir)` and `AuditLog(dir)` built separately
  (as tests and the CLI do) share one pool. Hold entries weakly; a
  `Database` closes its pool when closed or garbage-collected.
- `KeyStore(server_dir, env_key=None, *, db=None)`,
  `UserStore(server_dir, *, db=None)`, `AuditLog(server_dir, *, db=None)`:
  same public methods, same exceptions and messages, same return types
  (`KeyRecord`, `UserRecord`, `Principal`). `KeyStoreUnavailable` now means
  "the database could not be read or written".
- Multi-step rules move into one write transaction each, which removes the
  `keys.lock` dance:
  - `KeyStore.create`: name taken, user lookup, bootstrap `admin` user when
    there are no users, `only_if_no_admin` / setup-closed check, admin key
    needs an admin user, insert key, set `setup_closed` — one transaction.
  - `KeyStore.revoke(keep_admin=True)`: the last-usable-admin-key rule
    reads users and keys in the same transaction.
  - `UserStore.remove`: last-admin check, delete keys (cascade or explicit),
    delete user — one transaction; still returns the number of keys removed.
  - `set_role`, `set_password`, `create_invite`, `redeem_invite`,
    `set_identity`: one transaction each; generation bumps as in 2.2.
  - `redeem_invite`: look up by `invite_sha256` (the digest of a 256-bit
    token, so an indexed lookup is fine; keep `hmac.compare_digest` on the
    fetched row) and check expiry.
- `KeyStore.verify`: indexed lookup by `sha256` (digest of the presented
  key) plus `hmac.compare_digest`, then the env record. The 2.2
  "no early exit" loop protected a list scan; an indexed digest lookup does
  not reveal anything useful about other keys. Keep `current()` semantics.
- `KeyStore.touch`: `UPDATE keys SET last_used_at=? WHERE name=? AND
  sha256=? AND (last_used_at IS NULL OR last_used_at < ?)`; errors stay
  logged at most once a minute and never fail the request.
- No caches: every read goes to the database (SQLite reads are cheap), so
  a CLI change is visible to the running server at once, as before.
- `setup_closed()` / `clear_setup_closed()` / `prepare_setup()` use the
  `server_settings` row `setup_closed`. `clear_setup_closed()` keeps its
  contract: raise `FileNotFoundError` when the row is absent (call sites
  rely on it) or change both call sites consistently.
- `UserStore._name_owns_runs` still scans `remote.json` (runs move in 17).

### Sessions (`WebAuth`)

- `SessionRepo`: `create(digest, entry, cap)`, `get(digest)`,
  `touch(digest, now)`, `delete(digest)`, `delete_for_key(name, sha256)`,
  `prune(now, idle, max_age)`. Same fields as the 2.2 dict entry; same
  idle (12 h), max age (7 days), cap (1000, evict oldest `last_seen`) and
  the "touch at most once a minute" rule.
- `WebAuth` keeps its clock injection (`clock=time.time`); stored times are
  that clock's values. Behaviour of `_session`, `_create_session`,
  `_delete_session`, `drop_sessions` is unchanged; handlers still receive a
  dict in `handler.session`.
- A session made before a restart is valid after it (the reason for this
  change); a session whose user generation, key or role no longer matches is
  dropped exactly as in 2.2.

### Login throttle

- `LoginThrottle(clock=time.monotonic, store=None)`: the algorithm stays
  in one class. Failure counts and last-failure times live in a bucket
  store: `MemoryBuckets` (default, used by existing unit tests) or
  `DbBuckets` (`login_attempts` table). In-flight holds (tickets) stay in
  process memory in both cases.
- `WebAuth` builds the throttle with `DbBuckets` and a wall clock
  (`time.time`) so stored values mean something after a restart.
- Expiry (15 min) and the 1024-bucket cap keep their meaning: expired rows
  are deleted when touched and pruned on writes; when the table is at the
  cap, the oldest row without an in-memory hold is evicted.
- Run the existing throttle unit tests against both bucket stores
  (parametrize).

### Audit

- `AuditLog.record(...)` inserts one row; same fields, same truncation, same
  "never log secrets" rule, same best-effort error handling (log at most
  once a minute, never raise).
- `audit.tail(server_dir, limit, before)` keeps its signature and return
  shape `(events, skipped)` (`skipped` is always 0 now); newest first,
  `before` excludes events with `ts >= before`, limit capped at 1000. Order
  by `(ts, id)` descending. `detail` decoded from JSON.
- CLI `agenticml server audit` and `GET /api/server/audit` unchanged on the
  outside.

### One-time import of 2.x state (`db/import2x.py`)

Runs inside `make_server` and every CLI `server` command, right after
migrations, guarded by the `server_settings` row `imported_2x`:

1. If the row exists, return.
2. Run the existing 2.0/2.1 → 2.2 `users.migrate()` first when `users.json`
   is missing but `keys.json` exists (unchanged code; it still rewrites
   `remote.json` owners). It may still write `users.json` and `keys.json`;
   that is fine, they are imported next. Its audit line is written to the
   table.
3. In one write transaction: read `users.json` (version 1), `keys.json`
   (version 2), the `setup-closed` marker, and every complete line of
   `audit.jsonl` with the 2.2 parsers (`_parse_row`, `_parse_keys_object`,
   the audit line rules); insert them; set `imported_2x` to the current
   timestamp. A missing file imports nothing.
4. After the commit, rename each imported file to `<name>.2x`
   (`users.json.2x`, `keys.json.2x`, `audit.jsonl.2x`, `setup-closed.2x`)
   and delete `keys.lock`. A rename failure is logged and ignored (the
   flag is set, the files are never read again).
5. A corrupt or invalid `users.json` or `keys.json` (anything the 2.2
   parsers reject, a symlink, a non-regular file) aborts the import with
   `DatabaseError("could not import <file>: <reason>; fix or move it
   away")`: the server refuses to start and CLI commands exit 1. Malformed
   audit lines are skipped and counted in one warning. 2.2 kept serving
   with a broken `users.json`; refusing at the one-time import is the
   safer equivalent and is called out in the PR.
6. Fresh data directories set `imported_2x` with nothing to import.

Package 17 adds `remote.json`/`artifacts.json` to this import and package
18 moves the result into the `default` workspace, so a 2.x directory keeps
working through the whole release; package 23 keeps backup, `status` /
`verify` on the database and the end-to-end 2.2-directory test.

## Constraints and conventions

- Standard library only. Python ≥ 3.10 (CI runs 3.10/3.12 per
  `pyproject.toml`; do not use `sqlite3.Connection.autocommit` (3.12+)).
- Match the surrounding style: small functions, fixed client-safe error
  messages, `logger` names under `agenticml.server`, no secrets in logs.
- All SQL lives in `server/db/`. Handlers and pages never write SQL.
- Keep the existing public names and exception types; prefer changing a
  facade's internals over changing call sites.
- `server.lock` and the one-process rule stay.
- File modes: database 0600 in a 0700 directory; WAL/SHM files inherit
  the directory protection.
- Never print or log the database URL.

## Edge cases

- CLI `server keys create` while the server runs: WAL + busy timeout; the
  server sees the new key on the next request (test).
- Two openers apply migrations at once: second sees them applied (test with
  two `Database` objects on one file).
- Database file is a symlink, a FIFO or a directory: refused before
  `sqlite3.connect` (tests).
- Database on NFS (fixture mountinfo): refused with the message above.
- `SchemaTooNew` (insert a `schema_migrations` row with a high version):
  server start and CLI both refuse with the message.
- Disk full / database locked during a key create: the request answers 503
  as `KeyStoreUnavailable` did; nothing half-written (transaction).
- Audit write failure never fails the request it describes.
- Session for a removed user, a role change, a revoked key, a password
  change: dropped as in 2.2 (existing tests).
- `AGENTICML_SERVER_KEY` env key: still memory-only, never stored.
- `setup-closed` semantics: once any admin key was created, setup never
  re-arms, even after that key is revoked (existing tests).
- Import: 2.0 directory (keys.json v1, no users.json), 2.2 directory with
  invites and passwords, a planted symlink `users.json`, a torn last audit
  line, and a second start (no re-import, `.2x` files untouched).

## Tests and checks

- `tests/test_server_db.py`: pool and pragmas (`journal_mode` wal,
  `foreign_keys` 1, `synchronous` 2), file mode 0600, symlink/FIFO refusal,
  NFS refusal via fixture mountinfo, migration ordering and idempotence,
  concurrent openers, `SchemaTooNew`, URL parsing (relative path, unknown
  scheme, never echoed), transaction rollback on exception, nested
  transaction error, import cases above.
- Existing suites (`test_server_keys*.py`, `test_server_users.py`,
  `test_server_signin.py`, `test_server_login_throttle.py`,
  `test_server_audit.py`, `test_server_admin.py`, `test_server_teams.py`,
  `test_server_userspage.py`, `test_server_up.py`, `test_server_web.py`,
  `test_server_cli.py`, `test_server_lifecycle.py`, ingest/sync tests):
  pass. Where a test reads or plants `keys.json`, `users.json`,
  `audit.jsonl` or `setup-closed`, change it to use the repository or the
  import path, keeping what it asserts. List every test you removed or
  changed in meaning in your report, with the reason.
- New: session survives restart (make server, log in, close, make server on
  the same data dir, same cookie works); CLI change visible to a running
  server; audit CLI and API read the table; nothing writes the old files
  (after a full teams flow, `<data>/server` contains only `agenticml.db*`,
  `server.lock`, `tmp/` and any `.2x` files).
- Run: `PYTHONPATH=src testbed/.venv/bin/python -m pytest -q tests` from the
  repo root (the `testbed/.venv` in this worktree is a link to the main
  checkout's venv, so `PYTHONPATH=src` is required to test this tree).
- `scripts/server_smoke.sh` if it runs offline.

## Done when

All checks above pass, the old state files are no longer written, sessions
survive a restart, the audit view reads the table, a 2.x data directory
imports once, and both reviews (Sonnet, and the second † review) are clean.

## Notes for package 19 (from the package 16 reviews)

- `Tx.execute` returns the raw `sqlite3` cursor and `SettingsRepo.delete`
  relies on its `rowcount`; repositories read rows by name
  (`sqlite3.Row`). The Postgres backend must return the same row and
  rowcount interface (psycopg `dict_row` or an adapter).
- The `advisory_lock` / `lock_row` hooks are already called around every
  read-then-write rule; package 19 only fills them in.
- A Postgres URL holds a password: `server up --install` writes only an
  explicit `--database` into the service unit (0644 today). Package 19
  should move the URL to an `EnvironmentFile` with mode 0600 or the
  environment, never the unit's command line.

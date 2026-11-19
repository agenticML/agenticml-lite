# Package 31: usage history (server totals, daily snapshots, /admin overview)

Nov 17, 2026 · supervisor plan · coder: Sonnet · release 3.1

## Goal

Server admins can see how the service grows: how many accounts, how many
of them are active, how many workspaces, how much data is stored and
uploaded, and how many runs, both now and day by day. Today only
per-workspace counters exist (`workspace_usage`, shown per row on `/admin`
and by `agenticml server workspaces usage`); they have no history and no
server-wide totals, and there is no record of when an account was last
active (sessions live at most 7 days and are capped at 1000 rows).

This package adds:

1. `accounts.last_active_at`, set when an account uses a session or a key
   (at most once an hour per account).
2. A `stats_daily` table with one row per UTC day of server-wide totals,
   refreshed hourly by a small loop inside the server process.
3. One function that computes the totals, used by a new admin API
   `GET /api/admin/stats`, a new CLI `agenticml server stats`, and the
   snapshot.
4. A **Usage** section at the top of `/admin`: tiles with the current
   totals and three small growth charts.

It works the same on self-hosted (SQLite) and hosted (Postgres), needs no
AWS change, and nothing leaves the server.

## Decisions (supervisor, recorded here)

- **Snapshot inside the server, not a scheduled ECS task.** A scheduled
  task needs a bootstrap stack change by the owner, a new IAM role, a new
  entrypoint (the image's entrypoint always runs `server serve`) and does
  nothing for self-hosted. The server is always running; a loop with an
  idempotent per-day upsert is enough, also with two tasks.
- **A row is the day's latest state.** The loop refreshes today's row
  every hour (upsert), so a finished day holds its state as of its last
  refresh (at most an hour before midnight UTC, or before the server
  stopped). Daily sign-ups, runs and storage growth are differences
  between consecutive rows; the day's upload bytes are those counted up
  to the last refresh. Days the server was down have no row; charts skip
  them.
- **Active user = account with `last_active_at` in the window.** Using a
  dashboard session or an API key (sync) counts. The env admin key has no
  account and does not count.
- **Aggregates only in the history.** `stats_daily` holds integers and no
  emails, logins, slugs or names. The live API and CLI also list the top
  five workspaces by stored bytes (slug, name, bytes), which `/admin`
  already shows per row.
- **Upload history is not made exact.** Exact per-day upload totals would
  need a server-wide counter on the upload path (a hot row) or extra
  rollover columns in `workspace_usage`; not worth it now.

## Files to touch

- `src/agenticml/server/db/migrations/0010_usage_history.py` (new).
- `src/agenticml/server/db/usage.py`: factor the database clock out of
  `UsageRepo.now` into a module function `database_now(db, tx=None)`;
  `UsageRepo.now` calls it (behaviour unchanged).
- `src/agenticml/server/db/stats.py` (new): `StatsRepo` and
  `mark_account_active`.
- `src/agenticml/server/db/repos.py`: `SessionRepo.create`,
  `SessionRepo.touch`, `KeyRepo.touch` mark the account active in the same
  transaction.
- `src/agenticml/server/stats.py` (new): `StatsRefresher` (the hourly
  loop).
- `src/agenticml/server/app.py`: `serve()` starts and stops the refresher.
- `src/agenticml/server/routes.py`: route and handler for
  `GET /api/admin/stats`.
- `src/agenticml/cli.py`: `agenticml server stats [show|snapshot]`.
- `src/agenticml/ui/static/app.js` (and `app.css` only if the existing
  classes are not enough): the Usage section in `adminPage`.
- Tests: `tests/test_server_stats.py` (new),
  `tests/test_server_isolation.py` (`EXAMPLES` entry),
  `tests/test_server_workspace_routes.py` (admin route list, if it lists
  every admin route), `testbed/tests/test_server_admin_dashboard.py`.
- Docs (after the code review): `docs/SERVER.md`, `deploy/aws/README.md`
  ("Operations and scaling"), `website/src/content/docs/legal/privacy.md`
  (one sentence, see Docs), `docs/plans/on-prem-roadmap.md` (status).

## Approach

### 1. Migration `0010_usage_history.py`

A `.py` migration (`upgrade(tx, dialect)`, same style as
`0005_limits.py`) because it backfills in Python:

```sql
ALTER TABLE accounts ADD COLUMN last_active_at TEXT;
CREATE INDEX accounts_last_active ON accounts(last_active_at);
CREATE TABLE stats_daily (
 day TEXT PRIMARY KEY,               -- 'YYYY-MM-DD', UTC
 taken_at TEXT NOT NULL,             -- layout.format_timestamp
 accounts BIGINT NOT NULL,
 active_1d BIGINT NOT NULL, active_7d BIGINT NOT NULL, active_30d BIGINT NOT NULL,
 workspaces_active BIGINT NOT NULL, workspaces_suspended BIGINT NOT NULL,
 workspaces_archived BIGINT NOT NULL, workspaces_custom BIGINT NOT NULL,
 stored_bytes BIGINT NOT NULL, upload_bytes BIGINT NOT NULL,
 runs BIGINT NOT NULL
);
```

- No `workspace_id` column, so no row-level security (like `accounts`).
  Do not call `enable_workspace_rls`.
- Backfill `last_active_at` (the migration transaction already runs
  `cross_workspace('migrate')`): for each account, the latest of
  `keys.last_used_at` (ISO text) and `sessions.last_seen` (epoch seconds,
  convert with `layout.format_timestamp(datetime.fromtimestamp(x, timezone.utc))`),
  computed in Python, then `UPDATE accounts SET last_active_at=? WHERE id=?`.
  Accounts with neither stay NULL.
- Statements are split on `;`: no `;` inside the SQL text or comments.

### 2. Database clock

In `db/usage.py` add `database_now(db, tx=None)`: on Postgres
`SELECT clock_timestamp()` converted to UTC (exactly what `UsageRepo.now`
does today), else `datetime.now(timezone.utc)`. `UsageRepo.now` delegates
to it. Existing tests (`test_usage_views_use_database_clock`) must still
pass.

### 3. `db/stats.py`

```python
ACTIVE_RESOLUTION = 3600  # seconds; last_active_at moves at most this often

def mark_account_active(tx, account_id, now):
    """now: epoch seconds. No-op for None (env key) accounts."""
    # UPDATE accounts SET last_active_at=? WHERE id=?
    #   AND (last_active_at IS NULL OR last_active_at < ?)   -- now - ACTIVE_RESOLUTION

class StatsRepo:
    def __init__(self, db): ...
    def totals(self, *, now=None, tx=None) -> dict
    def top_workspaces(self, limit=5, *, tx=None) -> list[dict]
    def snapshot(self, *, now=None, min_age=None) -> bool
    def history(self, days=90, *, now=None) -> list[dict]
```

- `now` overrides (a timezone-aware UTC `datetime`) are for tests; by
  default `database_now(db, tx)` is read once per transaction.
- `totals` runs in one read transaction with
  `tx.cross_workspace('server stats')` (required on Postgres: `keys`,
  `runs`, `workspace_usage` have row-level security). Keys of the result
  (all ints except `as_of`):
  - `as_of`: `layout.format_timestamp(now)`.
  - `accounts`: `COUNT(*) FROM accounts`.
  - `new_accounts_1d`, `new_accounts_7d`: `created_at >= cutoff`.
  - `active_1d`, `active_7d`, `active_30d`: `last_active_at >= cutoff`.
  - `workspaces_active`: `status='active'`;
    `workspaces_suspended`: `status='suspended' AND archived_at IS NULL`;
    `workspaces_archived`: `archived_at IS NOT NULL`;
    `workspaces_custom`: `plan='custom' AND archived_at IS NULL`.
  - `stored_bytes`: `COALESCE(SUM(stored_bytes),0) FROM workspace_usage`
    (all workspaces, archived included: their data is still held). Never
    sum `run_files` or `objects`.
  - `upload_bytes_today`: `SUM(upload_bytes) WHERE upload_day = <day of now>`.
  - `runs`: `COUNT(*) FROM runs`.
  - Cutoffs are `layout.format_timestamp(now - timedelta(days=N))`,
    compared as text (the columns use the same fixed-width format).
- `top_workspaces`: join `workspaces` and `workspace_usage`, order by
  `stored_bytes DESC, slug`, limit; return `slug`, `name`,
  `stored_bytes`, `archived` (bool). Cross-workspace transaction.
- `snapshot(now=None, min_age=None)`: one write transaction:
  `tx.advisory_lock('stats_daily')` (serializes Postgres processes; no-op
  on SQLite where `BEGIN IMMEDIATE` already does), read `now`, `day =
  now.date().isoformat()`; if `min_age` is set and today's row has
  `taken_at > now - min_age`, return None; else compute the totals in the
  same transaction and upsert today's row
  (`db.dialect.upsert('stats_daily', columns, ('day',), <all other columns>)`),
  return the upserted row as a dict. `upload_bytes` = `upload_bytes_today`.
- `history(days)` (`days >= 1`, else ValueError): at most `days` rows, with
  `day >= <day of now - (days - 1)>` (the last one is today), ordered by day
  ascending, as dicts with every column.

### 4. Marking accounts active (`db/repos.py`)

- `SessionRepo.create`: after the insert, `mark_account_active(tx,
  entry['id'], entry['last_seen'])`.
- `SessionRepo.touch(digest, now)`: in the same transaction, look up the
  session's `account_id` (or use a subselect) and mark it active with
  `now`. Only when the session update matched a row is fine either way.
- `KeyRepo.touch(record, now, threshold)`: `now` is ISO text here; mark the
  key's `account_id` active (`SELECT account_id FROM keys WHERE
  workspace_id=? AND name=? AND sha256=?` in the same bound transaction,
  then `mark_account_active` with the epoch from `layout.parse_timestamp`).
  `accounts` has no row-level security, so the workspace-bound transaction
  can update it.
- Errors here must not break a request any more than today: session and
  key touch failures behave as before (the key path already catches and
  logs).

### 5. `server/stats.py`: `StatsRefresher`

```python
STATS_REFRESH = 3600

class StatsRefresher:
    def __init__(self, db, interval=STATS_REFRESH): ...
    def start(self): ...   # daemon thread "agenticml-stats"
    def stop(self, timeout=5): ...  # set Event, join
```

- Loop: call `StatsRepo(db).snapshot(min_age=interval / 2)` at once, then
  every `interval` seconds (`Event.wait(interval)`), until stopped.
- Any exception is caught and logged with `logger.warning` (at most once
  per interval; no traceback spam), and the loop continues. A
  `DatabaseBusy` or lost connection must never stop the server.
- `serve()` in `app.py` creates and starts it after `make_server` returns
  and stops it in the `finally` before `server.server_close(...)`. Do not
  start it in `make_server` (test fixtures call `make_server` directly and
  must not get a thread). `serve()` gets a keyword
  `stats_interval=STATS_REFRESH`; `0` or `None` disables the loop (for
  tests that call `serve()`).

### 6. API: `GET /api/admin/stats`

- Route row next to the other admin rows:
  `_r("GET", r"/api/admin/stats", "server_admin", None, "admin_stats", "admin_stats")`,
  handler `_admin_stats(webauth, handler, route, match)` in `HANDLERS`.
- Response (`handler._json(..., headers=webauth._headers())`):
  `{"totals": {...}, "top_workspaces": [...], "history": [...],
  "refresh_seconds": STATS_REFRESH}`, history for 90 days.
- Non-admins get the same 404 as every other admin route (the dispatcher
  already filters `server_admin` routes). No CSRF on GET.
- Add `"admin_stats": {"method": "GET", "path": "/api/admin/stats"}` to
  `EXAMPLES` in `tests/test_server_isolation.py`; update the admin route
  list in `tests/test_server_workspace_routes.py` if it is exhaustive.

### 7. CLI: `agenticml server stats`

- `server_sub.add_parser("stats", ...)` with an optional positional
  `action` (`show` default, or `snapshot`), `--data`, `--database URL`,
  `--json`, `--days N` (default 30, `show` only).
- Open with `for_server_dir(directory, args.database, create=False)`;
  without a database: `agenticml server: no server database found for
  <data>; check --data and --database`, exit 2. Error and exit-code
  convention as `server status` (`_cmd_server_report`): 130 interrupted,
  1 `DatabaseError`, 2 usage (`OSError`, `ValueError`).
- `show`: totals as a two-column table (bytes with
  `agenticml.store.gc._format_bytes`), then top workspaces, then the
  history table (day, accounts, active 7d, workspaces active, stored,
  uploads, runs) for `--days`. `--json` prints
  `json.dumps({"totals", "top_workspaces", "history"}, indent=2, sort_keys=True)`.
- `snapshot`: forces a snapshot (`min_age=None`), prints
  `snapshot saved for <day>`; `--json` prints the stored row.

### 8. `/admin` Usage section (`app.js`)

- In `adminPage`, a new section at the top, under the "Admin" heading and
  above the workspaces table: `div.stack` > `div.heading` > `h2` "Usage",
  then `dl.usage-tiles` with five tiles:
  - **Accounts**: total; small line "+N in 7 days".
  - **Active users**: 7-day count; line "1 day: a · 30 days: b".
  - **Workspaces**: active count; line "S suspended · A archived".
  - **Storage**: `formatGb(stored_bytes)`; line "Uploads today: …".
  - **Runs**: total.
- Below, `div.chart-grid` with three `div.card.chart-card` (header `h3`,
  `div.chart-host` > `canvas` with an `aria-label`): "Accounts",
  "Active users (7 days)", "Storage (GB)". Use `MetricChart` with
  `compact: true`, one pseudo-run per chart
  (`runs: [{id: "accounts"}]`, `metrics: {accounts: {accounts: [{step, value}]}}`,
  `activeMetric`), `step` = days since 1970-01-01 of the row's `day`,
  `xFormat` printing "Nov 17" style dates (UTC).
- With fewer than two history rows, show `p.muted` "Growth charts appear
  after two daily snapshots." instead of the charts.
- Fetch `/api/admin/stats` on its own (not inside the existing
  `Promise.all`), so a failure shows an error in this section only
  (`showError`) and the rest of the page still works. Fetch on page load
  and on each `refresh()`.
- Destroy every chart on re-render and in the page's `cleanup`/`dispose`
  (MetricChart holds a ResizeObserver and a theme listener).
- No horizontal overflow at 375px; the grids already use
  `minmax(min(…,100%),1fr)`.

## Constraints and conventions

- SQL must work on SQLite and Postgres; `?` placeholders; no
  backend-specific functions in `totals` (dates are computed in Python).
- Every cross-workspace read uses `tx.cross_workspace('<reason>')`; on
  Postgres unbound reads of RLS tables return zero rows silently, so tests
  must run on Postgres too (CI does, via `AGENTICML_TEST_DATABASE_URL`).
- No new dependency, no new static file (keep everything in `app.js`; a
  new file would need a route row and an isolation example).
- Day boundaries are UTC and come from the database clock on Postgres.
- `stats_daily` and the API never contain emails or logins.
- Keep the existing per-workspace usage code and the limits untouched.

## Edge cases

- Empty server (no accounts or workspaces): all totals 0, no error;
  history empty; the UI shows tiles with 0 and the "appear after" note.
- Two server processes (Postgres) refreshing at once: the advisory lock
  serializes them; with `min_age` the second one skips; the row is never
  duplicated (primary key) and never errors.
- A process started just before midnight UTC: the next refresh writes the
  new day's row; the old day keeps its last state.
- Clock skew between processes: `day` and cutoffs come from the database
  clock on Postgres; `last_active_at` uses the process clock (as session
  times do); a skew of seconds does not matter at hour resolution.
- Account deleted (`server accounts delete`): its `last_active_at` goes
  with it; history rows are totals and are not rewritten.
- Env-key sessions (`account_id` NULL) and keys without an account:
  nothing is marked.
- `last_active_at` touch on a busy server: at most one extra
  `UPDATE accounts` per session or key touch (already throttled to once a
  minute), which matches no row unless an hour has passed.
- Refresher error (database down): logged, retried next interval; the
  server keeps serving.
- Server started via `make_server` only (tests, embedded): no refresher.

## Tests and checks

New `tests/test_server_stats.py` (runs on SQLite, and on Postgres when
`AGENTICML_TEST_DATABASE_URL` is set; mark nothing `sqlite_only` unless it
must be):

- `totals` on an empty database: every count 0.
- `totals` with two workspaces (one archived, one suspended, one custom
  plan), accounts created at different times, `last_active_at` at 2 h,
  3 days and 20 days ago, usage rows with stored and today's/yesterday's
  upload bytes, and runs in both workspaces: every field exact (this also
  proves the cross-workspace read works under row-level security).
- `mark_account_active`: sets, does not move within an hour, moves after
  an hour, ignores `None`.
- Session create and touch, and key touch (through `KeyStore.touch`), set
  `last_active_at` for the right account; the env key sets nothing.
- Migration `0010` backfill: build a database at version 9 (the pattern
  of `test_migration_0009_adds_archived_at_to_existing_workspaces`), add a
  session and a used key, reopen at 10: `last_active_at` is the later of
  the two; an account with neither is NULL.
- `snapshot`: writes today's row; a second call with `min_age` returns
  False; without `min_age` it overwrites; a different `now` day writes a
  second row; `history(days)` returns rows ascending and respects the
  window.
- `StatsRefresher`: start/stop joins the thread within the timeout; with a
  short interval it takes a snapshot; an exception in `snapshot` is logged
  and the loop keeps running.
- CLI: `server stats` (table and `--json`), `server stats snapshot`,
  missing database exits 2 with the message, `--days` window.
- API: admin gets 200 with `totals`, `top_workspaces`, `history`,
  `refresh_seconds`; a non-admin member gets 404; anonymous gets the same
  as other admin routes.
- `tests/test_server_isolation.py` and
  `tests/test_server_workspace_routes.py` updated and passing.

UI (`testbed/tests/test_server_admin_dashboard.py`):

- The Usage section shows the five tiles with the seeded numbers.
- With two seeded `stats_daily` rows, three chart canvases render; with
  none, the note shows and no canvas.
- `assert_no_overflow` at 375 px with the Usage section present.
- No console errors (the `page` fixture fails on them).

Commands:

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- With a local Postgres if available:
  `AGENTICML_TEST_DATABASE_URL=... testbed/.venv/bin/python -m pytest -q tests/test_server_stats.py tests/test_server_rls.py tests/test_server_postgres.py`.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/`.

## Docs

- `docs/SERVER.md`: a "Usage history" section: what each number means,
  the hourly refresh and what a day's row holds, `agenticml server stats`
  and `stats snapshot`, that nothing is sent anywhere, and that history
  holds totals only.
- `deploy/aws/README.md` "Operations and scaling": `agenticml server
  stats --data /data` over ECS Exec next to `workspaces usage`, and that
  `/admin` shows the same.
- `website/src/content/docs/legal/privacy.md`: one sentence in the staff
  access section that admins also see server-wide daily totals (number of
  accounts, active accounts, workspaces, storage, runs), which contain no
  personal data, and that each account's last activity time is stored to
  count active accounts. Update "Last updated". Flag it for the owner in
  the PR.

## Review notes (Nov 18, 2026)

Paused by the owner on Nov 18 after the backend was coded, resumed the same
day. Five review lenses (correctness, concurrency, security and privacy,
tests, simplicity), each finding checked by two skeptics: 16 findings
confirmed, 4 refuted. Fixed:

- Activity marking could deadlock with `server accounts delete` (key row
  then account row, against the opposite order in `AccountRepo.delete`;
  reproduced on Postgres) and could stall sign-ins behind an account row
  lock. On Postgres `mark_account_active` now uses `FOR NO KEY UPDATE SKIP
  LOCKED` and never waits; marking is best effort at one-hour resolution.
- `StatsRefresher.stop()` joins until the snapshot in progress ends (the
  database timeouts bound it), so nothing writes after the server lock is
  released.
- `history(N)` returns N days ending today (was N + 1); `snapshot()`
  returns the stored row; `--days` is 1–3650 and an overflow exits 2.
- The migration backfill skips keys without an account and updates in
  chunks of 500.
- An existing test (`test_membership_roles_invites_and_key_sessions`) now
  ignores `last_active_at`; stats tests assert exact values and cover the
  503, exit 1 and exit 2 paths, the refresher log and a prompt stop.

Accepted as is: one query per total (14 small counts, once an hour and on
`/admin` loads); the API reports the default refresh interval.

## Done when

`/admin` shows current totals and growth charts from `stats_daily`; the
server refreshes today's row every hour on SQLite and Postgres, also with
two processes; `agenticml server stats` prints the same numbers; the repo
test suite and the UI tests pass, with no overflow at 375 px; docs and the
roadmap row are updated.

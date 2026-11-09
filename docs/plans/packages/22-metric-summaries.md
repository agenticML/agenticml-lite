# Package 22: Metric summaries; sort and filter runs by metric

Nov 9, 2026 · Supervisor plan · Release 3.0 · Coder: Cursor · Size: M

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), "Metric summaries
(package 22)", the `run_metrics` row of the tables section, exit
criterion 4 and the package summary. Builds on [17](17-runs-in-database.md)
(append transaction, `RunRepo.record_file`), [18](18-workspaces.md)
(workspace routes, isolation suite) and [19](19-postgres.md) (RLS on every
workspace table).

## Goal

The server keeps one summary row per run and scalar series (last, min,
max, count, last step), updated in the same transaction that commits the
appended bytes. The runs API can return, sort and filter by a summary
without opening run files, and the server dashboard's runs page gets a
metric picker that shows and sorts by it. Metric points stay in v1 files.

Out of scope: summaries in the local `agenticml ui` (no database; it keeps
its current behaviour and hides the picker), metric points in the
database, charts.

## Design

### Table (`db/migrations/0007_run_metrics.py`, new)

```sql
CREATE TABLE run_metrics (
  workspace_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  series_id INTEGER NOT NULL,          -- the <id> in scalars/<id>.bin
  name TEXT,                           -- from scalars/index.json; NULL until known
  last_value DOUBLE PRECISION,         -- NULL when the last value is NaN
  min_value DOUBLE PRECISION,          -- over non-NaN values; NULL if none
  max_value DOUBLE PRECISION,
  count BIGINT NOT NULL,               -- records, NaN included
  last_step BIGINT,                    -- step of the last record in append order
  decoded_bytes BIGINT NOT NULL,       -- bytes of the .bin already folded in
  PRIMARY KEY (workspace_id, run_id, series_id),
  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, run_id) ON DELETE CASCADE
);
CREATE INDEX run_metrics_name ON run_metrics (workspace_id, name);
```

Use the dialect's type placeholders the same way earlier migrations do
(SQLite `REAL`, Postgres `DOUBLE PRECISION`; check how 0002/0005 spell
types and the runs primary key after 0003). On Postgres call
`dialect.enable_workspace_rls(tx, 'run_metrics')` (required by the 0006
comment). ±inf are stored as-is (both backends accept them); NaN is never
stored.

Semantics, matching how the dashboard reads series:

- `last_value`/`last_step`: the last record in **append order** (the most
  recent point the run wrote), not the largest step.
- `min_value`/`max_value`: over non-NaN values; ±inf count.
- `count`: all records including NaN.
- `.ovw` files, `_time.bin` and `index.json` are not series.

### Update on ingest (`ingest.py`, `db/repos.py`)

- New pure helper `server/metrics.py`: `fold(summary, data: bytes) ->
  summary` decodes `<If` records (8 bytes; `layout.RECORD_SIZE`) with
  `struct.iter_unpack` and folds them into a summary dict;
  `summarize_file(path, size)` reads a committed prefix of a `.bin` in
  1 MiB chunks and folds it from scratch.
- Append to `scalars/<n>.bin` (not truncate): after the bytes are written
  and fsynced, read exactly the range `[old committed size, new size)`
  from the destination file (bounded by the upload size) and pass the
  folded result into `RunRepo.record_file(..., metric=(series_id,
  summary))`, which upserts `run_metrics` in the **same** transaction as
  the size update, only if `expected_size` matched. Guard: if the stored
  `decoded_bytes` is not equal to the old committed size (should never
  happen), recompute from the whole committed file instead of folding.
- Truncating replace (`truncate=1`, offset 0) of a `.bin`: recompute from
  the whole new file.
- Atomic upload of `scalars/index.json`: parse it (bounded; existing
  JSON limits) and, in the same transaction as its `record_file`, set
  `name` for each `(series_id → name)` in it (and NULL names whose id is no
  longer present). Ignore an unparsable index (summaries keep their
  names); never fail the upload because of it.
- An append that arrives before the index (id not named yet) stores
  `name = NULL`; it gains a name when the index arrives. On every `.bin`
  append, if the row's `name` is NULL, also look the name up from the
  committed `scalars/index.json` on disk (cheap, bounded) so out-of-order
  sync still converges.
- Run deletion/discard: rows go with the run (`ON DELETE CASCADE`, and
  any explicit run-file cleanup paths that delete `run_files` rows also
  delete `run_metrics` rows for that run).

### Rebuild and backfill

- `RunRepo.rebuild_metrics(run_id, root)`: recompute every series of one
  run from the committed sizes in `run_files` (never past the committed
  size) and the current index; replace that run's rows in one
  transaction.
- `agenticml server reindex` also rebuilds metric summaries for every run
  it indexes.
- New `agenticml server metrics rebuild [--workspace SLUG] [--run ID]
  [--data DIR] [--database URL]` for operators (offline or online; takes
  each run's run lock while rebuilding that run, as reindex does).
- First start after this migration: existing runs have no rows. At serve
  start, if `server_settings` has no `metrics_backfilled` entry, rebuild
  every run in every workspace (under `advisory_lock('metrics-backfill')`
  so only one process does it on Postgres; others skip when the setting
  appears), logging progress every 1000 runs, then write the setting.
  Runs synced during the backfill are safe because each run is rebuilt
  under its run lock.

### API (`server/routes.py`, `ui/server.py` runs handler, `RunRepo`)

- `GET /api/w/<slug>/runs` (and the legacy `/api/runs` alias it already
  has) accepts optional query parameters; with none, the response is
  byte-for-byte what it is today:
  - `metrics=loss,acc` (≤ 8 names, each ≤ 200 chars): each row gains
    `"metrics": {"loss": {"last":…, "min":…, "max":…, "count":…,
    "last_step":…}}` for the named series it has. Non-finite numbers are
    encoded the way the existing scalars API encodes them (it maps them to
    null; keep the same rule and document it in `docs/PROTOCOL.md`'s
    dashboard section if that section lists the runs API, otherwise in
    the code docstring).
  - `sort=metric:<name>:<last|min|max>` with `order=asc|desc` (default
    `asc`); runs without that metric (or with a NULL value) sort last in
    both orders; ties broken by `created_at` desc, then `run_id`. Other
    `sort` values are 422.
  - `filter=metric:<name>:<last|min|max>:<lt|le|gt|ge|eq>:<number>`,
    repeatable (≤ 8), all must hold; a run without the metric does not
    match. Bad syntax or a non-finite number → 422 with a short message.
  - Names with `:` are allowed in `metrics=` (split on the first-level
    comma) but `sort`/`filter` split the name as everything between the
    first `metric:` and the last two (filter: three) `:`-separated fields.
  - Sorting and filtering happen in SQL (a join on `run_metrics` by
    `workspace_id` and `name`), never by reading files. Rows with the same
    `name` in two series ids of one run cannot happen (index ids are
    unique per name); if the index changed names, take the row with the
    highest `series_id`.
- `GET /api/w/<slug>/metrics` (new, `read` permission, registered in the
  route table so the isolation suite covers it): `{"metrics": [names…]}`,
  distinct non-NULL names in the workspace, sorted, capped at 2000.
- Both are workspace-bound like every other route; a non-member gets the
  same 404 as a nonexistent workspace.

### Dashboard (`ui/static/app.js`, `app.css`)

- On the runs page in server mode, fetch `/metrics` once per load. If it
  answers with at least one name, show a compact "Metric" control in the
  runs toolbar: a `<select>` of names (with a "None" option) and a
  `<select>` of `last`/`min`/`max`. Local mode (`/metrics` 404) shows
  nothing.
- When a metric is picked, re-fetch the runs list with
  `metrics=<name>` and show the value as a small right-aligned column in
  each run row (formatted with the existing `fmt`), and sort runs within
  each task group by it (lower first for names that `lowerBetter` treats
  as losses, else higher first; missing last). Remember the choice in
  `localStorage` per workspace (try/catch).
- The existing filter box gains `<name>.<last|min|max><op><number>`
  tokens (e.g. `loss.min<0.5`) that test `run.metrics` when present; when
  the token's metric has not been fetched, fetch it (add it to
  `metrics=`) before filtering.
- No horizontal page overflow at 375px: the metric column wraps under the
  row or is hidden below 480px with the value shown inline.

## Edge cases

- NaN everywhere → `min`/`max`/`last` null, `count` > 0.
- inf values: `max` = inf; JSON gets null per the existing encoding.
- Restarted series (step goes backwards, `monotonic: false`): `last` is
  the last appended record, not the max step.
- Torn tail on disk from a crash: never folded (only committed bytes are
  read).
- Very large series (millions of points): appends fold only the new
  bytes; rebuild streams in 1 MiB chunks.
- Two processes appending to the same series: already serialised by the
  run lock (package 20); the summary update is inside `record_file`'s
  transaction.
- Metric names are client-controlled strings: never interpolated into
  SQL (parameters only) and escaped in the UI (use `el(..., {text})`).

## Tests and checks

- `tests/test_server_metrics.py` (new):
  1. Corpus test: generate runs with the SDK covering normal series, NaN,
     ±inf, a restarted (non-monotonic) series, a series with 0 points, one
     with > 65,536 points (overview file present), several flush
     batches; sync; for every series assert the row equals a full read
     through `store.reader` (last/min/max/count/last_step).
  2. Incremental: sync, write more points, sync again (append path), and
     a `truncate=1` replace; summaries match a full read each time.
  3. Out-of-order: append a `.bin` before `scalars/index.json` (raw v2
     requests); name is NULL, then set when the index arrives.
  4. Crash between write and commit (the package 17 test hook): no
     summary change; the next sync completes and summaries match.
  5. API: `metrics=`, `sort` asc/desc with missing values last, every
     filter op at, just under and just over a boundary, repeated filters,
     422 on bad syntax / non-finite numbers / > 8 names; no parameters →
     identical response to before. Prove no file reads: monkeypatch
     `open`/`RunReader` to fail during the request.
  6. `/metrics` lists names per workspace; the isolation suite covers both
     routes automatically (route table).
  7. `server metrics rebuild` and `reindex` restore deleted rows; first
     start backfill fills rows for runs synced before the migration (drop
     the rows and the setting, restart, assert rebuilt once).
  8. Postgres: the whole file runs on Postgres too; an RLS test shows a
     query on `run_metrics` without the workspace binding returns nothing
     (follow `tests/test_server_rls.py`).
- UI (`testbed`, `-m ui`): picker appears only in server mode, sorting
  by a metric orders rows, the `loss.min<0.5` filter token, choice
  remembered, and zero horizontal overflow at 375px.
- Checks: `testbed/.venv/bin/python -m pytest -q tests`, the Postgres
  server suite with `AGENTICML_TEST_DATABASE_URL`, and
  `.venv/bin/python -m pytest -q -m ui` from `testbed/`.

## Done when

Summaries match a full read for every series in the corpus (NaN, inf,
restarts included) on both backends; the sort/filter API is tested; the
UI suite passes with no horizontal overflow at 375px; exit criterion 4
holds.

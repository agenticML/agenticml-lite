# Fix: timeout-sensitive tests in the Postgres CI job

Maintenance fix, found on Nov 17, 2026 in PR #83 (main into dev): the
"Postgres tests" job (run 44143063569, attempt 1) failed 2 of 3,345 tests with
timeouts, and the same commit passed in run 44138198575 and on rerun. That run
took 1178s against 914s for the passing one, so the runner was slow.

## Goal

Tests should not fail just because a CI runner is slow. Their checks must stay
the same. Only wait limits change: an HTTP read timeout or a wait for a future
or event that ends as soon as its condition is met. A test that passes takes
about the same time as before.

## Failures seen

1. `tests/test_server_many_processes_e2e.py::test_two_process_sync_kill_restart_matches_single_process`
   timed out at `future.result(timeout=30)` (first push phase, about line 241).
   Both servers run with `AGENTICML_TEST_APPEND_DELAY_MS=1500`, and the
   workspace quota lock is held across the append delay, so appends from the 4
   push threads run one at a time. Nine runs with several appends each,
   1.5s per append, come close to the 30s per-future limit on a slow runner.
2. `tests/test_server_metrics.py::test_first_start_backfill_rebuilds_once`
   failed with `requests.exceptions.ReadTimeout` (read timeout=5) on
   `PUT /v2/runs/run-1/files/scalars/index.json` against an in-process server
   backed by Postgres.

## Files to touch

- `tests/test_server_many_processes_e2e.py`
- `tests/test_server_metrics.py`
- Other tests that build `Client(..., timeout=5, ...)` against a test server:
  `tests/test_server_backup.py`, `tests/test_server_convert_2x.py`,
  `tests/test_server_isolation.py`, `tests/test_server_runs_db.py`,
  `tests/test_hosted_check.py`, `tests/test_server_teams.py`
- `docs/plans/on-prem-roadmap.md` (Maintenance fixes entry)
- No source code under `src/` changes.

## Approach

1. In `test_server_many_processes_e2e.py`, raise these limits:
   - `RecordingClient(..., timeout=10, ...)` in `push`: 10 → 30.
   - `duplicate_barrier.wait(timeout=10)` in `RaceAdapter.send`: 10 → 30.
   - First push phase `future.result(timeout=30)`: 30 → 120.
   - `both_appending.wait(15)`: 15 → 60.
   - The `deadline = time.monotonic() + 15` loop that looks for the run-10
     append gap: 15 → 60.
   - After the kill, `future.result(timeout=20)`: 20 → 60.
   - `ping` (`requests.get(..., timeout=5)`): 5 → 30.
   Leave `Server.ready()` (15s), `Server.close()` (`wait(timeout=5)`), the
   `cli()` subprocess timeout (20s) and `a.process.wait(timeout=5)` after
   SIGKILL alone unless they look as tight to you (report it; don't change
   them silently).
2. In every listed test file, change `Client(..., timeout=5, ...)` to
   `timeout=30`. Use a module-level `CLIENT_TIMEOUT = 30` only if a file has
   several of them; otherwise use the literal. Keep `max_retries` as it is.
3. Before changing a timeout, check that the test does not depend on it:
   none may assert a `ReadTimeout`/`Timeout`, measure elapsed time against
   it, or expect a request to time out. Leave any such timeout alone and
   report it.
4. Add an entry under "Maintenance fixes" in `docs/plans/on-prem-roadmap.md`
   linking to this plan, with status `done (<PR link>)` set when the PR exists
   (the supervisor fills the link).

## Constraints and conventions

- Test-only change. Don't change server behavior, test hooks, or what any
  test checks.
- Keep the existing style (single quotes in tests, no new helpers unless one
  file needs a constant).
- Don't touch timeouts that a test deliberately keeps short to check
  timeout behavior.

## Edge cases

- The kill phase expects `requests.ConnectionError` from the killed server.
  A longer read timeout must not turn that into a hang: SIGKILL closes the
  socket, so the client sees a connection error at once. Keep that check as
  it is.
- A longer `future.result` timeout only matters when something is slow. A
  test that fails still fails, just later. The job's own 30-minute limit
  still applies.

## Tests and checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root (SQLite;
  the many-process test is skipped without a database URL).
- With a Postgres 16 available, run the two failing tests and the changed
  files with `AGENTICML_TEST_DATABASE_URL=postgresql://...` set.
- CI "Postgres tests" job passes on the PR.

## Done when

- The listed timeouts are raised, nothing a test checks has changed, and
  the full suite passes locally (SQLite) and in CI (all jobs, including
  Postgres).
- The roadmap has the Maintenance fixes entry with the PR link.

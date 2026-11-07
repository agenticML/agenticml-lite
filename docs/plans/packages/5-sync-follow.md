# Package 5: `sync --follow` and a systemd example

Nov 5, 2026 · Supervisor plan · Release 2.0 · Coder: Cursor · Size: M

Source: [on-prem-server-plan.md](../on-prem-server-plan.md), sections 4.6,
5.1 and 7 (package 5). Roadmap: [on-prem-roadmap.md](../on-prem-roadmap.md).
Builds on [3-sync-client-v2.md](3-sync-client-v2.md) (the v2 sync client)
and [2-remote-status.md](2-remote-status.md) (`stale` after
`AGENTICML_REMOTE_STALE_SECONDS`, default 600). Base branch: `dev`.

## Goal

A training machine runs one long-lived command, `agenticml sync --follow`,
usually under systemd. It:

- picks up new runs without a restart,
- keeps every running run's server status fresh (a commit at least every
  60 s), so a live run never shows `stale` on the server dashboard,
- survives the server being down or restarting, backing off up to 5 minutes,
- stops with an error when its key is rejected (401), instead of retrying
  forever,
- exits cleanly on SIGTERM (systemd stop) or SIGINT (Ctrl-C) after the
  current HTTP request, with the per-file cursor saved.

Out of scope: lineage, by-reference artifacts (2.1), notes sync, a Windows
service example, changing `--watch`.

## What exists today

- `cmd_sync` (`src/agenticml/sync.py`, around line 860) has a `--watch
  SECONDS` loop. Each pass calls `_selected_ids` (which rescans `list_runs`
  when `--all` is set and skips idle runs), syncs each selected run with
  `sync_run`, and prints one `SyncResult.line()` per run. It stops when every
  result is complete, permanent or stale.
- Every non-idle pass of `_sync_locked` ends with `client.commit`, so a
  running run commits on every pass. With a 30 s interval the heartbeat comes
  for free; the loop only has to make sure it never sleeps more than 60 s
  while a run is running.
- `Client._request` (`client.py`) already retries connection errors,
  timeouts and 5xx up to `max_retries=3` with a sleep (`_backoff`), and
  honours `Retry-After` on 503.
- `_permanent_failure` and `_short_error` classify errors (PROTOCOL.md
  section 10). `_identity` caches `whoami` on the client.
- KeyboardInterrupt in `cmd_sync` prints a summary and returns 130.

## Files to touch

- `src/agenticml/sync.py`: the follow loop (a new function, called from
  `cmd_sync`).
- `src/agenticml/client.py`: an optional stop event, checked before each
  request and used for the retry sleep.
- `src/agenticml/cli.py`: `--follow` and `--interval` on `p_sync`.
- `deploy/systemd/agenticml-sync.service` (new) and
  `deploy/systemd/README.md` (new, short: install, enable, logs, stop).
- `tests/test_sync_follow.py` (new).
- `docs/PROTOCOL.md` section 10 (add `--follow` next to `--watch`) and the
  sync lines in `docs/SDK.md`. Keep these edits short; package 6 writes the
  full server docs.

## Approach

### 1. CLI

- `--follow`: keep syncing until stopped. Without run ids it implies `--all`.
  With explicit run ids it follows only those, and exits 0 once every one of
  them is complete or has failed permanently (as `--watch` does).
- `--interval SECONDS`: the pause between passes, default 30. It must be a
  positive finite number (exit 2 otherwise, same message style as `--watch`).
  `--interval` without `--follow` is an error (exit 2).
- `--follow` cannot be combined with `--watch`, `--dry-run` or `--reset`
  (exit 2 with a one-line message). `--prune` is allowed.

### 2. Follow loop (`sync.py`)

Write it as its own function, for example `_follow(args, root, host, api_key)`,
rather than adding more branches to the `--watch` loop. Share the per-run
code (`sync_run`, `SyncResult`, `_selected_ids`, `_short_error`,
`_permanent_failure`) instead of copying it. Module constants:
`FOLLOW_INTERVAL = 30.0`, `HEARTBEAT = 60.0`, `BACKOFF_MAX = 300.0`.

Each pass:

1. **Identity.** If the client has no cached identity yet, call
   `_identity(client)`. Treat "unreachable" errors (below) as a failed pass;
   don't exit.
2. **Select.** `_selected_ids(root, host, run_ids, include_all=not run_ids,
   prune=..., key=identity["key"])`. That rescans the store, so new runs
   appear. Drop run ids that failed permanently earlier in this process (see
   step 5).
3. **Sync** each selected run as `cmd_sync` does today. Between runs, check
   the stop event.
4. **Classify** the pass:
   - **Unauthorized:** any 401 (from `whoami` or any run), i.e. an
     `HTTPError` with `response.status_code == 401`. Print
     `agenticml sync: API key rejected — run \`agenticml login\`` to stderr
     and exit with status **3**. A distinct status lets the systemd unit stop
     restarting (`RestartPreventExitStatus=3`). Stop the pass at the first
     401; don't try the remaining runs.
   - **Unreachable:** `requests.ConnectionError`, `requests.Timeout`,
     `ChunkedEncodingError`, an `HTTPError` with status 5xx, 408 or 429
     (after the client's own retries), or `RunMissingError`. Stop the pass at
     the first such error (the server is probably down for every run), and
     back off before the next pass.
   - **Permanent** per-run failures (`_permanent_failure(exc)` true, except
     401): print the run's line once and add the run id to an in-memory set
     so it is not retried until the process restarts. Other runs carry on.
     With explicit run ids, a permanent failure counts as "done" for the exit
     check.
   - **ProtocolError** from `whoami` (the host is not a protocol 2 server):
     exit 1 at once, as today.
5. **Sleep.**
   - After a pass with no unreachable error: reset the backoff, and wait
     `interval`, or `min(interval, HEARTBEAT)` if any run in this pass was
     running (its result status is `partial (running)`, or `effective` was
     `running`).
   - After an unreachable pass: `delay = min(interval * 2 ** k, BACKOFF_MAX)`
     where `k` counts consecutive unreachable passes, starting at 0, then
     sleep `random.uniform(delay / 2, delay)` (jitter). Print one line per
     backoff: `server unreachable (<short error>); retrying in <n>s`.
   - Every sleep is `stop.wait(seconds)`, so a signal ends it at once.
6. **Explicit ids:** after a pass, if every requested run is complete or
   permanent, exit 0 (1 if any failed permanently, matching `--watch`).

**Output.** A run's line is printed when it sent data (`files`, `objects` or
`bytes` > 0), when its status differs from the last line printed for it in
this process, or when it fails. A heartbeat-only commit of an unchanged
running run prints nothing, so the journal does not fill with one line per
run every 30 s. Print `following <host>: <n> runs, every <interval>s` once
at start. Don't print `up to date` on every pass.

### 3. Stopping on SIGINT / SIGTERM

- `Client.__init__` takes an optional `stop` (a `threading.Event`, default
  `None`). In `_request`, if `stop` is set before an attempt starts, raise a
  new `SyncStopped` exception (in `client.py`; subclass `Exception`, not
  `requests` errors, so no classifier mistakes it for a network error).
  `_backoff` waits with `stop.wait(delay)` instead of `time.sleep`, and
  raises `SyncStopped` if the event is set. With `stop=None`, behaviour is
  unchanged.
- In follow mode, on the main thread only, install handlers for SIGTERM and
  SIGINT that set the event and record which signal arrived. On the first
  SIGINT, restore the default SIGINT handler, so a second Ctrl-C interrupts
  at once (KeyboardInterrupt, exit 130 as today). Restore the previous
  handlers in `finally`.
- The request in flight completes (we never interrupt a socket). The next
  `_request`, between-run check or sleep sees the event. `SyncStopped`
  propagates out of `sync_run`. Its `finally` releases the run lock, and the
  cursor is already saved after every append and atomic write.
- On stop, print the summary line in the same format as today's
  `Interrupted: complete=… partial=… failed=…`, and exit **0** for SIGTERM
  and **130** for SIGINT.

### 4. systemd example

`deploy/systemd/agenticml-sync.service`, a **user** unit (so it reads the
same `~/.agenticml` store and `agenticml login` credentials as the person who
trains):

```ini
[Unit]
Description=AgenticML sync to the team server
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
ExecStart=%h/.local/bin/agenticml sync --follow
Restart=on-failure
RestartSec=30
# Exit status 3 means the API key was rejected: fix it with agenticml login.
RestartPreventExitStatus=3
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=default.target
```

Comments at the top of the file give the install steps: copy it to
`~/.config/systemd/user/`, fix the `ExecStart` path (`which agenticml`), run
`systemctl --user daemon-reload` and `systemctl --user enable --now
agenticml-sync`, and run `loginctl enable-linger $USER` so it runs without
a login session. Logs: `journalctl --user -u agenticml-sync -f`. Mention
`Environment=AGENTICML_DIR=...` for a non-default store. No hardening
directives that could block access to the home directory (`ProtectHome`).
`deploy/systemd/README.md` repeats the steps in a few lines.

## Constraints and conventions

- Standard library plus the existing `requests` dependency; no new packages.
  Python 3.9 compatible.
- `--watch`, plain `sync`, `--reset` and `--dry-run` behave exactly as
  today. Every existing test in `tests/test_sync.py`, `test_client.py` and
  `test_auth.py` passes unchanged.
- Never print the API key. Error lines go through `_short_error` and
  `_clean_detail`, as today.
- Match the surrounding code: small helpers, sparse comments that explain
  why.
- Report any deviation from this plan instead of changing the approach.

## Edge cases

- The server is down at start: the loop backs off on `whoami` and syncs
  once the server is back. It does not exit.
- The server restarts mid-run (connection reset mid-append): that counts as
  unreachable, and the next pass resumes from the saved cursor (package 3's
  replay rules prevent duplicates).
- The key is revoked while following: the next request gets 401, and the
  process exits 3.
- A new run is created while following: it is picked up on the next pass.
- A run finishes while following: its last pass sends the final files and
  commits `finished`. After that it is idle and no longer selected.
- Another `sync` holds a run's lock: the result is `skipped`. Not a failure;
  retry on the next pass.
- `--interval 120` with a running run: the loop still syncs every 60 s.
- The clock jumps: the loop uses `Event.wait` (monotonic), not wall-clock
  deadlines.
- `serve` and `sync --follow` on one machine: no shared state beyond HTTP.

## Tests (`tests/test_sync_follow.py`)

Use the in-process package 1 server fixtures from `tests/test_sync.py`
(`remote`, `writer_at`, `command`). Keep every test fast: tiny `--interval`
(e.g. `0.01`), and patch `HEARTBEAT`/`BACKOFF_MAX`, `random.uniform` and the
stop event where needed. Run the loop in a worker thread with a join timeout
so a bug fails the test instead of hanging it.

1. **Server down, then back.** Point the client at a port that refuses
   connections (or stop the fixture server), start `--follow`, check that the
   backoff lines appear and that the delays grow and are capped (patched
   values). Then bring the server up on that port, and the run syncs byte for
   byte (reuse `identical`).
2. **New run picked up.** Start following an empty store, create a run, and
   it reaches the server without a restart.
3. **Heartbeat.** A running run with no new data commits at least once per
   (patched) `HEARTBEAT`, and `remote.json` `last_sync_at` advances, so the
   server status stays `running` and not `stale` (set
   `AGENTICML_REMOTE_STALE_SECONDS` small and check the dashboard API
   status, or check `last_sync_at` directly). With `--interval` larger than
   the heartbeat, the sleep is capped.
4. **401 exits 3.** Revoke the key while following (or start with a
   revoked key): exit status 3, the stderr message, and no further requests.
5. **Signal exit.** SIGTERM to a real `agenticml sync --follow` subprocess
   against the fixture server exits 0 promptly with the summary line.
   Setting the stop event during an append lets that request finish and
   raises no further requests. The cursor in `sync.json` matches what the
   server has, and the next plain `sync` finishes the run with no duplicate
   bytes. SIGINT exits 130.
6. **Explicit ids.** `sync RUN --follow` exits 0 once the run is finished
   and synced.
7. **CLI validation.** `--follow` with `--watch`, `--dry-run` or `--reset`,
   `--interval 0`/`nan`, and `--interval` without `--follow`: each exits 2.
8. **Quiet heartbeats.** An unchanged running run prints its line once, not
   every pass.
9. **Client stop event.** A unit test in `tests/test_client.py`: with
   `stop` set, `_request` raises `SyncStopped` before sending, and `_backoff`
   returns as soon as the event is set.

## Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- From `testbed/`: `.venv/bin/python -m pytest -q -m cli` (the testbed CLI
  sync tests were rewritten for protocol v2 in package 3 and must still pass).
- `systemd-analyze verify deploy/systemd/agenticml-sync.service` if it is
  available (warnings about the `%h` ExecStart path are fine).

## Deviations accepted in review

- With explicit run ids, a `stale` run also counts as done, as it does for
  `--watch`. A writer that stalls past the stale window therefore ends the
  follower with exit 0.
- `Client._backoff` raises `SyncStopped` when the stop event is set, instead
  of returning, so the retry loop never sends one more request.
- `Client.whoami` raises `HTTPError` for 408, 429 and 5xx, so the follower
  backs off instead of treating them as "not a protocol 2 server".

## Done when

The tests above pass, and the roadmap shows package 5 as `done` in its PR.

# Backlog fixes before package 5 and 6

Nov 5, 2026 · Supervisor plan · Release 2.0 · Base branch: `dev`

Source: the Backlog section of [on-prem-roadmap.md](../on-prem-roadmap.md).
This plan covers all five entries. Coders do not edit the roadmap; the
supervisor removes each fixed entry from the Backlog once its task is
verified, so two coders never edit that file at the same time. Tasks B and D are security-sensitive (†):
Sonnet reviews them read-only, and the coder that did not write them gives a
second read-only review.

| Task | Entry | Files | Coder |
| --- | --- | --- | --- |
| A | Graceful SIGTERM in `agenticml server serve` | `server/app.py`, `cli.py`, `tests/test_server_lifecycle.py` (new) | Codex |
| B † | Throttle failed dashboard logins, warn on a weak env key | `server/webauth.py`, `server/keys.py`, `server/app.py` (`make_server` only), `tests/test_server_login_throttle.py` (new) | Cursor |
| C | Tests workflow on pushes to `dev` | `.github/workflows/test.yml` | Codex |
| D † | FIFO at a run file hangs the reader | `store/layout.py`, `tests/test_store.py` | Codex |
| E | Flaky lineage focus-trap UI test | `testbed/tests/test_task_groups.py` | Cursor |

Codex runs A, then C and D. Cursor runs B, then E. A and B both touch
`server/app.py`, but in different functions: A changes `serve` and
`IngestServer.server_close`, and B adds a few lines to `make_server`. Don't
reformat anything else in that file.

Conventions for every task: standard library only, no new dependencies,
Python 3.9 compatible. Match the surrounding style (short docstrings, sparse
comments that explain why). Log messages never contain key values.

## A. Graceful SIGTERM

**Problem.** `docker stop` and ECS send SIGTERM. Python's default handler
kills the process, so in-flight requests are cut off, and `server_close`
(join the workers, then release `server.lock`) never runs. Even on Ctrl-C,
`server_close` shuts down every open connection at once, including ones that
are in the middle of a request.

**Approach.**

1. In `serve` (`server/app.py`), when called on the main thread, install a
   SIGTERM handler before `serve_forever`. The handler starts a daemon thread
   that calls `server.shutdown()`. `shutdown()` must not run on the thread
   that is inside `serve_forever`, or it deadlocks. It sets a flag, so a
   second SIGTERM does nothing more. Restore the previous handler in
   `finally`. Skip the install when `serve` runs off the main thread
   (`signal.signal` raises `ValueError` there).
2. Add a drain step to `IngestServer.server_close`, as a keyword argument
   `drain=0.0` (seconds). The default of 0 keeps today's behaviour for tests
   and callers. With `drain > 0`, before the existing shutdown and join:
   - repeatedly shut down connections in state `"headers"` (idle keep-alive,
     or headers not yet read). No request is running on them.
   - wait (poll every 50 ms, or use a condition) until no connection is in
     any other state (`"body"`, `"busy"`, `"writing"`), or until the drain
     deadline passes.
   - then fall through to the existing code, which shuts down whatever is
     left and joins every worker.
   Handlers that finish a request during the drain go back to `"headers"`
   and are shut down on the next pass, so keep-alive clients cannot hold the
   drain open.
3. `serve` calls `server.close(drain=SHUTDOWN_DRAIN)` in its `finally`, with
   a module constant `SHUTDOWN_DRAIN = 10.0` seconds. Docker's default stop
   timeout is 10 s and ECS's is 30 s; keep a comment saying so. Drain on both
   SIGTERM and KeyboardInterrupt. If a second Ctrl-C interrupts the drain,
   the existing rule applies: the lock is kept while workers may still
   write.
4. After a SIGTERM shutdown, `serve` returns normally. `cmd_server_serve`
   (`cli.py`) prints `stopped` and returns 0, as it does today for
   KeyboardInterrupt.

**Edge cases.** A SIGTERM before `serve_forever` has started (the handler
thread's `shutdown()` waits for the loop to start, then stops it at once,
which is fine). A request that is still running at the deadline is cut off
as today. The local `agenticml ui` server (`ui/server.py`) is unchanged.

**Tests** (`tests/test_server_lifecycle.py`, new, Linux only, skip
without `fcntl` or `SIGTERM`):

- Start `python -m agenticml server serve --data <tmp> --port 0` (or the
  module entry point the CLI tests already use) as a subprocess, with an admin
  key created first through the keys CLI or `KeyStore`. Read the printed
  address. Open a raw socket, send the headers of a `/v2` append and half
  the body, send SIGTERM, wait about 200 ms, send the rest of the body.
  Expect a 2xx response, the bytes on disk, and exit status 0 within a few
  seconds. Afterwards `server.lock` can be taken again (start a second
  `make_server` on the same directory, then close it).
- An idle keep-alive connection does not delay exit: open one, complete a
  request, keep the socket open, send SIGTERM, and the process exits well
  inside the drain window.
- Unit level: `server_close(drain=0)` behaves exactly as before (existing
  tests cover this; they must keep passing).

**Done when** the first test passes. The supervisor then removes the
Backlog entry "`agenticml server serve` has no SIGTERM handler".

## B †. Throttle failed dashboard logins

**Problem.** `POST /login` (`WebAuth._login`) has no rate limit. Generated
keys carry 256 bits, but `AGENTICML_SERVER_KEY` is chosen by the operator
(32–256 characters) and may have much less entropy.

**Approach.**

1. Add a small `LoginThrottle` class in `server/webauth.py`, keyed by the
   client address string (`handler.client_address[0]`). Thread-safe (own
   lock). It takes a `clock` argument; `WebAuth` passes a new constructor
   argument `throttle_clock=time.monotonic`, so tests can inject a fake
   clock.
   - Per address: failure count, time of the last failure, and the earliest
     time the next attempt is allowed.
   - The first `FREE_FAILURES = 5` failures add no delay. After that, failure
     *n* sets the next allowed time to `now + min(1 s * 2 ** (n - 5), 60 s)`.
   - An entry is forgotten 15 minutes after its last failure.
   - At most 1024 entries (`OrderedDict`). When full, evict the entry whose
     last failure is oldest, so memory stays bounded however many addresses
     an attacker uses.
   - A successful admin login clears that address's entry.
2. In `_login`, after the CSRF check and before reading the body: if the
   address is throttled, answer `429 {"error": "too many failed logins; try
   again later"}` with `Retry-After: <whole seconds, rounded up>` and the
   usual `Cache-Control: no-store`, and set `close_connection = True` (the
   body is left unread). The key is not checked, so throttled attempts reveal
   nothing.
3. Only the 401 "invalid key" result counts as a failure. A valid sync key
   (403) is not a guess: it neither counts nor clears the entry. A malformed
   body (400) does not count either.
4. Responses never sleep. Holding a handler thread per failed attempt would
   let an attacker fill every connection slot.
5. Weak environment key warning: add `env_key_looks_weak(value)` in
   `server/keys.py`. It estimates `len(value) * log2(number of distinct
   characters)` and returns True below 128 bits. `make_server`
   (`server/app.py`) logs one warning when the env key is set and weak:
   `"AGENTICML_SERVER_KEY looks low-entropy; generate one with agenticml
   server keys create or secrets.token_urlsafe(32)"`. Never log the value.
6. Check how `login.js` shows errors. If it only shows the JSON `error`
   field, the 429 message needs no change there. If it maps status codes,
   add 429 with the server's message.

**Known limitation (write it as a code comment and in the PR body).** Behind
a reverse proxy every client has the proxy's address and shares one bucket,
so someone sending bad keys can delay the admin's sign-in by up to 60 s at a
time. The admin can still use the keys CLI. Package 13 (trusted identity
proxy) is where to key the throttle by the forwarded address, from configured
proxy addresses only. The supervisor adds that to package 13's roadmap
bullet. Never trust `X-Forwarded-For` here.

**Tests** (`tests/test_server_login_throttle.py`, new; reuse the helpers in
`tests/test_server_web.py` for the server, CSRF headers and login posts):

- Unit tests for `LoginThrottle` with a fake clock: 5 free failures; the
  delays grow 1, 2, 4 … and are capped at 60 s; entries expire after 15
  minutes; success clears; the 1024-entry cap evicts the oldest.
- Through HTTP: repeated wrong keys from one address get 429 with
  `Retry-After`, and a correct key from that address is also refused while it
  is throttled. A correct admin key from a different address still signs in
  at once. Use `127.0.0.1` and `127.0.0.2` on Linux (the whole of 127/8 is
  loopback), or inject the address at the `LoginThrottle` level if binding a
  client to `127.0.0.2` is not reliable.
- A sync key (403) and a malformed body (400) do not count.
- `env_key_looks_weak`: `"a" * 64` and `"password" * 4` are weak;
  `secrets.token_urlsafe(32)` is not. `make_server` with a weak env key logs
  the warning (caplog), and the key value is not in any log record.

**Done when** those tests pass, the throttle and the warning are in. The supervisor then
removes the Backlog entry "No throttling of failed dashboard logins".

**Addendum after the second review (Nov 5, 2026): throttle bearer keys too.**
The `/v2` API checks bearer keys in `IngestHandler._authenticate`
(`server/ingest.py`) with the same `KeyStore.verify`, and the env key is
valid there too. Without a throttle, `/v2/whoami` answers 401 or 200 for any
guess while `/login` returns 429, so the login throttle alone does not
protect a weak env key. Changes:

1. One `LoginThrottle` per server, shared by both paths: `_authenticate`
   uses `self.server.web.throttle` (the same instance `/login` uses), keyed
   by `self.client_address[0]` through `_bucket`.
2. In `_authenticate`, when an `Authorization` header is present, check
   `retry_after` before `verify`. If the bucket is throttled, set
   `close_connection = True` and raise `_RequestError(429, {"error": "too
   many failed authentications; try again later"}, {"Retry-After": "<n>"})`,
   even for a correct key, so throttled requests reveal nothing.
3. Only a well-formed bearer key that `verify` rejects counts as a failure.
   A missing or malformed header is a 401 as today, and does not count.
4. A successful bearer request does not clear the bucket. A sync machine on
   the same address (NAT) must not reset an attacker's count every pass.
   Entries still expire 15 minutes after the last failure. Dashboard login
   success still clears, as before.
5. Sync clients already treat 429 as transient (`_TRANSIENT_STATUS`), so no
   client change.

Tests (add to `tests/test_server_login_throttle.py`):
- Five wrong bearer keys on `/v2/whoami` from one address throttle that
  address. Then `/v2/whoami` with the correct key gets 429 with
  `Retry-After` and `Connection: close`, and so does `/login`.
- Failures on `/login` throttle `/v2/whoami` from the same address.
- A missing or malformed `Authorization` header never counts.
- A correct bearer key does not clear a partly filled bucket.
- A correct key from a different address still works at once.
- The existing sync tests (`tests/test_sync.py`, `tests/test_client.py`,
  `tests/test_server_ingest.py`) pass unchanged.

**Addendum 2 (Nov 5, 2026): make admission atomic.** Codex showed that the
check (`retry_after`), the key check and the failure record are separate
steps. With four failures recorded, 16 bearer requests admitted together all
got a guess, and bearer requests have no body-admission cap to bound this.
Fix in `LoginThrottle`, used by both `/login` and `_authenticate`:

1. Replace the check-then-record pair with `begin(address)`, which returns
   `(retry_after, ticket)`. Under the lock: let `count = failures +
   pending`. If `count >= FREE_FAILURES` and `now < allowed_at`, return the
   whole seconds left and no ticket. Otherwise `pending += 1`, and
   pessimistically set `allowed_at = now + delay(count + 1)`, as if this
   attempt will fail. The next concurrent attempt then sees the slot taken.
2. `finish(ticket, outcome)`, with outcome one of `"failed"`, `"ignored"`
   or `"success"`: `pending -= 1`. For `failed`: `failures += 1`,
   `last_failure = now`. For `ignored` (a valid bearer key, a 400 or 403
   login, a malformed header): recompute `allowed_at` from
   `last_failure + delay(failures + pending)`, which releases the
   pessimistic hold. For `success` on `/login`: set `failures = 0` and keep
   other attempts' `pending`. Drop the entry once `failures` and `pending`
   are both 0. A bearer success uses `ignored`, so it still never resets the
   count.
3. Every admitted attempt calls `finish` exactly once, on every path,
   including exceptions (`try/finally`). A leaked `pending` would lock the
   address out until expiry. Eviction and expiry must never drop an entry
   with `pending > 0`; skip such entries when evicting at the cap.
4. Keep `retry_after`/`record_failure`/`clear` only if other code or tests
   still need them. Otherwise remove them and update the tests.

Tests:
- Deterministic concurrency for both paths: with 4 failures recorded, hold
  `verify` on an event (monkeypatch `server.keys.verify`), start 16
  concurrent requests (bearer `/v2/whoami` with wrong keys, and `/login` with
  wrong keys), release, and check that exactly one was admitted to verify
  and the rest got 429. Use thread joins with timeouts.
- An admitted attempt that raises still releases its slot.
- A bearer success between failures does not reset the count.
- A PUT to `/v2/...` with `Content-Length` and a body, from a throttled
  address, gets 429 with `Connection: close`, and the body is not read (the
  server doesn't wait for it: send headers only and the response arrives).

**Addendum 2, revised after Codex's re-review (Nov 5, 2026): one hold per
ticket.** One shared `allowed_at`, recomputed from the shared
`last_failure` when an `ignored` attempt finishes, can erase the hold of a
different attempt that is still pending, which lets a second guess into the
same slot. Change the state to:

- per bucket: `failures`, `last_failure`, and `holds`, a dict mapping each
  pending ticket to its own deadline;
- `base = last_failure + delay(failures)`, or 0 with no failures;
- effective deadline `= max(base, *holds.values())`.

`begin`: `count = failures + len(holds)`. Refuse when `count >=
FREE_FAILURES` and `now <` the effective deadline. Otherwise create a unique
ticket (for example `(bucket, next(counter))`) with hold `now +
delay(count + 1)`. `finish(ticket, outcome)` removes only that ticket's
hold. For `failed`: `failures += 1` and `last_failure = now`. For
`success` (login only): `failures = 0`. For `ignored`: nothing else. Other
holds are never touched. Drop the bucket when `failures == 0` and there are
no holds. Expiry and eviction skip buckets that have holds. `finish` on an
unknown ticket is a no-op. Compare tickets with `is None`, never by
truthiness.

Also: remove `retry_after`, `record_failure` and `clear` (production no
longer uses them), and port their unit tests to `begin`/`finish`. Add a
comment that while every bucket holds a ticket, the table can exceed
`MAX_ENTRIES`, bounded by in-flight connections. Note in the docstring that
about 6 truly simultaneous valid-key requests from one bucket can get a
short spurious 429 (clients retry 429). That is accepted.

Extra tests: a mixed completion (a malformed login finishes while a wrong-key
guess admitted later is still pending) must not admit a second guess, for
both `/login` and bearer; and direct `begin`/`finish` unit tests for
`ignored` with another attempt pending, `success` with another pending,
holds that never expire or get evicted, the drop at zero, and an unknown
ticket.

## C. Tests workflow on pushes to `dev`

In `.github/workflows/test.yml`, change `on.push.branches` from `[main]` to
`[main, dev]`. Nothing else changes. **Done when** the change is merged and
the next push to `dev` starts the `Tests` workflow (checked after merge).
The supervisor removes the Backlog entry in the same PR.

## D †. FIFO at a run file hangs the reader

**Problem.** `layout.read_file_nofollow` opens with `O_RDONLY | O_NOFOLLOW`
and no `O_NONBLOCK`. Opening a FIFO for reading blocks until a writer
appears, so a FIFO named `meta.json` or `remote.json` hangs `list_runs` and
the dashboard listing. Every other caller of `read_file_nofollow` (reader,
sync, gc, keys, ingest, lineage) has the same exposure.

**Approach.** Add `os.O_NONBLOCK` (when present) to the open flags in
`read_file_nofollow`. A FIFO then opens at once, `fstat` shows it is not a
regular file, and the existing `S_ISREG` check raises `OSError(EINVAL)`. For
regular files, `O_NONBLOCK` has no effect on reads on Linux. Also clear the
flag after the `S_ISREG` check (`os.set_blocking(fd, True)`) so the read loop
behaves the same on every platform. Leave the rest of the function as it
is. Check that `list_runs` (`layout.py`, around line 434) and
`reader.py:280` already treat that `OSError` like any other unreadable or
invalid file; if one does not, make it do so the same way it does for an
invalid file.

**Tests** (`tests/test_store.py`, skip without `os.mkfifo`):

- `read_file_nofollow` on a FIFO raises `OSError` at once (run it under a
  short timeout, such as a worker thread joined with a 5 s limit, so a
  regression fails instead of hanging the suite).
- A run whose `meta.json` is a FIFO, and another run whose `remote.json` is a
  FIFO: `list_runs` returns promptly, skips the first or treats it as for
  any invalid `meta.json`, and handles the second as an invalid
  `remote.json` (the same fallback as package 2's tests for a malformed
  file).

**Done when** those tests pass. The supervisor then removes the Backlog
entry "FIFO at a run file hangs the reader".

## E. Flaky lineage focus-trap UI test

**Problem.**
`testbed/tests/test_task_groups.py::test_lineage_view_traps_focus_and_flags_failures`
fails about 1 run in 40. After the click on the failed node, the async
`/lineage` fetch re-renders the inspector. If that lands inside the Tab
loop, `document.activeElement` is briefly `<body>` and the per-press
`contains` check fails.

**Approach.** Fix the test, not the product, unless the investigation shows
that focus really escapes the dialog for a user (for example, focus is left
on `<body>` after the re-render and the next Tab leaves the dialog). In that
case, report it before changing app code. In the test:

1. Before the Tab loop, wait for the inspector to settle: wait for the
   `/lineage` response the click triggers (`page.expect_response` around
   the click), and then for the re-rendered content (the callout is already
   asserted; add whatever marks the final render, such as the inspector's
   code-diff or details section).
2. Make the per-press check tolerant of a re-render in flight: after each
   Tab, use `page.wait_for_function` with a short timeout (1 s) for focus to
   be inside `.lv-dialog`, instead of a single `evaluate`. The test still
   fails if focus ever settles outside the dialog.

**Revised after review (Nov 5, 2026).** A forced-race stress test showed
the cause is in the product, not only the test. When the per-run `/lineage`
fetch resolves, `render()` replaces the inspector, and its "Open run" and
"Compare with parent" links have no `data-focus` key. A focused link is
therefore lost and focus falls to `<body>` until the next Tab. The test-only
waits did not change the failure rate. New approach: give those links
`data-focus` keys so `render()` restores focus, keep the per-press check
strict, and add a regression test that gates the fetch, focuses "Open run",
releases the fetch, and checks focus is still on that link without pressing
Tab. The regression test must fail without the `app.js` change.

**Done when** 100 runs in a row pass:
`.venv/bin/python -m pytest -q -p no:randomly "tests/test_task_groups.py::test_lineage_view_traps_focus_and_flags_failures" --count 100`
if `pytest-repeat` is installed, otherwise a shell loop of 100 runs. The
supervisor then removes the Backlog entry "Flaky lineage focus-trap UI test".

## Checks for every task

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/` for task E. Run it
  for B too if `login.js` changes.
- The coder reports any deviation from this plan instead of changing the
  approach.

# Package 4b †: Key management in the dashboard

Nov 5, 2026 · Supervisor plan · Release 2.0 · Coder: Codex · Size: M ·
† security-sensitive (Sonnet reviews read-only; Cursor gives the second
read-only review, since Codex writes the change)

Source: [on-prem-server-plan.md](../on-prem-server-plan.md), sections 5, 5.1
and 7 (package 4b). Roadmap: [on-prem-roadmap.md](../on-prem-roadmap.md).
Builds on [1-server-ingest.md](1-server-ingest.md) (`KeyStore`) and
[4-server-dashboard.md](4-server-dashboard.md) (`WebAuth`, sessions, CSRF).
Base branch: `dev`.

## Goal

An admin can run the whole key lifecycle from the server dashboard:

1. **First key.** A fresh server with no admin key and no
   `AGENTICML_SERVER_KEY` logs a one-time setup URL at startup. Opening it
   creates the first admin key, shows it once, and signs the browser in.
2. **Keys page.** Issue `sync` or `admin` keys, list every key, and revoke
   keys. A revoked key's dashboard sessions and syncs end on their next
   request. The last admin key cannot be revoked from the page.

Local `agenticml ui` has no Keys page, no setup page and no `/api/server/*`
routes, and otherwise behaves exactly as today.

Out of scope: per-user keys and run ownership (package 12), the audit log
(14), the users admin page (15), `sync --follow` (5), docs and deployment
pages (6). The keys CLI keeps its behaviour, including revoking the last
admin key, which is the recovery path.

## Work split

Two tasks, both for Codex, run in order. Each one is reviewed before the next
starts. The API contract in section 3 is fixed, so task B builds against it.

| Task | Scope | Plan sections |
| --- | --- | --- |
| A, backend | `keys.py`, `webauth.py`, new `keyspage.py`, `app.py`, setup page assets, `tests/test_server_keys_web.py` | 1, 2, 3, 4, 6.1 |
| B, dashboard | `index.html`, `app.js`, `app.css`, `testbed/tests/test_server_keys_dashboard.py` | 5, 6.2 |

## Facts from the current code (checked at `329c7a9`)

- **`KeyStore`** (`src/agenticml/server/keys.py`).
  - Methods: `create(name, scope, created_by)` returns `(record, secret)`.
    `list()` returns the file records, then the env record. `revoke(name)`
    returns a bool and has no admin guard. Also `verify`, `touch`,
    `current(name, sha256)` and `has_admin()`.
  - Reads: `_read` raises `ValueError` on a corrupt file. `list()`, through
    `_maybe_reload`, logs and fails closed to `[]`.
  - `touch` swallows every error, because last-use writes are advisory. Each
    authenticated `/v2` request calls it (`ingest.py`, after
    `_authenticate`), and an error escaping it is a 500 for that sync.
- **`WebAuth`** (`src/agenticml/server/webauth.py`).
  - `handle(handler)` is the gate. It answers the `_ASSETS` static files,
    `/login`, `/logout` and `/api/server/session` before the session check.
    For a signed-in admin it sets `handler.session` and returns `False`.
  - Sessions are keyed by the SHA-256 of the cookie token and store the key's
    `name` and `sha256`.
  - `_session` re-checks `keys.current(name, sha256)` on every request. So a
    revoke already ends the session, and a recreated name with a new hash does
    not inherit it. It also requires `scope == "admin"`.
- **Routing** (`IngestHandler._dispatch` in `src/agenticml/server/ingest.py`).
  - Every path other than `/healthz` and `/v2*` goes to `DashboardHandler`:
    GET to `do_GET`, PUT to `do_PUT`, and everything else (POST, DELETE) to
    `_method_not_allowed`.
  - `do_GET` and `_method_not_allowed` both call `self.server.auth.handle(self)`
    first. So POST and DELETE under `/api/server/` reach `WebAuth.handle`,
    which must dispatch them; today a signed-in request falls through to 405.
  - Bodyless ingest routes set `close_connection` when `Content-Length` or
    `Transfer-Encoding` is present. `IngestHandler._json` then adds
    `Connection: close`.
- **`DashboardHandler` helpers** (`src/agenticml/ui/server.py`).
  - Checks: `_host_allowed`, and `_csrf_ok`, which needs `X-AgenticML-Request: 1`
    plus an Origin matching `public_url` or the Host. On the server a missing
    Origin fails.
  - Body: `_put_length(limit)` gives 411 or 413. `_read_put_body(length)`.
  - Responses: `_json` (adds `Cache-Control: no-store`), `_send`, `_error`.
  - `DashboardServer._admit_body(connection, limit)` and
    `_set_connection_state` cap concurrent bodies. `WebAuth._login` is the
    pattern to copy for a JSON POST.
- **`IngestServer.__init__`** (`src/agenticml/server/app.py`).
  - It builds `KeyStore` and `WebAuth`, then calls `super().__init__`, so the
    bound port is known only after that call.
  - `serve()` warns "no keys" or "no admin key", then prints the banner.
  - `_browser_host(address)` formats the bound address for a URL.
- **Dashboard SPA** (`ui/static/index.html` and `app.js`).
  - At load, `app.js` calls `/api/server/session`. A 404 means local; a 200
    sets `serverMode` and shows `#session` with Sign out; a 401 goes to
    `/login`.
  - `route()` dispatches on `location.hash`.
  - The topbar holds `.breadcrumb`, `#host`, `#session` and `#theme`.
- **Other facts.**
  - `read_remote(run_path)` in `src/agenticml/store/reader.py` returns the
    normalised `remote.json` with `owner`, or `None`. It never raises on a bad
    file.
  - `server/static/*` ships in the wheel through a pyproject glob.
  - The dashboard CSP is `script-src 'self'`, so no inline script.
- **Test environment.** The shared venv
  `/home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv` imports the
  main checkout's `src`, not this worktree's. Every test command must set
  `PYTHONPATH=<worktree>/src`, which takes precedence (checked).

## Files to touch

| Path | Task | Change |
| --- | --- | --- |
| `src/agenticml/server/keys.py` | A | Guarded `revoke`, new exceptions, `validate_name`, reserved `.` and `..` |
| `src/agenticml/server/webauth.py` | A | Setup token, `/setup`, keys API dispatch, `drop_sessions`, `setup.js` asset, 503 mapping |
| `src/agenticml/server/keyspage.py` | A | New. List, create and revoke handlers, run counts, command host |
| `src/agenticml/server/app.py` | A | Arm setup after binding. Adjust the startup warning |
| `src/agenticml/server/static/setup.html`, `setup.js` | A | New. Setup page, styled like `login.html` |
| `src/agenticml/cli.py` | A | Only if needed: catch `KeyStoreUnavailable` with the other errors in `cmd_server_keys`. No other change |
| `tests/test_server_keys_web.py` | A | New |
| `src/agenticml/ui/static/index.html` | B | Hidden `#nav-keys` link in the topbar |
| `src/agenticml/ui/static/app.js` | B | Nav link in server mode, `#/keys` route, create dialog, revoke confirm |
| `src/agenticml/ui/static/app.css` | B | Keys page and dialog styles. No page overflow at 375px |
| `testbed/tests/test_server_keys_dashboard.py` | B | New Playwright tests (marker `ui`) |
| `docs/plans/on-prem-roadmap.md` | supervisor | 4b status and plan link; package 4 `done (#28)` |

No new dependencies. The server side stays standard library only. No docs pages
(package 6 writes them).

## Approach

### 1. Key store (`keys.py`, task A)

- `validate_name(name)` is a module function used by `create`, `/setup` and
  the API. A valid name is a string matching `_NAME`, and is not `env`, `.` or
  `..`. Otherwise it raises `ValueError("invalid or reserved key name")`.
- `class KeyNameTaken(ValueError)`: `create` raises it for a duplicate name.
- `class LastAdminError(ValueError)`: raised by the guarded `revoke`.
- `class KeyStoreUnavailable(RuntimeError)`. `create` and `revoke` raise it
  when re-reading `keys.json` under the lock fails, whether from `OSError` or
  a `ValueError` for bad data. The message is "could not load keys.json",
  with no file contents.
  `touch` must keep catching **every** exception, including this one, then
  log and return. This one is not negotiable: a last-use write must never
  fail an authenticated request.
- `revoke(name, *, keep_admin=False)`:
  - `name == "env"` raises `ValueError`.
  - Under `self._lock` and `self._file_lock()`, re-read the file.
  - A missing name returns `None`.
  - With `keep_admin=True`, count the admin-scope records in the file plus
    the env record. If the target is an admin and the count is 1 or less,
    raise `LastAdminError`. An env admin counts, so a file admin can still be
    revoked while the env admin exists.
  - Otherwise write the remaining records and return the removed `KeyRecord`.
    A record is truthy, so the CLI's `if not keys.revoke(...)` keeps working.
    The CLI calls it without the guard.

### 2. Setup token (`webauth.py`, `app.py`, task A)

- **Arming.** `WebAuth.arm_setup(base_url)`:
  - It does nothing when `keys.has_admin()` is true. The env key counts, so
    setting `AGENTICML_SERVER_KEY` also prevents arming.
  - Otherwise set `self.setup_token = secrets.token_urlsafe(32)`. It is kept
    in memory only and never returned by any endpoint.
  - Set `self._setup_used = False` and log one WARNING, exactly:
    `No admin key yet. Open <base>/setup?token=<token> to create one, or run: agenticml server keys create --name admin --scope admin`
  - `<base>` is `public_url` when it is set. Otherwise it is
    `http://<_browser_host(server_address[0])>:<port>`.
  - `IngestServer.__init__` calls it after `super().__init__`. Tests that use
    `make_server` read `httpd.web.setup_token`.
- **Armed.** `setup_token is not None and not _setup_used and not keys.has_admin()`.
  It is evaluated on every request, so an admin created by any route,
  including the CLI while the server runs, disarms setup at once.
- **Disarming is permanent** (added after the second review). The first time
  any `/setup` request sees `keys.has_admin()` true, it sets
  `_setup_used = True` and `setup_token = None` under `_setup_lock`. After
  that, `/setup` is 404 until a restart. Otherwise, revoking the last admin
  later with the CLI would bring the logged token back.
- **Setup-closed marker** (added after the re-review). The latch above only
  fires when a `/setup` request arrives. So an admin created and then revoked
  with the CLI before any such request would leave the token valid. To close
  that window, `KeyStore.create` writes an empty file
  `<data>/server/setup-closed` whenever it creates an `admin` key, from any
  process. The write is atomic, uses no-follow, has mode 0600, and happens
  under `keys.lock` before the new key is written.
  - `/setup` treats an existing marker like `has_admin()` true: 404, and it
    latches.
  - `arm_setup` deletes a stale marker at startup before deciding whether to
    arm. A restart with no admin therefore still gets a fresh token, as
    release plan 5.1 says.
  - The keys CLI never deletes it.
- **No admin on disk, checked strictly.** `has_admin()` fails closed to "no
  admin" when `keys.json` cannot be read, so two extra rules apply:
  - `arm_setup` does not arm when `keys.json` exists but the strict
    `_read()` fails. It logs an error saying setup is disabled until the
    file is fixed and the server restarts.
  - The first-admin create runs as
    `keys.create(name, "admin", created_by="setup", only_if_no_admin=True)`.
    Under `keys.lock`, after the strict read, it raises `SetupClosed`
    (a subclass of `ValueError`) when any admin record exists or an env key
    is set. `_setup` answers that with the same 404 and disarms
    permanently. A transient read error can therefore never mint a second
    admin next to an existing one.
- **`GET /setup?token=T`** is handled before the session gate.
  - Answer 404 `{"error": "not found"}` unless all of these hold: setup is
    armed, the query has exactly one `token` value, and
    `hmac.compare_digest(T, setup_token)` is true (compare the encoded bytes,
    and treat non-ASCII as a mismatch).
  - Otherwise serve `setup.html` with `Cache-Control: no-store` and
    `Referrer-Policy: no-referrer`.
- **`setup.js`** is a module with no inline script.
  - At load it reads the token from `location.search`, keeps it in a
    variable, and calls `history.replaceState(null, "", "/setup")`, so the
    token leaves the address bar and history.
  - A reload then gets the 404. The operator reopens the logged link, which
    works until it is used or the server restarts.
- **`POST /setup`** takes the body `{"token": "...", "name": "admin"}`. `name`
  is optional and defaults to `admin`. The checks run in this order:
  1. `_csrf_ok()`, else 403.
  2. `Content-Type: application/json`, else 415. Length through
     `_put_length(LOGIN_BODY_MAX)`, with admission exactly as in `_login`.
  3. Parse and type-check the body (a JSON object with a string `token`, and
     `name` a string or absent), else 400.
  4. `validate_name(name)`, else 400. The token is not consumed.
  5. Under `self._setup_lock`:
     - Re-check armed and compare the token. Every failure here gives the same
       404 as GET.
     - If `name` already exists in `keys.list()`, return 409
       `{"error": "key name already exists"}` without consuming the token.
     - Otherwise call
       `keys.create(name, "admin", created_by="setup", only_if_no_admin=True)`
       while still holding `_setup_lock`. The lock serialises setup requests,
       so the token is marked used only after the outcome is known:
       - Success sets `_setup_used = True`.
       - `SetupClosed` gives the 404 and latches.
       - `KeyNameTaken` (a race with the CLI) gives 409.
       - `KeyStoreUnavailable` or `OSError` gives 503.
       The last two leave the token usable (changed after review: a
       transient store error no longer burns the token).
     - `only_if_no_admin` also raises `SetupClosed` when the setup-closed
       marker exists, checked under `keys.lock`.
     - `arm_setup` deletes the marker, checks `has_admin`, and does the
       strict read while holding `keys.lock`. A CLI create cannot then
       interleave with arming.
  6. Create the session with `_create_session`. Respond 201
     `{"ok": true, "key": name, "value": "aml_…", "host": …, "host_source": …}`
     with `Set-Cookie: self._cookie(token)` and `Cache-Control: no-store`.
     The value is never logged.
- No throttling. The token carries 256 bits, and every failure looks the same.

### 3. Keys API (`keyspage.py`, routed from `WebAuth.handle`, task A)

After the session check passes, `WebAuth.handle` calls
`keyspage.handle(webauth, handler, path)` for `/api/server/keys` and
`/api/server/keys/<name>`, and returns `True`. The session is therefore always
an admin one. Any other path under `/api/server/` that is not
`/api/server/session` gives 404 JSON. A method not listed below gives 405 JSON.

For GET and DELETE: if the request has a `Content-Length` or
`Transfer-Encoding` header, set `handler.close_connection = True` before
answering. The response then carries `Connection: close`, and unread bytes
cannot be parsed as the next request.

The same rule applies to every server-mode request that does not read a body
(added after the second review). At the top of `WebAuth.handle`, a GET,
HEAD or DELETE carrying either header sets `close_connection`. That covers
`/setup`, `/login`, the static assets and the dashboard pages. Every
response with `close_connection` set must carry exactly one
`Connection: close` header, including responses sent with `_send` as well as
with `_json`.

| Request | Behaviour |
| --- | --- |
| `GET /api/server/keys` | 200 `{"keys": [...], "host": "...", "host_source": "public_url" or "request"}`. Each item is `KeyRecord.to_public()` plus `runs` and `revocable`. `runs` counts the runs whose `remote.json` `owner` equals the name, using `read_remote` over the directories in `server.root / "runs"`, skipping symlinks, and scanning at most 10000; past the cap it is `null`. `revocable` is false for `env`. Never the hash, never a secret. Order: file records by `created_at` then name, then `env`. |
| `POST /api/server/keys` | Body `{name, scope}`. CSRF failure 403, non-JSON 415, length 411 or 413 through `_put_length(LOGIN_BODY_MAX)`, admission as in `_login`, and a body that is not a JSON object with string `name` and `scope` 400. A scope other than `sync`/`admin` is 400. `validate_name` failure 400. `create(name, scope, created_by=session["name"])`: `KeyNameTaken` 409, `KeyStoreUnavailable` or `OSError` 503. Success 201 `{"key": value, "record": public, "host": …, "host_source": …}` with `no-store`. The INFO log line names the key and scope only, never the value. |
| `DELETE /api/server/keys/<name>` | CSRF failure 403. The name must pass `_NAME` and must not be `.` or `..`, else 400; check it on the URL-decoded segment, and a `/` inside it is 400. `env` 409 `{"error": "environment key: remove AGENTICML_SERVER_KEY and restart"}`. Unknown 404. `LastAdminError` 409 `{"error": "last admin key; create another admin key first, or use the CLI"}`. `KeyStoreUnavailable` or `OSError` 503. Success 200 `{"revoked": name, "signed_out": bool}`. |

- On a successful revoke, call `webauth.drop_sessions(name, record.sha256)`,
  which deletes every session whose name **and** sha256 match. If the
  caller's own session matched, `signed_out` is true and the response carries
  `Set-Cookie: webauth._cookie("", max_age=0)`.
- The host for the printed command:
  - If `public_url` is set, use it, with `host_source` `public_url`.
  - Otherwise use `http://` plus the request's `Host` header, with
    `host_source` `request`. The header has already passed `_host_allowed`.
- In `WebAuth.handle`, map `KeyStoreUnavailable` to 503
  `{"error": "key store unavailable"}`. Put it before the existing
  `ValueError` → 400 branch. It is a `RuntimeError`, so the order is only for
  clarity.

### 4. Startup output (`app.py`, task A)

- `serve()` keeps the "no keys" warning when there are no keys at all.
- At startup, setup is always armed when there is no admin key, so the setup
  line replaces the "no admin key" warning. Remove that branch, or guard it
  with "setup not armed" if a test needs it.

### 5. Dashboard (task B)

- **Topbar.** In `index.html`, add
  `<a id="nav-keys" class="btn btn-ghost btn-sm" href="#/keys" hidden>Keys</a>`
  before `#session`. `app.js` unhides it in the `serverMode = true` branch.
  Local mode leaves it hidden.
- **Route.** `route()` sends `hash === "/keys"` to `keysPage(version)`, which
  respects `epoch` like the other pages.
  - A 404 from `/api/server/keys` (local UI) renders the runs home.
  - A 401 goes to `/login`.
- **Keys page.**
  - Heading "Keys" and a "New key" button.
  - Table columns: name, scope, prefix shown as `aml_xxxx…`, created, created
    by, last used ("never" when null), runs ("–" when null), and Revoke.
    There is no Revoke button when `revocable` is false.
  - Below 600px the table stacks into labelled rows. Any wrapper scrolls
    inside itself, never the page.
- **New key dialog** (`<dialog>`, focus trapped with the existing `trapTab`).
  - Fields: a name input (with a client-side pattern check matching `_NAME`),
    a scope select (default `sync`) and Create.
  - On 201 the dialog switches to a result view:
    - the key in a `<code>` element, with a copy button using
      `navigator.clipboard`, falling back to selecting the text;
    - the command `agenticml login --host <host> --key <value>`, with its own
      copy button;
    - "This key is shown once and cannot be recovered."
  - When `host_source` is `request`, also show "No --public-url is set, so
    this uses the address you opened the dashboard at."
  - On 400 or 409, show the server's `error` message inline.
  - Closing the dialog removes the key from the DOM, drops the JS reference
    and refreshes the list.
- **Revoke.** A confirm `<dialog>` reads "Revoke <name>? Machines using it
  stop syncing on their next request, and its dashboard sessions end."
  - On 200 with `signed_out`, go to `/login`. Otherwise refresh the list.
  - On 409, show the message inline.
- **Every request** sends `credentials: "same-origin"`. Writes also send
  `X-AgenticML-Request: 1` and `Content-Type: application/json`.
  - Server strings go in with `textContent` only; no `innerHTML`.
  - The key value never goes into the URL, `localStorage` or `console`.
- **Styles.** Reuse the existing dialog, button, table and field styles in
  `app.css` where possible. Add only what the Keys page needs.

### 6. Tests

#### 6.1 `tests/test_server_keys_web.py` (task A)

Copy the small helpers (`running`, `call`, `csrf`, cookie parsing) in the
style of `tests/test_server_web.py`; import them if they are importable
without side effects.

**Setup.**
- The URL is logged exactly once at startup (caplog), and the token in it
  matches `httpd.web.setup_token`.
- 404 for each of: a wrong token, a missing token, two `token` values, and an
  unarmed server.
- The GET page has `no-store` and `Referrer-Policy: no-referrer`.
- A valid POST creates an admin key, sets the cookie, returns the value once,
  and the cookie then opens `/api/server/session`.
- Reusing the token gives 404.
- A POST without CSRF gives 403, a bad name 400 and a taken name 409. None of
  them consumes the token, which still succeeds afterwards.
- Two concurrent valid POSTs give exactly `[201, 404]`, and one key is created.
- An admin created with the CLI while the server runs disarms `/setup` (404).
- No setup is armed when `AGENTICML_SERVER_KEY` is set or an admin exists.
- A new `make_server` on the same data dir arms a new token, and the old one
  gives 404.
- Neither the setup token nor the key value appears in any log record.

**Keys API.**
- The list has every public field plus `runs` and `revocable`, and no hash.
- The env record is present with `revocable` false.
- `runs` counts owned runs (two runs for one key, one for another).
- Creating `sync` and `admin` keys returns 201 with the value and `no-store`.
  The value is absent from the list and from the logs.
- Duplicate name 409. Bad name, `.`, `..`, `env` and bad scope 400.
  Non-JSON 415. Oversize 413.

**Revoke.**
- A revoked sync key gets 401 on `/v2/whoami`.
- A second admin's session gets 401 on its next request after the revoke.
- Revoking your own key gives `signed_out: true` and a clearing cookie.
- A recreated name does not revive an old session.
- The last admin gives 409 and `keys.json` is unchanged.
- An env admin plus one file admin: the file admin can be revoked.
- Two sessions revoking the only two admins concurrently give `[200, 409]`.
- CLI `revoke` still removes the last admin.

**Gate.**
- POST and DELETE without CSRF give 403.
- No session gives 401 JSON.
- A sync key cannot sign in, so it cannot reach the API.
- An unknown name gives 404, and `/api/server/keys/%2e%2e` 400.
- A GET with `Content-Length` and a DELETE with `Transfer-Encoding` both get
  `Connection: close`.

**Store failures.**
- A corrupt `keys.json` makes create and revoke return 503, with no file
  contents in the response.
- With `keys.json` unreadable after the cache reload, `touch` returns
  normally and an authenticated `/v2/whoami` still answers 200.

**Sync-scope key from the page.** It works on `/v2/whoami` and `/login` refuses it
with 403.

**Local UI.** A `DashboardServer` without auth gives 404 for
`GET /api/server/keys`, `/setup` and `/static/setup.js`, and 405 for DELETE.
Do not assert on `nav-keys` here; the browser suite checks the hidden link.

**End to end.** Create a `sync` key through the API, then run
`agenticml login --host … --key …` and `agenticml sync` against the
in-process server, reusing the pattern in `tests/test_sync.py`. The synced run
reads back with `RunReader` matching the source.

#### 6.2 `testbed/tests/test_server_keys_dashboard.py` (task B, marker `ui`)

Reuse the `browser`, `context` and `page` fixtures and `assert_no_overflow`
from `test_server_dashboard.py` (import them, or move shared ones into
`conftest.py` without changing their behaviour).

- **Setup.** Open the setup URL, create the admin key, and check the key is
  shown once and the URL bar no longer has the token. "Open dashboard" shows
  the Keys link.
- **Keys page.** Create a sync key; the dialog shows the command and the
  `host_source` note. Close it; the row appears and the key is no longer in
  the DOM. Revoke it; the row disappears. Revoking your own admin key with
  another admin present lands on `/login`.
- **Last admin.** Revoke shows the 409 message inline.
- **Overflow.** The Keys page (with the dialog open and closed) and the setup
  page have zero horizontal overflow at 375px.
- **Local UI.** `agenticml ui` shows no visible Keys link.
- No page errors and no console errors (the fixture asserts this).

## Edge cases

- With a corrupt `keys.json`, `list()` is empty (fail closed) and sign-in
  fails. Create and revoke give 503, never 404 or 400.
- Two admins revoked at once: the guard runs under `keys.lock`, so one of the
  two gets 409.
- The `Host` check runs before every route, `/setup` included.
- An HTTPS `public_url` gets the `__Host-` cookie name and `Secure` through the
  existing `_cookie`.
- The setup token never appears in an `/api` response, a 404 body, or a log
  other than the single startup line.
- Revoking a `sync` key ends no sessions, because sync keys cannot sign in.

## Checks

Run these from the worktree root, with
`PY=/home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv/bin/python`.

- Targeted: `PYTHONPATH=$PWD/src $PY -m pytest -q -p no:cacheprovider tests/test_server_web.py tests/test_server_keys.py tests/test_server_keys_web.py tests/test_sync.py`
  (baseline before 4b: 54 passed for the first two files).
- Unit: `PYTHONPATH=$PWD/src $PY -m pytest -q -p no:cacheprovider tests`.
- UI (task B, and after task A to show nothing broke): from `testbed/`,
  `PYTHONPATH=<worktree>/src $PY -m pytest -q -p no:cacheprovider -m ui`.
- `git diff --check`.

## Done when

- All tests above pass, including the end-to-end login and sync check.
- The UI suite passes, with no overflow at 375px on the Keys and setup pages.
- Sonnet's and Cursor's read-only reviews are clean. Each finding is fixed, or
  rejected with a reason recorded in the PR.
- The roadmap shows 4b `plan ready` in this change, then `done (<PR>)` once
  merged.


## Review record

This package was built twice. The first attempt was discarded when the
supervisor changed, and this plan was rewritten with the first attempt's
review findings built in. The record below is for the second attempt.

### Who wrote what

- **Codex:**
  - task A, the backend (sections 1–4, tests 6.1), and its security fix
    rounds;
  - task B, the dashboard (section 5, tests 6.2), and every UI fix round;
  - the final batches (the FIFO lock files, timestamp overflow, shell
    quoting, the bfcache wipe, and the low-severity items).
- **Cursor:** the setup-closed marker, `prepare_setup`, the outcome-based
  token use in `_setup`, and their tests. Cursor took this over when Codex
  hit its usage limit.
- **Sonnet** reviewed every round, read-only.
- **Second reviews** were crossed, as the † rule requires: Cursor reviewed
  everything Codex wrote, and Codex reviewed everything Cursor wrote.

### Findings fixed, by theme

- **Setup token.**
  - The token revived after an admin existed and was then revoked with the
    CLI. Fixed with a permanent latch, plus the setup-closed marker for a
    create and revoke that happen before any `/setup` request.
  - A transient read could mint a second admin. Fixed with a strict
    first-admin create under `keys.lock`.
  - A CLI create could interleave with startup arming. Fixed with
    `prepare_setup` under `keys.lock`.
  - A transient store error burned the token. The token is now used only on
    success.
- **Transport.** A GET, HEAD or DELETE with a body could leave unread bytes
  on a kept-alive connection, a request-smuggling gadget. It now closes the
  connection, and the response carries exactly one `Connection: close`.
- **Startup robustness.**
  - A broken `keys.lock` crashed startup. Setup is now disabled instead and
    the server starts.
  - A FIFO at `keys.lock` or `server.lock` hung startup. Fixed with a
    non-blocking open plus an `S_ISREG` check.
  - A timestamp overflow in `keys.json` broke every request and then
    startup. It now fails closed.
- **Disclosure.**
  - Only fixed `ClientError` messages reach clients.
  - The key no longer survives in the back/forward cache: it is wiped
    synchronously on `pagehide`, and selections inside the dialog are
    cleared.
  - The login command is now shell-quoted. An IPv6 host broke it in zsh,
    and a `--public-url` path could inject commands.
- **Error handling.**
  - Run-count errors broke the Keys page.
  - Non-JSON and odd-shaped responses showed raw parser errors, on the
    dashboard and on the setup page.
  - The `touch` error log was unthrottled, and its throttle took the cache
    lock.
- **Dashboard.**
  - The server-mode topbar covered the run page's sticky toolbar.
  - A server-mode sticky toolbar on mobile was introduced and then removed.
    The supervisor's brief caused it.
  - Focus was lost after a revoke.
  - Copy feedback was missing, and later a shared status timer cleared it
    early.
  - Stale refreshes could overwrite newer lists.
  - The stacked table lost its ARIA semantics.
  - Listeners leaked on some exit paths.
- **Tests that could not fail** were rewritten, about a third of all
  findings:
  - the log-leak tests could not see INFO records;
  - the recreated-name session test was not isolated;
  - the topbar height test used a fixed height;
  - the late-response tests could not tell the outcomes apart;
  - the masked strict-create test was mutation-checked by the supervisor;
  - the lock-atomicity tests did not hold the cross-process lock;
  - the selection test passed whatever the selection state.

### Findings rejected, with reasons

- **The verbose access log leaks the token.**
  `IngestHandler.log_message` logs only the HTTP method, never the target.
- **`hmac.compare_digest` raises on a length mismatch, so a wrong-length
  token gets a 400.** It returns `False` for bytes of different lengths.
  The supervisor checked this, and the cited tests pass.
- **Hold the file lock in the concurrent-revoke test.** The in-process
  cache lock serialises the two revokes anyway, so no test can observe a
  missing file lock.
- **A 400 from `_file_lock` on a non-POSIX host.** The server cannot start
  without `fcntl`, so that path is unreachable.
- **Lock files are now created 0600 instead of 0666 minus the umask.**
  `server/` is already 0700, so this is a tightening, not a regression.

One rejection was later reversed: shell-quoting the login command. The
second review showed concrete breakage, so it was fixed (see Disclosure).

### Found along the way, not part of 4b (roadmap backlog)

- `testbed/tests/test_task_groups.py::test_lineage_view_traps_focus_and_flags_failures`
  is flaky. It failed about 1 run in 40 both before and after 4b.
- The run-file FIFO hang in `store/layout.py` `read_file_nofollow`. It was
  already in the backlog, and the entry now notes that 4b fixed both lock
  files.

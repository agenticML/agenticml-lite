# Package 4 †: Server dashboard: key login, sessions, CSRF, last synced

Nov 4, 2026 · Supervisor plan · Release 2.0 · Coder: Cursor · Size: M ·
† security-sensitive (second read-only review by Codex)

Source: [on-prem-server-plan.md](../on-prem-server-plan.md), sections 3, 5,
5.1, 6 and 7 (package 4). Roadmap: [on-prem-roadmap.md](../on-prem-roadmap.md).
Builds on [1-server-ingest.md](1-server-ingest.md),
[2-remote-status.md](2-remote-status.md) and
[3-sync-client-v2.md](3-sync-client-v2.md). Base branch: `dev`.

## Goal

`agenticml server serve` also serves the dashboard (the same code as
`agenticml ui`) on its one port. Every dashboard request needs a session
that a person gets by signing in at `/login` with an **admin** API key. A
sync key is refused. Sessions live in memory, expire, and end as soon as
their key is revoked. Writes (note edits) pass a CSRF check that knows
the server's `--public-url`. Run views show when the run last synced. Local
`agenticml ui` behaves exactly as today.

Out of scope: the setup token and `/setup`, the Keys page and
`/api/server/keys` (package 4b); `sync --follow` (package 5); docs pages,
image and deployment (package 6); lineage replication (package 7). No
change to `/healthz` or `/v2/*`.

## Files to touch

| Path | Change |
| --- | --- |
| `src/agenticml/server/webauth.py` | New. `WebAuth`: login page, `/login`, `/logout`, `/api/server/session`, session store, request gate |
| `src/agenticml/ui/server.py` | `DashboardServer(..., auth=None, public_url=None)`; host and CSRF checks use `public_url`; auth gate in the handler entry points; `last_sync_at` in run list and run detail; server-mode lineage message |
| `src/agenticml/server/ingest.py` | `_dispatch`: `/healthz` and `/v2/*` as today; every other path goes to the dashboard handler |
| `src/agenticml/server/app.py` | `IngestServer` builds `WebAuth` and passes `auth` and `public_url` to `DashboardServer`; startup line prints the dashboard URL |
| `src/agenticml/server/keys.py` | `KeyStore.current(name, sha256) -> KeyRecord | None` (exact name and hash still present, after `_maybe_reload`) |
| `src/agenticml/server/static/login.html`, `login.js` | New. Login page (served only by `WebAuth`, not in the dashboard's `_FILES`) |
| `src/agenticml/ui/static/app.js`, `app.css`, `index.html` | 401 → `/login`; signed-in badge and Sign out (server only); "Last synced" display; login page styles |
| `pyproject.toml` | Only if needed so `server/static/*` ships in the wheel (check how `ui/static` is included and do the same) |
| `tests/test_server_web.py` | New |
| `testbed/tests/test_server_dashboard.py` | New Playwright tests (marked `ui`) |
| `docs/plans/on-prem-roadmap.md` | Package 3 `done (#27)`, package 4 status (supervisor) |

No new dependencies. Standard library only on the server side.

## Approach

### 1. Routing (`ingest.py`)

At the top of `IngestHandler._dispatch`, look at the raw request target's
path (before the ingest raw-path policy): `/healthz`, `/v2` and anything
under `/v2/` keep today's ingest flow unchanged. Every other path is handed
to the dashboard: call `DashboardHandler.do_GET(self)` for GET,
`DashboardHandler.do_PUT(self)` for PUT, and the dashboard's
method-not-allowed path for anything else (which now also routes the two
POST endpoints, see 3). Keep `IngestHandler.log_message` (never logs the
target) for both.

### 2. `DashboardServer` and handler changes (`ui/server.py`)

- `DashboardServer.__init__(self, root, host, port, read_only=False,
  handler_class=None, auth=None, public_url=None)`. Store both. `None` for
  both means today's behaviour, exactly; `make_server` / `serve` in this
  module do not pass them.
- **Host check.** `_host_permitted(host_header, bind_host, bound_address,
  public_url=None)`. With `public_url` set: allow only the public URL's host
  name and the loopback names (`localhost`, `127.0.0.1`, `::1`), compared
  with `_canonical_host` (port ignored as today). Without it: today's rule.
- **CSRF.** `_csrf_ok` keeps the `X-AgenticML-Request: 1` and
  `Sec-Fetch-Site` rules. Origin: with `public_url` set, a present Origin
  must equal the public URL's origin (`scheme://host[:port]`, default ports
  dropped, lowercase); a request whose Host is a loopback name may instead
  use `http://<Host>`. Without `public_url`: today's `http://<Host>` rule.
  With an auth hook, the Origin header is **required** (browsers send it on
  every POST/PUT fetch), so a missing Origin fails.
- **Auth gate.** In `do_GET`, `do_PUT` and `_method_not_allowed`, right after
  the Host check: `if self.server.auth is not None and
  self.server.auth.handle(self): return`. `handle` returns True when it has
  already answered (login routes, a rejected request) and False when the
  request has a valid session and should continue to the dashboard. It sets
  `self.session` for later use.
- **Last synced.** `/api/runs` rows gain `last_sync_at` (from the
  `list_runs` meta that package 2 fills; `None` otherwise). Run detail
  (`/api/runs/<id>`) gains `last_sync_at` from `run.last_sync_at()`. Local
  stores have no `remote.json`, so both stay `None` there.
- **Code diff on the server.** In `_lineage_changes`, when the dashboard runs
  with an auth hook (server mode) and the shadow repo is missing, the error
  text is exactly `code diff not available on this server` (answer stays
  200, `files` empty). Local behaviour unchanged.
- Notes: no change. On the server, `PUT /api/runs/<id>/notes` writes the
  server store's `notes.md` once the session and CSRF checks pass.

### 3. `webauth.py`

`WebAuth(keys: KeyStore, public_url: str | None, clock=time.time)`:

- **Routes it owns** (answered before the session gate):
  - `GET /login`: the login page (`server/static/login.html`), `Cache-Control:
    no-store`, the dashboard's CSP. With a valid session: `303` to `/`.
  - `GET /static/login.js`, `/static/app.css`, `/static/InstrumentSans.woff2`,
    `/static/JetBrainsMono.woff2`: served without a session (login page
    assets). `login.js` is read from `server/static/`; the other three are
    the dashboard's existing files.
  - `POST /login`: JSON `{"key": "<key>"}`, at most 4 KiB, read with the
    dashboard's body admission (`_admit_body`) and a short deadline;
    `Content-Length` rules as for notes (411/413). Requires the CSRF check
    (login CSRF). Then `keys.verify(key)`:
    - unknown or revoked → 401 `{"error": "invalid key"}`;
    - `sync` scope → 403 `{"error": "this is a sync key; sign in with an admin key"}`;
    - `admin` → create a session, `keys.touch(record)`, 200
      `{"ok": true, "key": name}` with the cookie below and `Cache-Control:
      no-store`.
    Never log or echo the key. Log failed logins at info level without the
    key.
  - `POST /logout`: CSRF check; delete the session if any; clear the cookie;
    204. Works without a session (idempotent).
  - `GET /api/server/session`: with a session, `{"server": true, "key":
    name, "scope": "admin"}`; without, 401. (Local `agenticml ui` has no
    auth hook, so this path is 404 there; the frontend uses that to tell the
    two apart. Package 4b adds more `/api/server/*` routes.)
- **Session gate** for every other path: read the session cookie; if the
  session is missing or invalid, `/api/*` → 401 `{"error": "login
  required"}`; any other GET → 303 to `/login`; other methods → 401 JSON.
  Static files other than the four above, objects and `index.html` all need
  a session.
- **Cookie.** Value `secrets.token_urlsafe(32)`. Name `__Host-aml_session`
  with `Secure` when `public_url` is `https://`, else `aml_session`.
  Attributes: `HttpOnly; SameSite=Strict; Path=/; Max-Age=604800`. Parse
  the `Cookie` header with `http.cookies.SimpleCookie`; a malformed header
  counts as no session.
- **Session store.** In memory, guarded by a lock. Keyed by SHA-256 of the
  token (never the raw token). Each entry: key name, key `sha256`, created,
  last seen. Valid when: idle < 12 h, age < 7 days, and
  `keys.current(name, sha256)` still returns an admin record (so revoking
  or replacing the key ends the session on the next request; the env key
  counts while `AGENTICML_SERVER_KEY` is set). An invalid entry is deleted
  when seen. Update last seen at most once a minute. Cap 1,000 sessions;
  creating one past the cap evicts the least recently seen. Constants
  `SESSION_IDLE = 12 * 3600`, `SESSION_MAX_AGE = 7 * 86400`,
  `SESSION_CAP = 1000` at module level so tests can patch them.
- Sessions are lost on restart; that is accepted (sign in again).

### 4. `app.py` and `keys.py`

- `IngestServer.__init__` creates `self.web = WebAuth(self.keys,
  public_url)` and passes `auth=self.web, public_url=public_url` to
  `DashboardServer.__init__`; `read_only=False`.
- `serve` prints `AgenticML server: <url> (data: <data>)` as today and, when
  `public_url` is set, `Dashboard: <public_url>`; when the bind is not
  loopback and `public_url` is unset, log one warning that browsers reach the
  dashboard only through a Host it accepts and that `--public-url` should be
  set behind a proxy.
- `KeyStore.current(name, sha256)`: after `_maybe_reload`, return the record
  with exactly that name and hash (file keys or the env key), else None.
  Constant-time compare of the hash.

### 5. Frontend

- `server/static/login.html` + `login.js`: one form (password-type input,
  `autocomplete="off"`, a "Sign in" button), an error line with
  `role="alert"`. `login.js` posts JSON with `X-AgenticML-Request: 1` and
  `credentials: "same-origin"`; on 200 goes to `/`; on 401/403 shows the
  server's `error` text. No inline script (CSP). Uses `app.css` classes;
  add only what the page needs. No horizontal overflow at 375px.
- `app.js`:
  - `get()` (and the notes PUT path, if any) on 401 → `location.assign("/login")`.
  - On start, `GET /api/server/session`; 200 → show `Signed in as <name>`
    and a **Sign out** button in the top bar (POST `/logout` with
    `X-AgenticML-Request: 1`, then go to `/login`); 404 → local dashboard,
    show nothing.
  - Run detail: when `last_sync_at` is set, show `Last synced <relative
    time>` next to the existing poll status (absolute time in `title`). Run
    list rows: same text in the status tooltip/`title` only (no new column).
  - Where the lineage view shows a lineage error, show the server's `error`
    text (so "code diff not available on this server" is what people see).
- Keep zero horizontal overflow at 375px on the run list, run detail and
  login page.

## Constraints and conventions

- Python 3.9 compatible, `from __future__ import annotations`, standard
  library only in `server/`; style of `ui/server.py` and `server/ingest.py`.
- With `auth=None` and `public_url=None`, `DashboardServer` and
  `DashboardHandler` behave byte-for-byte as before: existing
  `tests/test_ui.py`, `tests/test_ui_limits.py` and the testbed UI suite pass
  unchanged.
- `/healthz` and `/v2/*` behaviour and tests unchanged.
- Never write a key or session token to logs, error bodies or URLs.
  Compare secrets in constant time where a lookup is not by hash.
- Every response from `WebAuth` carries the dashboard's security headers
  (CSP, `X-Content-Type-Options: nosniff`, `Referrer-Policy` if the dashboard
  sets one); login responses add `Cache-Control: no-store`.

## Edge cases

- A sync key used at `/login` → 403 with the sync-key message; no session.
- Key revoked (CLI) while a browser is signed in → next request 401/303.
- Key revoked and re-created with the same name → old sessions end (hash
  differs).
- `AGENTICML_SERVER_KEY` signs in as `env`; a restart without the variable
  ends those sessions (they are in memory anyway).
- Cookie for a session that expired idle or absolute → treated as none,
  entry removed.
- Forged cross-site POST to `/login`, `/logout` or a notes PUT → CSRF
  failure (403), nothing changes.
- Host header not the public host or a loopback name → 403 before auth.
- Bearer `Authorization` header on a dashboard path → ignored (dashboard uses
  sessions only).
- Two server dashboards on one machine (different ports) share cookie names
  per host: acceptable; document nothing here.
- `/v2/whoami` with a session cookie and no bearer → 401 (ingest ignores
  cookies).

## Tests

`tests/test_server_web.py` (server on port 0 in a thread, `http.client`, keys
via `KeyStore`; clock patched for expiry):

- Login: admin key → 200, cookie flags (`HttpOnly`, `SameSite=Strict`,
  `Path=/`, `Max-Age`; `Secure` and `__Host-` name only with an https
  `public_url`); wrong key 401; sync key 403 with the message; missing CSRF
  header or wrong Origin → 403; oversize body 413; no key in any response
  or captured log.
- Gate: `/`, `/static/app.js`, `/api/runs`, `/api/objects/<sha>` without a
  session → 303 / 401 as specified; login assets reachable without a
  session; with a session everything works as in the local dashboard.
- Sessions: idle expiry, absolute expiry, last-seen throttling, cap
  eviction; revoking the key (through a second `KeyStore`, like the CLI)
  ends the session on the next request; re-created same-name key does not
  revive it; logout clears it and is idempotent.
- `/api/server/session` with and without a session; 404 on a local
  `agenticml ui` server.
- CSRF and Host with and without `public_url`: notes PUT accepted with the
  right Origin, refused with a foreign Origin, a missing Origin, a missing
  `X-AgenticML-Request`, `Sec-Fetch-Site: cross-site`; Host check accepts the
  public host and loopback names only.
- Routing: `/healthz` and `/v2/whoami` unchanged (bearer still works; a
  session cookie alone is 401); unknown dashboard paths with a session → 404.
- Last synced: a run pushed through `/v2` and committed shows
  `last_sync_at` in `/api/runs` and `/api/runs/<id>`.
- Code diff: `/api/runs/<id>/lineage` on the server returns the "not
  available on this server" error.
- Notes PUT on the server writes `<data>/store/runs/<id>/notes.md`.
- Local regression: `tests/test_ui.py`, `tests/test_ui_limits.py` unchanged
  and passing; `agenticml ui` has no `/login` (404).

`testbed/tests/test_server_dashboard.py` (Playwright, `ui` marker; server
started in-process or as a subprocess on a free port with an admin key and a
run synced into it):

- Visiting `/` redirects to the login page; a sync key shows the sync-key
  message; the admin key signs in and the run list shows the run.
- Run detail shows "Last synced"; Sign out returns to the login page and
  `/` redirects again.
- No horizontal overflow at 375px on the login page, run list and run
  detail.

## Checks (coder, before handing back)

1. `testbed/.venv/bin/python -m pytest -q tests` from the repo root, all
   green; report counts.
2. From `testbed/`: `.venv/bin/python -m pytest -q` (whole testbed suite,
   including UI) green.
3. `git diff --check`; Python 3.9 syntax compatibility.
4. Manual: `agenticml server keys create --name admin --scope admin --data
   <scratch>`, `agenticml server serve --data <scratch> --port <p>`, open
   `http://127.0.0.1:<p>/` in the test browser, sign in, sign out.
5. Report the diff summary, test counts and any deviation from this plan.

## Done when

- The tests above pass in the full suite on Linux (CI green on the PR,
  including the testbed UI job).
- Sonnet's review and Codex's second read-only review (†) have no open
  findings, and the roadmap row says `in review` in the PR.

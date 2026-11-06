# Package 13 †: Dashboard sign-in (passwords, invites, identity proxy)

Nov 6, 2026 · Supervisor plan · Release 2.2 · Coder: Cursor · Size: L ·
† security-sensitive (Sonnet reviews read-only; Codex gives the second
read-only review)

Source: [on-prem-teams-plan.md](../on-prem-teams-plan.md) ("Decisions",
"Sign-in"). Builds on [11-users-and-roles.md](11-users-and-roles.md)
(`UserStore`, `Principal`, `allowed`, `generation`, `handler.principal`).
Closes the roadmap Backlog entry "Funnel visitors share one failed-login
throttle bucket". Runs in parallel with package 12 in a separate worktree;
see "Shared files".

## Goal

Every user, whatever their role, signs in to the server dashboard as
themselves:

1. **Password.** Username and password, hashed with `hashlib.scrypt`.
   Users choose their own password from a one-time **invite link**.
2. **Identity proxy.** Behind ALB OIDC, Tailscale Serve or oauth2-proxy,
   the proxy's identity header signs the user in, honoured only on
   connections from configured proxy addresses.
3. **Admin key** (fallback): an admin-scope key of an admin user, as in 2.0.

The dashboard then applies the role: viewers read only, members and admins
edit notes. The failed-login throttle keys by the real client address
behind trusted proxies.

Done when the tests below pass, the whole `tests/` suite and the `ui`
suite pass, and the Backlog entry's check passes (two forwarded client
addresses behind one proxy: failures from one do not delay the other).

Out of scope: the Team admin page, users API and account menu UI (15;
this package only adds the backend endpoints listed here and the login,
setup and invite pages), audit log (14), per-user keys API (12).

## Configuration

`agenticml server serve`:

- `--trusted-proxy CIDR` (repeatable; env `AGENTICML_SERVER_TRUSTED_PROXIES`,
  comma-separated, used when no flag is given). Plain addresses mean /32 or
  /128. Refuse `0.0.0.0/0` and `::/0` (startup error "refusing to trust
  every address"). IPv4-mapped IPv6 peers are compared as IPv4.
- `--identity-header alb|tailscale|oauth2-proxy` (env
  `AGENTICML_SERVER_IDENTITY_HEADER`). Requires at least one trusted proxy,
  else startup error. Presets:

  | Preset | Header | Match against `users.identity` |
  | --- | --- | --- |
  | `alb` | `X-Amzn-Oidc-Identity` | exact |
  | `tailscale` | `Tailscale-User-Login` | case-insensitive (`casefold`) |
  | `oauth2-proxy` | `X-Forwarded-Email` | case-insensitive (`casefold`) |

  With `tailscale`, a request carrying `Tailscale-Funnel-Request` has no
  identity (a public Funnel visitor).
- `make_server(..., trusted_proxies=(), identity_header=None)` and
  `serve(...)` take the parsed values; `IngestServer` passes them to
  `WebAuth`. Log one startup line naming the header and the ranges.
- `agenticml server up` passes `trusted_proxies=("127.0.0.1", "::1")` and
  no identity header (its proxy is the local Tailscale daemon).

## Client address and identity (`webauth.py`)

- `WebAuth.client_address(handler) -> str`: the TCP peer, unless the peer
  is trusted; then walk all `X-Forwarded-For` values (all header lines,
  comma-joined) from right to left, skipping trusted addresses; the first
  untrusted valid IP is the client. An unparsable entry before one is
  found, or no entry at all, → the peer address.
- `WebAuth.proxy_identity(handler) -> str | None`: only when an identity
  header is configured and the peer is trusted; exactly one header value,
  stripped, 1–256 characters, no control characters; else `None`.
- Use `client_address` for the throttle in `/login`, `/invite`, `/setup`
  and in `ingest._authenticate` (replace `self.client_address[0]` there —
  the only change in `ingest.py`). Never trust `X-Forwarded-For` or an
  identity header from an untrusted peer.

## Sessions and the request gate

- Session entry: `{user, generation, via, key, key_sha256, created,
  last_seen}`; `via` is `password`, `key`, `invite` or `setup`. Same idle,
  max-age, cap and cookie rules as 2.0.
- A session is valid while its user exists with the same `generation`;
  key sessions also need the key to be live, admin scope, and the user's
  role `admin`. Invalid → dropped.
- Principal resolution in `handle()`, in order:
  1. A proxy identity that matches a user → `Principal(user, role,
     "proxy")`, no cookie needed (the cookie is ignored).
  2. A valid session → its principal.
  3. A proxy identity that matches no user → 403: HTML page for page
     GETs ("Signed in as X by your identity proxy, but no AgenticML user
     has that identity. Ask an admin to run `agenticml server users
     set-identity NAME X`." — HTML-escape X), JSON
     `{"error": "no user for this identity"}` for API routes.
  4. Otherwise the 2.0 refusal (303 to `/login`, or 401 JSON).
  Set `handler.principal` (and keep `handler.session` for cookie sessions).
- `/login` GET redirects to `/` when a principal already resolves.

## `/login`

`POST /login` with CSRF as today and either:

- `{"user", "password"}` → throttle buckets: client address **and**
  `"user:" + name`; refused by either → 429. Unknown user or wrong
  password → 401 `"invalid user name or password"`; run scrypt against a
  fixed dummy record for unknown users so timing does not reveal names. A
  user with no password (pending invite) → the same 401.
- `{"key"}` → as 2.0 (sync key 403), plus an admin-scope key whose user
  is not an admin → 403 `"this key cannot sign in"`.

Success: session cookie, `{"ok": true, "user", "role"}`.

## Passwords and invites (`users.py`)

- `set_password(name, password)`: 12–1024 characters; scrypt `n=2**14,
  r=8, p=1, dklen=32`, 16-byte random salt, base64 fields as in the
  release plan; clears `invite`, bumps `generation`. `verify_password`
  uses `hmac.compare_digest`. If `hashlib.scrypt` is unavailable, password
  routes answer 503 `"password sign-in needs Python with OpenSSL scrypt"`
  and the CLI exits 1 with the same text.
- `create_invite(name)` → secret `secrets.token_urlsafe(32)`; stores
  `{sha256, expires_at = now + 72 h}`, sets `password` to `null`, bumps
  `generation`. `redeem_invite(token, password)` finds the user by
  comparing digests of every pending invite (no early exit), checks expiry,
  then `set_password`. Single use.
- `GET /invite?token=...` serves `server/static/invite.html` when the token
  is valid (else 404 JSON as `/setup` does), `Referrer-Policy:
  no-referrer`, `Cache-Control: no-store`. `POST /invite`
  `{token, password}`: CSRF, throttle by client address (bad token =
  failure), redeem, session with `via="invite"`, 200 `{ok, user, role}`.
- `POST /api/server/account/password` `{current?, new}` for any principal:
  `current` is required when the user has a password; success bumps
  `generation`, then issues a fresh session cookie for this browser (other
  sessions end). 403 for the `env` user.

## First-run setup (`/setup`)

Same one-time token flow, armed when there is no admin **user**, no admin
key, no env key and no `setup-closed` marker. The page now asks for a user
name (default `admin`) and a password (twice) and creates an admin user
with that password (no key), writes `setup-closed`, and signs in with
`via="setup"`. Update `setup.html` / `setup.js`.

`server up` with no admin creates user `admin` (role admin) with an invite
and prints `Open <public_url>/invite?token=... to choose the admin
password. The link works once, for 72 hours.` instead of printing a key.
It still refuses without a TTY.

## CLI

```
agenticml server users add NAME [--role R] [--identity ID] [--no-invite] [--public-url URL]
agenticml server users invite NAME [--public-url URL]
agenticml server users set-password NAME [--stdin]
```

- `users add` creates an invite by default and prints the link:
  `<base>/invite?token=...`, where `<base>` is `--public-url`, else
  `AGENTICML_SERVER_PUBLIC_URL`; without either, print the path and
  "open it on your server's address". The token is printed once.
- `set-password` reads twice with `getpass` (or once from stdin with
  `--stdin`); recovery path that needs no browser.

## Dashboard role gate (`ui/server.py`)

- In server mode, note `writable` (GET `/api/notes`, run notes) is
  `allowed(principal, "notes")`; local mode unchanged.
- `PUT /api/runs/<id>/notes`: 403 `{"error": "read-only"}` before reading
  the body when the principal lacks `notes`.
- `/api/server/session` → `{server: true, user, role, via, key,
  can: {notes, own_keys, all_keys, users, audit}, password_set,
  identity_proxy}` (`key` is the key name for key sessions, else `null`;
  keep `scope` for the current `app.js`).

## Login, setup and invite pages

- `login.html`/`login.js`: user name + password form first; a "Sign in
  with an admin key" toggle shows the key form. Keep the existing ids
  where the UI tests rely on them, or update those tests.
- `invite.html`/`invite.js` (new): "Choose a password" with two fields,
  posts `/invite`, then goes to `/`. Add to `_ASSETS`.
- Styles: reuse existing login-card classes in `app.css`; add only what is
  missing. No horizontal overflow at 375 px.

## Shared files (parallel with package 12)

Package 12 edits `keyspage.py`, `admin.py`, `store/reader.py`,
`ingest._commit`, the `/api/runs` rows in `ui/server.py`, and the
`keys list` / `whoami` CLI. Keep your edits in `ingest.py`, `ui/server.py`
and `cli.py` to the functions named here.

## Edge cases

- Proxy identity header present but peer untrusted → ignored entirely.
- Trusted peer with no identity header → normal session/login flow.
- A user whose identity changes while signed in through the proxy → next
  request resolves the new mapping.
- A cookie session for alice while the proxy says bob → bob.
- Removing a user ends every session (user missing).
- Re-inviting a user signs them out everywhere (generation bump).
- Invite expired or already used → 404; still counts against the address
  throttle.
- Password with leading/trailing spaces is kept as typed (no stripping).

## Tests (`tests/test_server_signin.py`, plus updates)

1. Password set/verify; scrypt parameters stored; wrong password; unknown
   user path calls scrypt (mock to assert).
2. Viewer password login: reads `/api/runs`; notes GET `writable: false`;
   notes PUT 403 without reading the body. Member: notes PUT 200.
3. Key login: admin key of admin OK; sync key 403; admin key of a demoted
   user 403; session ends on demotion.
4. Generation: password change ends other sessions and keeps this one;
   re-invite ends all.
5. Invites: CLI `users add` prints a link; GET valid / invalid / expired;
   POST sets password and signs in; second use 404.
6. Setup creates an admin user with a password; setup then closed.
7. Proxy identity: trusted peer + header → signed in; untrusted peer →
   ignored; unknown identity → 403 page and JSON; Funnel header ignored
   with `tailscale`; two header values ignored; `alb` exact vs email
   casefold.
8. Forwarded throttle (Backlog done-when): two `X-Forwarded-For` clients
   behind one trusted proxy — five failures from one do not delay the
   other; an untrusted peer's `X-Forwarded-For` is ignored; right-to-left
   selection skips trusted hops; `/v2` bearer failures use the same rule.
9. Per-user bucket: failures for one user name from many addresses throttle
   that name only.
10. Config: identity header without trusted proxy → error; `0.0.0.0/0`
    refused; env variables parsed.
11. `server up` passes loopback trusted proxies and prints an invite link
    (update `tests/test_server_up.py`).
12. `/api/server/account/password` rules.
13. UI suite: update `testbed/tests/test_server_dashboard.py` and
    `test_server_keys_dashboard.py` sign-in steps; add a password sign-in
    and an invite page test; 375 px overflow check for login and invite.

## Checks

- `PYTHONPATH=src /home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv/bin/python
  -m pytest -q tests` from the worktree root.
- From `testbed/`: `PYTHONPATH=../src .venv/bin/python -m pytest -q -m ui`
  (use the main checkout's `testbed/.venv` if this worktree has none, and
  make sure it imports this worktree's `src`).
- Update the roadmap Backlog entry for the Funnel throttle bucket (remove
  it; package 13's status line records the fix).
- Report any deviation from this plan instead of changing the approach.

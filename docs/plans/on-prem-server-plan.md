# AgenticML On-Prem Server Plan (release 2.0)

Nov 3, 2026 · @bharath · Status: approved, not started

Release 2.0 ships `agenticml server`, a self-hosted sync server and shared
dashboard, inside the public MIT-licensed `agenticml-lite` package on PyPI
(the container image was dropped on Nov 5, 2026). This document is the implementation plan for 2.0. The
release sequence after it (2.1 full fidelity, 2.2 teams) is in
[on-prem-roadmap.md](on-prem-roadmap.md).

It replaces the On-Prem Server Plan (Nov 2), the Open-Core Server Plan and the
Open-Source Server Plan (Nov 3). It keeps their architecture and adds the
fixes found in review: append rollback, Docker default bind, the feature list,
the commit fingerprint, run-id ownership and the dashboard behind TLS. Keys are
issued from the server dashboard with separate sync and admin scopes
(section 5.1).

## 1. Decisions

| Topic | Decision |
| --- | --- |
| Deployment | On-prem only. No hosted backend, so the sync protocol can change freely |
| Code and license | Server lives in public `agenticML/agenticml-lite`, MIT, next to the client |
| Team features (2.2) | Also open source, same repo, MIT |
| Dashboards | The server hosts the shared dashboard; local `agenticml ui` keeps working unchanged |
| Users | Single user in 2.0, one API key per machine. Teams in 2.2 |
| Admin sign-in | An admin API key pasted once per browser (section 5.1). Kept for 2.0 only; the whole admin login is redesigned later (see the roadmap) |
| Storage | Server data directory holds a plain v1 store. No database |
| Sync | Byte-for-byte copy of the v1 store over protocol v2 (section 4) |
| Dependencies | Standard library only on the server. `pip install agenticml-lite` gets it; still only `requests` |
| Licensing / paid tier | None. No license file, no tiers |
| Version | `agenticml-lite` 2.0.0 (removing hosted sync is breaking) |
| Image | None. Removed Nov 5, 2026: the server ships via PyPI only (`pip install agenticml-lite`) and runs under systemd behind a TLS proxy; see [packages/remove-docker-deployment.md](packages/remove-docker-deployment.md) |
| PRs | One PR per work package, merged in the order of section 7 |
| Platforms | Server: Linux and any POSIX host. Test CI: Linux only. macOS is not a CI target and macOS-only test failures are not tracked |
| Private `agenticml-server` repo | Left as is (the old hosted stack). Not used or changed |

## 2. What exists today

- **Local store v1** ([FORMAT.md](../FORMAT.md)): append-only `scalars/<id>.bin`,
  `scalars/_time.bin` and `events.jsonl`; atomically replaced `meta.json`,
  `scalars/index.json`, `media/index.json`, `scalars/<id>.ovw`;
  content-addressed `objects/<hh>/<sha256>`; per-project shadow Git repos in
  `lineage/`.
- **`agenticml ui`** (`src/agenticml/ui/server.py`): a hardened stdlib dashboard.
  Trusts loopback only (`_host_permitted`), no authentication, CSRF check in
  `_csrf_ok` that assumes `http://`.
- **`agenticml login` / `sync`** (`auth.py`, `client.py`, `sync.py`): speak the old
  hosted protocol (`/init`, `/log/batch`, presigned S3) to the default host
  `https://api.agenticml.xyz`. Reusable parts: per-host cursor in `sync.json`,
  run lock, lineage ordering (`_order_runs_lineage`), prune, permanent-error
  skips, `normalize_host` (refuses plain HTTP to non-local hosts without
  `--insecure`).
- **`agenticml gc`** treats a run as synced when a host entry in `sync.json` has
  `complete: true` and `source_hash == run_fingerprint()` (`store/gc.py:206`).
- **CI**: `publish.yml`, `publish-testpypi.yml`, `docs.yml`. No test workflow.

## 3. Architecture

```
training machine A ─┐                      ┌──────────── container ─────────────┐
training machine B ─┼─ HTTPS ─▶ TLS ─ HTTP ▶│ /healthz, /v2/*  → ingest          │
laptop             ─┘   (ALB, tailscale    │ everything else  → dashboard       │
browser ───────────────  serve, Caddy)     │          │ writes        ▲ reads   │
                                            │          ▼               │         │
                                            │   <data>/store   (v1 store)        │
                                            │   <data>/server  (keys, lock)      │
                                            └─────────────────────────────────────┘
```

One process, one port. The ingest handler writes a v1 store; the dashboard is
the `agenticml ui` code reading that store. Nothing is translated.

Server data directory:

```
<data>/store/                 v1 store (runs/, objects/, lineage/)
<data>/store/runs/<id>/remote.json   server-only sync status (section 4.6)
<data>/server/keys.json       API keys, SHA-256 only, mode 0600
<data>/server/keys.lock       flock for keys CLI writes
<data>/server/server.lock     single-writer flock held by `serve`
<data>/server/tmp/            upload temp files, emptied at startup
```

`<data>` is `--data`, else `AGENTICML_SERVER_DATA`, else `/data` in the image
and `~/.agenticml-server` elsewhere.

## 4. Sync protocol v2

The full spec ships as `docs/PROTOCOL.md` in package 3. This section is its
contract.

### 4.1 General rules

- Every request except `GET /healthz` needs `Authorization: Bearer <key>`,
  with a `sync` or `admin` key (section 5.1).
  Keys are `aml_` + 43 URL-safe base64 characters (32 random bytes). The server
  compares SHA-256 digests with `hmac.compare_digest`. 401 on a missing,
  unknown or revoked key.
- `{id}` must pass `layout.valid_run_id`. `{path}` must be on the allow-list
  for its endpoint. Anything else is 404, before any filesystem access.
- Errors are JSON `{"error": "...", ...}`. 409 and 422 bodies carry the fields
  the client needs to recover (shown below).
- Limits (configurable, defaults): 16 MiB per atomic file, 64 MiB per append
  request, 4 GiB per object, 10,000 hashes per `missing` call. Body reads use
  deadlines like the dashboard's notes PUT, scaled to the request size.

### 4.2 Endpoints

| Method and path | Body | Result |
| --- | --- | --- |
| `GET /healthz` | – | `200 {"ok": true}`. No auth |
| `GET /v2/whoami` | – | `{"key": "<name>", "protocol": 2, "features": []}` |
| `POST /v2/objects/missing` | `{"objects": [sha256, ...]}` | `{"missing": [...]}` |
| `PUT /v2/objects/{sha256}` | raw bytes | 201 created, 200 already present, 422 hash mismatch |
| `GET /v2/runs/{id}/state` | – | `{"owner": "<key>", "files": {path: {"size": n, "sha256": h}}, "remote": {...}}`; 404 if unknown |
| `PUT /v2/runs/{id}/files/{path}` | raw bytes | 200. Atomic replace (temp file + rename) |
| `POST /v2/runs/{id}/append/{path}?offset=N[&truncate=1]` | raw bytes | `{"size": n}`; 409 `{"size": n}` on mismatch |
| `POST /v2/runs/{id}/commit` | JSON, see 4.6 | `{"complete": true, "fingerprint": "..."}`; 409 `{"sizes": {...}}` on size mismatch |

Allow-lists:

- Atomic files: `meta.json`, `scalars/index.json`, `media/index.json`,
  `scalars/<n>.ovw` (`<n>` a decimal series id).
- Append files: `events.jsonl`, `scalars/_time.bin`, `scalars/<n>.bin`.
- Never accepted: `run.lock`, `sync.json`, `notes.md`, `remote.json`.

`features` is empty in 2.0. Package 7 adds `lineage` and package 8 adds
`byref`; a server only lists a feature once it implements it, and the client
only uses a feature the server lists.

### 4.3 Run creation and ownership

The first `PUT meta.json` for an unknown run creates `<data>/store/runs/<id>/`
and records the key name as `owner` in `remote.json`. Any later write to that
run with a different key gets 409 `{"error": "run owned by another key"}`.
Run ids are user-settable, so two machines can produce the same id; this stops
them from interleaving into one run. In 2.2 ownership moves from key to user.

The server validates `meta.json` as a JSON object whose `id` equals `{id}`.

### 4.4 Appends, whole records, and rollback

- A request appends bytes at `offset`. The server's file has size `S`.
  - `offset > S`: 409 `{"size": S}`; the client resends from `S`.
  - `offset <= S`: bytes overlapping `[offset, S)` must equal the server's
    bytes, else 409 `{"size": S, "mismatch": true}`. Bytes past `S` are
    appended. A replay is therefore a no-op.
- The client only sends whole records: `.bin` files in multiples of
  `layout.RECORD_SIZE`; `events.jsonl` up to and including the last `\n`. The
  server rejects a body that would leave a torn record (422).
- **Rollback.** `write_scalars` is all-or-nothing: on an I/O error it
  truncates touched `.bin` files and `_time.bin` back to their pre-batch sizes
  (FORMAT.md). If sync read during that batch, the server can hold records the
  local store later drops, and the next append conflicts forever. So:
  - When a local file is smaller than the client's cursor, or an append gets
    409 with `mismatch`, the client re-sends the whole file with
    `offset=0&truncate=1`.
  - `truncate=1` replaces the server file atomically (temp file + rename)
    with exactly the body. It is only accepted while the run's remote status
    is `running`; for a `finished`/`crashed` run it is 409.
  - The client logs one warning per rewritten file.

### 4.5 Push order for one run

1. Objects referenced by the run (code manifest, media, copied artifacts):
   `missing`, then `PUT` each missing object.
2. `meta.json` (the first push creates the run).
3. Append files from the per-file cursor, whole records only.
4. Overviews and indexes, then `meta.json` again if it changed.
5. `commit`.

### 4.6 Commit and run status

Commit body: `{"status": "<meta status>", "fingerprint": "<run_fingerprint()>",
"sizes": {path: size}}`.

The server checks every size against its files (409 with the server's sizes if
any differ, so the client resyncs those files), then atomically writes
`remote.json`:

```json
{"owner": "gpu-box-1", "status": "running", "fingerprint": "...",
 "last_sync_at": "2026-11-03T12:00:00Z", "sizes": {"events.jsonl": 1234}}
```

The client stores `complete: true` and `source_hash: <fingerprint>` for the
host in `sync.json`, which keeps the existing `gc` rule working unchanged.

Status shown by the server dashboard (package 2):

| `remote.json` status | Shown as |
| --- | --- |
| `finished`, `crashed` | as is |
| `running`, last sync within `AGENTICML_REMOTE_STALE_SECONDS` (default 600) | `running` |
| `running`, older | `stale` |
| file missing or invalid | today's lock-based logic |

`sync --follow` (package 5) commits at least every 60 s for running runs, so a
live run does not turn stale.

## 5. Security

| Control | Design |
| --- | --- |
| API keys | Two scopes (section 5.1). `sync` keys go on training machines and can only call `/v2`. `admin` keys can also sign in to the dashboard and manage keys. Every key is shown once; `keys.json` (0600) stores name, scope, SHA-256, prefix, `created_at`, `created_by` and `last_used_at`. Revoking takes effect without a restart (the server reloads `keys.json` when its mtime changes). `AGENTICML_SERVER_KEY` adds an env-defined admin key named `env` at startup, not written to disk |
| Default bind | `serve` binds `127.0.0.1:8080` by default. Any other address needs an explicit `--host`. With zero keys the server still starts, logs a warning, and rejects every request except `/healthz`, so it is closed by default. The image passes `--host 0.0.0.0` |
| TLS | The server speaks HTTP behind an ALB, `tailscale serve` or Caddy. The client keeps refusing plain HTTP to non-local hosts without `--insecure` |
| Single writer | `serve` holds an exclusive flock on `server.lock`; a second process exits with an error. The keys CLI uses `keys.lock`, so it works while the server runs |
| Input hardening | Allow-listed paths, `valid_run_id`, no-follow opens, `contained()` checks, per-request size limits, body-read deadlines, connection caps reused from the dashboard, a per-run lock so concurrent writes to one run serialize |
| Dashboard login | `/login` takes an **admin** API key (a sync key is refused) and sets a random session cookie: HttpOnly, SameSite=Strict, Secure when `--public-url` is https. Sessions are in memory (12 h idle, 7 days absolute) and die when their key is revoked. CSRF: the existing `X-AgenticML-Request` check, with the Origin compared against `--public-url` |
| Host header | Server dashboard accepts the `--public-url` host plus loopback names |
| Disclosure | `SECURITY.md`: private reporting address and GitHub private advisories |
| Threat model | `docs/THREAT_MODEL.md`. In scope: network attacker with no key, a leaked or revoked key, malicious run files and paths, a second machine reusing a run id. Out of scope: a compromised server host or a compromised key holder's own runs |
| Supply chain | PyPI Trusted Publishing from a tag; Dependabot for Actions. (The container image, its signature and SBOM were dropped with the Docker deployment.) |
| Review | Packages marked † get a second read-only review from the coder that did not write them, in addition to Sonnet |

### 5.1 Issuing API keys

Keys are issued by the server, because only the server can check them. The
main way to issue keys is the **Keys page of the dashboard that
`agenticml server` hosts**. That is the same dashboard code as `agenticml ui`;
the Keys page only appears when the dashboard runs inside the server. A
training machine's local `agenticml ui` has no key store, so it has no Keys
page.

Two scopes, so that a key copied onto a GPU machine cannot create more keys:

| Scope | Used by | Can |
| --- | --- | --- |
| `sync` | Training machines (`agenticml login`, `sync`) | Call `/v2` only. Cannot sign in to the dashboard |
| `admin` | You, in a browser | Everything `sync` can, plus dashboard sign-in and creating, listing and revoking keys |

If any key could create keys, a key leaked from a GPU machine could create a
replacement before you revoked it. The scope split closes that hole, and it
maps onto the 2.2 roles (admin keys belong to admins, sync keys to members).

**First key (bootstrap).** A fresh server has no keys, so nobody can sign in.
At startup, if there is no admin key on disk and `AGENTICML_SERVER_KEY` is not
set, the server creates a one-time setup token in memory and logs:

```
No admin key yet. Open <public-url>/setup?token=<token> to create one,
or run: agenticml server keys create --name admin --scope admin
```

`/setup` checks the token in constant time, creates the first admin key, shows
it once, and signs the browser in. The token works once and is gone after use
or a restart. Once an admin key exists, `/setup` returns 404. Under systemd
the line appears in `journalctl -u agenticml-server`; otherwise in the
terminal running `agenticml server serve`.

**Issuing a key for a GPU machine (normal flow).**

1. Sign in to the server dashboard with an admin key.
2. Keys page → "New key" → name (for example `gpu-box-1`), scope `sync`
   (default).
3. The page shows the key once, with a copy button and a ready-to-paste
   command built from `--public-url`:
   `agenticml login --host https://aml.example.com --key aml_...`
4. On the GPU machine: paste the command, then `agenticml sync --all --follow`.

The Keys page lists every key with name, scope, prefix (`aml_xxxx…`), created,
created by, last used, and the number of runs it owns, with a Revoke button.
Revoking asks for confirmation, ends that key's dashboard sessions and stops
its syncs on the next request. Revoking the key your own session uses signs you
out. The last admin key cannot be revoked from the page, so you cannot lock
yourself out; use the CLI for that.

**CLI (same operations, for scripts and recovery):**
`agenticml server keys create --name N [--scope sync|admin]`,
`keys list`, `keys revoke N`. They work while the server is running.

All Keys page requests need an admin session and pass the CSRF check. The
create response is `Cache-Control: no-store`, and the key is never written to
logs.

## 6. Compatibility

- Local-only use (training plus `agenticml ui`) is unaffected.
- Sync to `api.agenticml.xyz` is removed. `login` requires `--host` unless a
  default host is already saved; `DEFAULT_HOST` is deleted from `auth.py` and
  `client.py`.
- Old-host entries in `sync.json` are ignored, not deleted.
- `whoami` against a server without `protocol: 2` fails with a clear message.
- `gc` still treats a run as synced when any host reports it complete with a
  matching fingerprint.
- In 2.0 the server shows the lineage graph (it comes from `meta.json`) but not
  code diffs, which need the shadow Git repos from package 7 (2.1). The
  dashboard shows "code diff not available on this server" instead of an error.
- The server needs a POSIX `fcntl`; Windows can run the client and
  `agenticml ui` but not the server.

## 7. Work packages

Order: **0**, then **1** and **2** in parallel, then **3** and **4** in
parallel, then **4b** and **5** in parallel, then **6**. Each package is one PR. Every package passes
`testbed/.venv/bin/python -m pytest -q tests`; dashboard packages also pass the
testbed UI suite (`-m ui`) and keep zero horizontal overflow at 375px.

Workflow per package (from `~/.claude/CLAUDE.md` and `AGENTS.md`): **planning
task first**, in which Claude writes `docs/plans/packages/<id>-<slug>.md` from
this section and the current code, then sets the status to `plan ready` → coder
implements from that file → Sonnet reviews read-only → fix loop
→ † packages get the other coder's read-only review → Claude verifies the diff
and runs the checks → the `git` agent opens the PR.

### Package 0: Test CI (Codex, S)

- **Files:** `.github/workflows/test.yml`.
- **Approach:** on push to `main` and on PRs. Job `unit`: `ubuntu-latest`,
  Python 3.9 and 3.12, install `-e '.[dev]'`, run `pytest -q tests`. Job `ui`:
  Python 3.12, set up the testbed venv as in `testbed/README.md`, install
  Playwright Chromium, run `pytest -q -m ui` from `testbed/`. Cache pip.
- **Done when:** the workflow is green on the PR and on `main`.

### Package 1 †: Server ingest (Codex, L)

- **Files:** new `src/agenticml/server/{__init__,app,ingest,keys}.py`;
  `src/agenticml/cli.py` (the `server` subcommand); new `tests/test_server_ingest.py`,
  `tests/test_server_keys.py`.
- **Approach:**
  - `keys.py`: create / list / revoke / verify with the `sync` and `admin`
    scopes and the fields of section 5; atomic writes of `keys.json` under
    `keys.lock`; mtime-based reload; `last_used_at` updated at most once a
    minute per key; `AGENTICML_SERVER_KEY` as an admin key. A Python API
    (`KeyStore`) that both the CLI and package 4b call.
  - `app.py`: data-dir layout and permissions, `server.lock`, temp-dir cleanup,
    a `ThreadingHTTPServer` subclass that reuses `DashboardServer`'s connection
    caps, routing of `/healthz` and `/v2/*` to ingest. Until package 4 lands,
    every other path is 404.
  - `ingest.py`: the endpoints of section 4.2 with the rules of 4.1, 4.3, 4.4
    and 4.6. Streamed object upload to `<data>/server/tmp`, hash check, rename
    into `objects/<hh>/` with no overwrite. Per-run `threading.Lock`.
  - CLI: `agenticml server serve [--data] [--host] [--port] [--public-url]`,
    `agenticml server keys create --name N [--scope sync|admin] | list |
    revoke N` (scope defaults to `sync`).
- **Tests:** auth (missing, wrong, revoked, env key, both scopes accepted on
  `/v2`); keys CLI while the server runs; default bind; zero-key
  start; second `serve` refused; path allow-list and traversal (`..`, absolute,
  encoded, symlinked run dir); oversize bodies; torn-record rejection; append
  replay is a no-op; offset gap and mismatch 409s; `truncate=1` allowed only
  while running; run ownership 409; object hash mismatch 422; a real run built
  with the writer, pushed through raw HTTP and read back with `RunReader`
  matching the source.
- **Done when:** the tests above pass and the server can be started from the CLI.

### Package 2: Run status from `remote.json` (Cursor, S)

- **Files:** `src/agenticml/store/reader.py` (status logic near lines 192–204),
  `docs/FORMAT.md` (new `remote.json` section), `tests/test_store.py`.
- **Approach:** if `runs/<id>/remote.json` exists and parses, map it per the
  table in 4.6; expose `last_sync_at` on the reader. Otherwise keep today's
  lock-based logic. Writers never create `remote.json`.
- **Done when:** every row of the table is tested, including invalid JSON,
  wrong types, a future `last_sync_at` and a custom stale window.

### Package 3: Sync client v2 (Codex, L)

- **Files:** `src/agenticml/sync.py`, `client.py`, `auth.py` (rewritten);
  `cli.py` (flags unchanged); new `docs/PROTOCOL.md`; `tests/test_sync.py`,
  `tests/test_client.py`, `tests/test_auth.py` (rewritten); `docs/SDK.md` sync
  section.
- **Approach:**
  - Keep: per-host state in `sync.json` (now a per-file cursor), run lock,
    `_order_runs_lineage`, `--all`, `--prune`, `--reset`, `--dry-run`,
    permanent-error skips, `normalize_host`.
  - Replace: `/init` payloads, `_scalar_batches` and presigned uploads with the
    push order of 4.5 and the rollback rule of 4.4.
  - `--reset` and a lost cursor use `GET /state` to resume from the server's
    sizes instead of re-uploading.
  - `login` / `whoami` check `protocol: 2`. Remove `DEFAULT_HOST`.
  - Write `docs/PROTOCOL.md` from section 4.
- **Tests:** end-to-end against an in-process package 1 server: sync finished,
  crashed and running runs and compare every file byte for byte; replayed and
  interrupted syncs add no duplicates; local rollback (truncate a `.bin` below
  the cursor) recovers via `truncate=1`; `--prune` and `gc` still see the run as
  synced; `task`, `task_source` and `arch` in `meta.json` reach the server;
  old-host `sync.json` entries are ignored.
- **Done when:** the tests above pass and no code references
  `api.agenticml.xyz`.

### Package 4 †: Server dashboard (Cursor, M)

- **Files:** `src/agenticml/ui/server.py` (changed), new
  `src/agenticml/server/webauth.py`, `src/agenticml/ui/static/` (login page and
  "last synced" display), `src/agenticml/server/app.py` (mount),
  `tests/test_server_web.py`, testbed UI tests.
- **Approach:**
  - `ui/server.py`: `DashboardServer` takes an optional auth hook and a
    `public_url`. `_host_permitted` and `_csrf_ok` use `public_url` when set.
    With no hook and no `public_url`, behaviour is exactly today's.
  - `webauth.py`: `/login` (GET page, POST admin key; a sync key is refused
    with "this is a sync key; sign in with an admin key"), `/logout`, session
    store, cookie flags of section 5. Every other dashboard path, including static
    files other than the login page's assets, needs a session; `/api/*`
    returns 401 JSON, pages redirect to `/login`.
  - Run views show "last synced <time>" from `remote.json`. Code diff shows the
    "not available on this server" message (section 6).
  - Notes edits on the server write the server's own `notes.md`.
- **Tests:** login success and failure, sync key refused, session expiry, revoked key ends the
  session, `/api/*` without a session, CSRF with and without `public_url`,
  Host check. Local UI tests unchanged. UI suite passes with no overflow at 375px.
- **Done when:** the tests above pass.

### Package 4b †: Key management in the dashboard (Codex, M)

- **Files:** `src/agenticml/server/webauth.py` (setup token, `/setup`),
  new `src/agenticml/server/keyspage.py` (`/api/server/keys` endpoints),
  `src/agenticml/ui/static/` (Keys page and setup page, nav entry shown only
  when the server reports `server: true`), `tests/test_server_keys_web.py`,
  testbed UI tests.
- **Approach:** implements section 5.1 on top of package 1's `KeyStore`.
  - Setup token: created at startup only when no admin key exists and
    `AGENTICML_SERVER_KEY` is unset; logged once; constant-time compare;
    single use; `/setup` is 404 once an admin key exists.
  - `GET /api/server/keys` (list, never hashes), `POST /api/server/keys`
    (`{name, scope}` → key shown once, `Cache-Control: no-store`),
    `DELETE /api/server/keys/{name}`. All need an admin session and the CSRF
    check; names must be unique and match `[A-Za-z0-9._-]{1,64}`.
  - Revoke ends that key's sessions; the last admin key cannot be revoked from
    the page.
  - The create dialog shows the key, a copy button, and the
    `agenticml login --host <public-url> --key …` command. When
    `--public-url` is unset it uses the request's Host and says so.
  - Local `agenticml ui`: no nav entry, and `/api/server/*` is 404.
- **Tests:** setup flow (valid token, wrong token, reused token, token absent
  once an admin exists, token gone after restart); create, list and revoke
  through the API; key value never in logs or list responses; sync-scope key
  from the page works for `/v2` and fails `/login`; revoked key's session and
  syncs end; last-admin guard; CSRF and no-session rejections; local
  `agenticml ui` has no Keys page. UI suite covers the Keys page and the
  create dialog with no overflow at 375px.
- **Done when:** the tests above pass and a key created on the Keys page is
  used by `agenticml login` and `sync` in the package 3 end-to-end test.

### Package 5: `sync --follow` (Cursor, M)

- **Files:** `src/agenticml/sync.py`, `cli.py`, new
  `deploy/systemd/agenticml-sync.service`, `tests/test_sync_follow.py`.
- **Approach:** loop: rescan runs (picking up new ones), sync each, commit
  running runs at least every 60 s, sleep `--interval` (default 30 s).
  Exponential backoff with jitter up to 5 min while the server is unreachable;
  401 stops the loop with an error. Clean exit on SIGINT / SIGTERM after the
  current request. `--follow` implies `--all` unless run ids are given.
- **Tests:** server down then back; a run created after start is synced
  without a restart; heartbeats keep a running run from turning stale; 401
  exits non-zero; signal exit.
- **Done when:** the tests above pass.

### Package 6: Packaging and release 2.0.0 (Codex; prose by Antigravity, M)

> Nov 5, 2026: the image, compose, ECS and image-workflow parts below were
> built in #34 and then removed by decision; the server ships via PyPI only.
> See [packages/remove-docker-deployment.md](packages/remove-docker-deployment.md).

Depends on 1, 3, 4, 4b and 5.


- **Files:** `docker/Dockerfile`, `.dockerignore`, `deploy/compose.yaml`,
  `deploy/ecs/` (task definition JSON and a Terraform example),
  `.github/workflows/image.yml`, `.github/dependabot.yml`,
  `scripts/server_smoke.sh`, `pyproject.toml` (version 2.0.0), `SECURITY.md`,
  `docs/THREAT_MODEL.md`, `docs/SERVER.md`, `README.md`, new pages under
  `website/src/content/docs/` (install, keys, sync, backups, upgrade).
- **Approach:**
  - Dockerfile: `python:3.12-slim` pinned by digest, installs `git`, the
    package from the build context, UID 10001, `VOLUME /data`,
    `EXPOSE 8080`, `CMD agenticml server serve --data /data --host 0.0.0.0
    --port 8080`.
  - `image.yml`: on `v*` tags, buildx for `linux/amd64` and `linux/arm64`, push
    to GHCR, cosign keyless sign, attach SBOM.
  - ECS example: one Fargate task, desired count 1,
    `minimumHealthyPercent: 0`, `maximumPercent: 100`, EFS access point with
    UID 10001 at `/data`, ALB HTTPS with health check `/healthz`,
    `AGENTICML_SERVER_KEY` from Secrets Manager, AWS Backup on the EFS.
  - `docs/SERVER.md`: laptop setup (`docker run` + `tailscale serve`), ECS,
    keys, client setup, backups (copy `<data>` while stopped, or EFS backup),
    upgrade.
  - `docs/SERVER.md` documents the first-key flow (setup URL from the logs)
    and issuing sync keys from the Keys page.
  - Smoke script: build image, run it, create a key, sync a testbed run,
    read it back through `/v2/runs/{id}/state` and the dashboard API.
- **Done when:** `docker build` passes, the smoke script passes, and docs are
  checked against the code by Claude.

## 8. Release 2.0 checklist

- [ ] Packages 0–6 merged in order, each with its review record in the PR.
- [ ] `pytest -q tests` and the UI suite green on `main` in CI.
- [ ] Smoke script green against the pip-installed server from the release tag
      (it runs in the unit CI job).
- [ ] Manual check: fresh laptop server over Tailscale, first admin key from
      the setup URL, a sync key issued on the Keys page, one training machine
      with `sync --follow`, live run visible and updating in the server
      dashboard, then the key revoked and the sync stopped.
- [ ] Tag `v2.0.0`: `publish.yml` pushes PyPI.
- [ ] Website pages live; README links to `docs/SERVER.md`.

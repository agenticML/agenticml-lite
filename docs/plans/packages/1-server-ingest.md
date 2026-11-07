# Package 1 †: Server ingest, keys, `/v2` endpoints, server lock

Nov 3, 2026 · Supervisor plan · Release 2.0 · Coder: Codex · Size: L ·
† security-sensitive (second read-only review by Cursor)

Source: [on-prem-server-plan.md](../on-prem-server-plan.md), sections 3, 4,
5, 5.1 and 7 (package 1). Roadmap: [on-prem-roadmap.md](../on-prem-roadmap.md).
Base branch: `dev`.

## Goal

`agenticml server serve` runs a standard-library HTTP server that accepts the
protocol v2 sync endpoints (section 4 of the release plan) into a plain v1 store
under `<data>/store`, authenticated by API keys that `agenticml server keys`
issues and revokes. One process per data directory. Every path other than
`/healthz` and `/v2/*` is 404 until package 4 mounts the dashboard.

Out of scope: the sync client (package 3), `docs/PROTOCOL.md` (package 3),
the dashboard, login, sessions and the setup token (packages 4 and 4b),
`remote.json` status in the reader (package 2), the image (package 6).

## Files to touch

| Path | Change |
| --- | --- |
| `src/agenticml/server/__init__.py` | New. Docstring; exports `KeyStore`, `make_server`, `serve` |
| `src/agenticml/server/keys.py` | New. `KeyStore`, key format, `keys.json` I/O |
| `src/agenticml/server/app.py` | New. Data-dir layout, `server.lock`, tmp cleanup, `IngestServer`, `make_server`, `serve` |
| `src/agenticml/server/ingest.py` | New. `IngestHandler` and the `/v2` endpoints |
| `src/agenticml/ui/server.py` | Small, behaviour-preserving: `DashboardServer.__init__` takes `handler_class=DashboardHandler`; `_admit_note_body` becomes a wrapper around a new `_admit_body(request, limit)` |
| `src/agenticml/cli.py` | `server` subcommand: `serve`, `keys create|list|revoke` |
| `tests/test_server_keys.py` | New |
| `tests/test_server_ingest.py` | New |
| `docs/plans/on-prem-roadmap.md` | Package 1 row: Status `in review` (supervisor does this) |

Nothing else changes. No new dependencies (`pyproject.toml` untouched).

## Approach

### 1. `keys.py`

- Key value: `"aml_" + secrets.token_urlsafe(32)` (43 characters after the
  prefix). Store only `sha256` (hex of the UTF-8 key), never the key.
- Name: `^[A-Za-z0-9._-]{1,64}$`, unique. `env` is reserved.
- Scopes: `sync` (default) and `admin`.
- `keys.json` (path `<data>/server/keys.json`, mode 0600):

  ```json
  {"version": 1, "keys": [{"name": "gpu-box-1", "scope": "sync",
    "sha256": "<hex>", "prefix": "aml_AbCd", "created_at": "<format_timestamp>",
    "created_by": "cli", "last_used_at": null}]}
  ```

  `prefix` is the first 8 characters of the key. Timestamps use
  `layout.format_timestamp`.
- Writes: take an exclusive `fcntl.flock` on `<data>/server/keys.lock`, re-read
  `keys.json` from disk, change it, write a temp file in the same directory
  opened with `os.open(..., O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o600)`, fsync,
  `os.replace`, fsync the directory. The CLI and the running server use the same
  path, so the CLI works while the server runs.
- `KeyStore(server_dir, env_key=None)` API (package 4b builds on this; keep it
  stable and documented in docstrings):
  - `create(name, scope="sync", created_by="cli") -> (KeyRecord, key_str)`;
    `ValueError` on a bad or duplicate name or bad scope.
  - `list() -> list[KeyRecord]` (file keys, then the env key if set; never
    the hash in any public representation).
  - `revoke(name) -> bool`; deletes the record. `env` cannot be revoked
    (`ValueError`). Revoking the last admin key is allowed here (the CLI is
    the recovery path; 4b adds the page guard).
  - `verify(key_str) -> KeyRecord | None`: hash the presented key and compare
    against **every** record with `hmac.compare_digest` (no early exit, no dict
    lookup by hash). Calls `_maybe_reload()` first.
  - `touch(record)`: if the record's `last_used_at` is older than 60 s (or
    unset), update it under `keys.lock`, only if a record with the same name
    **and** `sha256` still exists on disk (never resurrect a revoked key).
    Errors are logged, never raised to the request. The env key's
    `last_used_at` is in memory only.
  - `has_admin() -> bool` (used by 4b and the startup warning).
- `KeyRecord`: frozen dataclass `name, scope, sha256, prefix, created_at,
  created_by, last_used_at`, plus `to_public()` returning a dict without
  `sha256`.
- Reload: `_maybe_reload()` stats `keys.json` with `os.stat(follow_symlinks=False)`;
  when `(st_ino, st_mtime_ns, st_size)` differs from the cached tuple, re-read
  it with `layout.read_file_nofollow`. Missing file = zero file keys. A file
  that is unreadable, a symlink, or invalid (bad JSON, wrong types, bad names,
  duplicate names, non-hex hash) **fails closed**: zero file keys until it
  changes again, one `logger.error` per change. Thread-safe with a
  `threading.Lock` around the cache.
- Env key: `AGENTICML_SERVER_KEY`, read by `serve` and passed in as
  `env_key`. Must be 32–256 characters from `[A-Za-z0-9._~+/=-]`; anything
  else makes `serve` exit 1 with a message that does not print the value.
  Name `env`, scope `admin`, `created_by` `"env"`, not written to disk.

### 2. `app.py`

- `resolve_data_dir(arg)`: `--data`, else `AGENTICML_SERVER_DATA`, else
  `~/.agenticml-server`; `expanduser().resolve()`.
- `prepare_data_dir(data)`: create `<data>` (0700), `<data>/store` (0700),
  `<data>/server` (0700), `<data>/server/tmp` (0700) when missing. Refuse
  (`SystemExit`-style error from `serve`) if `store`, `server` or
  `server/tmp` is a symlink or not a directory. Warn (do not fail) if
  `<data>/server` is group- or world-accessible. Refuse to start if
  `<data>/server/tmp` and `<data>/store` are on different devices
  (`st_dev`), because uploads are renamed from one to the other.
- `acquire_server_lock(server_dir)`: open `server.lock` with
  `layout.open_nofollow(..., "ab")`, `flock(LOCK_EX | LOCK_NB)`. `EAGAIN` /
  `EACCES` → error "another agenticml server is using <data>". Locking
  unsupported (`ENOLCK`, `ENOSYS`, `EOPNOTSUPP`) → also refuse to start, with
  a message naming the filesystem problem (single writer is a safety
  property). Hold the handle for the life of the process. Windows/no `fcntl`:
  refuse to start ("the server needs POSIX fcntl").
- After the lock: empty `<data>/server/tmp` (unlink entries with
  `os.unlink`, never follow symlinks; ignore subdirectories with a warning).
- `IngestServer(DashboardServer)`: constructed with `root=<data>/store`,
  `handler_class=IngestHandler`. Extra attributes: `data_dir`, `tmp_dir`,
  `keys` (`KeyStore`), `public_url` (stored for package 4, unused here),
  `limits` (a small dataclass with the defaults below, overridable in tests),
  `run_locks` (per-run `threading.Lock`s in a dict guarded by a lock; a
  `weakref.WeakValueDictionary` is fine), `objects_lock`. IPv6 variant like
  `IPv6DashboardServer`.
- `make_server(data, host="127.0.0.1", port=8080, public_url=None,
  env_key=None)` → prepared, locked server (port 0 picks a free port; tests use
  it in a thread like `tests/test_ui_limits.py`). The lock handle is released
  in `server_close()`.
- `serve(...)`: prints `AgenticML server: http://<host>:<port> (data: <data>)`.
  With zero keys (no file keys, no env key) it logs a warning that every
  request except `/healthz` will be rejected until a key is created with
  `agenticml server keys create`. Never logs a key value.
- `--public-url`: optional; must be `http://` or `https://` with a host and no
  query or fragment, else exit 1. Only stored in this package.

Limits (module constants, defaults from section 4.1):

| Limit | Default |
| --- | --- |
| Atomic file body | 16 MiB |
| Append body | 64 MiB |
| Object body | 4 GiB |
| `missing` hashes per call | 10,000 (body cap 1 MiB) |
| Commit body | 1 MiB |
| Concurrent request bodies being received (non-evictable) | 8 |
| Body read deadline | 30 s + 1 s per 512 KiB declared |

### 3. `ui/server.py` refactor (no behaviour change)

- `DashboardServer.__init__(self, root, host, port, read_only=False,
  handler_class=None)`: `None` means `DashboardHandler`. `make_server` and
  `serve` in this module are unchanged.
- `_admit_body(request, limit)` holds the current `_admit_note_body` logic
  with `limit` as a parameter; `_admit_note_body(request)` calls it with the
  existing note limit. Every existing UI test must pass unchanged.

### 4. `ingest.py`

`IngestHandler(DashboardHandler)` reuses the header deadline, response
writer and connection-state tracking, `_json`, `_error`. It overrides
`do_GET`, `do_PUT`, `do_POST` and the other methods.

Request flow, in this order, before reading any body:

1. Route on the **raw** path from `urlsplit(self.path)`. A path containing
   `%`, `\`, `//`, a `.` or `..` segment, or any character outside
   `[A-Za-z0-9._/-]` is 404. Match against fixed regexes (below). Unknown path
   → 404 `{"error": "not found"}`. Known path, wrong method → 405 with `Allow`.
2. `/healthz` (GET): `200 {"ok": true}`, no auth, no Host check.
3. `/v2/*`: **no Host header check** (bearer auth, not cookies). Parse
   `Authorization`: exactly one header, `Bearer <token>` (scheme
   case-insensitive). Missing, malformed, unknown or revoked → 401
   `{"error": "unauthorized"}` with `WWW-Authenticate: Bearer`, and
   `close_connection = True` when a body was announced. Both scopes are
   accepted on every `/v2` endpoint. Then `keys.touch(record)`.
4. `{id}` must pass `layout.valid_run_id` and `{path}` must be on the
   endpoint's allow-list, else 404, before any filesystem access.
5. Body: `Transfer-Encoding` present or `Content-Length` missing /
   duplicated / non-ASCII-digit → 411; over the endpoint limit → 413. Both
   close the connection without reading the body. Then
   `server._admit_body(connection, 8)`; full → 503 with `Retry-After: 5`,
   connection closed.
6. Read the body with a deadline (pattern of `_read_put_body`, deadline from
   the table above). Small bodies (atomic files, `missing`, commit) are read
   into memory. Append and object bodies are **streamed to a temp file** in
   `<data>/server/tmp` (via `layout.unique_tmp`), hashing objects as they
   stream. Timeout or short body → 408 (or just close), temp file removed.
7. Only then take the per-run lock (or `objects_lock`) and apply. The lock is
   never held while reading from the network.

Routes (raw-path regexes; `<id>` is `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, also
checked with `valid_run_id`):

| Method and path | Notes |
| --- | --- |
| `GET /healthz` | see above |
| `GET /v2/whoami` | `{"key": name, "scope": scope, "protocol": 2, "features": []}` (`scope` is an addition to section 4.2, for packages 3 and 4b) |
| `POST /v2/objects/missing` | JSON `{"objects": [...]}`, list of ≤ 10,000 strings each passing `valid_sha`, else 422. Duplicates allowed. Returns `{"missing": [...]}` in input order, de-duplicated, using `objects.has` |
| `PUT /v2/objects/<sha>` | stream to tmp + SHA-256. Mismatch → 422 `{"error": "hash mismatch", "sha256": "<actual>"}`. Under `objects_lock`: if `objects.has` → refresh mtime (`os.utime`, no follow) and 200 `{"sha256": sha, "created": false}`; else fsync tmp, `mkdir` `objects/<hh>` (checked with `layout.object_path`), `os.replace`, fsync dir, 201 `{"sha256": sha, "created": true}`. Never write through a symlink at the destination: if the destination is a symlink or non-regular file, 500 and log; do not replace it |
| `GET /v2/runs/<id>/state` | 404 when the run is unknown (no `runs/<id>/meta.json`). Otherwise `{"owner": <name or null>, "files": {path: {"size": n, "sha256": h}}, "remote": <remote.json object or {}>}` over every allow-listed file present as a regular file (no-follow). Readable by any valid key |
| `PUT /v2/runs/<id>/files/<path>` | atomic allow-list; see 4.3 and validation below |
| `POST /v2/runs/<id>/append/<path>?offset=N[&truncate=1]` | append allow-list; see appends below |
| `POST /v2/runs/<id>/commit` | see commit below |

Allow-lists (exact regexes on `<path>`):

- Atomic: `meta.json`, `scalars/index.json`, `media/index.json`,
  `scalars/(0|[1-9][0-9]{0,8})\.ovw`.
- Append: `events.jsonl`, `scalars/_time.bin`, `scalars/(0|[1-9][0-9]{0,8})\.bin`.
- Everything else (including `run.lock`, `sync.json`, `notes.md`,
  `remote.json`) is 404.

Run directory safety: resolve the run dir with `layout.run_dir(root, id)`. If
`runs/<id>`, `runs/<id>/scalars` or `runs/<id>/media` exists and is a symlink
or not a directory, answer 404 and log a warning. Create `scalars/` and
`media/` on demand with `mkdir` and re-check with `lstat`. Every open is
no-follow (`layout.open_nofollow` / `read_file_nofollow`); every target path
also goes through `layout.contained`.

**Ownership and creation (4.3).** Under the run lock:

- Run "exists" when `runs/<id>/meta.json` exists. `owner` comes from
  `runs/<id>/remote.json` (`{"owner": ...}`); a missing or unparsable
  `remote.json`, or one without a string `owner`, means unowned.
- `PUT meta.json` on an unknown or unowned run: validate, create the run dir,
  write `remote.json` = `{"owner": <key name>}` (atomic, fsync), then write
  `meta.json`. Later writes to an owned run by a different key name → 409
  `{"error": "run owned by another key", "owner": "<name>"}`.
- Any other write (atomic non-meta, append, commit) to an unknown run → 404
  `{"error": "unknown run"}`.
- Key names are the owner identity, so a new key created with a revoked key's
  name can write that key's runs. Document this in the `KeyStore.revoke`
  docstring.

**Atomic file validation.** `meta.json`: JSON object (UTF-8) whose `run_id`
equals `<id>` (the field `docs/FORMAT.md` defines; section 4.3 of the release
plan says `id`, which is a typo) and whose `status` is `running`, `finished`
or `crashed`, else 422. `scalars/index.json` and
`media/index.json`: a JSON object, else 422. `.ovw`: `Overview.from_bytes`
must not raise, else 422. Write with `layout.atomic_write(path, body,
fsync=True)`. Response `200 {"size": n}`.

**Appends (4.4).** Query: `offset` required, ASCII digits only, at most one
`offset` and at most one `truncate`; `truncate` only `1`; any other parameter
→ 422. With the body spooled to tmp and the run lock held:

- Whole records: `.bin` files need `offset % 8 == 0` and `len % 8 == 0`
  (`layout.RECORD_SIZE`; `_time.bin` uses the same 8-byte record);
  `events.jsonl` needs a non-empty body to end with `\n`. Otherwise 422
  `{"error": "torn record"}`.
- `truncate=1`: `offset` must be 0 (else 422). Allowed only when
  `remote.json` has no `status` or `status == "running"`; otherwise 409
  `{"error": "run is not running", "status": s}`. Copy the body into a temp
  file in the target directory (or `os.replace` from tmp, same device is
  guaranteed at startup), fsync, `os.replace` over the file. Response
  `{"size": len}`.
- Otherwise open the file no-follow for read+write (create if missing),
  `S = fstat().st_size`:
  - `offset > S` → 409 `{"size": S}`.
  - Compare the overlap `[offset, min(S, offset+len))` chunk by chunk against
    the body; any difference → 409 `{"size": S, "mismatch": true}`.
  - Append the bytes past `S` (from the spooled file, in chunks), fsync.
    On any error after writing started, `ftruncate` back to `S` and answer
    500. Response `{"size": new_size}`. A full replay writes nothing.

**Commit (4.6).** Body `{"status": s, "fingerprint": f, "sizes": {path: n}}`:
`s` in `running|finished|crashed`, `f` a 64-hex string, `sizes` an object of
allow-listed paths (atomic or append) to non-negative ints, else 422. Under
the run lock:

- If `s` differs from the server's `meta.json` `status` → 409
  `{"error": "status does not match meta.json", "status": <server status>}`
  (the client re-pushes `meta.json`). This rule is an addition to section 4.6.
- For each path in `sizes`, compare with the server file size (missing =
  `null`). Any difference → 409 `{"error": "size mismatch", "sizes": {path:
  server_size_or_null, ...}}` for every path in the request.
- Else write `remote.json` atomically (fsync) with `owner` (kept), `status`,
  `fingerprint`, `last_sync_at` (`format_timestamp()`), `sizes`; respond
  `{"complete": true, "fingerprint": f}`.

Errors are always JSON `{"error": ...}`. Unexpected exceptions → 500
`{"error": "internal error"}`, logged with the traceback when `verbose`; never
echo request bodies or headers. Never log the `Authorization` header.

### 5. CLI

```
agenticml server serve [--data D] [--host 127.0.0.1] [--port 8080] [--public-url URL]
agenticml server keys create --name N [--scope sync|admin] [--data D]
agenticml server keys list [--data D] [--json]
agenticml server keys revoke N [--data D]
```

- Follow the `cmd_ui` pattern (`cmd_server_serve`, `cmd_server_keys`): lazy
  imports, `KeyboardInterrupt` → `stopped`, 0; `OSError` / startup errors →
  one-line `agenticml server: ...` on stderr, exit 1.
- `keys create` prints the key alone on stdout and a note on stderr ("shown
  once; store it now"). `keys create`/`revoke` do not need the server lock and
  work while the server runs. `keys` commands create `<data>/server` (0700)
  if needed.
- `keys list` prints name, scope, prefix, created, created by, last used; never
  the hash. `--json` prints the `to_public()` list.
- `keys revoke` of an unknown name → exit 1 with a message.

## Constraints and conventions

- Standard library only in `src/agenticml/server/`. Match the code style of
  `ui/server.py` and `store/layout.py`: short docstrings that say why,
  `from __future__ import annotations`, Python 3.9 compatible (no `match`, no
  `X | Y` at runtime outside annotations).
- Do not import `requests` or anything from `client.py` / `sync.py` /
  `auth.py` in the server.
- Reuse `layout` helpers (`valid_run_id`, `valid_sha`, `contained`,
  `run_dir`, `object_path`, `open_nofollow`, `read_file_nofollow`,
  `atomic_write`, `unique_tmp`, `fsync_dir`, `format_timestamp`,
  `RECORD_SIZE`) and `objects.has`; do not duplicate them.
- Local `agenticml ui` behaviour must not change.
- The server never writes `run.lock`, `notes.md` or `sync.json`.

## Edge cases

- Same object uploaded concurrently twice: both succeed (one 201, one 200).
- Append to a run while another key owns it → 409 before touching the file.
- Two appends to the same file at once: serialized by the run lock; the second
  sees the first's size.
- A `.bin` on the server that is not a multiple of 8 (operator damage): the
  normal path still applies; the client recovers with `truncate=1`.
- Client disconnects mid-body: temp file removed, nothing applied.
- `keys.json` replaced by the CLI while a request is verifying: atomic replace
  means the server reads either version.
- `keys.json` edited by hand into invalid JSON: all file keys rejected (env key
  still works), error logged.
- Data dir on a filesystem without `flock`: refuse to start.
- Revoked key mid-sync: the next request (after the mtime reload) is 401.
- `GET /state` on a run with a symlinked file: that file is skipped, not
  followed.

## Tests

`tests/test_server_keys.py`:

- create returns `aml_` + 43 URL-safe chars; `keys.json` is 0600, holds the
  hash and prefix but not the key; duplicate and invalid names and `env`
  rejected; bad scope rejected.
- verify: right key, wrong key, revoked key; env key valid with scope admin;
  invalid env key value refused by `serve` (via the CLI or `make_server`).
- reload: a second `KeyStore` (or the CLI) creates/revokes, the first sees it
  without restart; corrupt `keys.json` fails closed and recovers once fixed.
- `touch` throttles to once a minute (monkeypatch the clock) and does not
  resurrect a key revoked between verify and touch.
- CLI: `keys create/list/revoke` (list never prints the hash; `--json`),
  revoke unknown name exits 1, and create/revoke work while a server holds
  `server.lock`.

`tests/test_server_ingest.py` (server in a thread on port 0, raw
`http.client` requests):

- `/healthz` without auth; `/v2/whoami` with missing, malformed, wrong,
  revoked (revoked through `KeyStore` while running) and env keys; both scopes
  accepted; zero-key server starts and rejects everything but `/healthz`.
- Default bind is `127.0.0.1` (CLI parser default and `make_server`).
- Second `make_server` on the same data dir is refused; tmp dir emptied at
  start; non-`/v2` paths are 404.
- Path allow-list and traversal: `..`, absolute, `%2e%2e`, `%2f`, backslash,
  `run.lock`, `notes.md`, `sync.json`, `remote.json`, invalid run id, and a
  symlinked `runs/<id>` (pointing inside and outside the store) → 404, with
  no file created outside the run dir.
- Oversize bodies → 413 without reading (limits lowered in the test);
  missing `Content-Length` and chunked → 411; slow body hits the deadline.
- Torn records (`.bin` length or offset not a multiple of 8, `events.jsonl`
  without trailing `\n`) → 422 and the file is unchanged.
- Append replay is a no-op; offset gap → 409 with size; overlapping mismatch →
  409 with `mismatch`; partial overlap appends only the new tail.
- `truncate=1` works while running (and before the first commit), is 409 after
  a `finished` commit, and needs `offset=0`.
- Ownership: key B writing key A's run → 409; first `PUT meta.json` with a
  mismatched `id` → 422 and no run dir created; writes to an unknown run → 404.
- Objects: `missing` (validation, de-dupe, 10,001 hashes → 422), upload 201
  then 200, hash mismatch 422 with nothing stored.
- Commit: size mismatch 409 with server sizes; status mismatch 409; success
  writes `remote.json` with owner, status, fingerprint, `last_sync_at`, sizes.
- End to end: build a real run with `RunWriter` (scalars over several series,
  events, media, an artifact or code object, finished), push it through raw
  HTTP in the 4.5 order, then `RunReader` on `<data>/store` matches the source
  (meta, keys, series values, events, media index), every pushed file is
  byte-identical, and `run_fingerprint` on both stores is equal.
- CLI: `agenticml server serve` starts (subprocess or `main()` in a thread on
  port 0 is acceptable; at least one test goes through `cli.main`).
- Local UI regression: existing `tests/test_ui.py` and `tests/test_ui_limits.py`
  pass unchanged.

## Checks (coder, before handing back)

1. `testbed/.venv/bin/python -m pytest -q tests` from the repo root (all
   green; report counts).
2. `testbed/.venv/bin/python -m pytest -q tests/test_server_keys.py
   tests/test_server_ingest.py tests/test_ui.py tests/test_ui_limits.py`.
3. The new tests pass on Python 3.9 too if a 3.9 interpreter is available
   (`python3.9 -m pytest ...` with the package importable); report if not.
4. Manual: `agenticml server keys create --name t --data <scratch>`, then
   `agenticml server serve --data <scratch> --port 0` (or a fixed port), and
   `curl -H "Authorization: Bearer <key>" http://127.0.0.1:<port>/v2/whoami`.
5. Report the diff summary, test counts and any deviation from this plan.

## Done when

- The tests above pass in the full suite on Linux (CI on the PR is green).
- `agenticml server serve` starts from the CLI, accepts a key created by
  `agenticml server keys create`, and serves `/healthz` and `/v2/whoami`.
- Sonnet's review and Cursor's second read-only review (†) have no open
  findings, and the roadmap row says `in review` in the PR.

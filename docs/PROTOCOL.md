# AgenticML sync protocol v2

This document specifies protocol v2 for replicating an AgenticML v1 store
([FORMAT.md](FORMAT.md)) from a client (`agenticml sync`) to an `agenticml
server`.

## Overview

Protocol v2 copies the local v1 store byte for byte to a server. The server
stores incoming files in a standard v1 layout without translating schemas or
re-encoding records.

- **Transport**: HTTP/1.1 over a single TCP port.
- **TLS**: In production, the server runs behind a TLS-terminating reverse proxy
  (such as an ALB, Caddy, or Tailscale Serve). The client enforces HTTPS for
  non-local hosts unless `--insecure` is explicitly passed.
- **Protocol number**: `2`.
- **Features**: Advertised by the server in `GET /v2/whoami`. The `lineage`
  feature enables shadow Git replication when Git is installed on the server.
  The `byref` feature enables `sync --artifacts` uploads of unchanged
  `copy=False` artifacts. A 2.0 server returns an empty list (`[]`). Clients
  must only enable features that the server explicitly advertises.

## Authentication

Every endpoint except `GET /healthz` requires an HTTP `Authorization` header:

```http
Authorization: Bearer <key>
```

- **Key format**: The prefix `aml_` followed by 43 URL-safe base64 characters
  (32 random bytes, e.g. `aml_abc123...`).
- **Scopes and roles**: Keys belong to users and are issued with either
  `sync` or `admin` scope. A key whose user has role `viewer` can only call
  `GET /v2/whoami`; every other `/v2` endpoint returns `403 Forbidden`
  (`{"error": "role viewer cannot sync"}`). For users with role `member` or
  `admin`, both scopes are accepted on all `/v2` endpoints. (`sync` keys can
  only access `/v2`; `admin` keys can additionally sign in to the server
  dashboard and manage keys and users).
- **Verification**: The server hashes incoming bearer keys with SHA-256 and
  compares digests against its key store in constant time
  (`hmac.compare_digest`).
- **Rejection**: If the `Authorization` header is missing, malformed, unknown,
  or revoked, the server returns `401 Unauthorized` with header
  `WWW-Authenticate: Bearer` and body `{"error": "unauthorized"}`.
- **Privacy**: Key values are never logged and never reflected in error messages
  or responses.

## General rules

### Run ID validation

The `{id}` parameter in `/v2/runs/{id}/...` must match `layout.valid_run_id`:

```regex
^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$
```

Invalid IDs return `404 Not Found` before any filesystem access.

### Path allow-lists

File paths in URLs must match strict allow-lists before any filesystem access:

- **Atomic files** (`PUT /v2/runs/{id}/files/{path}`):
  - `meta.json`
  - `artifacts.json`
  - `scalars/index.json`
  - `media/index.json`
  - `scalars/<n>.ovw` where `<n>` is `0` or `[1-9][0-9]{0,8}` (decimal series ID)
- **Append files** (`POST /v2/runs/{id}/append/{path}`):
  - `events.jsonl`
  - `scalars/_time.bin`
  - `scalars/<n>.bin` where `<n>` is `0` or `[1-9][0-9]{0,8}`
- **Never accepted**: `run.lock`, `sync.json`, `sync.lock`, `notes.md`,
  `remote.json`, or any path containing `..`, `.`, `%`, `\`, or `//`. Unmatched
  paths return `404 Not Found`.

### Filesystem safety

Run directory paths are verified to remain contained inside `<data>/store/runs/`.
If `runs/<id>`, `runs/<id>/scalars`, or `runs/<id>/media` exists as a symlink or
non-directory, the server answers `404 Not Found` and logs a warning. All file
opens use `O_NOFOLLOW`. Hard-linked files are refused with `409 Conflict`.

### Error format

Errors return a JSON object with an `"error"` message:

```json
{"error": "message"}
```

Conflict (`409`) and validation (`422`) bodies include extra fields to guide
client recovery.

### HTTP status codes

| Status | Meaning | Reason |
| --- | --- | --- |
| `200 OK` | Success | Request completed successfully. |
| `201 Created` | Created | New content-addressed object stored (`PUT /v2/objects/{sha256}`). |
| `401 Unauthorized` | Auth failed | Missing, invalid, or revoked bearer token (`WWW-Authenticate: Bearer`). |
| `403 Forbidden` | Forbidden | Calling key belongs to a `viewer` user (`{"error": "role viewer cannot sync"}`). |
| `404 Not Found` | Not found | Unknown route, invalid run ID, unallowed path, or unknown run on state/append/commit/non-meta PUT. |
| `405 Method Not Allowed` | Method rejected | Method not allowed for endpoint (`Allow` header returned). |
| `408 Request Timeout` | Timeout | Request body read exceeded deadline or client disconnected. |
| `409 Conflict` | Conflict | State conflict: run owned by another user, append offset mismatch, append overlap mismatch, run not running, commit status mismatch, or commit size mismatch. |
| `411 Length Required` | Missing length | `Content-Length` missing, non-digit, duplicated, or `Transfer-Encoding` present. |
| `413 Payload Too Large` | Oversized body | Declared body length exceeds endpoint limit. |
| `422 Unprocessable Entity` | Validation error | Malformed JSON, torn record, object hash mismatch, or invalid query parameters. |
| `503 Service Unavailable` | Server busy | Request body admission capacity reached (`Retry-After: 5` header returned). |
| `500 Internal Error` | Internal error | Unexpected server failure (`{"error": "internal error"}`). |

### Default limits

| Resource | Default limit |
| --- | --- |
| Atomic file body | 16 MiB (16,777,216 bytes) |
| Append body | 64 MiB (67,108,864 bytes) |
| Lineage bundle body | 256 MiB (268,435,456 bytes) |
| Object body | 4 GiB (4,294,967,296 bytes) |
| Missing hashes per call | 10,000 hashes (body cap 1 MiB) |
| Commit body | 1 MiB (1,048,576 bytes) |
| Concurrent request bodies | 8 |
| Body read deadline | 30 s + 1 s per 512 KiB declared |

## Endpoints

| Method and path | Request body | Success response | Error codes |
| --- | --- | --- | --- |
| `GET /healthz` | None | `200 {"ok": true}` | None |
| `GET /v2/whoami` | None | `200 {"key": name, "scope": scope, "user": user, "role": role, "protocol": 2, "features": ["lineage", "byref"]}` | 401 |
| `POST /v2/objects/missing` | `{"objects": [sha256, ...]}` | `200 {"missing": [sha256, ...]}` | 401, 403, 411, 413, 422, 503 |
| `PUT /v2/objects/{sha256}` | Raw bytes (`application/octet-stream`) | `201 {"sha256": sha256, "created": true}` or `200 {"sha256": sha256, "created": false}` | 401, 403, 411, 413, 422, 503 |
| `GET /v2/runs/{id}/state` | None | `200 {"owner": name, "owner_user": user, "files": {path: {"size": n, "sha256": h}}, "remote": {...}}` | 401, 403, 404 |
| `PUT /v2/runs/{id}/files/{path}` | Raw bytes | `200 {"size": n}` | 401, 403, 404, 409, 411, 413, 422, 503 |
| `POST /v2/runs/{id}/append/{path}?offset=N[&truncate=1]` | Raw bytes | `200 {"size": n}` | 401, 403, 404, 409, 411, 413, 422, 503 |
| `GET /v2/runs/{id}/lineage` | None | `200 {"commit": sha_or_null, "tips": [sha, ...]}` | 401, 403, 404, 409, 422, 503 |
| `PUT /v2/runs/{id}/lineage` | Git bundle | `201 {"commit": sha, "created": true}` or `200 {"commit": sha, "created": false}` | 401, 403, 404, 409, 411, 413, 422, 503 |
| `POST /v2/runs/{id}/commit` | `{"status": s, "fingerprint": f, "sizes": {path: n}}` | `200 {"complete": true, "fingerprint": f}` | 401, 403, 404, 409, 411, 413, 422, 503 |

### `GET /healthz`

Unauthenticated health check.

- **Request**: `GET /healthz` (no auth header, no Host header check).
- **Response**: `200 OK`
  ```json
  {"ok": true}
  ```

### `GET /v2/whoami`

Identify the calling API key, owning user, role, and server capabilities.

- **Request**: `GET /v2/whoami`
  - Header: `Authorization: Bearer <key>`
- **Response**: `200 OK`
  ```json
  {
    "key": "gpu-box-1",
    "scope": "sync",
    "user": "alice",
    "role": "member",
    "protocol": 2,
    "features": ["lineage", "byref"]
  }
  ```
- **Errors**:
  - `401 Unauthorized`: `{"error": "unauthorized"}` with `WWW-Authenticate: Bearer`.

The `user` and `role` fields identify the key's owner and assigned role. A key
belonging to a user with role `viewer` can call `whoami`, but every other `/v2`
route returns `403 Forbidden` (`{"error": "role viewer cannot sync"}`).

The `lineage` feature is present only when Git was found at server startup;
without Git, lineage is omitted and the lineage endpoints return 404. The
`byref` feature is always listed.

### `GET /v2/runs/{id}/lineage`

Returns `200 {"commit": "<sha>", "tips": ["<sha>", ...]}`. `commit`
is the server's `refs/runs/{id}` tip, or JSON `null` if absent. `tips` lists
distinct tips of `refs/runs/*` in the same project, newest by committer date
first, capped at 10,000 refs. Only complete ref tips are advertised as bundle
prerequisites. The project comes from the server's `meta.json.lineage`.

Both lineage endpoints require bearer authentication and run ownership:
401 for an invalid key, 403 for viewer keys, 404 for an unknown run, 409 for another user's run.
A missing or invalid lineage block returns 422 `{"error": "run has no lineage"}`;
its project key must match the lineage key format and the key derived from
the string `meta.json.project`, and its commit must be 40 lowercase
hexadecimal characters. A server without Git omits the `lineage` feature
and returns 404 on both lineage routes.

### `PUT /v2/runs/{id}/lineage`

Uploads a raw Git v2 or v3 bundle (`application/octet-stream`), requiring
`Content-Length` and limited to 256 MiB (411 if missing, 413 if too large).
The header is limited to 1 MiB and must advertise exactly one ref:
`<meta.json.lineage.commit> refs/runs/{id}`. Only the v3 capability
`@object-format=sha1` is accepted. Prerequisite commit lines are allowed.

The server validates the header and verifies prerequisites, then checks the
streamed pack with `git index-pack --stdin --fix-thin --strict` in a temporary
object quarantine under `server/tmp`, using the project's live objects only
as alternates through a single absolute path in the quarantine's
`info/alternates` file (data paths containing `:` are supported).
Connectivity and commit type are checked in that environment
before pack files are moved into the live repo and the run ref is atomically
created. Hooks are disabled; Git uses a fixed configuration, no inherited
`GIT_*` overrides, no system/global configuration, and the C locale.
Git subprocess timeouts are 60 s plus the body size divided by 4 MiB/s.
No checkout is made, and existing refs are never moved or deleted.

- Success: 201 `{"commit": "<sha>", "created": true}`.
- Replay with the same server ref: 200 `{"commit": "<sha>", "created": false}`.
- Existing differing ref: 409 `{"error": "lineage ref differs", "commit": "<server-sha>"}`.
- Malformed or hostile bundle: 422 `{"error": "invalid bundle"}`.
- Missing prerequisite: 422 `{"error": "missing prerequisite"}`; resend a full bundle.
- Body or receive concurrency exhaustion: 503 `{"error": "server busy"}`,
  `Retry-After: 5`. At most two lineage receives run after body upload.
- Repository initialization or ref publication failure, Git timeout, or
  filesystem failure: 503 `{"error": "lineage busy"}`,
  `Retry-After: 5`; retry without remembering a permanent skip. This also
  applies to lineage state reads.

Rejected bundles leave existing refs and live objects unchanged. If publication
fails, rollback deletes only a ref published by that request, using its expected
commit as a compare-and-swap guard. Promoted packs are removed unless the ref
may still be published and deletion did not succeed; retaining those packs
keeps any live ref's objects available. Uploaded temporary files and quarantines
are removed after both successful and failed requests. Startup also removes
abandoned `lineage-*` quarantine directories from `server/tmp` after a crash.

### `POST /v2/objects/missing`

Query which content-addressed objects are missing on the server.

- **Request**: `POST /v2/objects/missing`
  - Header: `Authorization: Bearer <key>`, `Content-Type: application/json`, `Content-Length: <size>`
  - Body: JSON object containing a list of at most 10,000 SHA-256 strings:
    ```json
    {
      "objects": [
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
      ]
    }
    ```
- **Response**: `200 OK`
  ```json
  {
    "missing": [
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ]
  }
  ```
  Returns missing digests de-duplicated and in input order.
- **Errors**:
  - `401 Unauthorized`: `{"error": "unauthorized"}`
  - `411 Length Required`: `{"error": "length required"}`
  - `413 Payload Too Large`: `{"error": "payload too large"}`
  - `422 Unprocessable Entity`: `{"error": "invalid objects"}` (if `objects` is not a list, exceeds 10,000 items, or contains invalid SHA-256 strings)
  - `503 Service Unavailable`: `{"error": "server busy"}` (`Retry-After: 5`)

### `PUT /v2/objects/{sha256}`

Upload a single content-addressed object.

- **Request**: `PUT /v2/objects/{sha256}`
  - `{sha256}` must be a 64-character lowercase hexadecimal string.
  - Header: `Authorization: Bearer <key>`, `Content-Type: application/octet-stream`, `Content-Length: <size>`
  - Body: raw bytes up to 4 GiB.
- **Processing**:
  - Bytes stream to `<data>/server/tmp/` while hashing with SHA-256.
  - If the computed digest does not match `{sha256}`, the temp file is deleted and the server returns `422 Unprocessable Entity`.
  - If the object already exists under `objects/<hh>/<sha256>`, its modification time is refreshed (`os.utime`) and the server returns `200 OK`:
    ```json
    {"sha256": "e3b0...", "created": false}
    ```
  - If the object is new, the temp file is fsynced, permissions set, moved into place (`os.replace`), its parent directory fsynced, and the server returns `201 Created`:
    ```json
    {"sha256": "e3b0...", "created": true}
    ```
- **Errors**:
  - `401 Unauthorized`: `{"error": "unauthorized"}`
  - `404 Not Found`: invalid SHA format in path
  - `411 Length Required`: `{"error": "length required"}`
  - `413 Payload Too Large`: `{"error": "payload too large"}`
  - `422 Unprocessable Entity`: `{"error": "hash mismatch", "sha256": "<actual>"}`
  - `503 Service Unavailable`: `{"error": "server busy"}` (`Retry-After: 5`)

### `GET /v2/runs/{id}/state`

Retrieve current file sizes, hashes, and sync metadata for a run. Readable with
any valid sync or admin key.

- **Request**: `GET /v2/runs/{id}/state`
  - Header: `Authorization: Bearer <key>`
- **Response**: `200 OK`
  ```json
  {
    "owner": "gpu-box-1",
    "owner_user": "alice",
    "files": {
      "meta.json": {"size": 812, "sha256": "a1b2..."},
      "events.jsonl": {"size": 40960, "sha256": "c3d4..."},
      "scalars/_time.bin": {"size": 80, "sha256": "e5f6..."},
      "scalars/0.bin": {"size": 8000, "sha256": "0718..."}
    },
    "remote": {
      "owner": "gpu-box-1",
      "owner_user": "alice",
      "owner_key": "gpu-box-1",
      "last_sync_key": "gpu-box-1",
      "status": "running",
      "fingerprint": "9abc...",
      "last_sync_at": "2026-11-04T05:00:00Z",
      "sizes": {"events.jsonl": 40960, "scalars/0.bin": 8000}
    }
  }
  ```
  - **Compatibility rule**: To preserve compatibility with 2.0 and 2.1 clients,
    the `owner` field in `state`, `state.remote`, and 409 conflict payloads
    returns the **calling key's name** if the caller's user owns the run. If
    the run is owned by another user, `owner` returns the owning user's name.
  - `owner_user` always carries the real user name of the owner, both at the
    top level and inside `remote`.
  - `owner_key` (the key that created the run) and `last_sync_key` (the key of
    the last commit) in `remote` are visible only to keys belonging to the
    owning user; they are omitted for callers of other users.
  - `files` lists all allow-listed files present as regular files.
- **Errors**:
  - `401 Unauthorized`: `{"error": "unauthorized"}`
  - `403 Forbidden`: `{"error": "role viewer cannot sync"}`
  - `404 Not Found`: unknown run (`runs/<id>/meta.json` does not exist) or
    invalid run ID.

### `PUT /v2/runs/{id}/files/{path}`

Atomically write or replace an allow-listed atomic file.

- **Request**: `PUT /v2/runs/{id}/files/{path}`
  - `{path}` must match: `meta.json`, `artifacts.json`, `scalars/index.json`,
    `media/index.json`, or `scalars/<n>.ovw`.
  - Header: `Authorization: Bearer <key>`, `Content-Length: <size>`
  - Body: raw bytes up to 16 MiB.
- **Validation**:
  - `meta.json`: JSON object where `run_id == "{id}"` and `status` is one of
    `"running"`, `"finished"`, or `"crashed"`.
  - `artifacts.json`: JSON object `{"version": 1, "artifacts": [...]}` with at
    most 10,000 unique `{name, version, sha256, size}` rows. See
    [FORMAT.md](FORMAT.md#artifactsjson).
  - `scalars/index.json`, `media/index.json`: valid JSON object.
  - `<n>.ovw`: binary overview parseable by `Overview.from_bytes`.
- **Response**: `200 OK`
  ```json
  {"size": 812}
  ```
- **Errors**:
  - `401 Unauthorized`: `{"error": "unauthorized"}`
  - `403 Forbidden`: `{"error": "role viewer cannot sync"}`
  - `404 Not Found`: disallowed path, or unknown run for non-meta files
    (`{"error": "unknown run"}`).
  - `409 Conflict`: `{"error": "run owned by another user", "owner": "<name>", "owner_user": "<user>"}`
  - `411 Length Required`: `{"error": "length required"}`
  - `413 Payload Too Large`: `{"error": "payload too large"}`
  - `422 Unprocessable Entity`: `{"error": "invalid meta.json"}`,
    `{"error": "invalid artifacts.json"}`, `{"error": "invalid overview"}`,
    or `{"error": "invalid JSON object"}`.
  - `503 Service Unavailable`: `{"error": "server busy"}` (`Retry-After: 5`)

### `POST /v2/runs/{id}/append/{path}?offset=N[&truncate=1]`

Append records to an allow-listed append file, or atomically replace it when
`truncate=1`.

- **Request**: `POST /v2/runs/{id}/append/{path}?offset=N[&truncate=1]`
  - `{path}` must match: `events.jsonl`, `scalars/_time.bin`, or
    `scalars/<n>.bin`.
  - Query parameters:
    - `offset`: required decimal integer.
    - `truncate`: optional; if present, must be `1` and requires `offset=0`.
    - No other query parameters are allowed.
  - Header: `Authorization: Bearer <key>`, `Content-Length: <size>`
  - Body: raw bytes up to 64 MiB.
- **Validation**:
  - For `.bin` files: both `offset` and body `length` must be multiples of 8
    (`layout.RECORD_SIZE`).
  - For `events.jsonl`: non-empty bodies must end with `\n`.
  - Any torn record returns `422 Unprocessable Entity` (`{"error": "torn record"}`).
- **Response**: `200 OK`
  ```json
  {"size": 8000}
  ```
- **Errors**:
  - `401 Unauthorized`: `{"error": "unauthorized"}`
  - `403 Forbidden`: `{"error": "role viewer cannot sync"}`
  - `404 Not Found`: disallowed path or unknown run.
  - `409 Conflict`:
    - `{"error": "run owned by another user", "owner": "<name>", "owner_user": "<user>"}`
    - `{"size": S}` (gap: `offset > S`)
    - `{"size": S, "mismatch": true}` (overlapping bytes disagree)
    - `{"error": "run is not running", "status": "<status>"}` (`truncate=1` attempted
      when run status is `finished` or `crashed`)
    - `{"error": "refusing hard-linked file on server", "path": "<path>"}`
  - `411 Length Required`: `{"error": "length required"}`
  - `413 Payload Too Large`: `{"error": "payload too large"}`
  - `422 Unprocessable Entity`: `{"error": "invalid append query"}`,
    `{"error": "truncate requires offset=0"}`, or `{"error": "torn record"}`.
  - `503 Service Unavailable`: `{"error": "server busy"}` (`Retry-After: 5`)

### `POST /v2/runs/{id}/commit`

Finalize or checkpoint a run, verifying that all pushed files match the client's
sizes before updating `remote.json`.

- **Request**: `POST /v2/runs/{id}/commit`
  - Header: `Authorization: Bearer <key>`, `Content-Type: application/json`,
    `Content-Length: <size>`
  - Body: JSON object up to 1 MiB:
    ```json
    {
      "status": "running",
      "fingerprint": "9abc456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "sizes": {
        "meta.json": 812,
        "events.jsonl": 40960,
        "scalars/_time.bin": 80,
        "scalars/0.bin": 8000
      }
    }
    ```
- **Validation**:
  - `status` must be `"running"`, `"finished"`, or `"crashed"`.
  - `fingerprint` must be a valid 64-hex SHA-256 string.
  - `sizes` must be an object mapping allow-listed paths (atomic or append) to
    non-negative integers.
  - `status` must match the server's current `meta.json` status. If it differs,
    the server returns `409 Conflict` (`{"error": "status does not match meta.json", "status": "<server_status>"}`).
  - Each path in `sizes` must equal the server's filesystem size (missing = `null`).
    If any size differs, the server returns `409 Conflict` (`{"error": "size mismatch", "sizes": {...}}`).
- **Response**: `200 OK`
  ```json
  {
    "complete": true,
    "fingerprint": "9abc456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
  ```
- **Errors**:
  - `401 Unauthorized`: `{"error": "unauthorized"}`
  - `403 Forbidden`: `{"error": "role viewer cannot sync"}`
  - `404 Not Found`: unknown run (`{"error": "unknown run"}`).
  - `409 Conflict`:
    - `{"error": "run owned by another user", "owner": "<name>", "owner_user": "<user>"}`
    - `{"error": "status does not match meta.json", "status": "<server_status>"}`
    - `{"error": "size mismatch", "sizes": {path: server_size_or_null, ...}}`
  - `411 Length Required`: `{"error": "length required"}`
  - `413 Payload Too Large`: `{"error": "payload too large"}`
  - `422 Unprocessable Entity`: `{"error": "invalid commit"}`
  - `503 Service Unavailable`: `{"error": "server busy"}` (`Retry-After: 5`)

## Run creation and ownership

A run does not exist on the server until `meta.json` is created:

1. **First write**: The first write to an unknown run must be
   `PUT /v2/runs/{id}/files/meta.json`.
   - The server creates directory `<data>/store/runs/{id}/`.
   - It claims ownership for the authenticated key's user by writing
     `remote.json` containing:
     ```json
     {
       "owner": "alice",
       "owner_key": "gpu-box-1"
     }
     ```
     `owner` is the calling key's user name; `owner_key` records the key name
     that created the run.
   - It then atomically writes `meta.json`.
2. **Ownership guard**: Runs belong to users, not keys. Any valid key belonging
   to the owning user can write to the run (PUT, append, or commit). If a key
   belonging to another user attempts to write to the run, the server returns:
   ```json
   {"error": "run owned by another user", "owner": "alice", "owner_user": "alice"}
   ```
   with HTTP status `409 Conflict`. (Under the compatibility rule, `owner` is
   the owning user's name because the caller belongs to a different user).
3. **Commit attribution**: When committing (`POST /v2/runs/{id}/commit`), the
   server records `last_sync_key` (the key that performed the commit) alongside
   `last_sync_at` in `remote.json`.
4. **Validation**:
   - `meta.json` must be a valid UTF-8 JSON object.
   - Its `run_id` field must equal the `{id}` in the URL.
   - Its `status` must be `"running"`, `"finished"`, or `"crashed"`.
   - Any other write to an unknown run returns `404 Not Found` (`{"error": "unknown run"}`).

## Appends and rollback

### Append offset rules

An append request provides `offset` and data of length `L`. The server's current
file size is `S`:

- **Offset gap (`offset > S`)**: Returns `409 Conflict` with `{"size": S}`. The
  client resumes from `S`.
- **Overlap check (`offset <= S`)**: Bytes in `[offset, min(S, offset + L))`
  must match the server's existing bytes chunk by chunk.
  - If any byte differs: returns `409 Conflict` with `{"size": S, "mismatch": true}`.
  - If overlap matches: bytes past `S` are appended and fsynced.
- **Replay**: If `offset + L <= S` and all bytes match, no new bytes are written.
  The server returns `200 OK` with `{"size": S}`. A replayed append is a safe
  no-op.

### Whole records

The server enforces whole-record boundaries:

- `.bin` files (`scalars/<n>.bin`, `scalars/_time.bin`): `offset % 8 == 0` and
  `L % 8 == 0`.
- `events.jsonl`: non-empty payload must end with `\n`.
- Any torn record returns `422 Unprocessable Entity` (`{"error": "torn record"}`).

### Rollback and `truncate=1`

If a local training run hits an I/O error or abnormal exit during an uncommitted
scalar flush, `write_scalars` truncates touched `.bin` files back to their
pre-batch sizes ([FORMAT.md](FORMAT.md)). If `agenticml sync` read during that
batch, the server may hold records that the local store rolled back.

To resolve this without leaving conflicts:

- When a local file is smaller than the saved cursor, or an append returns `409`
  with `"mismatch": true`, the client rewrites the file from `offset=0` with
  `truncate=1`.
- `truncate=1` replaces the server file atomically with the request body (temp
  file + fsync + `os.replace`).
- `truncate=1` is accepted only while the run's remote status is `"running"` or
  unset. If the run is `"finished"` or `"crashed"`, it returns `409 Conflict`
  (`{"error": "run is not running", "status": "<status>"}`).

## Commit and `remote.json`

The commit request acts as a consistency barrier across all pushed files:

```json
{
  "status": "running" | "finished" | "crashed",
  "fingerprint": "<hex-sha256>",
  "sizes": {
    "meta.json": 812,
    "events.jsonl": 40960,
    "scalars/0.bin": 8000
  }
}
```

1. **Status check**: `status` must match the server's `meta.json` status. If it
   disagrees, the server returns `409 Conflict` with the server's status, and the
   client re-pushes `meta.json`.
2. **Size check**: The server verifies every path in `sizes` against its regular
   file size on disk. If any file size disagrees (or a file is missing), it
   returns `409 Conflict` with the server's actual sizes (`null` for missing files).
3. **Write**: On success, the server atomically writes `remote.json`:
   ```json
   {
     "owner": "alice",
     "owner_key": "gpu-box-1",
     "status": "running",
     "fingerprint": "...",
     "last_sync_at": "2026-11-04T05:00:00Z",
     "last_sync_key": "gpu-box-1",
     "sizes": {
       "meta.json": 812,
       "events.jsonl": 40960,
       "scalars/0.bin": 8000
     }
   }
   ```
   `owner` is the owning user name; `owner_key` is the key that created the
   run; `last_sync_key` is the key that made the commit.

### Server dashboard status

The server dashboard determines run status from `remote.json`:

| `remote.json` status | Last sync timestamp | Displayed status |
| --- | --- | --- |
| `finished` | Any | `finished` |
| `crashed` | Any | `crashed` |
| `running` | Age <= `AGENTICML_REMOTE_STALE_SECONDS` (default 600 s) | `running` |
| `running` | Age > `AGENTICML_REMOTE_STALE_SECONDS` | `stale` |
| Missing or unparseable | Any | Falls back to writer lock check (`run.lock`) |

## Client behaviour (`agenticml sync`)

The client synchronizes one run at a time under an advisory flock on
`<root>/runs/<id>/sync.lock`:

### 1. Push order for one run

1. **Objects**: Scan `media/index.json` snapshot bytes and `events.jsonl`
   snapshot bytes (up to its capped size) via `references_from_bytes`. Call
   `POST /v2/objects/missing` for all referenced digests (excluding permanently
   skipped objects). Upload each missing object with `PUT /v2/objects/{sha256}`.
   Because digests are extracted from the exact bytes being sent, every object
   is uploaded before any file referencing it.
2. **`meta.json`**: Push if the cursor hash differs or if the run has not been
   created on the server.
3. **Append files**: In fixed order: `events.jsonl`, `scalars/_time.bin`, then
   `scalars/<n>.bin` sorted by numeric `n`.
4. **Overviews and indexes**: `scalars/<n>.ovw` sorted by numeric `n`,
   `scalars/index.json`, `media/index.json`, whenever the snapshot SHA-256
   differs from the saved cursor.
5. **Lineage**: If the server advertises `lineage` and the local run has a
   valid snapshot matching its local ref, query `GET /lineage`. If the tip
   differs, upload a bundle excluding advertised tips available locally.
   Retry once with a full bundle on `missing prerequisite`. An empty bundle
   is rebuilt in full when a descendant tip already includes the run's
   commit. Other 422 errors and `lineage ref differs` are warned and
   remembered as permanent lineage skips; transient failures leave the run
   incomplete. A run completes only after its lineage is accepted or
   permanently skipped. Complete runs synced to 2.0 servers are backfilled
   when `lineage` becomes available. Dry runs count lineage as one file,
   assuming the server lists lineage, without querying the server.
6. **By-reference artifacts**: If the client passed `--artifacts` and the
   server advertises `byref`, hash each `copy=False` artifact whose source
   path, size, and mtime still match the event, upload missing objects, and
   PUT `artifacts.json`. A changed or missing source is warned once and
   remembered as a permanent `byref` skip. Without `--artifacts`, or when the
   server omits `byref`, these artifacts are ignored. Dry runs count pending
   candidates as objects without hashing or contacting the server.
7. **Commit**: The client commits (`POST /v2/runs/{id}/commit`) on every sync
   that is not idle (an idle run is complete, its fingerprint unchanged, and
   finished or crashed). `complete: true` is saved only after a successful
   commit, so a failed commit is retried on the next sync without resending
   files.

### 2. Snapshot capping

Before sending data, the client captures a consistent point-in-time snapshot of
the local files:

- **Atomic files**: Read completely using `read_file_nofollow`.
- **`.bin` files**: Capped to a multiple of 8 bytes (`size - size % RECORD_SIZE`).
- **`events.jsonl`**: Scanned backwards from the end to find the last `\n`. Any
  uncommitted trailing bytes after the last `\n` are excluded from the capped
  size.
- **`meta.json` crashed-run patch**: If local `meta.json` has `status: "running"`
  but the local run lock is free and its process has died (effective status
  `"crashed"`), the client patches the in-memory payload for `meta.json` to
  `status: "crashed"` before uploading. The local `meta.json` on disk is not
  modified.

### 3. Cursor state in `sync.json`

The client maintains host-scoped sync state in `<root>/runs/<id>/sync.json`:

```json
{
  "version": 1,
  "hosts": {
    "https://aml.example.com": {
      "protocol": 2,
      "owner": "gpu-box-1",
      "owner_user": "alice",
      "files": {
        "meta.json": {"size": 812, "sha256": "a1b2..."},
        "events.jsonl": {"size": 40960, "tail": "c3d4..."},
        "scalars/0.bin": {"size": 8000, "tail": "e5f6..."}
      },
      "complete": true,
      "source_hash": "9abc...",
      "skipped": [],
      "pruned": false
    }
  }
}
```

- **Atomic files**: Store `size` and `sha256` of accepted bytes.
- **Append files**: Store `size` (bytes on server) and `tail` (SHA-256 of the
  trailing 64 KiB window `[max(0, size - 65536), size)`). During append uploads,
  `tail` is computed from the bytes the server accepted (previous window plus the
  sent chunk), not by re-reading the local file.
- **Lineage**: `lineage` stores the accepted shadow commit. Permanent lineage
  skips use `{"kind": "lineage", "id": "<run-id>", "reason": "<error>"}` in
  `skipped`.
- **By-reference artifacts**: `byref` maps `"<name>:v<version>"` to
  `{path, size, mtime, sha256}` after a successful hash. Permanent skips use
  `{"kind": "byref", "id": "<name>:v<version>", "reason": "..."}` in
  `skipped`. The `artifacts.json` cursor lives under `files` like other
  atomic files.
- **Owner and user**: On servers that return `user` in `whoami`, the client
  compares ownership by user (`owner_user`), allowing a machine to switch to
  another key of the same user. It caches `owner_user` per host in `sync.json`.
  When syncing against a server that omits `user` (version 2.0 or 2.1), the
  client falls back to checking `owner` against its key name.
- **Update timing**: `sync.json` is atomically replaced and directory-fsynced
  after every accepted HTTP write request, ensuring interrupted syncs resume
  where they left off.
- **Compatibility**: Host entries without `"protocol": 2` (v1 entries) are
  treated as empty and replaced on first save. Entries for other hosts are
  retained untouched. `store.gc` reads `complete: true` and `source_hash == run_fingerprint()`.

### 4. Rollback detection and rewrite

Before appending, the client checks for local rollbacks:

- If the local capped file size is less than the saved cursor size, or if the
  local `tail` window no longer hashes to the cursor's `tail`:
  1. The client marks the file for rewrite.
  2. The first chunk is sent with `offset=0&truncate=1`.
  3. Subsequent chunks are sent as normal appends.
  4. The client logs: `rewrote {path} after a local rollback` and increments
     `result.rewritten`.
- If an append rewrite returns `409 Conflict` with `"run is not running"`, and
  the snapshot meta status is `"running"` (a resumed run that the server
  previously recorded as finished):
  1. The client sends `commit(status="running", fingerprint=source_hash, sizes={})`
     to flip the server run back to running.
  2. It then retries the rewrite once.

### 5. Cursor recovery via `GET /state`

When a host entry has no `files` map (new run, lost `sync.json`, after
`sync --reset RUN_ID`, or upgrading an older v1 host entry):

1. The client calls `GET /v2/runs/{id}/state`.
2. If the run is unknown on the server (`404 Not Found`), the client starts with
   empty cursors.
3. If `owner_user` is present in remote state and `whoami` returned `user`,
   the client checks that `owner_user` matches its user. Otherwise, if `owner`
   is set and does not match the client's key name, sync fails permanently:
   `run id {id} belongs to key {owner} on this server`. The owner and `owner_user`
   are saved in the host entry only after cursor recovery confirms ownership.
4. For each server file also present locally:
   - **Atomic**: Adopts the server's `size` and `sha256`.
   - **Append**: If the server size is at most the local capped size and the
     local prefix `[0, server size)` hashes to the server's `sha256`, the cursor
     is set to the server size and `tail` is computed locally. If the prefix
     differs, the file is marked for rewrite.
5. If an append returns `409 Conflict` with only `size` (offset mismatch without
   mismatch flag), the client runs recovery for that single file and retries
   once.

### 6. Conflict handling

- `409` with `"error": "run owned by another user"`: Fails permanently with an
  ownership error.
- `409` append with `"mismatch": true`: Triggers rewrite with `truncate=1`.
- `409` append with only `"size"`: Triggers cursor recovery for that file and
  retries once. A second conflict fails the run (transient).
- `409` commit with `"sizes"`: Runs cursor recovery on the conflicting files,
  pushes them, and retries commit once.
- `409` commit with `"status"`: Re-sends the `meta.json` snapshot and retries
  commit once.

### 7. Missing run recovery (404 during upload)

If the server answers `404 Not Found` with `{"error": "unknown run"}` to an
append, file PUT, or commit (for example, if the server was restored from an
older backup, or the run was deleted there):

- The client clears its local `files` cursor for that run in `sync.json`.
- It recovers remote state via `GET /v2/runs/{id}/state`.
- It re-sends `meta.json` (force=True) to recreate the run on the server and
  continues pushing the remaining files.
- This recovery runs at most once per sync pass. If a second 404 occurs in the
  same sync pass, the run fails with a transient `RunMissingError` (retried on
  the next sync pass).

### 8. Pruned runs

`agenticml sync --prune` drops raw `.bin` files for series that have an overview
(`.ovw` file):

- A missing local `.bin` file whose `.ovw` exists is pruned:
  - It is never sent over the network.
  - It is never marked for rewrite.
  - Its cursor in `sync.json` is preserved.
  - It is omitted from the commit `sizes` dictionary.
- **Upload guard**: If a run was pruned after syncing to another host, pushing to
  a new host fails permanently:
  `run was pruned after syncing to <host>; its full scalar history is no longer local`.
  Pushing is permitted only if the target host is listed in `pruned_hosts` or
  its host entry has `pruned: true`.

### 9. Retries and backoff

All protocol v2 operations are idempotent (PUTs replace, appends replay as
no-ops, commit overwrites `remote.json`).

- `Client` retries on `requests.ConnectionError`, `requests.Timeout`,
  `requests.exceptions.ChunkedEncodingError`, and HTTP status codes `500`,
  `502`, `503`, `504`.
- Default: up to 3 retries (4 attempts total).
- Exponential backoff: `min(0.5 * 2^attempt, 30.0)` seconds.
- Respects `Retry-After` header (in seconds, capped at 30 s) on HTTP `503`.
- For streamed bodies (`put_object` and `put_lineage`), `Client` rewinds the
  file handle (`handle.seek(start)`) before each retry attempt.
- Timeouts: 10 s connect timeout; 30 s read timeout for metadata calls; for
  `put_object` and `put_lineage`, read timeout scales to
  `max(30, 60 + size / 1 MiB)` seconds.

### 10. Permanent failures, `--watch`, and `--follow`

When running with `--watch N`, the client loops every N seconds until all
selected runs have synced, gone stale, or failed permanently. `sync --follow`
uses the same classification, looping until stopped (or until every explicit
run id is complete or permanent): HTTP 401 exits 3; connection errors,
timeouts, and 5xx/408/429 back off up to 5 minutes with jitter; a running run
is committed at least every 60 s.

- **Permanent failures** (stop `--watch` from retrying the run):
  - **Ownership conflict**: The run belongs to a different user on the
    server (`OwnershipError`).
  - **Pruned-run guard**: The run was pruned after syncing to another host, so
    its raw scalar history is no longer local.
  - **Unrewritable server copy**: `server copy is <status>; cannot rewrite <path>`
    when attempting a rewrite against a server run whose status is finished or
    crashed, and local status is also not running.
  - **Oversized event line**: An event line in `events.jsonl` exceeds 64 MiB.
  - **HTTP 4xx client errors**: Any 4xx HTTP response other than `408 Request
    Timeout` and `429 Too Many Requests` (and excluding a second 404
    `RunMissingError` during upload).
- **Transient failures** (retried on the next `--watch` pass):
  - Network errors, timeouts, and chunked encoding errors.
  - HTTP `408 Request Timeout`, `429 Too Many Requests`, and 5xx server errors
    (`500`, `502`, `503`, `504`).
  - A second 404 in the same sync pass (`RunMissingError`), allowing recovery on
    the subsequent pass.

### 11. Out of scope in protocol 2.0

- **Notes**: `notes.md` is never synced. Local notes remain local; notes
  edited in the server dashboard stay on the server.

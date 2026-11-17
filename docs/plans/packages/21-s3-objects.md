# Package 21: S3 object backend

Nov 9, 2026 · Supervisor plan · Release 3.0 · Coder: Codex · Size: M

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), "S3 objects
(package 21)", the configuration table (`--objects`), decision D7
(`[s3]` extra) and the package summary. Builds on
[18](18-workspaces.md) (per-workspace stores, `objects` table),
[18c](18c-signup-and-limits.md) (storage accounting) and
[20](20-many-processes.md) (cross-process object locks).

Not marked † in the release plan, but it touches cross-workspace access
to stored bytes, so Sonnet's review checks isolation explicitly.

## Goal

Objects (code snapshots, media, copied and by-reference artifacts) can
live in S3 instead of `<workspace store>/objects/`. Self-hosted keeps
today's layout and code path byte for byte (`file://`, the default). The
hosted service sets `--objects s3://bucket/prefix`. Run files and lineage
stay on disk (EFS in the hosted service).

Out of scope: run files on S3 (Later), pre-signed downloads (3.0 streams
every download through the server so access checks stay in one place),
`server status`/`verify` on S3 (package 23 adds them; 21 only makes them
not touch S3 and say so), Terraform (24).

## Design

### Backend interface (`src/agenticml/server/objectstore.py`, new)

```python
class ObjectBackend:
    name: str                       # "file" or "s3"
    def exists(self, workspace_id, sha) -> bool
    def missing(self, workspace_id, shas) -> list[str]   # order kept, no duplicates
    def put(self, workspace_id, sha, path, size) -> bool # True if created; never overwrites
    def open(self, workspace_id, sha)                    # binary stream (context manager), FileNotFoundError if absent
    def size(self, workspace_id, sha) -> int | None
    def delete(self, workspace_id, sha) -> None          # only used to undo a put whose DB record failed
    def delete_workspace(self, workspace_id) -> None     # delete_prefix
    def describe(self) -> str                            # "file" or "s3://bucket/prefix", never credentials
```

- Every method takes `workspace_id` first and validates it with
  `require_workspace_id`; `sha` is checked with `layout.valid_sha`. No
  method accepts a path or key from a caller.
- `FileObjects(data_dir)`: resolves `store_root(data_dir, workspace_id)`
  and uses the existing code unchanged: `objects.has`,
  `layout.object_path`, `_open_regular_object` (lstat, regular file only),
  the `_object` publish sequence in `ingest.py` (mkdir, fsync parent,
  `os.replace`, fsync dir, utime on existing). Move that sequence into
  `FileObjects.put` without changing its behaviour. `delete_workspace`
  is a no-op (the existing `delete_workspace` already removes the store
  directory).
- `S3Objects(bucket, prefix, client)`: key
  `<prefix>/<workspace_id>/objects/<sha[:2]>/<sha>` (prefix optional, no
  leading/trailing slash). The workspace id in the key is what keeps
  per-workspace dedupe and isolation; never share keys across
  workspaces.
  - `exists`/`size`: `head_object`; 404/NoSuchKey → absent; any other
    error propagates (becomes a 503 via the existing mapping, see below).
  - `missing`: `head_object` per hash with a small thread pool (8) —
    the request size is already capped by `limits.missing_hashes`.
  - `put`: called under the workspace object lock (thread + Postgres
    session lock from package 20), so `head_object` then `put_object` is
    race-free across processes. `put_object(Body=<open tmp file>,
    ContentLength=size, ChecksumSHA256=<base64 of the digest>,
    ServerSideEncryption=...)`: S3 recomputes SHA-256 and rejects a
    mismatch, so bytes that differ from the name are never stored. Also
    pass `IfNoneMatch="*"`; treat `PreconditionFailed` (412) as "already
    exists" (created=False). If the moto version in CI does not support
    `IfNoneMatch`, keep head-then-put under the lock and report it.
  - Encryption: `ServerSideEncryption="AES256"` by default;
    `AGENTICML_SERVER_S3_KMS_KEY_ID` set → `"aws:kms"` with that key.
  - `open`: `get_object` → wrap `Body` in a small class with `read(n)`,
    `close()`, `__enter__/__exit__`, so `_stream` and the code-diff reader
    use it like a file. Missing → `FileNotFoundError`.
  - `delete_workspace`: paginate `list_objects_v2` on
    `<prefix>/<workspace_id>/` and `delete_objects` in batches of 1000.
  - Client: `boto3.client("s3", endpoint_url=AGENTICML_SERVER_S3_ENDPOINT_URL
    or None, config=Config(retries={"mode": "standard", "max_attempts": 5},
    connect_timeout=5, read_timeout=60))`. Credentials come from the
    standard AWS chain (task role on ECS); never take them as flags.
- `open_backend(url, data_dir)`: `None`/`""`/`file://` → `FileObjects`
  (`file://` with a path other than the data directory is rejected with a
  clear message: objects must stay in the per-workspace store);
  `s3://bucket[/prefix]` → `S3Objects` (bucket name validated with the S3
  naming rule, prefix segments without `..` or empty parts). Anything else
  → `ValueError("--objects must be file:// or s3://bucket/prefix")`.
  `import boto3` only for `s3://`; on `ImportError` raise
  `"S3 objects need the [s3] extra: pip install 'agenticml-lite[s3]'"`.
- Exceptions: map botocore `ClientError` (not 404/412),
  `EndpointConnectionError`, `ConnectTimeoutError`, `ReadTimeoutError`
  to a new `ObjectStoreUnavailable` that the ingest and dashboard error
  mapping turn into 503 with `Retry-After: 5` (same as `DatabaseBusy`).
  Never include the bucket, key or AWS error text in client responses;
  log them server-side.

### Configuration (`cli.py`, `app.py`)

- `server serve` gains `--objects URL` (default
  `AGENTICML_SERVER_OBJECTS`, else `file://`). `server up` does not
  (laptop setup stays file-only).
- `app.serve`/`make_server` take `objects=` and store
  `server.objects = open_backend(...)` at start; a failed open (bad URL,
  missing extra) stops the server before it listens. At start, S3 mode
  calls `head_bucket` once and fails fast with a clear message if the
  bucket is unreachable.
- S3 together with SQLite is allowed (one process), but log one line at
  start noting S3 is intended for the hosted Postgres setup.

### Callers to switch

- `ingest.py`:
  - `_missing`: `self.server.objects.missing(self.workspace.id, hashes)`.
  - `_object`: hash check unchanged (`sha != actual` → 422 before any
    backend call). Under `object_lock` + quota lock: quota check, then
    `created = backend.put(...)`; record with `_record_object`. If the
    record fails after `created`, call `backend.delete` and re-raise
    (today's unlink, generalised). For the file backend the exact current
    sequence (including the unsafe-destination check and fsyncs) stays.
  - The storage-growth check near line 319 uses `backend.exists`.
- Dashboard (`ui/server.py`): `/api/objects/<sha>` and `_code_pair_diff`
  read through an opener. Add `_ctx_open_object(handler)` beside
  `_ctx_root`: in server mode it returns
  `lambda sha: ws.server.objects.open(ws.id, sha)`; in local mode
  `lambda sha: _open_regular_object(root, sha)`. `_code_pair_diff` takes
  an `open_object` callable instead of `root` (update its callers in
  `ui/server.py` and `server/app.py` `code_pair_diff`). Local `agenticml
  ui` behaviour must not change.
- `WorkspaceContext` (`access.py`): add `open_object(sha)` delegating to
  the backend with its own id.
- Workspace deletion (`server/workspaces.py` `delete_workspace`): after the
  database row is deleted and the store removed, call
  `server.objects.delete_workspace(id)` (CLI path: build the backend from
  `--objects`/env, so `server workspaces delete` gains `--objects`). If
  the S3 delete fails, print a warning naming the workspace id; the
  workspace is already gone from the database, so the bytes are orphaned
  but unreachable. Record this in the Backlog only if not fixed here.
- Any other `objects.has`/`object_path` use under `src/agenticml/server/`
  (grep) moves to the backend; `store/` (local SDK, gc) is untouched.
- `admin.py` status/verify: when `AGENTICML_SERVER_OBJECTS`/`--objects`
  is S3, skip the local objects walk and add a warning "objects are in
  S3; object checks come in package 23". Add `--objects` to both commands.

### Packaging and CI

- `pyproject.toml`: `[s3]` already lists `boto3>=1.34`; add
  `"boto3>=1.34"` and `"moto[s3]>=5.0"` to `dev`.
- `testbed/pyproject.toml` `[test]`: add `moto[s3]>=5.0`, `boto3>=1.34`.
- `.github/workflows/test.yml`: the jobs install `.[dev]`, so moto
  arrives with it; nothing else changes.

## Edge cases

- Same object uploaded concurrently through two processes: the object
  lock serialises them; the second sees `exists` (or 412) and returns
  `created: False`, 200.
- A client sends bytes that do not match the URL hash: rejected 422
  before the backend is touched (digest computed while streaming to the
  temp file, as today). S3's checksum is a second guard.
- Workspace A uploads sha X; workspace B asks `missing([X])` → X is
  missing for B, B uploads its own copy (`created: True`) and is charged
  for it. B's `GET /api/w/b/objects/X` before uploading → 404, same body
  as an unknown hash.
- S3 outage mid-upload: 503 with `Retry-After`; the temp file is removed
  as today; the database is unchanged; the client retries.
- `put` succeeds, DB record fails: object deleted, error propagates.
  `put` succeeds and the process dies before the record: an orphan
  object in S3 that the database does not list. The next upload of the
  same hash sees `exists` and records it (`created: False` path must
  still record the row and charge usage, as today). Package 23's verify
  reports orphans.
- Range requests: not supported today for objects; keep it that way.
- Empty object (size 0): works on both backends (S3 allows empty bodies).

## Tests and checks

New `tests/test_server_objects_s3.py`, using `moto.mock_aws` with
`AWS_ACCESS_KEY_ID=testing`, `AWS_SECRET_ACCESS_KEY=testing`,
`AWS_DEFAULT_REGION=us-east-1` set per test (monkeypatch), and a server
started with `objects="s3://agml-test/pfx"`. Reuse the helpers other
server tests use to start a server, create workspaces and keys
(`tests/server_db_helpers.py`, `test_server_isolation.py`).

1. A full `agenticml sync` of a run with code files, media and a copied
   artifact lands every object under `pfx/<wid>/objects/<hh>/<sha>`, none
   on local disk; the dashboard downloads each (bytes equal) and a code
   diff between two synced runs matches the file-backend result.
2. Hash mismatch → 422 and no key written; existing object bytes are
   never overwritten (put the same sha twice; second returns
   `created: False`; `head_object` ETag unchanged).
3. Isolation: two workspaces, same sha; each `missing` is per workspace;
   B cannot GET A's object through `/api/w/<b>/objects/<sha>` (404 equal
   to an unknown hash) or through any key/session/URL mix; the isolation
   suite (`test_server_isolation.py`) passes with an S3 parametrization
   of its object routes.
4. Every stored object has `ServerSideEncryption == "AES256"`; with
   `AGENTICML_SERVER_S3_KMS_KEY_ID` set the put uses `aws:kms` (assert on
   the request via a botocore event hook or moto's head result).
5. Workspace delete removes every key under `pfx/<wid>/` and nothing of
   the other workspace.
6. Storage limits (18c) count S3 objects the same as file objects
   (upload until the storage limit, then the 403 text).
7. Failure mapping: make `head_object` raise a 500 `ClientError` and an
   `EndpointConnectionError` (monkeypatch) → 503 with `Retry-After`, no
   bucket name or AWS text in the body.
8. `open_backend`: `file://`, `s3://b`, `s3://b/a/b`, bad bucket names,
   `s3://b/../x`, `http://x` → right backend or clear error; boto3
   missing (monkeypatch `sys.modules["boto3"] = None`) → the `[s3]`
   message.
9. `server status`/`verify` with `--objects s3://...` do not walk local
   objects and print the package 23 warning.
10. The record-fails path deletes the S3 object (inject a DB error in
    `_record_object`).

Checks: `testbed/.venv/bin/python -m pytest -q tests` (all pass, file
backend unchanged); the Postgres server suite with
`AGENTICML_TEST_DATABASE_URL` set; the new file on Postgres too.

## Done when

- All of the above pass on SQLite and Postgres, the existing suite is
  unchanged (no test edits needed for the file backend), and the real-S3
  check is in the roadmap Backlog for the owner (one sync plus one
  dashboard download against a real bucket with SSE).

## Review corrections (Nov 9, 2026)

- Keep the shared `ObjectStoreUnavailable` exception in stdlib-only
  `store/errors.py`, re-exported by `server/objectstore.py`. Local dashboard
  requests must never import the server package. Once response headers are
  sent, an object outage closes the connection without writing another response.
- Only S3 error codes `404`, `NoSuchKey` and `NotFound` mean an absent
  object. A deleted bucket and other 404 errors are service failures.
- S3 streams fill bounded reads until the requested length or EOF, normalize
  negative/None lengths to None, and map transport/streaming errors to the
  shared exception. Both object backends supply a callable for code diffs.
- `server up` explicitly selects `file://`, including when the environment
  selects S3. Workspace deletion returns an `object_cleanup_failed` marker
  on the deleted row so the CLI can warn on stderr with the workspace id.
- Regression coverage includes actual workspace object directories, fresh
  hash mismatch with no key, HTTP isolation and usage charging, cleanup of
  3 and 1005 keys, all four CLI `--objects` paths, bucket deletion, short
  reads, transport failures, and connection closure after partial responses.
  The unscoped `/api/objects/<sha>` stream test injects an S3 opener into
  the local dashboard handler; authenticated server requests use the scoped
  `/api/w/default/objects/<sha>` route.

Review verification:

- Requested six-file suite: 476 passed.
- Requested S3 + isolation suite against Postgres at port 55432: 61 passed.
- Testbed UI suite: 261 passed, 114 deselected (including mobile overflow checks).

# Package 3: Sync client v2 and `docs/PROTOCOL.md`

Nov 4, 2026 · Supervisor plan · Release 2.0 · Coders: Cursor (task A), Codex
(task B) · Docs: Antigravity (task C) · Size: L · not security-sensitive

Source: [on-prem-server-plan.md](../on-prem-server-plan.md), sections 4, 6 and
7 (package 3). Server side: [1-server-ingest.md](1-server-ingest.md) and
`src/agenticml/server/ingest.py`. Roadmap:
[on-prem-roadmap.md](../on-prem-roadmap.md). Base branch: `dev`.

## Goal

`agenticml login`, `whoami` and `sync` talk protocol v2 to an `agenticml
server` (package 1). `sync` copies the v1 store of each run byte for byte
(section 4.5 push order, section 4.4 rollback rule), resumes from the
server's state when the local cursor is lost, and marks a run synced in
`sync.json` so `gc` and `--prune` keep working unchanged. Every trace of the
hosted protocol and of `api.agenticml.xyz` is removed from code and tests.

Out of scope: `sync --follow` and its backoff (package 5; `--watch` stays as
it is), lineage bundles (package 7), uploading by-reference artifacts
(package 8), dashboard changes (packages 4 and 4b), any server change.
If the server turns out to need a change, stop and report it instead of
editing `src/agenticml/server/`.

## Tasks

The package is one PR, built in three tasks, in this order:

| Task | Coder | Scope |
| --- | --- | --- |
| A | Cursor | `client.py`, `auth.py`, their tests, `test_smoke.py` client tests |
| B | Codex | `sync.py`, the `reader.py` helper, `test_sync.py`, `test_review_fixes.py`, `cli.py` help text |
| C | Antigravity (prose only) | `docs/PROTOCOL.md`, `docs/SDK.md`, `README.md`, `website/.../configuration.md` |

B starts after A is reviewed, because B calls A's `Client`. C starts after B,
from facts the supervisor hands over, and is reviewed by the supervisor
against the code.

## Files to touch

| Path | Task | Change |
| --- | --- | --- |
| `src/agenticml/client.py` | A | Rewritten: v2 `Client` |
| `src/agenticml/auth.py` | A | `DEFAULT_HOST` removed; `login` / `whoami` / `logout` on v2 |
| `tests/test_client.py` | A | Rewritten |
| `tests/test_auth.py` | A | Host-default and login/whoami tests rewritten; credential-file tests kept |
| `tests/test_smoke.py` | A | `Client` construction tests updated |
| `src/agenticml/sync.py` | B | Rewritten push path; kept helpers listed below |
| `src/agenticml/store/reader.py` | B | `references_from_bytes` helper shared with `run_references` (no behaviour change) |
| `src/agenticml/cli.py` | B | Help text only (`--host` "default: saved default host", `--reset` wording). Flags unchanged |
| `tests/test_sync.py` | B | Rewritten, end-to-end against an in-process package 1 server |
| `tests/test_review_fixes.py` | B | Remove the two `_scalar_batches` tests; keep the sync-lock warning test working |
| `docs/PROTOCOL.md` | C | New |
| `docs/SDK.md` | C | Sync lines in the CLI cheat sheet and troubleshooting |
| `README.md`, `website/src/content/docs/reference/configuration.md` | C | No default host; login needs `--host` |
| `docs/plans/on-prem-roadmap.md` | supervisor | Package 3 status |

No new dependencies. Nothing under `src/agenticml/server/` or
`src/agenticml/ui/` changes.

## Task A: `client.py` and `auth.py`

### `client.py`

Replace the module. Keep `requests`; remove `urllib3.Retry` use, the v1
methods (`check_auth`, `init_run`, `log_batch`, `finalize`, `update_config`,
`artifacts_exists`, `upload_code`, `log_artifact`, `log_media`) and
`DEFAULT_BASE_URL`. `agenticml.Client` stays exported through
`agenticml.__getattr__`.

```python
class ProtocolError(ValueError):
    """The host answered, but it is not a protocol 2 agenticml server."""

class Conflict(Exception):
    """A 409 from the server. ``payload`` is the parsed JSON body (dict)."""
    payload: dict

class Client:
    def __init__(self, base_url=None, api_key=None, *, timeout=30,
                 max_retries=3, insecure=False): ...
    def close(self): ...                       # also __enter__/__exit__
    def whoami(self) -> dict                   # GET /v2/whoami
    def missing(self, hashes) -> list[str]     # POST /v2/objects/missing
    def put_object(self, sha256, handle, size) -> bool   # PUT /v2/objects/{sha}; True = created
    def run_state(self, run_id) -> dict | None # GET /v2/runs/{id}/state; None on 404
    def put_file(self, run_id, path, data: bytes) -> dict
    def append(self, run_id, path, offset, data: bytes, *, truncate=False) -> int
    def commit(self, run_id, status, fingerprint, sizes) -> dict
```

Rules:

- `base_url`: argument, else `AGENTICML_HOST`, else `ValueError("no server
  host: pass base_url or set AGENTICML_HOST")`. Normalized with
  `normalize_host(..., insecure=insecure)`. `api_key`: argument, else
  `AGENTICML_API_KEY`, normalized with `normalize_api_key`; `None` is allowed
  (requests then get 401).
- Header `Authorization: Bearer <key>` (no `x-api-key`), plus
  `User-Agent: agenticml-lite/<version>`. The key never appears in a
  `repr`, exception message or log line.
- `allow_redirects=False`; a 3xx raises `ValueError("server redirected;
  refusing to send the API key")` as today.
- `whoami`: 200 with a JSON object whose `protocol == 2` returns it
  (`key`, `scope`, `protocol`, `features`). 401 raises `requests.HTTPError`
  (response attached). 404, a non-JSON body, or any other `protocol` raises
  `ProtocolError(f"{host} is not an agenticml server with protocol 2")`.
- `missing`: splits the input into calls of at most 10,000 hashes and returns
  the union in input order.
- `put_object`: streams `handle` with `Content-Length: size`
  (`Content-Type: application/octet-stream`). 201 → `True`, 200 → `False`.
  422 raises `requests.HTTPError` (the caller reads `error == "hash
  mismatch"`).
- `append`: `POST /v2/runs/{id}/append/{path}?offset=N[&truncate=1]`, returns
  the server's `size`. `commit` sends `{"status", "fingerprint", "sizes"}`.
  Both raise `Conflict(payload)` on 409. `put_file` also raises `Conflict` on
  409 (ownership). `run_id` and `path` are put in the URL as given (they are
  already allow-listed names; do not percent-encode `/` in `path`).
- Other non-2xx responses raise `requests.HTTPError` with the response
  attached, so `sync._short_error` can read `{"error": ...}`.
- **Retries.** Every v2 call is idempotent (appends replay as no-ops, PUTs
  replace, commit rewrites `remote.json`), so all of them retry on
  connection errors, read timeouts, and 500/502/503/504: up to
  `max_retries` extra attempts, exponential backoff from 0.5 s capped at
  30 s, honouring `Retry-After` (seconds, capped at 30 s) on 503. Implement
  the loop in `Client` (not urllib3 `Retry`), and **rewind a streamed body**
  (`handle.seek(start)`) before each attempt, so a retried object upload sends
  the whole object again. Connection-level adapter retries stay off.
- Timeouts: `(10, timeout)` for small calls; for `put_object`, the read
  timeout is `max(timeout, 60 + size / 1 MiB)` seconds.

### `auth.py`

- Delete `DEFAULT_HOST`. In `_resolve`, when no host comes from the flag,
  `AGENTICML_HOST` or the saved `default_host`, raise
  `ValueError("no server host; run `agenticml login --host URL`")`. Keep the
  existing credential file, permission and `normalize_host` code unchanged.
- `cmd_login`: same flags. With verification (default), call
  `Client.whoami()`:
  - success → save as today; print
    `Logged in to {host} as {name} (scope {scope}) with key {prefix}`. When
    the scope is `admin`, also print to stderr: `warning: this is an admin
    key; training machines should use a sync key`.
  - 401 → `agenticml login: invalid API key`, exit 1.
  - `ProtocolError` → `agenticml login: {host} is not an agenticml server
    with protocol 2 (agenticml server 2.0 or newer)`, exit 1.
  - `--no-verify` saves without contacting the server, as today.
- `cmd_whoami`: prints `{host}  key={prefix}  name={name}  scope={scope}
  source={source}  valid`; 401 → `invalid API key`, exit 1;
  `ProtocolError` → the message above, exit 1; `--offline` unchanged.
- `cmd_logout`: with no `--host` and no saved default, print
  `agenticml logout: no host given and no default host saved` and exit 1.
- `_report` keeps hiding request exception details. `ProtocolError` is a
  `ValueError`, so its message prints; make sure its text never contains
  the key.

### Task A tests

`tests/test_client.py` (rewrite). Start a package 1 server in a thread on
port 0 (copy the `running` / `server` fixture pattern from
`tests/test_server_ingest.py`; create keys with `KeyStore`):

- `whoami` returns name, scope and protocol for a sync and an admin key; 401
  for a wrong key raises `HTTPError`; a key revoked through `KeyStore` gets
  401.
- `ProtocolError` from a tiny stdlib fake server that answers 404, a non-JSON
  200, and `{"protocol": 1}`.
- Round trip: `missing`, `put_object` (201 then 200), `put_file(meta.json)`,
  `append` (returns size), `run_state`, `commit`; 409 from a size mismatch
  raises `Conflict` with `payload["sizes"]`; `run_state` of an unknown run
  is `None`.
- `missing` with 25,000 hashes makes three requests (count them with a fake
  server or a wrapped session).
- Retry: a fake server that answers 503 (`Retry-After: 0`) once and then 201
  receives the full object body twice (rewind works); connection refused
  retries `max_retries` times then raises; a 3xx raises the redirect
  `ValueError` and the key is not sent to the `Location`.
- The key is not in `repr(client)` or in the text of any raised exception.
- No host and no `AGENTICML_HOST` → `ValueError`.

`tests/test_auth.py`: replace the two `DEFAULT_HOST` assertions with the
"no server host" error; replace the login/whoami tests that mock
`check_auth` with ones against the in-process server (success, admin-key
warning, invalid key, protocol error, `--no-verify`, logout without host).
Keep every credential-file and `normalize_host` test.

`tests/test_smoke.py`: `Client(api_key=...)` without a host raises
`ValueError`; trailing-slash and env tests use `https://example.com`.

After task A, `git grep -n "api.agenticml" -- src tests` is empty except
`src/agenticml/sync.py` (task B removes the rest).

## Task B: `sync.py`

### Kept as is

`_acquire_lock` / `_release_lock` (per-run `sync.lock`), `_publish_sync_state`,
`_label_part`, `_run_label`, `_order_runs_lineage`, `_selected_ids` (with the
new idle test), `_prune_synced_run`, `_prune_markers`, `_raw_scalars_removed`,
`_remember_skip`, `_skipped_ids`, `_permanent_object_reason`, `_clean_detail`,
`cmd_sync`'s argument checks and `--watch` loop, the `SyncResult` dataclass
(fields change, see below), `sync_run(reader, host, client, result, *,
dry_run=False, prune=False)` signature.

### Removed

`_init_payload`, `_wire_init_payload`, `_remote_parent`, `_config`, `_hash`,
`_json_safe`, `_scalar_batches`, `_skip_nonfinite_heads`, `ROUND_RECORDS`,
`BATCH_ITEMS`, `CODE_FILES`, the media / artifact / code upload loops,
`_byref_issue`, `_byref_restored`, `artifact_skips`, `_note_missing_remote`,
`_valid_content_type`, and the step-range checks. The server stores the
files as written, so nothing is translated or filtered.

### State in `sync.json`

The file keeps `{"version": 1, "hosts": {...}, "pruned": ..., "pruned_hosts":
[...]}`. A host entry written by this client:

```json
{"protocol": 2,
 "files": {"meta.json": {"size": 812, "sha256": "<hex>"},
           "events.jsonl": {"size": 40960, "tail": "<hex>"},
           "scalars/0.bin": {"size": 8000, "tail": "<hex>"}},
 "complete": true, "source_hash": "<run_fingerprint>",
 "skipped": [{"kind": "object", "id": "<sha>", "reason": "symlink"}],
 "pruned": false}
```

- Atomic files store `size` and `sha256` of the bytes last accepted.
- Append files store `size` (bytes the server has) and `tail`: SHA-256 of the
  local bytes `[max(0, size - 65536), size)`.
- An entry for this host **without** `"protocol": 2` (a v1 entry) is treated
  as empty and is replaced on the first save. Entries for other hosts
  (including `https://api.agenticml.xyz`) are never read, changed or deleted.
  `complete` / `source_hash` keep their meaning, so `store/gc.py:206` is
  unchanged.

### One run: `_sync_locked`

Under the existing `sync.lock`:

1. **Snapshot**, before any request:
   - `source_hash = run_fingerprint(root, run_id)`.
   - Effective status: briefly `try_lock_run`; compute
     `_effective_status(...)` as today; release the run lock right away.
     `stale` sets `result.stale`.
   - Local files: every regular file in the run dir matching the protocol
     allow-lists (`meta.json`, `scalars/index.json`, `media/index.json`,
     `scalars/<n>.ovw`, `events.jsonl`, `scalars/_time.bin`,
     `scalars/<n>.bin`; same regexes as `server/ingest.py`, copied, not
     imported). Read through `open_nofollow`; a symlinked or hard-linked file
     is skipped with one warning per sync and not sent.
   - Atomic files: read their bytes now (`read_file_nofollow`).
   - Append files: the capped size now. `.bin`: `size - size % 8`.
     `events.jsonl`: up to and including the last `\n` (scan back from the
     end; a file with no `\n` has capped size 0).
   - `meta.json`: if its `status` is `running` and the effective status is
     `crashed`, the bytes sent are `json.dumps(meta)` with `status` set to
     `crashed` (and nothing else changed); otherwise the original bytes.
     This is the only file that is ever altered, so the server dashboard
     shows a dead run as crashed.
   - References: after the sizes above, compute the object hashes from the
     `media/index.json` snapshot bytes and the `events.jsonl` bytes up to
     its capped size, with a new helper
     `reader.references_from_bytes(media_bytes, events_bytes, run_id)` that
     `run_references` now also uses (same rules: media rows, artifacts with
     `copy: true`, every code event; a corrupt complete line raises). A
     missing `media/index.json` counts as `{}` here. Because the hashes come
     from the same bytes that are sent, an object is always pushed before
     any file that refers to it.
2. **Prune guard.** Keep `_pruned_upload_block`, with a new allow rule: the
   upload is allowed when this host is in `pruned_hosts` or its entry has
   `pruned: true`; otherwise a pruned run fails permanently with today's
   message. A `.bin` missing locally while its `.ovw` exists is **pruned**:
   it is never sent, never rewritten, its cursor is kept, and it is left out
   of the commit's `sizes`.
3. **Idle.** If the entry has `protocol: 2`, `complete`, `source_hash ==
   source_hash`, and the effective status is `finished` or `crashed`, the
   run is `up to date`: no request (prune still runs as today).
4. **Cursor recovery.** If the entry has no `files` (new run for this host,
   v1 entry, lost `sync.json`, or after `--reset`), call
   `client.run_state(run_id)`:
   - `None` (unknown run): start from empty cursors.
   - `owner` set and not equal to `client.whoami()["key"]` (call `whoami`
     at most once per `cmd_sync`, cache it): fail permanently with
     `run id {id} belongs to key {owner} on this server`.
   - For each server file that is also local: atomic → cursor `{size,
     sha256}` from the server; append → if the server size is at most the
     local capped size and SHA-256 of the local bytes `[0, server size)`
     equals the server's `sha256`, cursor is the server size (compute
     `tail` locally); else mark the file for rewrite.
   The same recovery runs for one file when an append returns 409 without
   `mismatch` (see 6).
5. **Objects.** `client.missing(hashes)` for every referenced hash not in
   `skipped`; `put_object` each missing one from `open_object` (size from
   `fstat`). Errors:
   - `FileNotFoundError` / other `OSError` opening the object: warning, the
     object stays pending (run not complete); `_permanent_object_reason`
     symlink/hardlink → `_remember_skip(state, "object", sha, reason)`.
   - 422 `hash mismatch` → `_remember_skip(..., "sha256 mismatch")`.
   - Permanent skips do not block completion; pending ones do.
6. **Push.**
   - `meta.json` first when its snapshot `sha256` differs from the cursor
     (always on the first push; this creates the run on the server).
     `Conflict` with `owner` → the permanent ownership failure above.
   - Append files, in order `events.jsonl`, `scalars/_time.bin`, then
     `scalars/<n>.bin` by numeric `n`. For each:
     - **Rollback check.** If the cursor exists and the local capped size is
       below the cursor size, or the local `tail` window no longer hashes to
       the cursor's `tail`, rewrite the file.
     - **Rewrite**: `append(..., offset=0, data=<first chunk>,
       truncate=True)`, then normal appends for the rest. One warning per
       rewritten file per sync (`rewrote {path} after a local rollback`);
       count it in `result.rewritten`.
     - **Normal**: send `[cursor, capped size)` in chunks of at most
       `APPEND_CHUNK = 8 MiB`, cut at a record boundary (`.bin`: multiple of
       8; `events.jsonl`: after the last `\n` within the chunk; a single
       line longer than the chunk extends the chunk to its `\n`, up to the
       server's 64 MiB append limit, and past that the run fails with
       `event line larger than 64 MiB`).
     - Save `sync.json` after every accepted request (cursor `size` and
       `tail`), so an interrupted sync resumes where it stopped and a replay
       is a server-side no-op.
     - `Conflict` with `mismatch` → rewrite once. `Conflict` with only
       `size` → per-file cursor recovery (step 4) and retry once. A second
       conflict on the same file in one sync fails the run (not permanent).
     - `Conflict` `run is not running` on a rewrite: if the snapshot meta
       status is `running` (the run was resumed after the server saw it
       finish), send `commit(status="running", fingerprint=source_hash,
       sizes={})` to flip the server copy back to running, then retry the
       rewrite once; otherwise fail the run with `server copy is {status};
       cannot rewrite {path}`.
   - Then `.ovw` files, `scalars/index.json`, `media/index.json`, each only
     when its snapshot `sha256` differs from the cursor.
7. **Commit.** Whenever anything was sent, the cursor was recovered, or the
   effective status is not terminal (running runs always commit, which keeps
   `last_sync_at` fresh for package 5):
   `commit(status=<status in the meta bytes sent>, fingerprint=source_hash,
   sizes=<every local file's pushed size, minus pruned .bin files>)`.
   - `Conflict` with `sizes` → recover those files (step 4), push them again,
     commit once more; a second size conflict fails the run (not permanent).
   - `Conflict` with `status` → re-send the `meta.json` snapshot, commit once
     more.
8. **Result.** `complete = effective in (finished, crashed) and
   run_fingerprint(...) == source_hash and no pending objects`. Save
   `complete` and `source_hash`. Status text: `synced`, `partial`, or
   `partial (running)` as today. `--prune` runs after a complete sync as
   today (`_prune_synced_run`).

`--dry-run`: no client, no request, no `sync.json` write. Uses the saved
cursor only and reports what would be sent (`files`, `bytes`, referenced
`objects` as a count with "unchecked"). The existing
`test_verbose_init_and_sync_label_omit_literal_none` dry-run call must keep
passing.

### `SyncResult`, errors, `cmd_sync`

- `SyncResult` fields: `run_id, label, status, files, bytes, objects,
  skipped, rewritten, warnings, complete, failed, permanent, stale`.
  `line()`: `{run_id}  {label}  {status}  files=N objects=N bytes=N
  skipped=N rewritten=N` plus `; warnings`. Never print `None`.
- `_short_error`: 401 → `API key rejected — run `agenticml login``; other
  4xx → `HTTP {code}: {error}` from the JSON `error` field through
  `_clean_detail`; otherwise as today. `ProtocolError` → its message.
- `_permanent_failure`: 4xx except 408 and 429; plus the ownership and
  prune failures above.
- `cmd_sync`: build the `Client` once (`with` or `finally: close()`). Before
  the first run, call `whoami()` once; `ProtocolError` or 401 stops the
  command with exit 1 and a one-line message, before touching any run.
- `--reset RUN_ID...` keeps its checks and clears only this host's entry
  (keeping `pruned` / `pruned_hosts`); the next sync recovers the cursor from
  `GET /state` instead of uploading again. Help text: "Forget this host's
  saved cursor for the given runs; the next sync resumes from the server".

## Constraints and conventions

- Python 3.9 compatible; `from __future__ import annotations`; style of the
  current `sync.py` (short docstrings that say why, no new abstractions
  beyond what the steps need).
- `requests` only in `client.py` / `auth.py`; `sync.py` may import
  `requests.HTTPError` for error mapping as today.
- Do not import from `agenticml.server` in `sync.py` or `client.py`
  (tests may).
- Reuse `store.layout` helpers (`open_nofollow`, `read_file_nofollow`,
  `open_object`, `try_lock_run`, `RECORD_SIZE`, `resolve_root`) and
  `store.gc.run_fingerprint`. Do not duplicate `normalize_host`.
- Never send `run.lock`, `sync.json`, `sync.lock`, `notes.md` or
  `remote.json`.
- Local training and `agenticml ui` behaviour must not change; no writer
  changes.

## Edge cases

- Live run: a `.bin` grows between the snapshot and the push → only the
  snapshot size is sent; the rest goes next sync.
- Torn last line in `events.jsonl` → not sent until its `\n` is written; no
  exception from reference collection.
- Writer rollback below the cursor, or a rollback followed by new records
  that bring the file past the cursor → caught by the size or `tail` check.
- Same run id already owned by another key → permanent failure, exit 1,
  other runs still sync.
- Server restored from an older backup (server sizes smaller than cursors)
  → 409 size → per-file recovery from `GET /state`, no duplicates.
- `sync.json` deleted, or `--reset` → recovery; appends that already match
  send no bytes.
- Pruned run on the host that has its history → up to date / commit
  without its `.bin`; on another host → permanent prune failure (as today).
- A resumed run whose server copy is `finished` and needs a rewrite → the
  running-commit flip in step 6.
- An object referenced by the run but deleted locally (e.g. by hand) → run
  stays `partial`, warning each sync; a symlinked object → permanent skip.
- By-reference artifacts (`copy: false`) are not uploaded in 2.0 and never
  block completion.
- Server unreachable mid-run → retries, then the run fails (not permanent)
  and the cursor keeps everything accepted so far.

## Tests (task B)

`tests/test_sync.py` (rewrite). An in-process package 1 server per test
(`make_server(tmp/data, port=0)` in a thread, a sync key from `KeyStore`),
the local store in `tmp/local`, `cli.main(["sync", ...,
"--host", url, "--api-key", key, "--dir", local])` for end-to-end cases and
`sync_run` with a `Client` for focused ones. A helper compares every
allow-listed file under `local/runs/<id>` with `data/store/runs/<id>` byte
for byte and checks every referenced object exists on the server.

- Finished run (several series, events, media, a copied artifact, code
  manifest) → byte-identical; `remote.json` status `finished`;
  `sync.json` entry `complete` with `source_hash == run_fingerprint`.
- Crashed run via `finish("crashed")` → identical. Dead-process run (meta
  `running`, lock free, heartbeat old) → server `meta.json` equals local
  except `status == "crashed"`; server `remote.json` status `crashed`.
- Running run (writer open in the test) → files identical up to the
  snapshot, commit status `running`, `complete` false; log more, sync again,
  identical again.
- Replay: a second sync of a finished run prints `up to date` and makes no
  requests (count with a wrapped `Client`). Interrupted: make the client
  raise after the second append request, sync again → identical, no
  duplicated records.
- Lost cursor: delete `sync.json`, sync → identical and zero append bytes
  sent. Same after `sync --reset RUN_ID`.
- Rollback: sync a running run, truncate a `.bin` below the cursor and write
  different records, sync → `rewritten=1`, one warning, server identical.
  Same for a same-size rewrite of the tail window (the `tail` check).
- Resumed finished run that needs a rewrite → the running-commit flip, then
  identical.
- Chunking: monkeypatch `APPEND_CHUNK` to 64 bytes → many requests, server
  identical; a torn last event line is not sent.
- Server restored smaller: truncate a server `.bin` behind the client's back
  → 409 → recovery → identical.
- `--prune` after a complete sync deletes raw `.bin`s locally; the next sync
  is `up to date`; `gc` still sees the run as synced; the server's `.bin`s
  are untouched. A pruned run to a second server fails with the prune
  message.
- `meta.json` fields `task`, `task_source` and `arch` reach the server.
- Old entries: a `sync.json` with a v1 entry for this host and one for
  `https://api.agenticml.xyz` → sync succeeds, this host's entry becomes
  `protocol: 2`, the other entry is byte-for-byte unchanged.
- Ownership: run pushed with key A, then with key B → exit 1, message names
  key A, other runs in the same command still sync.
- 401 (revoked key) and a non-v2 host stop `cmd_sync` before any run.
- Objects: symlinked object → permanent skip, run still `synced`;
  deleted object → `partial` with warning; by-reference artifact does not
  block `synced`.
- Lineage order: a child selected before its parent is synced after it.
- `--dry-run`: no request, `sync.json` unchanged.

`tests/test_review_fixes.py`: delete
`test_thousand_key_scalar_round_budget_and_offsets` and
`test_scalar_round_budget_when_keys_outnumber_budget`; keep
`test_sync_lock_unsupported_warns_once_and_continues` passing (adapt only
what the rewrite forces).

## Checks (coder, before handing back)

1. `testbed/.venv/bin/python -m pytest -q tests` from the repo root, all
   green; report counts.
2. Task A: `testbed/.venv/bin/python -m pytest -q tests/test_client.py
   tests/test_auth.py tests/test_smoke.py`. Task B: `... tests/test_sync.py
   tests/test_review_fixes.py tests/test_gc.py tests/test_local_cli.py
   tests/test_server_ingest.py`.
3. Python 3.9 if available (`python3.9 -m pytest` with the package
   importable); report if not.
4. `git grep -n "api.agenticml" -- src tests` is empty (after task B).
5. Manual (task B): `agenticml server keys create --name box --data
   <scratch>`, `agenticml server serve --data <scratch> --port <p>`, train a
   testbed run into a scratch `AGENTICML_DIR`, `agenticml login --host
   http://127.0.0.1:<p> --key ...`, `agenticml whoami`, `agenticml sync
   --all`, then a second `sync --all` prints `up to date`.
6. Report the diff summary, test counts and any deviation from this plan.

## Done when

- The tests above pass in the full suite on Linux (CI green on the PR).
- `git grep -n "api.agenticml.xyz" -- src tests README.md website docs/*.md`
  is empty (the plan documents under `docs/plans/` may still name it).
- `docs/PROTOCOL.md` describes every endpoint, rule and client behaviour of
  section 4 plus the package 1 additions (`scope` in `whoami`, the commit
  status check, `meta.json` `run_id`), and the supervisor has checked it
  against the code.
- Sonnet's review has no open findings; the roadmap row says `in review` in
  the PR.

# Package 10: Server operator commands

Nov 6, 2026 · Supervisor plan · Release 2.1 · Coder: Codex · Size: S

Source: [on-prem-roadmap.md](../on-prem-roadmap.md) (2.1, package 10).
Builds on packages 7 (server lineage repos) and 8 (`artifacts.json`). Base
branch: `dev`. Ships in the single 2.1 PR, one commit per package.

## Goal

Two read-only commands for whoever runs the server:

- `agenticml server status [--data D] [--json]`: runs, sizes, keys, and the
  last sync per key.
- `agenticml server verify [--data D] [--json]`: re-hash every object and
  check each run's `remote.json` sizes against its files. Exit 0 when clean,
  1 when any problem is found, 2 on usage or unreadable data directory.

Both work while `agenticml server serve` is running (no `server.lock`, no
writes, no `keys.lock` needed beyond what `KeyStore.list()` already does).

Done when both are tested against a store with deliberately corrupted
files.

Out of scope: repairing anything, deleting objects, network calls to a
running server, `gc` on the server.

## What exists today

- `cli.py`: `server serve` and `server keys create|list|revoke`
  (`cmd_server_keys`), with `--data` resolved by `server.app.resolve_data_dir`
  and the same error style (`agenticml server: <message>` on stderr).
- `server/keys.py` `KeyStore(server_dir).list()` returns `KeyRecord`s
  (name, scope, prefix, created_at, created_by, last_used_at) via
  `to_public()`.
- Store helpers: `store.reader.list_runs`, `run_references`,
  `layout.object_path`, `layout.valid_sha`, `layout.read_file_nofollow`,
  `layout.contained`; `store/gc.py` and `cmd_du` in `cli.py` already compute
  store sizes; reuse their helpers where they fit rather than re-walking
  differently.
- `remote.json` (per run, server only): `{owner, status, fingerprint,
  last_sync_at, sizes}`.
- After package 7: `<store>/lineage/<key>.git` with `refs/runs/<id>`.
  After package 8: `artifacts.json` (server only).

## Files to touch

- `src/agenticml/server/admin.py` (new): `status(data) -> dict`,
  `verify(data) -> dict`, plus text formatting.
- `src/agenticml/cli.py`: `server status` and `server verify` subparsers
  and `cmd_server_status` / `cmd_server_verify`.
- `docs/SERVER.md`: a short "Operator commands" section (and backups /
  troubleshooting cross-links if natural).
- `tests/test_server_admin.py` (new).

## `server status`

Output dict (`--json` prints it with `json.dumps(indent=2, sort_keys=True)`):

```
{"data": "<path>", "runs": {"total": n, "by_status": {"running": n, "finished": n,
  "crashed": n, "stale": n, "unsynced": n}},
 "sizes": {"runs": bytes, "objects": bytes, "objects_count": n, "lineage": bytes, "total": bytes},
 "keys": [{"name", "scope", "prefix", "created_at", "last_used_at",
           "runs": n, "last_sync_at": ts|null}, ...]}
```

- Status per run: use the same effective status the server dashboard shows
  (`list_runs` already applies `remote.json` / stale logic from package 2;
  reuse it, do not re-implement). `unsynced` = no `remote.json` status yet.
- Per key: `runs` = runs whose `remote.json` owner is the key; `last_sync_at`
  = latest `last_sync_at` among them. Owners that are not current keys
  (revoked) appear under a single extra entry per owner name with
  `"revoked": true`.
- Sizes: lstat-based walk, never follow symlinks, skip and count entries
  that cannot be stat'ed.
- Text output: a short table-like summary, one key per line.
- Never print key secrets or hashes (only `prefix`).

## `server verify`

Checks, each problem reported as `{"kind", "path", "detail"}`:

1. **Objects**: for every file under `objects/<hh>/<sha>`: name must be a
   valid sha in the right `<hh>` directory, a regular file (not a symlink),
   and its streamed SHA-256 must equal the name (`kind: "object_hash"`,
   `"object_name"`, `"object_type"`). Temp files the object writer leaves
   (if any naming pattern exists in `store/objects.py`) are ignored.
2. **Run files vs `remote.json`**: for each run with a valid `remote.json`
   that has `sizes`, every listed file must exist as a regular file with
   exactly that size (`kind: "size_mismatch"`, detail `expected N, found M`
   or `missing`). Files that grew since the last commit while the run is
   `running` are still reported, but as `kind: "size_ahead"` with severity
   warning (not counted as failure), because a live sync appends between
   commits. A finished or crashed run must match exactly.
3. **Invalid `remote.json`** (not JSON, not an object): `kind:
   "remote_invalid"`.
4. **Missing objects**: digests from `run_references` (includes package 8
   byref digests) that are not present: `kind: "object_missing"`. Runs whose
   references cannot be read: `kind: "run_unreadable"`.
5. **Lineage** (only when `git` is available and the run's `meta.json` has
   a valid lineage block and the server repo exists): if `refs/runs/<id>`
   exists, it must equal `meta.json["lineage"]["commit"]` (`kind:
   "lineage_ref"`). Use the git helpers and environment from
   `agenticml.lineage` (no inherited `GIT_*`). Do not run `git fsck`.
6. **Stale ref locks** (closes the roadmap Backlog entry "A stale
   `refs/runs/<id>.lock` blocks a run's lineage forever"): any
   `refs/runs/*.lock` file in a server lineage repo is reported as `kind:
   "lineage_lock"`, with the exact removal command in `detail`, as a
   warning when younger than 10 minutes (a receive may be running) and as a
   problem when older. `verify` never removes it. Remove the Backlog entry
   in this package.

Output: `{"ok": bool, "checked": {"objects": n, "runs": n}, "problems":
[...], "warnings": [...]}`. Text output: one line per problem, then a summary
line `verify: N problems, M warnings (objects X, runs Y)`. Exit 1 iff
problems is non-empty.

Large stores: stream hashes in 1 MiB chunks; report progress only in text
mode to stderr every 1,000 objects when stderr is a TTY.

## Constraints and conventions

- Read-only: open files no-follow where the store helpers do; never create
  directories (`prepare_data_dir` must not be called; a missing data dir is
  exit 2 `agenticml server: no server data at <path>`).
- Standard library only. Match `cmd_server_keys` error handling and output
  style.

## Edge cases to test

Build a server store by running a real server (port 0) and syncing a couple
of runs (as `test_server_ingest.py` / package 7 tests do), or by writing the
files directly where simpler. Then:

1. Clean store: `verify` exit 0, `ok: true`; `status` counts runs per status,
   keys with run counts and last sync, sizes > 0.
2. Flip a byte in an object: `object_hash`, exit 1.
3. Object file renamed to a wrong sha / placed in the wrong `<hh>` dir:
   `object_name`.
4. Symlink in `objects/`: `object_type`, and the target is not hashed.
5. Truncate a `.bin` of a finished run: `size_mismatch`; append to a running
   run's `events.jsonl`: `size_ahead` warning, exit 0.
6. Delete an object a run references: `object_missing`.
7. Corrupt `remote.json`: `remote_invalid`.
8. Lineage ref moved to another commit (`git update-ref`): `lineage_ref`.
   A `refs/runs/<id>.lock` file: warning when fresh, `lineage_lock` problem
   when its mtime is older than 10 minutes.
9. `status` with a revoked key that still owns runs: listed with
   `revoked: true`.
10. `--json` output parses; text output contains no key hashes.
11. Missing data dir: exit 2, nothing created.

## Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.

## Done when

- Both commands exist, are documented in `docs/SERVER.md`, and the tests
  above pass.
- Roadmap status for package 10 is `done` in the 2.1 PR.

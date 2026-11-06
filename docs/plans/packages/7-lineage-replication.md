# Package 7 †: Lineage replication

Nov 6, 2026 · Supervisor plan · Release 2.1 · Coder: Codex · Size: M ·
Security-sensitive (second read-only review by Cursor)

Source: [on-prem-roadmap.md](../on-prem-roadmap.md) (2.1, package 7) and
[on-prem-server-plan.md](../on-prem-server-plan.md) sections 4.2 (features)
and 6 (code diffs). Base branch: `dev`. All 2.1 packages ship in one PR, one
commit per package.

## Goal

A run synced to the server shows the same code diff in the server dashboard
as in the local one. The client pushes each run's shadow commit
(`<store>/lineage/<project-key>.git`, `refs/runs/<id>`) as a `git bundle`,
incremental against the commits the server already has. The server fetches it
into its own shadow repo with hooks disabled and a fixed, minimal Git config,
and advertises the `lineage` feature in `whoami`.

Done when a server-side code diff between two synced runs matches the local
diff, and a malformed or hostile bundle is rejected without touching existing
refs.

Out of scope: worktree checkouts on the server, `agenticml lineage` CLI
against a remote host, notes, deleting refs on the server.

## What exists today

- `src/agenticml/lineage.py`: `snapshot()` writes `refs/runs/<id>` in a bare
  repo `lineage/<key>.git` (`project_key(project)`, `_KEY_RE`), and stores
  `meta.json["lineage"]` = `{project_key, commit, parent_commit, tree, ...}`.
  `_env()` strips every inherited `GIT_*` variable, disables system and global
  config, and sets a fixed identity. `_locked(root, repo)` is a per-repo
  flock. `_resolve_commit(repo, run_id)` reads a ref.
- The dashboard `GET /api/runs/<id>/lineage` (`_lineage_changes` in
  `ui/server.py`) runs `git diff-tree` on `<root>/lineage/<key>.git` using the
  commits from `meta.json`. On the server with no repo it answers "code diff
  not available on this server". Git bundles keep commit ids, so once the
  server repo holds the commits, this works unchanged.
- `server/ingest.py`: routes, `_authenticate`, `_ownership`, per-run lock
  `server.run_lock(run_id)`, `_body_length` / `_read_body` with deadlines,
  temp files in `server/tmp`, `Limits` in `server/app.py`. `whoami` returns
  `"features": []`.
- `sync.py` `_sync_locked`: push order objects → `meta.json` → appends →
  atomics → commit. `_identity(client)` caches `whoami`. `_sync_idle` and the
  "up to date" early return skip complete runs. `_order_runs_lineage` already
  sends parents before children. `_remember_skip` records permanent skips.
- `client.py` `Client`: `_request`, `_conflict_or_http`, retry logic.

## Files to touch

- `src/agenticml/server/lineage.py` (new): server-side receive (bundle
  checks, fetch, refs listing).
- `src/agenticml/server/ingest.py`: two routes, feature list, limits.
- `src/agenticml/server/app.py`: `Limits.lineage_body` (default 256 MiB),
  detect `git` at startup (`shutil.which("git")`; log a warning and leave
  the feature off when missing).
- `src/agenticml/lineage.py`: client helpers `bundle_for_run(root, run_id,
  have_commits, dest)` and anything shared with the server (reuse `_env`,
  `_git`, `_locked`, `_repo_path`, `_ref`; do not duplicate them).
- `src/agenticml/client.py`: `lineage_state(run_id)` and
  `put_lineage(run_id, handle, size)`.
- `src/agenticml/sync.py`: push the bundle when the server lists `lineage`.
- `docs/PROTOCOL.md`: the two endpoints, the `lineage` feature, client push
  order; `docs/SERVER.md`: drop "lineage code diffs" from "Unsupported
  features", say `git` must be installed on the server for code diffs;
  `docs/THREAT_MODEL.md`: hostile bundles.
- Tests: `tests/test_server_lineage.py` (new), additions to
  `tests/test_sync.py` if it is the natural place for client cases.

## Protocol

Both endpoints sit under the run, so run-id validation, authentication and
ownership work exactly like the other run routes (404 unknown run, 409 owned
by another key).

### `GET /v2/runs/{id}/lineage`

`200 {"commit": <sha or null>, "tips": [sha, ...]}`

- The project key and the run's commit come from the server's copy of
  `meta.json["lineage"]` (`project_key` must match `_KEY_RE`, `commit` must be
  40 lowercase hex). If `meta.json` has no valid lineage block: 422
  `{"error": "run has no lineage"}`.
- `commit`: what `refs/runs/{id}` points to in the server repo, or `null`.
- `tips`: the distinct commits of every `refs/runs/*` ref in that project's
  server repo (empty if the repo does not exist), capped at 10,000 (newest
  by committer date first, `git for-each-ref --sort=-committerdate
  --count=10000`). Only ref tips are listed, because only they are known to
  be complete (fetch checks connectivity before updating a ref).

### `PUT /v2/runs/{id}/lineage`

Body: a git bundle (v2 or v3) as raw bytes, `Content-Length` required,
limit `Limits.lineage_body` (413 above it). Written to `server/tmp` like an
object upload (`_read_body` with a handle), then checked under
`server.run_lock(id)` and the repo's `_locked` flock:

1. `meta.json` lineage block as above, else 422. Expected ref
   `refs/runs/{id}`, expected commit = `meta.json["lineage"]["commit"]`.
2. If the server ref already equals the expected commit: 200
   `{"commit": c, "created": false}` (idempotent replay). If it exists with
   a different commit: 409 `{"error": "lineage ref differs", "commit":
   <server commit>}`. Refs are never moved or deleted by this endpoint.
3. Parse the bundle header in Python before running Git: first line must be
   `# v2 git bundle\n` or `# v3 git bundle\n`; v3 capability lines (`@...`)
   other than `@object-format=sha1` are rejected; prerequisite lines
   (`-<sha> ...`) and exactly **one** ref line, `<expected commit>
   refs/runs/{id}`; header capped at 1 MiB. Anything else: 422
   `{"error": "invalid bundle"}`. (Do not trust `git bundle list-heads`
   alone; check the header ourselves, then let Git verify.)
4. Create the server repo lazily with the same code path as the client
   (`shadow_repo`-style: `git init --bare --template=`), under
   `<data>/store/lineage/<key>.git`, contained in the store root.
5. `git bundle verify <tmp>` then fetch:
   `git -c core.hooksPath=/dev/null -c transfer.fsckObjects=true
   -c fetch.fsckObjects=true -c fetch.writeCommitGraph=false
   -c gc.auto=0 -c core.fsmonitor=false -c protocol.allow=never
   -c protocol.file.allow=always fetch --no-tags --no-write-fetch-head
   --no-auto-gc <tmp> refs/runs/{id}:refs/runs/{id}` with `lineage._env(repo=...)`
   (no inherited `GIT_*`, no system/global config), stdin from /dev/null,
   a timeout (60 s), stdout/stderr captured and never echoed to the client
   (log the first line at warning level only). Confirm with a test that
   `protocol.allow=never` with `protocol.file.allow=always` still allows a
   bundle path on git 2.34; if it does not, drop only the protocol options
   and say so in the report.
6. After the fetch, re-read the ref; it must equal the expected commit and
   `git cat-file -t <commit>` must be `commit`. If the fetch fails: 422
   `{"error": "invalid bundle"}` (or 422 `{"error": "missing prerequisite"}`
   when `bundle verify` reports missing commits, so the client can resend a
   full bundle). Existing refs are untouched in every failure case.
7. 201 `{"commit": c, "created": true}`.

`whoami` lists `"lineage"` in `features` when `git` was found at startup.

## Client (`sync.py`)

- Only when `"lineage" in _identity(client).get("features", [])` and the
  local `meta.json["lineage"]` has a valid `commit` and `project_key`, and the
  local ref `refs/runs/<id>` resolves to that commit. Otherwise skip silently
  (a run without a snapshot, or a 2.0 server).
- Step order in `_sync_locked`: after `meta.json` is pushed and before
  `commit` (the server needs `meta.json` to know the expected commit).
- `client.lineage_state(id)`: if `commit` equals ours, record
  `state["lineage"] = commit` and stop. Otherwise build a bundle in a temp
  file under the local store (`lineage.bundle_for_run`): `git bundle create
  <tmp> refs/runs/<id> --stdin` with `^<tip>` lines for each server tip that
  exists locally (`git cat-file --batch-check` to filter unknown ones).
  If every ancestor is excluded Git refuses an empty bundle; that only
  happens when the server already has the commit, which step above covers.
- `put_lineage`: on 422 `missing prerequisite`, rebuild once with no
  exclusions and retry. On 409 `lineage ref differs` or other 422: warning
  `lineage: <error>`, `_remember_skip(state, "lineage", id, reason)` so it is
  not retried every pass, and do not block completion. Transient errors
  propagate like other requests (the run stays incomplete).
- Completion: when the feature is listed and the run has a local commit, the
  run is `complete` only after `state["lineage"] == commit` or a remembered
  lineage skip.
- Backfill: a run synced complete under 2.0 must get its lineage pushed once
  the server lists the feature. `_sync_idle` and the "up to date" early
  return in `_sync_locked` treat a run as not idle when the feature is listed,
  the run has a local commit, and `state.get("lineage")` differs and there is
  no lineage skip. `_selected_ids` therefore needs the identity's features;
  pass them in (it already gets `key`).
- `--dry-run` reports `lineage` as one more file when it would be sent; it
  does not call the server for lineage.
- Delete the temp bundle in `finally`.

## Constraints and conventions

- Server stays standard-library only; `git` is an external program, as on
  the client. Never use a shell; argument lists only; ref names built only
  from `valid_run_id` ids; project keys only from `_KEY_RE`.
- No new environment variables. No change to `meta.json`, the v1 store
  format, or other endpoints. 2.0 clients keep working against this server;
  this client keeps working against a 2.0 server.
- Match surrounding style (compact, comments only where the reason is not
  obvious).

## Edge cases to test

Server (`tests/test_server_lineage.py`, real `git`, server on port 0 as in
`test_server_ingest.py`):

1. Happy path: two runs in one project, parent and child; sync both; the
   server's `/api/runs/<child>/lineage` payload (files list) and
   `lineage.diff(server_root, child, parent)` equal the local ones. The
   second bundle is incremental (smaller than a full one, or contains the
   parent as prerequisite).
2. Replay of the same bundle: 200 `created: false`.
3. Bundle whose ref name is different (`refs/heads/x`, `refs/runs/other`),
   has two refs, or whose commit differs from `meta.json`: 422, no ref
   created, existing refs unchanged.
4. Ref already set to another commit: 409; ref unchanged.
5. Garbage bytes, truncated bundle, v3 with an unknown capability: 422.
6. Bundle with a missing prerequisite: 422 `missing prerequisite`; client
   retries with a full bundle and succeeds.
7. fsck-failing objects (for example a tree containing a `.git` entry or a
   `..` path, built with `git mktree`/`hash-object --literally`): 422; ref
   absent.
8. Body over the limit: 413 before reading.
9. Another key's run: 409; unknown run: 404; run without lineage in
   `meta.json`: 422.
10. Server repo is not affected by a hostile local Git config: set
    `GIT_CONFIG_*`/`GIT_DIR` env vars in the server process environment and a
    hook file in the server repo `hooks/` dir; hooks never run.
11. `whoami` features: `["lineage"]` with git, `[]` when `shutil.which`
    returns None (monkeypatch).

Client:

12. Server without the feature: no lineage requests; run completes as before.
13. Backfill: a run complete under a featureless server becomes non-idle and
    pushes lineage once the server lists the feature, then is idle again.
14. A remembered 409 skip does not block completion and is not retried.
15. Run with a lineage error / no commit: no request, completes.

## Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- No dashboard code changes are expected; if any, also run the UI suite.

## Done when

- The tests above pass, including the code-diff equality check and the
  hostile-bundle cases with existing refs unchanged.
- PROTOCOL.md, SERVER.md and THREAT_MODEL.md describe the feature.
- Roadmap status for package 7 is `done` in the 2.1 PR.

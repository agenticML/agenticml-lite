# Fix: lineage diff when the parent ran on another machine

Nov 21, 2026 · Supervisor plan · Maintenance fix after 3.3.0 · Size: S ·
Base branch: `main` (tag `v3.3.0` + release docs).

Status: plan draft, waiting for the owner's review. Nothing is built yet.

## Goal

A run's dashboard code diff ("Changed files" in the lineage view, `GET
/api/runs/<id>/lineage`) compares the run with its lineage parent even when
the parent was created in another store, as long as both runs' snapshots are
on the server, in either order. Today such a child shows every file as added.

Who hits it: agentlab runs jobs on several worker machines. Each worker has
its own local store and runs `agenticml sync --follow` to one self-hosted
`agenticml server serve`. agentlab's origin picks each run's id, task and
parent at queue time and passes `--id`, `--task` and `--parent`, so the parent
is often on another worker. The lineage graph link is already correct (it uses
`parent_run_id`); only the code diff is wrong.

Out of scope: the run-to-run code compare (`/api/compare/code`, matched by
path and hash), diffs across projects, and giving the child's shadow commit a
Git parent after the fact.

## What exists today (confirmed from the code)

- `src/agenticml/lineage.py` `snapshot()` (~line 348): inside the repo lock it
  sets `parent_commit = _resolve_commit(repo, parent_run_id)`, which reads
  `refs/runs/<parent>` in the **local** shadow repo
  `<store>/lineage/<project-key>.git`. With an explicit parent from another
  machine that ref does not exist, so `parent_commit` is `None`, the child
  commit is created without `-p`, and `meta.json["lineage"]` keeps
  `parent_commit: null` forever. `run.py` (~line 423) logs "parent run ... not
  found ...; recording it anyway" and keeps `parent_run_id`.
- `lineage.diff()` (CLI `agenticml lineage diff`, lines 461–476) falls back to
  `_resolve_commit(repo, info["parent_run_id"])` when `parent_commit` is empty.
  `docs/FORMAT.md` (~line 190) already promises this: "If the parent is still
  unpublished, the child has no Git parent and `parent_commit` is null; diff
  resolves `parent_run_id` live on later calls. Existing commits and metadata
  are not rewritten."
- `src/agenticml/ui/server.py` `_lineage_changes()` (lines 1011–1071) reads
  only the stored `parent_commit`. When it is empty it runs
  `git diff-tree --root -r --numstat -z <commit>`, so every file is "added".
  It is shared by the local `agenticml ui` and the server dashboard (the
  server routes `/api/w/<slug>/runs/<id>/lineage` to the same handler with
  `root = _ctx_root(self)`, the workspace store). It already validates
  `commit`/`parent_commit` with `_COMMIT_RE`, the key with `_PROJECT_KEY_RE`,
  and contains the repo path with `layout.contained`. The payload reports
  `parent_run_id = _stored_parent(meta)[0]` (top-level `parent_run_id` wins
  over `lineage.parent_run_id`).
- The server keeps one shadow repo per workspace and project:
  `<workspace store>/lineage/<key>.git`. `server/lineage.py` `receive()`
  fetches each run's bundle and publishes `refs/runs/<id>` (immutable, always
  a commit). Both runs' commits end up in the same repo when they share a
  project, whichever arrives first (`test_child_before_parent_resends_full_bundle`).
  So on the server `refs/runs/<parent>` exists once the parent has synced.
- `meta.json` is pushed byte for byte and re-pushed whenever the client's
  cursor hash differs (`docs/PROTOCOL.md`, sync order step 2). The server must
  not rewrite it.
- The lineage graph (`_lineage_graph`) compares trees (`same_tree_as_parent`),
  not `parent_commit`, so "code unchanged" is already right cross-host. The
  inspector in `ui/static/app.js` (~line 2030) fetches the per-run diff only
  when the trees differ, keeps it in a per-view `details` map, and shows
  `N files changed · <first path>`.
- Dashboard JSON answers are `Cache-Control: no-store`. There is no
  server-side cache for this endpoint.

## Options

| | (a) Read-time fallback in `_lineage_changes` | (b) Server fills `parent_commit` on receive | (c) Client fetches the parent at snapshot | (d) Local `agenticml ui` |
| --- | --- | --- | --- | --- |
| Parent syncs after the child | Correct on the next request | Needs a second trigger on the parent's receive (find children by `parent_run_id`) | Fails: the parent is not on the server yet | Same function as (a) |
| Parent never syncs | Root diff, clearly labelled | Same | Same | Same |
| Stored meta | Unchanged; `parent_commit` keeps meaning "resolved at snapshot time" | Cannot touch `meta.json` (client-owned, re-pushed); needs a server-owned sidecar or a DB column | Would store a commit whose objects are not in the local repo, breaking local `lineage diff` / `ui` | Unchanged |
| Git-argument safety | Run id checked with `_as_run_id` and a ref-format check; output checked with `_COMMIT_RE` | Same checks, inside the receive lock | New download path to secure | Same as (a) |
| Workspace / project isolation | Lookup only in the child's own workspace root and project repo | Same, plus the trigger must stay inside one workspace | Needs a cross-store fetch API | n/a |
| Caching | None needed (one `rev-parse`, ~ms) | Stored, but must handle refs removed later | n/a | Same as (a) |
| SQLite / Postgres | No database change | Migration on both dialects, or a sidecar file | No | No |
| Hosted service | Same code path; read-only Git on EFS, safe across processes | Extra writes and a children lookup on every receive | n/a | n/a |
| Older clients and stores | Fixed by upgrading the server alone, including runs already synced | Needs a backfill for runs already synced | Needs every worker upgraded; no fix for old runs | Fixed by upgrading the client |

(c) is not viable: at snapshot time the parent may still be running, unsynced
or unreachable; the protocol is push-only, so the client cannot download the
parent's objects; and a `parent_commit` without objects breaks the local diff.
The child's commit is also immutable, so (b) cannot add a Git parent either.
(b) gives the same answer as (a) with more state, a migration, a backfill and
a children lookup. (d) needs nothing extra: it is the same function.

**Recommendation: (a), which also covers (d).** Resolve the parent's ref at
read time when `parent_commit` is empty, in the shared `_lineage_changes`,
exactly like the CLI and as `docs/FORMAT.md` already promises. Add one payload
field so the dashboard can say when the parent's code is missing instead of
showing every file as added. No protocol, meta format or database change. A
server upgrade alone fixes every run already synced.

## Files to touch

- `src/agenticml/ui/server.py`:
  - new helper `_resolve_run_commit(repo, run_id)` next to `_git_numstat`;
  - `_lineage_changes`: fallback described below;
  - `_diff_payload`: new field `parent_source` (`"meta"`, `"ref"` or `null`).
- `src/agenticml/ui/static/app.js` (inspector, ~line 2040): label for a
  parent whose code is missing (see step 3).
- Tests: `tests/test_server_lineage.py`, `tests/test_ui.py`,
  `tests/test_server_workspaces.py`, `testbed/tests/test_task_groups.py`.
- Docs: `docs/FORMAT.md`, `docs/PROTOCOL.md`,
  `website/src/content/docs/server/sync.md`.
- `docs/plans/on-prem-roadmap.md`: "Maintenance fixes" entry with status.
- This plan file.

## Approach

### Step 1: failing test first

Add `test_cross_host_parent_diff` to `tests/test_server_lineage.py`, using the
existing `remote` fixture (one test server) and `authed`:

1. Two stores, `tmp_path / "worker-a"` and `tmp_path / "worker-b"`, and two
   source dirs with the same `util.py` (unchanged) and different `train.py`.
2. Worker A: `RunWriter.create(..., run_id="parent", meta={"project": "demo",
   ...})`, `lineage.snapshot(...)`, `_update_meta`, `finish`.
3. Worker B: same for `"child"` with `parent_run_id="parent"` passed to both
   the meta (top-level `parent_run_id`) and `lineage.snapshot`. Assert the
   child's local `lineage.parent_commit is None`.
4. `push(remote, parent)` and `push(remote, child)`; both complete.
5. `authed(server, "/api/w/default/runs/child/lineage")`: expect
   `parent_commit == parent's commit`, `parent_source == "ref"`, and `files`
   with only `train.py` (`added: 1, deleted: 1`), not `util.py`. Dashboard
   rows have `path`, `added`, `deleted` only (no `status`); compare them with
   the same fields of `lineage.diff(server.root, "child")`.

Run it before the fix and record in the PR that it fails with `util.py`
reported as added and `parent_commit: null`. A throwaway version of this test
was run while writing this plan (Nov 21, 2026, at `v3.3.0`+2): with either
push order the dashboard answered `parent_commit: null` and listed both
`train.py` and the unchanged `util.py`, while `lineage.diff(server.root,
"child")` on the same server repo returned only `train.py` (`M`). So the
server already holds everything needed; only the dashboard lookup is missing. Parametrize the push order
(parent first, child first). A third case pushes only the child: root diff,
`parent_commit: null`, `parent_source: null`, `parent_run_id == "parent"`.
Finally assert the server's `meta.json` for the child is byte-identical to
worker B's (the server rewrote nothing).

### Step 2: server-side fix in `_lineage_changes`

Keep everything before the repo-exists check as it is. After `repo.is_dir()`
succeeds:

1. `parent_source = "meta"` when the stored `parent_commit` is valid.
2. Otherwise, when `parent_run_id` (the same value the payload reports and the
   graph draws) is set and differs from `run_id`, call
   `_resolve_run_commit(repo, parent_run_id)`:
   - Refuse ids that can never be ref names before calling Git: contains
     `..`, ends with `.` or `.lock` (`_RUN_ID_RE` allows these). Return
     "not found".
   - Run `["git", f"--git-dir={repo}", "rev-parse", "--verify", "-q",
     f"refs/runs/{run_id}^{{commit}}"]` with `stdin`/`stderr` to DEVNULL,
     `env=_git_env()`, `cwd=str(repo)`, `timeout=_GIT_TIMEOUT`. The argument
     starts with `refs/`, so it cannot be read as an option; `--verify`
     accepts one object only.
   - Exit 0 and stdout (stripped) matching `_COMMIT_RE`: found. Exit 1:
     not found (checked on Git 2.50: a missing ref and a `..` name both exit
     1 with `-q`). Any other exit or malformed output: "git diff failed".
     `FileNotFoundError`: "git is not available". `TimeoutExpired`: "git diff
     timed out". Errors return the error payload, never a misleading root
     diff.
   - Found: `parent_commit = <sha>`, `parent_source = "ref"`.
   - Not found: `parent_commit = None`, `parent_source = None`, root diff as
     today.
3. The diff command is unchanged; it uses `parent_commit` when set.
4. No lock: refs are published with atomic `update-ref`, and
   `GIT_OPTIONAL_LOCKS=0` is already set. No cache: a ref can appear later
   (parent syncs) or disappear (local `gc`), and the lookup is one short Git
   call made only when `parent_commit` is empty.

The stored `parent_commit` always wins over a live lookup. Only the child's
own workspace store and project repo are searched.

### Step 3: dashboard label

In the lineage inspector (`app.js` ~line 2040), when the run has a parent and
the detail has `parent_run_id` set but `parent_commit` empty and no error,
show `parent code not available` instead of `N files changed · ...`, in local
and server mode. The full list stays available from "Open run". Keep the
375 px no-overflow rule (the text is shorter than today's).

### Step 4: docs

- `docs/FORMAT.md`, lineage section: say that both `agenticml lineage diff`
  and the dashboard run diff resolve `parent_run_id` in the same project repo
  when `parent_commit` is null, and that `parent_commit` keeps meaning
  "resolved at snapshot time". Add a short "Dashboard
  `/api/runs/<id>/lineage`" subsection after "Dashboard `/api/lineage`" with
  the payload fields, including `parent_source`.
- `docs/PROTOCOL.md`, lineage section: one paragraph. The server never
  rewrites `meta.json` or `parent_commit`. A child whose parent ran in another
  store has `parent_commit: null`; once the parent's bundle is on the server
  in the same workspace and project, the dashboard diff uses it. Sync order
  between machines does not matter.
- `website/src/content/docs/server/sync.md`, "Lineage code diffs" bullet: add
  that runs from several machines syncing to one server get correct diffs
  against a parent from another machine, once both have synced (pass
  `--parent` with the parent's run id). The Releases page gets a line at the
  next release, not in this PR.
- Roadmap "Maintenance fixes": entry with where, problem, status.

## Constraints and conventions

- No change to `meta.json`, the v1 store format, the sync protocol, the
  database schema or the hosted deploy. Standard library only.
- Git calls: argument lists only, no shell, `_git_env()`, fixed timeout,
  stderr discarded, every value checked by `_COMMIT_RE` before use.
- Payload change is additive (`parent_source`); the dashboard JS ships with
  the server, so there is no old-JS case.
- Match the surrounding compact style.

## Edge cases

| Case | Expected |
| --- | --- |
| Parent synced after the child | Next request resolves it; the open view refetches when reopened |
| Parent never synced, or missing on the server | Root diff, `parent_source: null`, label "parent code not available" |
| Parent in another project | Its ref is in another repo, so not found: root diff and label. No cross-repo diff |
| Parent in another task, same project | Same repo: resolved normally |
| Parent in another workspace with the same run id | Not visible: lookup is in the child's workspace root only |
| Parent is a descendant (A→B→A, explicit ids) | Each diffs against the other's tree; no traversal, no loop |
| Self-parent (`parent_run_id == run_id`) | No lookup; root diff |
| `parent_run_id` like `a..b`, `x.lock`, `x.` | Refused before Git; root diff |
| Pruned parent: local `agenticml gc` removed run and ref | Not found: root diff and label |
| Parent run dir gone but ref still there (orphan before `sweep_orphans`) | Resolved; the commit is still valid |
| `sync --prune` | Prunes series only; no effect |
| Lineage disabled on the parent | No ref: root diff and label |
| Lineage disabled or failed on the child | Unchanged: "no lineage for this run" / stored error |
| Child's own bundle not yet received (meta arrives first) | Unchanged: "git diff failed" until it arrives (pre-existing; see open questions) |
| Server without Git | Repo dir absent: "code diff not available on this server" (unchanged). Repo left from earlier: "git is not available" |
| Stored `parent_commit` set | Used as is; no lookup (`parent_source: "meta"`) |
| Top-level `parent_run_id` differs from `lineage.parent_run_id` | The lookup uses the top-level one, which the payload and graph show (CLI uses the lineage one; see open questions) |

## Tests and checks

Server (`tests/test_server_lineage.py`, real Git, runs on SQLite locally and
on Postgres in the CI Postgres job through the autouse fixture):

1. `test_cross_host_parent_diff`, parametrized: parent first, child first,
   parent never synced (step 1). Server `meta.json` unchanged.
2. Cross-project: parent synced in project `other`, child in `demo` with
   explicit parent: root diff, `parent_source: null`.
3. Cycle: two stores, A explicit parent B and B explicit parent A: both
   requests answer 200 with a two-tree diff.

Workspace isolation (`tests/test_server_workspaces.py`, `two` fixture): parent
synced into `alpha`, child into `beta` with the same parent id: `beta`'s diff
does not resolve it.

Unit (`tests/test_ui.py`, local `agenticml ui` path):

4. Same store, `parent_commit` null but `refs/runs/<parent>` present (the
   FORMAT.md race): diff against the parent, `parent_source: "ref"`.
5. Stored `parent_commit` wins even when the parent ref points elsewhere.
6. Self-parent, `a..b`, `x.lock`: no Git call (monkeypatch
   `subprocess.run` to fail if called), root diff.
7. `rev-parse` outcomes by monkeypatch: malformed stdout and exit 128 give
   "git diff failed"; `FileNotFoundError` gives "git is not available";
   `TimeoutExpired` gives "git diff timed out"; `files` empty in each.
8. Existing lineage tests keep passing; existing payload assertions gain
   `parent_source`.

Dashboard (`testbed/tests/test_task_groups.py`): a child whose parent has no
snapshot shows `parent code not available`; existing
`test_lineage_inspector_code_changed_without_repo` keeps "code changed";
no horizontal overflow at 375 px.

Checks:

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/`.
- Sonnet read-only review of the diff against this plan, then the git agent
  for commit and PR to `main`.

## Done when

- `test_cross_host_parent_diff` failed before the fix and passes after, in all
  three orders, on SQLite and Postgres.
- The other tests above pass; the full unit and UI suites pass.
- FORMAT.md, PROTOCOL.md and the website sync page describe the behavior.
- The roadmap "Maintenance fixes" entry says done with the PR link.

## Open questions for the maintainer

1. Label: is "parent code not available" (local and server) right, or should
   the inspector keep the all-added list and add a note, or show the label
   only in server mode as other lineage errors are today?
2. Parent id: should the lookup use the top-level `parent_run_id` (what the
   graph and payload show, recommended) or `lineage.parent_run_id` (what the
   CLI uses)? If the first, should the CLI `lineage diff` be aligned in the
   same PR? They differ only for hand-edited or legacy metadata.
3. Cross-project parents: root diff with the label is the plan. Is a
   specific message ("parent is in project X") worth one extra `meta.json`
   read per request?
4. Child commit missing on the server for the short window between its
   `meta.json` and its bundle: keep "git diff failed", or return a clearer
   "code not synced yet" in the same PR?
5. Release: ship as 3.3.1 on its own, or hold for the next minor release?
   agentlab only needs the server upgraded.

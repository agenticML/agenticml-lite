# Fix: lineage diff when the parent ran on another machine

Nov 21, 2026 · Supervisor plan · Maintenance fix after 3.3.0 · Size: S ·
Base branch: `main` (tag `v3.3.0` + release docs).

Status: plan draft, waiting for the owner's review. Nothing is built yet.
Updated Nov 22, 2026: code claims re-checked against `main`, costs measured
(see "Performance and limits"), timeout budget, helper reuse and dashboard
label made precise.

## Goal

A run's dashboard code diff (the `code` row of the lineage inspector, `GET
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
  fetches each run's bundle and publishes `refs/runs/<id>` with a create-only
  `update-ref` (immutable: a different commit for an existing ref is a 409;
  always a commit). Workspace separation comes from `root` being the
  workspace store (`_ctx_root`), not from code inside `receive()`. Both runs'
  commits end up in the same repo when they share a project, whichever
  arrives first (`test_child_before_parent_resends_full_bundle`).
  So on the server `refs/runs/<parent>` exists once the parent has synced.
- `docs/PROTOCOL.md` says the protocol copies the v1 store byte for byte
  without re-encoding records (line ~9) and that `meta.json` is pushed when
  the client's cursor hash differs (sync order, line ~655). The client owns
  `meta.json`: a server edit would diverge from the client's copy and be
  silently replaced by the client's next push. This plan's rule is that the
  server never rewrites it (PROTOCOL.md does not say so for `meta.json` in
  as many words; step 4 adds it).
- The lineage graph (`_lineage_graph`) compares trees (`same_tree_as_parent`),
  reads metadata only and starts no Git process, so "code unchanged" is
  already right cross-host and the graph's cost is not affected by this fix.
  The inspector in `ui/static/app.js` (lines ~2028-2052) fetches the per-run
  diff only for the focused node, only when the trees differ, at most once
  per run per open dialog (per-view `details` map, cleared on close). It
  renders no file list: it builds one text line,
  `N files changed · <first path>`. Lineage errors are shown only in server
  mode (`lineageError = serverMode && detail.error`); in local mode an error
  shows the placeholder "code changed". The `truncated` flag is not shown.
- The "Open run" page does not show this endpoint's file list: its code tab
  uses the run-to-run compare (`/api/compare/code`, `codediff.js`), which is
  out of scope here.
- `_git_numstat` (ui/server.py ~977) streams stdout through `Popen` in 64 KiB
  reads with a 5 s deadline (`_GIT_TIMEOUT`), stops at 500 rows
  (`_LINEAGE_FILES`) or 2 MiB (`_GIT_MAX_BYTES`) and terminates Git there.
  `lineage._resolve_commit` (lineage.py ~245) runs the same `rev-parse` this
  plan needs, but returns `None` for every non-zero exit (cannot tell "not
  found" from "Git failed"), defaults to a 30 s timeout and raises
  `ValueError` on bad ids; `server/lineage.py` `_commit` is a third variant.
- The dashboard server is a `ThreadingHTTPServer` (one thread per
  connection, at most 64 connections, `_MAX_CONNECTIONS`), the same class in
  local `agenticml ui`, `agenticml server serve` and the hosted image. A slow
  Git call holds only its own thread and slot.
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
  - new constant `_GIT_LOOKUP_TIMEOUT = 2` next to `_GIT_TIMEOUT`;
  - new helper `_resolve_run_commit(repo, run_id, timeout)` next to
    `_git_numstat` (why not reuse `lineage._resolve_commit`: see step 2);
  - `_git_numstat(args, cwd, timeout=_GIT_TIMEOUT)`: optional read timeout,
    default unchanged, so the lookup and the diff share one budget;
  - `_lineage_changes`: fallback described below;
  - `_diff_payload`: new field `parent_source` (`"meta"`, `"ref"` or `null`).
- `src/agenticml/ui/static/app.js` (inspector, ~line 2040): label for a
  parent whose code is missing (see step 3).
- Tests: `tests/test_server_lineage.py`, `tests/test_ui.py`,
  `tests/test_server_workspaces.py`, `testbed/tests/test_task_groups.py`
  (local mode), `testbed/tests/test_server_dashboard.py` (server mode).
- Docs: `docs/FORMAT.md`, `docs/PROTOCOL.md`,
  `website/src/content/docs/server/sync.md`.
- `docs/plans/on-prem-roadmap.md`: "Maintenance fixes" entry with status,
  and Backlog entries for the follow-ups in "Performance and limits".
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
   graph draws) is set and differs from `run_id`, start the request budget
   (`deadline = time.monotonic() + _GIT_TIMEOUT`) and call
   `_resolve_run_commit(repo, parent_run_id, timeout=_GIT_LOOKUP_TIMEOUT)`:
   - The id arrives already validated by `_stored_parent` → `_as_run_id`
     (`_RUN_ID_RE`); that is what keeps `^`, `~`, `:`, spaces and similar
     out of `refs/runs/{id}^{commit}`. The helper keeps its own dot-pattern
     check as defence in depth.
   - Refuse ids that can never be ref names before calling Git: contains
     `..`, ends with `.` or `.lock` (`_RUN_ID_RE` allows only
     `[A-Za-z0-9._-]` with an alphanumeric first character, so these dot
     patterns are the only ref-format hazards). Return "not found", the same
     outcome Git gives for them (exit 1), so the pre-check only saves a
     process spawn.
   - Run `["git", f"--git-dir={repo}", "rev-parse", "--verify", "-q",
     f"refs/runs/{run_id}^{{commit}}"]` with `subprocess.run`, `stdin`/
     `stderr` to DEVNULL, `stdout` captured, `env=_git_env()`,
     `cwd=str(repo)`, `timeout=timeout`. The argument starts with `refs/`,
     so it cannot be read as an option; `--verify` accepts one object only.
   - Exit 0 and stdout (stripped) matching `_COMMIT_RE`: found. Exit 1:
     not found (checked on Git 2.50 and 2.34: a missing ref and a `..` name
     both exit 1 with `-q`). Any other exit or malformed output: "git diff
     failed". `FileNotFoundError`: "git is not available". `TimeoutExpired`:
     "git diff timed out". Any other `OSError` (permission denied, EMFILE):
     "git diff failed", as in `_git_numstat`. Errors return the error
     payload, never a misleading root diff.
   - Found: `parent_commit = <sha>`, `parent_source = "ref"`.
   - Not found: `parent_commit = None`, `parent_source = None`, root diff as
     today.
   - Why a new helper instead of `lineage._resolve_commit`: the dashboard
     must tell "not found" (root diff) from "Git failed" (error payload),
     needs a short timeout, and must keep this module's hardening
     (`_git_env()`, stderr discarded, `_COMMIT_RE` on the output).
     `ui/server.py` does not import `agenticml.lineage` today. Say this in a
     one-line comment on the helper so the three variants are not merged by
     mistake later.
3. The diff command is unchanged; it uses `parent_commit` when set. When the
   lookup ran, pass the rest of the budget to the diff:
   `_git_numstat(args, str(repo), timeout=max(0.5, deadline -
   time.monotonic()))`. Without a lookup the call is unchanged
   (`_GIT_TIMEOUT`). This keeps one request's Git read deadline near
   today's 5 s even when the filesystem stalls, instead of 5 s + 5 s. The
   new parameter only feeds `_iter_stdout(proc, timeout)` (ui/server.py
   ~998); `_reap_git` keeps its fixed `_GIT_TIMEOUT` wait-and-kill after a
   timeout (up to 5 s + 5 s, unchanged and out of scope), and the lookup's
   `subprocess.run` adds its own short kill-and-wait on timeout. The
   `max(0.5, ...)` floor is only a guard: after a 2 s lookup about 3 s
   remain.
4. No lock: refs are published with atomic, create-only `update-ref`, and
   `GIT_OPTIONAL_LOCKS=0` is already set. No cache: a ref can appear later
   (parent syncs) or disappear (local `gc`), and the lookup is one short Git
   call (~1 ms measured) made only when `parent_commit` is empty.

The stored `parent_commit` always wins over a live lookup. Only the child's
own workspace store and project repo are searched.

### Step 3: dashboard label

In the lineage inspector (`app.js` ~line 2040), when the run has a parent, the
trees differ, and the fetched detail has `parent_run_id` set, `parent_commit`
empty and no `error`, show `parent code not available` instead of
`N files changed · ...`, in local and server mode.

Build this from the detail's fields directly, as its own branch of the
`code` expression (inside the existing `parent` / `!node.same_tree_as_parent`
branches) placed before the `files` branch:
`detail && !detail.error && detail.parent_run_id && !detail.parent_commit
? "parent code not available"`. Test `!detail.error` by truthiness, not
`typeof ... === "string"`: a failed fetch stores `{ error: true }`
(app.js ~2036). Do not route it through `lineageError`: that is gated on
`serverMode`, so local mode would keep showing "code changed". While the
request is in flight (`details` value `null`) or after a failed fetch, keep
today's "code changed" placeholder.

This endpoint's file list is not shown anywhere else in the dashboard ("Open
run" uses `/api/compare/code`), so the label replaces only the count line; the
full numstat list stays available from `agenticml lineage diff` and the API.
Keep the 375 px no-overflow rule (the text is shorter than today's).

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
- Roadmap Backlog: add the follow-ups listed under "Performance and limits"
  (loading state, retry after a failed fetch, showing `truncated`, packing
  server refs, hosted EFS timing) with where, problem, fix and done when.

## Performance and limits

Measured Nov 22, 2026 on a local NVMe disk, Git 2.34.1, warm page cache,
20 repetitions (median), through the real `_git_numstat` where noted. Hosted
EFS was not measured; see the last row.

| What | Today (parent in another store) | After the fix |
| --- | --- | --- |
| Parent lookup (`rev-parse`) | none | ~1 ms found or not found; 1.2-1.7 ms with 10,000 run refs (loose or packed) |
| Diff, 20,000 files, 1 file changed | 187 ms root diff (loose objects), 64 ms packed | 1.3 ms (~140x less) |
| Diff, 20,000 files, 5% changed | 189 ms | 31 ms loose, 13 ms packed (~6x less) |
| Diff, 2,000 files, 1 file changed | 22 ms | 1.2 ms |
| Parent never synced | root diff | same root diff + ~1 ms |
| Stored `parent_commit` set | unchanged | unchanged, no lookup |
| Git child peak RSS, 20,000 files | 13 MB (root diff) | 4.6 MB (parent diff), 4.1 MB (`rev-parse`); the two never overlap |
| Python-side memory per request | bounded by 500 rows / 2 MiB cap | same bound; no cache or stored state added |
| JSON payload for a cross-host child | ~39 KB (500 "added" rows at the cap) | ~0.3 KB for one changed file |
| Worst case on a stalled filesystem | 5 s read deadline | read deadline stays 5 s, shared by lookup and diff (step 2); without sharing it would be 5 s + 5 s. Not covered: the existing wait-and-kill in `_reap_git` (up to 5 s + 5 s after a timeout), unchanged |

Notes:

- The root diff is slow because `diff-tree --numstat` reads every blob to
  count lines and writes its output only at the end; stopping at 500 rows
  saves transfer, not Git's work. A parent diff reads only changed paths.
- The fix adds no request: the dashboard still fetches one diff per focused
  run, once per open dialog, and renders one text line (no per-file DOM), so
  display cost does not change. The lineage graph starts no Git process.
- Concurrency: up to 64 connections each running one Git child. Today's root
  diff at the 20,000-file limit is ~13-17 MB per child, so many concurrent
  cross-host requests could approach the hosted task's 1 GB; the fix lowers
  this for resolved parents.
- Hosted EFS: every file open is a network round trip, which hurts the root
  diff (thousands of reads) far more than the lookup (a few small reads), so
  the gain should be larger there. Not measured; an optional owner check is
  listed under "Tests and checks".

Existing gaps the fix does not change (backlog, not this PR): no loading
state (the inspector shows "code changed" until the answer lands), no retry
after a failed fetch in an open dialog, the `truncated` flag is not shown
(a capped list reads "500 files changed"), and loose refs make `diff-tree`
slower in repos with thousands of runs (a periodic `pack-refs` on the
server's shadow repos would help).

## Constraints and conventions

- No change to `meta.json`, the v1 store format, the sync protocol, the
  database schema or the hosted deploy. Standard library only.
- Git calls: argument lists only, no shell, `_git_env()`, bounded read
  timeout (lookup and diff share one `_GIT_TIMEOUT` read deadline;
  `_reap_git` is unchanged), stderr discarded, every value checked by
  `_COMMIT_RE` before use.
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
   `server.subprocess.run` to fail if called), root diff. `subprocess.run`
   on the module is the intended seam; existing tests that patch
   `server.subprocess.Popen` would also reach the lookup when
   `parent_commit` is empty, so keep their fixtures' `parent_commit` set.
7. `rev-parse` outcomes by monkeypatching `server.subprocess.run`:
   malformed stdout, exit 128 and a generic `OSError` give "git diff
   failed"; `FileNotFoundError` gives "git is not available";
   `TimeoutExpired` gives "git diff timed out"; `files` empty in each.
8. Timeout budget: call `server._lineage_changes(root, run_id, meta)`
   directly with `_resolve_run_commit` and `_git_numstat` stubbed, and a
   mutable clock (`[100.0]`) patched into `server.time.monotonic`. The
   lookup stub advances the clock by 1.5 and returns a sha. Assert the
   lookup got `timeout == _GIT_LOOKUP_TIMEOUT` and the numstat stub got
   `timeout == pytest.approx(_GIT_TIMEOUT - 1.5)`; in a no-lookup case
   assert `timeout == _GIT_TIMEOUT`.
9. Top-level `parent_run_id` differs from `lineage.parent_run_id`
   (`_seed_run(parent_run_id=...)` with `_lineage_meta(parent_run_id=
   <other>)`): the lookup uses the top-level one (the id the payload
   reports).
10. Existing lineage tests keep passing; existing payload assertions gain
    `parent_source`.

Dashboard: seed a child with `lineage.parent_commit = null`,
`parent_run_id` set and a tree different from its parent, plus either a real
bare repo holding the child commit (the `commit-tree` approach in
`tests/test_ui.py` ~1363-1400) or a mocked `/lineage` answer
(`page.route`). The existing fixtures (`_lineage_project`, `_lineage_pair`)
write a fake `parent_commit` and no repo, so they answer with an error and
cannot show the label. Assert `parent code not available` in local mode
(`testbed/tests/test_task_groups.py`) and in server mode
(`testbed/tests/test_server_dashboard.py`, `_lineage_pair` / `sign_in`
pattern). Existing `test_lineage_inspector_code_changed_without_repo` keeps
"code changed"; no horizontal overflow at 375 px.

Optional owner check (hosted, not a CI step): on the hosted task, time one
`rev-parse --verify -q refs/runs/<id>^{commit}` and one `diff-tree --root`
on a real workspace repo on EFS, and record the numbers in this plan.

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
- The roadmap "Maintenance fixes" entry says done with the PR link, and the
  follow-ups from "Performance and limits" are in the roadmap Backlog.

## Open questions for the maintainer

1. Label: is "parent code not available" (local and server) right, or should
   the inspector keep the all-added list and add a note, or show the label
   only in server mode as other lineage errors are today? If the label is
   accepted, the server could also skip the root diff when a parent id is
   set but not found (return `files: []`, `parent_source: null`), saving
   the 20-190 ms root diff on every such request. That changes the API
   answer for those runs from "all added" to "no files", so it is not in the
   plan unless you choose it.
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

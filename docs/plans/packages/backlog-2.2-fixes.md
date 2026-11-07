# Version 2.2.0 and Backlog fixes after 2.2

Nov 7, 2026 · Supervisor plan · Release 2.2 · Status: done · Ships in the 2.2 PR
([#38](https://github.com/agenticML/agenticml-lite/pull/38)).

Source: the Backlog in [on-prem-roadmap.md](../on-prem-roadmap.md). Four
tasks in two parallel tracks with disjoint files.

| Task | Coder | Files |
| --- | --- | --- |
| A1 Version 2.2.0 | Codex | `pyproject.toml`, `src/agenticml/__init__.py`, `README.md`, `docs/SERVER.md`, `website/src/content/docs/**` |
| A2 Transient Git failures answer 503 | Codex | `src/agenticml/server/lineage.py`, `tests/test_server_lineage.py` (and `sync.py` only if the client mishandles 503) |
| B1 Runs do not track their own store | Cursor | `src/agenticml/code_tracking.py` (and the SDK code that starts the collector), `tests/test_code_tracking.py` |
| B2 Last-admin-key rule counts usable keys only | Cursor | `src/agenticml/server/keys.py`, `tests/test_server_keys_users.py` or `tests/test_server_keys_web.py` |

Not in this batch: the real-tailnet and real-EFS checks (they need the
owner's machines), and dropping the server-side owner compatibility rule
(it must wait until no 2.0/2.1 clients remain; package 12 already made 2.2
clients compare `owner_user`). The roadmap entry for the latter is updated
to say so.

## A1 Version 2.2.0

- `pyproject.toml` `version = "2.2.0"` and `agenticml.__version__ =
  "2.2.0"`.
- Download URLs that pin a tag (`.../agenticml-lite/v2.0.0/deploy/...`) in
  `README.md`, `docs/SERVER.md` and `website/src/content/docs/**` point to
  `v2.2.0`. Only the repository has tag `v1.0.0` today, so the release step
  tags `v2.2.0`; the docs must name the tag that will contain the 2.2 unit
  files.
- `README.md` release example (`release: v2.0.0`, `git tag v2.0.0`) uses
  `v2.2.0`.
- Leave `website/package-lock.json` and other third-party version strings
  alone.
- Check: `grep -rn "2\.0\.0"` outside lock files and plans returns nothing
  that names the AgenticML version; `tests/test_smoke.py` passes.

## A2 Transient Git failures during a lineage receive answer 503

Backlog entry: "Transient Git failures during a lineage receive become
permanent skips". Today `lineage.receive` maps any `CalledProcessError` from
`index-pack`, `rev-list` and `cat-file` to 422 `invalid bundle`, which the
client records as a permanent lineage skip.

- Before unpacking, check free space on the filesystem holding the repo
  (`shutil.disk_usage(repo)`): if free space is below twice the bundle size
  plus 64 MiB, answer 503 `lineage busy` (log "not enough free space for
  lineage receive").
- Run every Git command with `LC_ALL=C` (check what `_git` sets today) and
  classify a `CalledProcessError` as an environment failure, answered 503
  `lineage busy`, when its stderr contains one of: `No space left on
  device`, `Disk quota exceeded`, `Read-only file system`, `Cannot allocate
  memory`, `Out of memory`, `unable to create temporary`, `Too many open
  files`, `Resource temporarily unavailable`, `Input/output error`.
  Anything else stays 422 `invalid bundle`. Keep one helper for this.
- Keep every existing rollback path (promoted packs, published ref).
- Client: confirm `sync.py` treats a 503 from `PUT /v2/runs/<id>/lineage`
  as retryable and does not record a skip; fix it only if it does not.
- Tests: make `index-pack` fail with a simulated ENOSPC (stderr "fatal:
  write error: No space left on device", e.g. via a fake `git` wrapper or
  monkeypatching `_git`) → 503 and no ref, no promoted packs; the same with
  a genuinely corrupt pack → still 422; the free-space pre-check → 503; and
  a sync test showing the client retries later and records no skip.
- Remove the Backlog entry.

## B1 Runs do not track their own store files

Backlog entry: "Runs track their own store files when the store is under
the project root". Runtime-read tracking (`code_tracking.py`,
`_audit_dispatch` → `RuntimeFileCollector._record_path(path,
"runtime_read")`) records every file opened for reading under the project
root, including the run's own `run.lock`, `events.jsonl` and other files in
`AGENTICML_DIR` when that directory lies inside the project root.

- Exclude the AgenticML store root (the resolved `AGENTICML_DIR` of the run
  being tracked, and anything under it) from runtime-read tracking, using a
  resolved-path containment check that does not follow symlinks
  differently from the existing path checks in `code_tracking.py`.
- Also exclude it from any other source that walks or snapshots files under
  the project root, if one would pick it up (check the module; report what
  you found).
- Tests: a project root (git repo in `tmp_path`) whose run store is a
  subdirectory of it; start a run, log, finish; the run's code manifest and
  `events.jsonl` contain no path under the store. A store outside the
  project root behaves as before; a normal project file read at runtime is
  still tracked.
- Remove the Backlog entry.

## B2 Last-admin-key rule counts only usable admin keys

Backlog entry: "Dashboard 'last admin key' rule ignores demoted users".
`KeyStore.revoke(keep_admin=True)` counts every admin-scope key plus the env
key, including keys whose user is no longer an admin (they cannot sign in).

- Under the existing lock, count admin-scope keys whose user exists in
  `users.json` with role `admin`, plus the env key. Refuse
  (`LastAdminError`) only when the key being revoked is itself usable (its
  user is an admin) and it is the last usable one. Revoking an unusable
  admin key (a demoted user's) is always allowed.
- The CLI path (`keep_admin=False`) is unchanged.
- Tests (done-when from the Backlog): one demoted admin key and one live
  admin key → revoking the live one from the dashboard is refused; revoking
  the demoted one is allowed; with two live admin keys either can be
  revoked.
- Remove the Backlog entry.

## Checks

- `PYTHONPATH=src /home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv/bin/python -m pytest -q tests`
  from the worktree root (run Codex/Cursor from the root, not `testbed/`).
- No dashboard change, so the `ui` suite is not required.
- Each coder reports deviations instead of changing the approach.

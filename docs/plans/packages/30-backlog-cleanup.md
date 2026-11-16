# Package 30: Backlog cleanup before the 3.0 release to `main`

Nov 17, 2026 · Supervisor plan · Release 3.0 · Status: in review ([#79](https://github.com/agenticML/agenticml-lite/pull/79))

The AWS roadmap is done (PR #77). Before `dev` is merged into `main` and
3.0.0 is tagged, every entry in the roadmap Backlog is either fixed,
turned into a precise owner checklist, or moved to "Later" with a reason.
This plan was written from a triage of all 27 entries against the code on
`dev` at `cb48cb5` (six read-only investigators, each checked by an
independent critic). The coder gets this plan and the triage notes for its
part; where the notes and this plan disagree, this plan wins, and the
critics' corrections listed here are part of the plan.

## Outcome per Backlog entry

| Backlog entry | Outcome | Part |
| --- | --- | --- |
| Sync client trusts its own cursor after a failed atomic upload | fix | 30.1 |
| Restore drill: a sync during the EFS backup window | documented repair + tests | 30.1 |
| Compatibility owner rule can mislead a 2.0/2.1 client † | remove `compat_owner` | 30.1 |
| Fix two doc texts that do not match the code | fix + guard tests | 30.1 |
| Account deletion tooling † | `server accounts delete` | 30.2 |
| Workspace delete cleanup can skip steps † | fix | 30.2 |
| Retry object cleanup after a workspace deletion fails † | `workspaces orphans` / `cleanup` | 30.2 |
| A removed member's login can be re-added by another workspace † | fix | 30.2 |
| Admin plan edits: concurrent merge and free→custom defaults | fix | 30.3 |
| Keys page copies the key onto the command line | fix | 30.3 |
| Metric picker polish | fix | 30.3 |
| Workspace rename by its owner † | `PATCH /api/w/<slug>` + Rename | 30.3 |
| Setup and invite pages in the new sign-in style | fix | 30.4 |
| Theme on the sign-in pages | fix | 30.4 |
| Firebase sign-in page test gaps | tests | 30.4 |
| ALB access logs keep invite tokens † | token in the URL fragment on hosted | 30.4 |
| Remaining package 19.1 second-review notes | transaction cap + test + record | 30.5 |
| Many-process follow-ups for package 24 | cap, size memo, DB clock; lock pooling → Later | 30.5 |
| ECS Exec hardening | init process; no session logging | 30.6 |
| Hosted alarm and action pinning follow-ups | fix | 30.6 |
| Hosted service monitoring for the public | weekly workflow | 30.6 |
| Permissions boundary for the hosted stack's roles † | Later (own package; needs an owner-run bootstrap apply before any hosted deploy) | 30.7 |
| Docs custom domain | stays, owner first; not needed for 3.0.0 (github.io redirects to the custom domain later) | 30.7 |
| Release 3.0.0: tag and publish (owner) | stays; release checklist added | 30.7 |
| Two-task rolling deploy check | stays (owner); checklist | 30.7 |
| On-demand EFS backup refused for the operator session | stays (owner); diagnosis checklist | 30.7 |
| Check Firebase sign-in against the real Firebase project | stays (owner); checklist | 30.7 |
| Check `agenticml server up` on a real tailnet | stays (owner); checklist | 30.7 |

† security-sensitive: needs a second review. With Codex and Cursor
disabled, the supervisor does it (owner's rule, Nov 5, 2026) and records it
in the PR.

Also fixed in 30.7: the stale 29.5b row (PR #69 merged Nov 16) and the
status of package 26.

## Order, branches and checks

Wave 1 runs in parallel, each part in its own worktree: 30.1, 30.2, 30.6.
Wave 2 after wave 1 is merged into the package branch: 30.3, 30.4, 30.5.
30.7 last, by the supervisor. Parts in one wave touch disjoint source files;
docs files may overlap and are merged by hand.

Every part:

- the coder (Sonnet) creates its own venv in its worktree
  (`cd testbed && uv venv --python 3.10 && uv pip install -e '..[dev]'
  playwright==1.62.0 && uv pip install -e '.[test]'`) so tests import its
  own `src`;
- runs `testbed/.venv/bin/python -m pytest -q tests` from the root and, for
  any dashboard or sign-in page change, `.venv/bin/python -m pytest -q -m ui`
  from `testbed/`; Postgres-relevant parts also run the Postgres tests with
  `AGENTICML_TEST_DATABASE_URL` pointing at the local test container
  (`postgres:16` on port 55432, started by the supervisor; never the `pg`
  container on 5432);
- keeps zero horizontal overflow at 375px on changed pages;
- removes its Backlog entries from `docs/plans/on-prem-roadmap.md` only in
  30.7 (the supervisor edits the roadmap, to avoid conflicts);
- does not commit; the supervisor reviews, and the `git` agent commits.

## 30.1 Sync integrity and doc truth (Sonnet)

Files: `src/agenticml/sync.py`, `src/agenticml/server/ingest.py`,
`tests/test_sync.py`, `tests/test_server_ingest.py`, `tests/test_client.py`,
`tests/test_server_users.py`, `tests/test_server_limits.py`,
`docs/PROTOCOL.md`, `docs/SERVER.md`, `docs/HOSTED.md`,
`docs/THREAT_MODEL.md`, `scripts/restore_drill/README.md`,
`deploy/aws/README.md`.

1. **Failed atomic upload drops the cursor.** Add `_put_atomic(reader,
   client, state, save, name, body)` in `sync.py`: on any `BaseException`
   from `client.put_file`, pop `state['files'][name]`, save (a failing save
   is logged with `logging.getLogger(__name__)` and the original error is
   re-raised), then for `Conflict` run `_check_owner` and re-raise. Use it in
   `_sync_locked.atomic()` and in `_push_byref` (`artifacts.json`, same
   bug). Append files and lineage are unchanged. A comment names the
   residual: a kill between the server's rename and the client's `except`
   still skips the drop; any `/state` client recovers.
   Tests: put_file applies then raises `ConnectionError` → cursor gone,
   restoring the old local bytes and re-syncing with the same `sync.json`
   leaves the server with the old bytes (`identical`); same for `meta.json`
   and by-reference `artifacts.json`; a `Conflict` from a foreign owner
   still raises `OwnershipError` and drops the cursor; a first-ever failed
   upload does not call `save()`. Close writers in `finally`.
2. **Restore drill: sync during the backup window.** No server change.
   Tests in `tests/test_sync.py`: after a push, damage the server copy
   (truncate `scalars/0.bin`, remove it, or rewrite `scalars/index.json`
   with same-length different JSON) for a running and a finished run;
   `admin.verify` (plain verify; `--deep` only adds object re-hashing) is
   not ok; `sync --reset <run>` then `sync <run>` makes it ok with no
   problems and `identical` holds. Also pin the limits: a finished run
   without `--reset` stays `up to date` and broken; a running run with no
   new local bytes is not repaired by a plain sync; a run synced with
   `--prune` whose server file is short stays a `size_mismatch` after
   `--reset` (no local data). The changed-atomic case is healed by any
   `/state` call; assert `not ok` before any client call.
   Docs: `scripts/restore_drill/README.md` step 1 replaces the "Limit"
   paragraph: record the backup window per drill (Nov 16: 05:00–08:03, the
   first EFS copy is full; later ones are incremental); window problems
   (`size_mismatch`, `file_hash` for runs synced inside it) are expected;
   repair = the client that holds the run runs `agenticml sync <run>
   --reset` then `agenticml sync <run>`, for running and finished runs,
   once per workspace key (`--reset` clears that host for every
   workspace); pruned runs or runs no client still has cannot be repaired:
   record them and leave the warning or delete the run. Add "expected after
   repair: 0 problems" to the record section. Same repair in one paragraph
   each in `deploy/aws/README.md` (restore-drill policy) and `docs/HOSTED.md`
   ("Backup and disaster recovery"); one line in `docs/SERVER.md` "Backups
   and restore". Keep the 05:00 UTC schedule; no CloudFormation change.
3. **Remove `compat_owner` (†).** Clients older than 2.2 are refused with
   426 by default (`clientwindow.DEFAULT_MIN_CLIENT = '2.2'`) and 2.x was
   never on PyPI, so 3.0 drops the shim. Replace it with
   `owner_fields(owner_user)` returning the real user in both `owner` and
   `owner_user`; keep the `owner_key`/`last_sync_key` hiding for other
   users exactly as is. Update the tests that pinned the key name
   (`test_server_ingest.py` ~690 and ~1094, `test_client.py` ~167,
   `test_server_users.py` `test_member_ownership_and_compat_state` →
   `test_member_ownership_and_state`); do NOT change `tests/test_sync.py`
   ~814 (that is client-side `sync.json`). New tests: own run read with a
   key whose name differs from the user returns `owner == user`; a user
   whose key is named like another user still gets 409 with the real owner
   (this pins behaviour; it did not change); `not hasattr(ingest,
   'compat_owner')`; an end-to-end sync with such a key is refused with
   "belongs to user". `docs/PROTOCOL.md`: fix every place that shows or
   describes the key name in `owner` (examples ~329/338, ~158, ~395, ~431,
   ~485, ~517, the "Compatibility rule" bullet ~351–357, and ~748–751),
   and state that before 3.0 `owner` carried the key name for the caller's
   own runs and that clients older than 2.2 are unsupported. `docs/SERVER.md`
   `--min-client-version` row and the 2.x → 3.0 upgrade section: values
   below 2.2 are unsupported (2.0/2.1 clients misreport their own runs).
4. **Doc texts.** `docs/SERVER.md` "Usage limits and quotas": the JSON
   example becomes exactly `docs/examples/limits.json`, with a sentence on
   the exact keys and `free_workspaces_per_account`; the enforcement list
   names `free_workspaces_per_account`. Suspended workspaces: dashboard and
   `/api/w/<slug>/...` answer 404 like an unknown workspace; sync keys get
   403 `workspace suspended` on every `/v2` route including `whoami`; fix
   `docs/HOSTED.md` (~183), `docs/SERVER.md` (~162) and
   `docs/THREAT_MODEL.md` (~35). Guard tests in `tests/test_server_limits.py`:
   the SERVER.md block (first ```json block under the heading, raw-string
   regex, assert found) loads with `Plans.load` and equals the example file
   (including `free_workspaces_per_account`); the HOSTED.md "Suspending a
   workspace" section (scoped to that section) mentions 404 and 403
   `workspace suspended`.

Done when: the tests above pass on SQLite; `tests/test_server_*` pass on
Postgres; docs match the code.

## 30.2 Account and workspace lifecycle (Sonnet, †)

Files: `src/agenticml/server/db/workspaces.py`, `db/repos.py`,
`src/agenticml/server/workspaces.py`, `admin.py`, `objectstore.py`,
`users.py`, `userspage.py`, `routes.py` (the `_invites` status mapping
only), `webauth.py` and `up.py` (operator flag only), `src/agenticml/cli.py`,
tests, `docs/SERVER.md`, `docs/HOSTED.md`, `deploy/aws/README.md`,
`scripts/restore_drill/README.md`, `website/src/content/docs/server/keys.md`.
No migration in this part.

1. **`agenticml server accounts delete LOGIN [--yes-delete LOGIN]
   [--json]`.** `AccountRepo.delete(login)` in one write transaction
   (`cross_workspace`, `advisory_lock('users')`):
   - refuse (new `AccountDeleteBlocked(ClientError)`, nothing changed) when
     the account is a server admin ("run set-server-admin LOGIN off first
     and remove it from the admin emails list"), owns any workspace (list
     the slugs; "transfer with `workspaces set-owner` or delete them first"),
     or owns runs (list workspace slugs and counts; "delete those runs'
     workspaces or the runs first"). The runs refusal replaces the
     "anonymise in place" idea: `reconcile` re-creates a login from the run
     files' remote metadata, so anonymising would not erase the person.
     Also refuse logins that equal a reserved actor word (`cli`, `setup`,
     `server up`, `legacy run owner`, a role name).
   - delete the account's sessions (by account and by its keys' digests),
     keys, pending invites addressed to the login (all workspaces) and
     memberships; null `workspaces.created_by`; replace the login in
     `accounts.created_by`, `keys.created_by` and `invites.invited_by` with
     `[deleted account]`; delete the account row.
   - `AuditRepo.redact_account` rewrites server-level rows only
     (`workspace_id IS NULL`): `actor` or `target` equal (case-insensitive)
     to the login, email, identity or Firebase uid → `[deleted account]`,
     and `client` set to NULL on rows the person acted in. In `detail`,
     replace whole-string values only under keys that hold people (find the
     keys the code writes: e.g. `owner`, `login`, `user`, `email`,
     `invited_by`, `name`, `new_owner`, `old_owner`); replace substrings only
     for needles that contain `@` (emails), with the boundary regex. The
     SQL prefilter must also match the JSON-escaped form of non-ASCII
     needles (`json.dumps(needle)[1:-1]`), or scan in Python. Workspace-level
     rows are not touched (the policy keeps them with the workspace); the
     command lists the person's former workspaces.
   - CLI: add `delete` to the parser (fix the parser's trailing `else` to
     `elif action == 'set-server-admin'`, and the dispatcher's too); prompt
     "Type the account login to delete it:" unless `--yes-delete`; add
     `EOFError` to the accounts handler; after commit record one
     server-level `account.delete` event whose target is the account id and
     whose detail has counts only. Print the Firebase uid and email with
     "delete it in the Firebase console (Authentication > Users)".
   - Runbook section "Deleting an account (privacy request)" in
     `deploy/aws/README.md`: find the login (`accounts list --json`), delete
     or transfer the person's workspaces, turn off server admin, delete the
     Firebase user FIRST (otherwise open sign-up re-creates the account),
     then the command over ECS Exec; ALB (90 days) and server logs (30 days)
     are not touched; personal workspace names derive from the email, so
     delete the personal workspace rather than transfer it. One line each
     in `docs/SERVER.md` and `website/src/content/docs/server/keys.md`.
     Leave the Privacy policy text alone (owner approval); say in the
     report whether its wording still matches.
   - Tests (new `tests/test_server_accounts_delete.py`): removes everything
     and leaves other members untouched; server audit redaction (actor,
     target, client, nested detail, a short login like `bob` does not touch
     `bobby` or `{"role": "admin"}`-style values, non-ASCII email);
     refusals (server admin, owner, runs, reserved word, unknown, wrong
     confirmation, EOF on stdin → exit 1, no traceback); `--json` and the
     Firebase uid line; `created_by` scrubbing; after delete, `reconcile`
     does not resurrect the login; the Postgres variant (sessions with NULL
     workspace, invites in other workspaces, NULL-workspace audit rows
     under RLS) runs when `AGENTICML_TEST_DATABASE_URL` is set.
2. **`delete_workspace` never skips a step.** `discard_store` returns True
   when `workspaces/<id>` is gone afterwards (also already missing, or a
   symlink it unlinked), False when anything is left or the safe path was
   skipped. `delete_workspace(db, data, slug, *, objects=None,
   on_deleted=None)`: require the row; delete the DB row (the `default`
   refusal still raises first); call `on_deleted(row)` at once, guarded (the
   CLI writes the `workspace.delete` audit event there); files step with
   `discard_store` (guarded; `fsync_dir` only on success); objects step
   independent of the files result; return `files_cleanup_failed` and
   `object_cleanup_failed`. Drop `prepare_store` here (it mkdirs); this
   changes behaviour for a symlinked store (the DB row is deleted and the
   link unlinked instead of an error before deletion): test it. CLI: the S3
   warning becomes `Warning: object cleanup failed for workspace <id>; retry
   with: agenticml server workspaces cleanup <id>` (replacing "; bytes are
   orphaned"), a files warning of the same shape, and a
   `workspace.cleanup_failed` server event; exit 0 (the workspace is
   deleted), and the runbook says to check stderr. Callers that ignore
   `discard_store`'s result (routes.py, webauth.py) keep working; keep
   `tests/test_server_signup.py` symlink tests passing.
3. **`workspaces orphans` and `workspaces cleanup`.** `S3Objects.workspace_ids()`
   lists `CommonPrefixes` under the base prefix (paged; only 32-hex names;
   empty base prefix works); the base class returns `[]`.
   `admin.orphan_workspaces(data, db, backend)` lists `data/workspaces/*`
   and the bucket prefixes FIRST, then snapshots the DB ids; orphans are
   listed ids not in the snapshot. `verify` (only when no `--workspace` is
   given) and `status` add warnings `workspace_orphan_files` /
   `workspace_orphan_objects` with the cleanup command; an S3 listing
   failure is a warning in both, never an exception. CLI: `workspaces
   orphans [--objects URL] [--json]` (read-only) and `workspaces cleanup
   (ID | --all --yes-delete all) [--objects URL]`: 32-hex ids only, refuse
   ids that are in the DB, files and objects steps independent, re-check
   the DB right before each id, exit 1 if anything failed, server event
   `workspace.cleanup`; with the file backend do not claim objects were
   cleaned. Runbook and `docs/HOSTED.md`: the retry flow; the restore-drill
   README: orphan warnings are expected after restoring an older database,
   and `cleanup --all` must never be run against a restored or drill
   database. Tests per the triage notes (moto S3: failed cleanup retried
   removes only that prefix; foreign names ignored; refusals; verify/status
   warnings; listing-before-snapshot order, implemented by making
   `WorkspaceRepo.list` create a row and directory when called and
   asserting it is not reported; file-backend files-only orphan).
4. **Detached accounts.** An account with no membership ("detached") may be
   attached to a workspace over HTTP (`POST /api/w/<slug>/members`,
   `POST /api/w/<slug>/invites`) only by an admin of the workspace it last
   left; anywhere else, and always for a detached server admin or an
   account that never had a membership, only an operator can attach it (CLI
   `users add`, `/setup`, `server up`). Record the workspace an account last
   left in `server_settings` (key `detached-from:<account id>`, value the
   workspace id) when `UserRepo.remove` removes its last membership; clear
   it when the account is attached again. Implement in `UserRepo.add` (move
   the existing-account lookup above `check_people`; keep the legacy run
   owner branch first) and `InviteRepo.create`, both with an `operator`
   keyword (default False); pass `operator=True` from cli.py `users add`,
   webauth.py setup and up.py. `import2x` uses `UserRepo.save` and needs no
   change. HTTP: 403 with `DETACHED_ACCOUNT = 'this account is not in any
   workspace; ask a server admin to add it'` in `userspage.py` and
   `routes.py` `_invites`. Tests: removed from A, re-added by B's admin
   (members and invites routes) → 403, credentials unchanged, no
   membership; re-added by A's admin → allowed (pins
   `test_readding_member_preserves_account_and_run_ownership` and the
   sign-in tests that remove and re-add); operator CLI re-add works; an
   `accounts add --server-admin` account cannot be claimed by a workspace
   admin; an account with a membership elsewhere can still be added as
   before. `docs/SERVER.md` members section and the runbook: the rule and
   the operator command.

Done when: all tests pass on SQLite and Postgres; supervisor second review
recorded.

## 30.3 Admin plan edits and dashboard (Sonnet; rename †)

Files: `src/agenticml/server/db/workspaces.py`, `routes.py`, `users.py`,
`src/agenticml/cli.py` (set-plan only), `src/agenticml/ui/static/app.js`,
`app.css` only if needed, tests, `testbed/tests/*`, `docs/SERVER.md`.

1. **Plan edits.** `WorkspaceRepo.set_plan(..., merge=False,
   free_limits=None)` merges partial custom limits inside its own write
   transaction from the row it reads there (custom → own limits; free →
   `free_limits` or `UNLIMITED`); non-merge callers keep the strict
   all-keys contract (test it). The admin PATCH and CLI `set-plan` use
   `merge=True` (the CLI keeps one read only for its "requires at least one
   limit flag" message; test it). GET `/api/admin/workspaces` adds
   top-level `free_limits` (null without plans); per-row `limits` stays
   null for free. `editPlan` prefills the free limits when switching a free
   workspace to custom. Docs: partial PATCH merges atomically; the
   dashboard sends all four fields (last write wins between two dashboard
   admins). Concurrency test through two concurrent HTTP PATCH requests
   (and a direct-transaction variant using raw `tx.execute`, since nested
   transactions raise), plus Postgres.
2. **Keys copy command.** The new-key dialog copies `agenticml login --host
   <host>` with no `--key`; add a muted hint "Run this command, then paste
   the key when it asks for it." Keep "Copy command" and "Copy key". Update
   the five tests; new negative assertion on the `<code>` element only; run
   the dialog-fit test at all its widths.
3. **Metric picker.** Refresh `/metrics` every third `refresh()` (15 s
   while live) without awaiting it, and fetch it in parallel with the first
   `/runs`; rebuild the `<select>` only when the list changed; keep the old
   list on failure; keep `preferredMetric` (the saved choice) so it returns
   when first logged, and make `saveMetricPref()` write `preferredMetric`.
   Do not reset the picked metric while the names list is still empty, so
   the first `/runs` carries the saved metric (one request on load, as
   today). UI tests: a metric logged after load appears without reload
   (fresh `last_sync_at`; `page.clock` plus `expect` waits, not back-to-back
   fast-forwards); the 422 fallback (route stub, `allow_http_errors`); the
   250 ms debounce. Idle-page polling stays out (Later entry).
4. **Workspace rename (†).** `ACTIONS['rename'] = 'admin'` and the client
   mirror in `app.js`, with a test that the two agree. `WorkspaceRepo.rename(
   workspace_id, name)` takes `advisory_lock('users')`, trims, rejects empty,
   over 256 and control characters, and changes only `name`; apply the same
   validator in `update()` so both paths agree. Route `PATCH /api/w/<slug>`
   (no trailing-slash alias), body exactly `{"name": str}`, role admin,
   CSRF as other PATCH routes, audited at workspace level and server level
   with the admin route's detail shape (`slug`, `name`) plus `old_name`.
   Settings > Workspace card gets a Rename button (owner and admin) with a
   dialog prefilled with the trimmed name; update the cached session and
   visible names in place. Isolation suite: add the route to EXAMPLES with
   an idempotent body; check no later test depends on fixture names. Tests:
   owner and admin allowed, member and viewer 403, non-member 404, admin-scope
   key session allowed, audit entries, validation, 375px. Docs: role table
   row and Settings text in `docs/SERVER.md`.

Done when: unit, UI and Postgres tests pass; 375px clean; supervisor
second review of the rename recorded.

## 30.4 Sign-in pages (Sonnet; token change †)

Files: `src/agenticml/server/static/*.html`, new `static/theme.js`,
`static/invite.js`, `static/setup.js`, `static/firebase-login.js` (token
read only), `app.css`, `webauth.py` (asset and existing-account page),
`userspage.py`/`keyspage.py` (`_invite_url`), `routes.py` (invite link
builder only), `cli.py` (`users invite` link only), tests,
`testbed/tests/*`, `deploy/aws/README.md`.

1. **Setup and invite pages in the `.auth-*` shell**, byte-for-byte brand
   panel and compact logo from `login.html`; keep ids and behaviour; move
   the existing-account invite page from an inline string to a static file;
   `_unknown_identity` stays on the old card (add a test that it still
   renders). Tests: server-side asserts in `tests/test_server_keys_web.py`
   (setup GET) and the invite tests; browser tests for both pages at 375px;
   the existing-account invite test uses a second workspace (`two`-fixture
   pattern), sets `allow_http_errors` for the whole test and expects
   `/login` after "Join workspace" (409).
2. **Theme.** Classic `static/theme.js` (served from `_ASSETS`, both modes)
   applies the dashboard's saved `agenticml-theme` (system/light/dark, same
   key and cycle as app.js) before first paint, follows system changes and
   other tabs, and wires a "Theme: <pref>" toggle with no `aria-label`,
   placed after the form in DOM order. Brand panel stays dark. Every
   sign-in page (and the 403 identity page, without toggle) includes the
   script. Tests: dark saved → dark sign-in page (login, hosted login and
   `/invite`), toggle cycles and persists, carries into the dashboard; the
   Firebase "no web storage writes" test never clicks the toggle.
3. **Firebase page tests** (test-only, `testbed/tests/test_server_firebase_dashboard.py`):
   extend the fake `firebase` (idempotent globals, call order, error
   injection, held promises) and assert: sign-out on terminal failures
   (count before navigation, not "exactly one" across a navigation),
   in-memory persistence, buttons disabled while a request is in flight,
   the mapped error texts, nothing written to web storage, and the
   account dialog with and without Firebase (separate test). Fill `#email`
   before the forgot-password failure. If a test finds a real bug, fix it
   and report it.
4. **Invite token out of URLs on hosted (†).** With Firebase on, invite
   links are `/invite#token=...` (one `_invite_url` helper used by
   userspage, routes and the CLI `users invite`); the hosted page reads the
   fragment first and the query second (old links keep working) and clears
   both from the address bar. Self-hosted keeps `?token=`. ALB log
   retention unchanged (the privacy text says 90 days). Tests: hosted link
   format (including the Settings > Members browser test), fragment and
   legacy query both redeem, self-hosted unchanged, CLI link format. Runbook
   step text and a note: if a mail client drops the fragment, the invitee
   signs in at `/login` with the invited email.

Done when: unit and UI suites pass; 375px clean on every sign-in page;
supervisor second review of the token change recorded.

## 30.5 Database follow-ups (Sonnet)

Files: `src/agenticml/server/db/postgres.py`, `db/__init__.py`,
`src/agenticml/store/reader.py`, `src/agenticml/server/app.py` (reader
wiring), `db/usage.py`, `routes.py` (usage views only), tests,
`docs/SERVER.md`, `docs/plans/packages/20-many-processes.md`.

1. **Transaction connection cap.** `TRANSACTION_CONNECTION_LIMIT = 24` and a
   10 s wait per `PostgresDatabase` instance (`BoundedSemaphore`, created at
   the very top of `__init__` so `close()`/`__del__` never hit a missing
   attribute); timeout raises `DatabaseBusy('database is locked')` (503
   with Retry-After, as today); release after the connection is returned.
   Check the health route's path and say whether it can wait on a slot.
   Tests: 20 concurrent transactions still pass; with the cap patched to 2,
   a third waits then gets `DatabaseBusy`; a thread holding a session lock
   gets `DatabaseBusy`, not a hang, when all slots are taken.
2. **Committed sizes per reader call.** A thread-local memo of
   `committed_size` entered inside the reader lock, per public reader call;
   never across requests. Test in new `tests/test_reader_committed_sizes.py`
   with a series larger than `max_points` and no overview (or `<= 1`
   calls), plus the many-process freshness tests still passing.
3. **Usage views use the database clock** (`UsageRepo.now()/day()`), with
   the day computed once per admin list. Postgres-only test in
   `tests/test_server_many_processes.py` (clock straddling midnight).
4. **19.1 notes.** Remove the unreachable `path is None` clause in the
   SQLite branch of `db/__init__.py` (do not restructure); comment the
   Postgres branch; add the session-lock schema-isolation test (two schemas,
   same key, both acquire; close both instances before `DROP SCHEMA`; use
   `with` so slots are released); a test that the Postgres registry branch
   returns None for a missing database with `create=False`.
5. Docs: the per-process connection ceiling (24 transactions + session
   locks, about 40 in practice with 8 body workers) in `docs/SERVER.md` and
   the package 20 plan; RDS sizing (db.t4g.micro ~112 connections) is fine
   for two tasks at that level. The paused-process residual is already in
   `docs/THREAT_MODEL.md`; cite it.

Not in this part (moved to Later by 30.7): pooling Postgres session-lock
connections and the lock-latency benchmark (they change mutual-exclusion
code for a cost nobody has measured as a problem).

Done when: SQLite and Postgres suites pass.

## 30.6 AWS template and CI (Sonnet)

Files: `deploy/aws/cloudformation/hosted.yaml`, `deploy/aws/entrypoint.sh`
(comment), `.github/workflows/*.yml`, new `.github/workflows/monitor.yml`,
new `scripts/monitor_hosted.py`, `tests/test_deploy_*.py`, new
`tests/test_monitor_hosted.py`, `deploy/aws/README.md`, `docs/HOSTED.md`,
`docs/THREAT_MODEL.md`.

1. **ECS Exec:** `LinuxParameters: {InitProcessEnabled: true}` on the
   `server` container; no exec session logging (keys and invite links
   printed in a session would land in CloudWatch; CloudTrail records each
   call) stated in the runbook and threat model; fix the "PID 1" comment in
   `entrypoint.sh`. If Docker is usable, build the image and check `docker
   run --init` + `docker stop -t 120` exits 0 promptly; record the result.
   Template tests check the init process.
2. **Alarm:** `HealthyHostsAlarm` threshold `!Ref DesiredCount`; comment,
   runbook alarm item and `docs/HOSTED.md` updated; note the brief window
   when scaling 2 → 1.
3. **Pinning:** every `uses:` in every workflow pinned to the full commit
   SHA its tag points to today (dereference annotated tags), tag in a
   comment; `pypa/gh-action-pypi-publish` pinned to its latest v1.x.y
   release; a header comment; one test asserting every `uses:` is a 40-hex
   SHA; update the e-mail regex test. Owner runs `publish-testpypi.yml`
   once before tagging.
4. **Monitor:** `scripts/monitor_hosted.py` (stdlib; `/healthz` with
   retries; external links from `website/src/content/docs`, ignoring
   example, local and our own app/docs hosts and raw `main` URLs until the
   release; GitHub 403/429 as warnings) and `monitor.yml` (weekly
   `17 6 * * 1` and dispatch; `contents: read`, `issues: write`; repository
   guard; job timeout; values only from `env` in `run:`; one open "Hosted
   monitor failed" issue with label `hosted`, commented on repeat failures
   and closed on recovery). The default branch is `dev`, so the schedule
   starts when this reaches `dev`. Tests with fake openers.

Done when: unit tests and cfn-lint (`cfn-lint==1.60.0`) pass. Owner after
the hosted deploy: check ECS Exec, PID 1 and a clean stop (exit 0).

## 30.7 Roadmap and Backlog (supervisor)

- Delete the entries fixed in 30.1–30.6; add a "Package 30" section to the
  3.0 table with sub-rows 30.1–30.7.
- Owner entries kept, each with its checklist from the triage: Release
  3.0.0 (TestPyPI dry run with the new pins; disable `publish.yml` around
  the `v2.2.0` tag push or accept 2.2.0 on PyPI; add a reviewer to the
  `pypi` environment; tag `v3.0.0` at the merge; verify; then 29.9),
  two-task check (the default branch is `dev`, so the workflow input works
  now; 2 GB daily upload limit), on-demand EFS backup (CloudTrail, console
  retry, PassRole, EFS policy denies only `Client*`), real Firebase email
  sign-in (fresh email with no invite), real tailnet.
- Docs custom domain: not needed for 3.0.0; correct the order (set the
  Pages domain only together with a deploy of the root-path build, since
  `base` changes asset URLs; GitHub redirects the github.io project URL to
  the custom domain root).
- Move to Later with the reason: permissions boundary (own package; owner
  applies bootstrap before the next hosted deploy; rollback caveat; tag
  scoping is bypassable via tagging and needs an exact action list), lock
  connection pooling and benchmark, idle-page metric polling.
- Fix the 29.5b row (done, #69) and anything else stale.

## Build notes (Nov 17, 2026)

Every part was coded by Sonnet in its own worktree, reviewed by the
supervisor, and merged onto the package branch; the † parts also got an
adversarial review pass (independent Sonnet reviewers, each finding
checked by a skeptic) and the supervisor's second review. Decisions and
deviations worth keeping:

- 30.1: a plain sync of a running run does repair short or missing append
  files (the commit is refused while a file is shorter than committed and
  the client recovers from `/state`); only a changed atomic file needs
  `--reset` or a `/state` call. The runbook says so and tests pin it.
- 30.2: the review found three gaps, all fixed: a pending invite token
  could re-attach a detached account (redemption now checks it; a verified
  Firebase email still may); `workspaces orphans`/`cleanup` could run
  against a fresh empty database after a mistyped `--data` (they now
  refuse a missing or empty database); and the display name and personal
  workspace name and slug survived redaction. Role names were dropped from
  the reserved logins so the default `admin` account can be deleted;
  throttle buckets naming the login are cleared. The Privacy policy now
  says server-level entries are redacted (the owner delegated the wording).
- 30.3: a sync-scope key's dashboard login answers 403 (not 401).
- 30.4: if a mail client drops the fragment, a new invitee sees "no access"
  and reopens the full link or asks for a new invite; during a rolling
  deploy a fragment link opened on an old task loses its token, so create
  invites after the deploy finishes.
- 30.5: the bulk committed-size preload was left out (not needed for the
  memo's guarantee).
- CI: `test_keys_401_redirects_to_login` was flaky (a background poll
  redirected first); it now drives the keys page by hash with the other
  reads parked.

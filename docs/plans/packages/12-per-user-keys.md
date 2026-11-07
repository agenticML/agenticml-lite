# Package 12 †: Per-user key management and sync attribution

Nov 6, 2026 · Supervisor plan · Release 2.2 · Coder: Codex · Size: M ·
† security-sensitive (Sonnet reviews read-only; Cursor gives the second
read-only review)

Source: [on-prem-teams-plan.md](../on-prem-teams-plan.md) ("Roles",
"Ownership"). Builds on [11-users-and-roles.md](11-users-and-roles.md):
`users.py` (`Principal`, `allowed`, `UserStore`), `keys.json` v2 with
`user`, run ownership by user, `owner_key`, and `handler.principal` on
every authenticated dashboard and `/v2` request. Runs in parallel with
package 13 in a separate worktree; see "Shared files".

## Goal

Keys are managed per user. Members issue and revoke sync keys for their own
machines; admins see and revoke everyone's keys and issue keys for any user.
Every sync write is attributed to the key that made it, and operators see
activity per user and per key. `agenticml whoami` shows the user and role.

Done when the tests below pass and the whole `tests/` suite passes.

Out of scope: dashboard JS/HTML/CSS (package 15 builds the UI on the API
here), sign-in methods and sessions for non-admins (13), the audit log (14).

## Files to touch

- `src/agenticml/server/keyspage.py`: authorization by `handler.principal`,
  `user` on create, filtering, row fields.
- `src/agenticml/server/ingest.py`: `_commit` records `last_sync_key`;
  nothing else in this file (package 13 edits `_authenticate`).
- `src/agenticml/store/reader.py`: `read_remote` returns `owner_key` and
  `last_sync_key` (`None` when absent or not strings); existing fields
  unchanged.
- `src/agenticml/server/admin.py`: `status()` per-user rows and per-key
  last sync via `last_sync_key` (fallback `owner_key`, with an owner-user
  match); text output with a Users table above the Keys table.
- `src/agenticml/ui/server.py`: in server mode only (`self.server.auth is
  not None`), add `owner` (the run's user, from `read_remote`) to each
  `/api/runs` row and to the run detail; local `agenticml ui` responses
  are unchanged.
- `src/agenticml/cli.py`: `server keys list [--user U]`; `cmd_whoami`
  prints `user` and `role` when the server returns them (keep the current
  output lines; add `user: NAME (ROLE)`).
- `src/agenticml/client.py`: only if `whoami()` drops unknown fields.
- `src/agenticml/sync.py` (review-directed extension): user-aware cached and
  payload ownership, retaining the key-name fallback for older servers.
- Tests: `tests/test_server_keys_users.py` (new); update
  `tests/test_server_keys_web.py`, `tests/test_server_admin.py`,
  `tests/test_local_cli.py`/`test_server_cli.py` where output changes.

## Key API (`/api/server/keys`)

All routes need a dashboard principal (`handler.principal`). Rules, in one
place as pure functions in `keyspage.py` so they can be unit-tested:

```python
def can_see(principal, record) -> bool        # all_keys, or record.user == principal.user
def can_create(principal, user, scope) -> str | None   # None = allowed, else error text
def can_revoke(principal, record) -> bool     # all_keys, or own key
```

- Viewer (no `own_keys`): every route → 403 `{"error": "forbidden"}`.
- `GET /api/server/keys[?user=NAME]`: rows the principal can see, sorted
  as today. A member passing another `user` gets an empty list (not 403).
  Row = `record.to_public()` (includes `user`) plus:
  - `runs`: runs whose `owner_key` and `owner` match this key's name and
    user, `null` when the scan is truncated, as today;
  - `revocable`: `can_revoke(...) and name != "env"`;
  - `active`: false when the key's user is missing, is a viewer, or holds
    an admin-scope key without role `admin`.
  Response also has `user` (the principal's user), `role`, and the
  existing `host`/`host_source`.
- `POST /api/server/keys` `{name, scope, user?}`: `user` defaults to the
  principal's user. `can_create` errors: another user without `all_keys`
  → 403 `"you can only create keys for yourself"`; scope `admin` without
  `admin_keys` → 403 `"only admins can create admin keys"`; scope `admin`
  for a non-admin user → 400 `"admin keys need an admin user"`; unknown
  user → 404 `"unknown user"`. `created_by` = principal user. Response as
  today plus `record.user`.
- `DELETE /api/server/keys/<name>`: a key the principal cannot see → 404
  `not found` (do not reveal other users' key names); visible but not
  revocable → 403. Keep the last-admin-key rule (`keep_admin=True`) and
  the env-key 409. Keep `drop_sessions` and `signed_out`.
- CSRF, content-type and body-size rules stay exactly as today.

## Attribution

- `_commit` writes `last_sync_key` (the principal's key name) next to
  `last_sync_at`, preserving `owner` and `owner_key`.
- `remote.json` after 2.2: `{owner, owner_key, status, fingerprint,
  last_sync_at, last_sync_key, sizes}`.
- `server status --json` adds `users: [{name, role, keys, runs,
  last_sync_at}]`; key rows gain `user`; `last_sync_at` per key uses
  `last_sync_key`, then `owner_key`, paired with the owning user. Runs with
  no `owner_key` contribute only to user rows. Revoked or unknown key/user
  pairs keep the existing `revoked: true` marker.

## Shared files (parallel with package 13)

Package 13 works in another worktree on `webauth.py`, `app.py`, `up.py`,
`server/static/*`, `ui/server.py` (notes `writable` and the PUT gate),
`cli.py` (`server serve` flags and `server users invite|set-password`),
and one call in `ingest.py` `_authenticate`. Keep your edits to those
files limited to the functions named above so the merge is mechanical.
Do not refactor shared code.

## Edge cases

- A key whose user was removed between list and delete → 404.
- A member revoking their own last key works (no "last key" rule for sync
  keys).
- `?user=` with an invalid name → 400 via `validate_name`.
- Run counts must not reveal other users' activity to members beyond their
  own keys (members only see their own rows anyway).
- Runs without `owner_key` count toward their user, but no key row. This
  review-directed correction avoids attributing activity to a reused name.

## Tests (`tests/test_server_keys_users.py`)

1. Unit tests of `can_see` / `can_create` / `can_revoke` for viewer,
   member and admin principals, own and other users, sync and admin scope.
2. HTTP with an admin session (key login exists today): list shows every
   user's keys with `user`; create for another user; create admin key for
   a member → 400; delete another user's key; `?user=` filter.
3. HTTP with a member principal: package 13 adds member sign-in in
   parallel, so build a member session through a small test helper that
   uses only `WebAuth`'s public/session-creation function as it exists
   after package 11 (document the helper; package 15's end-to-end test
   repeats these checks through real sign-in). Member sees only own keys,
   creates own sync key, gets 403 for admin scope and for another user,
   gets 404 deleting another user's key.
4. Viewer principal → 403 on GET, POST, DELETE.
5. Attribution: two keys of one user sync one run; `remote.json` has
   `owner_key` = first key and `last_sync_key` = second; `server status`
   reports per-user and per-key last sync correctly.
6. `/api/runs` and run detail include `owner` in server mode and not in
   local `agenticml ui` mode.
7. `agenticml whoami` prints `user: NAME (ROLE)` against a 2.2 server and
   the unchanged output against a server that omits those fields.
8. `server keys list --user U` filters; unknown user → exit 1.

## Checks

- `PYTHONPATH=src /home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv/bin/python
  -m pytest -q tests` from the worktree root; all pass.
- Report any deviation from this plan instead of changing the approach.

## As built (coder handoff)

- `can_see`, `can_create`, and `can_revoke` centralize role permissions via
  `users.allowed`. Store validation handles unknown users and the target
  user's admin role. The API filters visible rows, reports active keys, and
  preserves CSRF, env-key, last-admin-key, and session-drop behavior.
- Status counts runs by their creating key and owner user, and attributes
  the most recent sync to `last_sync_key` (fallback `owner_key`) for that
  user. Runs without `owner_key` count only toward the user. User rows
  include their latest sync; the Users table precedes Keys. Server dashboard
  run rows/details include `owner`; local responses stay unchanged.
- Small deviations, reported during implementation:
  - The `whoami` formatter is actually `auth.cmd_whoami`, so its two-line
    addition lives in `src/agenticml/auth.py`; the CLI wrapper stays unchanged.
    `client.whoami()` already preserves all response fields.
  - Env-admin sessions keep the existing default-user fallback when POST
    omits `user`, because the synthetic `env` user cannot own file keys.
    With multiple stored users, the caller must name a target explicitly.
  - `KeyStore.revoke` adds an optional `expected` record guard, checked under
    the mutation lock. The API supplies the authorized record's secret hash
    and user identity, preventing a concurrent key-name replacement from
    revoking another user's key. CLI callers retain their existing behavior.
  - Existing `tests/test_store.py` exact-dict assertions include the two new
    normalized fields; `tests/test_server_keys_web.py` includes `active`, and
    the exact commit payload in `tests/test_server_ingest.py` includes
    `last_sync_key`.
- Member/viewer HTTP tests call the existing `_create_session(record)` and
  shim only the test token's admin-only `_session` gate, leaving `_attach`
  to resolve the real principal. Production `webauth.py` is unchanged.
- Review-directed scope extension: `sync.py` now persists `owner_user` from
  `whoami.user` in per-host state. `_check_owner` and `_sync_idle` compare
  users on 2.2 servers, retaining key comparison without a server user.
  Switching keys checks the authoritative run state (including old caches
  without `owner_user`), and a successful pass refreshes the cached key.
  The attribution test retains the first key's local cache; regression
  tests cover idle/changed runs, old caches, another user, and 2.1 identities.
  The cached-owner Backlog entry is resolved and removed.
- Key rows are indexed by `(name, user)` so reused names cannot inherit
  another user's runs or sync time; unknown/revoked pairs retain their
  marker. The former pre-`owner_key` count fallback was removed as requested.
- Key creation keeps HTTP 409 for globally unavailable names. Principals
  without `all_keys` receive `key name is not available`, whether their own
  or another user's key has that name. Accepted limitation: global uniqueness
  still makes name availability observable through the 409 response.
- Env-admin API errors say `user is required when the server has more than
  one user`; the store/CLI retains `--user` wording. Env DELETE and commits
  with a principal whose key is `None` have explicit regression coverage.
- `list_runs(include_owner=True)` carries `_remote_owner` from its existing
  remote read; the server `/api/runs` response uses it without rereading each
  run. The default reader/local dashboard behavior remains unchanged.
- Static dashboard assets and the unrelated audit implementation/tests are
  untouched. Shared-file edits are confined to `_commit`, dashboard run
  list/detail response construction, and the `server keys` command/parser.
- Security reviews and supervisor verification remain for the review stage.

Validation: required whole-suite command passed with **2054 passed, 9 skipped**;
new package tests: **36 passed**. The dashboard UI suite, run from `testbed/`
with this worktree's `src` on `PYTHONPATH` and the main checkout's venv, passed
with **228 passed, 114 deselected**, including the existing 375px overflow
checks. `git diff --check` passed. No commits or branches were created.

Review-fix validation: the required whole-suite command passed with **2068
passed, 9 skipped, 0 failed**. This round added **14 cases across 10 test
functions**, all passing; the package test file now has **50 passing cases**.
The package file plus the updated legacy-status expectation passed together
with **51 passed**. Existing status/count fixtures now supply `owner_key`;
the pre-migration status test explicitly expects no key attribution without
it. The dashboard UI suite passed with **228 passed, 114 deselected, 0 skipped,
0 failed**, including the existing 375px overflow checks. `git diff --check`
passed. No review items were deferred, and the protected audit files and
`webauth.py` were unchanged. No commits, branches, or stashes were created.

Final re-review fixes (coder handoff):

- Done: `sync._sync_idle` requires a matching cached `owner_user` when
  `whoami` supplies a user. Legacy caches are visited through `--all` and
  checked through `run_state`, including when the key name stayed the same.
  The expanded regression covers same-user refresh and another user's
  replacement key; a verified same-user cache is skipped on the next pass.
- Done: `sync._sync_locked` tracks a successful ownership check and stamps
  identity in the up-to-date branch only after that check. Three regressions
  leave the cache unchanged when it lacks an owner, the server has no run,
  or the returned run state has no owner.
- Done: `admin.status` avoids calling `key_row` without an owner user, even
  when `owner_key` exists. Three regressions cover missing, null and invalid
  owners without creating phantom key rows or attributing their activity.
- Done: `keys.UserRequired` is a dedicated `ClientError` subclass raised by
  `KeyStore.create` and recognized by `keyspage.handle`. Store/CLI wording
  remains unchanged; API wording is independent of the exception text.
  Three regressions check the typed store error, changed exception wording,
  and an unrelated error with the former matching text.

No fixes were deferred and no plan deviations were needed. Regression changes
are in `tests/test_server_keys_users.py`: four new functions and one expanded
function, covering 11 cases (10 net-new cases). The package file passes with
**60 passed, 0 skipped, 0 failed**. The required whole-suite command passed
with **2078 passed, 9 skipped, 0 failed**. From `testbed/`, using this worktree's
`src` on `PYTHONPATH` and the main checkout's venv (this worktree has no venv),
the UI suite passed with **228 passed, 114 deselected, 0 skipped, 0 failed**,
including the existing 375px zero-overflow checks. `git diff --check` passed.
The audit implementation/tests and `webauth.py` are unchanged. No commits,
branches, stashes, or index-changing commands were made.

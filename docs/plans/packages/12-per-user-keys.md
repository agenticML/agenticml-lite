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
  last sync via `last_sync_key` (fallback `owner_key`, then `owner` for
  pre-2.2 runs); text output with a Users table above the Keys table.
- `src/agenticml/ui/server.py`: in server mode only (`self.server.auth is
  not None`), add `owner` (the run's user, from `read_remote`) to each
  `/api/runs` row and to the run detail; local `agenticml ui` responses
  are unchanged.
- `src/agenticml/cli.py`: `server keys list [--user U]`; `cmd_whoami`
  prints `user` and `role` when the server returns them (keep the current
  output lines; add `user: NAME (ROLE)`).
- `src/agenticml/client.py`: only if `whoami()` drops unknown fields.
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
  - `runs`: runs whose `owner_key` is this key (fallback `owner` for runs
    without `owner_key`), `null` when the scan is truncated, as today;
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
  `last_sync_key`, then `owner_key`, then `owner`. Revoked or unknown key
  names keep the existing `revoked: true` marker.

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
- Pre-2.2 runs (no `owner_key`) still count toward the key named by
  `owner` if that key exists, so 2.0 data shows sensible numbers before
  migration rewrites… after migration `owner_key` is always set; keep the
  fallback for robustness.

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

# Package 18 †: Workspaces and accounts

Nov 7, 2026 · Supervisor plan · Release 3.0 · Coder: Codex (18.1), Cursor
(18.2) · Size: L

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), "Workspaces and
accounts (package 18)", "Tables (after package 18)", decisions D9 and the
package summary. Builds on [16](16-database-layer.md) (database,
repositories) and [17](17-runs-in-database.md) (runs in the database).
Packages 18b (Firebase), 18c (sign-up, limits), 19 (Postgres, RLS) and 21
(S3) build on the workspace scoping here.

† Security-sensitive: second read-only review by the coder that did not
write each part. Cross-workspace leaks are the main risk of 3.0.

## Goal

One server holds many **workspaces**. People have one global **account**
and a **membership** with a role (`owner`, `admin`, `member`, `viewer`) in
each workspace they belong to. Every workspace-owned row, file and object
belongs to exactly one workspace. The workspace of a request always comes
from the URL (`/w/<slug>/...`, `/api/w/<slug>/...`) or, for sync, from the
key; never from a "current workspace". Everything that is not yours is the
same 404 as something that does not exist.

A self-hosted server starts with one `default` workspace and, for a
one-team server, looks like 2.2: `/` goes straight to `/w/default/`.

Done when:

- the **isolation suite** passes: for every route in the server,
  enumerated from the route table (a new route without an isolation case
  fails CI), a non-member, an unknown slug and a suspended workspace get
  byte-identical 404 answers; every key × session × URL combination across
  two workspaces is covered; sync with a key from workspace A can never
  read, list, write or detect anything in workspace B (runs, objects,
  lineage, notes, keys, members, audit);
- one account in two workspaces with different roles gets each role's
  rights in each and switches between them in the dashboard;
- the full suite and the UI suite pass, with no horizontal overflow at
  375px on the changed pages;
- a 2.x or package-17 data directory starts as one `default` workspace
  with every user, key, run, note and audit event in it, and syncing
  continues with the old keys and unchanged clients.

Out of scope: Firebase sign-in (18b), self-serve sign-up, plans and limits
(18c), Postgres and RLS (19), many processes (20), S3 (21), metric
summaries (22), backup and `status`/`verify` beyond keeping them working
(23). No sync client change (protocol v2 stays; `whoami` may add fields).

## Model

| Concept | Rule |
| --- | --- |
| Account | Global. `login` is unique: a 2.2 user name (`[A-Za-z0-9._-]{1,64}`) or an email address (lowercased). Display name, scrypt password record, proxy identity, `server_admin` flag, `generation`. |
| Workspace | Server-made `id` (32 hex, never changes), `slug` (unique, `[a-z0-9][a-z0-9-]{0,39}`, not reserved: `admin`, `api`, `static`, `w`, `account`, `login`, `logout`, `setup`, `invite`, `healthz`, `v2`), `name`, `status` (`active`, `suspended`), `created_by`, `created_at`. Columns for 18c's plan and limits are **not** added here. |
| Membership | (workspace, account, role). Exactly one `owner` per workspace: the creator; cannot be removed or demoted, has every admin right. Assignable roles: `admin`, `member`, `viewer`. Ownership transfer is out of scope (server-admin CLI can set it). |
| Key | Belongs to one workspace and one account (a member of it). Name unique per workspace; SHA-256 digest globally unique, so a key resolves to exactly one workspace. Scope `sync` or `admin` as 2.2. A key stops working when its account's membership is removed or the workspace is suspended. |
| Invite | Per workspace: `login` (name or email), role, token digest, expiry (72 h), invited_by. Accepting creates the account if needed (self-hosted: sets the password), then the membership. Inviting a login that already has an account adds the membership directly (no link needed). |
| Session | Per account, covers all its workspaces (cookie as 2.2). A **key-login** session (2.2 admin-key login) is bound to that key and only opens the key's workspace. |
| Server admin | Account flag. Creates, renames, suspends, resumes, deletes workspaces and sets the flag. Does **not** see a workspace's runs, keys, members or audit unless also a member. Its pages and APIs are 404 for everyone else. |
| Env key | `AGENTICML_SERVER_KEY` acts as an `admin` of the `default` workspace (sync and key login), never a server admin. If `default` does not exist it is refused with one logged warning. |

Permissions per workspace map 2.2's `ACTIONS` onto the four roles:
`viewer` read; `member` + notes, sync, own keys; `admin` and `owner` +
admin keys, all keys, members, invites, audit. Workspace creation,
renaming, suspension and deletion are server-admin only in 18 (18c adds
self-serve creation).

## Storage

### Migration `0003_workspaces` (SQL, plus a Python step if needed)

Allow migrations to be `.sql` or `.py` (`upgrade(tx, dialect)`), in one
number sequence. Every workspace-owned table gets `workspace_id` as the
first column of its primary key and of every index.

- New `workspaces`, `memberships`, `invites`.
- `users` → `accounts` (keep `id`, `generation`, password, identity;
  `name` → `login`; drop `role` after copying it to the `default`
  membership; add `server_admin`, `display_name`).
- `keys`: add `workspace_id`, `account_id` (from `user_name`); primary key
  `(workspace_id, name)`; `sha256` stays globally unique.
- `runs`, `run_files`, `run_notes`: add `workspace_id`; primary keys
  `(workspace_id, run_id[, path])`; `owner_user` → `owner_account_id`
  (responses still show the account's `login` where 2.2 showed the user
  name).
- `audit_events`: add `workspace_id` (NULL for server-level events).
- `sessions`: `user_name` → `account_id`; add `workspace_id` (set only for
  key-login sessions).
- Existing data: create the `default` workspace (id from
  `secrets.token_hex(16)`), give every existing user a `default`
  membership with their 2.2 role, make the **earliest-created admin** the
  `owner` (ties broken by name) and a server admin, and assign every key,
  run, note and audit row to `default`. A database with no users creates
  `default` with no owner; the first account made by `/setup`, `server up`
  or `server users add` with role admin becomes its owner and a server
  admin.

### Files

- Run files, objects and lineage move to
  `<data>/workspaces/<workspace_id>/store/` (lineage stays where 2.2 keeps
  it, under the store root, so it is per workspace too:
  `.../store/lineage/`). Ids are server-made, so renaming a slug never
  moves files.
- On start, after migrations: if `<data>/store` exists and the `default`
  workspace has no directory, rename `<data>/store` to
  `<data>/workspaces/<default_id>/store` (same filesystem, one rename,
  directory fsync), guarded by a `server_settings` flag. `server/tmp` must
  be on the same filesystem as `workspaces/` (extend `prepare_data_dir`).
- Objects are deduplicated per workspace only (they live in the
  workspace's store), so `/v2/objects/missing` cannot reveal another
  workspace's objects.
- Deleting a workspace removes its rows (cascade) and its directory, after
  the typed confirmation, and writes a server-level audit line.

## Request binding and access (`server/access.py`)

- One binding step runs before every workspace handler:
  1. Parse the slug from `/w/<slug>/...` or `/api/w/<slug>/...`.
  2. Resolve the workspace; unknown slug, suspended workspace, no session
     account membership, or a key-login session bound to a different
     workspace → the **same** 404 (same status, headers and body as a
     nonexistent route under an existing workspace).
  3. Check the member's role against the route's action; too low a role
     is 403 as in 2.2 (the member already knows the workspace exists).
  4. Build a `WorkspaceContext` (workspace id and slug, store root,
     principal with role, repositories bound to that workspace). Handlers
     receive it and never take a workspace id from anywhere else.
- `require_workspace_member`, `workspace_membership`,
  `require_workspace_manager` (owner, admin): one function each, used by
  the binding step and the CLI.
- Repositories that hold workspace data (`RunRepo`, `KeyRepo` for listing
  and creating, `NoteRepo`, `InviteRepo`, `MembershipRepo`, audit reads)
  are constructed from a `WorkspaceContext` (or a workspace id) and every
  query filters by it; there is no constructor without one. Key
  **verification** is global by digest and returns the key's workspace.
- Sync (`/v2/...`): the key picks the workspace; the run, object, lineage
  and state handlers use that workspace's store and repositories.
  A suspended workspace's key gets 403 `{"error": "workspace suspended"}`
  (the sync client prints it and stops for that run). `whoami` adds
  `"workspace": "<slug>"` (additive; 2.2 clients ignore it).
- Reader caches, run locks and object locks are keyed by
  `(workspace_id, ...)`.

## Routes

A single route table lists every HTTP route the server answers, with
method, pattern, scope and required action. Dispatch uses the table; the
isolation suite iterates it.

| Scope | Routes |
| --- | --- |
| public | `/healthz`, `/login` (GET/POST), `/logout`, `/setup`, `/invite`, `/static/...` |
| sync | `/v2/...` (as 2.2; workspace from the key) |
| account | `/` (redirect), `/workspaces` (picker page), `/api/account/session` (2.2 `/api/server/session` plus `workspaces: [{slug, name, role}]`), `/api/account/password`, `/api/account/workspaces` |
| workspace | `/w/<slug>/` (dashboard page), `/api/w/<slug>/runs`, `/runs/<id>`, `/runs/<id>/notes`, `/runs/<id>/scalars` and every other 2.2 `/api/...` run, compare, lineage, notes and object route; `/api/w/<slug>/keys[...]`, `/api/w/<slug>/members[...]` (2.2 users API, per workspace), `/api/w/<slug>/invites[...]`, `/api/w/<slug>/audit` |
| server admin | `/admin/` (page), `/api/admin/workspaces[...]` (list, create, rename, suspend, resume), `/api/admin/audit` (server-level events) |

- 2.2 dashboard URLs (`/api/runs`, `/api/server/keys`, ...) are removed;
  `/` redirects (303) a signed-in account to `/w/<slug>/` when it has one
  workspace or the `aml_ws` cookie names one it belongs to, else to
  `/workspaces`. The cookie is set by the client-side switcher; the
  server only reads it, and an unknown or foreign slug in it is ignored.
- The dashboard page (`index.html`) is served at `/w/<slug>/` and keeps
  hash routing (`#/runs/...`); it learns the slug from `location.pathname`
  and prefixes every API call with `/api/w/<slug>`.
- Proxy-identity and password sessions work as in 2.2; the login page
  redirects to `/` after sign-in.

## CLI (server admin, run on the server)

- `agenticml server workspaces list|create SLUG [--name N] [--owner
  LOGIN]|rename SLUG --name N [--slug NEW]|suspend SLUG|resume SLUG|delete
  SLUG` (delete asks the operator to type the slug, `--yes-delete SLUG`
  for scripts).
- `agenticml server accounts list|set-server-admin LOGIN on|off`.
- Existing `server users ...` and `server keys ...` commands take
  `--workspace SLUG` (default `default`) and act on memberships and that
  workspace's keys; `users add` creates the account if needed. Output and
  messages stay as close to 2.2 as possible.
- `server audit` takes `--workspace SLUG` or `--server` (server-level).
- Every change writes an audit line (workspace or server level).

## Dashboard (18.2)

- `app.js`: one `api(path)` helper adds `/api/w/<slug>`; replace every
  direct `/api/...` and `/api/server/...` URL (runs, notes, objects,
  compare, lineage, keys, users → members, audit, session → account).
- Account menu: a workspace switcher (name + role per workspace, current
  one marked; selecting navigates to `/w/<slug>/` and sets `aml_ws`).
  Shown only when the account has more than one workspace or is a server
  admin.
- Team page per workspace: members with roles (owner shown, not
  editable), invite by login or email with a role (copy-link flow from
  2.2), remove member, change role; keys page per workspace as 2.2.
- `/workspaces` picker page and `/admin/` server-admin page (workspace
  list with slug, name, status, member count, created; create, rename,
  suspend/resume). Both reuse `app.css` and the existing layout.
- No horizontal overflow at 375px on every changed or new page (UI
  tests).

## Isolation suite (`tests/test_server_isolation.py`)

- Fixture: one server, workspaces A and B; in each an owner, an admin, a
  member, a viewer, a sync key and an admin key; account X is a member of
  both with different roles; runs (with objects, lineage, notes) synced
  into each with the real client.
- For every route in the route table with scope `workspace`: an example
  request (a dict in the test keyed by route name; a route with no entry
  fails the test). Assert, for B's slug, as A-only principals (each
  role, key-login session bound to A, proxy identity, password session):
  the answer equals the answer for a nonexistent slug and for a
  nonexistent route (status, `Content-Type`, body; `Set-Cookie` and
  `Date` excluded). Same for a suspended workspace as its own members.
- Server-admin routes: 404 for every non-server-admin, identical to a
  nonexistent path; a server admin who is not a member of B gets 404 on
  B's workspace routes.
- Sync: A's key cannot read B's run state (404 "unknown run" even for a
  run id that exists in B), cannot append or commit to it (a same-id run
  is created in A instead), `objects/missing` for an object only B has
  reports it missing, lineage state and receive stay in A.
- Keys: a key name that exists in both workspaces resolves to the right
  workspace; revoking in A leaves B's working.
- Timing is not tested; responses must not differ in body or headers.

## Constraints and conventions

- Standard library only; SQL in `server/db/` only; no backend-specific SQL
  outside the dialect.
- The workspace is never read from a header, a query parameter, a body
  field or server state; only from the path or the key.
- Fixed client-safe messages; 404 bodies identical for every "not yours".
- Keep 2.2 sign-in, CSRF, session and throttle rules unchanged.
- Self-hosted single-workspace experience matches 2.2 apart from URLs.

## Edge cases

- Slug rename: old `/w/<old>/` URLs 404; files do not move; keys keep
  working (bound by id).
- Removing a member: their keys stop working and their sessions lose that
  workspace; their runs keep the owner (shown by login).
- An account removed from every workspace can still sign in and sees the
  picker with no workspaces (and a short "ask an admin to invite you"
  message).
- Last admin/owner rules: the owner cannot be removed or demoted; 2.2's
  "last admin key" rule now counts usable admin keys in the workspace
  (owner and admins).
- Run ids may repeat across workspaces; everything keyed by
  `(workspace_id, run_id)`.
- Server admin suspends the workspace a request is in the middle of: the
  next request is 404 (dashboard) / 403 (sync).
- Two workspaces sync the same object: stored once per workspace.
- 2.x conversion: users with `viewer`/`member`/`admin` roles, invites
  pending, keys of each scope, synced runs, notes, audit history; second
  start does not convert again; a symlinked or non-directory `<data>/store`
  aborts start with a clear message.

## Tasks

- **18.1 (Codex):** migration, accounts/workspaces/memberships/invites
  repositories, `access.py`, route table and dispatch, workspace-bound
  ingest and dashboard API handlers (server side), file move, CLI,
  isolation suite, rewritten server tests. Leaves the dashboard JS for
  18.2 but must keep the server-side page routes ready for it.
- **18.2 (Cursor):** `app.js` / `app.css` / `index.html` and the login,
  setup and invite pages: workspace URLs, API prefix, switcher, team and
  keys pages per workspace, picker page, server-admin page, UI tests in
  `testbed/tests` (update existing ones for `/w/default/`, add switcher,
  picker and admin page tests with the 375px overflow check).

## Tests and checks

- `PYTHONPATH=src testbed/.venv/bin/python -m pytest -q tests` from the
  repo root.
- `PYTHONPATH=../src .venv/bin/python -m pytest -q -m ui` from `testbed/`.
- The isolation suite as above; a two-workspace end-to-end test (account
  X with `admin` in A and `viewer` in B: can manage keys in A, cannot
  edit notes in B); conversion tests for a 2.x and a package-17 data
  directory.

## Done when

All checks pass, the isolation suite covers every route, and both
reviews (Sonnet, and the second † review by the other coder) are clean.

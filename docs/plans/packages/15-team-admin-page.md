# Package 15: Team admin page, account menu, and the end-to-end teams test

Nov 6, 2026 · Supervisor plan · Release 2.2 · Coder: Cursor · Size: M–L

Source: [on-prem-teams-plan.md](../on-prem-teams-plan.md) ("Dashboard",
"Exit criteria"). Builds on 11 (users), 12 (per-user keys API, run
`owner`), 13 (sign-in, `/api/server/session` with `can`, account password
endpoint) and 14 (`AuditLog`, `/api/server/audit`).

## Goal

Admins manage users, roles, invite links and everyone's keys from the
dashboard and see recent activity; members manage their own sync keys;
everyone sees who they are signed in as and can change their password.
One end-to-end test proves the release exit criteria.

Done when the tests below pass, the whole `tests/` suite and the `ui`
suite pass, and every new view has zero horizontal page overflow at
375 px.

## Backend: `/api/server/users` (new `server/userspage.py`)

Routed in `WebAuth.handle` next to `/api/server/keys`. Every route needs
`allowed(principal, "users")`, else 403 `{"error": "forbidden"}`. Body,
CSRF and content-type rules: reuse `keyspage.read_json` (and the DELETE
CSRF check pattern in `keyspage.handle`). Every success writes the audit
line from package 14.

- `GET /api/server/users` → `{"users": [user.to_public() + {keys, runs}],
  "me": principal.user}`; `runs` = runs owned by the user (bounded scan as
  in `keyspage._run_counts`; `null` when truncated).
- `POST /api/server/users` `{name, role, identity?}` → creates the user and
  an invite; 201 `{"user": public, "invite_url": "<host>/invite?token=…",
  "expires_at"}` using `keyspage.command_host` for `<host>`. 409 on a
  taken name or identity; 400 on invalid input.
- `PATCH /api/server/users/<name>` `{role?, identity?}` → `set_role` /
  `set_identity`; 409 `"last admin"` when it would leave no admin; 404
  unknown. Changing your own role is allowed if another admin remains (the
  response then has `signed_out: true` and clears the cookie, since the
  generation bump ends the session).
- `DELETE /api/server/users/<name>` → removes the user and their keys;
  `{"removed": name, "keys_revoked": n}`; 409 `"you cannot remove
  yourself"`; 409 last admin.
- `POST /api/server/users/<name>/invite` → new invite (signs the user out
  everywhere, clears the password); `{"invite_url", "expires_at"}`.

## Frontend (`app.js`, `index.html`, `app.css`)

Server mode only; local `agenticml ui` is unchanged (no new nav items, no
new requests other than the existing `/api/server/session` probe).

- **Top bar.** `Signed in as <user> · <role>`. Nav shows **Keys** when
  `can.own_keys`, **Team** when `can.users`. The session box gets an
  **Account** button opening a dialog: user, role, sign-in method, and a
  "Change password" form (current — shown only when `password_set` —,
  new, confirm) posting `/api/server/account/password`. Proxy users see
  "Signed in through your identity proxy" and no Sign out button.
- **Team page `#/team`** (admins): users table — name, role (a select
  that PATCHes; confirm before demoting yourself), identity (editable
  inline), sign-in (`password` / `invite pending` / `—`), keys, runs,
  actions **Invite link** (confirm: "signs them out and clears their
  password") and **Remove** (confirm, shows the number of keys that will
  be revoked). **Add user** dialog (name, role default member, identity)
  shows the invite link once with Copy, like the new-key dialog. Below,
  an **Activity** section lists `/api/server/audit?limit=200` (time,
  actor, action, target, client) with a "Load older" button using
  `before`.
- **Keys page `#/keys`.** Admins: a **User** column, a user filter, and a
  user select in the New key dialog (scope `admin` disabled unless the
  selected user is an admin). Members: only their keys; scope fixed to
  `sync`. Inactive keys (`active: false`) show an "inactive" badge with a
  tooltip giving the reason.
- **Runs.** In server mode, show `owner` on the run page header and as an
  optional column in the runs table; note editing already follows
  `writable` — check that viewers see no edit control.
- Reuse the existing dialog, table, copy-button and error patterns from
  `keysPage`; follow the file's existing code style; no new libraries.
- 375 px: tables scroll inside their container; dialogs fit the viewport.

## End-to-end teams test (`tests/test_server_teams.py`)

The release exit criteria, against one real server (`make_server` on a
free port), through HTTP only (no internal calls except creating the first
admin with the CLI functions):

1. Admin (password sign-in) creates user `bob` (member) and `vera`
   (viewer) through `/api/server/users`; both redeem their invite links.
2. Admin issues a sync key for herself; bob issues his own sync key
   through `/api/server/keys`; bob's attempt to issue an admin key or a key
   for the admin → 403.
3. Each syncs one run with the real sync client (`agenticml.sync`).
4. Bob's key writing the admin's run → 409 (sync raises
   `OwnershipError`), and vice versa.
5. Vera reads `/api/runs` (both runs, with `owner`), gets 403 for note
   PUT, `/api/server/keys` and `/api/server/users`.
6. Bob edits a note on the admin's run (members edit notes on any run).
7. `audit.jsonl` contains, in order, attributed lines for every write
   above (user.add ×2, user.invite ×2, user.password ×2, key.create ×2,
   run.create ×2, note.write ×1, plus the logins).

## UI tests (`testbed/tests/test_server_team_dashboard.py`)

- Admin: add a user, copy the invite link, change role, re-invite, remove;
  Activity lists the changes.
- Member: Keys page shows only own keys, scope fixed to sync, no Team nav.
- Viewer: no Keys or Team nav; notes not editable.
- Account dialog: change password, wrong current password error.
- 375 px: no horizontal page overflow on Team, Keys, Account dialog.
- Update existing `test_server_keys_dashboard.py` /
  `test_server_dashboard.py` where the top bar text or Keys columns
  changed.

## Checks

- `PYTHONPATH=src /home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv/bin/python
  -m pytest -q tests` from the worktree root.
- From `testbed/`: the `ui` suite (`-m ui`) with this worktree's `src` on
  the path.
- Report any deviation from this plan instead of changing the approach.

## As built

Deviations from this plan:

- No UI change for notes: the dashboard has no notes editor, so the viewer
  rule is enforced by the note PUT API (403).
- Run `owner` is shown in the run identity line (list card, run-page
  header, and identity facts), not as a separate runs-table column.
- `POST /api/server/users/<name>/invite` refuses a self re-invite with 409
  (`you cannot re-invite yourself; change your password from the Account
  menu`).
- Step 7 asserts the exact ordered `(action, actor, target)` list after
  dropping CLI setup lines (`actor` starting with `cli:`) and `login.ok`.
  `login.ok` is checked separately for each sign-in. Steal attempts and
  403s add no extra lines. Sync of a finished run also records
  `run.finish` after each `run.create`.

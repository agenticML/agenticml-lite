# Package 28: One Settings page (account, workspace, members, keys, activity)

Nov 13, 2026 · supervisor plan · coder: Sonnet

## Goal

The signed-in dashboard has three separate top-bar entries today: **Keys**
(`#/keys`), **Team** (`#/team`: members, usage, pending invites, activity)
and **Account** (a dialog: user, role, sign-in, workspaces, new workspace,
change password, Admin link). The owner asked for them under one Settings
page, organised like agenticml-server's
(`agenticml-server/frontend/src/components/SettingsPage.tsx`: eyebrow,
"Settings" heading, one-line subtitle, a column of card sections). The
design is on the design board as "Proposal · Settings" and "Wireframe ·
Settings proposal" (copies in the session scratchpad:
`board/project/Proposed-Settings.dc.html`, `WF-Settings.dc.html`).

Everything the three entries do today keeps working, with the same API
calls, permissions, messages and safety rules (one-time secrets wiped on
close, confirm dialogs, last-admin rules). This is a front-end change: no
server route, API or permission changes.

## Scope

In:

- `src/agenticml/ui/static/index.html`: top bar.
- `src/agenticml/ui/static/app.js`: the settings route and page; `keysPage`
  and `teamPage` turned into section mounts; the Account dialog's body
  shared with the Account card; routing and redirects.
- `src/agenticml/ui/static/app.css`: a commented `/* Settings page
  (package 28) */` section.
- Testbed UI tests that use Keys / Team / Account, plus new tests.
- `docs/plans/on-prem-roadmap.md`: row 28; row 27 → done.

Out (Backlog entries in the roadmap): renaming a workspace by its owner
(needs a new endpoint; today only server admins rename on `/admin`); a
sidebar app shell like agenticml-server's; the `/workspaces` picker and
`/admin` page layouts.

## Design

### Top bar (`index.html`, wired in `sessionReady`)

- Remove `#nav-keys` and `#nav-team`. Add `<a id="nav-settings"
  class="btn btn-ghost btn-sm" href="#/settings" hidden>Settings</a>` and
  `<a id="nav-admin" class="btn btn-ghost btn-sm" href="/admin/"
  hidden>Admin</a>`.
- In server mode on a workspace page: show Settings (every role has at
  least the Account section) and Admin when `session.server_admin`. On the
  Settings page, Settings gets `aria-current="page"`.
- `#account` (button opening the Account dialog) is shown **only on the
  `/admin` path**, which has no workspace and so no Settings page. On
  workspace pages it stays hidden; the Account card replaces it.
- `#host` ("local · host") is hidden in server mode; it stays for the local
  `agenticml ui`.
- Local `agenticml ui` (no session): no Settings, Admin or Account, as
  today.

### Routes (`route()`)

- `#/settings` and `#/settings/<section>`, sections `account`,
  `workspace`, `members`, `keys`, `activity`. Unknown section →
  `account`.
- `#/keys` → `location.replace("#/settings/keys")`; `#/team` →
  `location.replace("#/settings/members")` (old links and bookmarks keep
  working).
- When the Settings page is already mounted and only the section changes,
  do not re-render or refetch: scroll the section's card into view, move
  focus to its heading (`tabindex="-1"`), and update the nav's
  `aria-current`. Any other route change disposes the page as today.
- A section the role cannot open falls back to `account` (no error).

### Page (`settingsPage(version, section)`)

Header (like agenticml-server): eyebrow `Workspace · <workspace name>`,
`<h1>Settings</h1>`, subtitle "Your account, this workspace's people and
its API keys." Then a two-column layout: a section nav
(`<nav aria-label="Settings">`, links to `#/settings/<section>`, only the
sections the role can open, plus for server admins a separated
"Server admin" link to `/admin/`) and a column of cards
(`<section class="settings-card" aria-labelledby=…>` each with an `h2`).

Sections and who sees them (from `workspaceCan`, unchanged):

| Card | Content | Shown when |
| --- | --- | --- |
| Account (`h2` "Your account") | Today's Account dialog body: User / Role / Sign-in list, Workspaces switcher (with New workspace form when open sign-up is on), Change password form for password logins, Sign out button in the card header (hidden when signed in through an identity proxy, like the top-bar one) | always |
| Workspace | Name, slug, `plan` if the usage payload has one; today's Usage list as four tiles (Storage, Daily uploads, Requests per minute, People) with a meter for the three that have a used/limit pair; hidden as today when the server has no limits (`limits === null`) | `users` |
| Members | Inline invite form at the top, the members table, then "Pending invites" (h3) with its table | `users` |
| API keys | "New key" button and the User filter in the card header; today's keys table | `own_keys` |
| Activity | Today's activity table with Load older | `audit` |

Rules:

- **Reuse, don't rewrite.** Turn `keysPage` into `mountKeys(host, version)`
  and `teamPage` into `mountTeam(hosts, version)` that render into the given
  card bodies instead of `app`, and return their `dispose`. Keep their
  `request()`, dialogs, refresh sequences, wipe-on-close and error handling
  as they are. The settings page sets `cleanup` to a function that calls
  every mount's dispose. Their 404 path (role lost) must not call
  `home()` from inside a section; instead re-render the settings page
  after refreshing the session (or show the section's error in place).
- **Account body shared.** Factor the dialog body into
  `accountDetails(session)` used by both `openAccountDialog()` (admin path)
  and the Account card. Same ids (`account-current`, `account-new`,
  `account-confirm`), labels, messages ("Passwords do not match",
  "Password updated") and focus rules for the dialog. The Admin link moves
  out of `workspaceSwitcher` on workspace pages (it is in the top bar and
  the nav); keep it in the dialog on the admin path.
- **Inline invite form** replaces the Team page's "Invite" button +
  dialog: "Login or email" input, "Role" select (viewer / member / admin,
  default member), "Send invite" submit, and one muted line describing the
  selected role. It calls the same endpoint with the same payload. The
  result appears in a box under the form with today's texts: the one-time
  invite link with Copy and expiry, or "This login already has an account
  and was added to the workspace." Wipe the one-time link when the
  section is disposed or a new invite is sent (same rule as the dialog
  today). Errors appear under the form (`role="alert"`). The members
  table's per-row "Invite link" and "Remove" keep their confirm dialogs.
- **Headings and names tests rely on stay** where the thing stays: table
  names "Keys", "Members", "Invites", "Activity" (check the current
  `aria-label`s and keep them), dialog titles, button texts ("New key",
  "Revoke", "Invite link", "Remove", "Load older", "Create",
  "Change password").
- Breadcrumb text: "agenticml / settings" on the Settings page,
  "agenticml / runs" elsewhere.
- No inline `<script>`, no inline handlers; store strings enter the DOM
  only as text (`el(..., { text })`), as everywhere in app.js.

### CSS

Use existing tokens, `.btn`, `.field`, `.chip`, `.table-scroll`,
`.keys-table` rules. New: `.settings-page`, `.settings-layout` (grid
`200px minmax(0, 1fr)`, gap 32px; the content column max-width about
960px), `.settings-nav` (sticky under the top bar; links 13px, radius 6,
current item `--surface-3`), `.settings-card` (`--surface`, 1px
`--border`, radius 8, padding 20, gap 14; header row h2 14px/600 with an
aside on the right), `.usage-tiles` / `.usage-meter`. At ≤ 760px: one
column, the nav becomes a horizontally scrolling row of chips inside its
own box (`overflow-x: auto`), cards full width. Tables keep today's ≤ 599px
stacked-card layout. Zero horizontal page overflow at 375px, for owner,
member and viewer views, with the invite result and long errors shown.
Text colours: `--text-3` or darker for content text (not `--text-4`).

## Tests and checks

Update every testbed test that clicks Keys / Team / Account or asserts
`#nav-keys` / `#nav-team` (`test_server_keys_dashboard.py`,
`test_server_team_dashboard.py`, `test_server_usage_dashboard.py`,
`test_server_dashboard.py`, `test_server_admin_dashboard.py`,
`test_server_firebase_dashboard.py`, `test_dashboard.py`): open Settings
(or go to `#/settings/<section>`) and scope locators to the card. Keep each
test's intent; do not delete assertions.

Add (`ui`, `browser` marks):

- Owner/admin: the nav and cards show Account, Workspace, Members, API
  keys, Activity; top bar has Settings and no Keys/Team/Account.
- Member: Account and API keys only; viewer: Account only; their nav
  matches.
- `#/keys` and `#/team` land on `#/settings/keys` / `#/settings/members`.
- Clicking a nav item changes the hash and scrolls without refetching
  (e.g. a marker attribute set on the page element survives the click).
- Server admin sees the Admin link; others do not. On `/admin`, the
  Account button opens the dialog.
- Inline invite: link shown once, then wiped when another invite is sent;
  error shown under the form.
- 375px: no overflow for owner, member and viewer views, with the invite
  result and the change-password form shown.

Run from the repo root with this branch's code:

- `PYTHONPATH=<worktree>/src:<scratchpad>/pylib
  /home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv/bin/python
  -m pytest -q tests` (check `agenticml.__file__` points into the
  worktree; `pylib` holds a symlinked PyJWT; the shared venv has no boto3,
  so `tests/test_server_backup.py` and `tests/test_server_objects_s3.py`
  are skipped locally and run in CI).
- The same interpreter with `-m pytest -q -m ui` from `testbed/`.

## Edge cases

- Session not loaded yet when `#/settings` opens: wait for `sessionReady`
  (as the admin path does) before deciding sections.
- Workspace with no limits: no Workspace usage tiles; the card still shows
  name and slug.
- Role changed or removed while on the page (404/403 from a section):
  that section shows its error; the rest keeps working.
- Identity-proxy sign-in: no Sign out buttons, no password form.
- A dialog from a section is open during a route change: it closes and
  wipes secrets, as today.

## Done when

The top bar shows Settings (and Admin for server admins) instead of Keys,
Team and Account; `#/settings` holds every function those three had, by
role; old URLs redirect; all updated and new tests pass with zero overflow
at 375px; row 28 is `done (<PR link>)`.

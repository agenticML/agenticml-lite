# AgenticML 2.2: Teams

Nov 6, 2026 · Supervisor plan · Release 2.2

Roadmap: [on-prem-roadmap.md](on-prem-roadmap.md). 2.0 design:
[on-prem-server-plan.md](on-prem-server-plan.md). Package plans:
[11](packages/11-users-and-roles.md), [12](packages/12-per-user-keys.md),
[13](packages/13-sign-in.md), [14](packages/14-audit-log.md),
[15](packages/15-team-admin-page.md).

## Goal and exit criteria

Several people share one server. Each person is a **user** with a **role**;
machines sync with **keys** that belong to a user; people sign in to the
dashboard as themselves; every write is attributed in an audit log.

Exit criteria (from the roadmap): two users with different roles share one
server, each with the access their role allows, and every write is
attributed. Concretely, one end-to-end test (`tests/test_server_teams.py`,
package 15) does all of this against a real server:

1. An admin and a member, each with a sync key, sync one run each.
2. Neither key can write the other user's run (409).
3. A viewer signs in with a password, reads both runs, and cannot edit
   notes (403) or reach `/api/server/keys` or `/api/server/users` (403).
4. The member edits a note; the admin issues a key for the member.
5. `audit.jsonl` holds one attributed line for every write above.

The release ships as one PR from `claude/2-2-release-planning-84ba0f` into
`dev`, one commit per package plus a docs commit.

## Constraints carried from 2.0 and 2.1

- The server stays standard-library only. No new runtime dependency.
- One v1 store; team state lives beside it in `<data>/server/`.
- The local `agenticml ui` is unchanged: no users, no roles, no sign-in.
- 2.0/2.1 data directories upgrade in place on first start; no manual step.
- 2.0/2.1 sync clients keep working against a 2.2 server (see "Ownership").
- File rules from 2.0 apply to every new file: no-follow opens, atomic
  replace with fsync, mode 0600, strict parsing that fails closed.

## Decisions (made for the owner, Nov 6, 2026)

The roadmap left two questions open. The recommended answers were taken:

1. **Viewers see every project.** No project-level access in 2.2. Roles are
   server-wide. Project-level access changes the store reader and moves to
   "Later" in the roadmap.
2. **People sign in with a password or through a trusted identity proxy;
   keys stay for machines.** Both methods ship:
   - Built-in username and password, hashed with `hashlib.scrypt` (stdlib).
     Users set their own password from a one-time invite link that an
     admin creates, so an admin never chooses or sees anyone's password.
   - A trusted identity proxy (ALB OIDC, Tailscale Serve, oauth2-proxy),
     honoured only on connections from configured proxy addresses.
   - Signing in with an **admin-scope key** stays as a fallback for admins
     (break-glass and the 2.0 habit). Sync keys never sign in.

Smaller decisions, also taken as recommended:

- The 2.0 single user migrates to a user named `admin`. Renaming users is
  not in 2.2 (Backlog).
- No automatic user creation from proxy identities. An admin adds each
  user and sets their `identity`; an unknown identity gets a clear 403.
- Removing a user deletes their keys and ends their sessions; their runs
  keep `owner: <name>`. Adding a user with a name that still owns runs is
  refused unless `--reclaim-runs` is passed, so a new person never inherits
  them by accident. Transferring runs is Backlog.
- Roles are checked live on every request; changing a role takes effect on
  the next request without revoking anything.
- The audit log has no built-in rotation. It is opened per write, so
  `logrotate` with `create` works; this is documented.
- Version stays `2.0.0` in `pyproject.toml` (2.1 did not bump either); the
  version bump belongs to the release step.

## Model

### Roles

| Action | viewer | member | admin |
| --- | --- | --- | --- |
| Read the dashboard (every project) | yes | yes | yes |
| Change own password | yes | yes | yes |
| Edit run notes | – | yes | yes |
| Sync (`/v2` writes) with own keys | – | yes | yes |
| Issue / revoke own **sync** keys | – | yes | yes |
| Hold **admin**-scope keys, sign in with a key | – | – | yes |
| See and revoke everyone's keys, issue keys for anyone | – | – | yes |
| Add / remove users, change roles, create invite links | – | – | yes |
| Read the audit log | – | – | yes |

A key whose user is now a viewer still authenticates, and `whoami` works
and reports the role, but every other `/v2` route (reads such as `state`
and `lineage` GET included) answers 403 `{"error": "role viewer cannot
sync"}`. Viewers read through the dashboard, not through keys.

An admin cannot write another user's runs through sync: run ownership is
per user regardless of role.

### `<data>/server/users.json` (new, package 11)

```json
{"version": 1, "users": [
  {"name": "alice", "role": "admin", "identity": "alice@example.com",
   "password": {"alg": "scrypt", "n": 16384, "r": 8, "p": 1,
                "salt": "<base64>", "hash": "<base64>"},
   "invite": {"sha256": "<hex>", "expires_at": "<ts>"},
   "generation": 3,
   "created_at": "<ts>", "created_by": "cli:bharath"}
]}
```

- `name`: same pattern as key names (`[A-Za-z0-9._-]{1,64}`), not `env`,
  `.`, `..`. Unique. User names and key names are separate namespaces.
- `role`: `admin`, `member` or `viewer`.
- `identity`: `null` or a string of 1–256 printable characters, unique
  across users; the value the identity proxy sends for this person.
- `password`: `null` or the scrypt record (package 13 fills it).
- `invite`: `null` or a pending invite (package 13).
- `generation`: integer ≥ 0, bumped when the password changes, the user is
  re-invited, or the role changes; sessions remember it and end when it
  differs.
- Strict parse: exact field set, types checked, fail closed (no users)
  with one logged error per file change, exactly like `keys.json`.

### `<data>/server/keys.json` version 2 (package 11)

Version 2 adds `"user": "<user name>"` to every record. Version 1 is read
only by the migration. The `env` key (`AGENTICML_SERVER_KEY`) belongs to a
built-in user `env` with role `admin` that is not stored in `users.json`
and cannot be removed or changed; it exists only while the variable is set.

One lock, `keys.lock`, serializes every mutation of `users.json` and
`keys.json` across the server and CLI processes, so adding a key for a user
that is being removed cannot interleave.

### Migration (package 11)

Runs under `keys.lock` on server start and before every `server users` /
`server keys` CLI command. Idempotent; `users.json` is the commit point:

1. If `users.json` exists, stop.
2. Read `keys.json` (v1 or v2; missing is empty).
3. If there are file keys, or any run has a string `owner` other than
   `env` in `remote.json`: the migrated user is `admin` (role `admin`,
   `created_by: "migration"`). For every run whose `remote.json` owner is a
   string other than `env`, rewrite it atomically to `owner: "admin"` and
   add `owner_key: <old owner>` unless present. Then write `keys.json` v2
   with `user: "admin"` on every record.
4. Write `users.json` (with `admin`, or empty when there was nothing).
5. Log one line: `migrated 2.0 keys to user admin (N keys, M runs)`.

### Ownership (package 11; attribution fields in 12)

- `remote.json` `owner` is a user name; `owner_key` is the key that
  created the run (package 11); `commit` also records `last_sync_key`
  (package 12).
- A write is allowed when the run is new, or its owner equals the calling
  key's user.
- **Compatibility rule:** every `owner` the server returns to a `/v2`
  caller (in `state`, in `state.remote`, and in 409 payloads) is the
  **caller's key name** when the caller's user owns the run, and the owning
  user's name otherwise. `owner_user` always carries the real user name.
  2.0/2.1 clients compare `owner` with their key name, so they keep working
  unchanged; 2.2 clients do the same.
- `whoami` adds `user` and `role` to `{key, scope, protocol, features}`.

### Principals (package 11, used by 12–15)

Every authenticated request carries one `Principal` (frozen dataclass in
`server/users.py`): `user`, `role`, `via` (`key`, `password`, `proxy`,
`setup`, `invite`), `key` (key name or `None`). `/v2` requests get it from
the bearer key; dashboard requests from the session or the proxy header.
Permission checks call one helper, `users.allowed(principal, action)`,
with the actions from the table above (`read`, `notes`, `sync`,
`own_keys`, `admin_keys`, `all_keys`, `users`, `audit`).

### Sign-in (package 13)

- `POST /login` takes `{"user", "password"}` or `{"key"}` (admin-scope key
  of an admin user). Sessions stay in memory (2.0 rules: 12 h idle, 7 days
  max, 1000 cap, `__Host-` cookie on https) and now record the user and
  generation.
- Invites: an admin creates a one-time link (72 h) for a user; opening it
  sets that user's password and signs them in. Re-inviting clears the
  password. First-run `/setup` creates the first **admin user** with a
  password (it used to create a key).
- Identity proxy: `server serve --trusted-proxy CIDR` (repeatable) and
  `--identity-header alb|tailscale|oauth2-proxy`. Only requests whose TCP
  peer is in a trusted range may assert an identity or a forwarded client
  address. A matched identity is signed in per request (no cookie).
- The failed-login throttle keys by the forwarded client address on
  trusted-proxy connections (fixes the Funnel backlog entry), and password
  attempts also count against a per-user bucket. `server up` trusts
  loopback as its proxy.

### Audit log (package 14)

Append-only `<data>/server/audit.jsonl`, one JSON object per line:
`{"ts", "actor", "via", "action", "target", "client", "detail"}`. Actions:
`user.add`, `user.remove`, `user.role`, `user.identity`, `user.invite`,
`user.password`, `key.create`, `key.revoke`, `login.ok`, `login.failed`,
`logout`, `setup`, `run.create`, `note.write`, `migration`. CLI writes use
actor `cli:<os user>`. `agenticml server audit` and
`GET /api/server/audit` (admin) read the tail.

### Dashboard (package 15)

A Team page for admins (users, roles, invite links, keys per user,
activity), a per-user Keys page for members, an account menu (who am I,
change password, sign out), and role-aware note editing. Same CSRF and
session rules as notes; no horizontal overflow at 375 px.

## Packages, order and owners

| # | Package | Depends on | Coder | Second review † |
| --- | --- | --- | --- | --- |
| 11 † | Users, roles, migration, principals, run ownership by user, `server users` CLI | – | Cursor | Codex |
| 12 † | Per-user key management (API, CLI, status), sync attribution | 11 | Codex | Cursor |
| 13 † | Sign-in: passwords, invites, identity proxy, forwarded throttle | 11 | Cursor | Codex |
| 14 | Audit log | 11–13 | Codex | – |
| 15 | Team admin page, account menu, end-to-end teams test | 11–14 | Cursor | – |
| docs | `docs/SERVER.md` teams guide, `PROTOCOL.md`, `THREAT_MODEL.md`, README | 11–15 | Antigravity | – |

12 and 13 run in parallel in separate worktrees once 11 is merged into the
release branch; their only shared file is `ingest.py`, where 13 changes one
call (the throttle address) and 12 owns the rest.

Every package: Sonnet reviews read-only; † packages also get a read-only
review from the coder that did not write them; the supervisor verifies
with the full test suite (`tests/` and the `ui` suite for 13 and 15).

## Release checklist

- [ ] Packages 11–15 merged into the release branch, statuses updated.
- [ ] `testbed/.venv/bin/python -m pytest -q tests` passes.
- [ ] `pytest -q -m ui` from `testbed/` passes; 375 px overflow checks pass.
- [ ] A 2.1 data directory (keys, runs, notes) upgrades on first start and
      a 2.1 client still syncs to it (test in package 12).
- [ ] `docs/SERVER.md` documents users, roles, invites, the identity proxy
      setup for each supported proxy, and the audit log.
- [ ] Backlog entries for everything deferred.

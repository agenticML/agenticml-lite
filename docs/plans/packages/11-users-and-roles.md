# Package 11 †: Users, roles, and run ownership by user

Nov 6, 2026 · Supervisor plan · Release 2.2 · Coder: Cursor · Size: L ·
† security-sensitive (Sonnet reviews read-only; Codex gives the second
read-only review)

Source: [on-prem-teams-plan.md](../on-prem-teams-plan.md) (read it first:
sections "Model", "Migration", "Ownership", "Principals"). Roadmap:
[on-prem-roadmap.md](../on-prem-roadmap.md). Base: the release branch
`claude/2-2-release-planning-84ba0f` (at `dev` 4ed9684).

## Goal

The server knows **users** with roles `admin`, `member`, `viewer`. Keys
belong to users. Runs belong to users, so all of one person's machines can
write their runs and nobody else's. A 2.0/2.1 data directory migrates to a
single `admin` user on first start, keeping every key and its scope.
Operators manage users with `agenticml server users add|list|remove|
set-role|set-identity`.

Done when the tests in "Tests" pass, the whole `tests/` suite passes, and a
2.1 data directory (file keys, runs owned by key names, an env-key run)
migrates and keeps syncing with the unchanged sync client.

Out of scope (later packages; do not build): passwords, invites, identity
proxy, login page changes, throttle changes (13); per-user Keys API and
CLI `--user` listing columns beyond what is below, `whoami` CLI output,
`last_sync_key` (12); audit log (14); any dashboard JS/HTML/CSS (15).

## Facts from the current code (at 4ed9684)

- `server/keys.py`: `KeyRecord(name, scope, sha256, prefix, created_at,
  created_by, last_used_at)`; `KeyStore` reads `keys.json` v1 strictly
  (exact field set), reloads by `(ino, mtime_ns, size)` signature, takes
  `keys.lock` (flock) for mutations, writes atomically with fsync, mode
  0600. `create(name, scope, created_by, only_if_no_admin=)` returns
  `(record, secret)`. The env key is a synthetic `KeyRecord("env", "admin",
  ...)`. `setup-closed` marker logic must keep working.
- `server/ingest.py`: `_authenticate()` returns a `KeyRecord`;
  `_ownership(directory, record, meta_write)` compares `remote.json`
  `owner` with `record.name` and raises 409 `{"error": "run owned by
  another key", "owner": owner}`; `_atomic` writes `remote.json`
  `{"owner": record.name}` on the first `meta.json`; `_state` returns
  `{"owner", "files", "remote"}`; `_commit` rewrites `remote.json` keeping
  `owner`. `whoami` returns `{key, scope, protocol, features}`.
- `server/webauth.py`: sessions store `{name, sha256, created, last_seen}`
  of an **admin key**; `_session` drops the session when the key is gone or
  not admin. `/api/server/session` returns `{server, key, scope}`.
  `_setup` creates the first admin key.
- `server/keyspage.py`: `_run_counts` counts runs by `remote.json` owner;
  `created_by=handler.session["name"]`.
- `server/admin.py` `status()`: per-key rows keyed by `remote.json` owner.
- `server/up.py` `bootstrap_admin`: `keys.create("admin", scope="admin",
  created_by="server up", only_if_no_admin=True)`.
- `cli.py` `cmd_server_keys`, parser at `p_keys`.
- Sync client (`sync.py`) compares `state["owner"]` / 409 `owner` with its
  `whoami()["key"]` (`_check_owner`, `_sync_idle`) and stores
  `state["owner"] = key`. **Do not change `sync.py`** in this package; the
  compatibility rule below keeps it working.
- `store/reader.py` `read_remote(path)` parses `remote.json` for the
  dashboard; check that extra fields (`owner_key`) are tolerated.

## Files to touch

- `src/agenticml/server/users.py` (new): `ROLES`, `UserRecord`,
  `UserStore`, `Principal`, `allowed()`, `migrate()`, errors.
- `src/agenticml/server/keys.py`: `keys.json` v2 (`user` field),
  `create(..., user=None)` rules, `env` user, helpers below.
- `src/agenticml/server/ingest.py`: principal from the key, role check,
  ownership by user, `owner_key`, compatibility rule, `whoami` fields.
- `src/agenticml/server/app.py`: build `UserStore`, run `migrate()` in
  `make_server` after taking `server.lock` and before serving.
- `src/agenticml/server/webauth.py`: sessions record the user; validity
  also requires the key's user to exist with role `admin`;
  `/api/server/session` adds `user` and `role`; setup keeps creating the
  first admin key (now for user `admin`, created if missing).
- `src/agenticml/server/keyspage.py`: `created_by` = session user; key run
  counts use `owner_key` (fallback `owner`).
- `src/agenticml/server/admin.py`: per-key run counts via `owner_key`
  (fallback `owner`); add a `users` list (`name, role, keys, runs`) to the
  status dict and text output; read `keys.json` v1 or v2.
- `src/agenticml/server/up.py`: only adapt to the new `create` signature
  if needed (default user rule covers it).
- `src/agenticml/cli.py`: `server users ...` subcommands; run `migrate()`
  before `server keys` and `server users` actions.
- Tests: `tests/test_server_users.py` (new) and updates to existing server
  tests where they assert on-disk `remote.json` owners or the v1 file.

## Design

### `users.py`

```python
ROLES = ("viewer", "member", "admin")   # ordered by power
ACTIONS = {
    "read": "viewer", "password": "viewer",
    "notes": "member", "sync": "member", "own_keys": "member",
    "admin_keys": "admin", "all_keys": "admin", "users": "admin", "audit": "admin",
}
@dataclass(frozen=True)
class Principal:
    user: str
    role: str
    via: str            # "key", "password", "proxy", "setup", "invite"
    key: str | None = None
def allowed(principal, action) -> bool   # unknown action -> KeyError
```

`UserRecord` fields exactly as in the release plan (`name, role, identity,
password, invite, generation, created_at, created_by`). `to_public()`
returns `name, role, identity, created_at, created_by`, plus
`password_set: bool` and `invite_pending: bool` (never the hashes).

`UserStore(server_dir)` mirrors `KeyStore`: same strict reader style, same
reload signature cache, same "log one error per file change and fail
closed" rule, same atomic write helper, and the **same `keys.lock`** for
every mutation (factor the lock and atomic-write helpers so both stores use
one implementation; avoid an import cycle, e.g. a small shared module or a
local import). Methods:

- `list()`, `get(name)` (cached, reloads on change).
- `add(name, role="member", identity=None, created_by="cli")`.
- `remove(name)` → removes the user **and all their keys** (write
  `keys.json` first, then `users.json`, both under one lock hold).
- `set_role(name, role)` (bumps `generation`), `set_identity(name, value)`
  (`None` clears; uniqueness enforced).
- Last-admin rule: `remove` and `set_role` refuse to leave zero `admin`
  users in `users.json` (`LastAdminError`). The env user does not count.
- `by_identity(value)` for package 13 (exact match; plain loop).
- Validation: names like keys (`validate_name`); identity 1–256
  characters, no control characters, stripped value must equal the value.
- Errors: `UserNotFound`, `UserNameTaken`, `IdentityTaken`,
  `LastAdminError` (reuse the class from `keys.py`), `ClientError` for bad
  input, `KeyStoreUnavailable` when a file cannot be read strictly during
  a mutation.

The built-in `env` user (`role="admin"`) exists only while the KeyStore has
an env key; `UserStore` never stores it. Provide one function used by both
ingest and webauth: `resolve(keys, users, key_record) -> Principal | None`
(None when the key's user is missing; the `env` key resolves to
`Principal("env", "admin", "key", "env")`).

### `keys.py` changes

- `KeyRecord` gains `user: str`. `keys.json` is written as version 2.
  The strict reader accepts only version 2 for normal use; a separate
  `_read_v1_or_v2()` is used only by `migrate()` and by `admin.status`.
- `create(name, scope="sync", created_by="cli", *, user=None,
  only_if_no_admin=False)`, under `keys.lock`, reading `users.json`
  strictly:
  - `user=None`: if there are no users, add user `admin` (role `admin`,
    `created_by` = the key's `created_by`) and use it; if there is exactly
    one user, use it; otherwise raise `ClientError("--user is required when
    the server has more than one user")`.
  - The user must exist (`UserNotFound`). Scope `admin` requires role
    `admin` (`ClientError("admin keys need an admin user")`).
  - `only_if_no_admin` keeps its meaning (no admin key anywhere and setup
    not closed); keep the `setup-closed` marker behaviour unchanged.
- `revoke` unchanged; add `for_user(name)`.
- `to_public()` now includes `user`.

### Migration (`users.migrate(data_dir)`)

Exactly the steps in the release plan, under `keys.lock`, idempotent,
`users.json` last. Details:

- Called by `make_server` (after `server.lock`, before `IngestServer` is
  built) and by `cmd_server_keys` / `cmd_server_users` before acting. Not
  called by `server status` / `server verify` (read-only; they must handle
  both versions).
- Scan `runs/` with `os.scandir`, no symlinks followed; read `remote.json`
  with the existing no-follow helpers; rewrite with `layout.atomic_write`
  + `fsync_dir`. Skip unreadable or non-dict files with one warning each
  (do not abort the migration). Use the run's directory lock rules already
  in `ingest.py` only if the server is running — it is not (migration runs
  before serving, and CLI writes only `remote.json` files the server would
  serialize; holding `keys.lock` is enough because `make_server` migrates
  before accepting requests and a running server has already migrated).
- After migration, a CLI `keys create` against a running server must not
  rewrite any `remote.json` (it finds `users.json` and stops at step 1).

### Ingest

- `_authenticate()` keeps throttling exactly as now, then resolves the
  principal (`users.resolve`). A key whose user is missing → 401
  `unauthorized` (same as an unknown key; counts as a failure for the
  throttle? **No**: count only bad bearer values, as now).
- `whoami` → `{key, scope, user, role, protocol, features}` for any role.
- Every other route needs `allowed(principal, "sync")`, checked right after
  authentication and before reading any body: 403 `{"error": "role viewer
  cannot sync"}`; close the connection when a body was announced.
- Ownership: `owner == principal.user` (new runs are claimed by
  `principal.user`; `remote.json` gets `{"owner": user, "owner_key":
  key}`); `_commit` preserves `owner` and `owner_key`.
- 409 error text becomes `"run owned by another user"`; payload `owner`
  follows the compatibility rule.
- **Compatibility rule** (release plan): in `state` (top-level `owner` and
  `remote.owner`) and in every 409 payload, `owner` is the caller's key
  name when the owning user is the caller's user, else the owning user's
  name; always add `owner_user` (top level, and inside `remote` too).
  Implement in one helper used by all three places.

### Dashboard sessions (webauth, keyspage)

- Login stays admin-key only in this package. The session entry adds
  `user` (the key's user). `_session` additionally requires the user to
  exist with role `admin` (live lookup); otherwise the session is dropped.
- `/api/server/session` → `{server: true, key, scope: "admin", user,
  role}`. Keep the old fields for the current `app.js`.
- `keyspage` POST creates keys for `handler.session["user"]` with
  `created_by=handler.session["user"]`; scope rules from `create` apply.

### CLI

```
agenticml server users add NAME [--role viewer|member|admin] [--identity ID] [--data D]
agenticml server users list [--json] [--data D]
agenticml server users remove NAME [--data D]
agenticml server users set-role NAME ROLE [--data D]
agenticml server users set-identity NAME [ID] [--data D]   # no ID clears it
agenticml server keys create --name N [--scope S] [--user U] [--data D]
```

- Default role for `add` is `member`. `list` table columns: `name, role,
  identity, keys, created_at, created_by` (`-` for empty); `--json` prints
  `to_public()` rows plus `keys` count.
- `keys list` gains a `user` column.
- Same error style as `cmd_server_keys`: `agenticml server: <message>` on
  stderr, exit 1. Removing a user prints how many keys were revoked.

## Edge cases

- `users.json` corrupt: the running server authenticates nobody except the
  env key (fail closed; one logged error); CLI mutations fail with a clear
  message; migration does not overwrite it.
- `users.json` present but `keys.json` still v1 (hand-edited/restored
  backup): KeyStore fails closed (no file keys) and logs that the file is
  version 1; `migrate()` does not run again. Document in the error text:
  "keys.json is version 1; restore users.json or move users.json away to
  migrate again".
- A key's user removed by the CLI while the server runs: the next request
  with that key gets 401; a session for it ends.
- Demoting a user to `viewer`: their keys still answer `whoami` with
  `role: viewer`; writes get 403. Promoting back restores writes.
- Env key runs (`owner: "env"`) stay owned by `env`.
- Two keys of one user: either key writes the run the other created.
- Keys of different users: 409 with `owner` = the other user's name,
  `owner_user` the same.
- Names: user `admin` may exist together with a key named `admin`.

## Tests (`tests/test_server_users.py`, plus updates)

1. `UserStore` add/list/get/remove/set_role/set_identity; strict parse
   rejects extra/missing fields, bad role, duplicate name, duplicate
   identity, non-int or negative generation; corrupt file fails closed and
   logs once per change.
2. Last-admin refusal for remove and set-role; env user not counted.
3. `remove` deletes the user's keys; a running server rejects that key on
   the next request (401).
4. `create` default-user rule: 0 users → creates `admin`; 1 user → it;
   2 users → error; unknown user → error; admin scope for a member → error.
5. Migration: v1 `keys.json` with two keys + runs owned by those key names
   + an `env`-owned run + a run with corrupt `remote.json` → `users.json`
   has `admin`; `keys.json` v2 with `user: "admin"`; owners rewritten with
   `owner_key`; `env` run untouched; corrupt one warned and skipped; second
   run is a no-op (mtimes unchanged); crash simulation (keys v2 written, no
   `users.json`) completes; empty data dir → empty `users.json`.
6. `make_server` migrates; `server keys list` CLI migrates.
7. Ingest: viewer key `whoami` 200 with `role: viewer`; each write route
   (`missing`, `object`, `files/meta.json`, `append`, `commit`, lineage
   PUT) 403 without reading the body; member key creates a run;
   `remote.json` on disk has `owner` = user, `owner_key` = key; second key
   of the same user can append/commit to it; another user's key gets 409
   with `owner_user`.
8. Compatibility: `state` for the owning user's other key reports `owner`
   = that caller's key name and `remote.owner` likewise; the current sync
   client (`agenticml.sync`) syncs a run from two keys of one user and gets
   `OwnershipError` for another user's run. Use the existing test helpers
   in `tests/test_sync.py` / `tests/test_server_ingest.py`.
9. Dashboard session: an admin-key session ends when its user is demoted
   to `member` (next `/api/server/session` → 401); session info includes
   `user` and `role`.
10. CLI: each `users` subcommand, JSON output, error exit codes, and
    `keys create --user`.
11. `server status` handles v1 and v2 `keys.json` and attributes runs to
    keys through `owner_key`.

## Checks

- `PYTHONPATH=src /home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv/bin/python
  -m pytest -q tests` from the worktree root (all pass). The venv belongs
  to the main checkout; without `PYTHONPATH=src` it imports the main
  checkout's code, not this worktree's.
- No dashboard static files change, so the `ui` suite is not required.
- Report any deviation from this plan instead of changing the approach.

## As built (review outcome)

Reviewed by Sonnet (two rounds) and Codex (second review). Changes from
the plan that came out of review:

- `migrate()` returns `True`/`False` and logs every early return; the
  server still starts when it returns `False` (and then fails closed), while
  `server users`/`server keys` and `server up` stop with "could not migrate
  keys.json; fix the file and retry".
- A failed `remote.json` rewrite, a symlinked `store/` or `store/runs/`, or a
  version 2 `keys.json` with users other than `admin` abort the migration
  before `keys.json` and `users.json` are written.
- `users add` refuses a name that still owns runs unless `--reclaim-runs`.
- `state` hides `owner_key`/`last_sync_key` from other users.
- `server up` migrates before checking for an admin key.

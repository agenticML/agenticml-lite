# Package 14: Audit log

Nov 6, 2026 · Supervisor plan · Release 2.2 · Coder: Codex · Size: S–M

Source: [on-prem-teams-plan.md](../on-prem-teams-plan.md) ("Audit log").
Builds on packages 11 (users, `Principal`), 12 (per-user keys API) and 13
(sign-in, `WebAuth.client_address`). Package 15 builds the Activity view
on the API here and calls `AuditLog.record` from its users API.

## Goal

Every write on the server is attributed in an append-only
`<data>/server/audit.jsonl`: who (user, or `cli:<os user>`), how
(`via`), what (`action`, `target`), from where (`client`). Operators read
it with `agenticml server audit`; admins through `GET /api/server/audit`.

Done when every action below writes exactly one line in its test, the
reader handles a torn last line, and the whole `tests/` suite passes.

Out of scope: rotation, retention, export, signing/hash-chaining the log
(Backlog if wanted), the dashboard view (15).

## Files to touch

- `src/agenticml/server/audit.py` (new): `AuditLog`, `tail()`, the API
  handler.
- Call sites: `cli.py` (`server users`, `server keys`), `server/users.py`
  (`migrate` only), `server/keyspage.py`, `server/webauth.py` (setup,
  invite, login, logout, account password), `server/ingest.py` (run
  create / finish), `ui/server.py` (note write in server mode),
  `server/app.py` (construct one `AuditLog` on the server; expose as
  `server.audit`).
- `tests/test_server_audit.py` (new).

## Format

One JSON object per line, UTF-8, `\n`-terminated, keys in this order:

```json
{"ts": "<layout.format_timestamp()>", "actor": "alice", "via": "password",
 "action": "key.create", "target": "laptop", "client": "203.0.113.7",
 "detail": {"scope": "sync", "user": "alice"}}
```

- `actor`: principal user; `cli:<getpass.getuser()>` for CLI commands
  (`cli:unknown` if that fails); `server` for migration at start;
  `anonymous` for failed logins.
- `via`: principal `via` (`key`, `password`, `proxy`, `setup`, `invite`),
  or `cli` / `server`.
- `client`: `WebAuth.client_address(handler)` for HTTP, `null` for CLI.
- Strings in `target`/`detail` are truncated to 256 characters; control
  characters are escaped by `json.dumps` (`ensure_ascii=True`). **Never**
  log key values, key hashes, passwords, password hashes, invite tokens or
  session cookies.

## Actions

| Action | Where | target | detail |
| --- | --- | --- | --- |
| `migration` | `users.migrate` when it migrates | `admin` | `{keys, runs}` |
| `user.add` | CLI, users API (15) | user | `{role, identity}` |
| `user.remove` | CLI, users API | user | `{keys_revoked}` |
| `user.role` | CLI, users API | user | `{from, to}` |
| `user.identity` | CLI, users API | user | `{identity}` (null when cleared) |
| `user.invite` | CLI, users API, `server up` | user | `{expires_at}` |
| `user.password` | CLI set-password, invite redeem, account page, setup | user | `{method}` |
| `key.create` | CLI, keys API, `server up` (if it still creates keys) | key | `{user, scope}` |
| `key.revoke` | CLI, keys API, user removal (one line per key) | key | `{user, scope}` |
| `setup` | `/setup` success | user | `{}` |
| `login.ok` | `/login`, `/invite` redeem | user | `{method}` |
| `login.failed` | `/login` 401/403 (not 429) | attempted user name or `null` | `{method}` |
| `logout` | `/logout` with a session | user | `{}` |
| `run.create` | first `meta.json` of a run | run id | `{project, key}` |
| `run.finish` | `commit` that changes status to finished or crashed | run id | `{status, key}` |
| `note.write` | note PUT in server mode | run id | `{revision}` |

Proxy-identity requests are not logged per request (no `login.ok`).
Routine sync passes are not logged; only creation and finishing.

## `AuditLog`

- `AuditLog(server_dir)`; `record(actor, via, action, target=None,
  client=None, detail=None)`.
- Per write: `os.open(path, O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW|
  O_CLOEXEC, 0o600)`, refuse a non-regular file (`fstat`), `fcntl.flock`
  `LOCK_EX`, one `os.write` loop of the full line, `os.fsync`, close.
  Opening per write lets `logrotate` (`create` mode) rotate it.
- Called **after** the change succeeded. A failure to write the audit line
  logs `could not write audit.jsonl` at most once a minute and never fails
  the request or command (the change already happened).
- `tail(server_dir, limit=50, before=None)`: read backwards in 64 KiB
  chunks until `limit` complete lines are parsed, newest first; skip and
  count unparsable lines (including a torn last line); `limit` capped at
  1000. `before` is an optional `ts` cursor for paging.
- `GET /api/server/audit?limit=N[&before=TS]` (routed in `WebAuth.handle`
  next to `/api/server/keys`): needs `allowed(principal, "audit")`, else
  403; returns `{"events": [...], "skipped": n}`, `Cache-Control:
  no-store`.

## CLI

```
agenticml server audit [--data D] [--limit N] [--json]
```

Table columns `ts, actor, via, action, target, client` (`-` for empty);
`--json` prints the event list. Exit 1 with the usual error style when the
file is unreadable; an absent file prints nothing and exits 0.

## Edge cases

- `audit.jsonl` is a symlink or FIFO → refused (logged), request proceeds.
- Concurrent writers (server threads and a CLI) → whole lines only (flock).
- Very long user-controlled strings (identity, project) → truncated.
- `run.create` for a run created by an env key → actor `env`, via `key`.

## Tests (`tests/test_server_audit.py`)

1. Each action in the table writes exactly one line with the right fields
   (drive the real HTTP routes and CLI commands).
2. No secret appears in the file: after creating keys, invites and
   passwords, grep the file for each secret value and hash.
3. A symlinked `audit.jsonl` is not followed; the request still succeeds.
4. Concurrent records from 8 threads produce only whole JSON lines.
5. `tail` returns newest first, honours `limit` and `before`, and counts a
   torn last line as skipped.
6. `/api/server/audit` 403 for member and viewer, 200 for admin.
7. `agenticml server audit` table and `--json` output.

## Checks

- `PYTHONPATH=src /home/bharath/CAD-AI/agenticML/mlLinearTracking/testbed/.venv/bin/python
  -m pytest -q tests` from the worktree root.
- Report any deviation from this plan instead of changing the approach.

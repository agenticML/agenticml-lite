# Package 18c †: Self-serve sign-up and usage limits

Nov 8, 2026 · Supervisor plan · Release 3.0 · Coder: Codex (18c.1, 18c.2,
18c.3) · Size: M

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), "Self-serve sign-up
and usage limits (package 18c)", decision D2, exit criterion 7 and the
package summary. Builds on [18](18-workspaces.md) and
[18b](18b-firebase-sign-in.md). Packages 19 and 20 must keep these counters
correct on Postgres and across processes.

† Security-sensitive: second read-only review by the other coder (Cursor).
Strangers can upload to the hosted service; limits cap cost per workspace.

## Goal

With `--open-signup`, the first Firebase sign-in with a **verified** email
creates an account and a personal workspace on the `free` plan. With
`--limits FILE`, every workspace is held to its plan's numbers: storage,
uploads per day, requests per minute and people; and each account can own
a limited number of free workspaces. Server admins see usage and raise a
workspace's limits by hand (`custom` plan). Self-hosted defaults are
unchanged: sign-up off, no limits, and no extra database writes per
request.

Out of scope: billing, email delivery, abuse tooling beyond suspend,
Postgres specifics (19), multi-process specifics (20).

## Configuration

| Flag | Environment | Default |
| --- | --- | --- |
| `--open-signup` | `AGENTICML_SERVER_OPEN_SIGNUP` (`1`/`true`) | off |
| `--limits FILE` | `AGENTICML_SERVER_LIMITS` | unset: no limits |

`--open-signup` requires `--firebase-project` (refuse to start otherwise:
self-hosted password sign-in has no verified email).

Limits file (JSON, validated at start; unknown keys refused, numbers are
non-negative integers, `null` means unlimited):

```json
{
  "default_plan": "free",
  "free_workspaces_per_account": 1,
  "plans": {
    "free": {"storage_bytes": 5000000000, "upload_bytes_per_day": 2000000000,
             "requests_per_minute": 600, "people": 5}
  }
}
```

`docs/examples/limits.json` holds the D2 numbers. Without a limits file the
`plan` columns still exist but nothing is checked or counted except the
storage counter (cheap; kept exact so turning limits on later needs no
recount).

## Storage

Migration `0005_limits.py` (SQL must also run on Postgres in 19):

- `workspaces`: add `plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN
  ('free','custom'))`, `custom_limits TEXT` (JSON with the same four keys,
  only for `custom`), `personal INTEGER NOT NULL DEFAULT 0`.
  Existing workspaces become `free` (on a self-hosted server without a
  limits file this changes nothing).
- `objects (workspace_id, sha256, size BIGINT, created_at, PRIMARY KEY
  (workspace_id, sha256))`: which objects a workspace holds. Backfilled in
  the migration's Python step by listing each workspace's `objects/`
  directory (`store_root(data, wid)`); the migration needs the data
  directory, so pass it the way `import_state` gets it, or do the backfill
  in `import_state`-style startup code guarded by a `server_settings` flag
  (`objects_backfilled`). Pick the second if migrations cannot see the
  data dir; report the choice.
- `workspace_usage (workspace_id PRIMARY KEY, stored_bytes BIGINT NOT NULL,
  upload_day TEXT, upload_bytes BIGINT NOT NULL DEFAULT 0, request_minute
  BIGINT, request_count INTEGER NOT NULL DEFAULT 0)`. `stored_bytes` =
  SUM(run_files.size) + SUM(objects.size), initialised in the migration /
  backfill and maintained in the **same transaction** as every change to
  `run_files` or `objects` (record_file, replace_files, upsert_adopted,
  run delete, object insert, workspace delete).

New `UsageRepo(db, workspace_id)` in `db/repos.py` (or `db/usage.py`) owns
all of it; handlers never write SQL.

## Limits (`server/limits.py`, new)

`Plans` loaded from the file; `limits_for(workspace_row)` → effective
numbers (free plan, or `custom_limits` for custom). All checks happen
**before** any bytes are written or the body is read where the size is
known from `Content-Length`.

| Limit | Where | Rule | Answer |
| --- | --- | --- | --- |
| Storage | atomic, append, object (new only), lineage | `stored_bytes + growth > storage_bytes` → refuse. Growth: append `offset + length − committed` (truncate: `length − committed`), atomic `new − old size`, object `size` when not already held, lineage bundle size (counted as upload only, not storage; see edge cases) | 403 `{"error": "storage limit reached: 4.9 GB of 5.0 GB used; delete runs or ask for more"}` |
| Uploads per day | same routes, from `Content-Length` | `upload_bytes (today, UTC) + length > upload_bytes_per_day` | 403 `{"error": "daily upload limit reached: 2.0 GB of 2.0 GB today; resets at 00:00 UTC (in 3 h 12 min)"}` |
| Requests per minute | every authenticated request bound to a workspace (sync and `/api/w/...`) | counter per workspace per minute in `workspace_usage`, one upsert per request; > limit → refuse | 429 `{"error": "too many requests"}` with `Retry-After` = seconds to the next minute |
| People | dashboard invite create, member add, invite of an existing account | members + pending invites ≥ `people` → refuse | 403 `{"error": "people limit reached (5 of 5)"}` |
| Free workspaces per account | self-serve "New workspace" and sign-up | owned workspaces on `free` ≥ limit → refuse | 403 `{"error": "you already have a free workspace"}` |

- Storage and upload counters are updated after a successful write, in the
  transaction that records it (`record_file` / object insert gain an optional
  usage delta). Receive bodies in parallel, then take the run/object lock
  followed by `server.quota_lock(workspace_id)` for the fresh check, publish
  and recording. Without limits, skip quota locking and upload counters.
  Package 20 adds cross-process quota locking behind that method. Repair an
  on-disk object missing inventory without a storage check; remove a newly
  published object if recording fails.
- Reads, deletes, `/state`, `/missing`, `whoami`, `commit` and note edits
  are never refused for storage or uploads. Requests per minute applies to
  all of them (that is the point), except `GET /healthz` and sign-in.
- Server-admin CLI actions are never limited.
- With no limits file: no request counter writes, no checks.

## Sign-up (`--open-signup`, hosted)

Extends 18b's `POST /auth/firebase` for an unknown uid with no claimable
account and no invite:

- Verified email: in one transaction create the account (`login` =
  email, `display_name` from the token name or the email's local part)
  and a personal workspace: slug from the email local part
  (`[a-z0-9-]`, trimmed to 32, suffixed `-2`, `-3`… if taken or reserved),
  name `<display name>'s workspace`, `plan='free'`, `personal=1`, owner =
  the account; `prepare_store` its directory after commit. Audit
  `account.signup` and `workspace.create` (via `signup`). Then sign in as
  usual.
- Unverified email: create nothing; 403 `{"error": "verify your email",
  "reason": "email_unverified"}`; the sign-in page shows "Check your inbox
  to verify your email, then sign in again" with a "Resend email" button
  (Firebase `sendEmailVerification`).
- New workspace: `POST /api/account/workspaces {"slug", "name"}` (account
  route, CSRF, only with `--open-signup`, else 404). Requires a verified
  email (from the account row), checks the free-workspace limit, creates a
  `free` workspace owned by the account. Dashboard: "New workspace" item
  in the workspace switcher (hidden when sign-up is off) with a small
  form; the server's error text is shown when refused.

## Server admin

- CLI: `agenticml server workspaces usage [--json]` (all workspaces sorted
  by `stored_bytes` desc: slug, plan, storage used / limit, uploads today,
  people) and `agenticml server workspaces set-plan SLUG free|custom
  [--storage 100GB] [--uploads-per-day 10GB] [--requests-per-minute N]
  [--people N]` (sizes accept `KB/MB/GB/TB`, decimal; `unlimited`
  allowed). Audited `workspace.plan` with old and new values.
- `server workspaces recount SLUG|--all`: recompute `stored_bytes` from
  `run_files` and `objects` (repair tool; audited).
- Server-admin page (`/admin`): usage columns and a "Plan" edit form using
  `PATCH /api/admin/workspaces/<slug>` with `plan` and `limits` fields.

## Dashboard (18c.3)

- `GET /api/w/<slug>/usage` (members): used and limit for each of the four
  limits, or `{"limits": null}` without a limits file.
- Team page (managers) gets a "Usage" section with the four numbers.
- A banner on every workspace page at ≥ 80 % ("Storage 82 % used") and at
  100 % ("Storage limit reached: syncing is paused"), for storage and
  daily uploads; no banner without limits.
- No horizontal overflow at 375px.

## Sync client (`src/agenticml/sync.py`)

Under `sync --follow`, a run whose upload failed with HTTP 403 and an error
text starting `storage limit reached` or `daily upload limit reached` is
retried every 10 minutes instead of being dropped until restart; the
message is printed once per failure streak. One-shot `sync` is unchanged
(prints the server's error, exits non-zero). 2.2 clients already print the
403 text and stop.

## Constraints and conventions

- Protocol v2 is unchanged (new errors are existing status codes with an
  `error` text).
- Every check is per workspace and lives in the request binding or the
  ingest path; never trust a client-sent size beyond `Content-Length`
  (the body length is enforced as today).
- Counter updates go through repositories and the same transaction as the
  write they count. No disk scans on the request path.
- Numbers in messages: decimal GB with one decimal place, as D2.

## Edge cases

- Upload exactly at the limit (`used + growth == limit`) passes; one byte
  over fails; tests at, just under and just over for each limit.
- Append that only re-sends already-committed bytes (overlap) has zero
  growth and passes even at 100 %.
- Atomic replace with a smaller file reduces `stored_bytes`.
- Truncating append (`truncate=1`) growth is `length − committed`, may be
  negative.
- Object already held: no growth, `created: false`, passes at 100 %.
- Lineage bundles live on disk outside run files; they count toward
  uploads per day but not storage in 3.0 (documented; revisit if abused).
- Run deleted / workspace deleted: counters drop in the same transaction.
- Day rollover at 00:00 UTC resets `upload_bytes` lazily (row's
  `upload_day` differs from today).
- Clock skew between processes (20): minute and day keys come from the
  server process's clock in 18c (documented); package 20 switches them to
  the database clock.
- Custom plan with `null` fields: unlimited for that field.
- Plan file changed and server restarted: new numbers apply at once.
- Sign-up slug collisions and reserved slugs; email local part that is
  empty after cleaning → `workspace`.
- Second free workspace refused; being a *member* of many is fine.

## Tasks

- **18c.1 (limits core):** migration 0005 and backfill, `UsageRepo`,
  counters in every write path, `limits.py`, `--limits`, storage, uploads
  and requests checks in ingest and the dashboard binding, people limit,
  CLI `usage`, `set-plan`, `recount`, tests.
- **18c.2 (sign-up):** `--open-signup`, sign-up in `/auth/firebase`,
  personal workspace, `POST /api/account/workspaces`, free-workspace
  limit, unverified-email answer and page text, admin API plan fields,
  tests (route table and isolation suite updated for the new route).
- **18c.3 (dashboard and client):** usage API, Team page usage, banner,
  switcher "New workspace", admin page plan edit, `sync --follow` retry,
  UI tests.

## Tests and checks

- `tests/test_server_limits.py` (new): each limit at / under / over the
  boundary; an upload that would cross storage writes nothing (file size
  and `run_files` unchanged); counters equal a recount after a mixed sync
  (append, atomic, objects, delete); daily rollover with an injected clock;
  429 with `Retry-After`; people limit counts pending invites; no limits
  file → no counter rows written for requests.
- `tests/test_server_signup.py` (new): verified email creates exactly one
  account and one free workspace; second sign-in reuses them; second free
  workspace refused; unverified email creates nothing; sign-up off → 403
  no access as 18b.
- Client: a 2.2-style client (current client with follow off) prints the
  403 text and stops; `--follow` resumes after `set-plan` raises the limit
  (fake clock or short interval injected).
- UI (`-m ui`): usage section, banner at 80 % and 100 %, "New workspace"
  form; no horizontal overflow at 375px.
- Full suite and UI suite pass.

## Done when

Sign-up with a verified email creates exactly one free workspace and a
second free one is refused; each limit has a test at, just under and just
over the boundary; an upload that would cross the storage limit writes
nothing; a 2.2 client shows the 403 text and stops; a 3.0 `--follow`
client resumes after the limit is raised; the usage banner passes the UI
suite with no horizontal overflow at 375px.

# AgenticML 3.0: Database storage, workspaces and the hosted service

Nov 7, 2026 · Supervisor plan · Release 3.0 · Status: approved by the owner (Nov 7, 2026)

Roadmap: [on-prem-roadmap.md](on-prem-roadmap.md). Earlier designs:
[2.0](on-prem-server-plan.md), [2.2](on-prem-teams-plan.md). Package plans
are written one at a time, each before its package starts.

## Goal and exit criteria

One server codebase runs two ways:

- **Self-hosted** (open source, MIT, unchanged install): `pip install
  agenticml-lite` and `agenticml server serve --data DIR`. Server state lives
  in a SQLite file, run files and objects on local disk. One workspace to
  start with; admins can add more. No new runtime dependency, no Docker.
- **Hosted** (run by us on AWS): every customer is a **workspace** on one
  hostname, the way agenticml-server works. People have one account, belong
  to one or more workspaces with a role in each, and switch between them.
  Server state in Postgres (RDS), objects in S3, run files and lineage on
  EFS. Two or more server processes behind one load balancer. This replaces
  agenticml-server (`api.agenticml.xyz`, `dashboard.agenticml.xyz`).

Exit criteria, checked by tests and one real deployment:

1. The full server test suite passes on SQLite and on Postgres.
2. Two workspaces on one Postgres-backed deployment, each with an admin, a
   member and a sync key, sync runs and use the dashboard. One account
   belongs to both, with a different role in each, and switches between
   them. No request from a non-member (key, session or URL) can read, list,
   write or detect anything in the other workspace (isolation suite,
   package 18).
3. Two server processes share one database and data directory; syncing
   the same run through both gives the same result as one process, and
   killing one process mid-upload loses nothing that was acknowledged.
4. The runs list can be sorted and filtered by a metric summary (last,
   min, max) without reading run files.
5. A 2.2 sync client syncs to a 3.0 server unchanged (protocol v2 stays).
6. The hosted stack runs in our AWS account on one hostname with two
   customer workspaces, a deploy rolls with no lost uploads, and a restore
   from backup has been tried once.
7. A stranger signs up on the hosted service, verifies their email, gets a
   personal workspace on the free limits, syncs until the storage limit,
   gets a clear "storage limit reached" error, and syncs again after a
   server admin raises that workspace's limit.

## What changes and what does not

| | 2.2 | 3.0 self-hosted | 3.0 hosted |
| --- | --- | --- | --- |
| Accounts, workspaces, memberships, keys, invites, sessions, login throttle, audit | JSON files + memory | SQLite | Postgres |
| Run list, status, owner, sync sizes (`remote.json`), notes | files per run | SQLite | Postgres |
| Metric summaries (new) | – | SQLite | Postgres |
| Run files (`meta.json`, `events.jsonl`, `scalars/*`, `media/index.json`) | v1 store on disk | v1 store on disk | v1 store on EFS |
| Objects (code, media, artifacts) | `objects/` on disk | `objects/` on disk | S3 |
| Lineage (shadow Git) | `lineage/` on disk | on disk | EFS |
| Workspaces | – | `default`, more if the admin adds them | one per customer |
| Hostnames | one | one | one (`app.agenticml.xyz`) |
| Server processes per data directory | one (`server.lock`) | one | many |

Unchanged:

- The SDK, the local v1 store, `agenticml ui`, and `docs/FORMAT.md`.
- Sync protocol v2 (`docs/PROTOCOL.md`): same endpoints, same bytes. A key
  belongs to exactly one workspace, so the server knows the workspace from
  the key and the client does not change. The server stops writing
  `remote.json` into run directories; the same data is in the database
  and `/v2/runs/<id>/state` answers as before. (`artifacts.json` stays a
  run file; see package 17.)
- Workspace roles follow agenticml-server: `owner`, `admin`, `member`,
  `viewer` (2.2's three roles plus `owner`), per workspace.
  Self-hosted sign-in stays as in 2.2 (password + invite, trusted identity
  proxy, admin key fallback); the hosted service signs in with Firebase
  (D9). CSRF and session rules stay in both.

## Decisions

Made by the owner (Nov 7, 2026):

- Server state moves to a database: SQLite for self-hosted, Postgres for
  hosted, one schema and one code path.
- Customers are **workspaces on one hostname**, like agenticml-server. No
  subdomain per customer, no stack per customer.
- Metrics stay as v1 files; the database holds one summary row per run and
  metric. No metric points in the database.
- The 3.0 hosted service replaces agenticml-server outright. agenticml-server
  has no customers, so nothing is migrated from it.
- We host customer data, including code diffs (lineage). The open-source
  self-hosted server stays a full product, not a demo.
- **Self-serve sign-up with usage limits at launch** (option C, Nov 7,
  2026): anyone can sign up and create a workspace on the hosted service,
  within free limits (package 18c). Billing comes after 3.0.

Recommended by the supervisor and approved by the owner on Nov 7, 2026
(**D1–D9**):

- **D1 Hostname.** `app.agenticml.xyz` while 3.0 is built and tested; in
  package 26 `dashboard.agenticml.xyz` points at it too. One ordinary ACM
  certificate, no wildcard.
- **D2 Free limits (self-serve chosen by the owner).** Every
  new workspace gets the `free` plan: 5 GB storage (run files plus
  objects), 5 people (members and pending invites), 600 API requests a
  minute and 2 GB of uploads a day. Each account can own 1 workspace on
  the free plan, and can be a member of any number. A server admin can
  set any workspace to a `custom` plan with its own numbers (until billing
  exists, this is how a paying customer gets more). Worst case a full free
  workspace costs about $1.50/month if all 5 GB sat on EFS, much less on
  S3.
- **D3 agenticml-server features.** Not ported in 3.0: experiments and
  hypotheses, architectures / model design, AI threads, datasets, cost and
  budget, cloud connections. 3.0 ships what the lite dashboard has today
  (runs, charts, media, artifacts, lineage and code diffs, notes, team).
  Each missing feature becomes a "Later" roadmap entry the owner can pull
  forward.
- **D4 Client support window.** The hosted server accepts sync clients from
  the current and the previous minor release (3.0 accepts 2.2 and 3.0). A
  client older than that gets a clear "upgrade agenticml-lite" error.
- **D5 2.x data directories.** A 2.x self-hosted data directory converts in
  place on first start of 3.0 into one `default` workspace (package 23).
  Drop it if the owner knows no 2.x server is in use.
- **D6 Where the AWS code lives.** `deploy/aws/` in this repo (Dockerfile,
  Terraform, README) as a reference for self-hosters on AWS too. The image
  is built only by our deploy workflow into our private ECR and is never
  published. This amends the Nov 5 "PyPI only" decision for the hosted
  service only; customers still install from PyPI.
- **D7 Optional dependencies.** Base install stays dependency-free.
  `agenticml-lite[postgres]` adds `psycopg[binary]>=3.1`;
  `agenticml-lite[s3]` adds `boto3`; `agenticml-lite[firebase]` adds
  `PyJWT[crypto]` (the standard library cannot verify RS256 signatures);
  `agenticml-lite[hosted]` is all three.
- **D8 Version.** 3.0.0: the server data format changes and the hosted
  service is new.
- **D9 Accounts and sign-in.** An account is global and the same account
  can be in many workspaces. The hosted service signs people in with
  **Firebase**, as agenticml-server does (Google and email/password, with
  Firebase handling password resets and email verification); reuse
  agenticml-server's Firebase project, which has no customers. Self-hosted
  servers keep 2.2 sign-in, because an open-source install must not depend
  on our Firebase project; Firebase is an option a self-hoster can turn on
  with their own project. Server admin is a separate flag on the account,
  like agenticml-server's platform role: it creates, renames and suspends
  workspaces but does **not** see a workspace's runs unless it is also a
  member. Support access to a customer's workspace means the customer adds
  us as a member, which the audit log records.

## Design

### Configuration

| Flag | Environment | Default | Meaning |
| --- | --- | --- | --- |
| `--data DIR` | `AGENTICML_SERVER_DATA` | as today | Run files, lineage, local objects, SQLite file |
| `--database URL` | `AGENTICML_SERVER_DATABASE_URL` | `sqlite:///<data>/server/agenticml.db` | `sqlite:///path` or `postgresql://...` |
| `--objects URL` | `AGENTICML_SERVER_OBJECTS` | `file://<data>` | or `s3://bucket/prefix` |
| `--open-signup` | `AGENTICML_SERVER_OPEN_SIGNUP` | off | Anyone with a verified sign-in can create a workspace (hosted: on) |
| `--limits FILE` | `AGENTICML_SERVER_LIMITS` | unset (no limits) | JSON with the plans and their numbers (hosted: the D2 numbers) |

There is no separate "multi-tenant mode": hosted and self-hosted run the
same workspace code; hosted simply has many workspaces. The database URL can
hold a password, so it is never logged, never shown in `server status`, and
on AWS comes from Secrets Manager. More than one server process requires
Postgres; SQLite keeps `server.lock` and one process.

### Storage layer (package 16)

New package `src/agenticml/server/db/`:

- `connect(url)` returns a backend: `sqlite.py` (stdlib `sqlite3`) or
  `postgres.py` (`psycopg`, imported only when used; a clear error names
  the `[postgres]` extra when missing).
- A small dialect layer: placeholders, `INSERT ... ON CONFLICT`, row locks
  (`SELECT ... FOR UPDATE` on Postgres, `BEGIN IMMEDIATE` on SQLite),
  advisory locks (Postgres `pg_advisory_xact_lock`, SQLite: the write
  transaction itself). Handlers never write SQL; they call repository
  methods (`AccountRepo`, `WorkspaceRepo`, `KeyRepo`, `SessionRepo`,
  `AuditRepo`, `RunRepo`).
- Migrations: numbered SQL files in `db/migrations/`, one table
  `schema_migrations`, applied at start under a lock. A database newer than
  the code refuses to start with a clear message.
- SQLite settings: WAL, `synchronous=FULL`, `foreign_keys=ON`,
  `busy_timeout`. The file lives in `<data>/server/` with mode 0600. On
  Linux, refuse to open it on an NFS/EFS mount (`/proc/self/mountinfo`) and
  point to Postgres.

Package 16 moves 2.2's single-team state into tables as it is (users,
keys, sessions, throttle, audit). Package 18 then reshapes users into
accounts and memberships. Doing it in two steps keeps each diff reviewable
and lets the full 2.2 test suite prove 16 changed storage only.

### Tables (after package 18)

Every workspace-owned table has `workspace_id` as the first column of its
primary key and of every index.

| Table | Replaces | Notes |
| --- | --- | --- |
| `accounts` | `users.json` | id, email or 2.x name (unique), display name, scrypt record, identity (proxy), server_admin flag, created/updated |
| `workspaces` | – | id (server-made), slug (unique, used in URLs), name, status (`active`, `suspended`), plan (`free`, `custom`), custom limits, created_by, created_at |
| `workspace_usage` | – | workspace, stored bytes, upload bytes today, request counter window; updated in the same transaction as each write |
| `memberships` | role in `users.json` | account, workspace, role (`owner`, `admin`, `member`, `viewer`) |
| `invites` | invite fields in `users.json` | workspace, email, role, token digest, expiry, invited_by |
| `keys` | `keys.json` | name, account, workspace, scope, SHA-256 digest (globally unique, so a key resolves to exactly one workspace), created, last_used, revoked |
| `sessions` | memory | token digest, account, CSRF token digest, created, last_seen, expires. One session per sign-in covers all of the account's workspaces; sessions survive restarts and work across processes |
| `login_attempts` | memory | throttle buckets (address or `account:<login>`), window counts, locked_until |
| `audit_events` | `audit.jsonl` | 2.2 fields plus workspace (null for server-level events such as workspace creation); no secrets |
| `runs` | scan of `runs/` + `remote.json` | workspace, run id, project, name, owner account, status, fingerprint, created, last_sync_at, tags, notes |
| `run_files` | `remote.json` `sizes` | workspace, run id, path, committed size, SHA-256 of atomic files |
| (none) | `artifacts.json` | stays a run file on disk, tracked in `run_files` (changed in package 17: it is client content read by the shared `RunReader`) |
| `run_metrics` | – | workspace, run id, series id, name, last, min, max, count, last step, decoded byte offset |
| `objects` | – | workspace, sha256, size, created (which objects a workspace has) |
| `lineage_refs` | – | workspace, run id, project key, commit |

### Run files and crash safety (package 17)

The database is the source of truth for how many bytes of each run file
are committed. An append runs in one transaction:

1. Lock the `run_files` row.
2. Check the client's expected offset against the committed size (409 on
   mismatch, as today).
3. Truncate the file to the committed size, which drops any tail left by a
   crash, then write, fsync the file and directory.
4. Update the size and the run's `last_sync_at`, then commit.

Atomic files (`meta.json`, indexes) are written with the 2.0 temp + rename +
fsync rule, then recorded. A crash between file and commit leaves either
the old state or bytes beyond the committed size, which the next append
truncates. Readers already tolerate a partial last record (FORMAT.md).

### Workspaces and accounts (package 18)

Same model and checks as agenticml-server (`backend/deps.py`,
`backend/routers/workspaces.py`):

| agenticml-server | 3.0 |
| --- | --- |
| Global users (Firebase uid), `user_workspaces(user, workspace, role)` | `accounts` + `memberships` |
| Roles `owner` (creator, cannot be removed, everything an admin can do), `admin`, `member`, `viewer`; assignable: `admin`, `member`, `viewer` | Same. 2.2 permissions map on: admin and owner manage members, invites and keys; member syncs and edits notes; viewer reads |
| `require_workspace_member`, `workspace_membership`, `require_workspace_manager` (`owner`, `admin`) | Same three checks, as one function each in `server/access.py`, run before every workspace handler |
| API keys bound to one workspace; only a signed-in browser session can create or delete keys | Same. Keys also keep 2.2's per-account owner, so a member's machines write only that member's runs |
| A run in another workspace is 404, not 403 (`require_run`) | Same rule, applied to every "not yours" case (see below) |
| Platform admin (`users.role = 'admin'`); its routes are 404 for everyone else | Server admin flag; same 404 |
| Workspace invites by email with a role | Same |
| Personal workspace on first sign-in; anyone can create a workspace | Same flow, on in the hosted service, with free limits (package 18c) |

Two deliberate differences:

1. **No "current workspace".** agenticml-server picks the workspace from an
   `x-workspace-id` header and silently falls back to the user's default
   workspace when the header names one they do not belong to. Its own
   AGENTS.md then had to add "feature checks use the workspace in the
   path". 3.0 always takes the workspace from the URL (or, for sync, from
   the key), and a workspace you do not belong to is an error, never a
   fallback, so a request can never land in the wrong workspace.
2. **One answer for "not yours".** agenticml-server answers 403 "Not a
   member of this workspace" on some workspace routes and 404 on runs.
   3.0 answers 404 for all of them, so nobody can tell which workspaces
   exist.

Routing and access:

- Dashboard pages live under `/w/<slug>/...` and dashboard APIs under
  `/api/w/<slug>/...`. Account pages (`/account`, sign-in, invites) and
  server-admin pages (`/admin/workspaces`) have no workspace.
- Before any handler runs, the request is bound to one workspace: the
  path's slug is resolved, the session's account must be a member, and the
  member's role is checked against the action. Repositories are created
  for that workspace and cannot be built without one.
- A non-member, an unknown slug and a suspended workspace all get the
  same 404, so nobody can find out which workspaces exist.
- Sync (`/v2/...`) takes the workspace from the key. A key cannot name a
  different workspace.
- `/` sends a signed-in account to its last-used workspace (a cookie on
  the client, not server state) or a picker. The account menu gets a
  workspace switcher; the Team page (2.2) becomes per workspace.
- Invites are per workspace and name an email. Inviting an email that
  already has an account adds a membership. Otherwise the invite link lets
  the person sign in (Firebase hosted, set a password self-hosted) and then
  joins them; with Firebase the signed-in email must match the invite and
  be verified.
- Files: `<data>/workspaces/<workspace_id>/store/...` and
  `<data>/workspaces/<workspace_id>/lineage/...`. Ids are server-made, so
  renaming a workspace or its slug never moves files.
- Objects are deduplicated per workspace only, never across workspaces:
  the "missing objects" endpoint must not reveal whether another customer
  has a file. S3 keys: `workspaces/<workspace_id>/objects/<hh>/<sha256>`.
- Postgres row-level security as a second guard (package 19): every
  workspace table has a policy on `current_setting('app.workspace_id')`,
  set per transaction.
- Server-admin CLI and page: `agenticml server workspaces
  create|list|rename|suspend|resume|delete` and `agenticml server accounts
  list|set-server-admin`. `delete` removes rows, files and the S3 prefix
  after a typed confirmation and writes a server-level audit line.
- Every customer uses the same server URL: `agenticml login
  https://app.agenticml.xyz` with a key from their workspace. The key picks
  the workspace. The client stores one key per server URL, so a machine
  that must sync to two workspaces on the same server (rare: a consultant,
  or us testing) sets `AGENTICML_API_KEY` per process, which the client
  already supports. Documented in package 25; no client change.
- Self-hosted: the `default` workspace exists from first start, and `/`
  goes straight to it when the account has only one workspace, so a
  one-team server looks like 2.2. Workspaces also give self-hosters the
  access boundary the roadmap listed as "project-level access".

### Firebase sign-in (package 18b, hosted)

Firebase answers **who** someone is. The membership check above still
decides **which workspaces** they can open; Firebase does not replace it.

- The sign-in page loads the Firebase web SDK (vendored into the
  dashboard's static files, so the `script-src 'self'` CSP stays; only the
  Firebase auth endpoints are added to `connect-src` and `frame-src`).
- The browser signs in with Firebase and posts the ID token once to
  `/auth/firebase`. The server verifies it (RS256 against Google's cached
  public keys, issuer and audience = our project, expiry, `auth_time`),
  finds or creates the account by Firebase uid, and issues the server's own
  session cookie. From then on, sessions, CSRF and the throttle work as in
  2.2; no Firebase token is sent on every request.
- An account is linked to Firebase by uid, never by email alone. An
  unverified email cannot accept an invite.
- Configuration: `--firebase-project ID` (`AGENTICML_SERVER_FIREBASE_PROJECT`).
  With it set, password sign-in and the 2.2 admin-key dashboard login are
  off, as in agenticml-server where keys are for the SDK only. Break-glass
  access is the server-admin CLI, run on the server (ECS Exec).

### Self-serve sign-up and usage limits (package 18c)

Sign-up (hosted, `--open-signup`):

- First Firebase sign-in with a **verified** email creates the account and
  a personal workspace (`<name>'s workspace`, owner = the account) on the
  `free` plan. An unverified email can sign in but sees "verify your
  email" and cannot create a workspace or accept an invite.
- "New workspace" in the workspace switcher; refused with a clear message
  once the account owns its free-plan allowance.
- Self-hosted servers keep `--open-signup` off by default, so a self-hoster
  is never surprised by strangers creating workspaces.

Limits, checked in the server before any bytes are written:

| Limit | Counted | When exceeded |
| --- | --- | --- |
| Storage per workspace | committed run-file bytes + object bytes, from `run_files` and `objects` (exact, no disk scans) | 403 `{"error": "storage limit reached: 5.0 GB of 5.0 GB used ..."}` on uploads; reads, deletes and notes keep working |
| Uploads per day | bytes accepted per UTC day | 403 with the same shape and the reset time |
| Requests per minute | per workspace, shared across server processes (database counter) | 429 with `Retry-After` (clients already retry 429) |
| People per workspace | members + pending invites | invite refused in the dashboard |
| Free workspaces per account | workspaces the account owns on `free` | "New workspace" refused |

- Why 403 for storage: the sync client treats 4xx (except 408 and 429) as
  final for that run and prints the server's `error` text, so a 2.2 client
  shows "HTTP 403: storage limit reached ..." and stops instead of
  retrying. 5xx would make it retry forever.
- 3.0 client change (small, `sync.py`): under `sync --follow`, a run that
  failed with "storage limit reached" is retried every 10 minutes instead
  of being dropped until restart, so raising the limit resumes syncing.
- The dashboard shows usage against the limits on the workspace settings
  page and a banner at 80% and 100%.
- Server admins: `agenticml server workspaces usage` (all workspaces,
  sorted by storage) and `set-plan WORKSPACE custom --storage 100GB ...`;
  the same on the server-admin page. Every plan change is audited.
- Abuse handling: suspend a workspace (exists in 18), which stops reads
  and writes; deleting it frees the storage.

### Many processes (package 20)

Postgres mode drops `server.lock`. What was in-process becomes database
state: the run and object locks (row and advisory locks), the throttle,
sessions, and the lineage fetch lock (advisory lock per workspace and
project). The code-diff cache stays per process.

### S3 objects (package 21)

An object backend interface: `exists`, `missing(list)`, `put(stream, sha,
size)` (hash while streaming, reject a mismatch, never overwrite),
`open_range`, `delete_prefix`. `file://` keeps today's layout and code;
`s3://` uses boto3 with server-side encryption. Downloads stream through
the server (no pre-signed URLs in 3.0, so access checks stay in one place).

### Metric summaries (package 22)

On each append to `scalars/<n>.bin`, the server decodes the new complete
records (from the stored decoded offset) and updates the summary row in
the same transaction. `/api/w/<slug>/runs` gains
`sort=metric:<name>:<last|min|max>` and simple filters
(`metric:<name>:<op>:<value>`). The runs page adds a metric column picker
and sort. A rebuild command recomputes summaries from the files.

## Packages, order and owners

| # | Package | Depends on | Coder | Size |
| --- | --- | --- | --- | --- |
| 16 † | Database layer, SQLite backend, migrations; 2.2 users, keys, sessions, throttle and audit move to tables | – | Codex | L |
| 17 † | Runs, sync state and notes in the database; append crash safety; dashboard run list from the database | 16 | Cursor | L |
| 18 † | Workspaces and accounts: memberships, workspace-bound keys, `/w/<slug>` routes, switcher, server-admin commands, isolation suite | 17 | Codex | L |
| 18b † | Firebase sign-in for the hosted service | 18 | Cursor | M |
| 18c † | Self-serve sign-up and usage limits | 18b | Codex | M |
| 19 † | Postgres backend, row-level security, CI with Postgres, suite on both backends | 18 | Cursor | M |
| 20 † | Many server processes: database locks, shared throttle and sessions | 19 | Cursor | M |
| 21 | S3 object backend | 18 | Codex | M |
| 22 | Metric summaries, sort and filter in the runs list | 17 (after 18 lands) | Cursor | M |
| 23 | Operator commands: 2.x conversion (D5), `server backup`, `status` / `verify` on the database | 18 | Codex | S–M |
| 24 | AWS hosted deployment: Dockerfile, Terraform, deploy workflow, runbook, real-EFS benchmark | 18c, 19, 20, 21 | Codex; owner runs it | M |
| 25 | Docs, threat model, version 3.0.0, release checklist | 16–24, 18b, 18c | Codex; prose Antigravity | M |
| 26 | Retire agenticml-server | 24 live | Owner, with `git` agent for the repo | S |

† security-sensitive: second read-only review from the other coder.

Coders are tentative, assigned by fit (Codex for wide changes, Cursor for
concurrency and crash-safety reasoning) and adjusted for limits at the time.
18 touches almost every server file and the dashboard, so nothing else runs
in parallel with it. After 18, 18b and 21 can run in parallel; 18c
follows 18b, then 19; 22 and 23 can follow in parallel (different files).
19 and 20 must cover 18c's counters on Postgres and across processes.

### Package summaries

- **16 Database layer.** `server/db/` with the SQLite backend, dialect
  layer, migrations and repositories. `users.py`, `keys.py`, `audit.py`,
  `webauth.py` (sessions, throttle, setup token) and `storeio.py` move to
  repositories. `users.json`, `keys.json`, `keys.lock` and `audit.jsonl` are
  no longer written. Done when every existing server test passes on SQLite,
  sessions survive a restart, and the audit view reads from the table.
- **17 Runs in the database.** `ingest.py`, `lineage.py`, `byref` and the
  dashboard run listing move to `RunRepo`. `remote.json` and
  `artifacts.json` are no longer written; `/v2/runs/<id>/state` and the
  dashboard read the database. Append transaction as above. Done when the
  sync tests pass unchanged, a crash injected between write and commit
  (test hook) leaves a run that the next sync completes, and the runs page
  lists 10,000 runs without opening run directories.
- **18 Workspaces and accounts.** As designed above (agenticml-server's
  model and checks, with the two differences), including the
  dashboard changes (workspace URLs, switcher, per-workspace Team page,
  server-admin workspace page). Done when the isolation suite passes: for
  every route in the server (enumerated from the handler table, so a new
  route without a test fails CI), a non-member gets the same answer as for
  a nonexistent workspace, and every key, session and URL combination
  across two workspaces is covered; the UI suite passes with no horizontal
  overflow at 375px.
- **18b Firebase sign-in.** As designed above. Done when token
  verification is tested with locally signed tokens (wrong signature,
  issuer, audience, expired, unverified email, unknown uid) without calling
  Google, invite acceptance by verified email works, the UI suite covers the
  sign-in page, and one real sign-in against the Firebase project passes
  (owner).
- **18c Sign-up and limits.** As designed above. Done when: sign-up with
  a verified email creates exactly one free workspace and a second free
  one is refused; each limit has a test at, just under and just over the
  boundary; an upload that would cross the storage limit writes nothing; a
  2.2 client shows the 403 text and stops; a 3.0 `--follow` client resumes
  after the limit is raised; the usage banner passes the UI suite with no
  horizontal overflow at 375px.
- **19 Postgres.** `postgres.py`, RLS policies in migrations, a CI job with
  a `postgres:16` service running the whole server suite with
  `AGENTICML_TEST_DATABASE_URL`. Done when both backends pass and an RLS
  test shows a query without `app.workspace_id` returns nothing.
- **20 Many processes.** Done when a test runs two server processes on one
  Postgres and data directory, syncs many runs through both in parallel
  (including the same run), kills one mid-upload, and the result equals a
  single-process sync.
- **21 S3 objects.** Done when the object tests pass against a moto S3 (CI)
  and once against real S3 (owner), and a hash mismatch or another
  workspace's key is rejected.
- **22 Metric summaries.** Done when summaries match a full read of the
  files for every series in the test corpus (including NaN, inf and
  restarts), the sort/filter API is tested, the UI suite passes, and the
  runs page has no horizontal overflow at 375px.
- **23 Operator commands.** (Changed Nov 7, 2026: packages 16, 17 and 18
  each import the 2.x state they move, so a 2.x directory keeps working
  through the release; 23 keeps the end-to-end 2.2-directory test, the
  refuse-twice hardening, backup and `status`/`verify`.) 2.x conversion on first start into the
  `default` workspace: import `users.json` (users become accounts with a
  membership), `keys.json`, `audit.jsonl`, every `remote.json` and
  `artifacts.json`; the earliest 2.x admin becomes the workspace
  `owner`; move run files under the workspace directory; keep the
  originals renamed `*.2x`; refuse to convert twice. `server backup --to
  DIR` makes a consistent copy while running (SQLite backup API, `pg_dump`
  note for Postgres, then run files; the truncate rule makes the pair
  consistent). Done when a 2.2 test data directory converts and syncs, and
  a backup taken during a sync restores to a working server.
- **24 AWS deployment.** `deploy/aws/`: Dockerfile (pinned Python, installs
  `agenticml-lite[hosted]==<version>`, non-root, healthcheck) and Terraform
  for: VPC with two AZs and one NAT gateway, ALB with the ACM certificate
  for `app.agenticml.xyz`, Route 53 record, ECS Fargate service (2 tasks,
  rolling deploy), RDS Postgres (encrypted, automated backups), S3 bucket
  (encrypted, public access blocked), EFS with an access point (encrypted,
  AWS Backup), Secrets Manager for the database URL, CloudWatch logs and
  alarms on `/healthz`, 5xx and RDS storage. `.github/workflows/deploy.yml`
  copies agenticml-server's pattern: GitHub OIDC role, immutable ECR tags,
  register task definition, roll, wait for stable. A runbook covers first
  deploy, the first server admin, adding a customer workspace, upgrades,
  restore, and incident basics. The real-EFS benchmark Backlog item is done
  here. Done when exit criterion 6 holds.
- **25 Docs and release.** `docs/SERVER.md` (both modes, configuration,
  workspaces, backups), `docs/HOSTED.md` (new: operating the hosted
  service), `docs/THREAT_MODEL.md` (workspace isolation, operator access to
  customer data, shared Git processing, noisy neighbours),
  `docs/PROTOCOL.md` (client support window, workspace sync URLs, no more
  `remote.json` on the server), README, website, version 3.0.0. Done when
  the release checklist passes.
- **26 Retire agenticml-server.** Owner-run, after 24 has served real use:
  point `dashboard.agenticml.xyz` at the new service, stop and delete the
  old ECS service, RDS, S3 + CloudFront and deploy roles (each deletion
  confirmed by the owner), archive the repository.

## Estimated hosted cost

Approximate us-east-2 list prices per month, before data transfer and
storage growth: ALB ~$20, two Fargate tasks (0.5 vCPU, 1 GB) ~$36, RDS
`db.t4g.small` single-AZ ~$25 plus storage, one NAT gateway ~$35, EFS and
S3 by usage, Route 53 ~$1. About **$120/month for all workspaces
together**; a new customer adds only storage. Multi-AZ RDS (~+$25) when the
first paying customer needs it.

## Risks

- **Cross-workspace leaks** are the main risk, and with one hostname there
  is no browser-level separation between customers to fall back on.
  Firebase proves who is signed in, but a signed-in customer asking for
  another customer's workspace is still stopped only by the membership
  check. Mitigations: workspace bound from the URL or key before handlers,
  repositories that need a workspace, one 404 for every "not yours" case,
  RLS, per-workspace object dedupe, the route-enumerating isolation suite,
  and second reviews.
- **EFS latency** for appends with many clients. Package 24 measures it;
  the fallback is larger append batches on the client or run files on S3
  (Later).
- **Two database backends drift.** One test suite runs on both in CI; no
  backend-specific SQL outside the dialect layer.
- **Strangers on the hosted service.** Open sign-up means anyone can
  upload. Limits cap cost per workspace, verified email is required, and
  each account owns one free workspace, but someone with many email
  addresses can still open many free workspaces. Watch `workspaces usage`
  after launch; if it is abused, add Firebase App Check or a disposable-
  email blocklist (Later), or turn `--open-signup` off without a deploy.
- **Hostile lineage bundles** now run on shared infrastructure. The 2.1
  Git hardening stays; repos are per workspace; Git runs under the same
  timeouts and no hooks.

## Later (after 3.0)

Billing and paid plans (Stripe), email delivery (SES) for invites (3.0
shows invite links to copy; Firebase already sends verification and reset
emails), Firebase App Check and a disposable-email blocklist if sign-up is
abused, removal of long-inactive free workspaces, enterprise SSO (SAML / OIDC
per workspace through Firebase / Identity Platform), run files on S3, Multi-AZ by default, and the agenticml-server
features from D3.

## Release checklist

- [x] Packages 16–25 done, each with its plan file and tests (24.3 is the
      owner's AWS apply; see the roadmap Backlog).
- [x] `testbed/.venv/bin/python -m pytest -q tests` passes on SQLite, and
      the server suite passes on Postgres in CI.
- [x] UI suite passes; no horizontal overflow at 375px.
- [x] `scripts/server_smoke.sh` passes on a fresh venv with the base install
      (no extras).
- [ ] A 2.2 client syncs to 3.0 (self-hosted and hosted). Self-hosted: done
      (`tests/test_server_client_window.py`); hosted: owner, after 24.3.
- [ ] Exit criteria 1–7 hold. 1–5 hold (tests); 6 and 7 need the live
      hosted service (owner, roadmap Backlog).
- [ ] Terms of service, acceptable use policy and privacy policy published
      on agenticml.xyz before sign-up opens (owner; not code).
- [x] Docs updated; roadmap statuses set; version 3.0.0.

# AgenticML On-Prem Roadmap

Nov 3, 2026 · @bharath

This roadmap is the list of planned work for the on-prem product: the order of
releases, what each one contains, and how far along each package is. Agents
read it at the start of a session (see [AGENTS.md](../../AGENTS.md)). Detailed
plans live next to it:

- Release 2.0: [on-prem-server-plan.md](on-prem-server-plan.md)
- Release 2.1: sketched below, with package plans in `packages/`.
- Release 2.2: [on-prem-teams-plan.md](on-prem-teams-plan.md)
- Release 3.0: [on-prem-3.0-plan.md](on-prem-3.0-plan.md)

## Product direction

- One server codebase, two ways to run it (changed Nov 7, 2026, for 3.0):
  - **Self-hosted:** customers run the open-source server on their own
    machines, with nothing sent to us.
  - **Hosted:** we run the same server on AWS on one hostname; each customer
    is a workspace, like agenticml-server. We hold their data, including
    code diffs. It replaces agenticml-server.
- Everything is open source under MIT in `agenticML/agenticml-lite`, including
  the server and the team features. Revenue comes from the hosted service,
  support and managed installs, not closed code.
- The local workflow (training plus `agenticml ui`) is never broken to make
  the server work. The SDK and the local v1 store do not change.
- From 3.0, server state lives in a database (SQLite self-hosted, Postgres
  hosted); run files stay v1 files and metrics stay out of the database
  except for one summary row per run and metric.

## Releases

| Release | Theme | Contents | Exit criteria |
| --- | --- | --- | --- |
| **2.0** | Self-hosted server, single user | Test CI, server ingest, protocol v2 sync, shared dashboard with key login and key issuing, `sync --follow`, systemd units and server guide (PyPI only; no container image) | Release checklist in the 2.0 plan, section 8 |
| **2.1** | Full fidelity | Lineage code diffs on the server, by-reference artifacts, EFS performance check, operator tooling | A run synced to the server is indistinguishable from the local one in the dashboard, apart from notes |
| **2.2** | Teams | Users and roles, per-user keys, dashboard sign-in through a trusted identity proxy, audit log | Two users with different roles share one server, each with the access their role allows, and every write is attributed |
| **3.0** | Database storage, workspaces and hosted service | Server state in SQLite or Postgres, workspaces with accounts and memberships on one hostname, Firebase sign-in and self-serve sign-up with free limits, many server processes, S3 objects, metric summaries, AWS deployment, agenticml-server retired | Exit criteria 1–6 in the [3.0 plan](on-prem-3.0-plan.md) |
| Later | Scale and lifecycle | Unscheduled; see below | – |

Decision (Nov 5, 2026): the server ships only as the `agenticml-lite`
package on PyPI and runs as `agenticml server serve`, typically under
systemd behind a TLS proxy. The Docker image, compose file, ECS/Terraform
example and image workflow added in package 6 were removed
([plan](packages/remove-docker-deployment.md)).

Amended (Nov 7, 2026, decision D6 in the 3.0 plan):
customers still install from PyPI. Our hosted service is built from a
Dockerfile in `deploy/aws/` into our private ECR; no image is published.

## Status

Every package has two tasks, in this order:

1. **Plan (supervisor).** Write `docs/plans/packages/<id>-<slug>.md` from the
   release plan and the current code: goal, files to touch, step-by-step
   approach, constraints and conventions, edge cases, tests and checks, done
   when. Then set the status to `plan ready`.
2. **Build (coder → review → verify).** Only after the plan exists. The coder
   gets the plan file as its task.

Update the status column in the PR that changes it. Values: `not started`,
`planning`, `plan ready`, `in progress`, `in review`, `done (<PR link>)`,
`blocked: <reason>`. The Plan column links the package plan once written.

Progress is visible while work is under way (decided Nov 8, 2026):

- A package built in several parts gets one sub-row per part (for example
  18.1a, 18.1b, 18.2) under its own row, each with its own coder and
  status; the package row is `done` only when every part is.
- As soon as the first package of a PR is committed, the branch is pushed
  and a **draft** PR is opened, with the status column already updated.
  Each later package or part is pushed when it is committed, so the
  roadmap on the PR branch always shows the current state. The PR leaves
  draft when every package in it is done and verified.

### 2.0: self-hosted server, single user

| # | Package | Depends on | Coder | Size | Plan | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | Test CI (Linux, Python 3.9 and 3.12, UI suite) | – | Codex | S | [plan](packages/0-test-ci.md) | done ([#24](https://github.com/agenticML/agenticml-lite/pull/24)) |
| 1 † | Server ingest, keys, `/v2` endpoints, server lock | – | Codex | L | [plan](packages/1-server-ingest.md) | done ([#26](https://github.com/agenticML/agenticml-lite/pull/26)) |
| 2 | Run status from `remote.json` | – | Cursor | S | [plan](packages/2-remote-status.md) | done ([#25](https://github.com/agenticML/agenticml-lite/pull/25)) |
| 3 | Sync client v2 and `docs/PROTOCOL.md` | 1 | Cursor (client), Codex (sync); prose Antigravity | L | [plan](packages/3-sync-client-v2.md) | done ([#27](https://github.com/agenticML/agenticml-lite/pull/27)) |
| 4 † | Server dashboard: key login, sessions, CSRF, last synced | 1, 2 | Cursor | M | [plan](packages/4-server-dashboard.md) | done ([#28](https://github.com/agenticML/agenticml-lite/pull/28)) |
| 4b † | Key management in the dashboard: first-key setup, issue / list / revoke sync and admin keys | 1, 4 | Codex | M | [plan](packages/4b-key-management.md) | done ([#29](https://github.com/agenticML/agenticml-lite/pull/29)) |
| 5 | `sync --follow` and systemd example | 3 | Cursor | M | [plan](packages/5-sync-follow.md) | done ([#31](https://github.com/agenticML/agenticml-lite/pull/31)) |
| 6 | Packaging, docs, security policy, 2.0.0 release | 1, 3, 4, 4b, 5 | Codex, Cursor; prose Antigravity | M | [plan](packages/6-packaging-release.md) | done ([#34](https://github.com/agenticML/agenticml-lite/pull/34)) |

† security-sensitive: needs a second read-only review from the other coder.

### 2.1: full fidelity

| # | Package | Depends on | Coder | Size | Plan | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 7 † | Lineage replication | 2.0 | Codex | M | [plan](packages/7-lineage-replication.md) | done (2.1 PR) |
| 8 | By-reference artifacts | 2.0 | Cursor | S–M | [plan](packages/8-byref-artifacts.md) | done (2.1 PR) |
| 9 | EFS benchmark and tuning | 2.0 | Cursor | S | [plan](packages/9-efs-benchmark.md) | done (2.1 PR; real-EFS run in Backlog) |
| 10 | Server operator commands | 2.0 | Codex | S | [plan](packages/10-server-operator-commands.md) | done (2.1 PR) |
| 10b † | Server setup command (`server up`) | 2.0 | Cursor | M | [plan](packages/10b-server-up.md) | done (2.1 PR; real-tailnet check in Backlog) |

- **7 † Lineage replication.** The client pushes a `git bundle` of each run's
  shadow commit (`lineage/<project-key>.git`, `refs/runs/<id>`), incremental
  against the refs the server already has. The server fetches it into its own
  shadow repo with hooks disabled and a fixed, minimal Git config, and then
  advertises the `lineage` feature. Done when a server-side code diff between
  two synced runs matches the local diff, and a malformed or hostile bundle is
  rejected without touching existing refs.
- **8 By-reference artifacts.** Opt-in with `sync --artifacts`. For
  `copy=False` artifacts whose source file is unchanged, the client uploads the
  bytes as an object and records the mapping in an allow-listed atomic file. The
  server advertises `byref`. Done when a by-reference artifact can be
  downloaded from the server dashboard, and a changed or missing source is
  skipped with one warning.
- **9 EFS benchmark.** Run `scripts/bench.py` and `scripts/bench_store.py`
  against a server whose data directory is on EFS (or another network
  filesystem) with several machines syncing. Tune append batching
  and `--follow` intervals if needed. Done when the results and recommended
  settings are in `docs/SERVER.md`.
- **10 Operator commands.** `agenticml server status` (runs, sizes, keys,
  last sync per key) and `agenticml server verify` (re-hash objects and check
  `remote.json` sizes against the files). Done when both are tested against a
  store with deliberately corrupted files.

- **10b † Server setup command.** One command, `agenticml server up`, replaces
  the manual laptop setup (find the tailnet URL, create the first admin key,
  run `server serve --public-url`, run `tailscale funnel --bg`). It checks
  that Tailscale is installed, logged in, and allowed to use HTTPS and Funnel,
  and prints the exact fix when one is missing (those approvals stay manual).
  It reads the public URL from `tailscale status --json`, creates the first
  admin key if none exists so the `/setup` link is never exposed publicly,
  serves on loopback, and turns Funnel on while the server runs and off when it
  stops. `--install` writes a launchd job (macOS) or systemd unit (Linux) so
  it starts at boot. Security-sensitive because it exposes the server to the
  internet and creates admin keys. Added Nov 6, 2026 and deferred: it comes
  after the other 2.1 packages unless reprioritized. Done when a fresh machine
  with Tailscale logged in reaches a working HTTPS server with one command,
  each missing prerequisite gives a clear error, and Funnel is off after the
  server stops.

### 2.2: teams

Design constraints carried from 2.0: the server stays standard-library only
and keeps a single v1 store; team state lives beside it in `<data>/server/`.

| # | Package | Depends on | Coder | Size | Plan | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 11 † | Users, roles, migration, run ownership by user | 2.1 | Cursor | L | [plan](packages/11-users-and-roles.md) | done (2.2 PR) |
| 12 † | Per-user key management and sync attribution | 11 | Codex | M | [plan](packages/12-per-user-keys.md) | done (2.2 PR) |
| 13 † | Dashboard sign-in: passwords, invites, trusted identity proxy | 11 | Cursor | L | [plan](packages/13-sign-in.md) | done (2.2 PR) |
| 14 | Audit log | 11–13 | Codex | S–M | [plan](packages/14-audit-log.md) | done (2.2 PR) |
| 15 | Team admin page, account menu, end-to-end teams test | 11–14 | Cursor | M–L | [plan](packages/15-team-admin-page.md) | done (2.2 PR) |

- **11 Users and roles.** `users.json` with `admin`, `member` and `viewer`
  roles. `agenticml server users add|list|remove|set-role`. Admin manages users
  and keys; member syncs and edits notes; viewer only reads. The single-user
  `keys.json` from 2.0 migrates to one admin user on first start, keeping
  every key and its scope.
- **12 Per-user keys.** Keys belong to a user. The 2.0 Keys page becomes
  per-user: members issue sync keys for their own machines, admins see and
  revoke everyone's; run `owner` in `remote.json`
  becomes the user, so one person's machines can all write their runs and
  nobody else's. `whoami` returns the user and role.
- **13 Trusted identity proxy.** Single sign-on without adding a crypto
  dependency: the server trusts an identity header from a configured proxy
  (ALB OIDC `x-amzn-oidc-identity`, Tailscale `Tailscale-User-Login`, or
  oauth2-proxy `X-Forwarded-Email`), only on connections from configured proxy
  addresses. Key login stays as a fallback. The failed-login throttle from
  2.0 then keys by the forwarded client address, taken only from those proxy
  connections, instead of the proxy's own address.
- **14 Audit log.** Append-only `<data>/server/audit.jsonl`: key and user
  changes, logins, run creation, note edits.
- **15 Admin page.** Manage users, roles and keys from the dashboard (admin
  only), with the same CSRF and session rules as notes.

Decided Nov 6, 2026 (recommended options, taken for the owner; details in
the [2.2 plan](on-prem-teams-plan.md#decisions-made-for-the-owner-nov-6-2026)):
viewers see every project (no project-level access in 2.2), and people sign
in with a password (scrypt, set from a one-time invite link) or through a
trusted identity proxy; admin-key login stays as a fallback and keys stay
for machines.

### 3.0: database storage, workspaces and hosted service

Approved by the owner on Nov 7, 2026: the [3.0 plan](on-prem-3.0-plan.md)
and decisions D1–D9. Each package plan is written before its package
starts. Order:
16 → 17 → 18, then 18b and 21 in parallel, then 18c → 19 → 20; 22 and 23
after 18; 24 after 18c–21; 25 after 16–24; 26 after 24 is live.

| # | Package | Depends on | Coder | Size | Plan | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 16 † | Database layer, SQLite backend, migrations; users, keys, sessions, throttle, audit in tables | 2.2 | Codex | L | [plan](packages/16-database-layer.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 17 † | Runs, sync state and notes in the database; append crash safety; run list from the database | 16 | Cursor | L | [plan](packages/17-runs-in-database.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18 † | Workspaces and accounts: memberships, workspace-bound keys, `/w/<slug>` routes, switcher, server-admin commands, isolation suite | 17 | Codex, Cursor | L | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18.1a | Server core: accounts, workspaces, memberships, migration, `default` conversion, workspace-bound sync, CLI | 17 | Codex, Cursor | L | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18.1b | Route table, `/w/<slug>` and `/api/w/<slug>` routes, account and server-admin APIs, isolation suite | 18.1a | Cursor | L | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18.2 | Dashboard: workspace URLs, switcher, Team and Keys per workspace, admin page, picker | 18.1b | Cursor | M | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18b † | Firebase sign-in for the hosted service | 18 | Cursor | M | – | not started |
| 18c † | Self-serve sign-up and usage limits (free plan: storage, uploads, requests, people) | 18b | Codex | M | – | not started |
| 19 † | Postgres backend, row-level security, CI on both backends | 18 | Cursor | M | – | not started |
| 20 † | Many server processes: database locks, shared throttle and sessions | 19 | Cursor | M | – | not started |
| 21 | S3 object backend | 18 | Codex | M | – | not started |
| 22 | Metric summaries; sort and filter runs by metric | 17, 18 | Cursor | M | – | not started |
| 23 | Operator commands: end-to-end 2.x conversion test (the import itself landed in 16–18), `server backup`, `status`/`verify` on the database | 18 | Codex | S–M | – | not started |
| 24 | AWS hosted deployment (Dockerfile, Terraform, deploy workflow, runbook, real-EFS benchmark) | 18c, 19, 20, 21 | Codex; owner runs it | M | – | not started |
| 25 | Docs, threat model, version 3.0.0 | 16–24 | Codex; prose Antigravity | M | – | not started |
| 26 | Retire agenticml-server | 24 live | Owner | S | – | not started |

- **16–17** move everything the server keeps outside run files into tables
  behind one repository layer, with SQLite (standard library) as the
  self-hosted backend. The database records how many bytes of each run file
  are committed; an append truncates any crash tail, writes, fsyncs and
  commits in one transaction.
- **18** adds workspaces with agenticml-server's model and membership
  checks: one global account per person, roles `owner`, `admin`, `member`,
  `viewer` per workspace, keys bound to one workspace,
  dashboard URLs `/w/<slug>/...` (the workspace always comes from the URL
  or the key, never from a "current workspace"), and a switcher.
  Self-hosted starts with one `default` workspace. Per-workspace files and
  object dedupe; a test for every route that a non-member cannot see or
  detect another workspace.
- **18b** signs people in to the hosted service with Firebase, as
  agenticml-server does. Firebase proves who someone is; 18's membership
  check still decides which workspaces they can open. Self-hosted keeps
  2.2 sign-in.
- **18c** opens the hosted service to self-serve sign-up (owner's choice,
  Nov 7, 2026): a verified email gets one free workspace with limits on
  storage, uploads per day, requests per minute and people. Server admins
  see usage and raise a workspace's limits by hand until billing exists.
  Self-hosted servers keep sign-up off and have no limits by default.
- **19–21** make the hosted mode: Postgres with row-level security as a
  second guard, more than one server process, and objects in S3.
- **22** stores last/min/max/count per run and metric at ingest, so the
  runs list sorts and filters by metric without reading files.
- **24** stands the service up in our AWS account (about $120/month for all
  workspaces together; see the plan). **26** shuts down agenticml-server,
  which has no customers, after 24 is live.

### Maintenance fixes

- Empty object and lineage upload framing: [plan](packages/fix-empty-uploads.md),
  found in the 2.2 compatibility check; status: done (2.2 PR).
- Version 2.2.0, transient Git failures answer 503, runs no longer track
  their own store files, and the dashboard last-admin-key rule counts only
  usable keys: [plan](packages/backlog-2.2-fixes.md); status: done (2.2 PR).

### Later (unscheduled)

- (More than one server process and the S3 object backend moved to 3.0.)
- Billing and paid plans; email delivery for invites (after 3.0).
- Enterprise SSO per workspace (after 3.0).
- Abuse controls if needed: Firebase App Check, disposable-email
  blocklist, removal of long-inactive free workspaces.
- Run files on S3 instead of EFS, if the 3.0 EFS numbers call for it.
- agenticml-server features not ported in 3.0 (decision D3): experiments and
  hypotheses, architectures / model design, AI threads, datasets, cost and
  budget, cloud connections.
- Server-side `gc`, retention and per-user quotas.
- Two-way notes sync between machines and the server.
- Project-level access (viewers or members limited to some projects).
  Deferred from 2.2; 3.0 workspaces cover the common case (separate teams
  get separate workspaces).

## Backlog

Unfinished work found along the way that is not part of a package above. Each
entry says where, the problem, the fix, and when it is done. Place entries by
priority.

- **Sync client trusts its own cursor after a failed atomic upload** (package 17
  second review, Nov 8, 2026).
  - Where: `src/agenticml/sync.py` (atomic-file skip, around line 923).
  - Problem: if a replacement of an atomic file (for example
    `scalars/index.json`) reaches the server's disk but the request fails
    before it is acknowledged, the client keeps its old cursor. If the
    client's file later returns to the previously acknowledged bytes, the
    client skips the upload without asking `/state`, so the server keeps
    the unacknowledged bytes (for example a wrong metric label). Another
    machine, or a client that asks `/state`, recovers, because `/state`
    hashes atomic files from disk. 2.2 had the same gap.
  - Fix: on any failed atomic upload, drop that file's cursor entry so the
    next pass compares against `/state`.
  - Done when: a test crashes the server after the rename of an atomic
    replacement, restores the old bytes on the client, re-syncs with the
    same `sync.json`, and the server ends with the old bytes.
- **A removed member's login can be re-added by another workspace**
  (package 18 review, Nov 8, 2026).
  - Where: `src/agenticml/server/db/repos.py` `UserRepo.remove` and `add`.
  - Problem: removing someone from their only workspace keeps their global
    account (and password) with no memberships. An admin of another
    workspace can then `add` that login; the account is single-workspace
    again, so that admin may reset its credentials. Low risk (login-name
    squatting on a self-hosted server), but surprising.
  - Fix: when an account loses its last membership, either delete it (if it
    owns no runs) or mark it so that only a server admin can attach it to a
    workspace again.
  - Done when: a test removes a member from their only workspace, another
    workspace's admin tries to add and reset that login, and is refused.
- **Compatibility owner rule can mislead a 2.0/2.1 client** (package 11
  review, Nov 6, 2026). Blocked until no 2.0/2.1 sync clients remain.
  - Where: `src/agenticml/server/ingest.py` `compat_owner`.
  - Problem: old clients compare `owner` with their key name; another user
    whose user name equals the client's key name looks like the client's own
    run. The server's 409 stays authoritative, so there is no access, only a
    confusing client message.
  - Fix: the client half is done (package 12: 2.2 clients compare
    `owner_user` with `whoami.user`). In a later release, once 2.0/2.1
    clients are unsupported, return the real owner user in `owner` and drop
    the rule.
  - Done when: `compat_owner` is gone and a test covers the collision.
- **Check `agenticml server up` on a real tailnet** (package 10b caveat,
  Nov 6, 2026).
  - Where: `src/agenticml/server/up.py`; `docs/SERVER.md` quick start.
  - Problem: the tests use a fake `tailscale` executable. The real CLI's
    `status --json` and `funnel status --json` shapes, and the full path from
    a fresh machine to a working public HTTPS server, were not exercised on a
    real tailnet (doing so exposes the machine to the internet, so it was
    left to the owner).
  - Fix: on a machine with Tailscale logged in, HTTPS enabled and the
    `funnel` node attribute, run `agenticml server up`, sign in at the
    printed URL, sync one run from another machine, stop with Ctrl+C and
    confirm `tailscale funnel status` is empty; also try each missing
    prerequisite once.
  - Done when: those steps pass, or any mismatch with the real CLI is fixed
    with a test that reproduces it.
- **Run the server benchmark on real EFS** (package 9 caveat, Nov 6, 2026).
  - Where: `scripts/bench_server.py`; `docs/SERVER.md`, section "Network
    filesystems (EFS)".
  - Problem: the published numbers are local NVMe plus an fsync delay
    injected only into the benchmark's server process. No EFS or NFS mount
    was available, so real EFS commit latency, metadata operations and flock
    behaviour are unmeasured.
  - Fix: mount EFS (`nfsvers=4.1,hard,noresvport`) as the server data
    directory, run `python scripts/bench_server.py --data /mnt/efs/agenticml
    --clients 1` and `--clients 8` without `--fsync-delay-ms`, and replace
    the modelled tables; revisit `--follow --interval` and `APPEND_CHUNK` if
    the numbers disagree.
  - Done when: `docs/SERVER.md` shows real-EFS numbers for 1 and 8 clients,
    labelled as such with mount details, and the recommendations are
    confirmed or updated.
  - Scheduled: done as part of 3.0 package 24, on the hosted stack's EFS.

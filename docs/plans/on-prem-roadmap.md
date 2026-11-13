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
16 → 17 → 18, then 18b and 21 in parallel, then 18c → 19 → 20 (19.1,
the Postgres backend, runs beside 18b; 19.2 needs 18c's tables); 22 and 23
after 18; 24 after 18c–21; 25 after 16–24; 26 after 24 is live.

| # | Package | Depends on | Coder | Size | Plan | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 16 † | Database layer, SQLite backend, migrations; users, keys, sessions, throttle, audit in tables | 2.2 | Codex | L | [plan](packages/16-database-layer.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 17 † | Runs, sync state and notes in the database; append crash safety; run list from the database | 16 | Cursor | L | [plan](packages/17-runs-in-database.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18 † | Workspaces and accounts: memberships, workspace-bound keys, `/w/<slug>` routes, switcher, server-admin commands, isolation suite | 17 | Codex, Cursor | L | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18.1a | Server core: accounts, workspaces, memberships, migration, `default` conversion, workspace-bound sync, CLI | 17 | Codex, Cursor | L | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18.1b | Route table, `/w/<slug>` and `/api/w/<slug>` routes, account and server-admin APIs, isolation suite | 18.1a | Cursor | L | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18.2 | Dashboard: workspace URLs, switcher, Team and Keys per workspace, admin page, picker | 18.1b | Cursor | M | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18b † | Firebase sign-in for the hosted service | 18 | Cursor | M | [plan](packages/18b-firebase-sign-in.md) | done (3.0 PR; real sign-in in Backlog) |
| 18b.1 | Token verification, account linking, `/auth/firebase`, invites, CLI, extras | 18 | Cursor | M | [plan](packages/18b-firebase-sign-in.md) | done (3.0 PR) |
| 18b.2 | Sign-in page with the vendored Firebase SDK, per-page CSP, UI tests | 18b.1 | Cursor | S | [plan](packages/18b-firebase-sign-in.md) | done (3.0 PR) |
| 18c † | Self-serve sign-up and usage limits (free plan: storage, uploads, requests, people) | 18b | Codex, Cursor | M | [plan](packages/18c-signup-and-limits.md) | done (3.0 PR) |
| 18c.1 | Limits core: usage counters, limits file, storage/upload/request/people checks, CLI | 18b | Codex, Cursor | M | [plan](packages/18c-signup-and-limits.md) | done (3.0 PR) |
| 18c.2 | Sign-up with a personal workspace, new workspace, free-workspace limit | 18c.1 | Cursor | S | [plan](packages/18c-signup-and-limits.md) | done (3.0 PR) |
| 18c.3 | Usage page and banner, admin plan edit, `sync --follow` retry | 18c.2 | Cursor | S | [plan](packages/18c-signup-and-limits.md) | done (3.0 PR) |
| 19 † | Postgres backend, row-level security, CI on both backends | 18 | Codex, Cursor | M | [plan](packages/19-postgres.md) | done (3.0 PR) |
| 19.1 | Postgres backend, dialect, portability audit, test harness, CI job | 18 | Codex, Cursor | M | [plan](packages/19-postgres.md) | done (3.0 PR) |
| 19.2 | Row-level security policies and per-transaction workspace binding | 19.1, 18c | Cursor | S–M | [plan](packages/19-postgres.md) | done (3.0 PR) |
| 20 † | Many server processes: database locks, shared throttle and sessions | 19 | Codex, Cursor | M | [plan](packages/20-many-processes.md) | done (3.0 PR) |
| 20.1 | Cross-process session locks, process start, database clock, reader freshness, write fencing (compare-and-swap, lock checks) | 19 | Codex, Cursor | M | [plan](packages/20-many-processes.md) | done (3.0 PR) |
| 20.2 | Two-process end-to-end test with a kill mid-upload | 20.1 | Codex, Cursor | S | [plan](packages/20-many-processes.md) | done (3.0 PR) |
| 21 | S3 object backend | 18 | Codex, Sonnet | M | [plan](packages/21-s3-objects.md) | done (3.0 PR; real S3 in Backlog) |
| 22 | Metric summaries; sort and filter runs by metric | 17, 18 | Cursor | M | [plan](packages/22-metric-summaries.md) | done (3.0 PR) |
| 23 | Operator commands: end-to-end 2.x conversion test (the import itself landed in 16–18), `server backup`, `status`/`verify` on the database | 18, 21, 22 | Sonnet (Codex at limit) | S–M | [plan](packages/23-operator-commands.md) | done (3.0 PR) |
| 24 | AWS hosted deployment (Dockerfile, Terraform, deploy workflow, runbook, real-EFS benchmark) | 18c, 19, 20, 21 | Codex, Sonnet; owner runs it | M | [plan](packages/24-aws-deployment.md) | done (code, 3.0 PR; owner checks in Backlog) |
| 24.1 | Image: Dockerfile, entrypoint, limits file, local hosted smoke test | 21 | Codex, Sonnet | S | [plan](packages/24-aws-deployment.md) | done (3.0 PR) |
| 24.2 | Terraform, deploy workflow, `deploy-check` CI job, runbook | 24.1 | Codex, Sonnet | M | [plan](packages/24-aws-deployment.md) | done (3.0 PR; `terraform validate` runs in CI) |
| 24.3 | Apply, first deploy, restore drill, real-EFS benchmark, exit criterion 6 | 24.5 | Owner | S | [AWS roadmap](aws-deployment-roadmap.md) | in progress (AWS roadmap Phases 0–3 done Nov 12, 2026; Phase 4 steps 4.1, 4.2 and 4.2a done Nov 13: first admin through `/setup`, Firebase sign-in on with project `agenticml-hosted`; package 24.7 deployed Nov 13 and open sign-up turned on; next: 4.2b, the owner's first Google sign-in as server admin, then 4.3 on the web) |
| 24.4 † | CloudFormation replaces Terraform; Namecheap DNS; cost alerts | 24.2 | Sonnet; prose Antigravity | M | [plan](packages/24.4-cloudformation.md) | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49); second review by the supervisor) |
| 24.4a | `bootstrap.yaml`: ECR, OIDC, roles, certificate, alerts topic, budget, cost-step function | – | Sonnet | S–M | [plan](packages/24.4-cloudformation.md) | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49)) |
| 24.4b | `hosted.yaml` and its tests | 24.4a | Sonnet | M | [plan](packages/24.4-cloudformation.md) | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49)) |
| 24.4c | `deploy.yml`, CI `cfn-lint`, remove Terraform and `swap_image.py` | 24.4b | Sonnet | S | [plan](packages/24.4-cloudformation.md) | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49)) |
| 24.4d | Runbook for CloudFormation (`deploy/aws/README.md`, `docs/HOSTED.md`) | 24.4c | Antigravity | S | [plan](packages/24.4-cloudformation.md) | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49)) |
| 24.5 † | Lean hosted setup (~$60/month: one ARM64 task, `db.t4g.micro`, no Container Insights); deploys on pushes to `dev` | 24.4 | Supervisor; prose Antigravity | S | [plan](packages/24.5-lean-hosted.md) | done ([#52](https://github.com/agenticML/agenticml-lite/pull/52)) |
| 24.6 | Phase 4 runbook fixes: first admin through `/setup`, workspace/invite order, ECS Exec checks, training-machine sync section; `whoami` prints the workspace | 24.5 | Sonnet; prose Antigravity | S | [plan](packages/24.6-phase-4-runbook.md) | done ([#55](https://github.com/agenticML/agenticml-lite/pull/55)) |
| 24.7 † | Self-serve server admins: admin emails from the deploy config, server-admin management and workspace creation for new emails on `/admin`, Phase 4 through the web | 24.6 | Sonnet; prose Antigravity | M | [plan](packages/24.7-self-serve-admins.md) | done ([#58](https://github.com/agenticML/agenticml-lite/pull/58); second review by the supervisor; deployed Nov 13, 2026) |
| 24.7a † | `--admin-emails` / `AGENTICML_SERVER_ADMIN_EMAILS`: a verified Firebase sign-in whose email is on the list gets an account if it has none and becomes server admin, with an audit entry; the list is ignored, with a startup warning, when Firebase is off | 24.6 | Sonnet | S–M | [plan](packages/24.7-self-serve-admins.md) | done ([#58](https://github.com/agenticML/agenticml-lite/pull/58)) |
| 24.7b † | `/admin` page: "Server admins" section (list server admins, add by email or login, remove; never remove the last admin who can sign in; audited); creating a workspace for an owner email with no account yet (linked on its first verified sign-in); API and UI tests, 375px | 24.7a | Sonnet | M | [plan](packages/24.7-self-serve-admins.md) | done ([#58](https://github.com/agenticML/agenticml-lite/pull/58)) |
| 24.7c | Deploy wiring: GitHub variable `HOSTED_ADMIN_EMAILS` → `deploy.yml` → `hosted.yaml` parameter → task environment; `cfn-lint` and template tests | 24.7a | Sonnet | S | [plan](packages/24.7-self-serve-admins.md) | done ([#58](https://github.com/agenticML/agenticml-lite/pull/58)) |
| 24.7d | Runbook and docs: steps 4.2b and 4.3 through the web; re-check the Team page and `users add` on the Firebase path (24.6 checked only passwords); `deploy/aws/README.md`, `docs/HOSTED.md`, `docs/SERVER.md`, threat model | 24.7c | Sonnet (checks); prose Antigravity | S | [plan](packages/24.7-self-serve-admins.md) | done ([#58](https://github.com/agenticML/agenticml-lite/pull/58)) |
| 24.8 | Personal workspace for every verified sign-in without one when open sign-up is on (list admins included) | 24.7 | Sonnet | S | [plan](packages/24.8-personal-workspace-for-admins.md) | done (PR to `dev`; owner's request, Nov 13, 2026) |
| 25 | Docs, threat model, version 3.0.0 | 16–24 | Sonnet (Codex at limit); prose Antigravity | M | [plan](packages/25-docs-and-release.md) | done (3.0 PR; tag and PyPI release by the owner) |
| 25.1 | Client support window (426), version 3.0.0, base-install smoke, 2.2 client check | 16–24 | Sonnet (Codex at limit) | S | [plan](packages/25-docs-and-release.md) | done (3.0 PR) |
| 25.2 | SERVER.md, HOSTED.md, threat model, PROTOCOL.md, README, website, SECURITY.md | 25.1 | Antigravity | M | [plan](packages/25-docs-and-release.md) | done (3.0 PR) |
| 26 | Retire agenticml-server | 24 live | Owner | S | – | not started |
| 27 | Sign-in pages redesigned like agenticml-server (brand panel, logo, Sign in / Sign up tabs) | 18b | Sonnet | S | [plan](packages/27-sign-in-redesign.md) | done ([#57](https://github.com/agenticML/agenticml-lite/pull/57)) |
| 28 | One Settings page: Account, Workspace, Members, API keys and Activity replace the Keys, Team and Account entries (like agenticml-server) | 27 | Sonnet | M | [plan](packages/28-settings-page.md) | in review ([#63](https://github.com/agenticML/agenticml-lite/pull/63)) |

Getting the hosted service live on AWS, step by step: [AWS deployment roadmap](aws-deployment-roadmap.md).

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
  workspaces together; see the plan). **24.7** (owner, Nov 13, 2026) makes
  running the hosted service self-serve: with Firebase on, `/setup` is
  closed and server admin could only be granted with the server CLI over
  ECS Exec. Emails listed in the deploy config become server admins on
  their first verified sign-in, and server admins grant and revoke server
  admin and create customer workspaces on `/admin`. ECS Exec stays as the
  break-glass path. **26** shuts down agenticml-server,
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

- **Hosted launch and rolling-upload verification** (package 24.3, owner).
  - Where: `deploy/aws/README.md`, sections Prerequisites through Upgrades.
  - Problem: the infrastructure and deployment code have not run in our AWS
    account; exit criterion 6 needs a real deployment.
  - Fix: owner follows the AWS deployment roadmap Phases 2–5 and the
    CloudFormation runbook: bootstrap stack, GitHub `hosted` environment,
    first workflow deploy (one task by default since 24.5; for the two-task
    checks, dispatch with `desired_count=2` once `deploy.yml` is on `main`, or
    run `aws ecs update-service --desired-count 2` until the next deploy),
    the Namecheap `app` CNAME, then
    Firebase (later) and two customer
    workspaces with admin/member accounts and workspace-bound sync keys.
    Sync through both tasks and roll while uploading; verify acknowledged
    sizes/hashes and dashboard parity, membership isolation, and client retry.
  - Done when: `app.agenticml.xyz` serves both workspaces and evidence shows
    a rolling deployment loses no uploads; record apply/deploy revisions and
    sign-in, isolation and upload results. Owner only; no agent AWS calls.
- **Hosted backup restore drill** (package 24.3, owner).
  - Where: `deploy/aws/README.md`, Restore drill.
  - Problem: configured backups have not been restored; exit criterion 6
    requires one real restore.
  - Fix: restore RDS PITR to a new private instance, EFS from AWS Backup to
    a new filesystem/access point, and matching S3 versions to a test bucket;
    point an isolated test service and scoped roles at them with signup off.
    Verify both workspaces, login, run files/metrics, objects, and a new sync.
  - Done when: recovery age/duration and integrity results are recorded and
    drill resources removed without changing live resources.
- **Release 3.0.0: tag, publish, legal pages (owner)** (Nov 9, 2026).
  - Where: GitHub releases, PyPI, agenticml.xyz.
  - Problem: the 3.0 PR sets version 3.0.0 but nothing is tagged or
    published, and open sign-up needs terms of service, an acceptable use
    policy and a privacy policy first (3.0 plan release checklist). The
    `v2.2.0` tag was never created either; the 2.2 client compatibility
    test falls back to commit `bd0d499` until it exists.
  - Fix: after merging, tag `v2.2.0` at `bd0d499` and `v3.0.0` at the
    merge, publish 3.0.0 to PyPI (`publish.yml`), and publish the three
    policies before setting `HOSTED_OPEN_SIGNUP=true` in the `hosted`
    environment.
  - Done when: `pip install agenticml-lite==3.0.0` works and the policies
    are linked from the sign-in page.
  - Progress (Nov 13, 2026): the owner turned open sign-up on before the
    policies were published (`HOSTED_OPEN_SIGNUP=true`, deploy run
    43417765936). The policies and the sign-in page links are now the most
    urgent part of this entry.
- **Setup and invite pages in the new sign-in style** (package 27, Nov 13,
  2026).
  - Where: `src/agenticml/server/static/setup.html`, `invite.html`.
  - Problem: package 27 restyled `/login` (hosted and self-hosted) like
    agenticml-server; `/setup` and the self-hosted invite page still use
    the old centred card.
  - Fix: move both pages onto the `.auth-*` shell (brand panel, logo,
    underline fields), keeping their ids and behaviour.
  - Done when: both pages use the shell, their UI tests pass, and there is
    no overflow at 375px.
- **Theme on the sign-in pages** (package 27, Nov 13, 2026).
  - Where: `src/agenticml/server/static/*.html`, `app.css`.
  - Problem: the sign-in pages are always light; agenticml-server has a
    light/dark toggle and the dashboard follows the saved theme.
  - Fix: apply the dashboard's saved theme (or the system preference) on
    the sign-in pages with a same-origin script, and add the compact
    toggle.
  - Done when: a user with the dark theme sees a dark sign-in page, with a
    UI test.
- **Permissions boundary for the hosted stack's roles** (package 24.4a
  review, Nov 11, 2026).
  - Where: `deploy/aws/cloudformation/bootstrap.yaml` (`CloudFormationRole`,
    Sid `HostedRoles`).
  - Problem: the CloudFormation role can create `agenticml-hosted-*` roles
    and give them any inline policy, then pass them to ECS tasks. Only a
    reviewed template run through the protected `hosted` workflow can use
    it, but nothing caps what those roles may do.
  - Fix: a managed policy `agenticml-hosted-boundary` in the bootstrap stack
    that denies IAM, Organizations and account actions and allows only the
    services the hosted roles use; require it with an
    `iam:PermissionsBoundary` condition on `CreateRole`/`PutRolePolicy`/
    `AttachRolePolicy`, and deny removing it.
  - Also (24.4 second review): the role's ec2, elasticloadbalancing, ecs,
    rds, elasticfilesystem and backup write actions are scoped by region
    only, and the account runs other services in us-east-2. A malicious
    template could import their resources (an `IMPORT` change set) and
    delete them. Limit those writes to resources tagged
    `Project=agenticml-hosted` (`aws:ResourceTag` / `aws:RequestTag`), and
    deny `cloudformation:CreateChangeSet` with
    `cloudformation:ImportResourceTypes` in the deploy role.
  - Done when: a template test asserts the boundary condition, the tag
    conditions and the import deny, and a hosted role without the boundary
    cannot be created (checked on the next real deploy).
- **Metric picker polish** (package 22 review, Nov 9, 2026).
  - Where: `src/agenticml/ui/static/app.js` runs page;
    `testbed/tests/test_server_dashboard.py`.
  - Problem: `/metrics` is loaded once per page load (a metric first
    logged later, or a workspace that had none at load, needs a reload);
    filter-token names are matched case-insensitively only against that
    list; the first `/runs` waits for `/metrics`; the 422 fallback and the
    250 ms debounce have no UI test.
  - Fix: refresh `/metrics` every few polls and fetch it in parallel with
    the first `/runs`; add the two UI tests.
  - Done when: a metric logged after page load appears in the picker
    without a reload, with a test.
- **Stand up the hosted service on AWS (package 24.3, owner)** (Nov 9,
  2026).
  - Where: `deploy/aws/cloudformation/` (bootstrap and hosted stacks),
    `deploy/aws/README.md` runbook, `.github/workflows/deploy.yml`,
    [AWS deployment roadmap](aws-deployment-roadmap.md) Phases 2–6.
  - Problem: agents wrote and statically checked the stack (image build,
    `scripts/hosted_smoke.sh` against local Postgres and moto, `cfn-lint`
    and template tests in CI) but never ran it in AWS: creating billed
    resources and a public endpoint is the owner's call.
  - Progress (Nov 13, 2026): AWS roadmap Phases 0–3 are done: bootstrap
    stack, `hosted` environment, first deploy from `dev`, the Namecheap
    `app` CNAME, and `https://app.agenticml.xyz/healthz` returns 200. In
    Phase 4, ECS Exec works (4.1), the first admin exists (4.2) and Firebase
    sign-in is on with project `agenticml-hosted` (4.2a). Package 24.7 is
    deployed (server admins and customer workspaces without ECS Exec), and
    open sign-up is on. Next is 4.2b (the owner's first Google sign-in as
    server admin), then two workspaces and a sync to each.
  - Fix: follow `deploy/aws/README.md`: bootstrap stack (with the Namecheap
    validation record), GitHub environment `hosted` with the role
    variables, first deploy, the `app` CNAME,
    first server admin, two customer workspaces, a rolling deploy during a
    sync, an RDS + EFS restore drill, and the real-EFS benchmark (replaces
    the "Run the server benchmark on real EFS" entry below).
  - Done when: exit criterion 6 of the 3.0 plan holds and `docs/SERVER.md`
    shows real-EFS numbers.
- **Sync state per workspace, not per host** (package 24.6 check, Nov 12,
  2026).
  - Where: `src/agenticml/sync.py` (`_host_state`, `_save_state`,
    `_sync_idle`, `_selected_ids`); `runs/<id>/sync.json`.
  - Problem: the client keeps each run's sync cursor per host. Every hosted
    workspace shares `app.agenticml.xyz`, so a store that synced a run to one
    workspace reports `up to date` (exit 0) when the same run is synced with
    another workspace's key, and nothing is uploaded. `sync --all` with the
    second key skips those runs the same way. Phase 4 works around it with
    one `AGENTICML_DIR` per workspace, or `sync RUN_ID --reset`.
  - Fix: key the cursor by host and workspace (`/v2/whoami` returns
    `workspace`), migrating existing host-only entries to the workspace the
    current key resolves to; keep 2.x servers (no `workspace`) on the host
    key.
  - Done when: a test syncs one run to two workspaces on one host with two
    keys and both receive it, and an old `sync.json` still resumes.
- **Many-process follow-ups for package 24** (package 20 reviews, Nov 9,
  2026).
  - Where: `src/agenticml/server/db/postgres.py` (session locks),
    `store/reader.py`, `server/routes.py` usage views.
  - Problem: every session lock opens a fresh Postgres connection (slow on
    TLS/RDS; pins connections under RDS Proxy or pgbouncer); dashboard reads
    query the committed size per file call; the usage page computes "today"
    from the process clock while enforcement uses the database clock; a
    process paused longer than the keepalive window while holding a lock
    can still write after losing it (checks narrow this to the time
    between two checks; see the package 20 plan).
  - Fix: pool lock connections; cache committed sizes per read; use the
    database clock in the usage views; record the paused-process residual
    in the package 25 threat model and measure the rest in package 24.
  - Done when: package 24's benchmark shows acceptable lock latency and the
    threat model names the residual.
- **Admin plan edits: concurrent merge and free→custom defaults** (package
  18c.3 review, Nov 9, 2026).
  - Where: `src/agenticml/server/routes.py` admin PATCH; `ui/static/app.js`
    `editPlan`.
  - Problem: the custom-limits merge reads outside the `set_plan`
    transaction (two admins can overwrite each other's partial edits), and
    switching free → custom opens empty fields (unlimited) instead of the
    free limits.
  - Fix: read and write in one transaction; prefill the free limits.
  - Done when: both have tests.
- **Remaining package 19.1 second-review notes** (Nov 9, 2026; the
  password, test-isolation, connect-timeout and exception-chain items were
  fixed in the 3.0 PR).
  - Where: `src/agenticml/server/db/__init__.py`, `db/postgres.py`.
  - Problem: the registry lock is held across Postgres network I/O; advisory
    lock keys are database-wide (parallel test schemas share them); total
    connections are not capped (only idle ones).
  - Fix: open outside the registry lock; consider salting keys per schema
    and a connection cap. Revisit in package 20.
  - Done when: each has a test or a recorded decision.
- **Check S3 objects against a real bucket** (package 21, Nov 9, 2026).
  - Where: `src/agenticml/server/objectstore.py`, hosted server configuration.
  - Problem: moto covers the backend; real AWS checksums, conditional writes,
    IAM and server-side encryption still need an owner check.
  - Fix: sync a run and download its objects through the dashboard against
    a real S3 bucket; confirm SSE and workspace-scoped keys.
  - Done when: one sync and dashboard download match the source bytes and
    the stored objects have the configured SSE.
- **Retry object cleanup after a workspace deletion fails** (package 21,
  Nov 9, 2026).
  - Where: `server/workspaces.py` `delete_workspace`, S3 workspace prefix.
  - Problem: an S3 outage after database deletion leaves unreachable bytes;
    deletion logs a warning with the workspace id.
  - Fix: retain the warned id and retry prefix deletion when S3 recovers;
    add durable cleanup tracking if automatic retries are needed.
  - Done when: a failed cleanup can be retried and removes only that
    workspace's prefix.
- **Check Firebase sign-in against the real Firebase project** (package 18b
  caveat, Nov 8, 2026).
  - Where: `src/agenticml/server/firebase.py`, `static/firebase-login.js`,
    the vendored SDK in `static/vendor/firebase/`.
  - Problem: tests sign tokens locally and replace the Firebase SDK with a
    fake in the browser, so Google's real certificates, the real SDK
    (popup, redirect through `apis.google.com`, email verification) and the
    sign-in page's CSP were never exercised end to end.
  - Fix: on a test server with `--firebase-project` (agenticml-server's
    project), the web API key and the server's host added to the project's
    authorized domains, sign in once with Google and once with email and
    password, accept one invite, and watch the browser console for CSP
    violations.
  - Progress (Nov 13, 2026): the hosted service now runs with Firebase
    project `agenticml-hosted` (not agenticml-server's). `/login` loads the
    real SDK with no console or CSP errors. Real Google sign-in comes with
    AWS roadmap step 4.2b, an invite with 4.3.
  - Done when: those steps pass, or each mismatch is fixed with a test.
- **Firebase sign-in page test gaps** (package 18b.2 second review, Nov 8,
  2026).
  - Where: `testbed/tests/test_server_firebase_dashboard.py`.
  - Problem: the UI tests do not assert sign-out on terminal failures,
    in-memory persistence, button disabling while a request is in flight,
    the mapped Firebase error texts, that nothing is written to
    `localStorage`/`sessionStorage`, or the account dialog with and without
    Firebase.
  - Fix: add those assertions with the fake `firebase` global (delayed
    promises, injected failures, storage inspection).
  - Done when: each behaviour has a failing-then-passing UI test.
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
- **ECS Exec hardening** (package 24.6 check, Nov 12, 2026). Low priority.
  - Where: `deploy/aws/cloudformation/hosted.yaml` (container `server`,
    `Cluster`).
  - Problem: PID 1 is the Python server, which does not reap the processes
    ECS Exec sessions leave behind (AWS recommends `InitProcessEnabled`), and
    exec sessions are not logged anywhere; only CloudTrail records the
    `ExecuteCommand` call.
  - Fix: set `LinuxParameters: {InitProcessEnabled: true}` and check that
    SIGTERM still reaches the server within `StopTimeout`. Decide whether to
    log sessions (`ExecuteCommandConfiguration` to a CloudWatch group); if
    yes, note that invite links and keys printed in a session then land in
    that log.
  - Done when: template tests check the init process (and the logging
    choice), and cfn-lint is clean.
- **Hosted alarm and action pinning follow-ups** (Cursor's review of 24.5,
  Nov 12, 2026). Low priority.
  - Where: `deploy/aws/cloudformation/hosted.yaml`, `HealthyHostsAlarm`;
    `.github/workflows/deploy.yml`, `uses:` lines.
  - Problem: the healthy-hosts alarm fires below one healthy target, so with
    two tasks it does not fire when one task drops out of the target group
    (the unhealthy-hosts and 5xx alarms still cover the usual cases). The
    workflow's actions are pinned by tag (`@v4`, `@v3`), not by commit SHA.
  - Fix: set the alarm threshold from `DesiredCount` (or document that
    two-task mode relies on the other alarms); pin every `uses:` to a full
    commit SHA with the tag in a comment, and update the workflow tests.
  - Done when: the template and workflow tests check both, and cfn-lint is
    clean.
- **Workspace rename by its owner** (package 28, Nov 13, 2026). Low priority.
  - Where: `src/agenticml/server/routes.py` (workspace routes),
    `src/agenticml/ui/static/app.js` (Settings, Workspace card).
  - Problem: only server admins can rename a workspace (on `/admin`);
    agenticml-server lets a workspace owner or admin rename it from
    Settings, and the design board's Settings proposal shows a Rename button.
  - Fix: add `PATCH /api/w/<slug>` (name only, role `admin`, audited) and a
    Rename action on the Workspace card.
  - Done when: API and UI tests cover owner/admin allowed, member denied,
    and the audit entry.

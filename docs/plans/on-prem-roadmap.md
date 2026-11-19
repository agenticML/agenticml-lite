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
- Release 3.1: sketched below, with package plans in `packages/`.
- Release 3.3: [packages/36-aws-self-deploy.md](packages/36-aws-self-deploy.md).

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
| **3.0** | Database storage, workspaces and hosted service | Server state in SQLite or Postgres, workspaces with accounts and memberships on one hostname, Firebase sign-in and self-serve sign-up with free limits, many server processes, S3 objects, metric summaries, AWS deployment, agenticml-server retired | Exit criteria 1–6 in the [3.0 plan](on-prem-3.0-plan.md); released Nov 17, 2026 as 3.0.0 (tag `v3.0.0`, [PyPI](https://pypi.org/project/agenticml-lite/3.0.0/), [GitHub release](https://github.com/agenticML/agenticml-lite/releases/tag/v3.0.0)) |
| **3.1** | Scale and visibility | Usage history on `/admin` and `agenticml server stats`, capacity metrics with a CloudWatch dashboard and alarms, ECS autoscaling for the hosted service | A server admin sees accounts, active users, workspaces and stored data over time; the hosted service shows request, latency, task and database load on one dashboard; under a load test it scales between 1 and its maximum task count with no failed requests |
| **3.2** | Private networks | Run the server and dashboard inside a lab VPC without Tailscale: CA bundle and per-host http trust on the client, a TLS proxy kit (Caddy, nginx), an end-to-end test through a TLS proxy, a private-network docs page | A lab follows the docs to sync from its training machines and sign in from its browsers over HTTPS with an internal certificate, with nothing reaching in from the internet |
| **3.3** | Deploy in your own AWS account | One CloudFormation template (EC2 + SQLite from PyPI, ALB + ACM, Cognito sign-up and sign-in) and OIDC sign-in in the server | A deployer with an AWS account and a Route 53 domain launches the template, signs up with the admin email and syncs a run, with no other setup |
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
| 9 | EFS benchmark and tuning | 2.0 | Cursor | S | [plan](packages/9-efs-benchmark.md) | done (2.1 PR; the real-EFS benchmark ran in package 24.13, Nov 16, 2026) |
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
after 18; 24 after 18c–21; 25 after 16–24; 26 after 24 is live; 28
after 27; 29 after 24.7 and 27, beside 24.3 (29.7 and 29.9 wait on the owner); 30 (Backlog cleanup) last, before the merge to `main`.

Released Nov 17, 2026: `dev` merged into `main` in
[#80](https://github.com/agenticML/agenticml-lite/pull/80) (`d1465a2`), TestPyPI
dry run 44138465918, tag `v3.0.0`, Publish run 44139009661, 3.0.0 on PyPI
(wheel and sdist) and the GitHub release.

| # | Package | Depends on | Coder | Size | Plan | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 16 † | Database layer, SQLite backend, migrations; users, keys, sessions, throttle, audit in tables | 2.2 | Codex | L | [plan](packages/16-database-layer.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 17 † | Runs, sync state and notes in the database; append crash safety; run list from the database | 16 | Cursor | L | [plan](packages/17-runs-in-database.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18 † | Workspaces and accounts: memberships, workspace-bound keys, `/w/<slug>` routes, switcher, server-admin commands, isolation suite | 17 | Codex, Cursor | L | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18.1a | Server core: accounts, workspaces, memberships, migration, `default` conversion, workspace-bound sync, CLI | 17 | Codex, Cursor | L | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18.1b | Route table, `/w/<slug>` and `/api/w/<slug>` routes, account and server-admin APIs, isolation suite | 18.1a | Cursor | L | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18.2 | Dashboard: workspace URLs, switcher, Team and Keys per workspace, admin page, picker | 18.1b | Cursor | M | [plan](packages/18-workspaces.md) | done ([#44](https://github.com/agenticML/agenticml-lite/pull/44)) |
| 18b † | Firebase sign-in for the hosted service | 18 | Cursor | M | [plan](packages/18b-firebase-sign-in.md) | done (3.0 PR; real Google sign-in confirmed Nov 15, 2026; the email and password check is an owner step in the Backlog) |
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
| 21 | S3 object backend | 18 | Codex, Sonnet | M | [plan](packages/21-s3-objects.md) | done (3.0 PR; real S3 checked in AWS roadmap Phase 5: objects AES256 and hashes verified, Nov 16, 2026) |
| 22 | Metric summaries; sort and filter runs by metric | 17, 18 | Cursor | M | [plan](packages/22-metric-summaries.md) | done (3.0 PR) |
| 23 | Operator commands: end-to-end 2.x conversion test (the import itself landed in 16–18), `server backup`, `status`/`verify` on the database | 18, 21, 22 | Sonnet (Codex at limit) | S–M | [plan](packages/23-operator-commands.md) | done (3.0 PR) |
| 24 | AWS hosted deployment (Dockerfile, Terraform, deploy workflow, runbook, real-EFS benchmark) | 18c, 19, 20, 21 | Codex, Sonnet; owner runs it | M | [plan](packages/24-aws-deployment.md) | done (code, 3.0 PR; owner checks in Backlog) |
| 24.1 | Image: Dockerfile, entrypoint, limits file, local hosted smoke test | 21 | Codex, Sonnet | S | [plan](packages/24-aws-deployment.md) | done (3.0 PR) |
| 24.2 | Terraform, deploy workflow, `deploy-check` CI job, runbook | 24.1 | Codex, Sonnet | M | [plan](packages/24-aws-deployment.md) | done (3.0 PR; `terraform validate` runs in CI) |
| 24.3 | Apply, first deploy, restore drill, real-EFS benchmark, exit criterion 6 | 24.5 | Owner | S | [AWS roadmap](aws-deployment-roadmap.md) | done (Nov 16, 2026; AWS roadmap Phases 0–7 done; the owner still confirms the 5.5 alarm email and sends the D6 email to existing sign-ups. Per-step status: [AWS roadmap](aws-deployment-roadmap.md)) |
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
| 24.9 † | Email logins on the Keys and Team pages: key create, member role change, removal and invite link accept email logins (hosted mode); key names stay strict | 24.8 | Sonnet | S | [plan](packages/24.9-email-login-keys-team.md) | done ([#61](https://github.com/agenticML/agenticml-lite/pull/61), Nov 13, 2026) |
| 24.10 † | Archive and restore a workspace on `/admin` (data kept; frees the owner's free workspace; audited); permanent delete stays CLI-only | 24.7 | Sonnet | S | [plan](packages/24.10-admin-archive-workspace.md) | done (PR to `dev`; second review by the supervisor; owner's request, Nov 13, 2026) |
| 24.11 † | Phase 4 wrap-up: status per step in the AWS roadmap; sync state per workspace; no member "Invite link" on Firebase | 24.10 | Supervisor; Sonnet reviews | S–M | [plan](packages/24.11-phase-4-wrap-up.md) | done ([#65](https://github.com/agenticML/agenticml-lite/pull/65); Sonnet review; † second review by Cursor; owner's request, Nov 14, 2026) |
| 24.11a | AWS roadmap: Status column on every step, phase summary; stale on-prem rows (24.9, 28, 29) | – | Supervisor | S | [plan](packages/24.11-phase-4-wrap-up.md) | done ([#65](https://github.com/agenticML/agenticml-lite/pull/65)) |
| 24.11b | Sync cursor per host and workspace (`host/w/<slug>`), migrating host-only entries after a `/state` check; `--reset` clears every workspace of the host | 24.11a | Supervisor | S–M | [plan](packages/24.11-phase-4-wrap-up.md) | done ([#65](https://github.com/agenticML/agenticml-lite/pull/65)) |
| 24.11c † | Firebase servers: no member "Invite link" (API answers 409, Members hides it) | 24.11a | Supervisor | S | [plan](packages/24.11-phase-4-wrap-up.md) | done ([#65](https://github.com/agenticML/agenticml-lite/pull/65); † second review by Cursor: no findings) |
| 24.11d | Docs: runbook sync section, SERVER.md, PROTOCOL.md, README, website hosted workspaces and storage pages | 24.11b, 24.11c | Antigravity | S | [plan](packages/24.11-phase-4-wrap-up.md) | done ([#65](https://github.com/agenticML/agenticml-lite/pull/65); Antigravity drafted, supervisor checked against the code) |
| 24.12 | Phase 5 launch checks: `scripts/hosted_check.py` (make-run, verify, isolation), runbook section, roadmap rows | 24.11 | Sonnet; supervisor reviews | S | [plan](packages/24.12-phase-5-launch-checks.md) | done (Nov 16, 2026; 5.1 to 5.6 run and recorded; the 5.5 email confirmation is pending the owner) |
| 24.13 | Phase 6 restore drill and real-EFS benchmark: `scripts/restore_drill/`, tests, runbook, results | 24.12 | Sonnet; supervisor reviews and runs | M | [plan](packages/24.13-phase-6-restore-drill.md) | done (Nov 16, 2026; drill and benchmark run; resources removed) |
| 25 | Docs, threat model, version 3.0.0 | 16–24 | Sonnet (Codex at limit); prose Antigravity | M | [plan](packages/25-docs-and-release.md) | done (3.0 PR; released Nov 17, 2026: tag `v3.0.0`, PyPI 3.0.0) |
| 25.1 | Client support window (426), version 3.0.0, base-install smoke, 2.2 client check | 16–24 | Sonnet (Codex at limit) | S | [plan](packages/25-docs-and-release.md) | done (3.0 PR) |
| 25.2 | SERVER.md, HOSTED.md, threat model, PROTOCOL.md, README, website, SECURITY.md | 25.1 | Antigravity | M | [plan](packages/25-docs-and-release.md) | done (3.0 PR) |
| 26 | Retire agenticml-server | 24 live | Owner | S | – | not started |
| 27 | Sign-in pages redesigned like agenticml-server (brand panel, logo, Sign in / Sign up tabs) | 18b | Sonnet | S | [plan](packages/27-sign-in-redesign.md) | done ([#57](https://github.com/agenticML/agenticml-lite/pull/57)) |
| 28 | One Settings page: Account, Workspace, Members, API keys and Activity replace the Keys, Team and Account entries (like agenticml-server) | 27 | Sonnet | M | [plan](packages/28-settings-page.md) | done ([#63](https://github.com/agenticML/agenticml-lite/pull/63), Nov 13, 2026) |
| 28.1 | One Admin entry: drop the "Server admin ↗" link from the Settings nav; the top-bar Admin link stays (owner's request on the design board, Nov 14, 2026) | 28 | Supervisor | S | [plan](packages/28.1-settings-nav-admin-link.md) | done ([#66](https://github.com/agenticML/agenticml-lite/pull/66), Nov 15, 2026) |
| 28.2 | Top bar on every signed-in page: a Workspaces link back to `/workspaces` on workspace pages and `/admin`; Sign out and "Signed in as" on the `/workspaces` picker (owner's request, Nov 16, 2026) | 28.1 | Sonnet | S | [plan](packages/28.2-topbar-workspaces-signout.md) | done ([#71](https://github.com/agenticML/agenticml-lite/pull/71); Sonnet coded, supervisor reviewed, Nov 16, 2026) |
| 29 | Docs site: Releases page (1.0–3.0), hosted service section with status, home page, truth fixes, docs CI, policies and sign-in links | 24.7, 27 | Sonnet; prose Antigravity | M | [plan](packages/29-docs-site.md) | done (Nov 16, 2026): merged ([#64](https://github.com/agenticML/agenticml-lite/pull/64), Nov 14) and published to `main` in the docs-only PR [#75](https://github.com/agenticML/agenticml-lite/pull/75) (Nov 16; Pages deploy run 43923087030). Policies filled and fact-checked Nov 15; policy links live on the sign-in pages since deploy run 43811140859 (Nov 16). Left for the owner: D6, email existing sign-ups; Enforce HTTPS moved to the "Docs custom domain" Backlog entry. 29.9 done ([#81](https://github.com/agenticML/agenticml-lite/pull/81)) |
| 29.0 | Plan, roadmap rows, AWS roadmap Phase 7, Backlog entries | – | Supervisor | S | [plan](packages/29-docs-site.md) | done ([#64](https://github.com/agenticML/agenticml-lite/pull/64)) |
| 29.1 | Truth fixes in existing pages and README | 29.0 | Sonnet | S | [plan](packages/29-docs-site.md) | done ([#64](https://github.com/agenticML/agenticml-lite/pull/64)) |
| 29.2a | Docs CI: PR builds, link validator, Node pin, job permissions and concurrency | 29.1 | Sonnet | S | [plan](packages/29-docs-site.md) | done ([#64](https://github.com/agenticML/agenticml-lite/pull/64)) |
| 29.3 | Releases page (1.0, 2.0, 2.1, 2.2, 3.0) | 29.2a | Antigravity; Sonnet | S | [plan](packages/29-docs-site.md) | done ([#64](https://github.com/agenticML/agenticml-lite/pull/64)) |
| 29.4 | Hosted service section: overview and limits, quickstart, workspaces, service status | 29.3 | Antigravity; Sonnet | M | [plan](packages/29-docs-site.md) | done; Settings page wording and the deletion-request finding fixed ([#64](https://github.com/agenticML/agenticml-lite/pull/64)) |
| 29.5 | Home page and navigation | 29.4 | Sonnet | S | [plan](packages/29-docs-site.md) | done ([#64](https://github.com/agenticML/agenticml-lite/pull/64)) |
| 29.5b | Docs site names: "Self-hosted" becomes "On-prem server" and "Hosted" becomes "Hosted service" on the Home tiles; the sidebar's "Server" section becomes "On-prem server" (owner's request, Nov 15, 2026; the tile links landed in [#67](https://github.com/agenticML/agenticml-lite/pull/67)) | 29.5 | Sonnet | S | [plan](packages/29.5b-home-link-cards.md) | done ([#69](https://github.com/agenticML/agenticml-lite/pull/69), Nov 16, 2026) |
| 29.2b | 375px overflow check over every built page, in `docs.yml` | 29.5 | Sonnet | S | [plan](packages/29-docs-site.md) | done ([#64](https://github.com/agenticML/agenticml-lite/pull/64)) |
| 29.2c | Facts test: limits, minimum client, no unpublished tag URLs, no `login --key` | 29.4 | Sonnet | S | [plan](packages/29-docs-site.md) | done; covers the server pages and policy drafts too ([#64](https://github.com/agenticML/agenticml-lite/pull/64)) |
| 29.6 | Publish: docs-only PR to `main`; Pages HTTPS, repo description and homepage | 29.1–29.5, 29.2a–c, D3 | git agent; owner | S | [plan](packages/29-docs-site.md) | done (Nov 16, 2026): docs-only PR [#75](https://github.com/agenticML/agenticml-lite/pull/75) merged to `main` at 17:53 UTC; Pages deploy run 43923087030 succeeded; `/`, `/releases/`, `/hosted/`, `/hosted/quickstart/`, `/hosted/status/` and the three `/legal/` pages return 200; repo description and homepage set Nov 15. Enforce HTTPS is still refused ("The certificate does not exist yet"), so it moved to the "Docs custom domain" Backlog entry |
| 29.7 † | Terms, acceptable use and privacy policy on the site; links on the sign-in and invite pages | 29.6 | Antigravity; owner and lawyer approve; Sonnet | M | [plan](packages/29-docs-site.md) | done (Nov 16, 2026): policies filled and fact-checked on `dev` (Nov 15; D4 delegated to the supervisor, 28 corrections, no row-level security claim, no lawyer review); the owner approved the text by merging [#75](https://github.com/agenticML/agenticml-lite/pull/75); † second review by Cursor of the links ([#64](https://github.com/agenticML/agenticml-lite/pull/64)). `HOSTED_LEGAL_BASE_URL=https://agenticml.github.io/agenticml-lite/legal/` set; deploy run 43811140859 (the #71 merge, package 28.2, head `10fd4ba`) approved by the owner and succeeded; `/login` and `/invite?token=x` show the three links in a browser, no console errors, no overflow at desktop width or 375px. Left for the owner: D6, email existing sign-ups |
| 29.8 | Promotion: Sign in action, banner, header link | 29.7 | Sonnet | S | [plan](packages/29-docs-site.md) | done (Nov 16, 2026): built in [#64](https://github.com/agenticML/agenticml-lite/pull/64); header Sign in is a visible text link; live with [#75](https://github.com/agenticML/agenticml-lite/pull/75) |
| 29.9 | After PyPI 3.0.0: `pip install`, tag URLs, release dates, `pyproject.toml` URLs | owner release | Sonnet | S | [plan](packages/29-docs-site.md) | done ([#81](https://github.com/agenticML/agenticml-lite/pull/81), Nov 17, 2026; released with 3.0.0: install lines, `v3.0.0` raw URLs and the Releases page checked live) |
| 30 † | Backlog cleanup before the 3.0 release to `main`: every Backlog entry fixed, given an owner checklist, or moved to Later | 25, 29 | Sonnet; supervisor reviews (second review for † parts) | L | [plan](packages/30-backlog-cleanup.md) | done ([#79](https://github.com/agenticML/agenticml-lite/pull/79), merged Nov 17, 2026; every part reviewed; † second reviews by the supervisor) |
| 30.1 † | Sync integrity and doc truth: failed atomic upload drops the cursor, restore-window repair tests and runbook, `compat_owner` removed, limits and suspended-workspace texts | – | Sonnet | S–M | [plan](packages/30-backlog-cleanup.md) | done ([#79](https://github.com/agenticML/agenticml-lite/pull/79), merged Nov 17, 2026; Sonnet coded, supervisor reviewed and did the † second review) |
| 30.2 † | Account and workspace lifecycle: `server accounts delete`, workspace delete never skips a step, `workspaces orphans`/`cleanup`, detached accounts | – | Sonnet | M | [plan](packages/30-backlog-cleanup.md) | done ([#79](https://github.com/agenticML/agenticml-lite/pull/79), merged Nov 17, 2026; Sonnet coded; supervisor reviewed with an adversarial review pass, three findings fixed; † second review by the supervisor) |
| 30.3 † | Admin plan edits merge atomically and prefill free limits; Keys copy command without `--key`; metric picker refresh; workspace rename by its owner | 30.1, 30.2 | Sonnet | M | [plan](packages/30-backlog-cleanup.md) | done ([#79](https://github.com/agenticML/agenticml-lite/pull/79), merged Nov 17, 2026; Sonnet coded; supervisor reviewed and did the † second review of the rename) |
| 30.4 † | Sign-in pages: setup and invite in the new shell, theme toggle, Firebase page tests, invite token in the URL fragment on hosted | 30.1, 30.2 | Sonnet | M | [plan](packages/30-backlog-cleanup.md) | done ([#79](https://github.com/agenticML/agenticml-lite/pull/79), merged Nov 17, 2026; Sonnet coded; supervisor reviewed with an adversarial review pass, no findings; † second review by the supervisor) |
| 30.5 | Database: transaction connection cap, committed sizes per reader call, usage views on the database clock, 19.1 notes | 30.1, 30.2 | Sonnet | S–M | [plan](packages/30-backlog-cleanup.md) | done ([#79](https://github.com/agenticML/agenticml-lite/pull/79), merged Nov 17, 2026; Sonnet coded, supervisor reviewed; session-lock pooling moved to Later) |
| 30.6 | AWS template and CI: ECS init process, healthy-hosts alarm from DesiredCount, actions pinned to SHAs, weekly hosted monitor | – | Sonnet | S–M | [plan](packages/30-backlog-cleanup.md) | done ([#79](https://github.com/agenticML/agenticml-lite/pull/79), merged Nov 17, 2026; Sonnet coded, supervisor reviewed) |
| 30.7 | Roadmap and Backlog: owner checklists, deferred entries to Later, stale rows | 30.1–30.6 | Supervisor | S | [plan](packages/30-backlog-cleanup.md) | done ([#79](https://github.com/agenticML/agenticml-lite/pull/79), merged Nov 17, 2026; Backlog rewritten: owner checklists, three entries moved to Later) |

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
- **29** (owner, Nov 13, 2026) makes the GitHub Pages site the front door:
  a Releases page for 1.0, 2.0–2.2 and 3.0, a hosted section that takes a
  new user from sign-up at `app.agenticml.xyz` to a synced run, a public
  status page that mirrors the AWS deployment roadmap, and the policies the
  open sign-up needs. The live site is built from `main` (2.2) and already
  says things that are wrong, so truth fixes come first. Sign-up is not
  promoted on the site until the policies are live (29.7). It runs beside
  24.3; only 29.7 and 29.9 touch code or `pyproject.toml`.

### 3.1: scale and visibility

Started Nov 17, 2026 (owner: "we need metrics to track users and total data
used, to help us scale, and visibility"). Today the hosted service runs a
fixed 1 task (2 by hand), the database is sized for two tasks, and only
per-workspace counters exist, with no history. Order: 31, then 32, then 33.
33 also waits for the owner's "Two-task rolling deploy check" in the
Backlog.

| # | Package | Depends on | Coder | Size | Plan | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 31 | Usage history: `accounts.last_active_at`, daily `stats_daily` snapshots refreshed hourly inside the server, `GET /api/admin/stats`, `agenticml server stats`, Usage section with growth charts on `/admin` | 3.0 | Sonnet | M | [plan](packages/31-usage-history.md) | in review |
| 32 | Capacity metrics: per-minute requests, errors, latency percentiles, peak requests in progress and database connections, and the 31 totals, sent with `PutMetricData` (`--metrics cloudwatch`, off by default); alarms on database CPU credits, database connections and ECS memory in `hosted.yaml`; a CloudWatch dashboard in `bootstrap.yaml` | 31 | Sonnet | M | [plan](packages/32-capacity-metrics.md) | in review |
| 33 | ECS autoscaling: target tracking on service CPU (60%) between `DesiredCount` (minimum) and a new `MaxTaskCount` (1–4, default 1 = off); a per-process Postgres connection budget (`AGENTICML_SERVER_DB_CONNECTIONS`) sized by `DbInstanceClass` (new parameter; micro allows at most 2 tasks) so every task fits the database even during deploys; deploys stop resetting the count; `max_task_count` deploy input | 32, owner's two-task check | Sonnet; owner runs it | M | [plan](packages/33-autoscaling.md) | in review |

- **31** answers "are we growing?" with no AWS change and no data sent
  anywhere: the server keeps one row of totals per UTC day (accounts,
  active accounts over 1, 7 and 30 days, workspaces by state, stored and
  uploaded bytes, runs), refreshed every hour, and `/admin` charts them.
  It works the same self-hosted.
- **32** answers "what is the bottleneck?": 12 metrics with no dimensions
  (no raw paths, no per-task, per-workspace or per-user values; cost and
  privacy), about $1–4/month after the free ten. The dashboard lives in
  the bootstrap stack, so hosted deploys never wait for a bootstrap
  re-apply.
- **33** scales the app tier on CPU. Each Fargate task is one Python
  process; the database stays shared, so each task gets a connection
  budget of `floor(usable / (2 × max tasks))` (covering rolling-deploy
  surges), with usable connections estimated per instance class. RDS
  gives `db.t4g.micro` only about 75 usable connections, so micro allows
  at most 2 tasks; 3–4 tasks need `db.t4g.small` (about +$12/month). It
  ships off (`MaxTaskCount` 1); the owner turns it on after the bootstrap
  re-apply and the two-task check.

### 3.2: private networks

Added Nov 19, 2026 at the owner's request: a lab whose machines are all in a
private VPC, with Tailscale not allowed, wants the server and dashboard.
Sync is outbound HTTPS to any URL, so the server runs inside the VPC behind
a TLS proxy; this release closes the gaps found in the code (no way to trust
an internal CA, `--insecure` on every command, unclear TLS errors, no proxy
example or docs). It does not depend on 3.1 (packages 31–33, merged in
[#84](https://github.com/agenticML/agenticml-lite/pull/84)).

| # | Package | Depends on | Coder | Size | Plan | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 34 † | Private-network deployment (no Tailscale) | 3.0 | Sonnet; prose Antigravity; owner releases | M | [plan](packages/34-private-network.md) | done (this PR; owner steps 34.4 publish and 34.5 left) |
| 34.1 † | Client trust: `--ca-bundle`, saved bundle, `AGENTICML_CA_BUNDLE`, saved `insecure` per host, certificate errors without retries and with clear text, redirect text | 3.0 | Sonnet | M | [plan](packages/34-private-network.md) | done (this PR; Sonnet coded; supervisor reviewed with two adversarial review passes and did the † second review) |
| 34.2 † | Deploy kit (`deploy/private-network/`: Caddy, nginx, env file, checklist incl. first admin, CA, SELinux, backups), sync unit lines, end-to-end test through a TLS proxy with a private CA | 34.1 | Sonnet | M | [plan](packages/34-private-network.md) | done (this PR; Sonnet coded; supervisor reviewed, checked the Caddyfile with `caddy validate` and did the † second review) |
| 34.3 | Docs: SERVER.md section and troubleshooting, THREAT_MODEL.md, PROTOCOL.md, website page and facts test, README, systemd README | 34.1, 34.2 | Antigravity; supervisor checks | S–M | [plan](packages/34-private-network.md) | done (this PR; Antigravity drafted, Sonnet corrected the fact-check findings, supervisor checked against the code) |
| 34.4 | Version 3.2.0; docs require `agenticml-lite>=3.2` and the kit from the matching tag | 34.3 | Sonnet; owner tags and publishes | S | [plan](packages/34-private-network.md) | done in code (this PR: version 3.2.0, Releases page); owner tags and publishes |
| 34.5 | First lab install from the docs (Backlog owner step) | 34.4 | Owner | S | [plan](packages/34-private-network.md) | not started |

### 3.3: deploy in your own AWS account

Added Nov 19, 2026 at the owner's request ("make things a lot simpler for
onboarding … a CloudFormation script for users who want to deploy; target
users that have an AWS environment"). Today, self-serve sign-up needs a
Firebase project, and our AWS templates are tied to our account. Decisions
D36.1–D36.7 are in the plan: one EC2 instance with SQLite installed from
PyPI (no image), ALB + ACM, Amazon Cognito through a generic OIDC client
in the server (which also starts the Later "Enterprise SSO" entry), and
about $45–55/month. It comes after 3.2 (package 34); 36.1 and 36.2 do not
touch 34's files, and 36.3 and 36.4 build on 34 (merged into this branch from PR #86, which must merge first).

| # | Package | Depends on | Coder | Size | Plan | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 36 † | Deploy in your own AWS account | 3.2 | Sonnet; prose Antigravity; owner releases and deploys | L | [plan](packages/36-aws-self-deploy.md) | in progress |
| 36.1 † | Server: OIDC sign-in (authorization code + PKCE), open sign-up, admin emails and invites on OIDC, `--signup-email-domains`, `[oidc]` extra | 3.0 | Sonnet | M–L | [plan](packages/36-aws-self-deploy.md) | done ([#88](https://github.com/agenticML/agenticml-lite/pull/88); Sonnet coded; supervisor reviewed in three rounds with adversarial review passes and did the † second review; checked on Postgres 16 locally) |
| 36.2 † | Template `deploy/aws-self-hosted/agenticml.yaml` (VPC optional, ALB, ACM, Route 53, Cognito, EC2 Auto Scaling group of 1, data volume with daily snapshots), static tests, cfn-lint in CI | 36.1 options | Sonnet | M | [plan](packages/36-aws-self-deploy.md) | done ([#88](https://github.com/agenticML/agenticml-lite/pull/88); Sonnet coded; supervisor reviewed in three rounds with adversarial review passes and did the † second review; cfn-lint clean; AWS behaviour checked statically only until 36.5) |
| 36.3 | Docs: "Deploy on AWS" page, SERVER.md OIDC section, threat model, README, facts test | 36.2, 34.3 | Antigravity; supervisor checks | S–M | [plan](packages/36-aws-self-deploy.md) | plan ready |
| 36.4 | Version 3.3.0; template on the GitHub release; Launch Stack link (owner: public S3 bucket) | 36.3, 34.4 | Sonnet; owner tags and publishes | S | [plan](packages/36-aws-self-deploy.md) | plan ready |
| 36.5 | First real deploy in an AWS account (owner step) | 36.4 | Owner | S | [plan](packages/36-aws-self-deploy.md) | not started |

### Maintenance fixes

- Empty object and lineage upload framing: [plan](packages/fix-empty-uploads.md),
  found in the 2.2 compatibility check; status: done (2.2 PR).
- Version 2.2.0, transient Git failures answer 503, runs no longer track
  their own store files, and the dashboard last-admin-key rule counts only
  usable keys: [plan](packages/backlog-2.2-fixes.md); status: done (2.2 PR).
- Timeout-sensitive tests flaked in the Postgres CI job (many-process
  e2e and metrics tests): [plan](packages/fix-flaky-postgres-timeouts.md),
  found in [#83](https://github.com/agenticML/agenticml-lite/pull/83);
  status: done ([#85](https://github.com/agenticML/agenticml-lite/pull/85)).

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
- Permissions boundary for the hosted stack's roles (moved from the Backlog
  by package 30, Nov 17, 2026; own package after 3.0). A managed policy
  `agenticml-hosted-boundary` in `bootstrap.yaml` (allow-list of what the
  three hosted roles use, including everything the AWS Backup managed
  policies need for EFS and RDS backups and restores, plus a deny of IAM,
  Organizations and account actions), required by an
  `iam:PermissionsBoundary` condition on the CloudFormation role's role
  writes, and a deny of `cloudformation:CreateChangeSet` with
  `cloudformation:ImportResourceTypes` in the deploy role. Why later: the
  owner must apply the bootstrap stack before the next hosted deploy (that
  deploy fails on the missing export otherwise); if the first boundary
  deploy rolls back, removing the boundary is denied and the stack ends in
  `UPDATE_ROLLBACK_FAILED` (ship it as the only change of its deploy); an
  over-narrow boundary silently breaks scheduled backups (check a backup job
  and a restore afterwards); and scoping writes by `Project` tag needs an
  exact action list and is bypassable by tagging another resource (state it
  in the threat model). Only a reviewed template run through the protected
  `hosted` workflow can use the role today. Investigator spec with the
  owner's `simulate-principal-policy` checks: package 30 plan notes.
- Pool Postgres session-lock connections and measure lock latency on RDS
  (moved from the Backlog by package 30). Every session lock opens a new
  connection (about ten round trips on TLS). Pooling changes the
  mutual-exclusion code (reset timeouts per checkout, `pg_advisory_unlock_all`
  before reuse, never reuse a connection whose lock state is unknown) and
  needs a benchmark that does not run migrations against the hosted
  database; do it when the latency is measured as a problem. Package 30.5
  capped transaction connections and documented the per-process ceiling.
- Self-serve account deletion from Settings (the tail of the former
  "Hosted service monitoring" entry). Until then account deletion is an
  operator step: `agenticml server accounts delete` (package 30.2) and the
  runbook section "Deleting an account (privacy request)".
- Built-in TLS for `server serve` (`--tls-cert`, `--tls-key`), so a small
  lab needs no proxy (moved out of package 34, decision D1). The
  dashboard's slow-client defences read raw sockets and must be reworked for
  TLS sockets first; security-sensitive.
- Sync bundles for air-gapped labs (package 34, decision D5):
  `agenticml sync --to-bundle FILE` and `agenticml server import FILE`
  carrying runs, objects, lineage and the owner. Only if a lab has no network
  path from its training machines to any server; `server reindex` copy-in
  loses the owner, objects, lineage and notes.
- Metric picker refresh while no run is live (package 30.3 left it out):
  `/metrics` is refreshed only while the runs page polls, which stops when no
  run is `running`.

## Backlog

Unfinished work found along the way that is not part of a package above. Each
entry says where, the problem, the fix, and when it is done. Place entries by
priority.

Package 30 (Nov 17, 2026) cleared the Backlog before the 3.0 release: 21
entries were fixed in parts 30.1–30.6 (for two of them, the many-process
follow-ups and the metric picker, a remainder moved to "Later
(unscheduled)"), the permissions boundary moved to Later, and the entries
below are the ones that need the owner's accounts or machines. 3.0.0 was released
on Nov 17, 2026; none of the entries below blocked it.

### Owner steps

- **Capacity dashboard and metrics** (package 32, Nov 18, 2026). Do after
  the 3.1 PR is merged to `dev` and deployed.
  - Where: `deploy/aws/README.md`, "Capacity dashboard and metrics"; the
    bootstrap deploy command near the top of that README.
  - Problem: the `agenticml-hosted` CloudWatch dashboard lives in the
    bootstrap stack, which only the owner applies (laptop, admin
    credentials). The hosted deploy turns on `--metrics cloudwatch` but
    cannot create the dashboard.
  - Fix: (1) re-apply the bootstrap stack with the existing command;
    (2) after the next hosted deploy, open CloudWatch → Dashboards →
    `agenticml-hosted` and check that `Requests`, `Latency` and
    `DbConnectionsPeak` show data within a few minutes, and the usage
    totals within 15 minutes; (3) `aws logs tail /ecs/agenticml-hosted
    --since 15m | grep -i "capacity metrics"` shows no warnings;
    (4) confirm the three new alarms are in OK (`rds_cpu_credits`,
    `rds_connections`, `ecs_memory`).
  - Done when: the dashboard shows the custom metrics and the nine hosted
    alarms are OK, recorded here.
- **Turn on autoscaling** (package 33, Nov 19, 2026). After the capacity
  dashboard step above (same bootstrap re-apply) and the two-task rolling
  deploy check below.
  - Where: `deploy/aws/README.md`, "Operations and scaling".
  - Problem: autoscaling ships off (`MaxTaskCount` 1). The connection
    budgets per instance class are estimates (micro 64 usable, small 144,
    medium 300) until checked on the real database.
  - Fix: (1) `SHOW max_connections;` on the hosted database (ECS Exec);
    if the usable number is below the estimate for the class, lower the
    `ConnectionBudget` and `ConnectionsAlarm` mappings in `hosted.yaml`
    first; (2) for more than 2 tasks, set `DbInstanceClass` to
    `db.t4g.small` at a quiet time (a few minutes of downtime) and repeat
    (1); (3) `gh workflow run deploy.yml --ref dev -f max_task_count=2`
    (or 3–4 on small); (4) load test with several sync clients in parallel
    (for example `scripts/hosted_check.py make-run` from a few machines,
    or `bench_server.py` on Fargate as in the README) and watch the
    package 32 dashboard: the service scales out above 60% CPU and back
    in after load stops, `Rejected` and `Errors5xx` stay near 0, and
    `DbConnectionsPeak` and RDS `DatabaseConnections` stay under the
    budget and the alarm.
  - Done when: a load test scales out and back in with no failed syncs,
    the real `max_connections` is recorded here, and the mappings match
    it.
- **3.2.0 release** (package 34.4, owner). Do after this PR is merged to
  `dev` and `dev` is merged to `main`.
  - Where: tags; repository Settings → Environments → `pypi`; the docs.
  - Problem: the code says 3.2.0, but PyPI serves 3.0.0, so the install lines
    that say what `pip install` gets (README, Releases page "Which version
    should I use?", `getting-started/installation.md`, `index.mdx`,
    `server/upgrade.md`, `hosted/quickstart.md`, `deploy/aws/README.md`) and
    the unit-file URLs with `v3.0.0` still name 3.0.0. 3.1 was never
    released separately and ships inside 3.2.0.
  - Fix: (1) add the `pypi` required reviewer (see "Release follow-ups");
    (2) tag `v3.2.0` on `main` and approve the Publish run; (3) then flip the
    install lines, the "Which version" table and the `v3.0.0` unit URLs to
    3.2.0, set the Releases page status to the release date, and update
    `PUBLISHED_3_0` in `tests/test_website_facts.py` if needed.
  - Done when: 3.2.0 is on PyPI and the docs name it, and
    `pip install 'agenticml-lite>=3.2'` works on a clean machine.
- **First lab install on a private network** (package 34.5, owner; after the
  "3.2.0 release" step above).
  - Where: the lab's server VM; `deploy/private-network/README.md`.
  - Problem: the private-network setup has only been tested in CI, never on
    a real lab network.
  - Fix: follow the README on the lab's VM (internal certificate, proxy,
    systemd units) and confirm the Caddy root certificate path; sync from two
    training machines with `login --ca-bundle` and the sync unit; sign in from a lab browser; run
    `agenticml server verify`; note every step that did not match the docs.
  - Done when: both machines' runs show in the dashboard, `server verify`
    reports 0 problems, and each mismatch is fixed or recorded here.
- **Release follow-ups (owner)** (3.0.0 released Nov 17, 2026). Low priority.
  - Where: repository Settings → Environments → `pypi`; tags.
  - Problem: the `pypi` environment has no required reviewer, so any pushed
    `v*` tag publishes to PyPI at once (3.0.0 was published that way, with
    the owner's go). The `v2.2.0` tag was never created (the 2.2 client
    compatibility test falls back to commit `bd0d499`).
  - Fix: (1) add a required reviewer to the `pypi` environment before the
    next release; (2) optional: `gh workflow disable publish.yml`, `git tag
    v2.2.0 bd0d499 && git push origin v2.2.0`, `gh workflow enable
    publish.yml` (or skip it and remove the `v2.2.0` lookup from
    `tests/test_server_client_window.py`).
  - Done when: `gh api repos/agenticML/agenticml-lite/environments/pypi`
    shows a `required_reviewers` rule, and `v2.2.0` exists or the decision to
    skip it is recorded here.
- **Two-task rolling deploy check** (package 24.3, owner). Low priority; not
  needed for the launch.
  - Where: `deploy/aws/README.md`, Launch checks 5.1.
  - Problem: the service has only ever run one steady-state task.
  - Fix: `gh workflow run deploy.yml --ref dev -f desired_count=2` (the
    default branch is `dev`, so this works now; the `healthy_hosts` alarm then
    uses threshold 2); `aws ecs wait services-stable`; both targets healthy
    (`aws elbv2 describe-target-health`); sync several runs from two clients
    or workspaces (mind the 2 GB daily upload limit per free workspace),
    confirm both tasks served requests (log streams per task),
    `scripts/hosted_check.py verify <run ids>` PASS, `server verify --deep`
    over ECS Exec with 0 problems, reload the dashboard a few times (sessions
    across tasks); then `-f desired_count=1`.
  - Done when: a sync through two steady tasks verifies with 0 problems and
    the result is recorded here and on the status page.
- **On-demand EFS backup refused for the operator session** (package 24.13,
  Nov 16, 2026). Low priority.
  - Where: AWS Backup vault `agenticml-hosted`; `scripts/restore_drill/README.md`
    step 1 option A.
  - Problem: `aws backup start-backup-job` for the hosted EFS answered
    AccessDenied for the operator's `aws login` session; restores worked.
  - Fix (diagnosis): (1) `aws cloudtrail lookup-events --lookup-attributes
    AttributeKey=EventName,AttributeValue=StartBackupJob --max-results 5`
    (us-east-2) and read `errorMessage` and `userIdentity`/session policy;
    (2) retry from the console (AWS Backup → Protected resources → On-demand
    backup) and with access-key credentials; (3) if both fail, check
    `iam:PassRole` on `agenticml-hosted-backup` for the caller
    (`aws iam simulate-principal-policy`), `aws backup describe-region-settings`
    (EFS opted in), the vault access policy, and whether the same call works
    for the RDS instance; the EFS file system policy denies only `Client*`
    actions, so it is not the cause. Then the runbook names the credentials to
    use, or says on-demand EFS backups are unavailable from an `aws login`
    session (use the latest scheduled point).
  - Done when: an on-demand backup of the hosted EFS can be started, or the
    runbook says which credentials to use.
- **Check Firebase email and password sign-in on the real project** (package
  18b caveat, Nov 8, 2026; reduced Nov 16). Not release-blocking (Google
  sign-in, which uses the same token check, works).
  - Where: `app.agenticml.xyz/login`.
  - Fix: in a private window, with a fresh mailbox that has no invite:
    (1) create an account with email and password; expect "check your inbox"
    and no dashboard; (2) sign in before verifying: refused; (3) click the
    link, sign in again: dashboard with one personal workspace (sign-in
    repeats `signInWithEmailAndPassword`, which refreshes the token);
    (4) wrong password: "Wrong email or password."; (5) "Forgot password"
    sends a working reset; (6) devtools: no CSP or console errors, nothing in
    local or session storage. Delete the test Firebase user and workspace
    afterwards (`server accounts delete`, package 30.2).
  - Done when: every step passes, or each mismatch is fixed with a test in
    `testbed/tests/test_server_firebase_dashboard.py`.
- **Check `agenticml server up` on a real tailnet** (package 10b caveat, Nov
  6, 2026). Not release-blocking.
  - Where: `src/agenticml/server/up.py`; `docs/SERVER.md` quick start.
  - Fix: on a machine with Tailscale logged in, MagicDNS and HTTPS certificates
    on and the `funnel` node attribute: save redacted `tailscale status --json`
    and `tailscale funnel status --json` (idle and while running); run
    `agenticml server up`, open the URL from a phone off the network, sign in
    with the printed invite link, sync one run from another machine; Ctrl+C
    and confirm `tailscale funnel status` is empty; try each missing
    prerequisite once (Tailscale down, MagicDNS off, HTTPS off, no funnel
    attribute on a throwaway node, another service on 443); `server up
    --install` once on Linux and remove it.
  - Done when: those steps pass, or each mismatch is fixed with a test built
    from the captured JSON in `tests/test_server_up.py`.
- **Docs custom domain** (package 29, decision D8, Nov 13, 2026). Not needed
  for 3.0.0: PyPI's Documentation URL may stay on github.io, which GitHub
  redirects to the custom domain once it is set.
  - Where: `website/astro.config.mjs` (`site`, `base`), `website/public/CNAME`,
    internal `/agenticml-lite/` links in `website/src/content/docs` (also
    `href="..."` and frontmatter `link:` forms), `website/scripts/overflow-check.mjs`,
    `pyproject.toml` `Documentation`, `HOSTED_LEGAL_BASE_URL`; Namecheap; Pages
    settings.
  - Problem: the docs live at `agenticml.github.io/agenticml-lite/`, and
    `agenticml.xyz` has no `docs` record. Enforce HTTPS cannot be turned on for
    the github.io project URL ("The certificate does not exist yet").
  - Fix, in this order (Astro's `base` changes asset URLs, so the domain and
    the root-path build must go live together): (1) Sonnet prepares the
    root-path build (drop `base`, rewrite internal links, redirects from
    `/agenticml-lite/*`, CNAME file, overflow check, `pyproject.toml`) on a
    branch; (2) owner adds the Namecheap CNAME `docs` → `agenticml.github.io.`
    (never touch `app`); (3) owner sets the Pages custom domain and, at once,
    the build is deployed (merge to `main` or dispatch `docs.yml`); (4) tick
    Enforce HTTPS once the certificate exists; (5) set `HOSTED_LEGAL_BASE_URL`
    to `https://docs.agenticml.xyz/legal/` and run the deploy.
  - Done when: `https://docs.agenticml.xyz` serves the site with HTTPS
    enforced, old github.io URLs redirect, and the link check passes.


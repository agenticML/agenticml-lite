# AWS deployment roadmap

Nov 10, 2026 · Supervisor plan · Release 3.0

The steps, in order, to put the 3.0 hosted service live at
`https://app.agenticml.xyz` in our AWS account. Open sign-up and retiring
agenticml-server come after the service is up and checked. Firebase sign-in
moved ahead of step 4.3 on Nov 13, 2026 (see Phase 4).

Decisions (owner, Nov 10, 2026):

- **CloudFormation**, not Terraform. AWS keeps the stack state, so there is
  no state bucket or lock table.
- **One region:** `us-east-2`.
- **Deploys run from a GitHub workflow**: a protected `hosted` environment
  and an OIDC role. Changed Nov 12, 2026: a push to `dev` that changes the
  image or the stack queues a deploy for the owner's approval, so nothing has
  to reach `main` first; manual dispatch stays for later.
- **No NAT gateway.** Tasks run in public subnets with public IPs for
  outbound traffic only; RDS and EFS stay private.
- **No Firebase at first.** People sign in with passwords set through
  invite links. Changed Nov 13, 2026 (owner): Firebase sign-in is turned on
  before step 4.3, with a new Firebase project `agenticml-hosted` that the
  owner administers. The owner has no access to agenticml-server's project
  (`agenticml-auth`), so 3.0 plan decision D9 changed too.
- **DNS stays at Namecheap** (no Route 53). The owner adds the certificate
  validation CNAME once and a CNAME `app` → the load balancer.
- **Cost alerts:** a $100 monthly budget and an email for every $10 spent,
  with a per-service breakdown, to two addresses (given only when the
  bootstrap stack is deployed, never committed). Ops alarms go to the same
  addresses.

- **Lean setup, about $60/month** (owner, Nov 12, 2026; package
  [24.5](packages/24.5-lean-hosted.md)): one ARM64 Fargate task by default,
  RDS `db.t4g.micro`, Container Insights off.

Estimated cost: about **$62–66/month**: one ARM64 Fargate task (0.5 vCPU,
1 GB) ~$14.40, ALB ~$17–18, public IPv4 for the ALB (2) and the task (1)
~$11, RDS `db.t4g.micro` with 20 GB ~$14, EFS ~$1–2, S3 and ECR ~$1–2, logs
and 6 alarms ~$1–2, backups ~$1, Secrets Manager $0.40, cost alerts ~$0.60.
Data transfer under 100 GB/month is free. The earlier design (two x86 tasks,
`db.t4g.small`, Container Insights) was about $100–110. To scale up later:
a second task (~$18; dispatch with `desired_count=2` once `deploy.yml` is on
`main`), `db.t4g.small` (~$12).

Who does what: **agents** write and check code; only the **owner** runs
anything against AWS (billed resources, public endpoint). Exception: on
Nov 12, 2026 the owner had the supervisor run Phase 3 with the owner's
`agenticml-account` profile (bootstrap redeploys, approving the deploy run).

## Current state (Nov 16, 2026)

All seven phases are done as of Nov 16, 2026. Two owner confirmations remain:
the 5.5 alarm email and the D6 email to existing sign-ups.

Where things stand; update this section, and the Status column of the step,
in the PR that records a result.

### Phase summary

| Phase | Status | Done | Next, and who |
| --- | --- | --- | --- |
| 0. Account checks | **done** (Nov 11) | 0.1–0.4 | – |
| 1. CloudFormation code | **done** (Nov 11, [#49](https://github.com/agenticML/agenticml-lite/pull/49)) | 1.1–1.6 | – |
| 2. Bootstrap | **done** (Nov 11) | 2.1, 2.2, 2.3 | 2.1b closed: one alert address (owner's choice, Nov 16) |
| 3. First deploy | **done** (Nov 12) | 3.0a–3.3 | – |
| 4. First accounts and data | **done** (Nov 16) | 4.1, 4.2, 4.2a (Nov 13); 4.2b (Nov 15); 4.3 and 4.4 (owner confirmed Nov 16) | – |
| 5. Launch checks | **done** (Nov 16; the 5.5 email confirmation is pending the owner) | 5.1 to 5.6 (Nov 16); package [24.12](packages/24.12-phase-5-launch-checks.md) | The owner confirms the 5.5 alarm email arrived |
| 6. Recovery and performance | **done** (Nov 16, package [24.13](packages/24.13-phase-6-restore-drill.md)) | 6.1 restore drill, 6.2 real-EFS benchmark | – |
| 7. Public launch | **done** (Nov 16; the D6 email and the 5.5 email confirmation are pending the owner) | 7.1 (Nov 14, [#64](https://github.com/agenticML/agenticml-lite/pull/64)); 7.2 to 7.6 (Nov 16, [#75](https://github.com/agenticML/agenticml-lite/pull/75) and deploy run 43811140859) | The owner emails existing sign-ups about the policies (D6) and confirms the 5.5 alarm email |

### Live service

| What | State |
| --- | --- |
| Bootstrap stack `agenticml-hosted-bootstrap` | `UPDATE_COMPLETE`; redeployed twice on Nov 12 with the [24.5](packages/24.5-lean-hosted.md) fixes (immutable OIDC subject, `ecr:BatchGetImage`, EFS service-linked role) |
| Hosted stack `agenticml-hosted` | `UPDATE_COMPLETE` from deploy run 43811140859 (Nov 16; `dev` at `10fd4ba`, the [#71](https://github.com/agenticML/agenticml-lite/pull/71) merge, package 28.2), image `10fd4ba…-arm64`, with `AGENTICML_SERVER_LEGAL_BASE_URL` set so the policy links show on the sign-in pages. The owner approved the run, which shipped 28.2 and the policy links in one roll. Run 43722433179 (Nov 15, `dev` at `0bd558d`, the [#65](https://github.com/agenticML/agenticml-lite/pull/65) merge, which includes [#66](https://github.com/agenticML/agenticml-lite/pull/66)) carried package 24.11 (sync state per workspace in the client; no member "Invite link" button on Firebase) and package 28.1 (Settings nav without the Server admin link). The older waiting run 43705268601 (the #66 merge) was rejected as superseded. Earlier: run 43269670813 (Nov 12 and 13) shipped the 24.6 `whoami` change and the Firebase variables |
| Service | 1 of 1 ARM64 task (0.5 vCPU / 1 GB) running, a new task definition from run 43811140859 (Nov 16; the number was not checked); before it, `agenticml-hosted:9` (run 43722433179) and a forced deployment on Nov 16 for step 5.1 |
| Database | `agenticml-hosted`, `db.t4g.micro`, single-AZ, 20 GB, available |
| Alarms | all six OK; Container Insights off. The 5.5 drill put `agenticml-hosted-alb_5xx` in `ALARM` on Nov 16 (05:11 UTC); it returns to OK by itself |
| Load balancer | `agenticml-hosted-1705349735.us-east-2.elb.amazonaws.com`; `/healthz` 200 with the `app.agenticml.xyz` host |
| DNS | `app` CNAME at Namecheap → the load balancer (step 3.2, Nov 12); `https://app.agenticml.xyz/healthz` returns 200 with a valid certificate |
| Admin account | The first admin, created through `/setup` on Nov 13 (step 4.2): owner of `default`, server admin, password login. With Firebase on, it can no longer sign in on the web; it stays as the break-glass account for the server CLI over ECS Exec |
| Sign-in | **Firebase**, project `agenticml-hosted` (Google and email/password; `app.agenticml.xyz` is an authorized domain). `/login` serves the Firebase page with no console or CSP errors; password `POST /login` and `/setup` return 404. **Open sign-up is on** since Nov 13, 2026 (owner's choice): a verified sign-in with no account gets a free personal workspace. Since Nov 16 `/login` and `/invite` show the policy links ("By continuing you agree to the Terms and the Acceptable use policy. See the Privacy policy."), with no console errors and no horizontal overflow at desktop width or 375px |
| Email admin account | Through `HOSTED_ADMIN_EMAILS` (package [24.7](packages/24.7-self-serve-admins.md), [#58](https://github.com/agenticML/agenticml-lite/pull/58), task definition `agenticml-hosted:4`; the server logs one admin email configured). The owner's first Google sign-in created the account as server admin (step 4.2b, done; confirmed by the owner Nov 15) |
| GitHub `hosted` environment | `HOSTED_FIREBASE_PROJECT=agenticml-hosted` and `HOSTED_FIREBASE_API_KEY` set Nov 13; `HOSTED_ADMIN_EMAILS` (the owner's Google email) and `HOSTED_OPEN_SIGNUP=true` set Nov 13. `HOSTED_DESIRED_COUNT` was unused since 24.5 and was deleted Nov 15, 2026. `HOSTED_LEGAL_BASE_URL=https://agenticml.github.io/agenticml-lite/legal/` set Nov 16 (the live `/auth/firebase/config` returns the three policy URLs, all 200) |

Next: step 4.3 on `/admin` and each workspace's Settings page (4.2b, the
owner's Google sign-in as server admin, is done), then 4.4. Package
[24.11](packages/24.11-phase-4-wrap-up.md) (done) lets 4.4 use one local store for
both workspaces and removes the member "Invite link" button, which fails
with 403 for most members on Firebase. Both 24.11 and 28.1 are deployed
(run 43722433179, Nov 15, 2026), so the button is gone on the live service. Open sign-up was turned on early (Nov 13, 2026); the terms,
acceptable use and privacy policies and their sign-in page links were Phase 7
work (7.3, 7.4, done Nov 16, 2026).

## Phase 0: Account checks (owner, ~30 min)

**Status: done** (Nov 11, 2026). The account already has a GitHub OIDC
provider used by other roles; its ARN is passed to the bootstrap stack.

| # | Step | Done when | Status |
| --- | --- | --- | --- |
| 0.1 | Install AWS CLI v2 and the Session Manager plugin; sign in to the target account in `us-east-2` | `aws sts get-caller-identity` shows the right account | done (Nov 11) |
| 0.2 | Use a non-root admin profile (`agenticml-account`) | `aws sts get-caller-identity` shows the IAM user, not root | done (Nov 11) |
| 0.3 | Check for an existing GitHub OIDC provider (agenticml-server may have one): `aws iam list-open-id-connect-providers` | ARN noted, or "none" | done (Nov 11): provider exists; its ARN goes to the bootstrap stack |
| 0.4 | Enable Cost Explorer (Billing → Cost Explorer → Launch); the budget itself comes from the bootstrap stack | Cost Explorer opens | done (Nov 11) |

## Phase 1: CloudFormation code (agents)

Package 24.4, replacing the Terraform half of package 24.2. Plan:
[24.4-cloudformation.md](packages/24.4-cloudformation.md).

**Status: done** in [#49](https://github.com/agenticML/agenticml-lite/pull/49)
(Nov 11, 2026). Phase 0 checks found an existing GitHub OIDC provider in the
account, used by other roles: always pass its ARN to the bootstrap stack.

| # | Step | Done when | Status |
| --- | --- | --- | --- |
| 1.1 | `bootstrap.yaml`: ECR repository, GitHub OIDC provider (optional), deploy role, CloudFormation role, ACM certificate, alerts topic, $100 budget, $10-step cost emails | `cfn-lint` clean; tests pass | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49), Nov 11) |
| 1.2 | `hosted.yaml`: VPC (ALB, task and data subnets, no NAT), ALB with the bootstrap certificate, ECS Fargate, RDS Postgres 16, EFS, S3, Backup, alarms | `cfn-lint` clean; invariant test passes | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49), Nov 11) |
| 1.3 | `deploy.yml` builds or reuses the SHA image, then runs `aws cloudformation deploy`; CI runs `cfn-lint` in place of Terraform | Full test suite passes | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49), Nov 11) |
| 1.4 | Remove `deploy/aws/terraform/` and `swap_image.py` | Gone; no Terraform in CI | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49), Nov 11) |
| 1.5 | Review by the supervisor, plus a † second review of IAM and the deploy role (supervisor, while Codex and Cursor are disabled) | Both clean | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49), Nov 11) |
| 1.6 | Rewrite the runbook (`deploy/aws/README.md`, `docs/HOSTED.md`) for CloudFormation | Supervisor checked it against the templates | done ([#49](https://github.com/agenticML/agenticml-lite/pull/49), Nov 11) |

## Phase 2: Bootstrap (owner, ~15 min)

**Status: done** (checked against the account on Nov 12, 2026).

- Stack `agenticml-hosted-bootstrap` is `CREATE_COMPLETE` (Nov 11) with the
  existing OIDC provider ARN, a $100 budget and $10 cost steps. All five
  outputs are present.
- The certificate for `app.agenticml.xyz` is `ISSUED`. Keep its validation
  CNAME in Namecheap: ACM needs it to renew.
- The GitHub `hosted` environment requires the owner's review and allows only
  `dev`, `main` and `v*` tags. All six variables are set, and both role ARNs
  match the stack outputs.
- A hand-made $10 console budget was deleted, so `agenticml-monthly` is the
  only budget.
- Closed Nov 16 (owner's choice): the second alert address never confirmed
  its SNS subscription and the pending subscription expired, so the alerts
  topic has one email address and the second is dropped.

| # | Step | Done when | Status |
| --- | --- | --- | --- |
| 2.1 | Deploy the bootstrap stack from a laptop with admin credentials, passing the two alert emails and the OIDC ARN from 0.3 if one exists. While it waits, copy the certificate validation CNAME into Namecheap | Stack `CREATE_COMPLETE`; outputs show the roles, ECR URI, certificate and alerts topic | done (Nov 11) |
| 2.1b | Each alert address clicks the AWS confirmation email | Each alert address has a confirmed subscription | closed (Nov 16): one alert address, by the owner's choice. The second address had not confirmed on Nov 12; its pending subscription expired and AWS removed it, and the owner decided not to re-subscribe it |
| 2.2 | In GitHub, create the `hosted` environment with required reviewers, limited to trusted branches/tags | Environment exists | done (Nov 11) |
| 2.3 | Set the `hosted` variables: role ARNs from 2.1, `AWS_REGION=us-east-2`, desired count 2 (`HOSTED_DESIRED_COUNT`, deleted Nov 15, 2026); leave the Firebase variables empty and sign-up `false` | Variables set | done (Nov 11) |

## Phase 3: First deploy (owner, ~30 min, mostly waiting)

**Status: done** (Nov 12, 2026). A pre-flight check on Nov 12, 2026 found two prerequisites, 3.0a and 3.0b.
3.0a is done in package 24.5 (deploys run on pushes to `dev`); 3.0b is done
(the role already existed on Nov 12, 2026). The first deploy (Nov 12, 2026)
needed two bootstrap fixes, applied by redeploying the bootstrap stack: the
deploy role now trusts GitHub's immutable OIDC subject, and it may read
manifests in the ECR repository (`ecr:BatchGetImage`), which `docker push`
needs (see the 24.5 plan).

| # | Step | Done when | Status |
| --- | --- | --- | --- |
| 3.0a | Make the deploy workflow runnable without `main`. GitHub only runs a `workflow_dispatch` workflow whose file is on the default branch, so `deploy.yml` also runs on pushes to `dev` that change `src/`, `pyproject.toml`, `deploy/aws/` or the workflow (package 24.5). **Done**: merging 24.5 into `dev` queues the first run | A "Deploy hosted service" run waits for approval in GitHub Actions | done ([#52](https://github.com/agenticML/agenticml-lite/pull/52), Nov 12) |
| 3.0b | Create the EFS service-linked role once with admin credentials: `aws iam create-service-linked-role --aws-service-name elasticfilesystem.amazonaws.com`. The account has none yet, and the bootstrap CloudFormation role may create service-linked roles only for ECS, Elastic Load Balancing, RDS and Backup, so the first create would likely fail at the EFS mount targets and roll back. **Done**: the role already existed when checked on Nov 12, 2026 | `aws iam get-role --role-name AWSServiceRoleForAmazonElasticFileSystem` succeeds | done (role already existed, Nov 12) |
| 3.1 | After 3.0b, approve the waiting deploy run (the 24.5 merge into `dev`) in GitHub Actions; `desired_count` defaults to 1. **Done** Nov 12, 2026 (run 43206113226, image `23dcb7c…-arm64`) | Workflow green; stack `CREATE_COMPLETE` (RDS takes the longest) | done (Nov 12) |
| 3.2 | In Namecheap, add a CNAME `app` → the `AlbDnsName` output. **Done** Nov 12, 2026 (`agenticml-hosted-1705349735.us-east-2.elb.amazonaws.com`) | `dig +short app.agenticml.xyz` shows the load balancer | done (Nov 12) |
| 3.3 | Check the service; optionally delete the unused `HOSTED_DESIRED_COUNT` variable from the `hosted` environment (deleted Nov 15, 2026). Checked through the load balancer on Nov 12, 2026: `/healthz` 200, one ARM64 task, six alarms OK, logs clean. **Done** Nov 12, 2026: `curl --fail https://app.agenticml.xyz/healthz` returns `{"ok":true}` | `curl https://app.agenticml.xyz/healthz` returns 200; one task running; logs clean | done (Nov 12) |

## Phase 4: First accounts and data (owner)

**Status: done** (Nov 16, 2026; the owner confirmed 4.3 and 4.4). Earlier status (Nov 15, 2026): 4.1, 4.2 and 4.2a done (Nov 13);
4.2b done (the owner signed in with Google as server admin; confirmed Nov
15); package 24.7 (self-serve server admins) is deployed, so 4.3 runs
through the web; the ECS Exec commands below still work as a fallback.
Package [24.11](packages/24.11-phase-4-wrap-up.md) closed the two code gaps
found for 4.3 and 4.4 ([#65](https://github.com/agenticML/agenticml-lite/pull/65)). Packages 24.11 and 28.1 were deployed on Nov 15, 2026 (run 43722433179), so the member "Invite link" button is already gone on the live service before 4.3. Commands are in `deploy/aws/README.md`, sections "Accounts, Firebase
and customers" and "Sync from a training machine" (rewritten in package
[24.6](packages/24.6-phase-4-runbook.md) after a check against the code).

Firebase sign-in moved here from "After AWS is live" (owner, Nov 13, 2026),
so 4.3 creates email accounts that sign in with Google or Firebase
email/password instead of setting passwords through invite links. With
Firebase on, password sign-in on the web, `/setup` and password invites are
off; the server CLI over ECS Exec still works.

| # | Step | Done when | Status |
| --- | --- | --- | --- |
| 4.1 | ECS Exec access. The `agenticml-account` admin user already has `ecs:ExecuteCommand`, and the service and task role allow it; install the Session Manager plugin if `session-manager-plugin --version` fails. **Done** Nov 13, 2026: Session Manager plugin 1.2.896.0; the environment check showed the public URL, the RDS host with `sslmode=verify-full`, the S3 objects bucket, `PGPASSWORD` set and no `agenticml.db` | `aws ecs execute-command ... --command /bin/sh` opens a shell, and the environment check (database URL, `PGPASSWORD`, no `/data/server/agenticml.db`) passes | done (Nov 13) |
| 4.2 | Create the first admin through the `/setup` link logged at startup (newest "No admin user yet" line in `/ecs/agenticml-hosted`), on `https://app.agenticml.xyz` only; login without `@`, password of 12+ characters. This makes the account owner of `default` and server admin and closes `/setup`. **Done** Nov 13, 2026: `server users list` shows the account as `owner`, created by `setup` | You can sign in at `/login`; `/setup` returns 404 | done (Nov 13) |
| 4.2a | Turn on Firebase sign-in. Firebase console: create a project the owner administers, enable Email/Password (not email link) and Google, add `app.agenticml.xyz` to the authorized domains, register a web app (no Hosting). GitHub `hosted` environment: set `HOSTED_FIREBASE_PROJECT` and `HOSTED_FIREBASE_API_KEY`, keep `HOSTED_OPEN_SIGNUP=false`. Re-run the deploy and approve it. **Done** Nov 13, 2026: project `agenticml-hosted`, deploy run 43269670813 attempt 2, task definition `agenticml-hosted:3` | The task has `AGENTICML_SERVER_FIREBASE_PROJECT`; `/login` shows "Continue with Google" with no console errors; `POST /login` with a password and `/setup` return 404 | done (Nov 13) |
| 4.2b | Create the owner's web admin account, named after the exact email of the Google account they sign in with. With package 24.7: set `HOSTED_ADMIN_EMAILS` to that email in the GitHub `hosted` environment, approve the deploy, and sign in at `/login` with "Continue with Google"; no ECS Exec. Without 24.7 (fallback): in ECS Exec, `agenticml server accounts add <email> --server-admin --data /data`, then `agenticml server users add <email> --role admin --workspace default --public-url https://app.agenticml.xyz --data /data`. The account has no password, so the first verified Google sign-in with that email links to it. Then sign in at `/login` with "Continue with Google" | The owner lands in `default` as server admin; `server users list` shows the email account | done (owner confirmed Nov 15) |
| 4.3 | Create two customer workspaces, each with an admin, a member and a sync key, using email logins: with package 24.7, on `/admin` (workspace and owner email) and each workspace's Settings > Members and API keys sections; without it, through ECS Exec (`users add <email>`). People accept their invite or sign in with "Continue with Google" or Firebase email/password; the email must be verified and match the login. Then add one account to the other workspace with a different role (Settings > Members > Send invite, or `users add` over ECS Exec). The Firebase path was re-checked in package 24.7d; with Firebase on, Members has no per-member "Invite link" (24.11): someone who lost their invite gets a new one from "Send invite", and an existing member just signs in at `/login` | Each person sees only their own workspaces, with the right roles | done (owner confirmed Nov 16) |
| 4.4 | On a training machine with 3.0.0 installed from `dev`, sync one smoke run per workspace with that workspace's key. With a client that has package 24.11, one `AGENTICML_DIR` is enough (sync state is kept per workspace); with an older client, use one `AGENTICML_DIR` per workspace | Runs, metrics and artifacts show in the right dashboard and nowhere else | done (owner confirmed Nov 16) |

## Phase 5: Launch checks (owner, exit criterion 6)

**Status: done** (Nov 16, 2026; the 5.5 email confirmation is pending the owner). Phase 4 is done. Package
[24.12](packages/24.12-phase-5-launch-checks.md) built `scripts/hosted_check.py`
(`make-run`, `verify`, `isolation`) and the runbook section "Launch checks
(Phase 5)" in `deploy/aws/README.md`, which has the commands and pass lines for
each step. The owner runs 5.1 to 5.6 and records date, run id, result and any
Backlog entry in the row. On Nov 16 the supervisor ran the AWS steps (5.1 deploy,
5.4 and 5.5 checks, 5.6 log search) with the owner's profile at the owner's
request, and the owner ran the client side. Results: 5.1, 5.2, 5.3, 5.4 and 5.6
passed; 5.5 fired and is done except the owner's confirmation of the email.

| # | Step | Done when | Status |
| --- | --- | --- | --- |
| 5.1 | Rolling deploy during an upload: start a large upload (`make-run --mb 300`, then `agenticml sync --all`), then force a new deployment (`aws ecs update-service ... --force-new-deployment`) while it uploads. A re-run of the workflow does not roll anything: the commit is already live, so the change set is empty and ECS starts no new task | No acknowledged upload lost; the client retried and finished on its own; `hosted_check.py verify` passes for the run | done (Nov 16): forced deployment 05:34:23 UTC during a `make-run --mb 1500 --files 60` upload to customer-two (with customer-two's key; the first record said customer-one, corrected Nov 16 from the S3 prefix layout and the isolation probe); new task healthy 05:35:08; old task draining 05:36:09, stopped with exit 0 at 05:39:18; the 60 object uploads split 30/30 across the two tasks, one finishing during draining; no failed request (one expected 404); sync finished without a re-run; `verify` PASS |
| 5.2 | Isolation: `hosted_check.py isolation` with customer-one's key against a customer-two run, its objects and URLs, plus one browser session check (customer-one's owner opens customer-two's dashboard and a run URL) | Every probe gets 404, 401 or 403 (or "missing"), never data | done (Nov 16): `isolation` with customer-one's key against customer-two's run: state, lineage and commit 404; dashboard page 303 to `/login`; dashboard API 401 (with the key and without credentials); RESULT PASS. The objects probe was skipped (no local copy of the run in that store); S3 keeps objects under per-workspace prefixes, so the same file uploaded to both workspaces is stored twice. Browser step (owner): customer-one's owner sees "not found" for customer-two's dashboard and run URL |
| 5.3 | A 2.2 client (commit `bd0d499`) syncs a run to the hosted service | Sync succeeds; `verify` passes with the 3.0 tool | done (Nov 16): the 2.2 client (`bd0d499`, installed with `uv pip`) synced a smoke run to customer-one (files=8, objects=0 because the identical artifact already existed in that workspace); `verify` with the 3.0 tool PASS (owner). Gotchas are in the runbook, 5.3: `uv venv` makes a venv without `pip`, and a stale `agenticml` on `PATH` (shell command hash) can shadow the venv's |
| 5.4 | S3 check: `aws s3api head-object` on sampled objects shows `AES256`; a dashboard download matches the source bytes (`sha256sum`) | `ServerSideEncryption: AES256` on sampled objects; the download's sha256 equals the local artifact's | done (Nov 16): all 64 stored objects use SSE-S3 `AES256`, and all 64 hash exactly to their content address (server-side check by the supervisor, 0 mismatches); with `verify` PASS this shows the stored bytes equal the uploaded bytes. The owner's dashboard download was not separately confirmed |
| 5.5 | Alarm drill: `aws cloudwatch set-alarm-state` puts one alarm in `ALARM` | The alert address receives the ALARM email (one address, 2.1b closed) | done except the owner's confirmation of the email: the drill fired on Nov 16 (`ALARM` at 05:11 UTC, action published to the alerts topic); the owner has not yet confirmed the email arrived |
| 5.6 | Row-level security role check: search `/ecs/agenticml-hosted` for "row-level security is not enforced" (the task connects as the RDS master user) | Result recorded (warning present or absent) and the Backlog entry updated | done (Nov 16): no warning; RLS enforced |

## Phase 6: Recovery and performance (owner)

**Status: done** (Nov 16, 2026, package
[24.13](packages/24.13-phase-6-restore-drill.md)). The supervisor ran the AWS
steps with the owner's profile; the owner approved the drill resources (cost
under $1). Every drill resource was deleted afterwards; the scheduled recovery
point was kept. Runbook: `scripts/restore_drill/README.md`.

| # | Step | Done when | Status |
| --- | --- | --- | --- |
| 6.1 | Restore drill: RDS point-in-time restore, EFS restore from AWS Backup and S3 object versions, all into a separate test service | Recovery age, duration and integrity recorded; drill resources removed | done (Nov 16): from the scheduled daily EFS point (backup 05:00 to 08:03). **Rule found:** restore the database to the recovery point's completion time (T_DB = T_S3 = `CompletionDate`). Attempt 1 at the backup start (05:00) had 60 `object_missing` problems and 60 `object_orphan` warnings for the run synced at 05:33; attempt 2 at 08:03:34 had 0 and 0. Timings (attempt 2): RDS 12 min 36 s; EFS restore job 1 min 32 s, mount target and access points ready after about 17 min; S3 copy of 64 versions (1.57 GB) with SHA-256 check 3 min 11 s; app healthy 22 s after the task was created. The restores ran in parallel, so a verified service took the RDS restore plus about 1 minute. Recovery point age: EFS daily point (up to 24 h plus the 3 h backup run); RDS about 5 min; S3 continuous. Gates: read-only SQL (migrations 9, counts equal to production except one later run); server status lists all 11 workspaces; `verify --deep` 0 problems, 0 warnings; a new sync verified, admin-key logins list their runs, `verify --deep` 0 and 0 afterwards. Firebase sign-in was not testable (no public DNS). An on-demand EFS backup was refused with AccessDenied (Backlog, low). Production unchanged; every drill resource deleted. Final listing 16:03 UTC: nothing remains; production unchanged |
| 6.2 | Real-EFS benchmark with 1 and 8 clients | Numbers recorded in `docs/SERVER.md` | done (Nov 16): on the restored drill filesystem with production's settings (General Purpose, Elastic, encrypted, TLS access point), Fargate ARM64 4 vCPU, `bench_server.py` at `0bd558d`, SQLite on local disk, 3 repetitions each. 1 client: commit p50 about 11 ms, object PUT p50 about 35 ms, 1.04 MB/s initial sync. 8 clients: commit p50 about 53 ms, object PUT p50 about 76 ms, 3.43 MB/s. The 30 s `--follow --interval`, the 60 s heartbeat and `APPEND_CHUNK` recommendations were kept (numbers in `docs/SERVER.md`) |

At this point the AWS deployment is live and package 24.3 is done.

## Phase 7: Public launch (owner and agents)

**Status: done** (Nov 16, 2026): 7.1 merged to `dev` in
[#64](https://github.com/agenticML/agenticml-lite/pull/64). The docs site and
the policies were published together in the docs-only PR
[#75](https://github.com/agenticML/agenticml-lite/pull/75) to `main` (7.2), and
`/login` and `/invite` link to the policies since deploy run 43811140859
(7.4). The contact address is `contact@agenticml.xyz`, an existing mailbox on
the domain's Zoho Mail (decided Nov 16, 2026). One owner step is left: the D6
email to existing sign-ups.
Plan: package [29](packages/29-docs-site.md). The service has taken sign-ups since
Nov 13. This phase made the docs site the way people find and use it.

| # | Step | Who | Done when | Status |
| --- | --- | --- | --- | --- |
| 7.1 | Docs site: Releases page, hosted section (overview, quickstart, workspaces) and a public "Service status and roadmap" page that mirrors this roadmap without AWS identifiers (packages 29.1–29.5 and 29.2a–c) | Sonnet, Antigravity | Merged to `dev` with the build, link and 375px checks green | done ([#64](https://github.com/agenticML/agenticml-lite/pull/64), Nov 14) |
| 7.2 | Publish the site through a docs-only PR to `main`; enforce HTTPS on Pages; set the repo description and homepage (29.6) | git agent; owner merges and sets them | The live site shows the hosted pages and status page | done (Nov 16): docs-only PR [#75](https://github.com/agenticML/agenticml-lite/pull/75) merged to `main` at 17:53 UTC (`website/` and `docs.yml` copied from `dev`); Pages deploy run 43923087030 succeeded; `/`, `/releases/`, `/hosted/`, `/hosted/quickstart/`, `/hosted/status/`, `/legal/terms/`, `/legal/acceptable-use/` and `/legal/privacy/` return 200. The repo description and homepage were set Nov 15. Enforce HTTPS: GitHub still answers "The certificate does not exist yet" (Nov 16, after publishing); the default `github.io` project URL has no certificate of its own for this setting, so it moved to the "Docs custom domain" Backlog entry in the on-prem roadmap |
| 7.3 | Owner decides open sign-up until the policies exist (package 29, D1), and supplies the policy facts (D4) | Owner | Decisions recorded in the 29 plan | done (Nov 15): open sign-up stays on; D1 (publish together with the policies) and D4 (delegated to the supervisor; the owner approves by merging the PR to `main`) are recorded in the [29 plan](packages/29-docs-site.md); the owner approved the policy text by merging #75 (Nov 16, no lawyer review); the contact address `contact@agenticml.xyz` was chosen by the owner |
| 7.4 | Terms, acceptable use and privacy policy on the docs site, linked from `/login` and the invite page (29.7); email existing sign-ups (D6) | Antigravity drafts; owner and lawyer approve; Sonnet links; owner approves the deploy | `/login` and `/invite` show the three links in a browser, and they return 200 | done (Nov 16): policies filled and fact-checked on `dev` (Nov 15; 28 corrections, no row-level security claim); no lawyer review. `HOSTED_LEGAL_BASE_URL=https://agenticml.github.io/agenticml-lite/legal/` set; deploy run 43811140859 (the waiting #71 merge, package 28.2, head `10fd4ba`) approved by the owner and succeeded. The live `/auth/firebase/config` returns the three policy URLs (all 200); in a browser `/login` and `/invite?token=x` show the footer with the three links, no console errors, no horizontal overflow at desktop width or 375px. Left for the owner: D6, email existing sign-ups about the policies |
| 7.5 | Promote: Sign in action, banner and header link on the docs site (29.8) | Sonnet | Live, with no overflow at 375px | done (Nov 16): built in [#64](https://github.com/agenticML/agenticml-lite/pull/64); live with #75 |
| 7.6 | Keep the status page current: the PR that records a result in Phases 4–6 also updates the docs status page, then the git agent opens the docs-only PR to `main` | Supervisor; owner merges | The live status page shows the result and a new "last updated" date | done as a practice (Nov 16): PR #74 (contact address) and PR #76 (status page date) went to `dev` and were published through #75; from now on, each result that changes the public status page goes to `dev`, then a docs-only PR to `main`. Ongoing |

## After AWS is live (separate work, not in this roadmap)

- Firebase sign-in moved into Phase 4 (steps 4.2a and 4.2b) on Nov 13,
  2026.
- Terms of service, acceptable use and privacy policies, linked from the
  sign-in page: moved into Phase 7 (step 7.4) on Nov 13, 2026.
- Open sign-up (exit criterion 7): turned on Nov 13, 2026 (owner's choice) by
  setting `HOSTED_OPEN_SIGNUP=true` and re-running deploy run 43417765936.
  Exit criterion 7's storage-limit walk-through is still to do.
- Merge `dev` → `main`, tag `v2.2.0` and `v3.0.0`, publish to PyPI.
- Package 26: retire agenticml-server.

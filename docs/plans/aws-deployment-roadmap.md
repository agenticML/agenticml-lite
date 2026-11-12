# AWS deployment roadmap

Nov 10, 2026 · Supervisor plan · Release 3.0

The steps, in order, to put the 3.0 hosted service live at
`https://app.agenticml.xyz` in our AWS account. Firebase, open sign-up and
retiring agenticml-server come after the service is up and checked.

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
  invite links.
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
anything against AWS (billed resources, public endpoint).

## Phase 0: Account checks (owner, ~30 min)

**Status: done** (Nov 11, 2026). The account already has a GitHub OIDC
provider used by other roles; its ARN is passed to the bootstrap stack.

| # | Step | Done when |
| --- | --- | --- |
| 0.1 | Install AWS CLI v2 and the Session Manager plugin; sign in to the target account in `us-east-2` | `aws sts get-caller-identity` shows the right account |
| 0.2 | Use a non-root admin profile (`agenticml-account`) | `aws sts get-caller-identity` shows the IAM user, not root |
| 0.3 | Check for an existing GitHub OIDC provider (agenticml-server may have one): `aws iam list-open-id-connect-providers` | ARN noted, or "none" |
| 0.4 | Enable Cost Explorer (Billing → Cost Explorer → Launch); the budget itself comes from the bootstrap stack | Cost Explorer opens |

## Phase 1: CloudFormation code (agents)

Package 24.4, replacing the Terraform half of package 24.2. Plan:
[24.4-cloudformation.md](packages/24.4-cloudformation.md).

**Status: done** in [#49](https://github.com/agenticML/agenticml-lite/pull/49)
(Nov 11, 2026). Phase 0 checks found an existing GitHub OIDC provider in the
account, used by other roles: always pass its ARN to the bootstrap stack.

| # | Step | Done when |
| --- | --- | --- |
| 1.1 | `bootstrap.yaml`: ECR repository, GitHub OIDC provider (optional), deploy role, CloudFormation role, ACM certificate, alerts topic, $100 budget, $10-step cost emails | `cfn-lint` clean; tests pass |
| 1.2 | `hosted.yaml`: VPC (ALB, task and data subnets, no NAT), ALB with the bootstrap certificate, ECS Fargate, RDS Postgres 16, EFS, S3, Backup, alarms | `cfn-lint` clean; invariant test passes |
| 1.3 | `deploy.yml` builds or reuses the SHA image, then runs `aws cloudformation deploy`; CI runs `cfn-lint` in place of Terraform | Full test suite passes |
| 1.4 | Remove `deploy/aws/terraform/` and `swap_image.py` | Gone; no Terraform in CI |
| 1.5 | Review by the supervisor, plus a † second review of IAM and the deploy role (supervisor, while Codex and Cursor are disabled) | Both clean |
| 1.6 | Rewrite the runbook (`deploy/aws/README.md`, `docs/HOSTED.md`) for CloudFormation | Supervisor checked it against the templates |

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
- Open: on Nov 12 the second alert address had not yet confirmed its SNS
  subscription. It must click the AWS confirmation email; the link expires
  three days after the stack was created (about Nov 14). If it has expired,
  re-subscribe the address to the `AlertsTopicArn` output with
  `aws sns subscribe --protocol email`. This does not block Phase 3.

| # | Step | Done when |
| --- | --- | --- |
| 2.1 | Deploy the bootstrap stack from a laptop with admin credentials, passing the two alert emails and the OIDC ARN from 0.3 if one exists. While it waits, copy the certificate validation CNAME into Namecheap | Stack `CREATE_COMPLETE`; outputs show the roles, ECR URI, certificate and alerts topic |
| 2.1b | Each alert address clicks the AWS confirmation email | Both subscriptions confirmed |
| 2.2 | In GitHub, create the `hosted` environment with required reviewers, limited to trusted branches/tags | Environment exists |
| 2.3 | Set the `hosted` variables: role ARNs from 2.1, `AWS_REGION=us-east-2`, desired count 2; leave the Firebase variables empty and sign-up `false` | Variables set |

## Phase 3: First deploy (owner, ~30 min, mostly waiting)

**Status: in progress.** A pre-flight check on Nov 12, 2026 found two
prerequisites, 3.0a and 3.0b. 3.0a is done in package 24.5 (deploys run on
pushes to `dev`); 3.0b is the owner's, before approving the first run.

| # | Step | Done when |
| --- | --- | --- |
| 3.0a | Make the deploy workflow runnable without `main`. GitHub only runs a `workflow_dispatch` workflow whose file is on the default branch, so `deploy.yml` also runs on pushes to `dev` that change `src/`, `pyproject.toml`, `deploy/aws/` or the workflow (package 24.5). **Done**: merging 24.5 into `dev` queues the first run | A "Deploy hosted service" run waits for approval in GitHub Actions |
| 3.0b | Create the EFS service-linked role once with admin credentials: `aws iam create-service-linked-role --aws-service-name elasticfilesystem.amazonaws.com`. The account has none yet, and the bootstrap CloudFormation role may create service-linked roles only for ECS, Elastic Load Balancing, RDS and Backup, so the first create would likely fail at the EFS mount targets and roll back | `aws iam get-role --role-name AWSServiceRoleForAmazonElasticFileSystem` succeeds |
| 3.1 | After 3.0b, approve the waiting deploy run (the 24.5 merge into `dev`) in GitHub Actions; `desired_count` defaults to 1 | Workflow green; stack `CREATE_COMPLETE` (RDS takes the longest) |
| 3.2 | In Namecheap, add a CNAME `app` → the `AlbDnsName` output | `dig +short app.agenticml.xyz` shows the load balancer |
| 3.3 | Check the service; optionally delete the unused `HOSTED_DESIRED_COUNT` variable from the `hosted` environment | `curl https://app.agenticml.xyz/healthz` returns 200; one task running; logs clean |

## Phase 4: First accounts and data (owner)

| # | Step | Done when |
| --- | --- | --- |
| 4.1 | Give your own IAM user ECS Exec permissions | `aws ecs execute-command` opens a shell |
| 4.2 | Through ECS Exec, create the server admin account and an invite; set a password through the invite link | You can sign in to the dashboard |
| 4.3 | Create two customer workspaces, each with an admin, a member and a sync key; one account belongs to both with different roles | Both workspaces visible to the right people only |
| 4.4 | From a training machine, sync a run to each workspace | Runs, metrics and artifacts show in the dashboard |

## Phase 5: Launch checks (owner, exit criterion 6)

| # | Step | Done when |
| --- | --- | --- |
| 5.1 | Rolling deploy: re-run the workflow while a sync is uploading | No acknowledged upload lost; sizes and hashes match |
| 5.2 | Isolation: a key, session and URL from one workspace against the other | Every request gets the "not found" answer |
| 5.3 | A 2.2 client syncs to the hosted service | Sync succeeds |
| 5.4 | Real S3 check: stored objects use the configured encryption; dashboard downloads match the source bytes | Recorded |
| 5.5 | Exercise one alarm | Email received |

## Phase 6: Recovery and performance (owner)

| # | Step | Done when |
| --- | --- | --- |
| 6.1 | Restore drill: RDS point-in-time restore, EFS restore from AWS Backup and S3 object versions, all into a separate test service | Recovery age, duration and integrity recorded; drill resources removed |
| 6.2 | Real-EFS benchmark with 1 and 8 clients | Numbers recorded in `docs/SERVER.md` |

At this point the AWS deployment is live and package 24.3 is done.

## After AWS is live (separate work, not in this roadmap)

- Firebase sign-in: real Google and email sign-in, invites, and CSP.
- Terms of service, acceptable use and privacy policies, linked from the
  sign-in page (needs a small code change).
- Open sign-up (exit criterion 7).
- Merge `dev` → `main`, tag `v2.2.0` and `v3.0.0`, publish to PyPI.
- Package 26: retire agenticml-server.

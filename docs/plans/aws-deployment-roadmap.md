# AWS deployment roadmap

Nov 10, 2026 · Supervisor plan · Release 3.0

The steps, in order, to put the 3.0 hosted service live at
`https://app.agenticml.xyz` in our AWS account. Firebase, open sign-up and
retiring agenticml-server come after the service is up and checked.

Decisions (owner, Nov 10, 2026):

- **CloudFormation**, not Terraform. AWS keeps the stack state, so there is
  no state bucket or lock table.
- **One region:** `us-east-2`.
- **Deploys run from a GitHub workflow**: manual dispatch, a protected
  `hosted` environment, and an OIDC role.
- **No NAT gateway.** Tasks run in public subnets with public IPs for
  outbound traffic only; RDS and EFS stay private.
- **No Firebase at first.** People sign in with passwords set through
  invite links.
- **DNS stays at Namecheap** (no Route 53). The owner adds the certificate
  validation CNAME once and a CNAME `app` → the load balancer.
- **Cost alerts:** a $50 monthly budget and an email for every $10 spent,
  with a per-service breakdown, to two addresses (given only when the
  bootstrap stack is deployed, never committed). Ops alarms go to the same
  addresses.

Estimated cost: about **$89/month**. That is ALB ~$20, two Fargate tasks
~$36, RDS `db.t4g.small` ~$25 plus storage, two task IPv4 addresses ~$7,
cost alerts ~$0.60, plus EFS and S3 by usage. Data transfer, backups and
logs are extra. This is above the $50 budget, so the budget alert will fire
each month at this size.

Who does what: **agents** write and check code; only the **owner** runs
anything against AWS (billed resources, public endpoint).

## Phase 0: Account checks (owner, ~30 min)

| # | Step | Done when |
| --- | --- | --- |
| 0.1 | Install AWS CLI v2 and the Session Manager plugin; sign in to the target account in `us-east-2` | `aws sts get-caller-identity` shows the right account |
| 0.2 | Use a non-root admin profile (`agenticml-account`) | `aws sts get-caller-identity` shows the IAM user, not root |
| 0.3 | Check for an existing GitHub OIDC provider (agenticml-server may have one): `aws iam list-open-id-connect-providers` | ARN noted, or "none" |
| 0.4 | Enable Cost Explorer (Billing → Cost Explorer → Launch); the budget itself comes from the bootstrap stack | Cost Explorer opens |

## Phase 1: CloudFormation code (agents)

Package 24.4, replacing the Terraform half of package 24.2. Plan:
[24.4-cloudformation.md](packages/24.4-cloudformation.md).

| # | Step | Done when |
| --- | --- | --- |
| 1.1 | `bootstrap.yaml`: ECR repository, GitHub OIDC provider (optional), deploy role, CloudFormation role, ACM certificate, alerts topic, $50 budget, $10-step cost emails | `cfn-lint` clean; tests pass |
| 1.2 | `hosted.yaml`: VPC (ALB, task and data subnets, no NAT), ALB with the bootstrap certificate, ECS Fargate, RDS Postgres 16, EFS, S3, Backup, alarms | `cfn-lint` clean; invariant test passes |
| 1.3 | `deploy.yml` builds or reuses the SHA image, then runs `aws cloudformation deploy`; CI runs `cfn-lint` in place of Terraform | Full test suite passes |
| 1.4 | Remove `deploy/aws/terraform/` and `swap_image.py` | Gone; no Terraform in CI |
| 1.5 | Review by the supervisor, plus a † second review of IAM and the deploy role (supervisor, while Codex and Cursor are disabled) | Both clean |
| 1.6 | Rewrite the runbook (`deploy/aws/README.md`, `docs/HOSTED.md`) for CloudFormation | Supervisor checked it against the templates |

## Phase 2: Bootstrap (owner, ~15 min)

| # | Step | Done when |
| --- | --- | --- |
| 2.1 | Deploy the bootstrap stack from a laptop with admin credentials, passing the two alert emails and the OIDC ARN from 0.3 if one exists. While it waits, copy the certificate validation CNAME into Namecheap | Stack `CREATE_COMPLETE`; outputs show the roles, ECR URI, certificate and alerts topic |
| 2.1b | Each alert address clicks the AWS confirmation email | Both subscriptions confirmed |
| 2.2 | In GitHub, create the `hosted` environment with required reviewers, limited to trusted branches/tags | Environment exists |
| 2.3 | Set the `hosted` variables: role ARNs from 2.1, `AWS_REGION=us-east-2`, desired count 2; leave the Firebase variables empty and sign-up `false` | Variables set |

## Phase 3: First deploy (owner, ~30 min, mostly waiting)

| # | Step | Done when |
| --- | --- | --- |
| 3.1 | Run the deploy workflow with a reviewed commit SHA and approve it | Workflow green; stack `CREATE_COMPLETE` (RDS takes the longest) |
| 3.2 | In Namecheap, add a CNAME `app` → the `AlbDnsName` output | `dig +short app.agenticml.xyz` shows the load balancer |
| 3.3 | Check the service | `curl https://app.agenticml.xyz/healthz` returns 200; two tasks running; logs clean |

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

# Operating the Hosted Service

This document describes the operational architecture, deployment procedures, customer workspace management, and incident response for the AgenticML hosted service (`app.agenticml.xyz`).

Detailed step-by-step commands are maintained in the operator runbook at [deploy/aws/README.md](../deploy/aws/README.md).

## Architecture

The hosted service runs the standard `agenticml-lite` server image across multiple AWS managed services in `us-east-2`:

```
                       Internet
                          │
                          ▼
            Application Load Balancer (ALB)
                 [ACM TLS Certificate]
                          │
                          ▼
                 ECS Fargate Task
                 (agenticml server)
              (one by default; ARM64)
                          │
      ┌───────────────────┼───────────────────┐
      │                   ▼                   │
      │  Amazon RDS PostgreSQL (Single-AZ)    │
      │  - Accounts, workspaces, keys,        │
      │    sessions, runs, metrics summaries  │
      │                                       │
      │  Amazon EFS Filesystem                │
      │  - Run files (meta, events, scalars)  │
      │  - Lineage shadow Git repositories   │
      │                                       │
      │  Amazon S3 Bucket                     │
      │  - Content-addressed blobs (code,     │
      │    media, copied artifacts)           │
      └───────────────────────────────────────┘
```

- **Application Load Balancer (ALB)**: Public dualstack endpoint with AWS Certificate Manager (ACM) TLS termination for `app.agenticml.xyz` using the certificate from the bootstrap stack. DNS is configured as a CNAME at Namecheap. Routes traffic to the ECS task with a 120-second deregistration delay.
- **ECS Fargate Tasks**: Containerized server running `agenticml server serve` as a non-root user (uid/gid 10001). The service runs one task by default (0.5 vCPU, 1 GB, ARM64/Graviton; Container Insights is off); a second can be added for capacity. Tasks run in public task subnets with a public IPv4 address for outbound traffic only (no NAT gateway) and accept incoming traffic only from the ALB security group. Tasks have a 120-second stop timeout to allow active uploads to finish during deployments.
- **Amazon RDS PostgreSQL**: Managed PostgreSQL 16 (`db.t4g.micro`, 20 GB gp3) in private data subnets with no internet route, storing accounts, workspaces, team memberships, API keys, sessions, audit events, committed run file sizes, and metric summaries. Single-AZ by default (Multi-AZ is optional via `HOSTED_MULTI_AZ`). Passwords are managed via AWS Secrets Manager and injected into ECS task environments at runtime.
- **Amazon EFS**: General Purpose encrypted filesystem in private data subnets with no internet route, mounted at `/data` via an EFS Access Point matching uid/gid 10001. Holds raw experiment files and lineage shadow Git repositories. Protected by daily AWS Backup with 35-day retention.
- **Amazon S3**: Encrypted, versioned private bucket storing content-addressed objects under `{workspace_id}/objects/{hh}/{sha256}`. Public access is blocked at the bucket and account levels. Protected by object versioning (noncurrent versions kept 30 days). AWS Backup covers EFS and RDS only.

## Deployments and updates

Deploys are automated through GitHub Actions ([.github/workflows/deploy.yml](../.github/workflows/deploy.yml)) using AWS IAM OpenID Connect (OIDC) roles with required reviewer approvals:

1. **Image build and push**: The Dockerfile at [deploy/aws/Dockerfile](../deploy/aws/Dockerfile) builds an image containing `agenticml-lite[hosted]` pinned to the target commit. The workflow builds it for `linux/arm64` under QEMU and pushes it to private Amazon ECR with the immutable tag `<commit sha>-arm64`.
2. **Rolling CloudFormation deployment**: The deploy workflow deploys the hosted CloudFormation stack with the new image tag. CloudFormation registers the task definition revision and triggers a rolling service update (`minimumHealthyPercent=100`, `maximumPercent=200`, so the new task starts before the old one stops and a single task deploys without downtime); the ECS deployment circuit breaker automatically rolls back if a bad image fails to start.
3. **Database migrations**: Migrations run automatically during server process startup under PostgreSQL advisory migration locks (`pg_advisory_xact_lock`), safely coordinating concurrent starts.
4. **Initial deploy and config updates**: The first deploy creates the entire hosted CloudFormation stack in one go (no `desired_count=0` step). Environment configuration changes (Multi-AZ, Firebase settings, open sign-up, or admin emails) are made by updating GitHub environment variables and re-running the deploy workflow. The task count is not a variable: it comes from the workflow's `desired_count` input (`1`, `0` or `2`; default `1`; push runs keep the current count).
5. **How deploys start**: The workflow runs automatically on every push to `dev` that changes `src/**`, `pyproject.toml`, `deploy/aws/**` (except Markdown and `bootstrap.yaml`) or `.github/workflows/deploy.yml`. Each run waits for the required reviewer on the `hosted` environment; the reviewer reads the commit first and rejects an older waiting run when a newer one is queued. Manual dispatch (`gh workflow run deploy.yml`) works only once `deploy.yml` is on `main`. Until then, roll back by reverting on `dev`.

For deployment commands and runbook procedures, see [deploy/aws/README.md](../deploy/aws/README.md#first-deploy).

## First server administrator

With Firebase on, `/setup` returns 404; the first server administrator is provisioned via the `HOSTED_ADMIN_EMAILS` GitHub environment variable (and `AGENTICML_SERVER_ADMIN_EMAILS` server setting). Set `HOSTED_ADMIN_EMAILS` to your Google email, deploy, sign in at `https://app.agenticml.xyz/login` with "Continue with Google", and open `/admin`. A server admin without workspace memberships lands on `/workspaces` with an **Admin** button and sees no customer workspace data.

The `/setup` endpoint only exists with Firebase off. When the server starts without an administrator and Firebase is disabled, it logs a one-time `/setup` link. Find the newest setup link in CloudWatch:

```sh
aws logs filter-log-events \
  --log-group-name /ecs/agenticml-hosted \
  --filter-pattern '"No admin user yet"' \
  --region us-east-2 \
  --profile agenticml-account \
  --query 'events[].message' \
  --output text
```

Events are listed oldest first; use the newest (last) line. A fresh single-use token is logged at every task start until an admin exists.

Open the printed URL (`https://app.agenticml.xyz/setup?token=...`) in a browser:
- The link works only on `https://app.agenticml.xyz` (session cookies are `__Host-` and Secure; the Origin check requires the public URL, so the raw ALB hostname will not work).
- Enter a login name that does not contain `@` (default `admin`).
- Choose a password of at least 12 characters.
- Submitting makes the account the owner of the `default` workspace with `server_admin=1`, logs you in, and closes `/setup` permanently (subsequent visits return 404).

Server administration is available at `https://app.agenticml.xyz/admin/`.

### Break-glass CLI alternative

If `/setup` is unreachable (with Firebase off) or if granting server admin via ECS Exec is needed with Firebase on, open an interactive ECS Exec shell (see [deploy/aws/README.md](../deploy/aws/README.md#connecting-via-ecs-exec)):

```sh
# With Firebase on (linking on first verified sign-in):
agenticml server accounts add you@example.com --server-admin --data /data

# With Firebase off:
agenticml server users add admin2 --role admin --workspace default \
  --public-url https://app.agenticml.xyz --data /data
```

While `default` has no owner yet, `users add admin2` makes `admin2` its owner with `server_admin=1` and prints a one-time invite link to set the password (≥ 12 characters). `agenticml server accounts list` shows a `source` column (`list`, `manual`, or `-`). The last server admin who can sign in cannot be removed.

## Customer workspaces

Customers interact with the hosted service as isolated workspaces on `app.agenticml.xyz`.

### Manual workspace creation

The standard path is self-serve administration from `/admin` and the workspace dashboard:

1. **Create workspace**: On `/admin`, click **New workspace** and enter the slug, name, and owner email (e.g. `customer@example.com`). If no account exists for that email yet, a credential-less account is created.
2. **Owner sign-in**: The owner signs in with a verified Firebase sign-in (Google or verified email/password using that email). The first verified sign-in links the account and places them in the workspace as owner.
3. **Invite team members**: The workspace owner invites team members from the workspace **Team** page. An invite generates a one-time link (valid 72 hours). The invitee signs in with Google or email/password using that exact email, which must be verified. Inviting someone who already has an account adds the membership directly without generating an invite link.
4. **Issue sync keys**: A workspace admin creates sync API keys on the workspace **Keys** page for any member (the person must already be a workspace member).
5. **Multi-workspace accounts**: With Firebase on, there are no passwords to set; adding an account to additional workspaces attaches the membership directly. (The order rule requiring a user to set their password before being added to a second workspace applies to password sign-in only.)

### Break-glass CLI workspace creation

Workspaces can also be provisioned over ECS Exec using the server CLI:

1. Create the customer account:
   ```bash
   agenticml server accounts add customer@example.com --data /data
   ```
2. Create the workspace assigned to that owner:
   ```bash
   agenticml server workspaces create acme-corp --name "Acme Corp" --owner customer@example.com --data /data
   ```
3. Generate an invite link for the owner:
   ```bash
   agenticml server users invite customer@example.com --workspace acme-corp --data /data
   ```

### Self-serve sign-up

When configured with `--open-signup` (`AGENTICML_SERVER_OPEN_SIGNUP=true`), visitors signing in with a verified Firebase account (Google OAuth or verified email/password) are automatically provisioned:
- A global account record is created.
- A personal workspace (`<name>'s workspace`) is created on the `free` plan.
- The user is assigned as the workspace `owner`.

Unverified email accounts can sign in but cannot create workspaces or accept invites until their email address is verified.

## Plans and usage limits

Workspaces operate under plan limits:

| Resource | Free Plan | Custom Plan |
| --- | --- | --- |
| Storage (committed files + objects) | 5 GB | Configurable (`--storage`) |
| Upload traffic per UTC day | 2 GB | Configurable (`--uploads-per-day`) |
| Request rate | 600 req/min | Configurable (`--requests-per-minute`) |
| People (members + pending invites) | 5 | Configurable (`--people`) |
| Free workspaces per account | 1 | N/A |

### Inspecting usage

Server administrators inspect usage across all workspaces:

```bash
agenticml server workspaces usage --data /data
```

### Upgrading a customer plan

To allocate additional resources to a paying customer, set their plan to `custom`:

```bash
agenticml server workspaces set-plan acme-corp custom \
  --storage 100GB \
  --uploads-per-day 20GB \
  --requests-per-minute 1200 \
  --people 25 \
  --data /data
```

## Suspending and deleting workspaces

### Suspending a workspace

If an account violates terms of service or payment lapses, suspend the workspace. On `/admin`, click **Suspend** on its row, or over ECS Exec:

```bash
agenticml server workspaces suspend acme-corp --data /data
```

Suspended workspaces immediately block all API requests, dashboard access, and sync uploads. The server returns HTTP `404 Not Found` for all workspace endpoints, preventing disclosure of the workspace's existence.

To resume service:

```bash
agenticml server workspaces resume acme-corp --data /data
```

### Archiving a workspace

On `/admin`, **Archive** suspends a workspace (not `default`) and moves it to the Archived table. Its data is kept, nobody can open or sync to it, and it no longer counts toward its owner's free workspace limit. **Restore** brings it back as suspended; click **Resume** to reopen it. Restore is refused if the owner already has their free workspace again.

### Deleting a workspace

Deleting a workspace removes all database records, deletes on-disk files on EFS, and purges all objects under that workspace's prefix in S3. It cannot be undone, so it is a server CLI step only; the `default` workspace can never be deleted. The CLI asks for the slug:

```bash
agenticml server workspaces delete acme-corp --yes-delete acme-corp --data /data
```

All workspace deletions write a permanent entry to the server audit log.

## Customer support access

Server administrators do **not** have ambient access to customer data. A server administrator navigating to `/w/<customer-slug>/` receives HTTP `404 Not Found` unless they are a member of that workspace.

To grant support access:
1. The customer must explicitly invite the support account (e.g. `support@agenticml.xyz`) as a `member` or `admin` through the workspace Team settings page.
2. The invitation acceptance and all subsequent actions performed by support personnel are recorded in the workspace audit log (`/w/<slug>/team` and `server audit`).
3. Once support is complete, the customer removes the support account from their workspace members list.

## Backup and disaster recovery

The hosted service relies on managed AWS backup mechanisms:
- **RDS PostgreSQL**: Automated snapshots with 7-day Point-in-Time Recovery (PITR) and daily AWS Backup copies retained for 35 days.
- **EFS filesystem**: Daily AWS Backup copies retained for 35 days.
- **S3 objects**: Protected by S3 bucket versioning (noncurrent versions kept 30 days). AWS Backup covers EFS and RDS only.

Periodic restore drills must be performed in an isolated test environment as described in [deploy/aws/README.md](../deploy/aws/README.md#restore-drill-and-real-efs-benchmark).

## Monitoring and incident response

### Alarms

CloudWatch alarms monitor core service health and deliver notifications to the `agenticml-alerts` SNS topic:
- **ALB 5xx error rate**: Alerts when 5xx response count exceeds 5 within a 5-minute window (`agenticml-hosted-alb_5xx`).
- **Unhealthy target host**: Alerts if any target host remains unhealthy for 5 consecutive minutes based on ALB health checks on `/healthz` (`agenticml-hosted-unhealthy`). There is no synthetic monitor.
- **p95 target latency**: Alerts when target response time p95 exceeds 5 seconds over 5 minutes (`agenticml-hosted-latency-p95`).
- **RDS free storage space**: Alerts when free storage falls below 2 GB (`agenticml-hosted-rds_storage`).
- **RDS CPU utilization**: Alerts when database CPU utilization exceeds 80% over 5 minutes (`agenticml-hosted-rds_cpu`).
- **Healthy target count**: Alerts when the ALB `HealthyHostCount` minimum is below 1 for 5 consecutive 60-second periods, treating missing data as breaching (`agenticml-hosted-healthy_hosts`; active only when desired task count is greater than zero).
- **Cost and budget alerts**: The daily cost-step function (`agenticml-cost-steps`) emails spend summaries each time month-to-date spend passes another $10 milestone against a $100 monthly budget (with an error alarm on function execution failures), while an AWS Budget alert monitors actual and forecasted monthly spend exceeding 100%.

### Emergency procedures

To immediately disable public self-serve sign-up, set `HOSTED_OPEN_SIGNUP=false` in the GitHub `hosted` environment variables and re-run the deploy workflow as documented in [deploy/aws/README.md](../deploy/aws/README.md#upgrades-rollback-and-config).

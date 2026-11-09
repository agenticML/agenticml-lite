# Operating the Hosted Service

This document describes the operational architecture, deployment procedures, customer workspace management, and incident response for the AgenticML hosted service (`app.agenticml.xyz`).

Detailed step-by-step commands and Terraform instructions are maintained in the operator runbook at [deploy/aws/README.md](../deploy/aws/README.md).

## Architecture

The hosted service runs the standard `agenticml-lite` server image across multiple AWS managed services in `us-east-2`:

```
                       Internet
                          │
                          ▼
            Application Load Balancer (ALB)
                 [ACM TLS Certificate]
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
      ECS Fargate Task 1      ECS Fargate Task 2
      (agenticml server)      (agenticml server)
              │                       │
      ┌───────┼───────────────────────┼───────┐
      │       ▼                       ▼       │
      │  Amazon RDS PostgreSQL (Multi-AZ)     │
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

- **Application Load Balancer (ALB)**: Public dualstack endpoint with AWS Certificate Manager (ACM) TLS termination for `app.agenticml.xyz`. Routes traffic to ECS tasks in private subnets with a 120-second deregistration delay.
- **ECS Fargate Tasks**: Containerized server instances running `agenticml server serve` as a non-root user (uid/gid 10001). Tasks run across multiple Availability Zones with a 120-second stop timeout to allow active uploads to finish during deployments.
- **Amazon RDS PostgreSQL**: Managed PostgreSQL database storing accounts, workspaces, team memberships, API keys, sessions, audit events, committed run file sizes, and metric summaries. Passwords are managed via AWS Secrets Manager and injected into ECS task environments at runtime.
- **Amazon EFS**: General Purpose encrypted filesystem mounted at `/data` via an EFS Access Point matching uid/gid 10001. Holds raw experiment files and lineage shadow Git repositories. Protected by daily AWS Backup with 35-day retention.
- **Amazon S3**: Encrypted, versioned private bucket storing content-addressed objects under `{workspace_id}/objects/{hh}/{sha256}`. Public access is blocked at the bucket and account levels. Protected by object versioning and AWS Backup.

## Deployments and updates

Deploys are automated through GitHub Actions ([.github/workflows/deploy.yml](../.github/workflows/deploy.yml)) using AWS IAM OpenID Connect (OIDC) roles with required reviewer approvals:

1. **Image build and push**: The Dockerfile at [deploy/aws/Dockerfile](../deploy/aws/Dockerfile) builds an image containing `agenticml-lite[hosted]` pinned to the target commit. Images are pushed to private Amazon ECR with immutable Git SHA tags.
2. **Rolling ECS deployment**: The deploy workflow registers a new task definition revision updating only the container image, then triggers a rolling update (`minimumHealthyPercent=100`, `maximumPercent=200`).
3. **Database migrations**: Migrations run automatically during server process startup under PostgreSQL advisory migration locks (`pg_advisory_xact_lock`), safely coordinating concurrent starts.
4. **Initial bootstrap**: The initial Terraform stack applies with `desired_count=0`. Once the first container image is registered, the service is scaled to 2 tasks.

For manual deployment commands and image building steps, see [deploy/aws/README.md](../deploy/aws/README.md#first-image-and-first-deploy).

## First server administrator

The first server administrator account must be provisioned via ECS Exec on a running server task before enabling public sign-ups:

```bash
# Execute within a running ECS task (via deploy/aws/README.md commands):
agenticml server accounts add admin@example.com --server-admin --data /data
```

Once provisioned, the administrator can generate an invite or sign in using Firebase Authentication.

## Customer workspaces

Customers interact with the hosted service as isolated workspaces on `app.agenticml.xyz`.

### Manual workspace creation

To provision a customer workspace manually:
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

If an account violates terms of service or payment lapses, suspend the workspace:

```bash
agenticml server workspaces suspend acme-corp --data /data
```

Suspended workspaces immediately block all API requests, dashboard access, and sync uploads. The server returns HTTP `404 Not Found` for all workspace endpoints, preventing disclosure of the workspace's existence.

To resume service:

```bash
agenticml server workspaces resume acme-corp --data /data
```

### Deleting a workspace

Deleting a workspace removes all database records, deletes on-disk files on EFS, and purges all objects under that workspace's prefix in S3. It requires typing the workspace slug to confirm:

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
- **S3 objects**: S3 bucket versioning protects against accidental overwrites or deletions. Versioned objects are covered by AWS Backup.

Periodic restore drills must be performed in an isolated test environment as described in [deploy/aws/README.md](../deploy/aws/README.md#restore-drill-owner-isolated-test-service-only).

## Monitoring and incident response

### Alarms

CloudWatch alarms monitor core service health:
- **Health check failure**: Synthetic monitoring on `/healthz` triggers when tasks fail health checks.
- **ALB 5xx rate**: Alerts on elevated 500/502/503/504 response counts.
- **RDS storage and CPU**: Alerts when RDS free storage drops below threshold or CPU utilization spikes.

### Emergency procedures

To immediately disable public self-serve sign-up without rebuilding container images, update the ECS task definition environment variables as documented in [deploy/aws/README.md](../deploy/aws/README.md#upgrades-rollback-and-config).

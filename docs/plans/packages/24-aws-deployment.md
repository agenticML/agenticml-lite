# Package 24: AWS hosted deployment

Nov 9, 2026 · Supervisor plan · Release 3.0 · Coder: Codex; owner runs it · Size: M

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), package summary 24,
"Estimated hosted cost", decisions D1 (hostname), D2 (free limits), D6
(`deploy/aws/`, private ECR, no published image), D7 (`[hosted]` extra)
and exit criterion 6. Pattern: agenticml-server's
`.github/workflows/deploy-backend-ecs.yml` (OIDC role, immutable ECR tag,
register task definition, roll, wait for stable).

## Goal

Everything needed to run the hosted service in our AWS account is in
`deploy/aws/`, reviewed and statically checked: a container image, the
Terraform for the whole stack, a deploy workflow and a runbook. The owner
runs `terraform apply`, the first deploy, the real-EFS benchmark and the
exit-criterion-6 checks; those steps are not done by agents (they create
billed resources and expose a public service).

Split:

- **24.1** Dockerfile, entrypoint, limits file, local image smoke test.
- **24.2** Terraform, deploy workflow, CI checks, runbook.
- **24.3 (owner)** apply, first deploy, real-EFS benchmark, restore
  drill, exit criterion 6 → roadmap Backlog entries until done.

## 24.1 Image (`deploy/aws/Dockerfile`, `deploy/aws/entrypoint.sh`, `deploy/aws/limits.json`, `scripts/hosted_smoke.sh`)

- Multi-stage build from the repository root context (so unreleased code
  can be deployed): stage 1 `python:3.12-slim-bookworm` (exact patch tag,
  e.g. `3.12.7-slim-bookworm`; note in a comment how to update it) builds
  the wheel with `python -m build --wheel`; stage 2 (same base) installs
  `git` and `ca-certificates` from apt (`--no-install-recommends`, lists
  removed), then `pip install --no-cache-dir "<wheel>[hosted]"` with the
  hosted dependencies pinned to exact versions in
  `deploy/aws/requirements-hosted.txt` (psycopg[binary], boto3, PyJWT
  [crypto] and their direct pins; `pip install -c`).
- Non-root user `agenticml` uid/gid 10001 (matches the EFS access point);
  `/data` owned by it; `WORKDIR /data`.
- `HEALTHCHECK` with python's `urllib` against
  `http://127.0.0.1:8080/healthz` (no curl in the image).
- `ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]`: `exec agenticml server
  serve --data /data --host 0.0.0.0 --port 8080` with everything else from
  environment variables the server already reads
  (`AGENTICML_SERVER_DATABASE_URL`, `AGENTICML_SERVER_OBJECTS`,
  `AGENTICML_SERVER_PUBLIC_URL`, `AGENTICML_SERVER_LIMITS=/etc/agenticml/limits.json`,
  `AGENTICML_SERVER_OPEN_SIGNUP`, Firebase variables). Trusted proxy:
  `AGENTICML_SERVER_TRUSTED_PROXY` (comma-separated CIDRs) — if the
  server has no env var for `--trusted-proxy`, the entrypoint turns this
  variable into repeated `--trusted-proxy` flags (validate each as a
  CIDR; report it as a deviation if you add an env var to the server
  instead).
- Database password: the task gets the RDS-managed master secret's
  `password` field as `PGPASSWORD`; `AGENTICML_SERVER_DATABASE_URL` has no
  password (`postgresql://agenticml@<host>:5432/agenticml?sslmode=verify-full&sslrootcert=/etc/agenticml/rds-ca.pem`).
  Verify that `db/postgres.py` passes the URL to psycopg so libpq reads
  `PGPASSWORD`; fix if not. Bundle the RDS global CA bundle: the
  Dockerfile downloads `https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`
  at build time and checks it against a SHA-256 pinned in the Dockerfile
  (the coder records the current digest; if no network, leave a clearly
  marked placeholder and report it).
- `deploy/aws/limits.json`: the D2 free plan (5 GB storage, 5 people, 600
  requests/minute, 2 GB uploads/day, 1 free workspace per account) in the
  format `--limits` reads (copy the shape from `server/limits.py` and its
  tests).
- `.dockerignore` at the repo root keeping the context small (no `.git`,
  venvs, `testbed/`, `website/`, `node_modules`, caches).
- `scripts/hosted_smoke.sh`: builds the image, starts the local
  Postgres (`$AGENTICML_TEST_DATABASE_URL`) and a moto server
  (`python -m moto.server -p <port>`) — or MinIO if moto server mode is
  unavailable — runs the container with `AGENTICML_SERVER_OBJECTS=s3://…`
  and `AGENTICML_SERVER_S3_ENDPOINT_URL`, waits for `/healthz`, creates a
  workspace, admin account and sync key with `docker exec … agenticml
  server …`, syncs a small run from the host with `agenticml sync`,
  fetches it through the dashboard API, sends SIGTERM and checks the
  container exits within 30 s. Not a CI job (needs Docker and Postgres);
  the supervisor runs it locally.

## 24.2 Terraform, workflow, runbook

`deploy/aws/terraform/` (Terraform ≥ 1.6, AWS provider ~> 5.x, pinned in
`versions.tf`; `backend "s3" {}` with a partial config the owner passes at
`init`, documented in the runbook). Region variable default `us-east-2`.
Files by concern (`network.tf`, `alb.tf`, `ecs.tf`, `rds.tf`, `s3.tf`,
`efs.tf`, `secrets.tf`, `iam.tf`, `monitoring.tf`, `dns.tf`,
`variables.tf`, `outputs.tf`). Every resource tagged
`Project=agenticml-hosted`.

- **Network:** VPC (/16), two AZs, public and private subnets, one NAT
  gateway, S3 gateway endpoint. Security groups: ALB 443 (and 80 →
  redirect) from the internet; tasks 8080 only from the ALB SG; RDS 5432
  only from the task SG; EFS 2049 only from the task SG.
- **DNS/TLS:** look up the existing Route 53 zone `agenticml.xyz` (data
  source; variable for the zone name), ACM certificate for
  `app.agenticml.xyz` (variable `hostname`) with DNS validation, A/AAAA
  alias to the ALB.
- **ALB:** HTTPS listener with `ELBSecurityPolicy-TLS13-1-2-2021-06`,
  HTTP → HTTPS redirect, target group (IP targets, port 8080, health check
  `/healthz`, matcher 200, deregistration delay 120 s), idle timeout 300 s
  (large uploads), `drop_invalid_header_fields = true`, access logs to a
  log bucket (encrypted, 90-day expiry).
- **ECS:** cluster with Container Insights; Fargate service (desired 2,
  `deployment_minimum_healthy_percent = 100`, `maximum_percent = 200`,
  circuit breaker with rollback), task 0.5 vCPU / 1 GB, X86_64, stop
  timeout 120 s, `enable_execute_command = true` (the runbook's operator
  commands use ECS Exec), EFS volume via the access point at `/data`
  (transit encryption on, IAM auth), awslogs to a log group (30-day
  retention). The task definition in Terraform is the initial revision;
  the workflow registers later ones, so `lifecycle { ignore_changes =
  [task_definition] }` on the service.
- **RDS:** Postgres 16, `db.t4g.small`, single-AZ (variable for Multi-AZ),
  gp3 20 GB with autoscaling to 100 GB, encrypted, `manage_master_user_password
  = true` (password never in Terraform state), backups 7 days, deletion
  protection on, `rds.force_ssl = 1` parameter group, performance insights
  off (cost), final snapshot on destroy.
- **S3 objects bucket:** SSE-S3 default encryption, public access block
  (all four), `BucketOwnerEnforced`, versioning on, lifecycle: noncurrent
  versions expire after 30 days, abort incomplete multipart after 7 days;
  bucket policy denying non-TLS requests. The task role may
  `s3:GetObject`, `PutObject`, `DeleteObject`, `ListBucket` (with a prefix
  condition) on this bucket only, plus `HeadBucket`.
- **EFS:** encrypted, `generalPurpose`, elastic throughput, access point
  uid/gid 10001 at `/agenticml` with 0700 creation; file system policy
  requiring TLS and the access point; AWS Backup plan (daily, 35-day
  retention) for EFS and RDS (RDS also has its own automated backups).
- **Secrets / config:** task env from variables (`public_url`,
  `firebase_project`, `firebase_api_key` — the web API key is public by
  design — `open_signup`, objects URL), `PGPASSWORD` from the RDS-managed
  secret via `secrets` with `valueFrom = "<arn>:password::"`. The task
  execution role may read only that secret.
- **IAM:** task execution role (ECR pull, logs, that secret), task role
  (S3 bucket, EFS client mount/write via the access point, SSM messages
  for ECS Exec). GitHub OIDC provider (variable to reuse an existing one)
  and a deploy role trusted only for `repo:agenticML/agenticml-lite` on
  the `environment:hosted` subject, allowed to push to the one ECR repo,
  register task definitions, `iam:PassRole` on the two roles, and update /
  describe that one service.
- **ECR:** repository `agenticml/lite-server`, `IMMUTABLE` tags, scan on
  push, lifecycle keeping the last 30 images.
- **Monitoring:** SNS topic with an email subscription variable; alarms on
  ALB 5xx (> 5 in 5 min), unhealthy host count ≥ 1 for 5 min, target
  response time p95, RDS free storage < 2 GB, RDS CPU > 80%, ECS running
  task count < 2.
- `outputs.tf`: ALB DNS name, ECR repo URL, cluster/service names, deploy
  role ARN, log group, bucket, EFS id — no secrets.
- `terraform.tfvars.example` with every variable and a comment.

`.github/workflows/deploy.yml` (manual `workflow_dispatch` only, plus
optional `ref` input; GitHub environment `hosted` with required
reviewers): copies agenticml-server's steps — OIDC role from
`vars.AWS_DEPLOY_ROLE_ARN`, ECR login, build with
`docker build -f deploy/aws/Dockerfile .` tagged with the commit SHA (reuse
if the immutable tag exists), register a new task definition revision
from the current one with only the image swapped (a small
`deploy/aws/swap_image.py`, standard library), `update-service`, `aws ecs
wait services-stable`. `concurrency` group so two deploys never overlap.
Every action pinned to a full commit SHA with a version comment.

`.github/workflows/test.yml` gains a `deploy-check` job (runs on PRs that
touch `deploy/aws/**`, `.github/workflows/deploy.yml` or `pyproject.toml`):
`hashicorp/setup-terraform` (pinned) → `terraform fmt -check -recursive`
and `terraform init -backend=false && terraform validate`; `docker build
-f deploy/aws/Dockerfile .`; run the image's
`agenticml --version` and `python -c "import psycopg, boto3, jwt"`.

`deploy/aws/README.md` is the runbook: prerequisites (Terraform version,
AWS CLI, an S3 state bucket and DynamoDB/lockfile, the Route 53 zone),
first apply, first image push and deploy, the first server admin
(`aws ecs execute-command … agenticml server accounts …`; copy the exact
CLI from `cli.py`), Firebase settings (authorized domain), adding a
customer workspace by hand, upgrades (deploy workflow; migrations run at
start under the migrate lock), rollback (redeploy the previous SHA;
migrations are forward-only, so say when a rollback is not safe), restore
drills (RDS point-in-time restore to a new instance + EFS restore from
AWS Backup, then point a test service at them; S3 versioning), the
real-EFS benchmark (`scripts/bench_server.py` from a one-off task on the
cluster, 1 and 8 clients; record results in `docs/SERVER.md`), incident
basics (logs, alarms, scaling, suspending a workspace, turning
`open_signup` off without a deploy by updating the env and rolling), the
cost table from the 3.0 plan, and teardown (deletion protection, final
snapshot). Prose is drafted by the coder from the facts in this plan;
the supervisor checks every command against the code.

## Constraints

- No AWS calls, no `terraform apply`, no image push from agents. Static
  checks and the local smoke test only.
- No secrets in the repository, the image, Terraform state (RDS-managed
  password) or logs. The database URL is never logged by the server
  (already true; keep it).
- No published image (D6): ECR only, private.
- The base `pip install agenticml-lite` and self-hosted docs do not
  change; `deploy/systemd/` stays.

## Edge cases

- Rolling deploy with an upload in flight: the old task gets SIGTERM, the
  server drains (up to its built-in drain time, < 120 s stop timeout),
  the ALB deregistration delay covers it; clients retry on 503/connection
  errors (2.2+ client behaviour).
- Two tasks start at once on a fresh database: migrations and the 2.x
  import are serialised by the advisory locks (packages 19, 20); the
  metrics backfill too (package 22).
- EFS permission mismatch: access point forces uid/gid 10001, matching
  the image user.
- `server.lock` is not used on Postgres (package 20), so two tasks on
  one EFS directory are allowed.

## Tests and checks

- 24.1: `scripts/hosted_smoke.sh` passes locally (supervisor runs it);
  `testbed/.venv/bin/python -m pytest -q tests` still passes.
- 24.2: the `deploy-check` CI job passes on the PR (terraform fmt +
  validate, docker build, import check); `python -m py_compile
  deploy/aws/swap_image.py`, plus a small unit test
  `tests/test_deploy_swap_image.py` for it (only the image changes; other
  fields kept; registration-only fields such as `taskDefinitionArn`,
  `revision`, `status`, `requiresAttributes`, `compatibilities`,
  `registeredAt`, `registeredBy` dropped).
- Sonnet reviews the Terraform for least privilege, public exposure and
  encryption settings.

## Done when

24.1 and 24.2 are merged with passing checks, and the owner steps (apply,
first deploy, two customer workspaces, a rolling deploy with no lost
uploads, a restore drill, the real-EFS benchmark, exit criterion 6) are
Backlog entries with exact steps. The package row says `done (code;
owner checks in Backlog)` until the owner completes them.

# Hosted AWS service (operator runbook)

This stack is for our private hosted service. Customers continue to install
`agenticml-lite` from PyPI. Only the owner runs the AWS commands below. They
create billed resources and a public hostname. Package 24.3 remains open until
all launch checks below have recorded evidence.

## Prerequisites and configuration

Use Terraform >=1.6, <2 (CI uses 1.6.6), AWS CLI v2 with Session Manager plugin,
Docker with an x86_64 builder, and GitHub CLI. Authenticate to the intended AWS
account in us-east-2. The existing public Route 53 zone must delegate
`agenticml.xyz`. Confirm the SNS subscription when its email arrives.

Create an encrypted, versioned, private S3 state bucket separately, and a
DynamoDB state lock table (string partition key `LockID`). These must outlive
the service. Do not put credentials in backend files or tfvars. Terraform 1.6
uses DynamoDB locking; if upgrading to a version supporting native S3 lockfiles,
coordinate that migration across all operators before replacing DynamoDB.

```sh
cd deploy/aws/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit every REPLACE_ME; choose globally unique bucket names and final snapshot ID.
# Keep desired_count=0 and open_signup=false for the bootstrap apply.
terraform init \
  -backend-config='bucket=YOUR-STATE-BUCKET' \
  -backend-config='key=agenticml-hosted/terraform.tfstate' \
  -backend-config='region=us-east-2' \
  -backend-config='encrypt=true' \
  -backend-config='dynamodb_table=YOUR-STATE-LOCK-TABLE'
terraform fmt -check -recursive
terraform validate
terraform plan -out=/tmp/agenticml-hosted.tfplan
terraform apply /tmp/agenticml-hosted.tfplan
rm /tmp/agenticml-hosted.tfplan
terraform output
```

Ignore local `terraform.tfvars`, `.terraform/`, state, and plan files; never
commit them. The RDS-managed password is referenced by secret ARN and is not
read by Terraform. ECS injects only its `password` field as `PGPASSWORD`;
the URL has `sslmode=verify-full` and the pinned RDS CA bundle. Rotating the
managed secret requires a new task deployment to refresh the injected value.
The image runs uid/gid 10001, matching the EFS access point. Outbound task
traffic uses one NAT; this is a cost-conscious single-AZ egress dependency.
ALB is dualstack; private tasks and dependencies use IPv4.

## First image and first deploy

Before dispatch, create the GitHub environment `hosted`, **require reviewers**,
and restrict its permitted deployment branches/tags to trusted release refs.
YAML cannot configure required reviewers. An optional workflow `ref` can select
code; reviewers must verify that commit before granting the OIDC role. Set
repository/environment variables `AWS_DEPLOY_ROLE_ARN` from Terraform's
`deploy_role_arn` and `AWS_REGION=us-east-2`. The role trusts only
`repo:agenticML/agenticml-lite:environment:hosted`.

The workflow actions use major version tags, like the repository's other
workflows. The ECR repo is private, immutable, scans pushes, and retains 30 images. Preserve known-good
release images separately before lifecycle expiry if a long rollback window
is needed.

From the repository root, the owner can push the first image explicitly:

```sh
export AWS_REGION=us-east-2
sha=$(git rev-parse HEAD)
registry="$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${AWS_REGION}.amazonaws.com"
aws ecr get-login-password | docker login --username AWS --password-stdin "$registry"
docker build -f deploy/aws/Dockerfile -t "$registry/agenticml/lite-server:$sha" .
docker push "$registry/agenticml/lite-server:$sha"
gh workflow run deploy.yml -f ref="$sha"
# Approve hosted environment after reviewing the selected SHA; wait for success.
```

The workflow reuses an existing immutable SHA tag, changes only the `server`
container image in the current task definition, registers a revision, updates
the service, waits for stability, and detects circuit-breaker rollback. Its
concurrency group serializes deploys. With bootstrap desired count zero it
registers the real image before tasks start. Then change desired_count to 2 in
tfvars and apply a reviewed Terraform plan. Terraform ignores the service's
workflow-managed task_definition; it does not revert the image revision.

```sh
aws ecs wait services-stable --cluster agenticml-hosted --services agenticml-hosted
curl --fail https://app.agenticml.xyz/healthz
aws logs tail /ecs/agenticml-hosted --since 10m
```

## Accounts, Firebase and customers

In Firebase, enable Google and email/password sign-in and add
`app.agenticml.xyz` to Authentication's authorized domains. Configure the project
ID and its public web API key in tfvars. Verify actual Google and verified
email/password sign-in, invite acceptance, and browser CSP before enabling
open signup. The baked limits are decimal 5 GB storage, 2 GB uploads/day,
600 requests/minute, 5 people, 1 free workspace per account.

Select a running task, then run each operator command through ECS Exec. The
operator's own IAM identity needs ECS ExecuteCommand/SSM session permissions;
the GitHub deploy role deliberately does not have those permissions.

```sh
cluster=agenticml-hosted
service=agenticml-hosted
task=$(aws ecs list-tasks --cluster "$cluster" --service-name "$service" --query 'taskArns[0]' --output text)
aws ecs execute-command --cluster "$cluster" --task "$task" --container server --interactive \
  --command 'agenticml server accounts add owner@example.com --server-admin --data /data'
aws ecs execute-command --cluster "$cluster" --task "$task" --container server --interactive \
  --command 'agenticml server workspaces create customer-one --owner owner@example.com --data /data'
aws ecs execute-command --cluster "$cluster" --task "$task" --container server --interactive \
  --command 'agenticml server users invite owner@example.com --workspace customer-one --data /data'
```

For another customer, use `accounts add customer@example.com`, then
`workspaces create customer-two --owner customer@example.com`. Invite that
account with `users invite customer@example.com --workspace customer-two`.
To add another person to an existing workspace use
`agenticml server users add member@example.com --role member --workspace customer-one --data /data`.
Admin role for a member is `--role admin`. To issue a machine key:

```sh
aws ecs execute-command --cluster "$cluster" --task "$task" --container server --interactive \
  --command 'agenticml server keys create --name training --scope sync --user owner@example.com --workspace customer-one --data /data'
```

Keys/invite links are printed once: handle the session output as a credential
and do not paste it into CI logs or tickets. A server admin is a global account;
workspace membership/roles still control workspace access. Sign in through the
normal invite/Firebase path. Keep public signup disabled until setup is complete.

## Upgrades, rollback and config

Deploy a reviewed SHA with the manual workflow. Migrations/imports run at server
startup under database advisory migrate locks, including concurrent starts.
The old task drains on SIGTERM; ALB deregistration delay and ECS stop timeout
are both 120 seconds. Watch uploads and client retries during the roll.

Rollback by dispatching the workflow with the previous SHA (ensure its immutable
ECR tag still exists). This does **not** undo migrations. A rollback is safe only
if that server supports the new database schema and on-disk format. Otherwise
roll forward, or restore a matched database/files/object recovery point into an
isolated stack before moving traffic. Never guess at downgrade compatibility.

For Terraform-owned infrastructure/config changes, review/apply a plan. Since
the service ignores task_definition, applying new environment settings creates
a revision but does not select it automatically. Merge those settings into the
**current deployed** definition and register/select it; do not select an older
Terraform revision that could revert the running image.

To turn signup off immediately without building a new image:

```sh
current=$(aws ecs describe-services --cluster "$cluster" --services "$service" --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --task-definition "$current" > /tmp/current-task.json
python3 - <<'PY' > /tmp/config-task.json
import json
import importlib.util
spec=importlib.util.spec_from_file_location('swap', 'deploy/aws/swap_image.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d=json.load(open('/tmp/current-task.json'))['taskDefinition']
image=next(c['image'] for c in d['containerDefinitions'] if c['name']=='server')
d=m.swap_image(d,image)
env=next(c['environment'] for c in d['containerDefinitions'] if c['name']=='server')
for item in env:
    if item['name']=='AGENTICML_SERVER_OPEN_SIGNUP': item['value']='false'; break
else: env.append({'name':'AGENTICML_SERVER_OPEN_SIGNUP','value':'false'})
print(json.dumps(d))
PY
next=$(aws ecs register-task-definition --cli-input-json file:///tmp/config-task.json --query 'taskDefinition.taskDefinitionArn' --output text)
aws ecs update-service --cluster "$cluster" --service "$service" --task-definition "$next"
aws ecs wait services-stable --cluster "$cluster" --services "$service"
rm /tmp/current-task.json /tmp/config-task.json
```

Set `open_signup=false` in tfvars as well. Use the same current-definition
procedure for other environment changes, preserving the image and all other
fields. The image validates comma-separated trusted proxy CIDRs; Terraform
trusts only the public ALB subnet CIDRs, and task ingress allows only ALB SG.
No identity header is enabled implicitly.

## Restore drill (owner, isolated test service only)

1. Record a database recovery timestamp, EFS recovery point, and S3 versions
   before a fixture sync and after it. Quiesce writers for a coordinated
   recovery point when exact cross-store consistency is required. Daily AWS
   Backup retention is 35 days for EFS and RDS; native RDS PITR is 7 days.
2. Restore RDS to a **new** instance, for example:
   `aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier agenticml-hosted --target-db-instance-identifier agenticml-restore-drill --restore-time YOUR-UTC-TIMESTAMP --db-subnet-group-name agenticml-hosted --vpc-security-group-ids YOUR-RDS-SG --no-publicly-accessible`.
   Wait with `aws rds wait db-instance-available --db-instance-identifier agenticml-restore-drill`.
   Check encryption, TLS parameter group and a managed master secret before use.
3. In AWS Backup, restore the chosen EFS recovery point to a new encrypted
   filesystem. Inspect recovery metadata with `aws backup get-recovery-point-restore-metadata`
   using the vault and recovery-point ARN; pass the reviewed metadata and a
   restore-capable operator role to `aws backup start-restore-job`. Create
   private mount targets and an access point with uid/gid 10001. AWS Backup
   may restore into a recovery directory: verify its contents and place the
   restored store at the test access point's root before starting the server.
4. Restore S3 object versions (including objects hidden by delete markers)
   into a **separate test bucket**, selecting versions matching the recovery
   point. Never remove production delete markers for a drill. Noncurrent
   versions expire after 30 days; versioning is not an indefinite backup.
5. Copy the current task definition into a separate test service, replacing
   EFS IDs, database host/managed-secret ARN, S3 URL, public URL, and disabling
   signup. Give it a separate least-privilege task/execution role for the
   restored resources. Keep it off production DNS/target group. Verify login,
   both workspaces, metrics, artifacts/code diffs, usage, and a new sync.
6. Record recovery point age, recovery duration, commands, and integrity
   results. Remove only drill resources after review. Do not overwrite live
   database/files to test recovery. Record any cross-store inconsistency.

## Real EFS benchmark (owner)

Run the repository's `scripts/bench_server.py` on a one-off Fargate task with
the same EFS access point, network/roles, and image. The production image
intentionally contains only the installed wheel: create a temporary private
benchmark image `FROM <deployed-image>` and `COPY scripts/bench_server.py
/opt/bench_server.py`, then push it to a separate operator-owned private ECR
repo (not a public image). Register a copy of the current task definition,
substitute that image, and set entryPoint to `["python"]`, command to
`["/opt/bench_server.py","--data","/data/benchmark-1","--clients","1"]`.
Remove production database/S3/public-URL/Firebase/limits variables and secrets
from this benchmark definition: the script runs its own local server and
measures EFS file operations, not the live customer database. Give it a
separate EFS directory, never `/data` itself. Use `aws ecs run-task --launch-type
FARGATE --cluster agenticml-hosted --task-definition YOUR-BENCH-REVISION
--network-configuration file://YOUR-PRIVATE-NETWORK.json`. Repeat with
`/data/benchmark-8` and `--clients 8`, without `--fsync-delay-ms`. Inspect task
exit codes and collect awslogs output. The local embedded benchmark is distinct
from a production ALB/Postgres throughput test.

Record real EFS throughput/latency for 1 and 8 clients, region/AZ, task size,
EFS elastic/generalPurpose settings and NFS mount details in `docs/SERVER.md`.
Confirm or revise `--follow --interval` and `APPEND_CHUNK` recommendations;
remove only the isolated benchmark directories/images/tasks afterwards.

## Incidents, launch evidence and cost

Use `aws logs tail /ecs/agenticml-hosted --follow`, ECS service events and
CloudWatch alarms. ALB access logs expire in 90 days; app logs in 30 days.
Alarms cover ALB 5xx >5/5min, any unhealthy host over a 5-minute period, p95
latency >5 seconds, RDS free storage <2 GB, CPU >80%, and running tasks <2.
Missing running-task metrics also alarm. Confirm the email subscription and
exercise one alarm. Scale temporarily with `aws ecs update-service --cluster
agenticml-hosted --service agenticml-hosted --desired-count 4`; also update
Terraform desired_count before the next apply. Suspend abuse with ECS Exec:
`agenticml server workspaces suspend customer-one --data /data`.
Inspect `agenticml server workspaces usage --data /data`; signup can be disabled
using the configuration procedure above.

Owner launch checklist (24.3): two real customer workspaces; sign-in/invite
verification; sync and dashboard parity; denied cross-workspace requests;
a rolling deploy during uploads with eventual byte/hash parity and no lost
uploads; restore drill; real-EFS benchmark; and every 3.0 plan exit criterion 6.
Attach evidence to roadmap Backlog entries before closing them.

Approximate monthly us-east-2 planning estimate from the 3.0 plan (not a quote):
ALB ~$20, two 0.5-vCPU/1-GB Fargate tasks ~$36, single-AZ db.t4g.small ~$25
plus storage, one NAT ~$35, Route 53 ~$1, EFS/S3 by usage: about **$120 total**
before transfer, storage growth, Backup, logs, alarms and Container Insights.
Multi-AZ RDS adds roughly $25. Confirm current account pricing before apply.

## Teardown

Disable signup, quiesce sync, export recovery evidence, and verify backups.
Remove RDS deletion protection only in a reviewed Terraform change; keep
skip_final_snapshot=false and use a new unique final_snapshot_identifier.
`terraform plan -destroy` then owner-reviewed `terraform destroy` preserves the
final RDS snapshot. Versioned object/log buckets must be deliberately emptied
(including versions/delete markers) or retained; no force_destroy is set.
Nonempty backup vaults also block deletion. Decide retention explicitly before
removing EFS/backup recovery points; never delete the remote state/lock backend
as part of stack teardown.

## Local validation

```sh
testbed/.venv/bin/python -m pytest -q tests/test_deploy_swap_image.py
testbed/.venv/bin/python -m pytest -q tests
BUILDX_CONFIG=/tmp/agenticml-buildx docker build -f deploy/aws/Dockerfile -t agenticml-hosted-check .
docker run --rm --entrypoint agenticml agenticml-hosted-check --version
docker run --rm --entrypoint python agenticml-hosted-check -c 'import psycopg, boto3, jwt'
AGENTICML_TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres \
  bash scripts/hosted_smoke.sh
```

The smoke test uses Linux host networking and port 8080 (must be free), creates
and drops an isolated database, and checks a real sync, the dashboard API, S3
object bytes and a clean SIGTERM. The S3 stand-in is a moto server: on the host
when `moto[server]` is installed in the venv, otherwise in a disposable
`python:3.12.12-slim-bookworm` container (pinned moto 5.2.2) on the host network.
`SMOKE_S3_BACKEND=host` or `SMOKE_S3_BACKEND=moto-container` forces one of them.
If the default local Postgres is unavailable the script starts a disposable
postgres:16 container on 55432. No real AWS credentials are used; every
container and database it creates is removed on exit, also on failure.

IAM caveat: there is no `s3:HeadBucket` IAM action; it uses `s3:ListBucket`.
The task has prefixed ListBucket plus permission when `s3:prefix` is absent to
permit HeadBucket; this also permits an unprefixed listing in this dedicated
bucket. Object reads/writes/deletes remain restricted to `hosted/*`. ECR auth,
SSM channels, and ECS task registration/description need resource `*`; other
deployment permissions are scoped to the repository, service and two roles.

Authorization references: [AWS HeadBucket](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadBucket.html) documents ListBucket authorization; [AWS provider change](https://github.com/hashicorp/terraform-provider-aws/issues/35112) dates optional OIDC thumbprints to 5.81.0 (the configured provider floor).

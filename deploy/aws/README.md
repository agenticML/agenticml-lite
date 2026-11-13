# Hosted AWS service (operator runbook)

This stack is for our private hosted service. Customers continue to install
`agenticml-lite` from PyPI. Only the owner runs the AWS commands below. They
create billed resources and a public hostname. Package 24.3 remains open until
all launch checks below have recorded evidence.

Two CloudFormation stacks run in us-east-2:
- `agenticml-hosted-bootstrap`: deployed once from a laptop with admin credentials.
- `agenticml-hosted`: deployed and updated only by the GitHub Actions workflow.

## Prerequisites and configuration

Tools needed on your laptop:
- AWS CLI v2 with the Session Manager plugin installed.
- Docker with BuildKit, only for local builds (for example
  `scripts/hosted_smoke.sh`). The deploy workflow builds the image itself. The
  service runs on ARM64 (Graviton); on an x86 machine, `--platform linux/arm64`
  needs QEMU/binfmt.
- GitHub CLI (`gh`).
- An IAM admin user (not the account root user). Configure a local profile such
  as `export AWS_PROFILE=agenticml-account`.
- Region: `us-east-2`.

CloudFormation stores and manages stack state directly in AWS. There is no
remote state bucket or DynamoDB lock table.

DNS for `agenticml.xyz` stays at Namecheap. No Route 53 hosted zones or records
are created.

Enable Cost Explorer once in the AWS Billing console before deploying the
bootstrap stack (Billing → Cost Explorer → Launch Cost Explorer). Cost Explorer
data takes up to 24 hours to appear. Until data is available, the daily cost
function's `Errors` alarm may fire once on day one.

## Bootstrap stack (once)

The bootstrap stack (`deploy/aws/cloudformation/bootstrap.yaml`) creates the
foundation that must exist before the deploy workflow can run:
- **ECR repository** `agenticml/lite-server`: immutable tags, scan on push,
  lifecycle policy keeping the last 30 images, `DeletionPolicy: Retain`.
- **Deploy role** `agenticml-hosted-deploy`: trusted only by the OIDC subject
  `repo:agenticML@279794012/agenticml-lite@1435335342:environment:hosted`
  (GitHub's immutable subject; `repo:${GitHubRepo}:environment:hosted` when
  `GitHubSubjectPrefix` is empty) with audience `sts.amazonaws.com`. Has permissions to push images to ECR, describe images,
  initiate and execute change sets on the hosted stack, and pass the
  CloudFormation role.
- **CloudFormation role** `agenticml-hosted-cloudformation`: assumed by the
  CloudFormation service to create and update resources in the hosted stack.
- **ACM certificate** for `app.agenticml.xyz`: uses DNS validation with
  `DeletionPolicy: Retain`.
- **Alerts topic** `agenticml-alerts`: unencrypted SNS topic (AWS Budgets cannot
  publish to encrypted topics) with an email subscription for each address.
- **Monthly budget** `agenticml-monthly`: $100 monthly budget limit. Excludes
  credits and refunds so real spend is visible. Notifies the alerts topic when
  actual spend or forecasted spend exceeds 100%.
- **Daily cost-step function** `agenticml-cost-steps`: Lambda function running
  daily at 06:00 UTC via EventBridge rule `agenticml-cost-steps-daily`. Sends an
  email each time month-to-date spend crosses another $10 milestone, reporting
  a per-service breakdown and month-end forecast (`GetCostForecast`). On the 2nd
  of each month, emails the previous month's final total. Skips queries on the
  1st because Cost Explorer data lags by a day.

Deploy the bootstrap stack:

```sh
export AWS_PROFILE=agenticml-account AWS_REGION=us-east-2 AWS_PAGER=""
aws cloudformation deploy \
  --stack-name agenticml-hosted-bootstrap \
  --template-file deploy/aws/cloudformation/bootstrap.yaml \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides \
    AlertEmails='first@example.com,second@example.com' \
    GitHubOidcProviderArn=arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com
```

Our AWS account already has an OIDC provider for GitHub Actions used by other
roles. Always pass its ARN in `GitHubOidcProviderArn` (find it with
`aws iam list-open-id-connect-providers`). Leaving this parameter empty makes
CloudFormation attempt to create a new provider, which fails if one already
exists and would delete the provider if the stack is ever deleted.

The deploy role trusts only the `hosted` environment of this repository.
This repository uses GitHub's immutable OIDC subjects, which carry the owner
and repository IDs, so `GitHubSubjectPrefix` defaults to
`repo:agenticML@279794012/agenticml-lite@1435335342`, and `GitHubRepo` is
then ignored. For a fork or another repository, pass the `sub_claim_prefix` from
`gh api repos/OWNER/REPO/actions/oidc/customization/sub`, or an empty value
(with `GitHubRepo=OWNER/REPO`) if it sends the default `repo:OWNER/REPO`
subject. A mismatch shows up in the deploy run as `Not authorized to perform
sts:AssumeRoleWithWebIdentity`.

Optional parameters: `MonthlyBudget` (default 100), `CostStep` (default 10),
`Hostname` (default `app.agenticml.xyz`). Never commit real alert email
addresses. `CAPABILITY_AUTO_EXPAND` is required for the `AWS::LanguageExtensions`
transform used to create subscriptions for each email address.

Certificate validation while the deploy command waits:
In a second terminal:

```sh
arn=$(aws acm list-certificates --query "CertificateSummaryList[?DomainName=='app.agenticml.xyz'].CertificateArn" --output text)
aws acm describe-certificate --certificate-arn "$arn" --query 'Certificate.DomainValidationOptions[0].ResourceRecord'
```

The validation CNAME record also appears in the CloudFormation stack events for
`Certificate`.

In Namecheap: Domain List → agenticml.xyz → Advanced DNS → Add New Record → CNAME.
- Host: the record name without `.agenticml.xyz.` (for example `_abc123.app`).
- Value: the record value.
- TTL: Automatic.

Certificate validation usually completes 5–30 minutes later. Keep this record
permanently; ACM uses it to automatically renew the certificate. If
`aws cloudformation deploy` times out while waiting, resume waiting with:

```sh
aws cloudformation wait stack-create-complete --stack-name agenticml-hosted-bootstrap
```

Each alert address receives an AWS SNS subscription confirmation email and must
click the confirmation link. Alerts sent before confirmation are dropped.

Read the stack outputs:

```sh
aws cloudformation describe-stacks --stack-name agenticml-hosted-bootstrap --query 'Stacks[0].Outputs' --output table
```

Outputs include `DeployRoleArn`, `CloudFormationRoleArn`, `EcrRepositoryUri`,
`CertificateArn`, and `AlertsTopicArn`. These are exported as
`agenticml-hosted-bootstrap-<Output>` and imported by the hosted stack. The
bootstrap stack cannot be deleted while the hosted stack exists.

To change budget thresholds or alert email addresses later, re-run the same
`aws cloudformation deploy` command with updated parameters.

## GitHub environment `hosted`

In GitHub repository Settings → Environments, create the `hosted` environment.

1. **Require reviewers**: Enable required reviewers and assign team members.
   Workflow files cannot configure reviewers; this must be set in the UI.
2. **Deployment branches and tags**: Restrict permitted branches/tags to `main`,
   `dev`, and tags matching `v*`.
3. **Environment variables**: Set the following variables with `gh`:

```sh
gh variable set AWS_DEPLOY_ROLE_ARN --env hosted --body "<DeployRoleArn>"
gh variable set AWS_CFN_ROLE_ARN --env hosted --body "<CloudFormationRoleArn>"
gh variable set AWS_REGION --env hosted --body "us-east-2"
gh variable set HOSTED_MULTI_AZ --env hosted --body "false"
gh variable set HOSTED_OPEN_SIGNUP --env hosted --body "false"
gh variable set HOSTED_ADMIN_EMAILS --env hosted --body "you@example.com"
```

Leave `HOSTED_FIREBASE_PROJECT` and `HOSTED_FIREBASE_API_KEY` unset until Firebase is
ready; the workflow passes unset variables to the template as empty strings.

| Variable | Value | Default |
| --- | --- | --- |
| `AWS_DEPLOY_ROLE_ARN` | Bootstrap output `DeployRoleArn` | (Required) |
| `AWS_CFN_ROLE_ARN` | Bootstrap output `CloudFormationRoleArn` | (Required) |
| `AWS_REGION` | `us-east-2` | `us-east-2` |
| `HOSTED_MULTI_AZ` | `true` or `false` | `false` |
| `HOSTED_FIREBASE_PROJECT` | Firebase project ID (empty = Firebase off) | (empty) |
| `HOSTED_FIREBASE_API_KEY` | Public Firebase web API key | (empty) |
| `HOSTED_OPEN_SIGNUP` | `true` or `false` | `false` |
| `HOSTED_ADMIN_EMAILS` | Comma-separated admin emails (no spaces; empty = none) | (empty) |
| `HOSTED_LEGAL_BASE_URL` | `https://` base URL of the policy pages (`terms/`, `acceptable-use/`, `privacy/` under it); the sign-in page links to them. Set it only after the policies are published; empty = no links. A value with a query, fragment or credentials is rejected at deploy; the server also refuses to start on a bad value | (empty) |

`HOSTED_OPEN_SIGNUP=true` requires a non-empty `HOSTED_FIREBASE_PROJECT`;
otherwise CloudFormation template assertions fail before any resource changes.
The task count is no longer a variable: it comes from the deploy workflow's
`desired_count` input (see First deploy). Delete `HOSTED_DESIRED_COUNT` if it
is already set; nothing reads it. No secrets are needed. The database password is generated and stored in an
RDS-managed secret.

## First deploy

Before approving the first deploy, create the EFS service-linked role once
(the bootstrap stack's CloudFormation role can also create it now, because
`elasticfilesystem.amazonaws.com` is in its service-linked-role list, but that
only applies after the bootstrap stack is redeployed):

```sh
aws iam create-service-linked-role --aws-service-name elasticfilesystem.amazonaws.com
```

The deploy workflow runs automatically on every push to `dev` that changes
`src/**`, `pyproject.toml`, `deploy/aws/**` (except Markdown files and
`bootstrap.yaml`) or `.github/workflows/deploy.yml`. Each run waits for the
required reviewer on the `hosted` environment. So the first deploy is: merge
to `dev`, then approve the waiting "Deploy hosted service" run in GitHub
Actions. Push runs keep the stack's current task count (one on the first
create).

Before approving, read the commit the run deploys: anyone who can push to
`dev` controls what it runs, so keep `dev` protected. The first waiting run
holds the deploy slot, and later pushes queue behind it (only the newest one
is kept). If a newer commit is waiting, reject the older run instead of
approving it, then approve the newer one.

You can also start a run by hand with GitHub CLI, but GitHub only allows
`workflow_dispatch` for workflows on the default branch, so this works only
once `deploy.yml` is also on `main`:

```sh
gh workflow run deploy.yml --ref dev
gh workflow run deploy.yml --ref dev -f ref=<sha> -f desired_count=1
```

You can also run against `main` or a `v*` tag. An optional commit SHA passed
via `-f ref=<sha>` must be an ancestor already present on the selected branch
or tag; the workflow checks `git merge-base --is-ancestor` and refuses other
commits. `desired_count` accepts `1` (default), `0` or `2`.

The workflow:
1. Assumes the deploy role via GitHub OIDC.
2. Checks if an image for the commit SHA exists in ECR. If not, builds the
   image from `deploy/aws/Dockerfile` for `linux/arm64` under QEMU and pushes
   it with the tag `<commit sha>-arm64`.
3. Runs `aws cloudformation deploy` for `deploy/aws/cloudformation/hosted.yaml`
   passing the image tag and environment configuration, using the CloudFormation
   role.
4. Waits for stack stability. The first create takes 20–40 minutes (RDS
   instance creation takes 10–20 minutes; workflow timeout is 60 minutes).
5. On failure, outputs the last 30 CloudFormation stack events.
6. On success, prints the outputs `AlbDnsName` and `ServiceUrl` and verifies
   that the running ECS task count matches the service's desired count.

After the workflow finishes, add the application CNAME record in Namecheap:
- Domain List → agenticml.xyz → Advanced DNS → Add New Record → CNAME.
- Host: `app`.
- Value: `AlbDnsName` output (e.g. `agenticml-hosted-123456789.us-east-2.elb.amazonaws.com`).
- TTL: Automatic.

Verify DNS and service health:

```sh
dig +short app.agenticml.xyz
curl --fail https://app.agenticml.xyz/healthz
aws logs tail /ecs/agenticml-hosted --since 10m
```

The stack configures six CloudWatch alarms (described in detail under Incidents,
launch evidence and cost below).

## If the first create fails

If the initial stack creation fails, CloudFormation rolls back and deletes the
stack, except for retained resources:
- The EFS file system (`FileSystem`, `DeletionPolicy: Retain`).
- The two S3 buckets (`ObjectsBucket` and `LogsBucket`, with generated names
  starting with `agenticml-hosted-`, `DeletionPolicy: Retain`).
- The AWS Backup vault (`BackupVault`, named `agenticml-hosted`,
  `DeletionPolicy: Retain`).
- A final RDS snapshot (`DbInstance` has `DeletionPolicy: Snapshot`).

The RDS instance has deletion protection enabled (`DeletionProtection: true`).
If rollback occurs after the database instance was created, the rollback halts
in `ROLLBACK_FAILED` or `DELETE_FAILED`. Disable deletion protection to let
deletion proceed:

```sh
aws rds modify-db-instance --db-instance-identifier agenticml-hosted --no-deletion-protection --apply-immediately
```

Then delete or continue rollback on the stack.

Before retrying the first deploy, delete the empty retained backup vault because
its fixed name `agenticml-hosted` will collide on recreation:

```sh
aws backup delete-backup-vault --backup-vault-name agenticml-hosted
```

Delete unused retained EFS filesystems or S3 buckets by hand if you want a clean
account. S3 bucket names include random suffixes and will not collide on retry.

Do not rename fixed-name resources or change security group descriptions in
place. Modifying a security group description causes CloudFormation to replace
the security group, triggering a name collision on the fixed group name.

## Accounts, Firebase and customers

The hosted service uses its own Firebase project, `agenticml-hosted`
(administered by the owner), not agenticml-server's.

1. In the Firebase console, open Authentication and enable Email/Password
   (not the email-link option) and Google. Add `app.agenticml.xyz` to
   Authentication → Settings → Authorized domains. Register a web app
   (without Firebase Hosting) and note its `projectId` and `apiKey`.
2. Set `HOSTED_FIREBASE_PROJECT`, `HOSTED_FIREBASE_API_KEY`, and
   `HOSTED_ADMIN_EMAILS` (comma-separated, no spaces; your Google email
   address, e.g. `you@example.com`) in the GitHub `hosted` environment
   variables. Keep `HOSTED_OPEN_SIGNUP=false`.
3. Re-run the deploy workflow and approve it. The variables are read when the
   job runs, so re-running the latest deploy run picks them up.

### Self-serve administration and customer workspaces (main path)

With Firebase on, `/login` serves the Firebase page; password sign-in on the
web and `/setup` return 404.

1. **Sign in as first admin**: Open `https://app.agenticml.xyz/login` and click
   "Continue with Google". Because your email is in `HOSTED_ADMIN_EMAILS`, the
   first verified sign-in creates your account if it has none and grants
   server admin privileges (`source` is `list`). With open sign-up on, the
   account also gets the free personal workspace (once); with it off, a server
   admin with no workspace membership lands on `/workspaces` with an Admin
   button. Server admins see no customer workspace data without membership.
2. **Open `/admin`**: Click Admin or open `https://app.agenticml.xyz/admin/`.
   The "Server admins" section lists all server administrators (login, grant
   source, and sign-in state).
   - **Add server admin**: Enter an email or login. If an email has no account,
     a credential-less account is created; its first verified Firebase sign-in
     links it with source `manual`.
   - **Remove server admin**: Revoking on `/admin` takes effect immediately.
     Refused for the last server admin who can sign in. If an email is still in
     `HOSTED_ADMIN_EMAILS`, it will regain server admin at its next sign-in.
   - **List removal vs `/admin` Remove**: Removing an email from
     `HOSTED_ADMIN_EMAILS` revokes list-granted privileges at that person's next
     sign-in (after the next deploy). Server admin granted on `/admin` or with
     the CLI is kept.
   - **Last admin guard**: The last server admin who can sign in is never removed (by the list,
     `/admin`, or the CLI).
3. **Customer workspaces**: In `/admin`, click "New workspace" and enter the
   slug, name, and owner email (e.g. `alice@example.com`). If the owner has no
   account yet, a credential-less account is created. At their first verified
   Firebase sign-in (Google or email/password), the account is linked and they
   land in the workspace as owner.
4. **Invite team members**: The workspace owner invites team members from the
   workspace **Team** page. An invite produces a one-time link (72 hours). The
   person opens it and signs in with Google or email/password using that same
   email, which must be verified. Inviting someone who already has an account
   adds the membership directly (no link).
5. **Issue sync keys**: A workspace admin creates sync keys on the workspace
   **Keys** page for any member (the person must be a member, i.e. has redeemed
   their invite or been added directly).
6. **Multi-workspace accounts**: With Firebase on, there are no passwords to set;
   adding an existing account to another workspace adds the membership
   directly. (The order rule requiring a user to set their password before being
   added to a second workspace applies to password sign-in only.)

Firebase links accounts by uid, which differs per Firebase project, and there
is no unlink command; do not switch projects once people have signed in.

Verify actual Google and verified email/password sign-in, invite acceptance,
and browser CSP before enabling open signup. The baked free limits are decimal
5 GB storage, 2 GB uploads/day, 600 requests/minute, 5 people, and 1 free
workspace per account.

### First server administrator

The `/setup` endpoint only exists with Firebase off. With Firebase on, `/setup`
returns 404, and the first server admin comes from `HOSTED_ADMIN_EMAILS` (set the
variable, deploy, and sign in with Google as described above).

When running with Firebase off, the server logs a one-time `/setup` link at
startup while no administrator exists. Retrieve the link from CloudWatch:

```sh
aws logs filter-log-events \
  --log-group-name /ecs/agenticml-hosted \
  --filter-pattern '"No admin user yet"' \
  --region us-east-2 \
  --profile agenticml-account \
  --query 'events[].message' \
  --output text
```

Events are returned oldest first; use the newest (last) line. A new token is
logged at every task start until an admin exists.

Open the printed URL (`https://app.agenticml.xyz/setup?token=...`) in a browser:
- The setup form works only on `https://app.agenticml.xyz` (session cookies are
  `__Host-` and Secure, and CSRF origin validation pins to the public URL).
- The login name must not contain `@` (default `admin`).
- Choose a password of at least 12 characters.
- Submitting makes the account the owner of the `default` workspace and a server
  admin (`server_admin=1`), signs you in, and closes `/setup` permanently.

Server administration pages are located at `https://app.agenticml.xyz/admin/`.

Break-glass alternative: if `/setup` is unreachable (with Firebase off) or if
granting server admin via ECS Exec is needed with Firebase on, open an ECS Exec
shell (below) and run:

```sh
# With Firebase on (linking on first verified sign-in):
agenticml server accounts add you@example.com --server-admin --data /data

# With Firebase off:
agenticml server users add admin2 --role admin --workspace default \
  --public-url https://app.agenticml.xyz --data /data
```

While `default` has no owner yet, `users add admin2` makes `admin2` its owner and
a server admin, and prints a one-time invite link to set the password.
`agenticml server accounts list` shows a `source` column (`list`, `manual`, or `-`).

### Connecting via ECS Exec

Export AWS credentials and region once in your terminal session:

```sh
export AWS_PROFILE=agenticml-account AWS_REGION=us-east-2
```

The operator's own IAM identity needs ECS `ExecuteCommand` and SSM session
permissions; the GitHub deploy role deliberately lacks these permissions.

Find the running task and guard against `None`:

```sh
task=$(aws ecs list-tasks --cluster agenticml-hosted --service-name agenticml-hosted --desired-status RUNNING --query 'taskArns[0]' --output text)
[ "$task" = "None" ] && echo "No running task"
```

Verify that ECS Exec and the management agent are ready:

```sh
aws ecs describe-tasks --cluster agenticml-hosted --tasks "$task" \
  --query 'tasks[0].{status:lastStatus,exec:enableExecuteCommand,agents:containers[?name==`server`].managedAgents[].{name:name,status:lastStatus}}'
```

Output should show `status: RUNNING`, `exec: true`, and the `ExecuteCommandAgent`
with status `RUNNING`.

Open an interactive shell in the container:

```sh
aws ecs execute-command --cluster agenticml-hosted --task "$task" --container server --interactive --command /bin/sh
```

Inside the shell, verify the database and object environment before running CLI
commands:

```sh
printenv AGENTICML_SERVER_PUBLIC_URL
printenv AGENTICML_SERVER_DATABASE_URL | cut -d@ -f2-
printenv AGENTICML_SERVER_OBJECTS
test -n "$PGPASSWORD" && echo PGPASSWORD set
ls /data/server
```

Check that `ls /data/server` shows no `agenticml.db`. If
`AGENTICML_SERVER_DATABASE_URL` is missing, the CLI silently falls back to a
SQLite database on EFS at `/data/server/agenticml.db` instead of the hosted
PostgreSQL instance.

Keys and invite links print once. Exec sessions are not logged (the cluster has
no `ExecuteCommandConfiguration`; only CloudTrail records the `ExecuteCommand`
call and its `--command` string). Never put secrets on `--command` strings and
never pass passwords with `--stdin` there.

### Customer workspaces and users (break-glass CLI)

Creating customer workspaces on `/admin`, inviting members from the Team page,
and issuing sync keys from the Keys page is the standard path. The CLI commands
below remain available over ECS Exec as the break-glass path.

All `users` and `keys` commands default to `--workspace default`, so always pass
`--workspace <slug>` explicitly. All server commands require `--data /data`.

To provision the first customer workspace (`customer-one`):

1. Create the customer account:
   ```sh
   agenticml server accounts add alice@example.com --data /data
   ```
2. Create the workspace with that owner:
   ```sh
   agenticml server workspaces create customer-one --name "Customer One" --owner alice@example.com --data /data
   ```
3. Generate an invite link for the owner:
   ```sh
   agenticml server users invite alice@example.com --workspace customer-one \
     --public-url https://app.agenticml.xyz --data /data
   ```
4. Add team members:
   ```sh
   agenticml server users add bob@example.com --role member --workspace customer-one \
     --public-url https://app.agenticml.xyz --data /data
   ```
   `users add` prints an invite only when creating a new account. For an existing
   account, it adds the workspace membership and prints nothing.
5. Create a sync API key:
   ```sh
   agenticml server keys create --name training --scope sync --user alice@example.com --workspace customer-one --data /data
   ```
   The `--user` must be a non-viewer member of that workspace. Key names are
   unique per workspace. Plaintext keys and invite links are printed once: record
   them immediately.

To provision a second customer workspace (`customer-two`):

```sh
agenticml server accounts add carol@example.com --data /data
agenticml server workspaces create customer-two --name "Customer Two" --owner carol@example.com --data /data
agenticml server users invite carol@example.com --workspace customer-two \
  --public-url https://app.agenticml.xyz --data /data
agenticml server keys create --name training --scope sync --user carol@example.com --workspace customer-two --data /data
```

**Multi-workspace accounts:**
With Firebase on, there are no passwords to set. When an account belongs to more
than one workspace, adding them to another workspace adds the membership
directly:

```sh
agenticml server users add alice@example.com --role member --workspace customer-two --data /data
```

(The order rule requiring a person to open their invite link and set their
password in the browser before being added to a second workspace applies to
password sign-in only.)

A server admin account sees a customer workspace only if explicitly added as a
workspace member. The `/admin/` portal lists and creates workspaces across the
server. Keep public signup disabled until setup is complete.

## Sync from a training machine

### Installation

Version 3.0.0 is not yet published on PyPI. Install `agenticml-lite` with
system metrics support directly from the `dev` branch:

```sh
pip install 'agenticml-lite[system] @ git+https://github.com/agenticML/agenticml-lite@dev'
```

### Environment configuration

`agenticml login` keeps only one key per server host, so logging in for a second
workspace overwrites the first. When working with multiple workspaces on the
same host, configure each workspace in a separate shell or subshell:

```sh
export AGENTICML_HOST=https://app.agenticml.xyz
export AGENTICML_DIR=$HOME/.agenticml-customer-one
read -rsp 'customer-one sync key: ' AGENTICML_API_KEY; export AGENTICML_API_KEY; echo
```

Setting `AGENTICML_HOST` avoids warnings about sending keys to default hosts.
Reading `AGENTICML_API_KEY` via `read -rsp` keeps the key out of shell history.

Verify key binding and identity:

```sh
agenticml whoami
```

This prints the host, key prefix, key name, scope, user account, role, and the
bound workspace (`workspace: customer-one`).

### Smoke test

Run test scripts from an otherwise empty directory (e.g. `~/aml-smoke`),
because run lineage snapshots the script's project directory and uploads it with
the run.

Create `smoke.py`:

```sh
mkdir -p ~/aml-smoke && cd ~/aml-smoke
cat > smoke.py <<'EOF'
import sys
import agenticml

ws = sys.argv[1]
run = agenticml.init(project=f"smoke-{ws}", name=f"smoke-{ws}", config={"lr": 0.01, "workspace": ws}, tags=["smoke"], verbose=True)
for step in range(20):
    agenticml.log({"loss": 1 / (step + 1), "acc": step / 20}, step=step)
with open("model.txt", "w") as f:
    f.write(f"tiny model for {ws}\n")
agenticml.log_artifact("model.txt", name="checkpoint", copy=True)
agenticml.summary["best_loss"] = 1 / 20
agenticml.finish()
print("RUN_ID", run.run_id)
EOF
```

Execute the run and sync to the workspace:

```sh
python smoke.py customer-one
agenticml sync --all
```

A subsequent `agenticml sync --all` reports `up to date`.

To stream metrics continuously during a live training run:

```sh
agenticml sync --follow --interval 5
```

### Dashboard verification

View uploaded runs in the workspace dashboard:
- `https://app.agenticml.xyz/w/customer-one/`
- `https://app.agenticml.xyz/w/customer-two/`

Verify that the `smoke-customer-one` project, 20 metric steps, `checkpoint`
artifact, and summary appear. Users without membership in a workspace receive
an HTTP 404 response when attempting to access its dashboard.

### Sync state isolation between workspaces

Sync state (`runs/<id>/sync.json`) is keyed by server hostname, not by
workspace. If a single local store (`AGENTICML_DIR`) attempts to sync the same
run to two workspaces on the same host, the second sync reports "up to date" and
uploads nothing.

Always maintain one `AGENTICML_DIR` per workspace (e.g.
`$HOME/.agenticml-customer-one` and `$HOME/.agenticml-customer-two`).

If you need to push a single local run to a second workspace from the same store,
switch the shell to the second workspace's key (read with `read -rsp` as above,
so the key stays out of shell history), then reset the run's cursor for this
host and sync it again:

```sh
agenticml sync <RUN_ID> --reset
agenticml sync <RUN_ID>
```

## Upgrades, rollback and config

Every push to `dev` that changes the image or the stack queues a deploy of
that commit; approve it in GitHub Actions. Once `deploy.yml` is on `main`, you
can also dispatch a reviewed commit SHA by hand. Database
migrations and imports run at server startup under PostgreSQL advisory locks
(`pg_advisory_xact_lock`), safely coordinating concurrent task starts. Rolling
deploys start the new task before stopping the old one
(`minimumHealthyPercent=100`, `maximumPercent=200`), so a single task still
deploys without downtime. The old task drains on SIGTERM; ALB deregistration
delay and ECS stop timeout are both 120 seconds. Watch uploads and client
retries during the rolling deployment.

Until dispatch is available, roll back by reverting on `dev`: the push of the
revert triggers a deploy. With dispatch, run the workflow with
`-f ref=<previous sha>` (ensure its immutable ECR image tag still exists).
Rolling back does **not** undo database
migrations. A rollback is safe only if that server version supports the current
database schema and on-disk format. Otherwise roll forward, or restore a
matched database, files, and object recovery point into an isolated stack before
moving traffic. Never guess at downgrade compatibility.

To make configuration changes (Multi-AZ, Firebase settings, open sign-up, admin emails, or the policy base URL):
1. Update the corresponding variable in the GitHub `hosted` environment.
2. Re-run the deploy workflow with the currently deployed commit ref or SHA
   (this needs dispatch; until then, use "Re-run all jobs" on the last
   deploy run, which reads the variables again and needs approval again).

The task count is set per run with the `desired_count` input instead (see
Operations and scaling).

The workflow reuses the existing container image in ECR, CloudFormation
updates the task definition or stack resources, and ECS performs a rolling
service update in a few minutes.

To turn open sign-up off immediately, set `HOSTED_OPEN_SIGNUP=false` in the
GitHub environment and re-run the workflow.

Do not modify the ECS service directly with `aws ecs update-service`: the next
workflow deploy will overwrite direct changes. Use manual updates only in an
emergency or for the temporary task count described under Operations and
scaling, and put any lasting change into the workflow or the GitHub
environment.

Rotating the RDS-managed database password in Secrets Manager requires forcing
a new ECS task deployment so tasks fetch the new secret:

```sh
aws ecs update-service --cluster agenticml-hosted --service agenticml-hosted --force-new-deployment
```

Trusted proxies are configured via `AGENTICML_SERVER_TRUSTED_PROXY` using the
ALB subnet CIDRs only. Task security group ingress accepts traffic only from
the ALB security group on port 8080.

## Restore drill and Real EFS benchmark

### Restore drill (owner, isolated test service only)

1. Record a database recovery timestamp, EFS recovery point, and S3 versions
   before a fixture sync and after it. Quiesce writers for a coordinated
   recovery point when exact cross-store consistency is required. Daily AWS
   Backup retention is 35 days for EFS and RDS; native RDS PITR is 7 days.
2. Restore RDS to a **new** instance:
   ```sh
   rds_sg=$(aws ec2 describe-security-groups --filters Name=group-name,Values=agenticml-hosted-rds --query 'SecurityGroups[0].GroupId' --output text)
   aws rds restore-db-instance-to-point-in-time \
     --source-db-instance-identifier agenticml-hosted \
     --target-db-instance-identifier agenticml-restore-drill \
     --restore-time YOUR-UTC-TIMESTAMP \
     --db-subnet-group-name agenticml-hosted \
     --vpc-security-group-ids "$rds_sg" \
     --no-publicly-accessible
   ```
   Wait with `aws rds wait db-instance-available --db-instance-identifier agenticml-restore-drill`.
   Check encryption, TLS parameter group, and managed master secret before use.
3. In AWS Backup, restore the chosen EFS recovery point to a new encrypted
   filesystem. Inspect recovery metadata with
   `aws backup get-recovery-point-restore-metadata` using the vault
   `agenticml-hosted` and recovery-point ARN; pass the reviewed metadata and a
   restore-capable operator role to `aws backup start-restore-job`. Create
   mount targets in the data subnets and an access point with uid/gid 10001.
   AWS Backup may restore into a recovery subfolder: verify its contents and
   place the restored store at the test access point root before starting the
   server.
4. Restore S3 object versions (including objects hidden by delete markers) into
   a **separate test bucket**, selecting versions matching the recovery point.
   Never remove production delete markers for a drill. Noncurrent versions
   expire after 30 days; versioning is not an indefinite backup.
5. Copy the current task definition into a separate test service, replacing
   EFS IDs, database host/managed-secret ARN, S3 URL, public URL, and disabling
   signup. Networking: tasks run in the public task subnets with a public IPv4
   address for outbound traffic only (no NAT). Use the hosted stack outputs
   `TaskSubnets` and `TaskSecurityGroup` with `assignPublicIp=ENABLED`. Give it
   a separate least-privilege task/execution role for the restored resources.
   Keep it off production DNS and the production target group. Verify login,
   both workspaces, metrics, artifacts/code diffs, usage, and a new sync.
6. Record recovery point age, recovery duration, commands, and integrity
   results. Remove only drill resources after review. Do not overwrite live
   database or files to test recovery. Record any cross-store inconsistency.

### Real EFS benchmark (owner)

Run the repository's `scripts/bench_server.py` on a one-off Fargate task with
the same EFS access point, network/roles, and image. The production image
intentionally contains only the installed wheel: create a temporary private
benchmark image `FROM <deployed-image>` and `COPY scripts/bench_server.py /opt/bench_server.py`,
then push it to a separate operator-owned private ECR repo (not a public image).
Register a copy of the current task definition, substitute that image, and set
entryPoint to `["python"]`, command to
`["/opt/bench_server.py", "--data", "/data/benchmark-1", "--clients", "1"]`.
Remove production database, S3, public-URL, Firebase, and limits variables and
secrets from this benchmark definition: the script runs its own local server
and measures EFS file operations, not the live customer database. Give it a
separate EFS directory, never `/data` itself.

Fetch the network settings from stack outputs:

```sh
subnets=$(aws cloudformation describe-stacks --stack-name agenticml-hosted --query "Stacks[0].Outputs[?OutputKey=='TaskSubnets'].OutputValue" --output text)
task_sg=$(aws cloudformation describe-stacks --stack-name agenticml-hosted --query "Stacks[0].Outputs[?OutputKey=='TaskSecurityGroup'].OutputValue" --output text)
aws ecs run-task --launch-type FARGATE \
  --cluster agenticml-hosted \
  --task-definition YOUR-BENCH-REVISION \
  --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$task_sg],assignPublicIp=ENABLED}"
```

Repeat with `/data/benchmark-8` and `--clients 8`, without `--fsync-delay-ms`.
Inspect task exit codes and collect awslogs output. The local embedded benchmark
is distinct from a production ALB/Postgres throughput test.

Record real EFS throughput/latency for 1 and 8 clients, region/AZ, task size,
EFS elastic/generalPurpose settings, and NFS mount details in `docs/SERVER.md`.
Confirm or revise `--follow --interval` and `APPEND_CHUNK` recommendations;
remove only the isolated benchmark directories, images, and tasks afterwards.

## Incidents, launch evidence and cost

Monitor the service with `aws logs tail /ecs/agenticml-hosted --follow`, ECS
service events, and CloudWatch alarms. ALB access logs expire in 90 days (stored
in the logs bucket); application logs in CloudWatch expire in 30 days.

### Alarms

Alarms publish to the `agenticml-alerts` SNS topic:
1. `agenticml-hosted-alb_5xx`: ALB 5xx error count > 5 in 5 minutes (period
   300 s, statistic `Sum`).
2. `agenticml-hosted-unhealthy`: Any unhealthy target host for 5 minutes
   (`UnHealthyHostCount` ≥ 1 across 5 consecutive 60 s periods). Fed by ALB
   target group health checks on `/healthz`.
3. `agenticml-hosted-latency-p95`: Target response time p95 > 5 seconds over
   5 minutes (period 300 s, extended statistic `p95`).
4. `agenticml-hosted-rds_storage`: RDS free storage < 2 GB (`FreeStorageSpace`
   < 2,000,000,000 bytes over 300 s).
5. `agenticml-hosted-rds_cpu`: RDS CPU utilization > 80% over 5 minutes (period
   300 s, statistic `Average`).
6. `agenticml-hosted-healthy_hosts`: ALB `HealthyHostCount` for the target
   group, statistic `Minimum`, below 1 for 5 periods of 60 s, missing data
   treated as breaching. This alarm exists only while `DesiredCount` is
   greater than 0. (It replaces `agenticml-hosted-running_tasks`, which needed
   Container Insights; Container Insights is off.)

Additional bootstrap alarms:
- `agenticml-cost-steps-errors`: CloudWatch alarm on the `agenticml-cost-steps`
  Lambda function errors (≥ 1 failure in 24 hours).
- `agenticml-monthly`: AWS Budget notification when actual spend or forecasted
  spend exceeds 100% of the monthly budget.

Confirm the email subscriptions and exercise one alarm to verify alerting.

### Operations and scaling

The service runs one Fargate task by default. To change the task count, run the
deploy workflow with the `desired_count` input (choices `1`, `0`, `2`; default
`1`; push-triggered runs keep the current count):

```sh
gh workflow run deploy.yml --ref dev -f ref=<deployed sha> -f desired_count=2
```

This needs `deploy.yml` on `main` (see First deploy). Until then, to run two
tasks temporarily:

```sh
aws ecs update-service --cluster agenticml-hosted --service agenticml-hosted --desired-count 2
```

The next deploy that updates the service sets the count back to the stack's
value.

To suspend an abusive workspace using ECS Exec:

```sh
aws ecs execute-command --cluster agenticml-hosted --task "$task" --container server --interactive \
  --command 'agenticml server workspaces suspend customer-one --data /data'
```

To inspect aggregate workspace usage:

```sh
aws ecs execute-command --cluster agenticml-hosted --task "$task" --container server --interactive \
  --command 'agenticml server workspaces usage --data /data'
```

### Launch checklist (package 24.3)

Before considering the hosted service live:
- Create two real customer workspaces.
- Verify sign-in and invite links.
- Confirm sync and dashboard parity.
- Confirm denied cross-workspace requests (membership isolation).
- Verify a rolling deployment during an active upload with eventual byte and
  hash parity and no lost uploads.
- Complete one restore drill.
- Complete the real-EFS benchmark.
- Satisfy every exit criterion 6 of the 3.0 plan.
Attach evidence to roadmap Backlog entries before closing them.

### Cost estimate

Estimated monthly cost in `us-east-2` (on demand) is about **$62–66/month**
total:
- One ARM64 Fargate task (0.5 vCPU, 1 GB RAM): ~$14.40
- Application Load Balancer: ~$17–18
- Public IPv4 addresses (ALB ×2, task ×1; no NAT gateway): ~$11
- RDS PostgreSQL `db.t4g.micro` with 20 GB gp3 storage: ~$14
- EFS: ~$1–2
- S3 and ECR: ~$1–2
- CloudWatch logs and the six alarms: ~$1–2
- AWS Backup: ~$1
- Secrets Manager: ~$0.40
- Cost alerts (Cost Explorer API queries): ~$0.60

Data transfer under 100 GB/month is free. DNS at Namecheap avoids Route 53
zone fees. The bootstrap budget stays at $100 with $10 steps; to alert earlier,
lower `MonthlyBudget` to 75 by redeploying the bootstrap stack.

Ways to scale up later:
- A second task (~$18)
- `db.t4g.small` (~$12)
- Multi-AZ RDS (roughly doubles the RDS cost)

## Teardown

Tear down the hosted stack first, then the bootstrap stack.

1. **Hosted stack teardown**:
   Disable open signup, quiesce sync traffic, export recovery evidence, and
   verify backups.

   Turn off RDS deletion protection:
   ```sh
   aws rds modify-db-instance --db-instance-identifier agenticml-hosted --no-deletion-protection --apply-immediately
   ```

   Delete the stack using the CloudFormation role:
   ```sh
   cfn_role=$(aws cloudformation describe-stacks --stack-name agenticml-hosted-bootstrap --query "Stacks[0].Outputs[?OutputKey=='CloudFormationRoleArn'].OutputValue" --output text)
   aws cloudformation delete-stack --stack-name agenticml-hosted --role-arn "$cfn_role"
   ```

   Retained resources:
   - S3 objects bucket and logs bucket (`DeletionPolicy: Retain`). Versioned
     buckets must be emptied deliberately (including versions and delete
     markers) if deleting them.
   - EFS file system (`DeletionPolicy: Retain`).
   - Backup vault `agenticml-hosted` (`DeletionPolicy: Retain`).
   - Final RDS snapshot (`DeletionPolicy: Snapshot`).

2. **Bootstrap stack teardown**:
   Once the hosted stack deletion is complete:
   ```sh
   aws cloudformation delete-stack --stack-name agenticml-hosted-bootstrap
   ```

   Retained resources:
   - ECR repository `agenticml/lite-server` (`DeletionPolicy: Retain`).
   - ACM certificate for `app.agenticml.xyz` (`DeletionPolicy: Retain`).
   - SSM parameter `/agenticml/cost-alerts/state` (created by Lambda runtime).
   - Shared GitHub OIDC provider is untouched when its ARN was passed via
     `GitHubOidcProviderArn`.

3. **DNS cleanup**:
   Remove the Namecheap CNAME records (for `app` and certificate validation)
   only after the certificate is deleted.

## Local validation

Run the test suite and lint CloudFormation templates:

```sh
testbed/.venv/bin/python -m pytest -q tests/test_deploy_cloudformation.py tests/test_cost_alerts.py
testbed/.venv/bin/python -m pytest -q tests
python3 -m venv /tmp/cfnlint && /tmp/cfnlint/bin/pip install cfn-lint==1.60.0
/tmp/cfnlint/bin/cfn-lint deploy/aws/cloudformation/*.yaml
BUILDX_CONFIG=/tmp/agenticml-buildx docker build -f deploy/aws/Dockerfile -t agenticml-hosted-check .
docker run --rm --entrypoint agenticml agenticml-hosted-check --version
docker run --rm --entrypoint python agenticml-hosted-check -c 'import psycopg, boto3, jwt'
AGENTICML_TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres \
  bash scripts/hosted_smoke.sh
```

CI pins `cfn-lint` 1.60.0.

The smoke test uses Linux host networking and port 8080 (must be free), creates
and drops an isolated database, and checks a real sync, the dashboard API, S3
object bytes, and a clean SIGTERM. The S3 stand-in is a moto server: on the host
when `moto[server]` is installed in the venv, otherwise in a disposable
`python:3.12.12-slim-bookworm` container (pinned moto 5.2.2) on the host network.
`SMOKE_S3_BACKEND=host` or `SMOKE_S3_BACKEND=moto-container` forces one of them.
If the default local Postgres is unavailable the script starts a disposable
`postgres:16` container on port 55432. No real AWS credentials are used; every
container and database it creates is removed on exit, also on failure.

## IAM caveats and references

IAM caveat: there is no `s3:HeadBucket` IAM action; it uses `s3:ListBucket`.
The task has prefixed ListBucket plus permission when `s3:prefix` is absent to
permit HeadBucket; this also permits an unprefixed listing in this dedicated
bucket. Object reads/writes/deletes remain restricted to `hosted/*`. ECR
authorization and the SSM message channels need resource `*`. The GitHub deploy
role can only push to the ECR repository, run change sets on the
`agenticml-hosted` stack, pass the CloudFormation role to CloudFormation, and
describe the one ECS service; CloudFormation itself registers task definitions.

Authorization references: [AWS HeadBucket](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadBucket.html) documents ListBucket authorization.

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
  CloudFormation service to create and update resources in the hosted stack,
  and to manage the service's autoscaling.
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
- **CloudWatch dashboard** `agenticml-hosted`: holds widgets for requests and
  errors, latency (p50/p95/p99), load (in-flight and database connections, plus
  RDS DatabaseConnections), ECS CPU and memory with RDS CPU, RDS CPU credits,
  and usage totals.

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
2. **Deployment branches and tags**: Restrict permitted branches/tags to `main`
   and tags matching `v*`.
3. **Environment variables**: Set the following variables with `gh`:

```sh
gh variable set AWS_DEPLOY_ROLE_ARN --env hosted --body "<DeployRoleArn>"
gh variable set AWS_CFN_ROLE_ARN --env hosted --body "<CloudFormationRoleArn>"
gh variable set AWS_REGION --env hosted --body "us-east-2"
gh variable set HOSTED_MULTI_AZ --env hosted --body "false"
gh variable set HOSTED_OPEN_SIGNUP --env hosted --body "false"
```

Leave `HOSTED_FIREBASE_PROJECT` and `HOSTED_FIREBASE_API_KEY` unset until Firebase is
ready; the workflow passes unset variables to the template as empty strings.

4. **Environment secret**: Set the admin emails as a secret, not a variable:

```sh
gh secret set HOSTED_ADMIN_EMAILS --env hosted
```

`gh` prompts for the value (comma-separated, no spaces; for example
`you@example.com`), so it stays out of your shell history. Run logs of this
public repository print variables, while secrets show as `***`. GitHub cannot
show a secret's value again, so keep the list somewhere safe.

| Variable | Value | Default |
| --- | --- | --- |
| `AWS_DEPLOY_ROLE_ARN` | Bootstrap output `DeployRoleArn` | (Required) |
| `AWS_CFN_ROLE_ARN` | Bootstrap output `CloudFormationRoleArn` | (Required) |
| `AWS_REGION` | `us-east-2` | `us-east-2` |
| `HOSTED_MULTI_AZ` | `true` or `false` | `false` |
| `HOSTED_FIREBASE_PROJECT` | Firebase project ID (empty = Firebase off) | (empty) |
| `HOSTED_FIREBASE_API_KEY` | Public Firebase web API key | (empty) |
| `HOSTED_OPEN_SIGNUP` | `true` or `false` | `false` |
| `HOSTED_LEGAL_BASE_URL` | `https://` base URL of the policy pages (`terms/`, `acceptable-use/`, `privacy/` under it); the sign-in page links to them. Set it only after the policies are published; empty = no links. A value with a query, fragment or credentials is rejected at deploy; the server also refuses to start on a bad value | (empty) |

`HOSTED_OPEN_SIGNUP=true` requires a non-empty `HOSTED_FIREBASE_PROJECT`;
otherwise CloudFormation template assertions fail before any resource changes.
The task count is no longer a variable: it comes from the deploy workflow's
`desired_count` and `max_task_count` inputs (see First deploy and Operations and
scaling). Delete `HOSTED_DESIRED_COUNT` if it is already set; nothing reads it.

The dashboard's Contact us button writes to the stack parameter `ContactEmail`
(server setting `AGENTICML_SERVER_CONTACT_EMAIL`), which defaults to
`contact@agenticml.xyz`. It has no GitHub variable: the deploy workflow does
not pass it, so the default applies when the stack first gets the parameter.
After that the stack keeps its stored value: `aws cloudformation deploy`
reuses the previous value of any parameter it is not given, so editing the
default in `hosted.yaml` later changes nothing on an existing stack. The button
only opens the visitor's email app; nothing is sent by the page. To change the
address (or `off` for no button) on an existing stack, update the stack
parameter itself: run `aws cloudformation deploy ... --parameter-overrides
ContactEmail=ADDRESS` with the other parameters unchanged, or change
`ContactEmail` in the CloudFormation console and keep the other values.

| Secret | Value | Default |
| --- | --- | --- |
| `HOSTED_ADMIN_EMAILS` | Comma-separated admin emails (no spaces; empty = none) | (empty) |

`HOSTED_ADMIN_EMAILS` is a secret, never a variable: the deploy stops at "Check
required variables" while a `HOSTED_ADMIN_EMAILS` *variable* exists. The
database password is generated and stored in an RDS-managed secret.

### Moving the admin emails from a variable to a secret

Existing deployments that set `HOSTED_ADMIN_EMAILS` as a variable must move it
before the next deploy:

```sh
gh secret set HOSTED_ADMIN_EMAILS --env hosted
gh variable delete HOSTED_ADMIN_EMAILS --env hosted
```

Enter the same value at the prompt. Old run logs still contain the address. To
remove it, delete those runs' logs (`gh run list --workflow deploy.yml --limit 200`, then
`gh api -X DELETE repos/<owner>/<repo>/actions/runs/<id>/logs`), or delete the
runs in the Actions tab.

## First deploy

Before approving the first deploy, create the EFS service-linked role once
(the bootstrap stack's CloudFormation role can also create it now, because
`elasticfilesystem.amazonaws.com` is in its service-linked-role list, but that
only applies after the bootstrap stack is redeployed):

```sh
aws iam create-service-linked-role --aws-service-name elasticfilesystem.amazonaws.com
```

The deploy workflow runs automatically on every push to `main` that changes
`src/**`, `pyproject.toml`, `deploy/aws/**` (except Markdown files and
`bootstrap.yaml`) or `.github/workflows/deploy.yml`. Each run waits for the
required reviewer on the `hosted` environment. So the first deploy is: merge
to `main`, then approve the waiting "Deploy hosted service" run in GitHub
Actions. Push runs keep the stack's current task count (one on the first
create).

Before approving, read the commit the run deploys: anyone who can push to
`main` controls what it runs, so keep `main` protected. The first waiting run
holds the deploy slot, and later pushes queue behind it (only the newest one
is kept). If a newer commit is waiting, reject the older run instead of
approving it, then approve the newer one.

You can also start a run by hand with GitHub CLI. GitHub only allows
`workflow_dispatch` for workflows on the default branch. That is `main`, and
`deploy.yml` is on it, so this works now:

```sh
gh workflow run deploy.yml --ref main
gh workflow run deploy.yml --ref main -f ref=<sha> -f desired_count=1
```

You can also run against a `v*` tag. An optional commit SHA passed
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

The stack configures nine CloudWatch alarms (described in detail under Incidents,
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
   address, e.g. `you@example.com`) in the GitHub `hosted` environment:
   the first two as variables, `HOSTED_ADMIN_EMAILS` as a secret
   (`gh secret set HOSTED_ADMIN_EMAILS --env hosted`). Keep
   `HOSTED_OPEN_SIGNUP=false`.
3. Re-run the deploy workflow and approve it. The variables and the secret are
   read when the job runs, so re-running the latest deploy run picks them up.

### Self-serve administration and customer workspaces (main path)

With Firebase on, `/login` serves the Firebase page; password sign-in on the
web and `/setup` return 404.

1. **Sign in as first admin**: Open `https://app.agenticml.xyz/login` and click
   "Continue with Google". Because your email is in `HOSTED_ADMIN_EMAILS`, the
   first verified sign-in creates your account if it has none and grants
   server admin privileges (`source` is `list`). With open sign-up on, the
   account also gets the free personal workspace (once); with it off, a server
   admin with no workspace membership lands on `/workspaces` with an Admin
   button and Sign out. Server admins see no customer workspace data without membership.
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
4. **Invite team members**: The workspace owner invites team members from
   **Settings > Members** in the workspace. An invite produces a one-time link
   (72 hours). The person opens it and signs in with Google or email/password
   using that same email, which must be verified. Inviting someone who already
   has an account adds the membership directly (no link). An account that was
   removed from its last workspace can only be added back by an admin of that
   workspace or by an operator (`agenticml server users add LOGIN --workspace
   SLUG`); otherwise the invite is refused (403). On this stack the link
   looks like `https://app.agenticml.xyz/invite#token=...`: the token sits in the
   URL fragment, which browsers never send, so it stays out of the load balancer
   access logs. If a mail client drops the fragment, the person lands on the
   plain sign-in page without the invite and is told they have no access; they
   open the full link again in a browser, or the owner sends a new invite.
5. **Issue sync keys**: A workspace admin creates sync keys for any member in
   **Settings > API keys** in the workspace (the person must be a member, i.e.
   has redeemed their invite or been added directly).
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
secret, deploy, and sign in with Google as described above).

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

Keys and invite links print once. Exec session contents are deliberately not
logged: the cluster has no `ExecuteCommandConfiguration`, because keys and invite
links printed in a session would be copied into CloudWatch (30-day retention).
CloudTrail records each `ExecuteCommand` call and its `--command` string. Never
put secrets on `--command` strings and never pass passwords with `--stdin`
there. ECS Exec needs no task-role change beyond the SSM messages permissions
the task role already has.

The task definition enables Fargate's init process (`InitProcessEnabled`), so
PID 1 in the container is an init process and the `agenticml` server is its
child. The init process reaps processes that exec sessions leave behind and
forwards SIGTERM to the server, which drains for up to 120 s on a stop. The
image has no init of its own and no `ps`. After a deploy, check from a session
that PID 1 is an init process and that `agenticml` is its child:

```sh
cat /proc/1/comm
for p in /proc/[0-9]*; do echo "${p#/proc/} ppid=$(awk '/^PPid/{print $2}' $p/status) $(cat $p/comm)"; done
```

and check a clean stop: after `aws ecs update-service --cluster
agenticml-hosted --service agenticml-hosted --force-new-deployment`, the old
task's exit code is 0 (not 137) and it stopped in well under 120 s:

```sh
aws ecs describe-tasks --cluster agenticml-hosted --tasks <old task arn> \
  --query 'tasks[0].[stoppedReason,containers[0].exitCode,stoppingAt,stoppedAt]'
```

### Customer workspaces and users (break-glass CLI)

Creating customer workspaces on `/admin`, inviting members from **Settings > Members**,
and issuing sync keys from **Settings > API keys** is the standard path. The CLI commands
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
   The task has the Firebase settings in its environment, so the link prints as
   `https://app.agenticml.xyz/invite#token=...` (token in the URL fragment, never
   sent to the load balancer). Run outside the task without those settings, the
   command prints the self-hosted `?token=` form, which the hosted page also
   accepts.
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

**Accounts with no workspace:** a person removed from their last workspace has an
account but no membership (a "detached" account). A workspace admin can add such
an account over HTTP (Settings > Members) only for the workspace it last left, and
never when it is a server admin. For any other workspace, and for accounts that never
had a membership, an operator adds it over ECS Exec:

```sh
agenticml server users add alice@example.com --role member --workspace customer-two --data /data
```

### Deleting a workspace and retrying cleanup

```sh
agenticml server workspaces delete customer-one --yes-delete customer-one --data /data
```

The order is: database row, audit entry (`workspace.delete`), files on EFS, then S3
objects (`--objects` or `AGENTICML_SERVER_OBJECTS`); every step runs even if another
fails. The command exits 0 once the row is gone, so **check stderr**: a line
`Warning: file cleanup failed for workspace <id>; retry with: ...` or
`Warning: object cleanup failed for workspace <id>; retry with: ...` means data is left
and a `workspace.cleanup_failed` event was recorded. Retry (safe to repeat; only that
id, only `workspaces/<id>` and the `<prefix>/<id>/` S3 prefix are touched):

```sh
agenticml server workspaces orphans --data /data       # lists ids with files or objects but no row
agenticml server workspaces cleanup <id> --data /data  # refuses ids that are still in the database
```

`server verify` and `server status` list the same leftovers as
`workspace_orphan_files` / `workspace_orphan_objects` warnings. `workspaces cleanup --all
--yes-delete all` removes every listed id; never run it against a restored or drill
database (see `scripts/restore_drill/README.md`).

### Deleting an account (privacy request)

Erases one person's account on request. Steps, in this order:

1. Find the login and its Firebase uid: `agenticml server accounts list --json --data /data`.
2. Delete or transfer the person's workspaces. The command refuses an account that owns a
   workspace (`workspaces set-owner`, or `workspaces delete`) or owns runs (delete those
   runs or their workspaces first). Personal workspace names derive from the email, so delete
   the personal workspace rather than transferring it.
3. If the account is a server admin, run `agenticml server accounts set-server-admin LOGIN off`
   and remove the email from the admin emails list.
4. Delete the Firebase user in the Firebase console (Authentication > Users) FIRST. With open
   sign-up on, a person who can still sign in would otherwise get a fresh account and a new
   personal workspace right after the deletion.
5. Run the command over ECS Exec:
   ```sh
   agenticml server accounts delete LOGIN --yes-delete LOGIN --data /data
   ```
   It removes sessions, keys, pending invites, memberships and failed-login throttle
   entries, replaces the login in
   `created_by`/`invited_by` columns with `[deleted account]`, redacts the server-level audit
   log (actor and target replaced, display name and the personal workspace's name and slug
   replaced, client address cleared on rows the person acted in) and
   records one `account.delete` event that names only the account id.

Not touched: load-balancer access logs (kept 90 days), server logs (30 days), and the Activity
logs of workspaces the person belonged to (they stay with their workspace until it is deleted;
the command lists the workspaces).

## Sync from a training machine

### Installation

Install `agenticml-lite` 3.3.1 from PyPI with system metrics support:

```sh
pip install 'agenticml-lite[system]'
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

A single local store (`AGENTICML_DIR`) can sync the same run to multiple
workspaces on the same host. On 3.0 servers, sync state in `runs/<id>/sync.json`
is keyed by host and workspace (such as `https://app.agenticml.xyz/w/customer-one`).
Because each workspace maintains its own cursor, syncing to a second workspace
uploads the run instead of reporting "up to date".

To push a run to a second workspace from the same store, switch the shell to the
second workspace's key (read with `read -rsp` as above, so the key stays out of
shell history) and sync:

```sh
read -rsp 'customer-two sync key: ' AGENTICML_API_KEY; export AGENTICML_API_KEY; echo
agenticml sync <RUN_ID>
```

Running `agenticml sync <RUN_ID> --reset` forgets every cursor for that host
across all workspaces; the next sync recovers state from the server.

The first `sync --all` or `--follow` after upgrading the client checks each
run synced before, once (one `/state` request and one commit per run; no
accepted bytes are sent again). A run pruned with `sync --prune` after syncing
to one workspace cannot be uploaded to another: its full scalar history is no
longer local.

Older clients (installed before package 24.11, or 2.x clients) keep only one
cursor per server host. With older clients, maintain one `AGENTICML_DIR` per
workspace (e.g. `$HOME/.agenticml-customer-one` and `$HOME/.agenticml-customer-two`),
or run `agenticml sync <RUN_ID> --reset` before syncing with the second key.

## Launch checks (Phase 5)

Six checks that close AWS roadmap Phase 5. The owner runs them; each has a pass
line. Record the date, run id, result and any Backlog entry in the roadmap row.
`scripts/hosted_check.py` has three subcommands: `make-run` creates a finished
local run, `verify` compares local runs with the server (file sizes and
sha256, referenced objects, status, fingerprint), and `isolation` probes
another workspace with this workspace's key. It reads the key from
`AGENTICML_API_KEY` (or asks for it) and never prints it. The script lives in
the repository. On a training machine without a checkout, fetch it from the `v3.3.1` tag:

```sh
curl -fsSLO https://raw.githubusercontent.com/agenticML/agenticml-lite/v3.3.1/scripts/hosted_check.py
```

The commands below use the setup from "Environment configuration" above
(`AGENTICML_HOST`, a per-workspace `AGENTICML_DIR`, and the key read with
`read -rsp`). For the AWS commands, export the profile and region once, as in
"Connecting via ECS Exec":

```sh
export AWS_PROFILE=agenticml-account AWS_REGION=us-east-2
```

### 5.1 Rolling deploy during an upload

Re-running a deploy of the commit that is already live does not roll anything:
the change set is empty and ECS starts no new task. Force a new deployment
instead; it uses the same rolling settings.

The upload must overlap the moment the old task drains. The new task needs a
few minutes to start, so start the roll first and the upload when the new
task is running.

1. In terminal 1 (customer-one key), make the run but do not sync yet. Note
   the run id and the `checkpoint sha256` line. Use `--mb 1000` on a fast
   link, so the upload takes at least two minutes:

   ```sh
   python scripts/hosted_check.py make-run --mb 300
   ```

2. In terminal 2, start the roll and watch the deployments until the new one
   (`PRIMARY`) shows `runningCount` 1 while the old one (`ACTIVE`) is still
   listed:

   ```sh
   aws ecs update-service --cluster agenticml-hosted --service agenticml-hosted --force-new-deployment
   watch -n 10 "aws ecs describe-services --cluster agenticml-hosted --services agenticml-hosted --query 'services[0].deployments[].[status,runningCount,rolloutState]' --output text"
   ```

3. As soon as the new task runs, start the upload in terminal 1. The old task
   now drains (up to 120 seconds) while requests move to the new one:

   ```sh
   agenticml sync --all
   ```

4. In terminal 2, wait for the roll to finish:

   ```sh
   aws ecs wait services-stable --cluster agenticml-hosted --services agenticml-hosted
   ```

5. If the sync ends early with an error, run `agenticml sync --all` again. That
   counts as a pass only if `verify` passes afterwards.
6. Verify, then read the service events:

   ```sh
   python scripts/hosted_check.py verify <RUN_ID>
   aws ecs describe-services --cluster agenticml-hosted --services agenticml-hosted \
     --query 'services[0].events[:5]'
   ```

Pass: `verify` prints `RESULT: PASS`, no acknowledged upload was lost, and the
client retried and finished on its own. Record that the old task stopped after
the new one was healthy, and the time of each. A `verify` failure blocks the
launch: record it and open a Backlog entry.

### 5.2 Isolation

With customer-one's key, probe a customer-two run. Point `AGENTICML_DIR` at
customer-two's store so the script finds that run's object digests (or pass
`--object <SHA>`):

```sh
AGENTICML_DIR=$HOME/.agenticml-customer-two \
  python scripts/hosted_check.py isolation --run <customer-two RUN_ID>
```

Then do the browser step the script prints: signed in as customer-one's owner,
open `https://app.agenticml.xyz/w/customer-two/` and a customer-two run URL.
Both must show "not found".

Pass: every probe gets 404, 401, 403, a redirect to `/login`, or lists the
object as missing, and never data. The script only reads from customer-two.

### 5.3 Older client

Install the 2.2 client from commit `bd0d499` in a separate venv, use a
separate `AGENTICML_DIR`, and run the smoke script from "Smoke test":

```sh
python -m venv ~/aml-old-venv && . ~/aml-old-venv/bin/activate
pip install 'agenticml-lite @ git+https://github.com/agenticML/agenticml-lite@bd0d499'
export AGENTICML_DIR=$HOME/.agenticml-old-client
cd ~/aml-smoke && python smoke.py customer-one
agenticml sync --all
```

Then switch back to the 3.0 environment (so `verify` uses the 3.0 tool, with
the same `AGENTICML_DIR`) and run `python scripts/hosted_check.py verify <RUN_ID>`.

Pass: the sync succeeds and `verify` passes. If the server answers 426 (the
client is older than the minimum client version), record that this check fails
and why. Do not change the minimum.

Two things went wrong on the first run (Nov 16, 2026):

- `uv venv` makes a venv without `pip`. Install with `uv pip install ...`
  (with the venv active), not `pip install`.
- A stale `agenticml` on `PATH` (the shell's command hash, for example in zsh)
  can shadow the venv's. Run `rehash` (zsh) or `hash -r` (bash) after
  activating, and check `which agenticml` points into the venv before
  syncing.

If the identical artifact already exists in that workspace, the 2.2 client
uploads no object (`objects=0`); that is expected, and `verify` still decides.

### 5.4 Object encryption

Stored objects use SSE-S3. Take the objects bucket name from the stack
outputs (`ObjectsBucket`), list three keys, and read each one:

```sh
aws s3api list-objects-v2 --bucket <ObjectsBucket output> --prefix hosted/ --max-items 3
aws s3api head-object --bucket <ObjectsBucket output> --key <KEY>
```

Then download the `checkpoint` artifact of the 5.1 run from the dashboard and
compare:

```sh
sha256sum <downloaded file>
```

Pass: each `head-object` shows `"ServerSideEncryption": "AES256"`, and the
`sha256sum` equals the `checkpoint sha256` line `make-run` printed.

### 5.5 Alarm drill

Set one alarm to `ALARM` by hand. This also confirms both SNS subscriptions
(step 2.1b):

```sh
aws cloudwatch set-alarm-state --alarm-name agenticml-hosted-alb_5xx \
  --state-value ALARM --state-reason "Phase 5 alarm drill"
```

Pass: both alert addresses receive the ALARM email. The alarm returns to OK at
its next evaluation. If the second address gets nothing, re-subscribe it
(step 2.1b).

### 5.6 Row-level security role check

The task connects to RDS as the master user. The server logs "row-level
security is not enforced for database role" at startup if that role has
SUPERUSER or BYPASSRLS:

```sh
aws logs filter-log-events --log-group-name /ecs/agenticml-hosted \
  --filter-pattern '"row-level security is not enforced"' --max-items 5
```

Pass: the result is recorded (warning present or absent) and the Backlog entry
is updated. Any event means row-level security is bypassed. CloudWatch keeps
30 days, so a task started within that window has logged its startup line.

## Upgrades, rollback and config

Every push to `main` that changes the image or the stack queues a deploy of
that commit; approve it in GitHub Actions. You can also dispatch a reviewed
commit SHA by hand. Database
migrations and imports run at server startup under PostgreSQL advisory locks
(`pg_advisory_xact_lock`), safely coordinating concurrent task starts. Rolling
deploys start the new task before stopping the old one
(`minimumHealthyPercent=100`, `maximumPercent=200`), so a single task still
deploys without downtime. The old task drains on SIGTERM; ALB deregistration
delay and ECS stop timeout are both 120 seconds. Watch uploads and client
retries during the rolling deployment.

Re-running a deploy of the commit that is already live is an empty change set
and does not restart the task. To roll the service without a new commit, use
`aws ecs update-service --cluster agenticml-hosted --service agenticml-hosted --force-new-deployment`.

To roll back, run the workflow with `-f ref=<previous sha>` (ensure its
immutable ECR image tag still exists), or revert on `main`: the push of the
revert triggers a deploy.
Rolling back does **not** undo database
migrations. A rollback is safe only if that server version supports the current
database schema and on-disk format. Otherwise roll forward, or restore a
matched database, files, and object recovery point into an isolated stack before
moving traffic. Never guess at downgrade compatibility.

To make configuration changes (Multi-AZ, Firebase settings, open sign-up, admin emails, or the policy base URL):
1. Update the corresponding variable in the GitHub `hosted` environment (the
   admin emails are the secret `HOSTED_ADMIN_EMAILS`).
2. Re-run the deploy workflow with the currently deployed commit ref or SHA
   (or use "Re-run all jobs" on the last deploy run, which reads the
   variables again and needs approval again).

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

The step-by-step runbook is [`scripts/restore_drill/README.md`](../../scripts/restore_drill/README.md),
with scripts that discover every ID at run time (stack outputs, the live task
definition, describe calls) and keep the drill's own IDs in a git-ignored state
directory (`DRILL_STATE_DIR`, default `./restore-drill-state`). This section
states the policy and the decisions behind it. Everything is named
`agenticml-restore-drill*` and tagged `Project=agenticml-restore-drill`, runs
in its own ECS cluster `agenticml-restore-drill`, and is removed afterwards.

### Restore drill (owner, isolated test service only)

1. Record a database recovery timestamp, EFS recovery point, and S3 versions
   before a fixture sync and after it. Quiesce writers for a coordinated
   recovery point when exact cross-store consistency is required. **Restore the
   database to the EFS recovery point's completion time** (`CompletionDate`),
   and cut the S3 versions at the same time, not at the point's start. An EFS
   backup copies files while it runs (the Nov 16 daily point ran about 3
   hours), so files synced in that window exist in the restored files and in S3;
   a database restored to the start does not know them (attempt 1: 60
   `object_missing` problems and 60 `object_orphan` warnings in `verify
   --deep`), and a database restored to the completion time does (attempt 2: 0
   and 0). A sync made during the backup window can still leave the database
   ahead of the files for that run (`size_mismatch` or `file_hash` problems in
   `verify`, expected after restoring). Repair it from the client that holds the
   run: `agenticml sync <run> --reset`, then `agenticml sync <run>` (once per
   workspace key; see step 1 of the restore-drill runbook in
   `scripts/restore_drill/README.md`). Runs that were pruned locally or that no
   client has cannot be repaired: record them and leave the warning or delete
   the run. After the repair `verify` reports 0 problems. The EFS recovery point
   can be an on-demand AWS Backup job (tagged, and deleted afterwards) or the
   latest scheduled point. On Nov 16 an on-demand `start-backup-job` was
   refused with AccessDenied for the operator's `aws login` session: use the
   latest scheduled point if it is refused. Daily AWS Backup retention is 35 days for EFS and
   RDS; native RDS PITR is 7 days.
2. Restore RDS to a **new** instance with a point-in-time restore. Reuse the
   production network and parameter group and add the drill flags:
   ```sh
   aws rds restore-db-instance-to-point-in-time \
     --source-db-instance-identifier agenticml-hosted \
     --target-db-instance-identifier agenticml-restore-drill \
     --restore-time EFS-RECOVERY-POINT-COMPLETION-UTC --db-instance-class db.t4g.micro \
     --db-subnet-group-name agenticml-hosted --vpc-security-group-ids "$RDS_SG" \
     --db-parameter-group-name "$DB_PARAM_GROUP" --option-group-name "$DB_OPTION_GROUP" \
     --ca-certificate-identifier "$DB_CA" --no-publicly-accessible --no-multi-az \
     --no-deletion-protection --no-auto-minor-version-upgrade \
     --backup-retention-period 0 \
     --tags Key=Project,Value=agenticml-restore-drill
   ```
   `--db-parameter-group-name` keeps `rds.force_ssl` in force (without it the
   restore uses the default group). `--no-deletion-protection` lets the drill
   instance be deleted; `--backup-retention-period 0` skips automated backups,
   so none are left behind. Wait with
   `aws rds wait db-instance-available --db-instance-identifier agenticml-restore-drill`.
   Check encryption, the TLS parameter group, and that `MasterUserSecret` is
   null: a point-in-time restore keeps the master password as of the restore
   time (which must be after the last rotation) and creates no managed secret.
   Drill tasks therefore read the production secret at task start through the
   existing execution role, exactly as the live service does. No person reads
   it, and no IAM change is needed.
3. In AWS Backup, restore the chosen EFS recovery point to a new encrypted
   filesystem with **explicit metadata**: `file-system-id`, `Encrypted`,
   `KmsKeyId` (the production key), `PerformanceMode` (`generalPurpose`),
   `CreationToken` and `newFileSystem` (`true`), passed with
   `aws backup start-restore-job --metadata file://... --resource-type EFS
   --no-copy-source-tags-to-restored-resource --iam-role-arn <the stack's backup
   role>` (compare with `aws backup get-recovery-point-restore-metadata`).
   Make the restored filesystem match production (`ThroughputMode` elastic),
   create a mount target in a data subnet with the production EFS security
   group, and delete any filesystem policy it carries (**drill filesystem only**;
   IAM mount authorization stays off, access is limited by the mount-target
   security group, which admits only the task security group). Inspect the
   restored tree with a one-off read-only task through a root access point:
   AWS Backup may restore into an `aws-backup-restore_<timestamp>/` subfolder.
   Then create the data access point (uid/gid 10001) with the `agenticml`
   directory as its root path.
4. Restore S3 object versions (including objects hidden by delete markers) into
   a **separate test bucket** (`agenticml-restore-drill-*`: all public access
   blocked, default encryption, a policy that names the task role and denies
   insecure transport), selecting the versions current at the recovery time
   (`scripts/restore_drill/copy_versions_at.py`, which only reads the
   production bucket and checks each copy against its SHA-256). Never remove
   production delete markers for a drill. Noncurrent versions expire after 30
   days; versioning is not an indefinite backup.
5. Generate the drill task definitions with `scripts/restore_drill/mk_td.py`
   from the live task definition, replacing EFS IDs, database host, S3 URL and
   public URL (loopback), and disabling signup and Firebase. The script
   refuses any definition that names a production identifier, ECS Exec or a
   port mapping. Networking: tasks run in the public task subnets with a public
   IPv4 address for outbound traffic only (no NAT), using the stack outputs
   `TaskSubnets` and `TaskSecurityGroup` with `assignPublicIp=ENABLED`, in the
   drill cluster (never the production service or target group, never
   production DNS). **Roles:** the drill reuses the existing execution role and,
   for the server task only, the existing task role through the drill bucket's
   policy (inspect, SQL and benchmark tasks have no task role). That role can
   also write the production `hosted/*` prefix, so the guards matter: the drill
   entrypoint (`drill-run.sh`) refuses to start unless the database host is the
   drill instance, the bucket is a drill bucket and the public URL is
   loopback. A dedicated least-privilege role is the alternative; it is an IAM
   create and needs the owner's approval. Verify, as one-off tasks (the image
   has no `psql`, and ECS Exec is not used): a read-only SQL check, the server
   `status`, `workspaces usage` and `verify --deep`, then a new sync and a
   dashboard login per workspace with a temporary key in the restored database.
6. Record recovery point age, recovery duration, commands, and integrity
   results. Remove only drill resources after review, in dependency order
   (RDS, task definitions, cluster, EFS access points, mount target and
   filesystem, the drill bucket, the on-demand recovery point, drill log
   streams) and finish with a listing that proves nothing remains and
   production is unchanged. Do not overwrite live database or files to test
   recovery. Record any cross-store inconsistency.

### Real EFS benchmark (owner)

Run the repository's `scripts/bench_server.py` on a one-off Fargate task in the
drill cluster, on the **restored drill filesystem**, never on production EFS
(the benchmark writes files). The drill filesystem has production's settings:
the same KMS key, `generalPurpose`, elastic throughput, a TLS mount through an
access point, a mount target in the same availability zone as the task, and the
same image. Only IAM mount authorization is off, which affects authorization,
not I/O. Use its own access point with root path `/bench` (uid/gid 10001), so the
benchmark never touches restored application data.

No benchmark image and no ECR repository are needed. The production image
intentionally contains only the installed wheel, so
`scripts/restore_drill/mk_td.py bench` embeds `scripts/bench_server.py` as
committed at the deployed commit (`git show <commit>:scripts/bench_server.py`,
gzipped and base64-encoded when the task definition is generated) together with
its sha256, and the container verifies the checksum before running it. The
definition is a copy of the production one with a 4 vCPU / 8 GB size, no task
role, and no database, S3, public-URL, Firebase, limits or secret variables: the
script runs its own local server and measures EFS file operations, not the live
customer database. SQLite refuses network filesystems and the hosted database
is not on EFS either, so the benchmark keeps the server's SQLite database on the
container's local disk (`/tmp`, one file per run, set per command through
`AGENTICML_SERVER_DATABASE_URL`) while run files, appends and objects go to EFS. It runs 1 and 8 clients, 3 repetitions each, without
`--fsync-delay-ms`, in a throw-away `/data/benchmark-<epoch>` directory that is
removed when it ends.

Run it with `scripts/restore_drill/run_task.sh`, which reads the network from
the stack outputs (`TaskSubnets`, `TaskSecurityGroup`, `assignPublicIp=ENABLED`),
runs only in the drill cluster, prints `createdAt`, `startedAt`, `stoppedAt` and
the exit code, and saves the CloudWatch log. The embedded benchmark is distinct
from a production ALB/Postgres throughput test: it measures EFS file I/O
plus a local-disk SQLite, not the hosted Postgres path.

Record real EFS throughput/latency for 1 and 8 clients, region/AZ, task size,
EFS elastic/generalPurpose settings, and NFS mount details in `docs/SERVER.md`.
Confirm or revise `--follow --interval` and `APPEND_CHUNK` recommendations;
remove the drill resources (including the benchmark access point and task
definition) afterwards.

## Incidents, launch evidence and cost

Monitor the service with `aws logs tail /ecs/agenticml-hosted --follow`, ECS
service events, and CloudWatch alarms. ALB access logs expire in 90 days (stored
in the logs bucket); application logs in CloudWatch expire in 30 days. Invite
links on this stack carry the token in the URL fragment, which is never sent to
the load balancer, so new access logs hold no invite tokens. Links created before
that change used `?token=`; they are single use, expire in 72 hours and only work
for the invited verified email, and their log lines age out with the rest.

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
   group, statistic `Minimum`, below `DesiredCount` (the minimum, when
   autoscaling is on) for 5 periods of 60 s, missing data treated as
   breaching. Alarms when there are fewer healthy targets than `DesiredCount`
   for 5 minutes; extra tasks above the minimum never alarm. This alarm exists
   only while `DesiredCount` is greater than 0. The threshold follows the
   stack's value after the service is stable. (It replaces
   `agenticml-hosted-running_tasks`, which needed Container Insights; Container
   Insights is off.)
7. `agenticml-hosted-rds_cpu_credits`: RDS `CPUCreditBalance` minimum below 20
   for three 5-minute periods. RDS t4g instances run in Unlimited mode: when
   credits run out the database is not throttled but bursts on surplus credits,
   which are billed. A new or restored database instance starts with 0 credits,
   so after a restore this alarm stays in ALARM for about two hours until it has
   earned 20.
8. `agenticml-hosted-rds_connections`: RDS `DatabaseConnections` maximum above
   the class threshold in 5 minutes: 60 (`db.t4g.micro`), 130 (`db.t4g.small`),
   270 (`db.t4g.medium`), about 80% of the estimated `max_connections`.
9. `agenticml-hosted-ecs_memory`: ECS service `MemoryUtilization` maximum above
   85% for two 5-minute periods (AWS/ECS metric; Container Insights stays off).

Additional bootstrap alarms:
- `agenticml-cost-steps-errors`: CloudWatch alarm on the `agenticml-cost-steps`
  Lambda function errors (≥ 1 failure in 24 hours).
- `agenticml-monthly`: AWS Budget notification when actual spend or forecasted
  spend exceeds 100% of the monthly budget.

Confirm the email subscriptions and exercise one alarm to verify alerting.

A weekly GitHub Actions run (`.github/workflows/monitor.yml`, Mondays 06:17 UTC,
also runnable with `gh workflow run monitor.yml`) requests
`https://app.agenticml.xyz/healthz` and the docs site's external links. It is
not an uptime guarantee and does not replace the alarms: on a failure it opens
one issue titled "Hosted monitor failed" with the label `hosted`, comments on
it while the failure lasts and closes it when the check passes again. Scheduled
runs use the default branch (`main`), so the schedule starts once the workflow
is there.

All workflow actions are pinned to full commit SHAs (tag in a comment). Update
the SHA and the comment together; look up a tag's commit with
`gh api repos/<owner>/<repo>/git/ref/tags/<tag>` and dereference annotated tags.
After changing the pins, run `publish-testpypi.yml` once before tagging a
release.

### Capacity dashboard and metrics

The hosted task runs with `AGENTICML_SERVER_METRICS=cloudwatch`. The task role may call `cloudwatch:PutMetricData` only for the namespace `AgenticML/Server`, publishing 12 custom metrics with no dimensions (requests, errors, latency, in-flight requests, process database connections, and usage totals).

The bootstrap stack holds a CloudWatch dashboard named `agenticml-hosted`. It contains widgets for:
- Requests and errors (`Requests`, `Errors4xx`, `Errors5xx`, `Rejected`).
- Latency (`Latency` p50, p95, p99).
- Load (`InFlightPeak`, `DbConnectionsPeak`, and RDS `DatabaseConnections`).
- ECS CPU and memory alongside RDS CPU.
- RDS CPU credits (`CPUCreditBalance`).
- Usage totals (`Accounts`, `ActiveUsers7d`, `WorkspacesActive`, `StoredBytes`, `UploadBytesToday`).

Re-applying the bootstrap stack from a laptop with admin credentials creates or updates the dashboard, using the existing bootstrap deploy command in this README; hosted deploys do not depend on it.

Open the dashboard in the CloudWatch console under Dashboards, `agenticml-hosted`. The custom metric widgets show data after the next hosted deploy with metrics on.

### Operations and scaling

The service runs one Fargate task by default. It can scale tasks automatically or run a fixed task count.

#### Scaling settings and database budgets

The hosted stack defines two scaling parameters:
- `MaxTaskCount`: most tasks autoscaling may run (`1` to `4`, default `1`). `1` turns autoscaling off.
- `DbInstanceClass`: RDS database instance class (`db.t4g.micro` default, `db.t4g.small`, or `db.t4g.medium`).

`DesiredCount` is the minimum task count while autoscaling is on. Autoscaling is on when `MaxTaskCount` is above 1 and `DesiredCount` is above 0. It uses an ECS target tracking policy on average service CPU:
- Target average CPU: 60%.
- Scale-out cooldown: 60 seconds.
- Scale-in cooldown: 300 seconds.
- Range: between `DesiredCount` and `MaxTaskCount`.

While autoscaling is on, deploys leave the running task count to the autoscaler. Setting `desired_count=0` always stops the service and removes the autoscaling resources.

Each task gets a database connection budget (`AGENTICML_SERVER_DB_CONNECTIONS`) so all tasks fit the database even during a rolling deploy, which briefly runs up to twice the tasks:

`budget = floor(usable / (2 × MaxTaskCount))`

Usable connections are estimated at 64 (`db.t4g.micro`), 144 (`db.t4g.small`), and 300 (`db.t4g.medium`). Budgets per task:
- `db.t4g.micro`: 2 tasks = 16.
- `db.t4g.small`: 2 tasks = 36; 3 tasks = 24; 4 tasks = 18.
- `db.t4g.medium`: 2 tasks = 75; 3 tasks = 50; 4 tasks = 37.

`db.t4g.micro` allows at most 2 tasks (a CloudFormation template rule refuses more). Running 3 or 4 tasks requires `db.t4g.small` or larger.

Changing `DbInstanceClass` modifies the database in place, with a few minutes of downtime on single-AZ instances. Run `SHOW max_connections;` over ECS Exec afterwards to see the real value. `DbInstanceClass` has no workflow input yet; change it with parameter overrides in `aws cloudformation deploy`, matching other stack parameter updates.

#### Deploy workflow and task count

The deploy workflow provides two scaling inputs:
- `desired_count`: `0`, `1`, or `2` (default `1`). Sets the minimum task count when autoscaling is on, or the fixed count when off.
- `max_task_count`: `keep`, `1`, `2`, `3`, or `4` (default `keep`). `keep` retains the stack's current value; push-triggered runs keep it too. `1` turns autoscaling off.

```sh
gh workflow run deploy.yml --ref main -f ref=<deployed sha> -f desired_count=1 -f max_task_count=2
```

The workflow's final check verifies running tasks:
- With autoscaling on: waits up to 5 minutes for the running count to match.
- With autoscaling off: fails if the service's desired count differs from `DesiredCount` (for example, a scale-out left over after turning autoscaling off). It tells you to run:
  ```sh
  aws ecs update-service --cluster agenticml-hosted --service agenticml-hosted --desired-count <DesiredCount>
  ```
  or redeploy with `desired_count`.

#### Manual updates and caveats

Do not rely on `aws ecs update-service` for routine scaling:
- With autoscaling on: any manual desired count set with `aws ecs update-service` is quickly overridden by the autoscaler.
- With autoscaling off: `update-service` bypasses CloudFormation. The `healthy_hosts` alarm retains the threshold from the stack's `DesiredCount`, and the next workflow deploy sets the count back to the stack's value. Prefer the workflow input.

#### Turning autoscaling on

Follow these steps in order:
1. Re-apply the bootstrap stack from your laptop with admin credentials. This adds the deploy role's Application Auto Scaling permissions and creates the ECS autoscaling service-linked role. (The package 32 capacity dashboard needs this same re-apply.) Without this step, deploying autoscaling fails with `AccessDenied` and CloudFormation rolls back.
2. Complete the two-task rolling deploy check in the roadmap Backlog.
3. For more than 2 tasks, change `DbInstanceClass` to `db.t4g.small` first.
4. Run the deploy workflow with `max_task_count=2` (or `3` or `4`):
   ```sh
   gh workflow run deploy.yml --ref main -f max_task_count=2
   ```

#### Turning autoscaling off safely

Turn autoscaling off in two steps. CloudFormation updates the service before it removes the scaling target, so an unexpected scale-out could happen in between:
1. Lower the live target to the minimum:
   ```sh
   aws application-autoscaling register-scalable-target \
     --service-namespace ecs \
     --resource-id service/agenticml-hosted/agenticml-hosted \
     --scalable-dimension ecs:service:DesiredCount \
     --min-capacity <DesiredCount> \
     --max-capacity <DesiredCount>
   ```
   Wait until the service has scaled in.
2. Deploy with `-f max_task_count=1`:
   ```sh
   gh workflow run deploy.yml --ref main -f max_task_count=1
   ```

Do toggles at low load. During the deploy that changes `MaxTaskCount`, old and new tasks run with different connection budgets.

#### What to watch

Monitor the package 32 CloudWatch dashboard and ALB metrics:
- Custom metrics: `DbConnectionsPeak`, `Rejected`, `Errors5xx`, and `Latency`.
- ALB metric: `TargetResponseTime`.

Do not watch CPU alone. Requests waiting for a database connection or lock slot do not raise CPU, so CPU-based autoscaling cannot see them.

To suspend an abusive workspace, click **Suspend** on its row on `/admin`. **Archive**
there suspends it and moves it to the Archived table (data kept; Restore brings it back).
Permanent deletion is a CLI step. Using ECS Exec instead:

```sh
aws ecs execute-command --cluster agenticml-hosted --task "$task" --container server --interactive \
  --command 'agenticml server workspaces suspend customer-one --data /data'
```

To inspect aggregate workspace usage:

```sh
aws ecs execute-command --cluster agenticml-hosted --task "$task" --container server --interactive \
  --command 'agenticml server workspaces usage --data /data'
```

To inspect server-wide totals, top workspaces, and daily usage history over ECS Exec:

```sh
aws ecs execute-command --cluster agenticml-hosted --task "$task" --container server --interactive \
  --command 'agenticml server stats --data /data'
```

`/admin` shows the same totals and growth charts.

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

Estimated monthly cost in `us-east-2` (on demand) is about **$63–70/month**
total:
- One ARM64 Fargate task (0.5 vCPU, 1 GB RAM): ~$14.40
- Application Load Balancer: ~$17–18
- Public IPv4 addresses (ALB ×2, task ×1; no NAT gateway): ~$11
- RDS PostgreSQL `db.t4g.micro` with 20 GB gp3 storage: ~$14
- EFS: ~$1–2
- S3 and ECR: ~$1–2
- CloudWatch logs, nine alarms, and custom metrics (about $1–4): ~$2–6
- AWS Backup: ~$1
- Secrets Manager: ~$0.40
- Cost alerts (Cost Explorer API queries): ~$0.60

Data transfer under 100 GB/month is free. DNS at Namecheap avoids Route 53
zone fees. The bootstrap budget stays at $100 with $10 steps; to alert earlier,
lower `MonthlyBudget` to 75 by redeploying the bootstrap stack.

Ways to scale up later:
- An extra task: about $0.025 an hour while it runs (Fargate 0.5 vCPU / 1 GB ARM plus its public IPv4 address)
- Target-tracking CloudWatch alarms: about $0.20 a month for the two alarms
- `db.t4g.small`: adds about $12 a month
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

# Package 35: deploy in your own AWS account (release 3.3)

Nov 19, 2026 · supervisor plan · status: plan ready

## Goal

A team with an AWS account gets its own AgenticML server, with sign-up,
sign-in and `agenticml sync`, from one CloudFormation template. They fill in
a short form (domain, admin email, sign-up mode) and get an `https://` URL
about 10 minutes later. There is no Firebase project to create, no container
image to build, nothing to install by hand, and nothing is sent to us.

Owner's request (Nov 19, 2026): "make things a lot simpler for onboarding,
like providing a CloudFormation script to the users who want to deploy …
target users that have an AWS environment".

Today a self-hoster who wants what app.agenticml.xyz has must set up a
Firebase project by following our private runbook, provide Postgres and S3,
and adapt `deploy/aws/cloudformation/*.yaml`. Those templates are tied to our
account: GitHub OIDC, our ECR, us-east-2 and our domain names. Self-serve
sign-up also needs Firebase (`require_open_signup` in
`src/agenticml/server/firebase.py`).

## Decisions (taken by the supervisor, recommended options; owner may change)

- **D35.1 Compute: one EC2 instance, SQLite, PyPI.** One Graviton instance
  (default `t4g.small`) in an Auto Scaling group of exactly 1, so a dead
  instance is replaced automatically. It installs
  `agenticml-lite[oidc]==<version>` from PyPI into a venv and runs the
  existing systemd unit. Data lives on a separate encrypted gp3 EBS volume
  that outlives the instance. Why: it keeps the Nov 5, 2026 decision (PyPI
  only, no published image), costs about $45–55/month instead of $60–120 for
  Fargate + RDS + S3, and SQLite covers a team (the 3.0 benchmarks). The
  scale-out variant that reuses `hosted.yaml` would need a public image, so
  it goes to Later.
- **D35.2 Sign-in: Amazon Cognito through a generic OIDC client in the
  server.** CloudFormation can create Cognito (user pool, managed login,
  email verification, password reset), and Cognito sends its own emails, so
  there is no SMTP. It is free up to 10,000 monthly active users on the Lite
  tier. The server gains a standard OpenID Connect authorization-code flow
  with PKCE (public client, so no client secret anywhere). Nothing in it is
  specific to Cognito, so Okta, Entra ID and Google Workspace also work.
  That is the first step of the Later entry "Enterprise SSO". We do not use
  ALB `authenticate-cognito` with the `alb` identity header: that header
  carries the Cognito `sub` (not an email), unknown identities get 403
  instead of sign-up, and requests on paths without the authenticate action
  could forge the header.
- **D35.3 TLS and DNS: ALB + ACM.** If the deployer gives a Route 53 hosted
  zone, the stack creates and DNS-validates the certificate and the alias
  record. Otherwise they pass an existing ACM certificate ARN and add a
  CNAME themselves. A domain is required: the Cognito callback needs a
  stable `https://` URL, and a no-domain mode through CloudFront goes to
  Later.
- **D35.4 Sign-up modes.** Cognito's own sign-up is always on. The
  AgenticML server decides who gets in, as it already does with Firebase.
  `SignUp=open` turns on `--open-signup` (every verified email gets a
  personal workspace). `SignUp=invite-only` lets in only invited emails and
  the admin email. `AllowedEmailDomains` (for example `example.com`)
  restricts open sign-up in the server and, through a small pre-sign-up
  Lambda, in Cognito, so outsiders cannot create Cognito logins either.
- **D35.5 First admin: `AdminEmail` parameter → `--admin-emails`.** The
  deployer signs up at the URL with that email and becomes server admin with
  a personal workspace. They never see `/setup` or a key.
- **D35.6 Storage of the OIDC subject.** One sign-in provider per server
  (Firebase, OIDC or identity header, never two). The OIDC subject is stored
  in the existing `accounts.firebase_uid` column as `oidc:<sub>`, so it
  cannot collide with a Firebase uid. This package has no column rename or
  migration. Code reads it through a helper named `external_uid`.
- **D35.7 Admin access: SSM Session Manager.** No SSH key and no port 22.
  The instance role has `AmazonSSMManagedInstanceCore`, so operators run
  `agenticml server …` commands through Session Manager.

## Parts

| Part | What | Coder | Size |
| --- | --- | --- | --- |
| 35.1 † | Server: OIDC sign-in, open sign-up / admin emails / invites on OIDC, `--signup-email-domains`, `[oidc]` extra | Sonnet | M–L |
| 35.2 † | Template `deploy/aws-self-hosted/agenticml.yaml`, static tests, cfn-lint in CI | Sonnet | M |
| 35.3 | Docs: website "Deploy on AWS" page, SERVER.md, deploy README, threat model, README, facts test | Antigravity; supervisor checks | S–M |
| 35.4 | Version 3.3.0; template attached to the GitHub release; Launch Stack link (owner: public S3 bucket) | Sonnet; owner tags and publishes | S |
| 35.5 | First real deploy in an AWS account (owner's go; supervisor may drive it with owner credentials) | Owner | S |

Order: 35.1 → 35.2 → 35.3 → 35.4 → 35.5. 35.1 and 35.2 touch different
files, so 35.2 can start once 35.1's flags and route names are fixed (step
35.1-1 below). 35.3 waits for package 34.3 (both edit SERVER.md and
THREAT_MODEL.md). 35.4 comes after 3.2.0 (package 34.4).

## 35.1 † Server: OIDC sign-in

Files: new `src/agenticml/server/oidc.py`; `webauth.py`, `app.py`, `cli.py`
(serve options), `firebase.py` (`require_open_signup`), `up.py`
(`install_unit` passes the new options), `db/workspaces.py` (an
`external_uid` alias over `firebase_uid`), `static/login.html` / `login.js`
(OIDC button), `pyproject.toml` (`oidc` extra = the same PyJWT +
cryptography pins as `firebase`; `hosted` unchanged); tests
`tests/test_server_oidc.py`, `testbed/tests/test_server_oidc_dashboard.py`
(ui).

Steps:

1. **Options** (fix these names first; 35.2 depends on them):
   `--oidc-issuer URL` / `AGENTICML_SERVER_OIDC_ISSUER`,
   `--oidc-client-id` / `AGENTICML_SERVER_OIDC_CLIENT_ID`,
   `--oidc-logout-url URL` / `AGENTICML_SERVER_OIDC_LOGOUT_URL` (optional;
   Cognito's `/logout` is not in its discovery document),
   `--oidc-label TEXT` / `AGENTICML_SERVER_OIDC_LABEL` (button text, default
   "Sign in"), `--signup-email-domains LIST` /
   `AGENTICML_SERVER_SIGNUP_EMAIL_DOMAINS`. Routes: `GET /auth/oidc/start`,
   `GET /auth/oidc/callback`. The issuer must be `https://` (except
   `http://127.0.0.1` / `localhost` in tests). OIDC together with Firebase
   or `--identity-header` refuses to start. Without the `[oidc]` extra it
   refuses to start with a clear `pip install` message, like
   `FIREBASE_EXTRA`.
2. **`oidc.py`**: discovery (`<issuer>/.well-known/openid-configuration`;
   the `issuer` in the document must equal the configured value; cache with
   max-age bounds as `CertificateSource` does) and JWKS by `kid` (reuse the
   capped-body, timeout, cooldown and unknown-kid-interval patterns in
   `firebase.py`; factor shared helpers out instead of copying them). Verify
   the ID token: RS256 only, `iss`, `aud` = client id (or `azp` when `aud`
   is a list), `exp`/`iat` with `LEEWAY`, `nonce`, and `token_use == "id"`
   when present (Cognito). Return the existing `Claims` with
   `uid="oidc:"+sub`, `email`, `email_verified` (accept the bool or the
   strings `"true"`/`"false"`), `name`, `provider="oidc"`. Never log tokens
   or codes.
3. **Flow**: `/auth/oidc/start?next=…&invite=…` makes `state`, `nonce` and
   a PKCE `code_verifier` (S256). It stores them server-side in the
   database settings/sessions store under a hash of `state`, with a
   10-minute expiry and single use. It sets an `HttpOnly; Secure;
   SameSite=Lax` cookie that binds the browser to `state`, and redirects
   (302) to `authorization_endpoint` with `scope=openid email profile`.
   `/auth/oidc/callback` checks that the cookie matches `state`, consumes
   the state row, exchanges the code at `token_endpoint` (POST,
   `client_id`, `code_verifier`, `redirect_uri` = `<public-url>/auth/oidc/callback`;
   5 s timeout, capped body), verifies the ID token, then runs step 4.
   `next` is only ever a same-origin path (reuse the existing
   `next`-sanitising helper). The provider's `error=` on the callback shows
   the sign-in page with a generic message.
4. **One sign-in path for Firebase and OIDC**: move the body of the
   Firebase handler after token verification (invite redeem, find-or-claim,
   admin list, open sign-up, personal workspace, audits, session cookie)
   into `_external_sign_in(handler, claims, method)`, called by both. Firebase
   behaviour must not change: the existing Firebase tests pass unchanged.
   `email_verified_for_link` trusts `email_verified` for `provider="oidc"`
   (the configured issuer is the trust root, documented in the threat
   model). Session method `"oidc"`; audit `method="oidc"`.
5. **Invites**: the invite page shows the OIDC button. The invite token
   goes into the stored state (never into the provider URL). Redeem as
   `redeem_firebase` does: the email must match and be verified. Invite
   links on OIDC servers use the `#token=` fragment form, as with Firebase,
   and the member "Invite link" behaves as on Firebase servers (24.11c):
   check what that rule protects and apply the same answer.
6. **Open sign-up and admin emails**: `require_open_signup` accepts
   Firebase **or** OIDC. `--admin-emails` works with OIDC, and its startup
   warning now says "ignored without Firebase or OIDC".
   `--signup-email-domains`: open sign-up (not invites, not admin list)
   creates an account only for an email whose domain matches exactly
   (case-insensitive, no subdomain wildcard). Others get the existing
   `no_account` answer. It also applies to Firebase servers.
7. **Sign-out**: when `--oidc-logout-url` is set, sign-out clears the
   session, then redirects to it with `client_id` and
   `logout_uri=<public-url>/login` (Cognito's parameters; document that
   other providers may need `post_logout_redirect_uri`, and accept a full
   URL template if that is simpler). Without it, only the local session
   ends.
8. **Login page**: with OIDC on, `/login` shows one button (label from
   `--oidc-label`) linking to `/auth/oidc/start`, plus the admin-key
   fallback only where it exists today. CSP needs no new `connect-src`
   (top-level navigation only). It must not overflow at 375px.
9. **Other surfaces**: `whoami`/settings show sign-in method "OIDC"; the
   `/admin` server-admin list counts OIDC-linked accounts as able to sign in
   (`can_sign_in` already checks `firebase_uid`); `accounts delete` prints
   "delete the user in your identity provider" for `oidc:` uids instead of
   the Firebase console hint.

Edge cases: state replay (second callback with the same state → refused);
callback without cookie or with a mismatched cookie; expired state; nonce
mismatch; token with `alg=none`/HS256; `aud` list without our client;
unverified email + open sign-up (→ `email_unverified`); provider down
(discovery/JWKS/token endpoint) → 503 page, not 500; an `oidc:` uid that
collides with nothing; switching a server from Firebase to OIDC leaves old
accounts linkable by verified email (find-or-claim) — test it; `next`
pointing off-site.

Tests: a local fake OIDC provider (stdlib HTTP server: discovery, JWKS from
a generated RSA key, token endpoint that checks PKCE) used by unit and UI
tests. Cases: happy path open sign-up → personal workspace; invite-only
denies a stranger; admin email becomes server admin; domain restriction;
every edge case above; Firebase regression suite unchanged; ui test of
`/login` and `/invite` at 375px.

## 35.2 † Template

Files: `deploy/aws-self-hosted/agenticml.yaml`, `deploy/aws-self-hosted/README.md`
(short; points to the docs page), `tests/test_deploy_self_hosted.py`,
`.github/workflows/test.yml` (`deploy-check` also lints the new template
and runs on changes under `deploy/aws-self-hosted/`).

Parameters (grouped with `AWS::CloudFormation::Interface`, with labels):

- `DomainName` (required, e.g. `aml.example.com`), `HostedZoneId`
  (optional), `CertificateArn` (optional). Exactly one of the last two is
  required; a rule (`Rules`) enforces it.
- `AdminEmail` (required), `SignUp` (`invite-only` default | `open`),
  `AllowedEmailDomains` (optional, comma-separated).
- `AgenticmlVersion` (default: the release the template ships with;
  pattern `^\d+\.\d+\.\d+$`).
- `InstanceType` (default `t4g.small`; allowed list of Graviton types),
  `DataVolumeSize` GiB (default 50), `BackupRetentionDays` (default 14).
- `Scheme` (`internet-facing` default | `internal`), `AllowedCidr`
  (default `0.0.0.0/0`; with `internal`, the deployer sets the VPC or
  corporate range).
- `VpcId` + `PublicSubnetIds` (both optional; empty = the stack creates a
  VPC with two public subnets in two AZs, an internet gateway and routes).
- `WorkspaceStorageGB` (default 100), `PeoplePerWorkspace` (default 50):
  written to the limits file (open sign-up requires `--limits`).

Resources:

- Optional VPC (condition `CreateVpc`).
- Security groups: ALB ingress 443 (and 80 for redirect) from
  `AllowedCidr`; instance ingress 8080 only from the ALB group; egress
  open (PyPI, Cognito, OS updates). No port 22.
- ALB (scheme from the parameter), HTTPS listener with
  `ELBSecurityPolicy-TLS13-1-2-2021-06`, HTTP listener redirect to HTTPS,
  target group HTTP 8080 with health check `/healthz` (as `hosted.yaml`),
  `deregistration_delay` 30 s, drop invalid header fields on, access logs
  off by default.
- ACM certificate with DNS validation in `HostedZoneId` (condition) and a
  Route 53 alias A/AAAA record (condition).
- Cognito: user pool (email as username, `AutoVerifiedAttributes: [email]`,
  password policy ≥ 12 chars, `AccountRecoverySetting` email, deletion
  protection on, Lite tier), user pool domain (prefix from the stack name
  plus a short account hash; an optional `CognitoDomainPrefix` parameter
  overrides it), public app client (`GenerateSecret: false`,
  `AllowedOAuthFlows: [code]`, scopes `openid email profile`,
  `CallbackURLs: [https://<DomainName>/auth/oidc/callback]`,
  `LogoutURLs: [https://<DomainName>/login]`,
  `PreventUserExistenceErrors: ENABLED`, token validity defaults). Optional
  pre-sign-up Lambda (condition `RestrictDomains`; inline Python under 4096
  bytes; rejects emails outside `AllowedEmailDomains`) with its invoke
  permission.
- Data volume: `AWS::EC2::Volume` gp3, encrypted, in the first subnet's AZ,
  `DeletionPolicy: Snapshot`, `UpdateReplacePolicy: Snapshot`, tag
  `agenticml:data`. Daily snapshots with a DLM lifecycle policy (retain
  `BackupRetentionDays`) and its role.
- Instance role: `AmazonSSMManagedInstanceCore`, plus
  `ec2:AttachVolume`/`DescribeVolumes` scoped to the data volume.
- Launch template: AL2023 arm64 via the public SSM parameter, IMDSv2
  required (hop limit 1), root volume encrypted, no key pair, UserData
  below. Auto Scaling group min = max = desired = 1, only in the data
  volume's subnet, ELB health checks, grace 300 s, `UpdatePolicy`
  `AutoScalingRollingUpdate` with `MinInstancesInService: 0`,
  `MaxBatchSize: 1` (never two writers on one SQLite store; a
  version change is a stack update with ~3–5 minutes of downtime). The
  `AgenticmlVersion` sits in the UserData so a change rolls the instance.
- UserData (bash, `set -euo pipefail`, log to
  `/var/log/agenticml-bootstrap.log`): install `python3.11`; attach the
  data volume (retry until it is `available`, since the previous instance
  may still be detaching); find the device by the volume id in
  `/dev/disk/by-id`; `mkfs.xfs` **only if** `blkid` shows no filesystem;
  mount at `/var/lib/agenticml-server` through fstab by UUID with
  `nofail`; create the `agenticml` user; venv install
  `agenticml-lite[oidc]==${AgenticmlVersion}`; write the limits file and
  `/etc/agenticml/server.env` (public URL, OIDC issuer
  `https://cognito-idp.<region>.amazonaws.com/<pool id>`, client id, logout
  URL `https://<domain prefix>.auth.<region>.amazoncognito.com/logout`,
  admin email, open sign-up, sign-up domains, limits, trusted proxy = the
  VPC CIDR); write the systemd unit from the package
  (`deploy/systemd/agenticml-server.service` content, or `server up
  --install`'s generator if it can be used without Tailscale; decide by
  reading `up.py`, and report the choice); `systemctl enable --now`.
  No secrets: nothing in UserData or the env file is secret.
- Outputs: `Url`, `FirstSteps` ("Sign up at <Url> with <AdminEmail>"),
  `SyncCommand` (`pip install agenticml-lite && agenticml login --host
  <Url>`), `DataVolumeId`, `CognitoUserPoolId`, `SessionManagerCommand`.

Constraints: the template stays under 51,200 bytes (as for the existing
templates, so `aws cloudformation deploy --template-file` works without an
S3 bucket); every region with Cognito managed login and Graviton works (no
hard-coded region or account); `cfn-lint==1.60.0` clean.

Tests (`tests/test_deploy_self_hosted.py`, reusing the YAML loader from
`test_deploy_cloudformation.py`): size limit, parameter set and defaults,
the `Rules` for domain or certificate, IMDSv2 required, volume encrypted
with snapshot deletion policy, ASG max 1 with `MinInstancesInService: 0`,
no SSH ingress, instance port reachable only from the ALB group, callback
URL path equals the server's route (import the route constant), env var
names in UserData exist in the server's option table (parse `cli.py`
options or a shared constant), UserData passes `bash -n` (and
`shellcheck` when installed), inline Lambda ≤ 4096 bytes, and a
`git grep` style check that nothing names our account, ECR, `agenticml.xyz`
or us-east-2.

## 35.3 Docs (Antigravity drafts, supervisor checks against the code)

- Website: new page `server/deploy-on-aws.md`: what you get, cost table,
  Launch Stack button (35.4) and CLI command, parameter table, first
  sign-in, inviting people, connecting training machines (`login`,
  `sync --follow`), upgrades (stack update with a new version), backups and
  restore from a snapshot, deleting the stack (the snapshot stays), private
  `internal` scheme, troubleshooting (bootstrap log through Session Manager,
  health check, Cognito email limit of 50/day without SES). Add it to the
  sidebar and to the home "On-prem server" tile.
- `docs/SERVER.md`: OIDC options in the configuration table and a section
  "Sign-in with an OIDC provider (Cognito, Okta, Entra ID)".
- `docs/THREAT_MODEL.md`: the issuer as trust root, PKCE/state/nonce, open
  sign-up on a public pool, the pre-sign-up domain filter, single writer.
- README: one line and a link. `tests/test_website_facts.py`: the template
  path exists and the default version matches the package version.

## 35.4 Release

Version 3.3.0. The release workflow attaches `agenticml.yaml` to the GitHub
release. Owner step: a public-read S3 bucket (for example
`agenticml-templates`) holding `self-hosted/<version>/agenticml.yaml` and
`self-hosted/latest/agenticml.yaml`, so the docs can link
`https://console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?templateURL=…`.
Until the bucket exists, the docs say "download the template, then upload
it in the CloudFormation console".

## 35.5 First real deploy (owner)

In a clean account or region: launch with `SignUp=open` and a Route 53
zone; sign up as the admin email → server admin plus personal workspace;
a second email signs up → its own workspace; an email outside
`AllowedEmailDomains` is refused by Cognito; create a key and
`sync --follow` one run from a laptop; dashboard shows it; stack update to a
new version → same data afterwards; terminate the instance by hand → the
ASG replaces it and data is intact; restore drill from a DLM snapshot;
delete the stack → the final snapshot exists. Record cost after 7 days.

## Constraints and conventions

- The base install stays dependency-free; OIDC needs the extra.
- The local workflow, the SDK and the sync protocol do not change. A 3.2
  client syncs to a 3.3 server.
- Firebase servers (our hosted service) behave exactly as before.
- Security-sensitive (†): 35.1 and 35.2 need a second read-only review (the
  supervisor, per the standing rule while Codex and Cursor are disabled).
- 375px with zero horizontal overflow on every changed page.

## Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- `.venv/bin/python -m pytest -q -m ui` from `testbed/`.
- `cfn-lint deploy/aws-self-hosted/agenticml.yaml`.
- Docs build and the 375px overflow check (`website`).

## Done when

- A deployer with an AWS account and a Route 53 domain launches one
  template, signs up with the admin email and syncs a run, with no other
  setup (35.5 recorded in the Backlog/roadmap).
- Every test above passes, and the Firebase suites are unchanged and green.
- The docs page exists and the facts test covers it.

## Later (not in this package)

- Scale-out variant (Fargate + RDS + S3, reusing `hosted.yaml`); needs a
  public image, which reverses the Nov 5, 2026 decision.
- No-domain mode (CloudFront default certificate in front of the ALB).
- SES for Cognito email above 50/day; CloudWatch agent for logs and
  metrics (`--metrics cloudwatch` from package 32 could be a parameter).
- Marketplace listing.

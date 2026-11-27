---
title: Releases
description: Versions and release history for agenticml-lite.
---

This page lists the releases and version history for agenticml-lite.

## Which version should I use?

Use this table to choose the version for your setup:

| Path | Version | Install command |
| --- | --- | --- |
| Local tracking only | 3.3.1 from PyPI | `pip install agenticml-lite` |
| Self-hosted server | 3.3.1 from PyPI | `pip install 'agenticml-lite[system]'` |
| Hosted service | 3.3.1 from PyPI | `pip install 'agenticml-lite[system]'` |

`pip install agenticml-lite` installs 3.3.1, which includes the server and hosted service support. To stay on 1.0.0, run `pip install agenticml-lite==1.0.0`. Version 1.0.0 supports local tracking only. It cannot sync to a 2.x or 3.0 server.

A 2.2 client has not been checked against the hosted service. Use 3.0 or newer.

### Check your version

To check which version you have installed:

```bash
pip show agenticml-lite
```

This works on every version. The `agenticml --version` command works from 3.0. On 1.0.0 and 2.x, use `pip show`.

---

## 3.3.1

**Status:** Released Nov 23, 2026 (tag `v3.3.1`). On PyPI.

This release changes the dashboard and its API only. Upgrade the machine that runs the server or `agenticml ui`. Training machines can stay on 3.3.0. Nothing in the store or the sync protocol changes.

Install from PyPI:

```bash
pip install -U 'agenticml-lite[system]'
```

### Fixes

- **Lineage diff against a parent from another machine:** When a run and its parent were created on different machines, the dashboard showed every file as added, because the parent commit was not stored with the run. The dashboard now looks up the parent run's snapshot in the run's own project repository. If the parent's snapshot is not there, the inspector says "parent code not available". `/api/runs/<id>/lineage` has a new `parent_source` field (`meta`, `ref` or `null`) that tells which one was used. See [FORMAT.md](https://github.com/agenticML/agenticml-lite/blob/main/docs/FORMAT.md).
- **Word spacing in small sans text:** Labels such as "Clip outliers on y-axis" no longer run together.
- **Run page header:** An empty owner or parent no longer shows the text "null".
- **Runs without a snapshot:** The lineage inspector and the run page say "no snapshot" or "parent has no snapshot" instead of "code changed".
- **Lineage inspector:** "· gen N" stays on one line.

---

## 3.3.0

**Status:** Released Nov 20, 2026 (tag `v3.3.0`). On PyPI.

This release includes the 3.1 and 3.2 work below, which was not published on its own.

Install from PyPI:

```bash
pip install 'agenticml-lite[system]'
```

### Deploy in your own AWS account

Deploy an AgenticML server in your own AWS account with a single CloudFormation template. See [Deploy on AWS](/server/deploy-on-aws/).

- **CloudFormation template:** One template (`deploy/aws-self-hosted/agenticml.yaml`) creates a Graviton EC2 instance running SQLite on an encrypted EBS data volume, an HTTPS Application Load Balancer with an ACM certificate, and Amazon Cognito for user management.
- **OIDC sign-in:** The server supports OpenID Connect (Cognito, Okta, Entra ID) using authorization code flow with PKCE and public clients, via the `[oidc]` extra.
- **Sign-up email domains:** `--signup-email-domains` limits open sign-up and personal workspace provisioning to approved email domains (exact match).
- **Template on the release:** The [GitHub release](https://github.com/agenticML/agenticml-lite/releases/tag/v3.3.0) has the template as `agenticml.yaml`.

---

## 3.2.0

**Status:** Not published on its own; included in 3.3.0.

Version 3.1 was not released separately either. Install 3.3.0 to get this work.

### Private networks

Run the server and dashboard inside a private network, with no Tailscale, behind a TLS proxy that uses an internal certificate. See [Private network](/server/private-network/).

- **CA bundle:** `agenticml login`, `sync` and `whoami` take `--ca-bundle PATH`. `login --ca-bundle` saves the bundle for that host. `AGENTICML_CA_BUNDLE` sets one for every https host that has no saved bundle.
- **Plain http per host:** `agenticml login --insecure` to a non-local `http://` host remembers that consent for that exact host only. Other hosts are still refused.
- **Clear TLS errors:** A failed certificate check, or an `https://` URL that points at a port that does not speak TLS, fails at once with a hint instead of retrying. Other handshake failures are retried a few times, then show a "TLS handshake failed" hint. A redirect error says to use the final `https://` URL.
- **Deploy kit:** `deploy/private-network/` has a Caddyfile, an nginx configuration, an environment file for the server unit, and a step-by-step README. The server and every training machine need `agenticml-lite>=3.2`.

### Usage history and scale

- **Usage history:** The server keeps one row of totals per day (accounts, active accounts, workspaces, stored and uploaded bytes, runs). `/admin` charts them, and `agenticml server stats` prints them. This works on a self-hosted server too.
- **Capacity metrics:** With `--metrics cloudwatch`, the server sends request, error, latency, load and connection metrics to Amazon CloudWatch once a minute. They are off by default. The AWS deployment adds alarms and a CloudWatch dashboard.
- **Autoscaling for the hosted service:** The ECS service can scale on CPU between `DesiredCount` and a new `MaxTaskCount` (1 to 4). The default, 1, keeps autoscaling off. Each task gets a database connection budget sized by the new `DbInstanceClass` parameter.

---

## 3.0.0

**Status:** Released Nov 17, 2026 (tag `v3.0.0`). On PyPI. The hosted service is in public preview.

Install from PyPI:

```bash
pip install 'agenticml-lite[system]'
```

### Features

- **Database storage:** Server state lives in a SQL database. SQLite is used for single-process self-hosted deployments. PostgreSQL is used for multi-process or hosted deployments.
- **Workspaces:** All runs, API keys, and memberships belong to isolated workspaces on a single hostname. People have a role in each workspace. Dashboard views run at `/w/<slug>/`.
- **Workspace switcher:** Switch between workspaces from the dashboard.
- **Hosted service:** A service on AWS at `https://app.agenticml.xyz`. It is a public preview, run on a best-effort basis with no SLA. See the [hosted quickstart](/hosted/quickstart/).
- **Firebase sign-in:** Sign in to the hosted service with Google or email and password.
- **Self-serve sign-up:** See [Sign-up](/hosted/#sign-up) for whether open sign-up is on.
- **Free plan limits (as of Nov 13, 2026):** 5 GB storage, 2 GB uploads per UTC day, 600 requests per minute per workspace, 5 people (members plus pending invites), and 1 free workspace per account. Over the storage, daily upload or people limit the server answers 403. Over the request rate it answers 429 with `Retry-After`.
- **Several server processes and S3 objects:** PostgreSQL deployments can run more than one server process. Content-addressed objects (code snapshots, media, copied artifacts) can be stored in Amazon S3. The hosted service uses both.
- **Metric summaries:** The server keeps a summary of each metric series in the database, so the runs list can sort and filter by metric.
- **Automatic data conversion:** A 2.x data directory converts to the 3.0 database on first start. Converted runs belong to the `default` workspace.
- **Client support window:** By default the server requires client version 2.2 or newer. Older clients get HTTP 426 and must update.

### Documentation

- [Server overview](/server/)
- [Installation and deployment](/server/install/)
- [Authentication and keys](/server/keys/)
- [Syncing runs](/server/sync/)
- [Backups and restore](/server/backups/)
- [Upgrades and troubleshooting](/server/upgrade/)
- [Hosted service](/hosted/), [Hosted quickstart](/hosted/quickstart/), [Workspaces and team](/hosted/workspaces/), [Service status](/hosted/status/)

---

## 2.2

**Status:** Shipped in the 2.2 code on `main`; never published on its own; included in 3.0.

Version 2.2 (commit `bd0d499` on `dev`) reached `main` on Nov 7, 2026, as merge `e0916d7`. It was never tagged and never published on PyPI.

### Features

- **Users and roles:** Added `admin`, `member`, and `viewer` roles. Admins manage users and keys. Members sync runs and edit notes. Viewers have read-only access.
- **Per-user keys:** API keys belong to individual users. Runs record user ownership in `remote.json`.
- **Dashboard sign-in:** Sign in with password credentials and one-time invite links. Supports single sign-on through a trusted identity proxy (`x-amzn-oidc-identity`, `Tailscale-User-Login`, or `X-Forwarded-Email`).
- **Audit log:** Append-only log at `<data>/server/audit.jsonl` records logins, user changes, key changes, and note edits.
- **Team administration:** Dashboard page for admins to manage users, roles, and keys.

### Documentation

If you run 2.2 from Git, the 3.0 server pages do not describe your server. The 2.2 documentation stays in the repository. See the [2.2 documentation tree on GitHub](https://github.com/agenticML/agenticml-lite/tree/bd0d499/website/src/content/docs).

---

## 2.1

**Status:** Shipped in the 2.2 code on `main`; never published on its own; included in 3.0.

Version 2.1 was never tagged and never published on PyPI.

### Features

- **Lineage replication:** The client uploads Git bundles of shadow commits. This enables code diffs between runs in the server dashboard.
- **By-reference artifacts:** Artifacts logged with `copy=False` can be uploaded by reference using `agenticml sync --artifacts`.
- **Operator commands:** `agenticml server status` inspects runs, storage, and keys. `agenticml server verify` validates file sizes and re-hashes objects.
- **Server up command:** `agenticml server up` provisions an internet-accessible server through Tailscale Funnel.

---

## 2.0

**Status:** Shipped in the 2.2 code on `main`; never published on its own; included in 3.0.

Version 2.0 was merged to `dev` in pull requests from Nov 3 to Nov 5, 2026, and reached `main` with 2.2 on Nov 7, 2026. It was never tagged and never published on PyPI.

### Features

- **Self-hosted server:** `agenticml server serve --data DIR` runs a central sync server and web dashboard.
- **Sync protocol v2:** Byte-for-byte run replication over HTTP. Interrupted syncs resume where they stopped.
- **Background sync:** `agenticml sync --follow` keeps syncing active and new runs in the background.
- **Server dashboard:** Web interface displays runs, charts, lineage, and run sync status. Includes key-based login and the Keys management page.
- **Systemd units:** Example unit files for running the server and background sync client as system services.
- **Client commands:** `agenticml login --host URL`, `agenticml whoami`, and `agenticml logout` work against the new server.

### Breaking changes

- **Old sync removed:** Sync to the old hosted service (`api.agenticml.xyz`) was removed.
- **No default host:** `agenticml login` needs `--host` the first time.
- **Python 3.9 dropped:** Requires Python 3.10 or newer.
- **No Docker deployment:** The server ships only as a Python package. Docker images and compose files were removed.
- **POSIX only:** The server requires POSIX file locking (`fcntl`). It runs on Linux and macOS. Windows runs the client and local `agenticml ui`.

---

## 1.0.0

**Status:** Released Nov 1, 2026 (tag `v1.0.0`). On PyPI.

Version 1.0.0 was the first release on PyPI. Version 3.0.0 replaces it.

### Features

- **Local-first tracking:** Log machine learning experiments directly to disk in `~/.agenticml`. No server or database required.
- **Flexible logging:** Track runs from Python with `agenticml.init()`, `agenticml.log()`, and `agenticml.finish()`, or wrap training scripts without code changes using `agenticml run train.py`.
- **Automatic capture:** Captures command-line arguments, Git code state and PyTorch components. `agenticml run` also mirrors TensorBoard summaries.
- **Local dashboard:** `agenticml ui` serves a web dashboard at `http://127.0.0.1:6006` with run tables, metric charts, lineage graphs, and code diffs.
- **CLI tools:** Manage the local store with `agenticml ls`, `agenticml lineage`, `agenticml du`, and `agenticml gc`.
- **Distributed training:** Multi-GPU support with rank-0 logging.

### Retired

- **Old hosted sync retired:** The `login` and `sync` commands in 1.0.0 default to `https://api.agenticml.xyz`, a service that has been retired. Sync from 1.0.0 no longer works, and old guides that use that host no longer apply. Version 1.0.0 cannot sync to a 2.x or 3.0 server.

### Documentation

- [Installation](/getting-started/installation/)
- [Quickstart](/getting-started/quickstart/)
- [Python SDK](/guides/python-sdk/)
- [Zero-code tracking](/guides/zero-code/)
- [Local dashboard](/guides/dashboard/)
- [Storage reference](/reference/storage/)
- [Configuration reference](/reference/configuration/)

---
title: Releases
description: Versions and release history for agenticml-lite.
---

This page lists the releases and version history for agenticml-lite.

## Which version should I use?

Use this table to choose the version for your setup:

| Path | Version | Install command |
| --- | --- | --- |
| Local tracking only | 1.0.0 from PyPI | `pip install agenticml-lite` |
| Local tracking only | 3.0 from GitHub | `pip install 'agenticml-lite[system] @ git+https://github.com/agenticML/agenticml-lite@dev'` |
| Self-hosted server | 3.0 from GitHub | `pip install 'agenticml-lite[system] @ git+https://github.com/agenticML/agenticml-lite@dev'` |
| Hosted service | 3.0 client from GitHub | `pip install 'agenticml-lite[system] @ git+https://github.com/agenticML/agenticml-lite@dev'` |

`pip install agenticml-lite` installs 1.0.0 from PyPI. It supports local tracking only. It cannot sync to a 2.x or 3.0 server.

Version 3.0 includes the server and hosted service support. It is a pre-release and is not on PyPI yet. Install it from GitHub. This command follows the `dev` branch, so what you get moves with development.

A 2.2 client has not been checked against the hosted service. Use the 3.0 client.

### Check your version

To check which version you have installed:

```bash
pip show agenticml-lite
```

This works on every version. The `agenticml --version` command works from 3.0. On 1.0.0 and 2.x, use `pip show`.

---

## 3.0

**Status:** Pre-release. The hosted service is in public preview.

Version 3.0 is on the `dev` branch. It is not tagged and is not on PyPI yet. The `pyproject.toml` version there is 3.0.0.

Install from GitHub:

```bash
pip install 'agenticml-lite[system] @ git+https://github.com/agenticML/agenticml-lite@dev'
```

### Features

- **Database storage:** Server state lives in a SQL database. SQLite is used for single-process self-hosted deployments. PostgreSQL is used for multi-process or hosted deployments.
- **Workspaces:** All runs, API keys, and memberships belong to isolated workspaces on a single hostname. People have a role in each workspace. Dashboard views run at `/w/<slug>/`.
- **Workspace switcher:** Switch between workspaces from the dashboard.
- **Hosted service:** A service on AWS at `https://app.agenticml.xyz`. It is a public preview, run on a best-effort basis with no SLA. See the [hosted quickstart](/agenticml-lite/hosted/quickstart/).
- **Firebase sign-in:** Sign in to the hosted service with Google or email and password.
- **Self-serve sign-up:** See [Sign-up](/agenticml-lite/hosted/#sign-up) for whether open sign-up is on.
- **Free plan limits (as of Nov 13, 2026):** 5 GB storage, 2 GB uploads per UTC day, 600 requests per minute per workspace, 5 people (members plus pending invites), and 1 free workspace per account. Over the storage, daily upload or people limit the server answers 403. Over the request rate it answers 429 with `Retry-After`.
- **Several server processes and S3 objects:** PostgreSQL deployments can run more than one server process. Content-addressed objects (code snapshots, media, copied artifacts) can be stored in Amazon S3. The hosted service uses both.
- **Metric summaries:** The server keeps a summary of each metric series in the database, so the runs list can sort and filter by metric.
- **Automatic data conversion:** A 2.x data directory converts to the 3.0 database on first start. Converted runs belong to the `default` workspace.
- **Client support window:** By default the server requires client version 2.2 or newer. Older clients get HTTP 426 and must update.

### Documentation

- [Server overview](/agenticml-lite/server/)
- [Installation and deployment](/agenticml-lite/server/install/)
- [Authentication and keys](/agenticml-lite/server/keys/)
- [Syncing runs](/agenticml-lite/server/sync/)
- [Backups and restore](/agenticml-lite/server/backups/)
- [Upgrades and troubleshooting](/agenticml-lite/server/upgrade/)
- [Hosted service](/agenticml-lite/hosted/), [Hosted quickstart](/agenticml-lite/hosted/quickstart/), [Workspaces and team](/agenticml-lite/hosted/workspaces/), [Service status](/agenticml-lite/hosted/status/)

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

Version 1.0.0 is the only release published on PyPI.

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

- [Installation](/agenticml-lite/getting-started/installation/)
- [Quickstart](/agenticml-lite/getting-started/quickstart/)
- [Python SDK](/agenticml-lite/guides/python-sdk/)
- [Zero-code tracking](/agenticml-lite/guides/zero-code/)
- [Local dashboard](/agenticml-lite/guides/dashboard/)
- [Storage reference](/agenticml-lite/reference/storage/)
- [Configuration reference](/agenticml-lite/reference/configuration/)

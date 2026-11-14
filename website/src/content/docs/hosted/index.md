---
title: Hosted service
description: What the agenticml-lite hosted service is, who it is for, its free plan, and what it stores.
sidebar:
  order: 1
  badge:
    text: '3.0'
    variant: tip
---

The hosted service is the agenticml server, run for you at `https://app.agenticml.xyz`. You sign in, sync runs from your training machines, and share a dashboard with your team. There is nothing to install or run on the server side.

It runs the same open-source server code as the [self-hosted server](/agenticml-lite/server/).

:::note[Public preview]
The hosted service is a public preview. It runs on a best-effort basis, with no SLA. The terms of service, acceptable use policy and privacy policy are not published yet. It runs one server task and a database in one availability zone, so an outage is possible. See the [service status](/agenticml-lite/hosted/status/) page for what has been checked and what has not.
:::

## Sign-up

Open sign-up is on as of Nov 13, 2026. Sign in with a verified Google or email account, and if you have no account yet you get one free personal workspace. If sign-up is turned off, you need an invite from a workspace admin.

## Local, self-hosted or hosted

| | Local tracking | Self-hosted server | Hosted service |
| --- | --- | --- | --- |
| Where your data lives | Files under `~/.agenticml` on your machine | Your own server (SQLite or PostgreSQL, disk or S3) | Our AWS account in `us-east-2` |
| Cost | Free | Free; you pay for your own hardware | Free plan only; no paid plan yet |
| Sign-in | None | Passwords and invite links, a trusted identity proxy, or Firebase | Google, or email and password |
| Teams | One person | Workspaces with roles | Workspaces with roles, up to 5 people on the free plan |
| Setup | `pip install agenticml-lite` | [Install 3.0 and run the server](/agenticml-lite/server/install/) | [Sign up and install the 3.0 client](/agenticml-lite/hosted/quickstart/) |

Choose local tracking if you work alone on one machine. Choose the hosted service if you want a shared dashboard and sync from several machines without running a server. Choose self-hosting if your data must stay on your own hardware.

## Free plan and limits

Every hosted workspace is on the free plan. As of Nov 13, 2026, each workspace has these limits:

| Limit | Value | When you reach it |
| --- | --- | --- |
| Storage | 5 GB of run files and objects | Uploads fail with HTTP 403 `storage limit reached`. Existing runs stay readable. |
| Uploads | 2 GB per UTC day | Uploads fail with HTTP 403 `daily upload limit reached`. They work again at 00:00 UTC. |
| Requests | 600 per minute per workspace | The server answers HTTP 429 with a `Retry-After` header. |
| People | 5 (members plus pending invites) | Inviting another person fails with HTTP 403 `people limit reached`. |
| Free workspaces | 1 per account | A second free workspace is refused. |

The dashboard warns you when storage or daily uploads pass 80% of the limit. `agenticml sync --follow` pauses on a storage or daily upload limit and tries again every 10 minutes. There is no paid plan and there are no prices. To ask for more, see [Support](#support).

## What we store and where

**Account data.** Your email address, your display name, your Firebase user ID, your workspace memberships and roles, web sessions, and audit events (sign-ins, membership changes, with the IP address of the request). Passwords are handled by Firebase, not by the agenticml server. Sync keys and session tokens are stored only as SHA-256 hashes.

**Workspace data.** Everything a sync uploads: run metadata and config, metric series and summaries, event logs, media, copied artifacts, and code snapshots with their lineage history. A run's local note (`notes.md`) is not uploaded. By-reference artifacts are uploaded only with `sync --artifacts`.

**Where.** All of it runs in AWS region `us-east-2` (Ohio): run metadata in a PostgreSQL database, run files on network file storage, and objects (code, media, copied artifacts) in S3. All three are set up to encrypt data at rest. A check of the stored objects is on the [open list](/agenticml-lite/hosted/status/).

**Backups.** The database and file storage have daily backups kept for up to 35 days, and the database also allows point-in-time recovery for 7 days. S3 keeps old object versions for 30 days. A restore has not been tested yet, so do not rely on a restore. Deleted data can stay in backups for up to 35 days.

**Staff access.** A server administrator gets "not found" for a workspace they are not a member of. Support staff see a workspace only if a workspace admin invites a support account, and that is recorded in the workspace audit log.

## Support

Use [GitHub Issues](https://github.com/agenticML/agenticml-lite/issues) and put "hosted" in the title. Issues are public, so do not post sync keys, run data or other private details. A mailbox for support is not set up yet.

## Next steps

- [Hosted quickstart](/agenticml-lite/hosted/quickstart/): sign up, make a sync key, and sync your first run.
- [Workspaces and team](/agenticml-lite/hosted/workspaces/): roles, invites, and several workspaces from one machine.
- [Service status](/agenticml-lite/hosted/status/): what is live, what is being checked, and what is still open.

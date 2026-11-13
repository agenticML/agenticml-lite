---
title: Server overview
description: Central sync server and shared web dashboard for AgenticML runs.
sidebar:
  order: 1
---

`agenticml server` provides a central synchronization target and shared web dashboard for AgenticML experiments. It runs self-hosted on your own infrastructure or as our managed multi-workspace service on AWS. To use the hosted service instead of running your own server, start with the [Hosted service](/agenticml-lite/hosted/) section.

## Architecture

- **Workspaces**: All runs, metrics, API keys, and team memberships belong to an isolated workspace on a single server hostname. Dashboard views run at `/w/<slug>/`.
- **Database storage**: Server state (accounts, workspaces, keys, sessions, audit events, run metadata, committed file sizes, and metric summaries) lives in a SQL database: SQLite for self-hosted single-process servers, or PostgreSQL for multi-process deployments.
- **Run files and objects**: Experiment files and shadow Git lineage repositories live on disk (or Amazon EFS). Content-addressed blobs (code, media, copied artifacts) live on disk or in Amazon S3.
- **Single-process vs multi-process**: SQLite deployments acquire an exclusive lock on `<data>/server/server.lock` and run as a single process. PostgreSQL deployments coordinate concurrency via database locks and support multiple server processes behind a load balancer.
- **POSIX requirement**: The server runs on Linux and POSIX hosts (Python 3.10+). (The client CLI and `agenticml ui` support Windows).
- **TLS termination**: The server speaks plain HTTP on its configured port. In production, terminate TLS using a reverse proxy such as AWS ALB, Tailscale, or Caddy.

## Topics

- [Installation and deployment](/agenticml-lite/server/install/): Install via pip, optional extras (`[postgres]`, `[s3]`, `[firebase]`, `[hosted]`), run directly or under systemd, and configure a TLS proxy.
- [Authentication and keys](/agenticml-lite/server/keys/): Workspaces, roles (`owner`, `admin`, `member`, `viewer`), workspace-bound API keys, and dashboard sessions.
- [Syncing runs](/agenticml-lite/server/sync/): Connect training machines with `agenticml login`, `sync --all`, continuous `sync --follow`, and storage limit handling.
- [Backups and restore](/agenticml-lite/server/backups/): Online consistent backups via `agenticml server backup`, SQLite snapshots, PostgreSQL dumps, and restore.
- [Upgrades and troubleshooting](/agenticml-lite/server/upgrade/): Upgrading to 3.0, automatic 2.x data migration into `default`, and troubleshooting common operational issues.

---
title: Server overview
description: Self-hosted sync server and shared web dashboard for AgenticML runs.
sidebar:
  order: 1
---

`agenticml server` provides a central synchronization target and shared web dashboard for AgenticML experiments. It runs entirely on your own infrastructure with no external vendor dependencies.

## Architecture

- **Single process**: One server process handles protocol v2 sync traffic and serves the web dashboard.
- **Single data directory**: All runs are stored in `<data>/store/` using the standard v1 store layout. Server locks and key metadata live in `<data>/server/`.
- **Single writer**: The server acquires an exclusive lock on `<data>/server/server.lock`. A second server process targeting the same data directory exits immediately with an error.
- **POSIX requirement**: The server relies on POSIX `fcntl` file locks. It runs on Linux or POSIX systems. (The client CLI and `agenticml ui` support Windows).
- **TLS termination**: The server serves plain HTTP on a single TCP port. Terminate TLS externally using a reverse proxy such as AWS ALB, Tailscale Serve, or Caddy.
- **Container image**: Published at `ghcr.io/agenticml/agenticml-server:2.0.0` (and `:latest` for stable releases), running as non-root UID/GID 10001.

## Topics

- [Installation and deployment](/agenticml-lite/server/install/): Run with Docker Compose, `tailscale serve`, or deploy to AWS ECS Fargate.
- [Authentication and keys](/agenticml-lite/server/keys/): Scopes (`sync` vs `admin`), bootstrap setup token, and key management.
- [Syncing runs](/agenticml-lite/server/sync/): Connect training machines with `agenticml login`, `sync --all`, and background `sync --follow`.
- [Backups and restore](/agenticml-lite/server/backups/): Understand directory layout, offline backups, and EFS snapshots.
- [Upgrades and troubleshooting](/agenticml-lite/server/upgrade/): Container image upgrades, Cosign signature verification, and troubleshooting.

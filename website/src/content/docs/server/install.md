---
title: Installation and deployment
description: Run the AgenticML server on a local machine or AWS ECS Fargate.
sidebar:
  order: 2
---

The server runs from an official container image:

```
ghcr.io/agenticml/agenticml-server:2.0.0
ghcr.io/agenticml/agenticml-server:latest
```

The container runs as non-root user `aml` (UID `10001`, GID `10001`), listens on port `8080`, and stores data in `/data`.

## Laptop or workstation setup

To run the server on a workstation or personal server and access it across devices, use Docker and `tailscale serve`.

### Docker Compose

`deploy/compose.yaml` binds port 8080 to localhost only (`127.0.0.1`), drops all capabilities, mounts `/tmp` as a tmpfs, and runs with a read-only root filesystem:

1. Forward port 8080 to your Tailscale network:
   ```bash
   tailscale serve --bg 8080
   ```
2. Set your Tailscale machine URL:
   ```bash
   export AGENTICML_SERVER_PUBLIC_URL=https://<machine>.<tailnet>.ts.net
   ```
3. Start the container:
   ```bash
   docker compose -f deploy/compose.yaml up -d
   ```

### Docker run

To run directly with `docker run`:

```bash
docker run -d --name agenticml-server \
  -p 127.0.0.1:8080:8080 \
  -v agenticml-data:/data \
  -e AGENTICML_SERVER_PUBLIC_URL=https://<machine>.<tailnet>.ts.net \
  --read-only \
  --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  ghcr.io/agenticml/agenticml-server:2.0.0
```

When using a host directory bind mount instead of a named volume, ensure UID 10001 owns the directory:

```bash
sudo chown -R 10001:10001 /path/to/data
```

## Cloud deployment (AWS ECS Fargate)

Production deployment templates are provided in the repository under `deploy/ecs/` (Terraform configurations and an ECS task definition).

Key components:
- **Single task**: The ECS service must maintain `desired_count = 1`, `deployment_minimum_healthy_percent = 0`, and `deployment_maximum_percent = 100`. The server holds an exclusive lock on the store, so only one task can run against the filesystem.
- **EFS storage**: Encrypted Elastic File System mounted at `/data` through an EFS access point configured with POSIX user `10001:10001` and TLS.
- **Application Load Balancer**: Internet-facing ALB terminating HTTPS (port 443) with an ACM certificate and health checks against `/healthz` on port 8080. HTTP (port 80) redirects to 443.
- **Secrets Manager**: Admin key injected as `AGENTICML_SERVER_KEY`.
- **AWS Backup**: Daily backup plan for the EFS file system with 35-day retention.
- **NAT Gateway**: Fargate tasks in private subnets require a NAT gateway or an ECR pull-through cache to pull images from `ghcr.io`.

For complete Terraform deployment steps, see `deploy/ecs/README.md` in the repository.

## Configuration options

`agenticml server serve` accepts the following options:

| Flag | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--data DIR` | `AGENTICML_SERVER_DATA` | `~/.agenticml-server` (`/data` in image) | Path to server data directory. |
| `--host HOST` | | `127.0.0.1` (`0.0.0.0` in image) | Network interface to bind. |
| `--port PORT` | | `8080` | Port to listen on. |
| `--public-url URL` | `AGENTICML_SERVER_PUBLIC_URL` | unset | Canonical public URL (e.g. `https://aml.example.com`). Required behind reverse proxies for Host/Origin checks and cookies. |
| | `AGENTICML_SERVER_KEY` | unset | Pre-configured admin API key named `env`. Stored in memory only. |
| | `AGENTICML_REMOTE_STALE_SECONDS` | `600` | Inactivity threshold before an active run is marked `stale`. |

## Next

Continue to [Authentication and keys](/agenticml-lite/server/keys/) to configure access.

---
title: Installation and deployment
description: Run the AgenticML server on Linux or POSIX hosts.
sidebar:
  order: 2
---

The server is part of the `agenticml-lite` package (Python 3.10+, POSIX only). It needs version 3.0, which is a pre-release installed from GitHub until it is on PyPI.

## Installation

Create a dedicated virtual environment and install `agenticml-lite`:

```bash
sudo python3 -m venv /opt/agenticml
sudo /opt/agenticml/bin/python -m pip install agenticml-lite
```

3.0 is not on PyPI yet, so this installs 1.0.0, which has no server. Until 3.0 is published, install it from GitHub instead (this follows the `dev` branch, so it moves as development continues):

```bash
sudo /opt/agenticml/bin/python -m pip install 'agenticml-lite @ git+https://github.com/agenticML/agenticml-lite@dev'
```

### Optional server extras

Install optional extras depending on your deployment backend:

```bash
# PostgreSQL database backend
sudo /opt/agenticml/bin/pip install "agenticml-lite[postgres]"

# Amazon S3 object storage
sudo /opt/agenticml/bin/pip install "agenticml-lite[s3]"

# Firebase ID token verification
sudo /opt/agenticml/bin/pip install "agenticml-lite[firebase]"

# Full hosted bundle (PostgreSQL, S3, Firebase)
sudo /opt/agenticml/bin/pip install "agenticml-lite[hosted]"
```

While 3.0 is only on GitHub, add the source to the extra, for example `'agenticml-lite[hosted] @ git+https://github.com/agenticML/agenticml-lite@dev'`.

## Running the server

Start the server using `agenticml server serve`:

```bash
/opt/agenticml/bin/agenticml server serve \
  --data /var/lib/agenticml-server \
  --host 127.0.0.1 \
  --port 8080 \
  --public-url https://aml.example.com
```

- `--data DIR`: server data directory. Defaults to `AGENTICML_SERVER_DATA`, then `~/.agenticml-server`.
- `--database URL`: SQL database URL. Defaults to `sqlite:///<data>/server/agenticml.db` or PostgreSQL (`postgresql://...`).
- `--objects URL`: blob storage backend. Defaults to `file://` (local disk) or Amazon S3 (`s3://bucket/prefix`).
- `--host`: interface to bind (defaults to `127.0.0.1`).
- `--port`: TCP port (defaults to `8080`).
- `--public-url`: canonical public HTTPS URL (`AGENTICML_SERVER_PUBLIC_URL`).

## TLS proxy

The server serves plain HTTP over its configured port. In production or across a network, terminate TLS using a reverse proxy and configure the public URL:

### Laptop or workstation (Tailscale)

To share the server securely across your Tailscale network:

1. Forward port 8080 to your Tailscale network:
   ```bash
   tailscale serve --bg 8080
   ```
2. Set your Tailscale machine URL and start the server:
   ```bash
   export AGENTICML_SERVER_PUBLIC_URL=https://<machine>.<tailnet>.ts.net
   /opt/agenticml/bin/agenticml server serve --port 8080
   ```
   Or run `agenticml server up` to automatically expose the server via Tailscale Funnel.

### Production reverse proxy

Use Caddy, Nginx, or an AWS Application Load Balancer to terminate TLS and forward requests to `127.0.0.1:8080`. Always configure `--public-url` (or `AGENTICML_SERVER_PUBLIC_URL`) to match the public HTTPS endpoint for Host and Origin validation.

## Systemd service

For production deployments on Linux, run the server as a systemd system service using the unit provided in `deploy/systemd/agenticml-server.service`:

1. Install the package and create the dedicated service user:
   ```bash
   sudo python3 -m venv /opt/agenticml
   sudo /opt/agenticml/bin/python -m pip install agenticml-lite
   # 3.0 is not on PyPI yet; use the GitHub install shown under Installation instead
   sudo useradd --system --home /var/lib/agenticml-server --shell /usr/sbin/nologin agenticml
   sudo curl -fsSL -o /etc/systemd/system/agenticml-server.service https://raw.githubusercontent.com/agenticML/agenticml-lite/main/deploy/systemd/agenticml-server.service
   ```
   The unit is fetched from `main` because there is no 3.0 tag yet. Once 3.0.0 is released, use the tag that matches the agenticml-lite version you installed (`pip show agenticml-lite`).
2. Edit `AGENTICML_SERVER_PUBLIC_URL` in the unit (or `sudo systemctl edit agenticml-server`), then reload systemd:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now agenticml-server
   ```
3. Check status and view logs:
   ```bash
   sudo systemctl status agenticml-server
   sudo journalctl -u agenticml-server -f
   ```

The unit's `StateDirectory=agenticml-server` automatically manages `/var/lib/agenticml-server` owned by the `agenticml` user.

## Configuration options

`agenticml server serve` accepts the following options:

| Flag | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--data DIR` | `AGENTICML_SERVER_DATA` | `~/.agenticml-server` | Path to server data directory. |
| `--database URL` | `AGENTICML_SERVER_DATABASE_URL` | `sqlite:///<data>/server/agenticml.db` | Server database connection URL. |
| `--objects URL` | `AGENTICML_SERVER_OBJECTS` | `file://` | Object store backend (`file://` or `s3://bucket/prefix`). |
| `--host HOST` | | `127.0.0.1` | Network interface to bind. |
| `--port PORT` | | `8080` | Port to listen on. |
| `--public-url URL` | `AGENTICML_SERVER_PUBLIC_URL` | unset | Canonical public URL (e.g. `https://aml.example.com`). |
| `--trusted-proxy CIDR` | `AGENTICML_SERVER_TRUSTED_PROXIES` | unset | Trust proxy CIDR for `X-Forwarded-For` and identity headers. |
| `--identity-header PRESET` | `AGENTICML_SERVER_IDENTITY_HEADER` | unset | Honour proxy identity header (`alb`, `tailscale`, `oauth2-proxy`). |
| `--firebase-project ID` | `AGENTICML_SERVER_FIREBASE_PROJECT` | unset | Firebase project ID for token verification. |
| `--firebase-api-key KEY` | `AGENTICML_SERVER_FIREBASE_API_KEY` | unset | Firebase web API key. |
| `--firebase-auth-domain HOST` | `AGENTICML_SERVER_FIREBASE_AUTH_DOMAIN` | unset | Firebase auth domain. |
| `--limits FILE` | `AGENTICML_SERVER_LIMITS` | unset | JSON file defining workspace usage limits. |
| `--open-signup` | `AGENTICML_SERVER_OPEN_SIGNUP` | off | Allow verified Firebase sign-in to create an account and workspace. |
| `--admin-emails LIST` | `AGENTICML_SERVER_ADMIN_EMAILS` | unset | Comma-separated emails granted server admin at their next verified Firebase sign-in. Ignored without Firebase. |
| `--min-client-version X.Y` | `AGENTICML_SERVER_MIN_CLIENT_VERSION` | `2.2` | Reject `agenticml-lite` clients older than `X.Y` on `/v2` requests with HTTP 426. |

Two more environment variables apply to S3 object storage:

- `AGENTICML_SERVER_S3_ENDPOINT_URL`: custom S3 endpoint, for MinIO, LocalStack or a VPC endpoint.
- `AGENTICML_SERVER_S3_KMS_KEY_ID`: AWS KMS key ID or ARN. Uploads then use `aws:kms` encryption instead of `AES256`.

## Next

Continue to [Authentication and keys](/agenticml-lite/server/keys/) to configure access and workspaces.

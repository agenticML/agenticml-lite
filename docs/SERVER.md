# AgenticML Server

`agenticml server` is a self-hosted synchronization server and shared web dashboard for AgenticML runs.

## Architecture

The server runs as a single process backed by a single data directory.

- **Storage**: The server stores experiment runs in `<data>/store/` using the standard v1 store layout described in [FORMAT.md](FORMAT.md).
- **Single writer**: The server process acquires an exclusive file lock on `<data>/server/server.lock`. If another server process attempts to start against the same data directory, it exits with:
  ```
  another agenticml server is using <data>
  ```
- **POSIX requirement**: The server relies on POSIX `fcntl` file locking. It runs on Linux and POSIX-compliant hosts. It does not run directly on Windows (the client CLI and `agenticml ui` support Windows).
- **Network and TLS**: The server serves plain HTTP over a single TCP port. In production, run it behind a TLS-terminating reverse proxy such as AWS Application Load Balancer (ALB), Tailscale Serve, or Caddy.

## Container image

The official multi-architecture container image (`linux/amd64`, `linux/arm64`) is published to GitHub Packages:

```
ghcr.io/agenticml/agenticml-server:2.0.0
ghcr.io/agenticml/agenticml-server:latest
```

Image properties:
- **User**: Runs as non-root user `aml` (UID `10001`, GID `10001`).
- **Data volume**: Declares `/data` as a volume mount point.
- **Port**: Exposes TCP port `8080`.
- **Entrypoint**: `["agenticml", "server", "serve"]`.
- **Default CMD**: `["--data", "/data", "--host", "0.0.0.0", "--port", "8080"]`.
- **Environment**: Sets `AGENTICML_SERVER_DATA=/data`.
- **Health check**: Built-in container health check queries `http://127.0.0.1:8080/healthz` every 30 seconds using Python's `urllib`.
- **Shutdown signal**: `SIGTERM` initiates a graceful shutdown.

### Verifying image signatures

Server images are signed keylessly with Cosign using GitHub Actions OpenID Connect (OIDC). Verify an image before running it:

```bash
cosign verify \
  --certificate-identity-regexp '^https://github\.com/agenticML/agenticml-lite/\.github/workflows/image\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/agenticml/agenticml-server:2.0.0
```

Replace `2.0.0` with the specific tag or `@sha256:<digest>`.

## Laptop setup

Use Docker and `tailscale serve` to run the server on a workstation or personal server and share it securely over a Tailscale private network.

### Docker Compose

`deploy/compose.yaml` binds the server to localhost (`127.0.0.1:8080`), drops all capabilities, mounts `/tmp` as a tmpfs, and enforces `read_only: true`.

1. Publish port 8080 to your Tailscale network:
   ```bash
   tailscale serve --bg 8080
   ```
2. Note your Tailscale machine domain (for example `https://workstation.tailnet-xyz.ts.net`) and export it:
   ```bash
   export AGENTICML_SERVER_PUBLIC_URL=https://<machine>.<tailnet>.ts.net
   ```
3. Start the server:
   ```bash
   docker compose -f deploy/compose.yaml up -d
   ```

### Docker run

To run the container directly without Compose:

```bash
docker run -d \
  --name agenticml-server \
  -p 127.0.0.1:8080:8080 \
  -v agenticml-data:/data \
  -e AGENTICML_SERVER_PUBLIC_URL=https://<machine>.<tailnet>.ts.net \
  --read-only \
  --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  ghcr.io/agenticml/agenticml-server:2.0.0
```

Named volumes (such as `agenticml-data`) are initialized with ownership set to UID `10001`. If you use a host bind mount instead of a named volume, set ownership before starting the container:

```bash
sudo chown -R 10001:10001 /path/to/host/data
```

## Cloud deployment (AWS ECS Fargate)

A complete production deployment example is in [deploy/ecs/README.md](../deploy/ecs/README.md).

Architecture on AWS:
- **Compute**: ECS Fargate task running `ghcr.io/agenticml/agenticml-server:2.0.0` with 0.5 vCPU and 1 GiB memory.
- **Single task constraint**: The ECS service must be configured with `desired_count = 1`, `deployment_minimum_healthy_percent = 0`, and `deployment_maximum_percent = 100`. The server holds an exclusive lock on EFS; two tasks cannot share the filesystem.
- **Storage**: AWS Elastic File System (EFS) encrypted with an access point enforcing POSIX user `10001:10001` mounted at `/data` with TLS encryption enabled.
- **Ingress**: Internet-facing Application Load Balancer terminating HTTPS (port 443) with an ACM certificate and redirecting HTTP (port 80) to HTTPS. Target group checks `/healthz` on port 8080.
- **Credentials**: Administrator key generated and stored in AWS Secrets Manager, injected via `AGENTICML_SERVER_KEY`.
- **Backups**: AWS Backup plan performing daily backups of EFS with 35-day retention.
- **Network**: Private subnets require a NAT gateway (or an ECR pull-through cache mirror) to pull the image from `ghcr.io`.

## Authentication and keys

All API and dashboard requests (except `GET /healthz`) require authentication.

### Key scopes

Keys are issued with one of two scopes:

| Scope | Allowed operations | Intended use |
| --- | --- | --- |
| `sync` | Call `/v2` endpoints (push runs, upload objects, read run state). Cannot sign in to the web dashboard. | Training machines and unattended worker nodes. |
| `admin` | Everything `sync` can do, plus sign in to the web dashboard, issue keys, and revoke keys. | Human operators and administrators. |

### Bootstrap: First admin key

When a server starts with no keys in `<data>/server/keys.json` and `AGENTICML_SERVER_KEY` is unset, it generates a single-use bootstrap setup token in memory and logs:

```
No admin key yet. Open <public-url or http://host:port>/setup?token=<token> to create one, or run: agenticml server keys create --name admin --scope admin
```

To retrieve the setup link:
- Docker: `docker logs <container-name>`
- Docker Compose: `docker compose -f deploy/compose.yaml logs server`
- AWS ECS: Check the CloudWatch log group `/ecs/agenticml-server`

Open the `/setup?token=<token>` link in a browser. It prompts for a key name, generates an admin key, displays it once, and signs the browser in.

The setup token is stored only in memory and becomes invalid once used or after a server restart. After an admin key exists, `/setup` returns 404.

### Environment admin key

You can pre-configure an admin key via environment variable:

```bash
export AGENTICML_SERVER_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
```

Rules for `AGENTICML_SERVER_KEY`:
- Named `env` on the server.
- Stored only in memory; never written to `keys.json`.
- Cannot be revoked from the Keys web page. To revoke it, remove the environment variable and restart the server.
- Must consist of 32 to 256 allowed ASCII characters (`[A-Za-z0-9._~+/=-]`). The server warns at startup if the key has low entropy.

### Managing keys in the dashboard

Administrators can manage keys from the web dashboard:

1. Sign in to the dashboard at your server's public URL using an `admin` key.
2. Navigate to **Keys** in the sidebar.
3. Click **New key**, enter a descriptive name (e.g. `gpu-cluster-1`), and select the scope (`sync` is default).
4. The key is displayed once with a copy button and a pre-formatted `agenticml login` command. Store the key immediately; the server stores only its SHA-256 digest and cannot recover it.
5. The Keys table shows each key's name, scope, prefix, creation date, creator, last used timestamp, and count of owned runs.
6. Click **Revoke** next to a key to revoke it. Revocation takes effect immediately without a server restart. The last admin key cannot be revoked from the page (the error says to create another admin key first, or use the CLI). Revoking the key your own session uses signs you out.

### Managing keys with the CLI

The server CLI can create, list, and revoke keys on the server host while the server is running.

In a running container:

```bash
# Create a sync key (scope defaults to sync)
docker exec agenticml-server agenticml server keys create --name worker-1 --scope sync --data /data

# Create an admin key
docker exec agenticml-server agenticml server keys create --name operator-2 --scope admin --data /data

# List all keys
docker exec agenticml-server agenticml server keys list --data /data

# Revoke a key
docker exec agenticml-server agenticml server keys revoke worker-1 --data /data
```

`keys create` prints the plaintext key once to stdout and status information to stderr.

### Dashboard login and sessions

Signing in to the dashboard requires an `admin` key:

- Browsers send `POST /login` with JSON `{"key": "aml_..."}`. A `sync` key is rejected with HTTP `403`.
- On success, the server sets a session cookie with `HttpOnly`, `SameSite=Strict`, and `Path=/`. When `--public-url` is HTTPS, the cookie is named `__Host-aml_session` and includes `Secure`; otherwise it is named `aml_session`.
- Sessions live in memory: 12-hour idle timeout, 7-day absolute lifetime. Sessions terminate if their key is revoked or if the server restarts.
- Failed login attempts are throttled by client IP address.
- CSRF protection requires requests to include the `X-AgenticML-Request` header and an `Origin` matching `--public-url`.
- The dashboard rejects requests whose `Host` header does not match the host in `--public-url` or a loopback address (`localhost`, `127.0.0.1`, `::1`). When binding off loopback, always configure `--public-url`.

## Client setup

Configure training machines to sync experiments to the server.

### 1. Install client

```bash
pip install agenticml-lite
```

### 2. Log in

On each training machine:

```bash
agenticml login --host https://aml.example.com
```

The command prompts for the API key interactively or reads it from stdin:

```bash
echo "$AGENTICML_API_KEY" | agenticml login --host https://aml.example.com
```

Do not pass `--key` on the command line; arguments are visible in process tables (`ps`).

Credentials are saved in `~/.config/agenticml/credentials.json` with permissions `0600`.

Verify connection and permissions:

```bash
agenticml whoami
```

If the key is rejected or revoked, `agenticml whoami` exits with status code `1`.

### 3. Sync runs

Manually sync all unsynced or updated runs:

```bash
agenticml sync --all
```

To continuously follow and sync active training runs:

```bash
agenticml sync --follow
```

Properties of `--follow`:
- Implies `--all`.
- Polls for changes every `--interval` seconds (default 30 seconds).
- Commits running runs to the server at least every 60 seconds.
- Backs off exponentially up to 5 minutes when the server is unreachable.

### 4. Background service (systemd)

To run `agenticml sync --follow` as a background user daemon on Linux, install the user systemd unit described in [deploy/systemd/README.md](../deploy/systemd/README.md):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/agenticml-sync.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agenticml-sync
loginctl enable-linger $USER
```

If the key is rejected (exit code 3), the service does not restart automatically. Update credentials with `agenticml login` and restart the unit.

## Run status on server

The server tracks run lifecycle via commits recorded in `remote.json`:

| Stored status | Condition | Dashboard status |
| --- | --- | --- |
| `finished` | Client finished run | `finished` |
| `crashed` | Client reported crash | `crashed` |
| `running` | Last sync commit within `AGENTICML_REMOTE_STALE_SECONDS` (default 600 s) | `running` |
| `running` | Last sync commit older than `AGENTICML_REMOTE_STALE_SECONDS` | `stale` |

Runs uploaded with `sync --follow` commit heartbeats at least every 60 seconds while active, preventing live runs from showing as stale.

## Unsupported features in 2.0

Release 2.0 supports metric, scalar, event, media, and copied artifact synchronization over [protocol v2](PROTOCOL.md). The following features are not uploaded in 2.0:

- **Run notes (`notes.md`)**: Notes remain local to the machine where they were written.
- **By-reference artifacts**: Artifacts logged with `copy=False` are not uploaded to the server (scheduled for Release 2.1).
- **Lineage code diffs**: The server displays the lineage run graph, but Git diffs between commits are not replicated in 2.0 (the dashboard displays "code diff not available on this server"; scheduled for Release 2.1).
- **Cross-key run collision**: A run ID is owned by the key that first created it. Pushing to an existing run ID using a different key fails with an error.

## Configuration reference

### CLI options (`agenticml server serve`)

| Option | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--data DIR` | `AGENTICML_SERVER_DATA` | `~/.agenticml-server` (`/data` in Docker) | Path to server data directory. |
| `--host HOST` | | `127.0.0.1` (`0.0.0.0` in Docker) | Host interface to bind. |
| `--port PORT` | | `8080` | TCP port to listen on. |
| `--public-url URL` | `AGENTICML_SERVER_PUBLIC_URL` | unset | Canonical public URL (e.g. `https://aml.example.com`). Required behind reverse proxies for Host header checks, Origin checks, and `__Host-` cookies. Empty string is treated as unset. |
| | `AGENTICML_SERVER_KEY` | unset | Static admin API key named `env`. Stored only in memory. |
| | `AGENTICML_REMOTE_STALE_SECONDS` | `600` | Inactivity threshold before an active run is marked `stale`. |

## Data directory layout and backups

The server data directory contains two subdirectories:

```
<data>/
├── store/                 # Standard v1 store layout (see docs/FORMAT.md)
│   ├── runs/              # Individual run directories
│   │   └── <run_id>/
│   │       ├── meta.json
│   │       ├── events.jsonl
│   │       ├── scalars/
│   │       ├── media/
│   │       └── remote.json
│   └── objects/           # Content-addressed blobs (code, media, copied artifacts)
└── server/                # Server metadata and runtime locks
    ├── keys.json          # Key metadata and SHA-256 digests (mode 0600)
    ├── keys.lock          # Lock for atomic key modifications
    ├── server.lock        # Exclusive single-writer lock for server process
    └── tmp/               # Ingest staging directory (must be on same filesystem as store/)
```

### Performing backups

1. **Local or Docker deployment**: Stop the server process cleanly (`docker stop agenticml-server`) to release `server.lock` and flush pending writes, then copy the entire `<data>` directory:
   ```bash
   docker stop agenticml-server
   tar -czf agenticml-backup-$(date +%Y%m%d).tar.gz -C /path/to/data .
   docker start agenticml-server
   ```
2. **ECS / AWS EFS deployment**: Use AWS Backup with daily snapshots of the EFS filesystem.

### Restoring backups

To restore a backup, stop the server, extract the backup directory into the data volume, verify permissions (`chown -R 10001:10001`), and start the server.

## Upgrades

1. Pull the new container image version:
   ```bash
   docker pull ghcr.io/agenticml/agenticml-server:<new-version>
   ```
2. Stop the existing container to allow graceful shutdown:
   ```bash
   docker stop --time 30 agenticml-server
   ```
3. Start the new container pointing at the same data volume or directory.
4. Verify server health:
   ```bash
   curl -s http://127.0.0.1:8080/healthz
   ```

Because the server holds an exclusive lock on `<data>/server/server.lock`, the old container must be stopped before the new container starts. Downgrades are not supported. Read release notes prior to upgrading.

## Troubleshooting

### "another agenticml server is using <data>"
The error means another server process (another container, or a process on the host) has the same data directory open. Stop it (`docker ps`, `docker stop`) and start again.

### HTTP 403 ("forbidden host") on dashboard
When the server is accessed through a proxy or external hostname, the browser sends that hostname in the HTTP `Host` header. The dashboard accepts only loopback hostnames (`localhost`, `127.0.0.1`, `::1`) unless `--public-url` is configured; a refused Host receives HTTP 403 with `{"error": "forbidden host"}`. Set `AGENTICML_SERVER_PUBLIC_URL=https://<your-hostname>` in the server environment.

### `agenticml sync` exits with status 3 (`401 Unauthorized`)
When running `agenticml sync` (including `--follow` and `--watch`), the client exits with status code 3 if the server returns 401 because the API key was rejected or revoked. Run `agenticml login --host https://...` to enter a new key, or check key status on the server with `agenticml server keys list`.

### Runs appear as "stale" in dashboard
A run appears stale when its status in `remote.json` is `running` but no commit has occurred for longer than `AGENTICML_REMOTE_STALE_SECONDS` (default 600 seconds). Ensure training jobs run `agenticml sync --follow` to send heartbeats, or check network connectivity between the training node and the server.

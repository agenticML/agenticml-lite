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

## Installation and running

Install `agenticml-lite` into a virtual environment on a POSIX host (Python 3.10+):

```bash
sudo python3 -m venv /opt/agenticml
sudo /opt/agenticml/bin/python -m pip install agenticml-lite
```

Run directly (a venv anywhere the user owns is fine, e.g. `python3 -m venv ~/agenticml-venv`):
```bash
/opt/agenticml/bin/agenticml server serve --data DIR --host 127.0.0.1 --port 8080 --public-url https://...
```
`--data` falls back to `AGENTICML_SERVER_DATA`, then `~/.agenticml-server`. `--public-url` falls back to `AGENTICML_SERVER_PUBLIC_URL`.

### TLS proxy

The server serves plain HTTP over its configured port (typically loopback `127.0.0.1:8080`). In production or across a network, terminate TLS using a reverse proxy and configure the public URL:

- **Laptop or workstation (Tailscale):**
  ```bash
  tailscale serve --bg 8080
  export AGENTICML_SERVER_PUBLIC_URL=https://<machine>.<tailnet>.ts.net
  ```
- **Production:** Run behind Caddy, Nginx, or a cloud load balancer. Set the public URL to the proxied HTTPS URL.

### Systemd service

A production system unit example is provided in [deploy/systemd/agenticml-server.service](../deploy/systemd/agenticml-server.service). See [deploy/systemd/README.md](../deploy/systemd/README.md) for full setup instructions:

```bash
sudo python3 -m venv /opt/agenticml
sudo /opt/agenticml/bin/python -m pip install agenticml-lite
sudo useradd --system --home /var/lib/agenticml-server --shell /usr/sbin/nologin agenticml
sudo curl -fsSL -o /etc/systemd/system/agenticml-server.service https://raw.githubusercontent.com/agenticML/agenticml-lite/v2.0.0/deploy/systemd/agenticml-server.service
```

Use the tag that matches the agenticml-lite version you installed (`pip show agenticml-lite`). Edit `AGENTICML_SERVER_PUBLIC_URL` in the unit (or `sudo systemctl edit agenticml-server`), then `daemon-reload` and `enable --now`:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agenticml-server
```

The unit runs under the `agenticml` user and group, stores data in `/var/lib/agenticml-server` (managed by `StateDirectory`), binds `127.0.0.1:8080`, and reads optional environment settings from `/etc/agenticml-server.env`.

Check service status and logs:
```bash
sudo systemctl status agenticml-server
sudo journalctl -u agenticml-server -f
```
Stop the service:
```bash
sudo systemctl stop agenticml-server
```

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
- Terminal: Check the standard output from `agenticml server serve`.
- systemd: Run `sudo journalctl -u agenticml-server`.

Open the `/setup?token=<token>` link in a browser. It prompts for a key name, generates an admin key, displays it once, and signs the browser in.

The setup token is stored only in memory and becomes invalid once used or after a server restart. After an admin key exists, `/setup` returns 404.

### Environment admin key

You can pre-configure an admin key via environment variable:

```bash
export AGENTICML_SERVER_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
```

Under systemd, configure `AGENTICML_SERVER_KEY` in the unit's environment file (e.g. `/etc/agenticml-server.env`, owned by root with mode `0600`).

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

```bash
# Create a sync key (scope defaults to sync)
agenticml server keys create --name worker-1 --scope sync --data DIR

# Create an admin key
agenticml server keys create --name operator-2 --scope admin --data DIR

# List all keys
agenticml server keys list --data DIR

# Revoke a key
agenticml server keys revoke worker-1 --data DIR
```

Under systemd, run the commands as the service user with the same data directory:

```bash
sudo -u agenticml /opt/agenticml/bin/agenticml server keys create --name worker-1 --scope sync --data /var/lib/agenticml-server
sudo -u agenticml /opt/agenticml/bin/agenticml server keys list --data /var/lib/agenticml-server
sudo -u agenticml /opt/agenticml/bin/agenticml server keys revoke worker-1 --data /var/lib/agenticml-server
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
curl -fsSL -o ~/.config/systemd/user/agenticml-sync.service https://raw.githubusercontent.com/agenticML/agenticml-lite/v2.0.0/deploy/systemd/agenticml-sync.service
```

Use the tag that matches the agenticml-lite version you installed (`pip show agenticml-lite`).

```bash
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

## Sync features and limits

The server supports metric, scalar, event, media, copied artifact, by-reference artifact, and lineage synchronization over [protocol v2](PROTOCOL.md). Install `git` on the server to enable lineage code diffs. Git is detected at startup; if missing, the server logs a warning and leaves the `lineage` feature off. The client sends incremental shadow-repository bundles, and server code diffs then match local diffs. No server worktree checkout is needed.

`agenticml sync --artifacts` uploads `copy=False` artifacts whose source file is unchanged since it was logged, then records the mapping in server-only `artifacts.json`. A changed or missing source is skipped with one warning. Without `--artifacts`, those artifacts stay as metadata. The local dashboard never serves arbitrary local paths.

Lineage validation supports data directory paths containing `:`. Repository
initialization and ref publication failures (including stale Git lock files),
Git timeouts, and filesystem failures return retryable 503 `lineage busy`
responses; clients leave the run incomplete without remembering a permanent
skip. If a failed publication cannot be safely rolled back, the server retains
the promoted packs so any published ref keeps its objects. Startup removes
abandoned `lineage-*` quarantine directories under `server/tmp` after a crash
and warns about other subdirectories.

The following limits remain:

- **Run notes (`notes.md`)**: Notes remain local to the machine where they were written.
- **Cross-key run collision**: A run ID is owned by the key that first created it. Pushing to an existing run ID using a different key fails with an error.

## Configuration reference

### CLI options (`agenticml server serve`)

| Option | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--data DIR` | `AGENTICML_SERVER_DATA` | `~/.agenticml-server` | Path to server data directory. |
| `--host HOST` | | `127.0.0.1` | Host interface to bind. |
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

Stop the service cleanly before backing up the data directory to release `server.lock` and flush pending writes:
```bash
sudo systemctl stop agenticml-server
sudo tar czf agenticml-backup.tgz -C /var/lib/agenticml-server .
sudo systemctl start agenticml-server
```

### Restoring backups

To restore a backup, stop the service, extract the backup directory into the data directory, verify permissions, and start the service:
```bash
sudo systemctl stop agenticml-server
sudo tar xzf agenticml-backup.tgz -C /var/lib/agenticml-server
sudo chown -R agenticml:agenticml /var/lib/agenticml-server
sudo systemctl start agenticml-server
```

## Upgrades

Back up the data directory first.

One server process per data directory; downgrades are not supported. Read release notes prior to upgrading.

1. Upgrade the package in the virtual environment:
   ```bash
   sudo /opt/agenticml/bin/pip install -U agenticml-lite
   ```
2. Restart the server service:
   ```bash
   sudo systemctl restart agenticml-server
   ```
3. Verify server health:
   ```bash
   curl -s http://127.0.0.1:8080/healthz
   ```

Because the server holds an exclusive lock on `<data>/server/server.lock`, only one server process may access the data directory at a time.

## Troubleshooting

### "another agenticml server is using <data>"
The error means another server process has the same data directory open. Check `systemctl status agenticml-server` or look for running `agenticml server serve` processes on the host. Stop the other process and start again.

### HTTP 403 ("forbidden host") on dashboard
When the server is accessed through a proxy or external hostname, the browser sends that hostname in the HTTP `Host` header. The dashboard accepts only loopback hostnames (`localhost`, `127.0.0.1`, `::1`) unless `--public-url` is configured; a refused Host receives HTTP 403 with `{"error": "forbidden host"}`. Set `AGENTICML_SERVER_PUBLIC_URL=https://<your-hostname>` in the server environment (or `/etc/agenticml-server.env` under systemd).

### `agenticml sync` exits with status 3 (`401 Unauthorized`)
When running `agenticml sync` (including `--follow` and `--watch`), the client exits with status code 3 if the server returns 401 because the API key was rejected or revoked. Run `agenticml login --host https://...` to enter a new key, or check key status on the server with `agenticml server keys list`.

### Runs appear as "stale" in dashboard
A run appears stale when its status in `remote.json` is `running` but no commit has occurred for longer than `AGENTICML_REMOTE_STALE_SECONDS` (default 600 seconds). Ensure training jobs run `agenticml sync --follow` to send heartbeats, or check network connectivity between the training node and the server.

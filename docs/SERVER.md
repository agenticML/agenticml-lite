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

### Quick start on a laptop: `server up`

On a Mac or Linux machine with Tailscale already installed and logged in:

```bash
agenticml server up
```

`server up` makes the server reachable from the public internet through Tailscale Funnel: anyone can reach the login page; only keys grant access. It takes port 443 of this machine's Tailscale serve config. This checks that Tailscale is running and that HTTPS certificates and Funnel are already allowed in the admin console (those approvals stay manual), creates the first admin key if none exists, serves on loopback, and turns Funnel on until the server stops. `Ctrl+C`, SIGTERM, or SIGHUP (closing the terminal) turns Funnel off. After SIGKILL, a crash, or power loss, Funnel stays on. The next `server up` on the same port turns it back on and off again; a stale Funnel entry for a different port is refused ("Tailscale already serves port 443 on this machine: check `tailscale funnel status` and turn it off first") and must be turned off with `tailscale funnel --https=443 off` first. All Funnel visitors reach the server from `127.0.0.1`, so they share one failed-login throttle bucket — someone sending bad keys can delay sign-in and syncs by up to 60 s (fixed by package 13 in 2.2).

To start at login (systemd user unit on Linux, launchd agent on macOS):

```bash
agenticml server up --install
```

Then run the `systemctl --user` / `loginctl enable-linger` or `launchctl load` command it prints.

The rest of this page is the manual path: `server serve` plus your own TLS proxy (including production systemd).

### TLS proxy

The server serves plain HTTP over its configured port (typically loopback `127.0.0.1:8080`). In production or across a network, terminate TLS using a reverse proxy and configure the public URL:

- **Laptop or workstation (Tailscale):**
  ```bash
  tailscale serve --bg 8080
  export AGENTICML_SERVER_PUBLIC_URL=https://<machine>.<tailnet>.ts.net
  ```
  Or replace those steps with `agenticml server up` (Funnel instead of Serve; see above).
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

## Operator commands

Inspect the data directory on the server host:

```bash
agenticml server status --data /var/lib/agenticml-server
agenticml server verify --data /var/lib/agenticml-server
```

Both commands are read-only and work while the server is running. Add `--json`
for machine-readable output. `status` shows run counts by effective dashboard
status (`unsynced` before a remote status is committed), disk sizes, skipped
size entries, and each key's run count and latest sync. JSON includes a
top-level `warnings` list. Runs owned by `AGENTICML_SERVER_KEY` are marked
`"env": true`. A corrupt or unreadable `keys.json` is a warning and still
exits 0. Revoked owners remain visible; key secrets and hashes are never
printed.

`verify` streams SHA-256 checks of stored objects and checks committed run
file sizes, missing references (including by-reference artifacts), lineage
refs, and leftover Git ref locks. It exits 0 when clean (warnings are allowed),
1 for problems, and 2 for usage errors or an unreadable data directory. Files
that grew beyond a running run's committed sizes are warnings because a sync
may be in progress. A ref lock younger than ten minutes is also a warning;
older locks are problems. Stop the server before using the printed removal
command for a leftover lock. Verification never repairs or removes files.

After [restoring a backup](#restoring-backups), run `server verify` before
resuming syncs. Recheck live-sync warnings once the run has finished.

## Network filesystems (EFS)

Every append, atomic write, and object put on the server does `os.fsync` plus a directory fsync. That is the main extra cost when `<data>` is on EFS or another network filesystem. `server/tmp` and `store` must already be on the same filesystem (the server refuses to start otherwise). Run a single server process per data directory. NFSv4 emulates `flock` on `server.lock`; AWS documents NFSv4.1 (`nfsvers=4.1`) for EFS, and that is the recommended mount.

These numbers were measured on local NVMe, then again with a modelled fsync delay injected **only in the benchmark's server process**. They are not measurements on real EFS. 5 ms and 20 ms are assumptions for typical EFS commit latency on small writes. Recommendations below are from **1 and 8 clients only**; larger N is unmeasured.

The first published tables held a process-wide `objects_lock` across each object PUT's temp-file `fsync`, rename, and directory fsync. After those runs, object writes fsync the private temp file with no lock held, then take one lock per `objects/<hh>` prefix only for the unsafe-destination check, the already-present check, `mkdir`, `os.replace`, and the directory fsync. The 20 ms 1-client and 8-client configs and the local 8-client config were re-run after that change. Before/after rows below use the numbers already published here as **before**. The local 1-client and both 5 ms rows were not re-run (labelled pre-change). Every before/after row is a single run with unquantified noise. The before and after runs were not on guaranteed identical machine state.

**Machine:** Intel Core i7-14700K (28 threads), 32 GiB RAM, NVMe SSD `CT1000P3PSSD8` (931.5 G, ROTA=0), ext4 on `/dev/nvme0n1p3`, Python 3.10.12, git 2.34.1.

**Workload** (`scripts/bench_server.py` defaults): each client writes 4 live runs × 4000 steps × 8 metrics and 50 distinct 65536-byte `copy=True` artifacts (split across that client's runs), then 3 passes of sync (one full sync, then two incremental append+sync passes, back to back). Clients start together. Uploaded payload on the first pass is about 4.32 MB per client (about 1.04 MB of scalars plus 3.28 MB of objects).

### Local disk

| Clients | Initial wall | Throughput | Incremental p50 / p95 | Commit p50 / p95 | Object PUT p50 / p95 | Objects/s | Server CPU |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 (pre-change) | 1.629 s | 2.65 MB/s | 0.640 s / 0.694 s | 14.32 ms / 16.81 ms | 10.94 ms / 13.85 ms | 30.69 (50 objects) | 1.206 s |
| 8, before | 5.691 s | 6.07 MB/s | 1.289 s / 1.402 s | 25.94 ms / 80.99 ms | 52.42 ms / 70.47 ms | 70.28 (400 objects) | 9.505 s |
| 8, after | 4.836 s | 7.15 MB/s | 1.558 s / 1.969 s | 43.56 ms / 87.60 ms | 28.00 ms / 45.91 ms | 82.72 (400 objects) | 11.006 s |

The 8-client local run after the lock change used 11.006 s of server CPU over an 8.407 s sync window (about 1.31 cores). The server is above one core because fsync and I/O release the GIL; the Python work itself is close to one core. Before the change it was 9.505 s of CPU over an 8.444 s window (about 1.13 cores).

### Modelled fsync delay (not EFS)

| Delay | Clients | Initial wall | Throughput | Incremental p50 / p95 | Commit p50 / p95 | Object PUT p50 / p95 | Objects/s | Server CPU |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 5 ms (pre-change) | 1 | 2.859 s | 1.51 MB/s | 1.138 s / 1.155 s | 26.11 ms / 30.57 ms | 22.11 ms / 24.49 ms | 17.49 (50 objects) | 1.377 s |
| 5 ms (pre-change) | 8 | 9.343 s | 3.70 MB/s | 1.663 s / 1.927 s | 37.01 ms / 79.08 ms | 123.74 ms / 140.46 ms | 42.81 (400 objects) | 10.904 s |
| 20 ms | 1, before | 6.186 s | 0.70 MB/s | 2.384 s / 2.399 s | 56.33 ms / 60.45 ms | 51.79 ms / 55.05 ms | 8.08 (50 objects) | 1.413 s |
| 20 ms | 1, after | 6.053 s | 0.71 MB/s | 2.386 s / 2.414 s | 55.21 ms / 58.59 ms | 51.13 ms / 54.14 ms | 8.26 (50 objects) | 1.315 s |
| 20 ms | 8, before | 23.143 s | 1.49 MB/s | 3.207 s / 3.398 s | 65.00 ms / 92.52 ms | 384.96 ms / 412.60 ms | 17.28 (400 objects) | 11.272 s |
| 20 ms | 8, after | 6.731 s | 5.13 MB/s | 2.930 s / 3.239 s | 63.77 ms / 125.78 ms | 49.47 ms / 58.58 ms | 59.43 (400 objects) | 8.895 s |

Per-run locks hold across that run's fsyncs. They did not serialize unrelated clients' scalar appends or commits: on the 20 ms model after the object-lock change, eight clients finished the incremental pass in 1.2× the 1-client time (2.930 s vs 2.386 s). Commit p50 for 8 clients was 63.77 ms versus 55.21 ms for 1 client (a 1-vs-8 comparison, not before/after).

Object PUTs previously held the process-wide `objects_lock` across `os.fsync`, `os.replace`, and a directory fsync. On the 20 ms model, eight clients (400 distinct objects) only reached 17.28 objects/s vs 8.08 objects/s for one client (2.1×, not 8×), and object PUT p50 rose from 51.79 ms to 384.96 ms (7.4×). After per-prefix locks with the temp-file fsync outside the lock, eight-client object PUT p50 is 49.47 ms (vs 51.13 ms for one client) and objects/s is 59.43 vs 8.26 (7.2×). Initial wall dropped from 23.143 s to 6.731 s, in line with the 1-client 6.053 s. The 1-client 20 ms row is unchanged within noise. The 1-client objects/s figure is still diluted by scalar work on the same initial pass; the PUT latencies are the cleaner signal.

The lock change also showed regressions (each a single run, noise unquantified): local 8-client incremental p50 1.289→1.558 s / p95 1.402→1.969 s, local commit p50 25.94→43.56 ms, 20 ms 8-client commit p95 92.52→125.78 ms, and local server CPU 9.505→11.006 s. A hypothesis is that object PUTs now finish sooner and compete with appends and commits on one GIL-bound process.

Three repeat runs of the local 8-client config on the final code (same workload) gave: initial wall 3.514 / 4.975 / 3.688 s, incremental p50 1.571 / 1.520 / 1.637 s, commit p50 25.72 / 26.13 / 27.48 ms (p95 45.24 / 49.63 / 44.40 ms), object PUT p50 21.99 / 29.12 / 22.12 ms, server CPU 6.0–6.5 s. So the local commit p50 rise above was noise (it matches the pre-change 25.94 ms); the incremental p50 is about 0.25 s higher than the single pre-change run, which is within the spread seen for initial wall time but was not re-measured before the change.

### Recommended settings

These are from 1 and 8 clients only. Larger N is unmeasured.

- **`sync --follow --interval`:** keep the default 30 s. The slowest incremental pass here was 3.239 s (8 clients, 20 ms modelled delay, after the object-lock change; 3.398 s before) for 4 live runs. Passes ran back to back; this was not a 30 s follow loop.
- **Heartbeat (60 s) vs stale (600 s):** the 60 s heartbeat was not exercised. Passes were back to back. The slowest commit was 125.78 ms (8-client, 20 ms modelled, commit p95 after the change; 92.52 ms before). At this N a 60 s heartbeat would stay well under the 600 s stale threshold; that is inferred from commit latency, not measured in a follow loop.
- **`APPEND_CHUNK` (8 MiB):** leave it. Each series file in this workload is 32 kB per pass (`4000 × 8` bytes), far below 8 MiB. The cost is fsync round-trips per request, not chunk size. Shrinking the chunk would add requests and fsyncs.
- **EFS mount options (not measured here):** `nfsvers=4.1`, `hard`, `noresvport`. NFSv4 emulates `flock`; 4.1 is the version to use. General Purpose vs Elastic throughput was not measured.

### Reproduce on real EFS

Point `--data` at the EFS mount and omit `--fsync-delay-ms`:

```bash
python scripts/bench_server.py --data /mnt/efs/agenticml --clients 1
python scripts/bench_server.py --data /mnt/efs/agenticml --clients 8
```

The script starts a server on `127.0.0.1:0`, creates one sync key per client, and deletes `bench-*` runs and keys from `--data` on the way out. On loopback it probe-binds `127.0.0.1`–`127.0.0.N` and gives each client its own source address so the failed-authentication throttle (keyed by client IP) does not 429 concurrent processes. If those addresses are missing (typical on macOS), it prints a warning, falls back to `127.0.0.1` for every client, and enables client retries: more than about 6 simultaneous requests from one address may get a short 429.

Machines behind the same NAT or HTTP proxy share one throttle bucket, even if they are distinct hosts. A burst of simultaneous starts can see a brief 429; the client retries 429 up to `max_retries`, sleeping `Retry-After` when present (capped at 60 s). With `--follow`, a single request can now wait up to about 3 × 60 s on repeated 429s before follow's own backoff starts. This benchmark uses `max_retries=0` when it has distinct `127.0.0.x` addresses.

`--quick` is a smoke size (under a minute). Replace the modelled tables above with real-EFS numbers when that run is done.

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

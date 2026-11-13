# AgenticML Server

`agenticml server` is a synchronization server and shared web dashboard for AgenticML runs.

It runs in two modes from the same codebase:
- **Self-hosted**: runs on your own hardware or VM. Server state lives in SQLite, run files and objects on local disk. One workspace (`default`) is configured by default; operators can create more. No external dependencies, no Docker container required.
- **Hosted**: multi-workspace deployment on AWS. Server state lives in PostgreSQL (Amazon RDS), content-addressed objects in Amazon S3, run files and shadow Git lineage on Amazon EFS. Multiple server processes run behind an Application Load Balancer on a single hostname (`app.agenticml.xyz`) with Firebase authentication.

## Architecture

- **Workspaces**: All experiment runs, API keys, and team memberships belong to a workspace. Workspaces provide strict data isolation on a single server hostname.
- **Database storage**: Server state (accounts, workspaces, memberships, API keys, sessions, audit events, run metadata, committed file sizes, and metric summaries) lives in a SQL database: SQLite for single-process self-hosted deployments, PostgreSQL for multi-process or hosted deployments.
- **Run files**: Raw run files (`meta.json`, `events.jsonl`, `scalars/*`, `media/index.json`) and shadow Git lineage repositories remain in standard filesystem directories (`<data>/workspaces/<workspace_id>/store/` and `lineage/`).
- **Object storage**: Blobs (source code snapshots, media files, and copied artifacts) are content-addressed by SHA-256 and stored on local disk under `<data>/workspaces/<workspace_id>/store/objects/` or in Amazon S3. Objects are isolated per workspace.
- **Single-writer vs multi-process**:
  - SQLite deployments acquire an exclusive lock on `<data>/server/server.lock` and run as a single process.
  - PostgreSQL deployments support multiple concurrent server processes sharing the database and data directory. Concurrency is coordinated through database row locks and transactional advisory locks.
- **Network and TLS**: The server serves plain HTTP over a single TCP port. In production, run it behind a TLS-terminating reverse proxy such as AWS ALB, Caddy, or Tailscale.

## Installation and extras

Install `agenticml-lite` into a Python virtual environment (Python 3.10+ on Linux or POSIX hosts):

```bash
python3 -m venv /opt/agenticml
/opt/agenticml/bin/pip install agenticml-lite
```

The base package has zero required third-party dependencies and uses the standard library SQLite backend and local filesystem object storage.

Optional extras are available for PostgreSQL, S3, and Firebase:

```bash
# PostgreSQL database backend (psycopg binary)
pip install "agenticml-lite[postgres]"

# Amazon S3 object storage (boto3)
pip install "agenticml-lite[s3]"

# Firebase ID token verification (PyJWT with cryptography)
pip install "agenticml-lite[firebase]"

# Hosted bundle: postgres, s3, and firebase
pip install "agenticml-lite[hosted]"
```

## Running the server

Start the server using `agenticml server serve`:

```bash
agenticml server serve --data /var/lib/agenticml-server --host 127.0.0.1 --port 8080 --public-url https://aml.example.com
```

### Quick start on a workstation: `server up`

On a machine with Tailscale installed and logged in:

```bash
agenticml server up
```

`server up` makes the server reachable over the public internet through Tailscale Funnel. It binds loopback port 8080, configures Funnel on port 443 of the machine's Tailscale domain, and generates a first-admin invite link if no admin exists. Throttling uses client addresses from `X-Forwarded-For` and per-account buckets.

To install as a background user service (systemd user unit on Linux, launchd agent on macOS):

```bash
agenticml server up --install
```

### Production systemd service

For Linux production systems, run the server as a systemd system service. See [deploy/systemd/agenticml-server.service](../deploy/systemd/agenticml-server.service) and [deploy/systemd/README.md](../deploy/systemd/README.md):

```bash
sudo python3 -m venv /opt/agenticml
sudo /opt/agenticml/bin/python -m pip install agenticml-lite
sudo useradd --system --home /var/lib/agenticml-server --shell /usr/sbin/nologin agenticml
sudo curl -fsSL -o /etc/systemd/system/agenticml-server.service https://raw.githubusercontent.com/agenticML/agenticml-lite/v3.0.0/deploy/systemd/agenticml-server.service
```

Edit `/etc/systemd/system/agenticml-server.service` or use `sudo systemctl edit agenticml-server` to set `AGENTICML_SERVER_PUBLIC_URL`. Then enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agenticml-server
```

Check service status and logs:

```bash
sudo systemctl status agenticml-server
sudo journalctl -u agenticml-server -f
```

## Workspaces and routing

All data on the server is scoped to a workspace.

### Workspace URLs

- Dashboard views: `/w/<slug>/...` (e.g. `/w/default/`, `/w/team-a/runs/20261109...`).
- Dashboard APIs: `/api/w/<slug>/...`.
- Global routes (no workspace prefix): `/account`, `/login`, `/invite`, `/healthz`, and `/admin/workspaces`.

When an account belongs to multiple workspaces, the dashboard provides a workspace switcher in the navigation menu. Navigating to `/` redirects to the last-used workspace or the workspace selection page.

### Roles and permissions

Every member of a workspace holds one of four roles within that workspace:

| Action | `viewer` | `member` | `admin` | `owner` |
| --- | --- | --- | --- | --- |
| Read dashboard and runs | yes | yes | yes | yes |
| Edit run notes | – | yes | yes | yes |
| Sync runs (`/v2` writes) | – | yes | yes | yes |
| Issue / revoke own sync keys | – | yes | yes | yes |
| Manage workspace members and invites | – | – | yes | yes |
| Issue / revoke keys for any member | – | – | yes | yes |
| Change workspace settings and rename | – | – | yes | yes |
| Delete workspace | – | – | – | yes |

- **`owner`**: The creator of the workspace. A workspace has exactly one owner. The owner cannot be removed or demoted. Ownership can be transferred with `server workspaces set-owner`.
- **`admin`**: Full administrative rights within the workspace (members, invites, keys, notes, settings).
- **`member`**: Can sync runs, edit notes, and manage their own sync keys.
- **`viewer`**: Read-only dashboard access. Viewer sync keys can only call `GET /v2/whoami`; all other `/v2` endpoints return `403 Forbidden`.

### Server administrators

A global account can have the `server_admin` flag set. Server administrators:
- Manage workspaces: create, rename, suspend, resume, and delete workspaces. On `/admin`, **Archive** suspends a workspace (not `default`), keeps its data and stops it counting toward the owner's free workspace limit; **Restore** brings it back as suspended. Permanent deletion is the CLI `workspaces delete` only.
- Manage accounts: list accounts, create accounts, and grant or revoke server administrator privileges.
- View global storage and usage across all workspaces.
- Configure workspace plans and limits.
- **Isolation boundary**: Server administrators cannot view, list, or download runs or files within a workspace unless they are explicitly added as a member of that workspace. An admin without workspace membership lands on `/workspaces` with an Admin button and sees no customer workspace data.

Server administrator privileges can be granted through three paths:

- **Admin emails (`--admin-emails` / `AGENTICML_SERVER_ADMIN_EMAILS`)**: Comma-separated list of emails, matched case-insensitively. Requires Firebase authentication (`--firebase-project`). Without Firebase, the list is ignored with a startup warning. Invalid entries are dropped with a warning. When a user completes a verified Firebase sign-in (Google OAuth or email/password with verified email) matching the list, their account is created if none exists and granted server admin with source `list`. With `--open-signup` the account also gets the free personal workspace (once); without it, no workspace is created. If an email is removed from the list, list-granted server admin is revoked at that person's next sign-in.
- **Web administration (`/admin`)**: The "Server admins" section lists all server administrators (login, source, and sign-in state). Administrators can add server admins by email or login. Adding an email without an existing account creates a credential-less account that links on its first verified Firebase sign-in (`source` is `manual`). Administrators can remove server admins immediately; an email still in `--admin-emails` will regain server admin at its next sign-in.
- **Server CLI (`agenticml server accounts set-server-admin`)**: `set-server-admin LOGIN on|off` and `accounts add LOGIN --server-admin` grant or revoke server admin with source `manual`. Manual grants survive removal of an email from `--admin-emails`.

**Last server admin rule**: The last server administrator who can sign in (has a password, an identity, or a linked Firebase account) can never be removed through the list, the web interface, or the CLI; a pending admin that has never signed in does not count and can always be removed (`agenticml server accounts set-server-admin LOGIN off` fails with "cannot remove the last server admin").

**CLI source column**: `agenticml server accounts list` displays a `source` column (`list`, `manual`, or `-`). The JSON output (`--json`) includes `server_admin_source`.

### Workspace management CLI

Manage workspaces using `agenticml server workspaces`:

```bash
# List all workspaces
agenticml server workspaces list [--json] [--database URL] [--data DIR]

# Create a new workspace
agenticml server workspaces create SLUG [--name NAME] [--owner LOGIN] [--database URL] [--data DIR]

# Rename a workspace or change its slug
agenticml server workspaces rename SLUG --name NEW_NAME [--slug NEW_SLUG] [--database URL] [--data DIR]

# Suspend or resume a workspace (suspended workspaces reject all access with 404)
agenticml server workspaces suspend SLUG [--database URL] [--data DIR]
agenticml server workspaces resume SLUG [--database URL] [--data DIR]

# Delete a workspace (requires typed confirmation of the slug; deletes files and objects)
agenticml server workspaces delete SLUG [--yes-delete SLUG] [--objects URL] [--database URL] [--data DIR]

# Transfer workspace ownership
agenticml server workspaces set-owner SLUG --owner LOGIN [--database URL] [--data DIR]

# View usage across all workspaces
agenticml server workspaces usage [--limits FILE] [--json] [--database URL] [--data DIR]

# Adjust workspace limits or plan
agenticml server workspaces set-plan SLUG custom --storage 50GB --uploads-per-day 10GB --requests-per-minute 1200 --people 20
agenticml server workspaces set-plan SLUG free [--database URL] [--data DIR]

# Recount stored bytes from committed database state
agenticml server workspaces recount SLUG [--database URL] [--data DIR]
agenticml server workspaces recount --all [--database URL] [--data DIR]
```

### Account management CLI

Manage global user accounts using `agenticml server accounts`:

```bash
# List all registered accounts
agenticml server accounts list [--json] [--database URL] [--data DIR]

# Add a global account
agenticml server accounts add LOGIN [--display-name NAME] [--server-admin] [--database URL] [--data DIR]

# Grant or revoke server admin privileges
agenticml server accounts set-server-admin LOGIN on|off [--database URL] [--data DIR]
```

### Workspace membership and user CLI

Manage memberships and users within a specific workspace using `agenticml server users`:

```bash
# Add a member to a workspace (creates global account if needed; prints invite link)
agenticml server users add LOGIN --workspace SLUG [--role viewer|member|admin] [--no-invite] [--public-url URL]

# Generate a 72-hour invite link for a user
agenticml server users invite LOGIN --workspace SLUG [--public-url URL]

# Set a local password for a user
agenticml server users set-password LOGIN --workspace SLUG [--stdin]

# List members in a workspace
agenticml server users list --workspace SLUG [--json]

# Change a member's role
agenticml server users set-role LOGIN member|admin --workspace SLUG

# Remove a member from a workspace (revokes their keys in this workspace)
agenticml server users remove LOGIN --workspace SLUG

# Set proxy identity for header authentication
agenticml server users set-identity LOGIN [IDENTITY] --workspace SLUG
```

`--workspace` defaults to `default`.

## API keys and sync authentication

All sync requests over [protocol v2](PROTOCOL.md) authenticate using HTTP bearer tokens:

```http
Authorization: Bearer aml_...
```

### Key properties

- **Workspace binding**: Every API key belongs to exactly one workspace and one user account.
- **Automatic workspace resolution**: The server resolves the workspace directly from the bearer key on incoming `/v2` requests. Sync clients do not need to specify a workspace slug or URL prefix.
- **Run ownership**: Runs synced with an API key are owned by the account associated with that key. Only keys belonging to the run's owner can write or append to that run.
- **Identity verification**: `GET /v2/whoami` reports the calling key name, user account, role, protocol version, and the bound workspace slug (`"workspace": "<slug>"`).
- **Multiple workspaces on one machine**: `agenticml login` saves only one key per server hostname, so running login for a second workspace replaces the first. For multiple workspaces on the same host, keep credentials and sync cursors separate using environment variables in each workspace's shell: set `AGENTICML_HOST` (e.g. `https://app.agenticml.xyz`), read `AGENTICML_API_KEY` without echo (`read -rsp 'API key: ' AGENTICML_API_KEY; export AGENTICML_API_KEY`), and set a distinct local store directory via `AGENTICML_DIR=$HOME/.agenticml-<slug>`. Sync state is keyed by host, so using separate `AGENTICML_DIR` directories prevents runs from being skipped as "up to date" in the second workspace (or use `agenticml sync RUN_ID --reset` with the second key).

### Managing API keys via CLI

Manage keys with `agenticml server keys`:

```bash
# Create an API key in a workspace (prints plaintext key once)
agenticml server keys create --name KEY_NAME --workspace SLUG [--scope sync|admin] [--user LOGIN]

# List keys in a workspace
agenticml server keys list --workspace SLUG [--user LOGIN] [--json]

# Revoke an API key immediately
agenticml server keys revoke KEY_NAME --workspace SLUG
```

`--workspace` defaults to `default`. The `--scope` flag defaults to `sync`. `admin` scope keys permit both `/v2` sync and passwordless dashboard login (self-hosted only).

## PostgreSQL and multi-process deployment

For high-availability, high-throughput, or hosted deployments, configure PostgreSQL as the server database:

```bash
agenticml server serve \
  --data /var/lib/agenticml-server \
  --database "postgresql://agenticml:password@db.example.com:5432/agenticml?sslmode=verify-full"
```

Benefits of PostgreSQL:
- **Multiple server processes**: The single-writer `server.lock` file is bypassed. Multiple server processes can run concurrently across multiple hosts or containers behind a load balancer.
- **Distributed concurrency**: File appends and commits use PostgreSQL row locks on `run_files` and transaction-level advisory locks (`pg_advisory_xact_lock`).
- **Shared state**: Web sessions, failed login throttling, and rate limits are shared across all server processes in database tables.
- **Row-Level Security**: Defense-in-depth isolation ensures queries only access rows matching the session's active workspace.

Run files and shadow Git repositories must reside on a shared POSIX network filesystem (such as Amazon EFS mounted with NFSv4.1).

## Amazon S3 object storage

Store content-addressed blobs (code snapshots, media, copied artifacts) in Amazon S3 instead of local disk:

```bash
agenticml server serve \
  --data /var/lib/agenticml-server \
  --objects s3://my-agenticml-bucket/optional-prefix
```

S3 storage features:
- **Key layout**: Objects are stored at `{prefix}/{workspace_id}/objects/{hh}/{sha256}`.
- **Workspace isolation**: Objects are strictly partitioned by workspace ID. Missing-object checks (`POST /v2/objects/missing`) inspect only the calling workspace's inventory; files are never deduplicated across workspaces.
- **Server-side encryption**: Uploads enable AWS server-side encryption (`AES256` by default). Set `AGENTICML_SERVER_S3_KMS_KEY_ID` to use AWS KMS encryption (`aws:kms`).
- **Custom endpoints**: Use `AGENTICML_SERVER_S3_ENDPOINT_URL` for MinIO, LocalStack, or AWS VPC endpoints.
- **Direct streaming**: Objects stream directly through the server with streaming SHA-256 verification. The server does not issue pre-signed S3 URLs, ensuring workspace access controls are enforced on every download.

## Metric summaries and runs list query

The server maintains per-series metric summaries in the `run_metrics` table for every run.

### Summary calculation

When a client appends records to `scalars/<n>.bin`, the server parses new records and updates the series summary in the same transaction:
- `last_value`: value of the final record in append order.
- `last_step`: step number of the final record.
- `min_value` / `max_value`: minimum and maximum values (skipping NaN, preserving $\pm\infty$).
- `count`: total number of records.

### Query API (`/api/w/<slug>/runs`)

Dashboard API queries filter and sort runs by metric summaries directly in SQL without reading run files:
- `metrics=name1,name2`: include summary statistics for specified metric names (up to 8 metrics).
- `sort=metric:<name>:<stat>`: sort by metric stat (`last`, `min`, `max`), e.g. `sort=metric:val_loss:min`.
- `order=asc|desc`: sort direction (default `asc`).
- `filter=metric:<name>:<stat>:<op>:<value>`: filter runs, e.g. `filter=metric:accuracy:last:gt:0.95`. Ops: `lt`, `le`, `gt`, `ge`, `eq` (up to 8 filters).

### Rebuilding metric summaries

If scalar files were copied or imported out of band, recompute summaries using `agenticml server metrics rebuild`:

```bash
agenticml server metrics rebuild [--workspace SLUG] [--run RUN_ID] [--data DIR] [--database URL]
```

## Usage limits and quotas

Enforce storage and traffic limits per workspace using `--limits`:

```bash
agenticml server serve --limits /etc/agenticml-limits.json
```

Example limits configuration file:

```json
{
  "free": {
    "storage_bytes": 5000000000,
    "upload_bytes_per_day": 2000000000,
    "requests_per_minute": 600,
    "people": 5,
    "workspaces_per_account": 1
  }
}
```

### Limit enforcement

- **Storage limit**: Sum of committed run files and objects. Checked before writes. Uploads that would exceed the quota return HTTP `403 Forbidden` (`{"error": "storage limit reached: ..."}`). Reads, deletes, and notes remain accessible.
- **Daily upload quota**: Bytes uploaded within the current UTC day. Exceeding the quota returns HTTP 403 with the reset time.
- **Request rate limit**: Requests per minute per workspace across all server processes. Exceeding the rate returns HTTP `429 Too Many Requests` with a `Retry-After` header.
- **People quota**: Maximum members plus pending invites. Inviting additional members is blocked once the quota is reached.
- **Free workspaces**: Maximum free-plan workspaces an individual account can own.

## Backups and restore

### Consistent backup while running: `server backup`

The server includes an online backup command that copies server state while the server is running:

```bash
agenticml server backup --to /backups/20261109-0000 [--data DIR] [--database URL] [--objects URL]
```

Backup consistency rule:
1. The database is copied first:
   - For SQLite: captured using the SQLite Online Backup API.
   - For PostgreSQL: take a dump first (`pg_dump --format=custom "$DATABASE_URL" > /backups/.../server.dump`), then run `server backup --files-only`.
2. Run files are copied second, truncated to the exact committed sizes recorded in the database snapshot. Any bytes appended after the snapshot are excluded, ensuring consistent files.
3. Local content-addressed objects and shadow Git lineage repositories are copied. (S3 objects are not copied; protect S3 using bucket versioning and AWS Backup).
4. Writes a metadata manifest to `BACKUP.json`.

### Restoring from backup

To restore a SQLite server:
1. Point `agenticml server serve` at the backup directory:
   ```bash
   agenticml server serve --data /backups/20261109-0000
   ```
2. Or copy the contents into your standard data directory and set permissions.

To restore a PostgreSQL server:
1. Restore the PostgreSQL database dump into an empty database:
   ```bash
   pg_restore --clean --if-exists -d "$DATABASE_URL" /backups/20261109-0000/server.dump
   ```
2. Start the server pointing to the restored data directory and database:
   ```bash
   agenticml server serve --data /backups/20261109-0000 --database "$DATABASE_URL"
   ```

## Operator diagnostics: `status` and `verify`

Inspect server health and data integrity on the host:

```bash
# Overview of workspaces, runs, disk usage, and key activity
agenticml server status [--workspace SLUG] [--data DIR] [--database URL] [--objects URL] [--json]

# Integrity check: verify run file sizes, object SHA-256 hashes, and lineage refs
agenticml server verify [--workspace SLUG] [--deep] [--data DIR] [--database URL] [--objects URL] [--json]
```

- `status` displays workspace counts, run counts by status, committed storage, and key activity.
- `verify` checks file sizes against database committed records, tests object presence, and checks lineage refs. Adding `--deep` streams and re-hashes all S3 objects (local filesystem objects are always hashed). Exits `0` when clean, `1` on corruption or missing data, and `2` on usage errors.

## Upgrading from 2.x to 3.0

When starting a 3.0 server against an existing 2.x data directory, the server automatically migrates data in place on first start:

1. **Automatic database migration**: Creates the SQLite database at `<data>/server/agenticml.db` and applies migrations.
2. **Workspace creation**: Creates the `default` workspace.
3. **Account and membership import**: Imports `users.json`. Legacy users become global accounts and members of `default`. The earliest administrator is assigned as the workspace `owner`.
4. **Keys and audit import**: `keys.json` and `audit.jsonl` are imported into database tables.
5. **Run and metadata import**: Scans run directories, imports `remote.json` and `artifacts.json` into `runs` and `run_files` tables, and moves runs into `<data>/workspaces/<workspace_id>/store/runs/`.
6. **File retirement**: Legacy files are renamed with a `.2x` extension (`users.json.2x`, `keys.json.2x`, `audit.jsonl.2x`).
7. **Refusal to convert twice**: If old 2.x files reappear alongside converted state, the server refuses to start with an error:
   ```
   found 2.x <path> after this server was converted on <date>; move it away (the converted copy is <path>.2x)
   ```

Sync clients on version 2.2 continue to sync to a 3.0 server without modification.

## Configuration reference

### CLI options (`agenticml server serve`)

| Flag | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--data DIR` | `AGENTICML_SERVER_DATA` | `~/.agenticml-server` | Path to server data directory. |
| `--database URL` | `AGENTICML_SERVER_DATABASE_URL` | `sqlite:///<data>/server/agenticml.db` | Database connection URL (`sqlite:///path` or `postgresql://...`). |
| `--objects URL` | `AGENTICML_SERVER_OBJECTS` | `file://` | Object storage backend (`file://` or `s3://bucket/prefix`). |
| `--host HOST` | | `127.0.0.1` | Network interface to bind. |
| `--port PORT` | | `8080` | TCP port to listen on. |
| `--public-url URL` | `AGENTICML_SERVER_PUBLIC_URL` | unset | Canonical public URL (e.g. `https://aml.example.com`). Required behind reverse proxies for Host/Origin validation and cookies. |
| `--trusted-proxy CIDR` | `AGENTICML_SERVER_TRUSTED_PROXIES` | unset | Trust proxy CIDR for `X-Forwarded-For` and identity headers (repeatable; comma-separated in env). Refuses `0.0.0.0/0` and `::/0`. |
| `--identity-header PRESET` | `AGENTICML_SERVER_IDENTITY_HEADER` | unset | Honour proxy identity header (`alb`, `tailscale`, or `oauth2-proxy`). Requires `--trusted-proxy`. |
| `--firebase-project ID` | `AGENTICML_SERVER_FIREBASE_PROJECT` | unset | Firebase project ID for token verification. |
| `--firebase-api-key KEY` | `AGENTICML_SERVER_FIREBASE_API_KEY` | unset | Firebase web API key for dashboard sign-in. |
| `--firebase-auth-domain HOST` | `AGENTICML_SERVER_FIREBASE_AUTH_DOMAIN` | unset | Firebase auth domain (defaults to `<project>.firebaseapp.com`). |
| `--limits FILE` | `AGENTICML_SERVER_LIMITS` | unset | Path to JSON file defining workspace usage limits and plans. |
| `--admin-emails LIST` | `AGENTICML_SERVER_ADMIN_EMAILS` | unset | Comma-separated emails granted server admin on verified Firebase sign-in. Ignored without Firebase. |
| `--open-signup` | `AGENTICML_SERVER_OPEN_SIGNUP` | off | Allow verified Firebase sign-in to create an account and personal workspace. A verified account with no workspace gets one at sign-in (once; an admin-deleted sign-up workspace is not recreated). |
| `--min-client-version X.Y` | `AGENTICML_SERVER_MIN_CLIENT_VERSION` | `2.2` | Reject `agenticml-lite` clients older than `X.Y` on `/v2` requests with HTTP 426. |

Additional environment variables:
- `AGENTICML_SERVER_KEY`: Static admin API key named `env` kept in memory. Bound to the `default` workspace.
- `AGENTICML_SERVER_S3_ENDPOINT_URL`: Custom S3 endpoint URL for MinIO, LocalStack, or VPC endpoints.
- `AGENTICML_SERVER_S3_KMS_KEY_ID`: AWS KMS Key ID or ARN for S3 server-side encryption (`aws:kms`).
- `AGENTICML_REMOTE_STALE_SECONDS`: Inactivity threshold before an active run is marked `stale` (default `600`).

## Network filesystems (EFS)

Every append, atomic write, and object put on the server does `os.fsync` plus a directory fsync. That is the main extra cost when `<data>` is on EFS or another network filesystem. `server/tmp` and `store` must already be on the same filesystem (the server refuses to start otherwise). Run a single server process per data directory when using SQLite. NFSv4 emulates `flock` on `server.lock`; AWS documents NFSv4.1 (`nfsvers=4.1`) for EFS, and that is the recommended mount.

These numbers were measured on local NVMe, then again with a modelled fsync delay injected **only in the benchmark's server process**. They are modelled numbers, not measurements on real EFS (labelled as modelled until the owner's real-EFS benchmark run). 5 ms and 20 ms are assumptions for typical EFS commit latency on small writes. Recommendations below are from **1 and 8 clients only**; larger N is unmeasured.

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

## Troubleshooting

### "another agenticml server is using <data>"
Another server process has the SQLite data directory open. Check `systemctl status agenticml-server` or look for running `agenticml server serve` processes on the host. Stop the conflicting process. If multiple processes are required, use PostgreSQL via `--database`.

### HTTP 403 ("forbidden host") on dashboard
The dashboard accepts requests only from loopback addresses unless `--public-url` is configured. When operating behind a reverse proxy, set `AGENTICML_SERVER_PUBLIC_URL=https://<your-hostname>` in the server environment (or `/etc/agenticml-server.env` under systemd).

### HTTP 426 ("this server needs agenticml-lite >= X.Y")
The sync client is running an older `agenticml-lite` version below `--min-client-version` (default 2.2). Update the client package on the training machine:
```bash
pip install -U agenticml-lite
```

### `agenticml sync --follow` exits with status 3 (`401 Unauthorized`)
Under `agenticml sync --follow`, authentication failure (401) exits with status 3. A one-shot `agenticml sync` prints `agenticml sync: API key rejected - run agenticml login` and exits with status 1. In either case, the client's API key is missing, invalid, or has been revoked. Run `agenticml login --host https://...` to enter a new key, or check key status on the server with `agenticml server keys list --workspace <slug>`.

### HTTP 403 ("storage limit reached")
The workspace has reached its storage quota (committed run files plus objects). Additional uploads are rejected until an administrator raises the quota using `agenticml server workspaces set-plan <slug> custom --storage <size>` or removes unnecessary runs. Existing runs remain readable. Under `sync --follow`, the client automatically retries every 10 minutes and resumes once the limit is raised.

### Runs appear as "stale" in dashboard
A run appears stale when its status is `running` but no commit heartbeat has arrived for longer than `AGENTICML_REMOTE_STALE_SECONDS` (default 600 seconds). Ensure training jobs run `agenticml sync --follow`, which transmits heartbeats every 60 seconds while runs are active.

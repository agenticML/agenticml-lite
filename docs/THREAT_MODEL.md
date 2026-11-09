# Threat Model

This document outlines the security architecture, trust boundaries, threat landscape, and defensive controls of `agenticml server` for both self-hosted and hosted multi-workspace deployments.

## Assets

The server protects seven primary assets:

1. **Run data and committed files**: Experiment metrics, configurations, summaries, events, and artifacts stored in the v1 store layout on disk or Amazon EFS under `<data>/workspaces/<workspace_id>/store/runs/`.
2. **Content-addressed objects**: Code snapshots, media files, and copied artifacts stored on disk or Amazon S3, indexed and partitioned strictly per workspace.
3. **Lineage Git repositories**: Shadow Git repositories containing committed code snapshots and diffs stored under `<data>/workspaces/<workspace_id>/lineage/`.
4. **API keys and machine credentials**: Stored in the server database (`keys` table) as SHA-256 digests. Plaintext keys are shown once upon creation and never stored or logged. Each key is bound to exactly one workspace.
5. **User accounts and credentials**: Global accounts stored in the `accounts` table. Passwords are encrypted with computationally expensive `scrypt` hashes with unique random salts. Firebase accounts are bound to immutable Firebase user IDs (`firebase_uid`).
6. **Workspace memberships and roles**: Stored in `memberships` table, defining whether an account can access a workspace and whether their role is `viewer`, `member`, `admin`, or `owner`.
7. **Audit trail**: Append-only audit records stored in the `audit_events` table (or local `audit.jsonl`), tracking security-relevant actions, authentication, membership changes, plan updates, and deletions. No secrets are ever written to the audit log.

## Trust boundaries

Trust boundaries define the isolation model across single-tenant and multi-tenant configurations:

1. **Client / Training Node ↔ Reverse proxy ↔ Server**: The server speaks HTTP on a private interface behind a TLS-terminating reverse proxy (e.g. AWS ALB, Tailscale, or Caddy). The server trusts `X-Forwarded-For` and proxy identity headers only when requests originate from configured `--trusted-proxy` CIDR ranges.
2. **Workspace ↔ Workspace (Multi-tenancy)**: In hosted or multi-workspace environments, multiple organizations and teams share the same hostname, database, and infrastructure. One workspace must never access, list, modify, or detect the existence of another workspace's runs, keys, files, or objects.
3. **Dashboard user ↔ Server**: Users authenticate via local username/password, trusted identity proxy headers, or Firebase Authentication tokens. Sessions are tracked via database-backed session cookies.
4. **Server admin ↔ Workspace data**: Server administrators possess global management privileges (creating and suspending workspaces, setting quotas), but are not granted ambient read access to customer runs or experiment files.
5. **Server ↔ Backend storage services**: Communication with the PostgreSQL database, Amazon S3 bucket, and Amazon EFS filesystem.

## In-scope threats and defensive controls

### 1. Workspace isolation and cross-tenant leakage

- **Threat**: A user or API key in Workspace A attempts to read, list, modify, or probe the existence of resources in Workspace B.
- **Controls**:
  - **Explicit URL and key binding**: Dashboard and API routes require a workspace slug in the URL path (`/w/<slug>/...` and `/api/w/<slug>/...`). Sync API requests (`/v2/...`) derive the workspace directly from the bearer API key's cryptographic digest. A request can never default or fall back to an arbitrary workspace.
  - **Pre-handler authorization**: Before any route handler executes, the request is bound to a single workspace via `require_workspace_member` or `require_workspace_manager`. Data repositories cannot be instantiated without a verified `workspace_id`.
  - **Uniform 404 responses**: Requests for non-existent workspaces, workspaces the caller does not belong to, and suspended workspaces all return identical HTTP `404 Not Found` responses. An attacker cannot enumerate valid workspace slugs.
  - **PostgreSQL Row-Level Security (RLS)**: As defense-in-depth, PostgreSQL migrations define RLS policies on all workspace tables. The active workspace ID is set via `SET LOCAL app.workspace_id` within each transaction. Any query executing without the setting returns zero rows.
  - **Per-workspace object partitioning**: S3 objects are stored under `{prefix}/{workspace_id}/objects/{hh}/{sha256}`. Missing-object checks (`POST /v2/objects/missing`) inspect only the calling workspace's inventory. There is no cross-workspace object deduplication, preventing side-channel probing of whether another customer possesses a specific file.
  - **Isolation test suite**: CI runs a dedicated isolation suite that enumerates all server routes and asserts that cross-workspace access attempts return HTTP 404.

### 2. Operator access to customer data

- **Threat**: A platform administrator or support engineer abuses administrative rights to access proprietary customer models, code, or metrics.
- **Controls**:
  - **No ambient data access**: The `server_admin` flag grants authority over platform operations (workspace creation, quotas, account status), but grants no access to workspace runs, files, or metrics.
  - **Support access protocol**: If a customer requires support, a workspace administrator must explicitly invite the support user account as a `member` of their workspace.
  - **Audit logging**: Support invitations, member role assignments, and data access are permanently recorded in the workspace audit trail (`audit_events`).
  - **Cloud infrastructure access**: AWS operators with ECS Exec or direct database access have actions audited via AWS CloudTrail and ECS Exec session logging.

### 3. Shared Git processing of hostile bundles

- **Threat**: A malicious uploader transmits a malformed Git bundle, crafted pack file, or corrupt Git objects designed to achieve code execution or disrupt shared server infrastructure.
- **Controls**:
  - **Strict payload limits**: Bundle bodies are capped at 256 MiB and headers at 1 MiB.
  - **Single ref validation**: The bundle header must advertise exactly one ref matching `refs/runs/{id}` and the run's expected commit SHA-1.
  - **Quarantine validation**: Uploaded bundles are unpacked into an isolated temporary quarantine directory under `<data>/server/tmp/` with `git index-pack --stdin --fix-thin --strict`. Live project objects are referenced only as read-only alternates.
  - **Hardened Git subprocesses**: Subprocesses execute with clean argument lists, no shell invocation, C locale, disconnected stdin, and strict execution timeouts (60s + size-based allowance). Hooks, fsmonitor, automatic garbage collection, and commit-graph writes are explicitly disabled.
  - **Concurrency limits**: At most two concurrent Git bundle receives can run simultaneously across the server, preventing CPU exhaustion.

### 4. Noisy neighbours and resource exhaustion

- **Threat**: A tenant floods the server with high-volume requests, oversized files, or excessive storage consumption, causing degradation for other tenants.
- **Controls**:
  - **Storage quotas**: Hard limits on combined committed run files and object storage. When exceeded, uploads return HTTP `403 Forbidden` (`{"error": "storage limit reached: ..."}`); existing runs remain readable.
  - **Daily upload limits**: Uploaded bytes are tracked in UTC daily buckets in `workspace_usage`. Exceeding the quota returns HTTP 403 with the reset timestamp.
  - **Shared rate limiting**: Shared across all server processes via database counters (`requests_per_minute`). Exceeding the rate returns HTTP `429 Too Many Requests` with a `Retry-After` header.
  - **Body admission throttles**: HTTP request bodies are limited (16 MiB atomic files, 64 MiB appends, 256 MiB Git bundles, 4 GiB objects). Maximum 8 concurrent request bodies are admitted concurrently; additional requests queue or receive HTTP 503 with `Retry-After: 5`.

### 5. Open sign-up abuse

- **Threat**: Malicious actors automate mass account and workspace creation on the hosted service to consume free storage or spam the infrastructure.
- **Controls**:
  - **Verified email requirement**: First sign-in via Firebase requires a verified email address before an account or personal workspace is provisioned. Unverified accounts cannot create workspaces or accept invites.
  - **Free workspace quota**: Each user account is restricted to owning at most 1 workspace on the `free` plan.
  - **Administrative suspension**: Server administrators can immediately suspend or delete abusive workspaces via the CLI or admin dashboard.

### 6. Firebase authentication and token verification

- **Threat**: An attacker presents a forged, expired, or tampered Firebase ID token to gain unauthorized access.
- **Controls**:
  - **Cryptographic verification**: Tokens are verified locally using RS256 signature validation against Google's public keys. Expiry, issuer (`https://securetoken.google.com/<project>`), audience, and `auth_time` are validated.
  - **Immutable UID linkage**: Server accounts are linked to the token's immutable Firebase `uid`, never by email address alone.
  - **No token pass-through**: The client sends the Firebase token once to exchange it for an AgenticML session cookie. Subsequent requests rely on the server's own session management.

### 7. S3 object security

- **Threat**: Unauthorized exposure or tampering of object blobs stored in Amazon S3.
- **Controls**:
  - **Server-side encryption**: All objects uploaded to S3 enforce server-side encryption (`AES256` or AWS KMS via `AGENTICML_SERVER_S3_KMS_KEY_ID`).
  - **No pre-signed URLs**: All object uploads and downloads stream through the server process. Every request undergoes authentication and workspace authorization.
  - **Least-privilege IAM roles**: The ECS task role is granted access strictly to the target bucket and prefix. Bucket policies block all public access.

### 8. Many server processes and the residual stale-writer window

- **Threat**: When multiple server processes run concurrently, a paused process (e.g. prolonged garbage collection or hypervisor pause) or a process that lost its database connection might continue writing stale data to shared storage while another process holds the lease.
- **Controls**:
  - **Database locking**: Append operations acquire row locks on `run_files` and transaction-level advisory locks.
  - **Lock confirmation during write**: Append operations verify session locks before writing, again every 8 MiB during streaming writes, and re-read written byte ranges against uploaded temporary bytes prior to committing the new size.
  - **Residual window**: If a process is paused longer than the keepalive window while holding a lock, it could write bytes after losing the lock. Defensive checks narrow this window to the interval between lock confirmations. The server treats uncommitted file bytes on disk as untrusted until the corresponding size is committed in the database.
  - **Atomic file safety**: Atomic file writes use a unique temporary file, fsync, verify locks, compare-and-swap the `(size, sha256)` record, and rename into place under the recording transaction.

### 9. Secrets and credentials management

- **Threat**: Accidental leakage of database credentials, API keys, or session tokens in logs or diagnostic dumps.
- **Controls**:
  - **AWS Secrets Manager**: The PostgreSQL password is stored in Secrets Manager and injected directly into task environments as `PGPASSWORD`. The database URL does not contain credentials.
  - **No secrets in logs or diagnostics**: Database connection strings with passwords are never logged and never printed by `agenticml server status` or `verify`.
  - **One-way hashing**: API keys, session tokens, and invite tokens are stored exclusively as SHA-256 digests. Passwords use `scrypt`.

## Out of scope

The following threats are outside the system's defensive boundary:

- **Compromised server host or container**: If an attacker gains root access or arbitrary code execution on an ECS task or server host, they can inspect memory and environment variables.
- **Authorized user malicious data**: An authenticated member with write permissions is trusted to write valid experiment data within their workspace quota.

## Controls by module

| Module | Enforced security controls |
| --- | --- |
| `server/db/` | Database abstraction, migrations, connection management, dialect handling, PostgreSQL RLS policies, advisory locks (`pg_advisory_xact_lock`), and repository storage. |
| `server/access.py` | Workspace membership authorization (`require_workspace_member`, `require_workspace_manager`), role enforcement (`owner`, `admin`, `member`, `viewer`), uniform 404 responses. |
| `server/keys.py` | Key creation, SHA-256 hashing, constant-time comparison, workspace binding, and immediate revocation. |
| `server/firebase.py` | RS256 Firebase ID token signature verification, audience/issuer validation, verified email checks, and open sign-up account provisioning. |
| `server/limits.py` | Quota enforcement for storage, daily upload volume, request rates, member counts, and free workspace allocations. |
| `server/objectstore.py` | Local and S3 object storage, per-workspace path partitioning, streaming SHA-256 verification, SSE-S3/SSE-KMS encryption, and workspace-scoped inventory checks. |
| `server/ingest.py` | Protocol v2 routing, bearer key authentication, workspace resolution from key, client support window enforcement (HTTP 426), body size limits, and concurrency caps. |
| `server/backup.py` | Consistent online snapshots, SQLite backup API, database-first ordering, and committed size truncation. |
| `server/lineage.py` | Bounded Git bundle headers, isolated quarantine validation, immutable ref updates, and sanitized Git execution environments. |
| `store/layout.py` | Path traversal prevention, `O_NOFOLLOW` file operations, regular file checks (`S_ISREG`), and hard link rejection. |
| `ui/server.py` | Dashboard sessions, CSRF headers (`X-AgenticML-Request`), Origin checks, Host header validation, and role-based note editing. |

## Known limits

- **Audit log rotation**: While audit events are written to the database or an append-only file, cryptographic hash-chaining is not implemented. In self-hosted mode, operators should configure log rotation.
- **Stale writer residual window**: As documented above, a process paused longer than database keepalive timeouts could write to file storage after losing its lease; all readers treat uncommitted bytes as untrusted until committed in the database.
- **Client TLS termination**: The server process speaks plain HTTP and relies on an external TLS-terminating reverse proxy or ALB in production.

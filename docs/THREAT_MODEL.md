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
7. **Audit trail**: Audit records, added but never edited (except that `server accounts delete` redacts the server-level entries that name the deleted account), stored in the `audit_events` table (or local `audit.jsonl`), tracking security-relevant actions, authentication, membership changes, plan updates, and deletions. No secrets are ever written to the audit log.

## Trust boundaries

Trust boundaries define the isolation model across single-tenant and multi-tenant configurations:

1. **Client / Training Node ↔ Reverse proxy ↔ Server**: The server speaks HTTP on a private interface behind a TLS-terminating reverse proxy (e.g. AWS ALB, Tailscale, Caddy, or nginx). The server trusts `X-Forwarded-For` and proxy identity headers only when requests originate from configured `--trusted-proxy` CIDR ranges. In private-network deployments:
   - `--trusted-proxy` is configured strictly as the proxy's exact `/32` address (`127.0.0.1/32`), never a subnet.
   - Reverse proxies must strip incoming client-supplied identity headers (`X-Forwarded-Email`, `X-Amzn-Oidc-Identity`, `Tailscale-User-Login`).
   - Enable `--identity-header` only when an authenticating proxy (such as oauth2-proxy) sits directly in front of the server and is the only trusted proxy; the server cannot tell whether the proxy authenticated the user.
   - Reverse proxies must exclude query strings from access logs to prevent recording one-time setup and invite tokens.
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
  - **Uniform 404 responses**: Requests on the dashboard and API for non-existent workspaces, workspaces the caller does not belong to, and suspended workspaces return identical HTTP `404 Not Found` responses. An attacker cannot enumerate valid workspace slugs. A sync client holding a valid key for a suspended workspace gets `403` `workspace suspended` (it already knows the workspace exists).
  - **PostgreSQL Row-Level Security (RLS)**: As defense-in-depth, PostgreSQL migrations define RLS policies on all workspace tables. The active workspace ID is set via `SET LOCAL app.workspace_id` within each transaction. Any query executing without the setting returns zero rows.
  - **Per-workspace object partitioning**: S3 objects are stored under `{prefix}/{workspace_id}/objects/{hh}/{sha256}`. Missing-object checks (`POST /v2/objects/missing`) inspect only the calling workspace's inventory. There is no cross-workspace object deduplication, preventing side-channel probing of whether another customer possesses a specific file.
  - **Isolation test suite**: CI runs a dedicated isolation suite that enumerates all server routes and asserts that cross-workspace access attempts return HTTP 404.

### 2. Operator access to customer data

- **Threat**: A platform administrator or support engineer abuses administrative rights to access proprietary customer models, code, or metrics.
- **Controls**:
  - **No ambient data access**: The `server_admin` flag grants authority over platform operations (workspace creation, quotas, account status), but grants no access to workspace runs, files, or metrics.
  - **Support access protocol**: If a customer requires support, a workspace administrator must explicitly invite the support user account as a `member` of their workspace.
  - **Audit logging**: Support invitations, member role assignments, and data access are permanently recorded in the workspace audit trail (`audit_events`).
  - **Cloud infrastructure access**: AWS operators with ECS Exec have `ExecuteCommand` API calls recorded in AWS CloudTrail (including the `--command` string); interactive session contents are deliberately not logged (the cluster has no `ExecuteCommandConfiguration`), because keys and invite links printed in a session would be copied into CloudWatch; the task runs with an init process so session leftovers are reaped.

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
  - **Administrative suspension**: Server administrators can immediately suspend or archive abusive workspaces on the admin dashboard or with the CLI. Permanent deletion is the CLI only (`server workspaces delete`).

### 6. Firebase authentication and token verification

- **Threat**: An attacker presents a forged, expired, or tampered Firebase ID token to gain unauthorized access.
- **Controls**:
  - **Cryptographic verification**: Tokens are verified locally using RS256 signature validation against Google's public keys. Expiry, issuer (`https://securetoken.google.com/<project>`), audience, and `auth_time` are validated.
  - **UID linkage**: The first link of a credential-less account (created by an admin, an invite, or the list) is by verified email; after that by Firebase uid.
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

### 10. Admin emails and server administrator provisioning

- **Threat**: An attacker attempts to escalate privileges to server administrator through email spoofing, account takeover, or unauthorised list modification.
- **Controls**:
  - **Protected configuration**: The admin email list lives in the protected GitHub `hosted` environment (requiring manual reviewer approval) and the stack parameter (`AdminEmails` / `AGENTICML_SERVER_ADMIN_EMAILS`).
  - **Verified email matching**: Only a Firebase-verified email address (Google OAuth or email/password with email verified) matches the list (compared case-insensitively). Unverified emails and untrusted identity providers are rejected.
  - **No account takeover**: The list never takes over an account that has a password or another identity. Accounts created by the list get a personal workspace only when open sign-up is on, at most once, within the free limits.
  - **Scoped revocation**: List removal revokes only list-granted rights, at the next sign-in (use `/admin` Remove to revoke at once). Rights granted on `/admin` or via the CLI are kept.
  - **Last server admin guard**: The last server admin who can sign in (password, identity proxy, Firebase or OIDC) is never removed by the list, web, or CLI; a pending admin that has never signed in does not count.
  - **No ambient workspace access**: Server administrators see no workspace data without membership. An admin without workspace membership lands on `/workspaces` with an Admin button and Sign out and cannot access customer runs, files, or objects.

### 11. Client transport security and trust controls

- **Threat**: Interception, eavesdropping, or tampering with training data and machine credentials in transit; exfiltration of API keys via plain HTTP or HTTP redirects; local privilege escalation through untrusted CA bundles; or information leakage through exception details.
- **Controls**:
  - **Certificate verification is never disabled**: TLS certificate verification is always enforced on HTTPS connections; there is no flag, environment variable, or setting to turn verification off.
  - **Per-exact-host HTTP consent**: Non-local plain `http://` URLs are rejected by default. Passing `--insecure` on `agenticml login` records consent strictly for that exact normalized host and port; subsequent commands to other hosts still require HTTPS. Commands that send API keys over plain HTTP emit an explicit warning to stderr (`warning: sending the API key over plain http to <host>`).
  - **CA bundle ownership and permissions**: Custom CA bundles and their parent directories must be regular files owned by the current user or root, and neither the file nor its directory may be writable by group or other users (`mode & 0o022 == 0`). Saved bundle paths must be absolute.
  - **No redirects followed**: The client strictly refuses HTTP 3xx redirects on all requests, aborting immediately without transmitting the bearer API key to a redirected location. The target `Location` header is never reflected in error messages.
  - **Fixed-text TLS error messages**: TLS and certificate errors display sanitized, fixed hint strings. Raw exception text from `requests` or `ssl` is never echoed, preventing leakage of internal network details or exception parameters.

### 12. OIDC sign-in

- **Threat**: An attacker attempts to forge tokens, tamper with authentication state, hijack accounts during provider migrations, intercept invite tokens from URLs, or bypass sign-up restrictions.
- **Controls**:
  - **Issuer as trust root**: The configured `--oidc-issuer` is the trust root. The server validates the provider discovery document (`.well-known/openid-configuration`), verifies that the issuer claims match the configured URL, and caches discovery and JWKS public keys by the response's `Cache-Control` max-age, clamped between 5 minutes and 24 hours; a failed fetch is not retried for 30 seconds; an unknown key id causes at most one refetch per 60 seconds.
  - **Single-use hashed state**: State tokens are generated cryptographically, stored server-side in the database under a SHA-256 hash, and bound to a per-slot browser cookie (`__Host-aml_oidc_<0-7>`). State records expire after 10 minutes and are consumed on first callback (single-use).
  - **PKCE, nonce, and RS256 token verification**: The server generates the S256 challenge and sends the verifier to the token endpoint; the provider checks it. The server checks the nonce in the ID token and accepts RS256 only. Symmetric algorithms (`HS256`) and `alg=none` are rejected. The server validates `iss` and `aud` (when `aud` is a list, `azp` must equal the client ID). For Cognito tokens, `token_use == "id"` is enforced.
  - **Rate limiting on start**: Calls to `/auth/oidc/start` are limited to 30 per minute per client address (in-memory bucket per process, returning HTTP 429 with `Retry-After`). The server bounds pending states to at most 2,000 and each browser to at most 8 pending cookies.
  - **Invite token privacy**: Invites are accepted only through `POST /auth/oidc/start`, with the token in the form body (capped at 4 KB; a cross-site `Origin`/`Sec-Fetch-Site` is refused). `GET /auth/oidc/start` rejects an `invite` parameter. Invite links carry the token in the URL fragment, which browsers do not send, and the token is never passed to the identity provider, so it does not appear in request lines or load balancer logs. OIDC servers never produce legacy `/invite?token=` links (which put the token in the query). If a client visits such a link, the server serves the sign-in page without reading the token; client-side JavaScript moves the token from the query into the form, switches the form method to POST, and removes the query from the browser address bar with `history.replaceState`.
  - **Email claim validation**: An `email` claim that does not parse as a valid address is ignored and treated as unverified. It never overwrites a stored account email.
  - **UID format and linking rules**: The stored identifier is `oidc:<issuer tag>:<sub>`, where the tag is 12 hex digits derived from the issuer URL. A verified email links an OIDC sign-in only to an account that has no password, identity or provider link yet; accounts with a password or a proxy identity and no provider link are not linked. An account linked to a Firebase uid, or to an OIDC uid from a different issuer (for example a re-created Cognito user pool), is relinked at the next sign-in with the same verified email, audited as `account.relink`. The same issuer with a different subject is refused. `/setup` is off with OIDC; the first admin comes from `--admin-emails`.

### 13. Self-deployed AWS stack

- **Threat**: Unauthorized network access, metadata exfiltration, data volume loss, or unauthorized account creation in a customer-deployed AWS environment.
- **Controls**:
  - **Network isolation**: The EC2 instance security group admits traffic on port 8080 only from the Application Load Balancer security group. Port 22 is closed and SSH keys are disabled. Administrative access is restricted to AWS Systems Manager Session Manager.
  - **Instance metadata and storage encryption**: IMDSv2 is enforced with a hop limit of 1. The root volume and gp3 data volume are encrypted with AWS KMS. The instance IAM role follows least privilege, granting `AmazonSSMManagedInstanceCore` and scoped EC2 volume attachment permissions.
  - **Trusted proxy subnets**: The server configures `--trusted-proxy` strictly to the ALB subnets, preventing spoofed `X-Forwarded-For` headers from untrusted network clients.
  - **Cognito sign-up and pre-sign-up filter**: Sign-up is enabled on the public Cognito user pool. When `AllowedEmailDomains` is configured, an inline pre-sign-up Lambda trigger always admits the `AdminEmail` and rejects sign-ups from outside those domains; it also blocks invitees outside the domains. The server enforces the domain check independently on open sign-up. Note that `AllowedEmailDomains` gates sign-up only; an admitted user updating their email later is governed by Cognito email verification.
  - **Single writer during upgrades**: The Auto Scaling group maintains `MinSize=1`, `MaxSize=1`, and `DesiredCapacity=1`, with `AutoScalingRollingUpdate` specifying `MinInstancesInService=0` and `MaxBatchSize=1`. An existing instance terminates and detaches the data volume before a replacement instance attaches it, guaranteeing a single writer on SQLite.
  - **Crash-consistent snapshots**: Daily Amazon Data Lifecycle Manager (DLM) snapshots capture the EBS data volume while the server runs. Snapshots are crash-consistent; integrity is verified after restore with `agenticml server verify`. Point-in-time consistent backups can be taken at any time with `agenticml server backup`.

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
| `server/oidc.py` | OpenID Connect discovery and JWKS caching, RS256 token verification, PKCE S256 challenge generation; the provider checks the verifier, hashed state lifecycle, and provider logout redirects. |
| `server/webauth.py` | External sign-in dispatch (`_external_sign_in`), `oidc:<issuer tag>:<sub>` UID mapping, verified email relinking, `--signup-email-domains` enforcement, session cookies, and login rate limiting. |
| `server/limits.py` | Quota enforcement for storage, daily upload volume, request rates, member counts, and free workspace allocations. |
| `server/objectstore.py` | Local and S3 object storage, per-workspace path partitioning, streaming SHA-256 verification, SSE-S3/SSE-KMS encryption, and workspace-scoped inventory checks. |
| `server/ingest.py` | Protocol v2 routing, bearer key authentication, workspace resolution from key, client support window enforcement (HTTP 426), body size limits, and concurrency caps. |
| `server/backup.py` | Consistent online snapshots, SQLite backup API, database-first ordering, and committed size truncation. |
| `server/lineage.py` | Bounded Git bundle headers, isolated quarantine validation, immutable ref updates, and sanitized Git execution environments. |
| `store/layout.py` | Path traversal prevention, `O_NOFOLLOW` file operations, regular file checks (`S_ISREG`), and hard link rejection. |
| `ui/server.py` | Dashboard sessions, CSRF headers (`X-AgenticML-Request`), Origin checks, Host header validation, and role-based note editing. |
| `auth.py` | Connection resolution (`resolve_connection`), CA bundle validation and POSIX mode checks (`load_ca_bundle`), per-exact-host insecure HTTP consent, credentials file permissions, and plain HTTP warnings. |
| `client.py` | TLS verification enforcement, fixed-text TLS failure hints (`tls_hint`), redirect rejection without credential leakage, and body streaming. |

## Known limits

- **Audit log rotation**: While audit events are written to the database or an append-only file, cryptographic hash-chaining is not implemented. In self-hosted mode, operators should configure log rotation.
- **Stale writer residual window**: As documented above, a process paused longer than database keepalive timeouts could write to file storage after losing its lease; all readers treat uncommitted bytes as untrusted until committed in the database.
- **Contact button (nothing leaves the server)**: The dashboard's Feedback or Contact us button only builds a `mailto:` link, so no data leaves the server and no third-party code loads. A user who presses Send in their own email app chooses to email the project (default) or the operator's `--contact-email` address. The draft holds only the server version, plus the page and workspace slug when the operator set an address. The server accepts only a plain `local@domain.tld` address, so the link cannot carry extra headers such as `cc` or `bcc`.
- **Client TLS termination**: The server process speaks plain HTTP and relies on an external TLS-terminating reverse proxy or ALB in production. On private networks, reverse proxies must strictly enforce the private-network conditions (exact `/32` trusted proxy, stripping identity headers, and omitting query strings from access logs).

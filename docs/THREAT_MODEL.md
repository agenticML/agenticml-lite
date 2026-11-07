# Threat Model

This document outlines the security architecture, trust boundaries, threat landscape, and defensive controls of `agenticml server`.

## Assets

The server protects five primary assets:

1. **Run data**: Experiment metrics, configurations, summaries, events, artifacts, and content-addressed objects stored in `<data>/store/`.
2. **Keys**: Machine and administrator credentials. On-disk key state lives in `<data>/server/keys.json` (mode `0600`), storing SHA-256 digests, owning user names, and metadata, never plaintext keys.
3. **User accounts and credentials**: `<data>/server/users.json` (mode `0600`), storing user accounts, roles, proxy identities, account-lifetime IDs, generation counters, scrypt password hashes with unique random salts, and SHA-256 digests of pending invite tokens. Plaintext passwords and tokens are never stored.
4. **Audit trail**: `<data>/server/audit.jsonl` (mode `0600`), recording every state-changing action, authentication event, and run lifecycle transition. No secrets are ever written to the log.
5. **Dashboard sessions**: Ephemeral authentication state held in server memory for authenticated browser sessions, bound to specific user account IDs and generation counters.

## Trust boundaries

Four boundaries define the trust perimeter:

1. **Network ↔ Reverse proxy / Identity proxy ↔ Server**: The server speaks plain HTTP on a local or private interface. It relies on an external reverse proxy (such as AWS ALB, Tailscale Serve, oauth2-proxy, or Caddy) to terminate TLS and forward requests. When configured with `--trusted-proxy`, the server trusts `X-Forwarded-For` client addresses and proxy identity headers only on connections originating from configured proxy IP ranges. Connections from unconfigured addresses cannot assert identities or forwarded client IPs.
2. **Training machine ↔ Server**: Training clients authenticate to the server over HTTP using bearer tokens over [protocol v2](PROTOCOL.md).
3. **Dashboard user ↔ Server**: Users authenticate to the dashboard via username and password, through a trusted identity proxy, or via an admin key.
4. **Server ↔ Data directory**: The server process reads and writes experiment, user, and authentication files on the local filesystem at `<data>/`.

## In-scope threats

The following threats are within the design and threat model of the system:

- **Unauthenticated network attacker**: An attacker without an API key attempts to query runs, write arbitrary files, access the dashboard, or exhaust server resources. Mitigated by requiring bearer authentication on all `/v2` endpoints, credential check on `/login`, and Host header enforcement. Only `GET /healthz` is accessible without authentication.
- **Header spoofing from untrusted peers**: An untrusted client attempts to spoof client IP addresses via `X-Forwarded-For` or assert arbitrary identities using proxy identity headers (`X-Amzn-Oidc-Identity`, `Tailscale-User-Login`, `X-Forwarded-Email`). Mitigated by only inspecting forwarded addresses and identity headers on connections originating from configured `--trusted-proxy` CIDRs. Startup refuses catch-all ranges (`0.0.0.0/0` and `::/0`) and requires at least one `--trusted-proxy` when `--identity-header` is configured. Untrusted peers have identity headers ignored and use their direct TCP peer address.
- **Password guessing and brute force**: An attacker attempts to guess user passwords via `/login` or `/api/server/account/password`. Mitigated by computationally expensive `scrypt` hashing ($N=16384, r=8, p=1$), constant-time hash comparisons (`hmac.compare_digest`), dummy scrypt evaluations for non-existent users to eliminate timing side channels, and dual-bucket rate limiting (keyed by real client IP from the rightmost untrusted hop in `X-Forwarded-For` on trusted-proxy connections, and by target user name bucket `user:<name>`).
- **Invite token theft and replay**: An attacker attempts to guess or intercept one-time invite tokens. Mitigated by high-entropy 32-byte URL-safe tokens (`secrets.token_urlsafe(32)`), storing only their SHA-256 digests in `users.json`, single-use redemption, constant-time digest comparison across all pending invites, and a 72-hour expiration window.
- **Session revival after user recreation**: An attacker attempts to reuse an old session cookie after an account is deleted and recreated with the same name. Mitigated by binding sessions to an account-lifetime 32-hex `id` and a monotonic `generation` counter. Re-inviting a user, changing their password, or deleting the user invalidates all existing sessions immediately.
- **Privilege escalation between roles**: An authenticated user attempts operations outside their assigned role (`viewer`, `member`, `admin`). Mitigated by strict role checks on every request: viewers can only read the dashboard (their sync keys return 403 on `/v2` routes except `whoami`, and note edits return 403); members can sync only their own runs and manage only their own sync keys; only admins can manage users, issue keys for others, hold admin keys, or read the audit log. The last admin user cannot be demoted or removed.
- **Cross-user run writes and hijacking**: A user or key attempts to write to, append to, or overwrite a run created by another user. Mitigated by user-level run ownership (`owner` in `remote.json`). Any key belonging to the owning user can write to the run; keys belonging to any other user receive HTTP 409 Conflict ("run owned by another user").
- **Leaked or revoked API key**: A key is leaked or revoked. Mitigated by least-privilege key scopes (`sync` keys cannot access the dashboard or manage keys), immediate revocation without server restarts (via `mtime` detection on `keys.json`), and immediate revocation of active dashboard sessions bound to revoked keys. Removing a user automatically revokes all of their keys under `keys.lock`.
- **Malicious run files and paths**: An attacker attempts directory traversal (such as `../`), symlink traversal, special file injection (FIFOs, device nodes, Unix domain sockets), or hard links. Mitigated by strict path allow-lists, run ID format validation, path containment checks, no-follow opens (`O_NOFOLLOW`), non-blocking regular-file checks (`S_ISREG`), and hard-link detection.
- **Hostile lineage bundles**: An authenticated uploader may send malformed
  headers, unexpected refs, missing prerequisites, non-commit tips, or corrupt
  or unsafe Git objects. The server caps the body at 256 MiB and the header at
  1 MiB, accepts exactly the metadata's run ref and SHA-1 commit, and binds
  `lineage.project_key` to the key derived from the string `meta.json.project`.
  It validates prerequisites with `git bundle verify` and checks the streamed
  pack with `git index-pack --stdin --fix-thin --strict` in a temporary object
  quarantine under `server/tmp`. The live project's objects are read-only
  alternates during validation via a single absolute path in the quarantine's
  `info/alternates` file. Connectivity and commit type are checked
  before packs are promoted and an immutable run ref is atomically created;
  rejected bundles leave existing refs and live objects unchanged. Rollback
  compare-and-swap deletes only a ref published by that request and removes
  promoted packs unless publication is confirmed or uncertain and ref deletion
  did not succeed; retained packs keep any live ref's objects available.
  Hooks, fsmonitor, automatic GC and commit-graph writes are disabled; only
  the local file protocol is allowed. Inherited `GIT_*` variables and global
  and system config are removed; internal environment overrides allow only
  `GIT_OBJECT_DIRECTORY`. Subprocesses use argument lists, and stdin
  is disconnected except for the streamed pack. Git uses the C locale and
  timeouts of 60 seconds plus the body size divided by 4 MiB/s per command.
  At most two receives run after body upload; exhaustion and transient Git
  or filesystem failures (including repository initialization and ref
  publication failures) return retryable 503 responses. Git output is never
  returned to clients (only the first diagnostic line, capped at 240
  characters, is logged). Bundle files and quarantines are removed after
  every request; startup removes abandoned `lineage-*` quarantine directories
  after a crash and warns about other subdirectories. The server never checks
  out their contents. Git must be installed and kept patched by the operator.

## Out of scope

The following threats are explicitly out of scope:

- **Compromised server host**: If an attacker achieves code execution or root access on the host running the server, they can access server memory, disk storage, and plaintext environment variables.
- **Malicious key holder's own data**: An authenticated key holder has full authority to upload, append, or corrupt runs owned by that same user, within the constraints of protocol v2.

## Controls by module

Each security control is enforced by a specific module:

| Module | Enforced controls |
| --- | --- |
| `server/keys.py` | Key storage in `keys.json` (mode `0600`, version 2 with `user`), SHA-256 key hashing, constant-time token comparison (`hmac.compare_digest`), scope validation (`sync` vs `admin`), key creation and revocation, shared concurrent mutation locking (`keys.lock`) with `UserStore`, and automatic in-memory key cache reload on file modification. |
| `server/users.py` | User registry in `users.json` (mode `0600`), scrypt password hashing ($N=16384, r=8, p=1$), invite token generation and constant-time SHA-256 verification, account ID and generation counters, role validation (`viewer`, `member`, `admin`), last-admin protection, and in-place 2.0/2.1 data migration. |
| `server/webauth.py` | Dashboard authentication, credential checks (passwords, trusted identity proxy headers from ALB, Tailscale, oauth2-proxy, or admin keys), session management (`HttpOnly`, `SameSite=Strict`, `Path=/`; `__Host-aml_session` with `Secure` on HTTPS), session binding to account ID and generation, forwarded client address extraction from `X-Forwarded-For`, dual-bucket login throttling (client IP and username), invite/setup flows, and CSRF protection (`X-AgenticML-Request` header and `Origin` validation). |
| `server/audit.py` | Append-only audit logging to `<data>/server/audit.jsonl` (mode `0600`), non-blocking open with `O_NOFOLLOW` and regular-file check, whole-line writes under advisory `flock`, tailing with pagination, and redaction of secrets. |
| `server/ingest.py` | Protocol v2 endpoint routing, bearer token authentication, user-based run ownership verification via `remote.json`, role-based sync gating (`allowed(principal, "sync")`), compatibility owner mapping, request body size limits, record framing validation, and run creation/finish audit logging. |
| `server/lineage.py` | Bounded bundle headers, exact run ref and commit validation, immutable refs, Git verification/fsck and isolated subprocess configuration. |
| `store/layout.py` | Run ID regex validation (`layout.valid_run_id`), atomic and append file path allow-lists, path containment verification (`contained()`), directory traversal prevention, symlink rejection with `O_NOFOLLOW`, non-blocking open with `S_ISREG` validation to prevent FIFO denial-of-service hangs, and rejection of hard links. |
| `ui/server.py` | Server dashboard request handling, `Host` header enforcement (accepts only `--public-url` host and loopback names), session cookie validation, object serving containment and responses sent with `X-Content-Type-Options: nosniff`, role-aware note editing (`allowed(principal, "notes")`), run owner attribution in API responses, and read-only run inspection. |

## Known limits

The 2.2 release has several known security boundaries scheduled for expansion in upcoming releases:

- **No per-project access control**: Roles are server-wide. Viewers and members can see every project on the server; project-level access control is deferred to a future release.
- **Audit log rotation and tamper evidence**: The audit log is append-only on the local filesystem and reopened per write, but it does not use cryptographic signing or hash-chaining. Operators must configure log rotation (e.g. `logrotate` with `create` mode).
- **Global key name namespace**: Key names are globally unique across the server. A member creating a key can observe whether a key name is already taken by another user via a 409 Conflict response.
- **TLS termination at proxy**: The server process speaks plain HTTP. It must be deployed behind a TLS-terminating proxy (such as AWS ALB, Tailscale Serve, or Caddy) in production.
- **In-memory sessions**: Web dashboard sessions live in process memory. Restarting the server process invalidates all active browser sessions, requiring users to sign in again.

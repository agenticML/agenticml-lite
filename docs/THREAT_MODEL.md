# Threat Model

This document outlines the security architecture, trust boundaries, threat landscape, and defensive controls of `agenticml server` (Release 2.0).

## Assets

The server protects three primary assets:

1. **Run data**: Experiment metrics, configurations, summaries, events, artifacts, and content-addressed objects stored in `<data>/store/`.
2. **Keys**: Machine and administrator credentials. On-disk key state lives in `<data>/server/keys.json` (mode `0600`), storing SHA-256 digests and metadata, never plaintext keys.
3. **Dashboard sessions**: Ephemeral authentication state held in server memory for authenticated browser sessions.

## Trust boundaries

Three boundaries define the trust perimeter:

1. **Network ↔ Reverse proxy ↔ Server**: The server speaks plain HTTP on a local or private interface. It relies on an external reverse proxy (such as AWS ALB, Tailscale Serve, or Caddy) to terminate TLS and forward requests.
2. **Training machine ↔ Server**: Training clients authenticate to the server over HTTP using bearer tokens over [protocol v2](PROTOCOL.md).
3. **Server ↔ Data directory**: The server process reads and writes experiment and authentication files on the local filesystem at `<data>/`.

## In-scope threats

The following threats are within the design and threat model of the system:

- **Unauthenticated network attacker**: An attacker without an API key attempts to query runs, write arbitrary files, access the dashboard, or exhaust server resources. Mitigated by requiring bearer authentication on all `/v2` endpoints, admin key authentication on `/login`, and Host header enforcement. Only `GET /healthz` is accessible without authentication.
- **Leaked or revoked API key**: A key is leaked or revoked. Mitigated by least-privilege key scopes (`sync` keys cannot access the dashboard or manage keys), immediate revocation without server restarts (via `mtime` detection on `keys.json`), and immediate revocation of active dashboard sessions bound to revoked keys.
- **Malicious run files and paths**: An attacker attempts directory traversal (such as `../`), symlink traversal, special file injection (FIFOs, device nodes, Unix domain sockets), or hard links. Mitigated by strict path allow-lists, run ID format validation, path containment checks, no-follow opens (`O_NOFOLLOW`), non-blocking regular-file checks (`S_ISREG`), and hard-link detection.
- **Run ID collision and hijacking**: A second machine or key attempts to write to or overwrite a run created by another machine. Mitigated by run ownership tracking (`owner` in `remote.json` bound to the creating key). Requests to modify an existing run with a differing key are rejected.

## Out of scope

The following threats are explicitly out of scope:

- **Compromised server host**: If an attacker achieves code execution or root access on the host running the server, they can access server memory, disk storage, and plaintext environment variables.
- **Malicious key holder's own data**: An authenticated key holder has full authority to upload, append, or corrupt runs owned by that same key, within the constraints of protocol v2.

## Controls by module

Each security control is enforced by a specific module:

| Module | Enforced controls |
| --- | --- |
| `server/keys.py` | Key storage in `keys.json` (mode `0600`), SHA-256 key hashing, constant-time token comparison (`hmac.compare_digest`), scope validation (`sync` vs `admin`), key creation and revocation, concurrent key modification locking (`keys.lock`), and automatic in-memory key cache reload on file modification. |
| `server/webauth.py` | Dashboard authentication, `POST /login` credential check (accepts `admin` keys only; rejects `sync` keys with 403), session management (always `HttpOnly`, `SameSite=Strict`, `Path=/`; cookie named `__Host-aml_session` with `Secure` when `--public-url` is HTTPS, otherwise `aml_session`), in-memory session lifetime (12-hour idle timeout, 7-day absolute cap), constant-time single-use bootstrap token verification (`/setup`), client IP-based failed login throttling, and CSRF protection (`X-AgenticML-Request` header and `Origin` validation against `--public-url`). |
| `server/ingest.py` | Protocol v2 endpoint routing, bearer token validation against the key store, run ownership verification via `remote.json`, per-run serialization locking, request body size limits, record framing validation, and append offset verification. |
| `store/layout.py` | Run ID regex validation (`layout.valid_run_id`), atomic and append file path allow-lists, path containment verification (`contained()`), directory traversal prevention, symlink rejection with `O_NOFOLLOW`, non-blocking open with `S_ISREG` validation to prevent FIFO denial-of-service hangs, and rejection of hard links. |
| `ui/server.py` | Server dashboard request handling, `Host` header enforcement (accepts only `--public-url` host and loopback names), session cookie validation, object serving containment and responses sent with `X-Content-Type-Options: nosniff`, and read-only run inspection. |

## Known limits

The 2.0 release has several known security boundaries scheduled for expansion in upcoming releases:

- **Admin sign-in by API key**: In 2.0, administrators sign in to the web dashboard using an `admin` API key. Package 13 (Release 2.2) replaces this with trusted identity proxy authentication and dedicated user accounts.
- **No per-user roles**: Release 2.0 provides two operational scopes (`sync` and `admin`), but does not have per-user identity or fine-grained role-based access control. Release 2.2 introduces user identities and roles.
- **TLS termination at proxy**: The server process speaks plain HTTP. It must be deployed behind a TLS-terminating proxy (such as AWS ALB, Tailscale Serve, or Caddy) in production.
- **In-memory sessions**: Web dashboard sessions live in process memory. Restarting the server process invalidates all active browser sessions, requiring administrators to sign in again.

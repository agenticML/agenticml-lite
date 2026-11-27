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

# OIDC ID token verification (PyJWT with cryptography)
pip install "agenticml-lite[oidc]"

# Hosted bundle: postgres, s3, and firebase
pip install "agenticml-lite[hosted]"
```

## Running the server

Start the server using `agenticml server serve`:

```bash
agenticml server serve --data /var/lib/agenticml-server --host 127.0.0.1 --port 8080 --public-url https://aml.example.com
```

To deploy in your own AWS account with a CloudFormation template (EC2, SQLite, ALB, Cognito), see [Deploy on AWS](https://agenticml.xyz/server/deploy-on-aws/).

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
sudo curl -fsSL -o /etc/systemd/system/agenticml-server.service https://raw.githubusercontent.com/agenticML/agenticml-lite/v3.3.1/deploy/systemd/agenticml-server.service
```

The unit is fetched from the `v3.3.1` tag. Use the tag that matches the agenticml-lite version you installed (`pip show agenticml-lite`).

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

## Contact and feedback button

Every dashboard of `agenticml server` shows a floating button after sign-in. It opens a small panel with **Write an email** (opens your email app with the address, a subject and a short draft filled in) and **Copy address**. The page sends nothing: your email app sends the mail when you press Send, and no agenticml server handles it. The dashboard loads no third-party code for it. The local `agenticml ui` shows no button.

What the button does depends on `--contact-email` (see the options table). Only `agenticml server serve` reads it (and the environment variable). `agenticml server up` ignores both and always shows the **Feedback** button below; it cannot set an address or turn the button off yet.

- **Not set (the default).** The button is **Feedback** and writes to the agenticml-lite team at `contact@agenticml.xyz`. The panel says so: it does not reach your server admin. The draft holds only the server version. It never names your workspaces or pages.
- **An address.** The button is **Contact us** and writes to that address, for example your lab's help desk. The draft also holds the page and workspace the user was on and the server version. The workspace picker gets a Contact us link, and so does the Firebase sign-in page.
- **`off`.** No button and no links.

```bash
# Contact us goes to your own address
agenticml server serve --contact-email help@lab.example.org ...
# or: AGENTICML_SERVER_CONTACT_EMAIL=help@lab.example.org

# No button at all
agenticml server serve --contact-email off ...
```

The hosted service (`app.agenticml.xyz`) sets `contact@agenticml.xyz` through the `ContactEmail` stack parameter, so it runs with a **Contact us** button.

## Private network (no Tailscale)

Run the server and dashboard inside a private VPC or lab network without Tailscale or internet connectivity. All traffic starts inside the network, terminated by a local TLS reverse proxy with an internal certificate.

```
training machines ──HTTPS :443──▶ TLS proxy (internal cert) ──▶ agenticml server serve
lab users' browsers ──HTTPS :443──┘        Caddy or nginx            127.0.0.1:8080, SQLite on local disk
```

The server binds loopback (`127.0.0.1:8080`) and speaks HTTP. TLS ends at the reverse proxy.

### Setup checklist

See [deploy/private-network/README.md](../deploy/private-network/README.md) for the complete operator guide. In brief:

1. **Prerequisites**: Python 3.10+ on the server and training machines. Install `git` on the server for lineage code diffs. Run NTP or chrony across all machines to prevent TLS clock errors. Assign a DNS name (or `/etc/hosts` entry) to the server.
2. **Install `agenticml-lite>=3.2`**: Install on the server (`/opt/agenticml`) and training machines. For machines without internet access, download wheels on a connected machine with matching OS and Python minor version via `pip download -d wheelhouse 'agenticml-lite>=3.2'`, transfer the directory, and install using `sudo /opt/agenticml/bin/python -m pip install --no-index --find-links wheelhouse 'agenticml-lite>=3.2'`. Fetch deployment configs from the matching release tag.
3. **Configure systemd service**: Install `agenticml-server.service` and copy `deploy/private-network/agenticml-server.env.example` to `/etc/agenticml-server.env` (`0600`, root). Set `AGENTICML_SERVER_PUBLIC_URL` to your canonical HTTPS URL (including any non-443 port) and `AGENTICML_SERVER_TRUSTED_PROXIES=127.0.0.1/32`. Do not set `AGENTICML_SERVER_KEY`, which disables the first-admin setup link.
4. **Configure reverse proxy**: Set up Caddy (using `tls internal` or your own certificates) or nginx using configs from `deploy/private-network/`. Under SELinux, run `sudo setsebool -P httpd_can_network_connect 1`.
5. **Distribute CA certificate**: For Caddy's internal CA, copy the root certificate (`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`) or your internal PKI root CA to `/etc/ssl/certs/lab-ca.pem` on each training machine. Install the CA in users' browsers or OS certificate stores. Back up Caddy's data directory so regenerated roots do not invalidate clients.
6. **Create first admin**: Before exposing port 443 to users, retrieve the one-time setup link from `sudo journalctl -u agenticml-server | grep 'setup?token'` and complete admin registration in a browser. Alternatively, run `sudo -u agenticml /opt/agenticml/bin/agenticml server users add NAME --role admin --public-url https://... --data /var/lib/agenticml-server`.
7. **Issue sync keys**: Issue sync keys from the dashboard Keys page or CLI (`sudo -u agenticml /opt/agenticml/bin/agenticml server keys create --name box-1 --scope sync --user NAME --data /var/lib/agenticml-server`). The machine's runs are owned by that user; a key created on the dashboard Keys page belongs to the signed-in person.
8. **Configure training machines**: Run `agenticml login --host https://... --ca-bundle /etc/ssl/certs/lab-ca.pem`. Verify with `agenticml whoami` and `agenticml sync --all`. Install the user service `agenticml-sync.service` for continuous synchronization.
9. **Firewall rules**: Allow inbound connections to port 443 (and port 80 for browser redirects) on the proxy. Port 8080 must stay on loopback.
10. **Automate backups**: Add a nightly cron job running `agenticml server backup` as the `agenticml` user.

### Reverse proxy rules

A reverse proxy in front of the server must satisfy these requirements:

- **No redirects on sync requests**: The client rejects HTTP 3xx redirects to prevent sending API keys across hosts or schemes. Sync clients must connect directly to the final `https://` URL.
- **Forward Content-Length without re-chunking**: Streaming upload requests must retain their `Content-Length`. Re-chunking request bodies causes the server to return HTTP 411 Length Required.
- **Support 4 GiB request bodies**: Object uploads (large files and artifacts) can be up to 4 GiB in one request (`OBJECT_BODY_LIMIT`). Run files are capped at 16 MiB, appends at 64 MiB and lineage bundles at 256 MiB. Set `client_max_body_size 4g` in nginx.
- **Long timeouts**: Set proxy read and send timeouts to at least 3 hours (`proxy_read_timeout 3h`, `proxy_send_timeout 3h`). Server body timeouts allow approximately 30 seconds plus 2 seconds per MiB (~2.3 hours for 4 GiB).
- **Forward the Host header**: Forward `Host` unchanged (`proxy_set_header Host $http_host` in nginx). Its host name must be the public URL's host name; the server does not compare the port. The port in `AGENTICML_SERVER_PUBLIC_URL` must be the one users type, because the browser's `Origin` (scheme, host and port) must equal it.
- **Strip identity headers**: Drop incoming `X-Forwarded-Email`, `X-Amzn-Oidc-Identity`, and `Tailscale-User-Login` headers from clients so untrusted clients cannot spoof authenticated identities.
- **Filter query strings from access logs**: Setup and invite tokens travel in query strings (`/setup?token=...`, `/invite?token=...`). Omit query strings from proxy access logs.
- **Strict trusted proxy CIDR**: Configure `AGENTICML_SERVER_TRUSTED_PROXIES` with the proxy's exact IP as `/32` (`127.0.0.1/32`), never a broad subnet.

### Sign-in options

- **Local passwords and invite links (default)**: Users accept one-time invite links generated by administrators and sign in with local passwords (`scrypt`). Firebase remains disabled (`AGENTICML_SERVER_FIREBASE_PROJECT` unset).
- **Single sign-on with oauth2-proxy (variant)**: To authenticate users via an external identity provider, chain `client -> TLS proxy -> oauth2-proxy -> agenticml server serve`:
  1. Configure `--identity-header oauth2-proxy` (or `AGENTICML_SERVER_IDENTITY_HEADER=oauth2-proxy`) and set `AGENTICML_SERVER_TRUSTED_PROXIES` strictly to oauth2-proxy's `/32` address.
  2. The outer TLS proxy must strip any incoming `X-Forwarded-Email` header from clients.
  3. oauth2-proxy must skip authentication for `/v2/` and `/healthz` routes; sync clients authenticate using Bearer API keys and cannot negotiate browser SSO flows.
  4. Map user accounts to emails using `sudo -u agenticml /opt/agenticml/bin/agenticml server users set-identity LOGIN email@example.com --data /var/lib/agenticml-server`.

### Sign-in with an OIDC provider (Cognito, Okta, Entra ID)

The server supports single sign-on with OpenID Connect (OIDC) identity providers such as Amazon Cognito, Okta, and Microsoft Entra ID.

The integration uses the standard OpenID Connect authorization code flow with PKCE (RFC 7636, S256 code challenge) and a public client (no client secret is required or supported).

Requirements and behavior:
- **Dependency**: Install the `[oidc]` extra (`pip install 'agenticml-lite[oidc]'`). The server refuses to start without it when `--oidc-issuer` is set.
- **Provider registration**: Register a public application client at the provider with:
  - Callback URL (redirect URI): `<public-url>/auth/oidc/callback` (for example `https://aml.example.com/auth/oidc/callback`)
  - Post-logout redirect URI: `<public-url>/login` (only used with `--oidc-logout-url`)
  - Required scopes: `openid email profile`
- **ID token requirements**: The ID token must be RS256-signed and carry `email` with `email_verified: true`. Without a verified email, nobody can be linked, signed up, listed as admin or invited. Cognito with email verification sets it. Okta and Entra ID have not been tested yet; only the Cognito path is the documented deployment. Entra ID needs a verified-email claim configured.
- **Sign-out**:
  - The post-logout redirect URI `<public-url>/login` is only used with `--oidc-logout-url`.
  - That option is optional and sends `client_id` and `logout_uri` as query parameters, which is Cognito's form. Okta and Entra ID use `post_logout_redirect_uri`, so provider sign-out there works only if the provider accepts these parameters.
  - Without the option, sign-out ends the AgenticML session only.
- **HTTPS required**: `--public-url` must use the `https://` scheme (except `http://127.0.0.1` and `localhost` in local testing).
- **One provider per server**: Configure at most one sign-in provider. Running OIDC together with Firebase (`--firebase-project`) or an identity proxy header (`--identity-header`) is refused at startup.
- **Open sign-up and admin emails**: Both `--open-signup` and `--admin-emails` work with OIDC. A user completing a verified OIDC sign-in gets a personal workspace if open sign-up is enabled, and becomes server administrator if their email matches `--admin-emails`. `/setup` is off with OIDC; the first admin comes from `--admin-emails`.
- **Domain restrictions (`--signup-email-domains`)**: Comma-separated list of allowed domains (e.g. `example.com,example.org`). Open sign-up provisions accounts only for email addresses whose domain matches exactly (case-insensitive, no subdomain wildcards). It also withholds the personal workspace from existing accounts whose email is outside the listed domains. Accounts on the `--admin-emails` list are exempt from domain restrictions.
- **Invites accepted via POST**: When inviting users, invite links use the fragment format (`/invite#token=...`). The client accepts the invite by submitting `POST /auth/oidc/start` with the token in the form body. `GET /auth/oidc/start` explicitly rejects invite tokens, preventing tokens from appearing in query strings or load balancer logs.
- **Account linking and relinking rules**:
  - The stored identifier is `oidc:<issuer tag>:<sub>`, where the tag is 12 hex digits derived from the issuer URL.
  - A verified email links an OIDC sign-in only to an account that has no password, identity or provider link yet. Accounts with a password or a proxy identity and no provider link are not linked.
  - An account linked to a Firebase uid, or to an OIDC uid from a different issuer (for example a re-created Cognito user pool), is relinked at the next sign-in with the same verified email, audited as `account.relink`.
  - The same issuer with a different subject is refused.

### Proxy bypass with NO_PROXY

If training machines define `HTTPS_PROXY` in their environment, add the server hostname to `NO_PROXY`:

```bash
export NO_PROXY=agenticml.lab.internal
```

Add the same setting to `~/.config/systemd/user/agenticml-sync.service` (`Environment=NO_PROXY=agenticml.lab.internal`) so sync traffic reaches the internal server directly.

### Client CA bundles and host trust

When connecting to internal HTTPS servers signed by private CAs:

- **CA bundle precedence**:
  1. `--ca-bundle PATH` flag on `agenticml login`, `sync`, `whoami`
  2. CA bundle saved for that exact host by `agenticml login --ca-bundle`
  3. `AGENTICML_CA_BUNDLE` environment variable
  4. System default CA roots (honours `REQUESTS_CA_BUNDLE`)

  `login` ignores the bundle saved for the host: it uses `--ca-bundle`, then `AGENTICML_CA_BUNDLE`, then the system roots. A successful `login` replaces the saved entry for the host, so pass `--ca-bundle` on every `login`. Items 1-4 apply to `sync` and `whoami`.
- **Why save bundles per host**: Specifying a CA bundle replaces the default roots rather than adding to them. A workstation syncing to both an internal lab server and the public hosted service (`app.agenticml.xyz`) should save the bundle per host using `agenticml login --ca-bundle` rather than exporting `AGENTICML_CA_BUNDLE` globally, which would cause public certificate verification to fail.
- **Verification cannot be disabled**: There is no flag or option to disable TLS certificate checks.
- **File ownership and permissions**: Custom CA bundles and their parent directories must be owned by the user or root and must not be group- or world-writable (`mode & 0o022 == 0`).
- **Plain HTTP trust**: Running `agenticml login --insecure` to a non-local `http://` host records consent for that exact host in `credentials.json`. Subsequent `sync` and `whoami` commands to that saved host do not require the `--insecure` flag. Older saved entries require one more `login --insecure` to store this flag. Commands that transmit API keys over plain HTTP emit `warning: sending the API key over plain http to <host>`. Running `login` again for a host replaces its saved entry, so repeat `--ca-bundle` or `--insecure`.

## Workspaces and routing

All data on the server is scoped to a workspace.

### Workspace URLs

- Dashboard views: `/w/<slug>/...` (e.g. `/w/default/`, `/w/team-a/runs/20261109...`).
- Dashboard APIs: `/api/w/<slug>/...`.
- Global routes (no workspace prefix): `/account`, `/login`, `/invite`, `/healthz`, and `/admin/workspaces`.

The workspace switcher is in the **Account** section of the dashboard's **Settings** page. It appears when an account belongs to more than one workspace, when open sign-up is on, or for a server administrator. Navigating to `/` redirects to the last-used workspace or the workspace selection page.

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
| View workspace name, plan and usage (Settings > Workspace) | – | – | yes | yes |
| Rename the workspace (Settings > Workspace, `PATCH /api/w/<slug>`) | – | – | yes | yes |

- **`owner`**: The creator of the workspace. A workspace has exactly one owner. The owner cannot be removed or demoted. An owner or admin can rename the workspace in **Settings > Workspace** or with `PATCH /api/w/<slug>` and a body of exactly `{"name": "..."}`; the name is trimmed, must be 1 to 256 characters and may not contain control characters, and the slug is never changed. The rename is recorded as `workspace.rename` in the workspace's Activity and in the server log. Only the server operator can change the slug (on `/admin` or `PATCH /api/admin/workspaces/<slug>`), transfer ownership (`server workspaces set-owner`) or delete a workspace (`server workspaces delete`).
- **`admin`**: Full administrative rights within the workspace (members, invites, keys, notes); can view the workspace's plan and usage.
- **`member`**: Can sync runs, edit notes, and manage their own sync keys.
- **`viewer`**: Read-only dashboard access. Viewer sync keys can only call `GET /v2/whoami`; all other `/v2` endpoints return `403 Forbidden`.

### Server administrators

A global account can have the `server_admin` flag set. Server administrators:
- Manage workspaces: create, rename, suspend, resume, and delete workspaces. On `/admin`, **Archive** suspends a workspace (not `default`), keeps its data and stops it counting toward the owner's free workspace limit; **Restore** brings it back as suspended. Permanent deletion is the CLI `workspaces delete` only.
- Manage accounts: list accounts, create accounts, and grant or revoke server administrator privileges.
- View global storage and usage across all workspaces.
- Configure workspace plans and limits.
- **Isolation boundary**: Server administrators cannot view, list, or download runs or files within a workspace unless they are explicitly added as a member of that workspace. An admin without workspace membership lands on `/workspaces` with an Admin button and Sign out and sees no customer workspace data.

Server administrator privileges can be granted through three paths:

- **Admin emails (`--admin-emails` / `AGENTICML_SERVER_ADMIN_EMAILS`)**: Comma-separated list of emails, matched case-insensitively. Requires Firebase or OIDC authentication (`--firebase-project` or `--oidc-issuer`). Without Firebase or OIDC, the list is ignored with a startup warning. Invalid entries are dropped with a warning. When a user completes a verified Firebase or OIDC sign-in (Google OAuth, OIDC, or email/password with verified email) matching the list, their account is created if none exists and granted server admin with source `list`. With `--open-signup` the account also gets the free personal workspace (once); without it, no workspace is created. If an email is removed from the list, list-granted server admin is revoked at that person's next sign-in.
- **Web administration (`/admin`)**: The "Server admins" section lists all server administrators (login, source, and sign-in state). Administrators can add server admins by email or login. Adding an email without an existing account creates a credential-less account that links on its first verified Firebase or OIDC sign-in (`source` is `manual`). Administrators can remove server admins immediately; an email still in `--admin-emails` will regain server admin at its next sign-in.
- **Server CLI (`agenticml server accounts set-server-admin`)**: `set-server-admin LOGIN on|off` and `accounts add LOGIN --server-admin` grant or revoke server admin with source `manual`. Manual grants survive removal of an email from `--admin-emails`.

**Last server admin rule**: The last server administrator who can sign in (has a password, an identity, or a linked Firebase or OIDC account) can never be removed through the list, the web interface, or the CLI; a pending admin that has never signed in does not count and can always be removed (`agenticml server accounts set-server-admin LOGIN off` fails with "cannot remove the last server admin").

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

# Suspend or resume a workspace (suspended workspaces answer 404 on the dashboard and 403 to sync clients)
agenticml server workspaces suspend SLUG [--database URL] [--data DIR]
agenticml server workspaces resume SLUG [--database URL] [--data DIR]

# Delete a workspace (requires typed confirmation of the slug; deletes files and objects)
agenticml server workspaces delete SLUG [--yes-delete SLUG] [--objects URL] [--database URL] [--data DIR]

# Find and retry leftovers of a deletion (workspace ids with files or S3 objects but no database row)
agenticml server workspaces orphans [--objects URL] [--json] [--database URL] [--data DIR]
agenticml server workspaces cleanup WORKSPACE_ID [--objects URL] [--database URL] [--data DIR]
agenticml server workspaces cleanup --all --yes-delete all [--objects URL] [--database URL] [--data DIR]

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

`set-plan SLUG custom` merges the flags you pass onto the workspace's current limits inside one database transaction: a custom workspace keeps its other limits, and a free workspace starts from the free plan in `--limits FILE` (or `AGENTICML_SERVER_LIMITS`), or from unlimited when there is no limits file. `PATCH /api/admin/workspaces/<slug>` with a partial `limits` object merges the same way, so two administrators editing different limits at once do not lose each other's change. The dashboard's plan dialog always sends all four limits, so there the last save wins; switching a free workspace to custom in it starts from the free plan's numbers. `GET /api/admin/workspaces` returns the free plan's limits as `free_limits` (null without a limits file).

### Account management CLI

Manage global user accounts using `agenticml server accounts`:

```bash
# List all registered accounts
agenticml server accounts list [--json] [--database URL] [--data DIR]

# Add a global account
agenticml server accounts add LOGIN [--display-name NAME] [--server-admin] [--database URL] [--data DIR]

# Grant or revoke server admin privileges
agenticml server accounts set-server-admin LOGIN on|off [--database URL] [--data DIR]

# Erase an account (privacy request): sessions, keys, memberships, pending invites; redacts the
# server-level audit log. Refused while the account is a server admin or owns a workspace or runs.
agenticml server accounts delete LOGIN [--yes-delete LOGIN] [--json] [--database URL] [--data DIR]
```

`accounts delete` leaves workspace Activity logs alone (they stay with their workspace) and prints the Firebase uid to delete in the Firebase console. `workspaces cleanup --all` removes every id `orphans` lists, so never run it against a restored or drill database; `server verify` and `server status` list orphans as warnings.

### Workspace membership and user CLI

An account that belongs to no workspace (removed from its last one) can be added again over HTTP (**Settings > Members**) only by an admin of the workspace it last left, and never when it is a server admin. For any other workspace, and for accounts that never had a membership, an operator must run `agenticml server users add LOGIN --workspace SLUG`.

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

Invite and setup links on a self-hosted server carry their token in the query string (`/invite?token=...`, `/setup?token=...`), because the server reads it to choose the page. If such a server sits behind a proxy or load balancer that logs request URLs, those tokens (single use, 72 hours for invites) appear in its logs; exclude query strings from that log. With Firebase or OIDC sign-in on, `users invite` and the Members page print `/invite#token=...` instead: the fragment is never sent to the server or to any proxy in front of it.

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
- **Multiple workspaces on one machine**: `agenticml login` saves only one key per server hostname, so running login for a second workspace replaces the first. For multiple workspaces on the same host, keep credentials separate using environment variables in each workspace's shell: set `AGENTICML_HOST` (e.g. `https://app.agenticml.xyz`) and read `AGENTICML_API_KEY` without echo (`read -rsp 'API key: ' AGENTICML_API_KEY; export AGENTICML_API_KEY`). Sync state is keyed by host and workspace (`<host>/w/<slug>`), so a single local store can sync the same run to multiple workspaces on one host. Separate `AGENTICML_DIR` directories (or `agenticml sync RUN_ID --reset` with the second key) are only needed with older clients.

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

### Database connections per process

Each server process opens a bounded number of connections:

- **Transactions**: at most 24 at once (`TRANSACTION_CONNECTION_LIMIT`) without a budget. A request waits up to 10 seconds for a free slot, then gets HTTP `503` with `Retry-After: 5`. The pool keeps at most `min(16, transaction limit)` idle connections (16 when unset). The `/healthz` check never opens a transaction, so it is not held up by this wait.
- **Session locks**: at most 64 more without a budget, each on its own connection (48 for run and object locks, 16 for quota, lineage and import locks), plus one lifetime connection for the server lock. A request waiting for a lock slot waits up to 30 seconds before returning HTTP `503` with `Retry-After: 5`.
- **Ceiling without a budget**: 89 connections (24 transaction + 48 run/object + 16 quota/lineage + 1 server lock). The 8-concurrent-bodies limit does not bound lock holding, because locks are taken after the body is received. Real connection use follows load and is usually far below the ceiling.

RDS sets `max_connections` from the instance's memory after the OS reserve, so `db.t4g.micro` gives roughly 75 usable connections, not 112. Run `SHOW max_connections;` to see the real value. A process paused while it holds a lock is a known residual risk, covered in section 8 of [THREAT_MODEL.md](THREAT_MODEL.md).

#### Connection budget (`--db-connections`)

To bound connections on a shared or smaller database, set `--db-connections N` or the environment variable `AGENTICML_SERVER_DB_CONNECTIONS`.

- **Value**: a whole number from 8 to 1000. It sets the most Postgres connections one server process opens, including its one lifetime server-lock connection.
- **Defaults and precedence**: unset (the default) uses the limits above (89 connections). The CLI flag overrides the environment variable; a blank environment variable counts as unset. An invalid value stops the server with `AGENTICML_SERVER_DB_CONNECTIONS must be a whole number from 8 to 1000`. Only `agenticml server serve` accepts this option.
- **Backend support**: Postgres only. With SQLite, the setting is ignored and one INFO log line is emitted.
- **Budget split**: for a budget `B`, let `W = B - 1`:
  - Quota and lineage locks: `max(round(W × 0.18), min(4, W − 3))`. This pool keeps at least 4 connections when the budget allows, so two lineage uploads never block upload quota locks.
  - Transactions: `max(2, round(W × 0.27))`.
  - Run and object locks: the rest of `W`.
  - Server lock: 1 connection.
  - Example: a budget of 16 allocates 4 transaction connections, 7 run/object locks, 4 quota/lineage locks, and 1 server lock.
- **Exhaustion**: when the budget is exhausted, a request waits (up to 10 seconds for a transaction connection, up to 30 seconds for a lock) and then gets HTTP `503` with `Retry-After: 5`. Sync clients retry 503 automatically.
- **Idle pool**: keeps at most `min(16, transaction limit)` connections.

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

Example limits configuration file (also shipped as `docs/examples/limits.json`; `deploy/aws/limits.json` is the hosted one):

```json
{
  "default_plan": "free",
  "free_workspaces_per_account": 1,
  "plans": {
    "free": {
      "storage_bytes": 5000000000,
      "upload_bytes_per_day": 2000000000,
      "requests_per_minute": 600,
      "people": 5
    }
  }
}
```

The file must have exactly these keys: anything else makes the server refuse to start with `invalid limits file`. `free_workspaces_per_account` is how many free workspaces one account may own (`0` disables free workspaces).

### Limit enforcement

- **Storage limit**: Sum of committed run files and objects. Checked before writes. Uploads that would exceed the quota return HTTP `403 Forbidden` (`{"error": "storage limit reached: ..."}`). Reads, deletes, and notes remain accessible.
- **Daily upload quota**: Bytes uploaded within the current UTC day. Exceeding the quota returns HTTP 403 with the reset time.
- **Request rate limit**: Requests per minute per workspace across all server processes. Exceeding the rate returns HTTP `429 Too Many Requests` with a `Retry-After` header.
- **People quota**: Maximum members plus pending invites. Inviting additional members is blocked once the quota is reached.
- **Free workspaces per account** (`free_workspaces_per_account`): Maximum free-plan workspaces an individual account can own.

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

After a restore, `agenticml server verify` can list runs whose files are behind the database (`size_mismatch`, `file_hash`) when they were synced while the backup ran. The client that holds such a run repairs it with `agenticml sync RUN --reset`, then `agenticml sync RUN`.

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

## Usage history

Server admins see server-wide totals and their daily history on `/admin`, in the "Usage" section at the top of the page. It displays five tiles and three growth charts:

- **Tiles**:
  - **Accounts**: total accounts, with new accounts created in the last 7 days ("+N in 7 days").
  - **Active users**: 7-day count, with 1-day and 30-day counts.
  - **Workspaces**: active workspaces, with suspended and archived counts.
  - **Storage**: stored bytes, with uploads today.
  - **Runs**: total runs.
- **Growth charts**: Accounts, Active users (7 days), and Storage (GB). Charts appear once there are two daily snapshots.

### Definitions

- **Accounts**: all accounts. New accounts are accounts created in the last 1 or 7 days.
- **Active**: an account that used a dashboard session or an API key (for example a sync client) in the last 1, 7, or 30 days. The environment admin key (`AGENTICML_SERVER_KEY`) has no account and does not count. Each account's last activity time is stored (`accounts.last_active_at`) and moves at most once an hour.
- **Workspaces**: active, suspended (not archived), and archived. Custom workspaces are workspaces on the custom plan, not archived.
- **Stored bytes**: committed bytes of all workspaces, archived included.
- **Uploads today**: bytes uploaded since midnight UTC.
- **Runs**: all runs.

### Daily history

The server keeps one row of these totals per UTC day in the `stats_daily` table. A loop inside the server process refreshes today's row every hour (and once at startup), so a finished day holds its totals as of its last refresh; that day's upload bytes are those counted up to that refresh. Days the server was not running have no row. Several server processes on one Postgres database never write the same day twice.

The history holds totals only: no emails, logins, or workspace names.

### Usage CLI

Inspect current totals and daily history using `agenticml server stats`:

```bash
# Print totals, five largest workspaces by storage, and 30-day history
agenticml server stats [--data DIR] [--database URL] [--json] [--days N]

# Save today's row now and print it
agenticml server stats snapshot [--data DIR] [--database URL] [--json]
```

- `stats` prints the current totals, the five largest workspaces by stored bytes (slug, name, size), and the daily history for the last `N` days (default 30, from 1 to 3650).
- `stats snapshot` saves today's row now and prints it.
- Exit codes: `0` on success, `1` on database error, and `2` on usage error (for example no server database at `--data`, or `--days` out of range).

### Admin API

The endpoint `GET /api/admin/stats` (server admins only; others get 404 like every admin route) returns `{"totals", "top_workspaces", "history" (90 days), "refresh_seconds": 3600}`.

### Privacy

Usage history works the same self-hosted (SQLite) and hosted (Postgres). Totals stay in the server's database, and nothing is sent anywhere.

## Capacity metrics

Metrics are off by default (`--metrics off`); with `off`, nothing is sent anywhere. With `--metrics cloudwatch` (or `AGENTICML_SERVER_METRICS=cloudwatch`), the server sends 12 metrics to Amazon CloudWatch once a minute with `PutMetricData`, in the namespace `AgenticML/Server` and with no dimensions (no per-task, workspace, user, or route values; no paths, slugs, logins, or keys).

### Measured metrics

Published once a minute:
- `Requests`: requests finished in the minute (health checks on `/healthz` are not counted).
- `Errors4xx`: finished requests with HTTP status 400–499.
- `Errors5xx`: finished requests with HTTP status 500–599.
- `Rejected`: connections refused at the server's 64-connection cap.
- `Latency`: server time per request in milliseconds, excluding time spent receiving request bodies and sending responses (published as values and counts so CloudWatch can show p50/p95/p99).
- `InFlightPeak`: most requests in progress at once in the minute.
- `DbConnectionsPeak`: most database connections the process held at once in the minute.

Published every 15 minutes (usage totals from [Usage history](#usage-history)):
- `Accounts`: total accounts.
- `ActiveUsers7d`: accounts active in the last 7 days.
- `WorkspacesActive`: active workspaces.
- `StoredBytes`: total committed bytes stored across all workspaces.
- `UploadBytesToday`: bytes uploaded since midnight UTC.

### Multi-process behaviour

Several server processes publish to the same metrics: counts add up and latency values merge. For the usage totals, each process sends the same values; use CloudWatch statistic `Maximum` to view them.

### Requirements

Requirements for `cloudwatch`:
1. The `[cloudwatch]` extra:
   ```bash
   pip install 'agenticml-lite[cloudwatch]'
   ```
   This installs `boto3`. The `[hosted]` extra includes it.
2. An AWS region in `AWS_REGION` or `AWS_DEFAULT_REGION`.
3. AWS credentials allowed `cloudwatch:PutMetricData`. This permission can be limited with the condition `cloudwatch:namespace` = `AgenticML/Server`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "AgenticML/Server"
        }
      }
    }
  ]
}
```

Without the `[cloudwatch]` extra or a region, the server refuses to start with a message saying so.

The CLI flag overrides the environment variable; a blank environment variable counts as unset. Any value other than `off` or `cloudwatch` causes the server to refuse to start with `AGENTICML_SERVER_METRICS must be off or cloudwatch`. Only `agenticml server serve` has this option (not `server up`).

### Unreachable CloudWatch

If CloudWatch is unreachable, the server logs a warning (at most every 10 minutes for the same message), drops that minute's numbers, and keeps serving.

### Cost

Custom metrics on AWS cost about $0.30 each per month after the first 10 free in an account, so about $1–4 a month. The API calls (one a minute per process) fit in the CloudWatch free tier.

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

Sync clients on version 2.2 continue to sync to a 3.0 server without modification. Clients must be 2.2 or newer: older ones are refused with HTTP 426, and 2.0 and 2.1 clients misreport their own runs as foreign if you lower `--min-client-version`.

## Configuration reference

### CLI options (`agenticml server serve`)

| Flag | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--data DIR` | `AGENTICML_SERVER_DATA` | `~/.agenticml-server` | Path to server data directory. |
| `--database URL` | `AGENTICML_SERVER_DATABASE_URL` | `sqlite:///<data>/server/agenticml.db` | Database connection URL (`sqlite:///path` or `postgresql://...`). |
| `--db-connections N` | `AGENTICML_SERVER_DB_CONNECTIONS` | unset (`89`) | Most Postgres connections one server process opens (8 to 1000). Flag wins over env; blank counts as unset. Ignored with SQLite. |
| `--objects URL` | `AGENTICML_SERVER_OBJECTS` | `file://` | Object storage backend (`file://` or `s3://bucket/prefix`). |
| `--host HOST` | | `127.0.0.1` | Network interface to bind. |
| `--port PORT` | | `8080` | TCP port to listen on. |
| `--public-url URL` | `AGENTICML_SERVER_PUBLIC_URL` | unset | Canonical public URL (e.g. `https://aml.example.com`). Required behind reverse proxies for Host/Origin validation and cookies. |
| `--trusted-proxy CIDR` | `AGENTICML_SERVER_TRUSTED_PROXIES` | unset | Trust proxy CIDR for `X-Forwarded-For` and identity headers (repeatable; comma-separated in env). Refuses `0.0.0.0/0` and `::/0`. |
| `--identity-header PRESET` | `AGENTICML_SERVER_IDENTITY_HEADER` | unset | Honour proxy identity header (`alb`, `tailscale`, or `oauth2-proxy`). Requires `--trusted-proxy`. |
| `--firebase-project ID` | `AGENTICML_SERVER_FIREBASE_PROJECT` | unset | Firebase project ID for token verification. |
| `--firebase-api-key KEY` | `AGENTICML_SERVER_FIREBASE_API_KEY` | unset | Firebase web API key for dashboard sign-in. |
| `--firebase-auth-domain HOST` | `AGENTICML_SERVER_FIREBASE_AUTH_DOMAIN` | unset | Firebase auth domain (defaults to `<project>.firebaseapp.com`). |
| `--oidc-issuer URL` | `AGENTICML_SERVER_OIDC_ISSUER` | unset | Sign in with this OpenID Connect issuer (Cognito, Okta, Entra ID, etc.); needs `--public-url` and the `[oidc]` extra. |
| `--oidc-client-id ID` | `AGENTICML_SERVER_OIDC_CLIENT_ID` | unset | OIDC public client ID; redirect URI to register is `<public-url>/auth/oidc/callback`. |
| `--oidc-logout-url URL` | `AGENTICML_SERVER_OIDC_LOGOUT_URL` | unset | Provider sign-out URL; sign-out redirects there with `client_id` and `logout_uri=<public-url>/login`. Optional. |
| `--oidc-label TEXT` | `AGENTICML_SERVER_OIDC_LABEL` | `Sign in` | Sign-in button text on the login page. |
| `--signup-email-domains LIST` | `AGENTICML_SERVER_SIGNUP_EMAIL_DOMAINS` | unset | Comma-separated email domains (exact match, no subdomains) that open sign-up serves; others get no account. |
| `--limits FILE` | `AGENTICML_SERVER_LIMITS` | unset | Path to JSON file defining workspace usage limits and plans. |
| `--metrics MODE` | `AGENTICML_SERVER_METRICS` | `off` | Capacity metrics mode: `off` or `cloudwatch`. Flag wins over env; blank counts as unset. Any other value refuses to start. Only `server serve` has it. |
| `--admin-emails LIST` | `AGENTICML_SERVER_ADMIN_EMAILS` | unset | Comma-separated emails granted server admin on verified Firebase or OIDC sign-in. Ignored without Firebase or OIDC. |
| `--legal-base-url URL` | `AGENTICML_SERVER_LEGAL_BASE_URL` | unset | `https://` base URL of your Terms, Acceptable use and Privacy pages (`terms/`, `acceptable-use/`, `privacy/` under it). With Firebase on, the sign-in page (`/login` and `/invite`) then shows "By continuing you agree to the Terms and the Acceptable use policy. See the Privacy policy." with those links. A non-`https://` value stops the server at startup. Ignored without Firebase. Not set means no links. |
| `--blog-url URL` | `AGENTICML_SERVER_BLOG_URL` | unset | `https://` URL of your blog. With Firebase on, the sign-in page (`/login` and `/invite`) then shows a **Blog** link under the form and, beside the brand panel on wide screens, a "From the blog" card with the newest post (title, date and a link to it) and an "All posts" link. Your visitor's browser reads the blog's RSS feed at `rss.xml` under that URL, so the blog host must answer with an `Access-Control-Allow-Origin` header (GitHub Pages does). The sign-in page's `connect-src` then also allows that one origin (`https://host[:port]`); no other page changes. The card shows only a post whose link is on the blog's own origin, and stays hidden if the feed is slow, missing or not valid (the Blog link stays). A non-`https://` value stops the server at startup. Ignored without Firebase. Not set means no link and no card. |
| `--contact-email ADDRESS\|off` | `AGENTICML_SERVER_CONTACT_EMAIL` | unset | Where the dashboard's contact button writes. Unset or empty shows a **Feedback** button that writes to the agenticml-lite team (`contact@agenticml.xyz`). An address (a plain address such as `name@example.com`: the part before `@` uses letters, digits and `. _ + -`; the domain uses letters, digits, `.` and `-` and needs at least one dot) shows a **Contact us** button that writes to that address, adds a Contact us link to the workspace picker and, with Firebase on, a "Questions? Contact us" link to the sign-in page. `off` (any case) shows no button and no links. Flag wins over the environment. Any other value stops the server at startup. Only `server serve` has it; `server up` ignores it and always shows the Feedback button. |
| `--open-signup` | `AGENTICML_SERVER_OPEN_SIGNUP` | off | Allow verified Firebase or OIDC sign-in to create an account and personal workspace. A verified account with no workspace gets one at sign-in (once; an admin-deleted sign-up workspace is not recreated). |
| `--min-client-version X.Y` | `AGENTICML_SERVER_MIN_CLIENT_VERSION` | `2.2` | Reject `agenticml-lite` clients older than `X.Y` on `/v2` requests with HTTP 426. Values below `2.2` are unsupported: 2.0 and 2.1 clients misreport their own runs as foreign. |

Additional environment variables:
- `AGENTICML_SERVER_KEY`: Static admin API key named `env` kept in memory. Bound to the `default` workspace.
- `AGENTICML_SERVER_S3_ENDPOINT_URL`: Custom S3 endpoint URL for MinIO, LocalStack, or VPC endpoints.
- `AGENTICML_SERVER_S3_KMS_KEY_ID`: AWS KMS Key ID or ARN for S3 server-side encryption (`aws:kms`).
- `AGENTICML_SERVER_DATABASE_PASSWORD_SECRET`: Optional, Postgres only. The ARN or name of an AWS Secrets Manager secret whose JSON value has a `password` key (the format of an RDS-managed master password secret). When Postgres refuses a new connection with `password authentication failed`, for example after RDS rotated the password, the server reads the secret, retries once with the password in it, and keeps using it; no restart is needed. It reads the secret at most once every 30 seconds and logs `database password refreshed from the secret` on success. The task or instance role needs `secretsmanager:GetSecretValue` on the secret, and `boto3` (included in `[hosted]`) must be installed; the region comes from `AWS_REGION` or `AWS_DEFAULT_REGION`. Unset (the default): the server never contacts Secrets Manager and uses the password in the database URL or `PGPASSWORD`.
- `AGENTICML_REMOTE_STALE_SECONDS`: Inactivity threshold before an active run is marked `stale` (default `600`).

### Client options

Options on `agenticml login`, `agenticml sync`, and `agenticml whoami`:

| Flag | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--ca-bundle PATH` | `AGENTICML_CA_BUNDLE` | unset | Path to a custom CA bundle file for TLS certificate verification. |

Trust resolution order (most specific first):
1. `--ca-bundle PATH` CLI option
2. CA bundle saved for the exact host by `agenticml login --ca-bundle`
3. `AGENTICML_CA_BUNDLE` environment variable
4. System default CA bundle (honours `REQUESTS_CA_BUNDLE`)

`login` ignores the bundle saved for the host: it uses `--ca-bundle`, then `AGENTICML_CA_BUNDLE`, then the system roots. A successful `login` replaces the saved entry for the host, so pass `--ca-bundle` on every `login`. The list above applies to `sync` and `whoami`.

A custom CA bundle replaces default system root certificates. Save the bundle per host via `login --ca-bundle` when syncing to both private and public HTTPS endpoints. Certificate verification can never be turned off. CA bundle files and parent directories must be owned by the user or root and must not be group- or world-writable (`mode & 0o022 == 0`).

## Network filesystems (EFS)

Every append, atomic write, and object put on the server does `os.fsync` plus a directory fsync. That is the main extra cost when `<data>` is on EFS or another network filesystem. `server/tmp` and `store` must already be on the same filesystem (the server refuses to start otherwise). Run a single server process per data directory when using SQLite. NFSv4 emulates `flock` on `server.lock`; AWS documents NFSv4.1 (`nfsvers=4.1`) for EFS, and that is the recommended mount.

The first two tables below were measured on local NVMe, then again with a modelled fsync delay injected **only in the benchmark's server process**. They are modelled numbers, not measurements on real EFS. 5 ms and 20 ms are assumptions for typical EFS commit latency on small writes. The real-EFS table (measured on the hosted service's filesystem settings, Nov 16, 2026) comes after them. Recommendations below are from **1 and 8 clients only**; larger N is unmeasured.

The first published tables held a process-wide `objects_lock` across each object PUT's temp-file `fsync`, rename, and directory fsync. After those runs, object writes fsync the private temp file with no lock held, then take one lock per `objects/<hh>` prefix only for the unsafe-destination check, the already-present check, `mkdir`, `os.replace`, and the directory fsync. The 20 ms 1-client and 8-client configs and the local 8-client config were re-run after that change. Before/after rows below use the numbers already published here as **before**. The local 1-client and both 5 ms rows were not re-run (labelled pre-change). Every before/after row is a single run with unquantified noise. The before and after runs were not on guaranteed identical machine state.

**Machine (local and modelled tables):** Intel Core i7-14700K (28 threads), 32 GiB RAM, NVMe SSD `CT1000P3PSSD8` (931.5 G, ROTA=0), ext4 on `/dev/nvme0n1p3`, Python 3.10.12, git 2.34.1.

**Workload** (`scripts/bench_server.py` defaults): each client writes 4 live runs × 4000 steps × 8 metrics and 50 distinct 65536-byte `copy=True` artifacts (split across that client's runs), then 3 passes of sync (one full sync, then two incremental append+sync passes, back to back). Clients start together. Uploaded payload on the first pass is about 4.32 MB per client (about 1.04 MB of scalars plus 3.28 MB of objects).

### Real EFS (Fargate, us-east-2)

Measured on Nov 16, 2026 on a restored copy of the hosted filesystem (see the restore drill in `deploy/aws/README.md`), with the same settings as production: General Purpose, Elastic throughput, encrypted, mounted over TLS through an access point.

**Environment:** Fargate ARM64, 4 vCPU and 16 GB visible (task size 4096/8192), Python 3.12.12, us-east-2b. NFS 4.1 with `rsize`/`wsize` 1 MiB, `hard`, `noresvport`, `timeo=600`, `retrans=2`. `scripts/bench_server.py` from commit `0bd558d`, default workload above. The task ran for about 3 minutes. Each configuration was run 3 times. Cells show the median with the range of the 3 runs.

**What this measures:** run files, appends, and object writes on real EFS, with the SQLite database on the task's local disk. It is not SQLite on EFS (3.0 refuses that) and it is not the hosted Postgres path: in hosted mode the database is Postgres, not on EFS. So the numbers cover the EFS file I/O part of the hosted server, not RDS or S3 latency.

| Clients | Initial wall | Throughput | Incremental p50 / p95 | Commit p50 / p95 | Object PUT p50 / p95 | Objects/s | Server CPU |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 4.14 s (3.89-4.23) | 1.04 MB/s (1.02-1.11) | 1.51 s (1.33-1.57) / 1.77 s (1.59-1.80) | 11.3 ms (10.1-11.3) / 13.2 ms (11.7-13.3) | 35.0 ms (33.9-35.9) / 38.8 ms (37.3-39.9) | 12.1 (11.8-12.9) | 1.20 s (1.20-1.26) |
| 8 | 10.07 s (9.87-10.19) | 3.43 MB/s (3.39-3.50) | 3.93 s (3.91-3.96) / 4.24 s (4.23-4.28) | 52.7 ms (50.8-53.6) / 86.8 ms (85.4-91.3) | 75.6 ms (74.5-75.7) / 97.2 ms (94.1-103.1) | 39.7 (39.3-40.5) | 17.5 s (17.4-17.7) |

The runs agree closely, so the spread is small. Throughput is the aggregate over all clients; objects/s counts 50 objects per client.

**Against the models:** the machines differ (Fargate ARM64 versus the local i7), so this is a rough comparison. Commit p50 on real EFS is about 10-11 ms at 1 client and about 51-54 ms at 8. At 1 client that is below both models (26 ms at 5 ms, 55 ms at 20 ms). At 8 clients it sits between them (37 ms and 64 ms). Object PUT p50 is about 34-36 ms at 1 client, between the 5 ms model (22 ms) and the 20 ms model (51 ms). At 8 clients the PUT p50 of about 75 ms is above the 20 ms model after the object-lock change (49 ms) and below the pre-change 5 ms row (124 ms). So the 20 ms assumption is a safe upper bound for commits, and object PUTs under 8 clients are somewhat slower than the model.

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

- **`sync --follow --interval`:** keep the default 30 s (kept after the real-EFS run). The slowest incremental pass was 4.28 s on real EFS (8 clients, p95 of the worst repetition) and 3.239 s on the 20 ms model, for 4 live runs. Both are far below 30 s. Passes ran back to back; this was not a 30 s follow loop.
- **Heartbeat (60 s) vs stale (600 s):** the 60 s heartbeat was not exercised. Passes were back to back. The slowest commit p95 was 125.78 ms (8-client, 20 ms modelled, after the change) and 91.3 ms on real EFS (8 clients). Kept: at this N a 60 s heartbeat would stay well under the 600 s stale threshold; that is inferred from commit latency, not measured in a follow loop.
- **`APPEND_CHUNK` (8 MiB):** leave it (kept; the real-EFS numbers do not contradict this). Each series file in this workload is 32 kB per pass (`4000 × 8` bytes), far below 8 MiB. The cost is fsync round-trips per request, not chunk size. Shrinking the chunk would add requests and fsyncs.
- **EFS mount options:** `nfsvers=4.1`, `hard`, `noresvport`. NFSv4 emulates `flock`; 4.1 is the version to use. The real-EFS run used these options with General Purpose and Elastic throughput. Other mount options and other throughput modes were not compared. A filesystem restored from a backup comes back in Bursting mode; switch it to Elastic if you want the same behaviour.

### Reproduce on real EFS

To repeat the real-EFS run, point `--data` at the EFS mount and omit `--fsync-delay-ms`:

```bash
python scripts/bench_server.py --data /mnt/efs/agenticml --clients 1
python scripts/bench_server.py --data /mnt/efs/agenticml --clients 8
```

The script starts a server on `127.0.0.1:0`, creates one sync key per client, and deletes `bench-*` runs and keys from `--data` on the way out. On loopback it probe-binds `127.0.0.1`–`127.0.0.N` and gives each client its own source address so the failed-authentication throttle (keyed by client IP) does not 429 concurrent processes. If those addresses are missing (typical on macOS), it prints a warning, falls back to `127.0.0.1` for every client, and enables client retries: more than about 6 simultaneous requests from one address may get a short 429.

Machines behind the same NAT or HTTP proxy share one throttle bucket, even if they are distinct hosts. A burst of simultaneous starts can see a brief 429; the client retries 429 up to `max_retries`, sleeping `Retry-After` when present (capped at 60 s). With `--follow`, a single request can now wait up to about 3 × 60 s on repeated 429s before follow's own backoff starts. This benchmark uses `max_retries=0` when it has distinct `127.0.0.x` addresses.

`--quick` is a smoke size (under a minute). The local and modelled tables stay as published; they are labelled as modelled.

## Troubleshooting

### "another agenticml server is using <data>"
Another server process has the SQLite data directory open. Check `systemctl status agenticml-server` or look for running `agenticml server serve` processes on the host. Stop the conflicting process. If multiple processes are required, use PostgreSQL via `--database`.

### HTTP 403 ("forbidden host") on dashboard
The dashboard accepts requests only from loopback addresses unless `--public-url` is configured. The incoming `Host` header must carry the public URL's host name (or a loopback name); the port is not compared here. When operating behind a reverse proxy, set `AGENTICML_SERVER_PUBLIC_URL=https://<your-hostname>` in the server environment (or `/etc/agenticml-server.env` under systemd) and configure the proxy to forward the `Host` header unchanged (`proxy_set_header Host $http_host;` in nginx).

### HTTP 403 ("cross-site request refused") on dashboard
The dashboard rejected a state-changing POST request because the request's `Origin` does not match `AGENTICML_SERVER_PUBLIC_URL`. The browser's scheme, host and port must all equal `AGENTICML_SERVER_PUBLIC_URL`. Ensure the browser visits the exact scheme, host, and port configured there (include the port in the public URL if it is not 443).

### TLS certificate check failed
The client prints:
```
TLS certificate check failed (untrusted CA, wrong host name, expired, or a wrong clock); pass --ca-bundle PATH or set AGENTICML_CA_BUNDLE, and use the name in the certificate
```
The TLS certificate presented by the server or proxy could not be validated. Common causes:
- The certificate was issued by an internal CA not trusted by the client. Pass `--ca-bundle PATH` or set `AGENTICML_CA_BUNDLE`.
- The hostname in `--host` does not match the Subject Alternative Names (SAN) in the certificate. Connect using the exact DNS name in the certificate.
- The certificate has expired, or the client or server system clock is wrong. Check clocks using `timedatectl` or `chronyc tracking`.

Diagnose the TLS connection with `openssl`:
```bash
openssl s_client -connect agenticml.lab.internal:443 -CAfile lab-ca.pem
```

A certificate failure, or an `https://` URL that points at a port that does not speak TLS, is not retried by one-shot commands (`sync`, `whoami`, `login`). Continuous follow mode (`sync --follow`) keeps retrying with backoff and shows the hint in its status line:
```
server unreachable (TLS certificate check failed (untrusted CA, wrong host name, expired, or a wrong clock); pass --ca-bundle PATH or set AGENTICML_CA_BUNDLE, and use the name in the certificate); retrying in 30s
```

### TLS handshake failed
The client prints:
```
TLS handshake failed (the server closed the connection, or this is not an https port)
```
The client could not establish a TLS connection. Other handshake failures (for example the peer closing the connection) are retried a few times before this hint appears. Check whether the server closed the connection abruptly, or whether an `https://` URL was mistakenly pointed at a plain HTTP port (e.g. connecting directly to backend port 8080 over HTTPS instead of port 443 on the TLS proxy).

### "server redirected to another URL"
The client prints:
```
server redirected to another URL; use the final https:// URL as --host (refusing to send the API key on a redirect)
```
The server or reverse proxy returned an HTTP 3xx redirect (such as an HTTP-to-HTTPS redirect or adding a trailing slash). The client refuses to follow redirects to prevent exposing API keys. Update `--host` to the final canonical `https://` URL.

### CA bundle file and permission errors
The client validates custom CA bundles before use:
- `CA bundle not found or unreadable: <path>`: The file does not exist, is a directory, or lacks read permissions.
- `CA bundle is not owned by you or root: <path>`
- `CA bundle directory is not owned by you or root: <dir>`
- `CA bundle is writable by others: <path>; chmod go-w`: The bundle file has group- or world-writable permissions.
- `CA bundle directory is writable by others: <dir>; chmod go-w <dir>`: The directory containing the bundle is group- or world-writable.
- ``CA bundle path in the credentials file is not absolute: <path>; run `agenticml login --ca-bundle PATH` again`` (the fix is to run login again with an absolute path)

The bundle file and its directory must be owned by the user or root and must not be writable by group or others. Fix permissions:
```bash
chmod go-w <path>
chmod go-w <dir>
```

### Proxy errors: HTTP 411, 413, or 408
- **HTTP 411 Length Required**: The reverse proxy re-chunked the body and omitted `Content-Length`. The server requires `Content-Length` on upload requests. In nginx, configure `proxy_http_version 1.1;` and `proxy_request_buffering off;`.
- **HTTP 413 Payload Too Large / Request Entity Too Large**: The reverse proxy's body size limit is below 4 GiB. In nginx, set `client_max_body_size 4g;`.
- **HTTP 408 Request Timeout**: The server received the body too slowly. It allows about 30 s plus 2 s per MiB, so an upload slower than about 512 KiB/s fails. It also happens when the connection ended before the whole body arrived. Keep `proxy_request_buffering off;` and the 3 hour `proxy_read_timeout 3h;` and `proxy_send_timeout 3h;`, which prevent proxy-side 504 errors on long uploads.

### HTTP 502 under SELinux
Under SELinux, the reverse proxy is blocked from connecting to loopback network ports by default. Enable network connections for the proxy:
```bash
sudo setsebool -P httpd_can_network_connect 1
```
Ensure certificate files are stored in `/etc/pki` or `/etc/ssl` and restore proper file labels:
```bash
sudo restorecon -Rv /etc/ssl /etc/pki
```

### Browser certificate warnings
Web browsers display certificate warnings when opening the dashboard if the internal CA root is not trusted. Install the lab CA in the browser or OS store on every client workstation.

### Client version check skipped
A proxy that strips the User-Agent header makes the server skip its client-version check (HTTP 426). Ensure the reverse proxy forwards `User-Agent` if client version enforcement is desired.

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

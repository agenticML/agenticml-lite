---
title: Authentication and keys
description: Manage workspaces, roles, API keys, and dashboard access.
sidebar:
  order: 3
---

All requests to the server (except `GET /healthz`) require authentication. Training clients authenticate via bearer API keys over protocol v2, while browser users authenticate via sessions.

## Workspaces and roles

Every experiment run, API key, and team membership is scoped to a **workspace**.

Within each workspace, members hold one of four roles:

| Role | Permissions |
| --- | --- |
| `owner` | Workspace creator. Full admin permissions. Cannot be removed or demoted. Can delete workspace or transfer ownership. |
| `admin` | Full workspace administration: manage members, invite users, issue/revoke any member's keys, edit notes, sync runs. |
| `member` | Sync runs (`/v2`), edit run notes, and create/revoke own sync keys. |
| `viewer` | Read-only dashboard access. Cannot sync (sync keys can only call `GET /v2/whoami`). |

Server administrators hold a global `server_admin` flag on their account. They manage workspaces and global accounts, but do not have access to customer runs unless explicitly added as workspace members.

## Workspace-bound API keys

Every API key is bound to:
1. Exactly one **workspace**.
2. Exactly one **user account**.

When syncing over protocol v2, the server determines the target workspace directly from the bearer key. Clients do not need to specify a workspace slug in sync URLs.

### Key scopes

| Scope | Permissions | Target use |
| --- | --- | --- |
| `sync` | Access `/v2` endpoints (push metrics, upload objects, commit runs). Cannot log in to the web dashboard. | Training nodes, compute clusters, and automation scripts. |
| `admin` | Full `/v2` access and passwordless web dashboard login (self-hosted only). | Human operators. |

## First admin account and setup

When starting a server with an empty data directory, the server logs a one-time setup URL:

```
No admin key yet. Open <url>/setup?token=<token> to create one, or run: agenticml server keys create --name admin --scope admin
```

Open `/setup?token=<token>` in a browser to create the initial admin key and sign in to the default workspace.

When Firebase sign-in is on, `/setup` returns 404. Use `--admin-emails` (see [Installation and deployment](/agenticml-lite/server/install/#configuration-options)) or the CLI instead.

Alternatively, create an account and invite link directly using the CLI:

```bash
agenticml server users add admin --role admin --workspace default
```

### Environment admin key

You can define an administrator key using an environment variable before starting the server:

```bash
export AGENTICML_SERVER_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
```

The environment key is named `env`, held in memory only, and bound to the `default` workspace.

## Managing keys

### In the dashboard

1. In the sidebar, select **Keys**.
2. Click **New key**, enter a name (e.g. `gpu-worker-1`), and select the scope (`sync` is default).
3. Copy the key value immediately; keys are hashed with SHA-256 and cannot be displayed again.
4. To revoke a key, click **Revoke**. Revocation takes effect immediately across all server processes.

### With the CLI

Manage keys across workspaces using `agenticml server keys`:

```bash
# Create a sync key in a workspace
agenticml server keys create --name worker-1 --workspace default [--scope sync|admin] [--user LOGIN]

# List keys in a workspace
agenticml server keys list --workspace default [--json]

# Revoke a key immediately
agenticml server keys revoke worker-1 --workspace default
```

## Managing workspace members and accounts

### Workspace members CLI

```bash
# Add a member and generate an invite link
agenticml server users add alice@example.com --workspace team-a --role member

# Generate a fresh 72-hour invite link
agenticml server users invite alice@example.com --workspace team-a

# Set a user password directly
agenticml server users set-password alice@example.com --workspace team-a

# Change a member's role
agenticml server users set-role alice@example.com admin --workspace team-a

# Remove a member (revokes their keys in this workspace)
agenticml server users remove alice@example.com --workspace team-a
```

### Global accounts CLI

Server administrators manage global accounts using `agenticml server accounts`:

```bash
# List all accounts
agenticml server accounts list

# Add a global account
agenticml server accounts add user@example.com --display-name "User Name"

# Grant server admin privileges
agenticml server accounts set-server-admin user@example.com on
```

## Dashboard sign-in and sessions

The server supports several authentication methods:
- **Password**: Authenticate with username and password set via invite links or CLI.
- **Trusted identity proxy**: When operating behind an authenticating proxy (ALB OIDC, Tailscale, oauth2-proxy), headers assert identity directly via `--trusted-proxy` and `--identity-header`.
- **Firebase Authentication**: On the hosted service (or self-hosted with `--firebase-project`), users sign in with Google or email/password verified via Firebase.
- **Admin key**: Direct sign-in using an `admin`-scope key.

Sessions are persisted in the database (`sessions` table) with a 12-hour idle timeout and 7-day maximum lifetime. Sessions survive server restarts and work across multiple server processes behind a load balancer.

## Next

Continue to [Syncing runs](/agenticml-lite/server/sync/) to connect training machines.

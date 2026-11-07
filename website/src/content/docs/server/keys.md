---
title: Authentication and keys
description: Manage API keys, scopes, bootstrap tokens, and dashboard login.
sidebar:
  order: 3
---

All requests to the server (except `GET /healthz`) require authentication using bearer API keys.

## Key scopes

Keys are divided into two scopes:

| Scope | Permissions | Target use |
| --- | --- | --- |
| `sync` | Access `/v2` endpoints (push metrics, upload objects, commit runs). Cannot log in to the web dashboard. | Training machines and automated jobs. |
| `admin` | Full `/v2` access, dashboard login, and key management (create, list, revoke). | Human operators. |

## First admin key (Bootstrap)

A new server starts with an empty key store. If `AGENTICML_SERVER_KEY` is not set, the server logs a one-time bootstrap setup URL:

```
No admin key yet. Open <public-url or http://host:port>/setup?token=<token> to create one, or run: agenticml server keys create --name admin --scope admin
```

To find the URL:
- Terminal: Check the standard output from `agenticml server serve`.
- systemd: Run `sudo journalctl -u agenticml-server`.

Open `/setup?token=<token>` in your browser to name your admin key, receive the key token, and automatically sign in. The setup token is single-use and kept only in memory. After an admin key is created, `/setup` returns 404.

### Environment admin key

You can define an administrator key using an environment variable before starting the server:

```bash
export AGENTICML_SERVER_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
```

Under systemd, configure `AGENTICML_SERVER_KEY` in the unit's environment file (e.g. `/etc/agenticml-server.env`, owned by root with mode `0600`).

The environment key:
- Receives the name `env` on the server.
- Lives in memory and is never written to `keys.json`.
- Cannot be revoked from the web dashboard. To revoke it, remove the environment variable and restart the server.
- Must contain between 32 and 256 allowed characters (`[A-Za-z0-9._~+/=-]`).

## Managing keys in the dashboard

Signed-in administrators can issue and revoke keys from the web dashboard:

1. In the sidebar, select **Keys**.
2. Click **New key**, enter a name (such as `cluster-box-1`), and select the scope (`sync` is default).
3. Copy the key value immediately. The server hashes keys with SHA-256 and cannot display them again. The dashboard provides a pre-filled `agenticml login` command.
4. The key table lists name, scope, prefix, creation date, creator, last used time, and runs owned.
5. Click **Revoke** to revoke access. Revocation takes effect immediately without a server restart. The last admin key cannot be revoked from the page (the error says to create another admin key first, or use the CLI). Revoking the key your own session uses signs you out.

## Managing keys with the CLI

The server CLI manages keys directly against `<data>/server/keys.json` while the server is running:

```bash
agenticml server keys create --name worker-1 --scope sync --data DIR
agenticml server keys list --data DIR
agenticml server keys revoke worker-1 --data DIR
```

Under systemd, run the commands as the service user with the same data directory:

```bash
sudo -u agenticml /opt/agenticml/bin/agenticml server keys create --name worker-1 --scope sync --data /var/lib/agenticml-server
sudo -u agenticml /opt/agenticml/bin/agenticml server keys list --data /var/lib/agenticml-server
sudo -u agenticml /opt/agenticml/bin/agenticml server keys revoke worker-1 --data /var/lib/agenticml-server
```

`keys create` outputs the key to stdout and reminders to stderr. The `--scope` flag defaults to `sync`.

## Dashboard login and sessions

Signing in to the server web dashboard requires an `admin` key:

- `POST /login` accepts `{"key": "aml_..."}`. `sync` keys are refused with status `403`.
- Successful login issues a session cookie with `HttpOnly`, `SameSite=Strict`, and `Path=/`. When `--public-url` is HTTPS, the cookie is named `__Host-aml_session` and includes `Secure`; otherwise it is named `aml_session`.
- Sessions are stored in memory with a 12-hour idle timeout and a 7-day maximum lifetime. Restarting the server or revoking the key invalidates all associated sessions.
- Failed logins are rate-limited per client IP address.
- CSRF protection enforces the `X-AgenticML-Request` header and checks that the `Origin` header matches `--public-url`.
- The dashboard rejects requests if the `Host` header does not match `--public-url` or a loopback address (`localhost`, `127.0.0.1`, `::1`).

## Next

Continue to [Syncing runs](/agenticml-lite/server/sync/) to connect training machines.

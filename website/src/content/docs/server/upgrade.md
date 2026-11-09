---
title: Upgrades and troubleshooting
description: Upgrade server versions, migrate from 2.x to 3.0, and resolve operational issues.
sidebar:
  order: 6
---

## Upgrading to 3.0

Back up the data directory first before upgrading.

Downgrades are not supported. Review the release notes before upgrading.

To upgrade a self-hosted server:

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

### Upgrading from 2.x to 3.0

When a 3.0 server starts against an existing 2.x data directory, it automatically converts the data in place on first start:

1. **Database initialization**: The server creates the SQLite database at `<data>/server/agenticml.db` and applies migrations.
2. **Default workspace**: The `default` workspace is created.
3. **Accounts and memberships**: Legacy users from `users.json` become global accounts and members of `default`. The earliest administrator is assigned as the workspace `owner`.
4. **Keys and audit log**: `keys.json` and `audit.jsonl` are imported into database tables.
5. **Runs and storage**: The server reads `remote.json` and `artifacts.json` into the database and moves runs into `<data>/workspaces/<workspace_id>/store/runs/`.
6. **File retirement**: Legacy files are renamed with a `.2x` extension (`users.json.2x`, `keys.json.2x`, `audit.jsonl.2x`).
7. **Refusal to convert twice**: If old 2.x files reappear in the directory after conversion, the server refuses to start with an error:
   ```
   found 2.x <path> after this server was converted on <date>; move it away (the converted copy is <path>.2x)
   ```
   Move the reappeared files away to allow the server to start.

Existing 2.2 sync clients continue to sync to a 3.0 server without any client changes.

## Troubleshooting

### "another agenticml server is using <data>"

Another server process has the SQLite data directory open. Check `systemctl status agenticml-server` or look for running `agenticml server serve` processes on the host. Stop the conflicting process. If you require multiple concurrent processes, switch to PostgreSQL using `--database`.

### HTTP 403 ("forbidden host") on dashboard

When accessed through a reverse proxy, the browser sends the proxy's hostname in the `Host` header. The dashboard rejects non-loopback hostnames unless `--public-url` is configured, returning HTTP 403 with `{"error": "forbidden host"}`. Set `AGENTICML_SERVER_PUBLIC_URL=https://<hostname>` in the server environment (or `/etc/agenticml-server.env` under systemd).

### HTTP 426 ("this server needs agenticml-lite >= X.Y")

The sync client is running an older `agenticml-lite` version below `--min-client-version` (default 2.2). Update the client package on the training machine:
```bash
pip install -U agenticml-lite
```

### `agenticml sync` exits with status 3 (`401 Unauthorized`)

When running `agenticml sync`, the client exits with status code 3 if the server returns 401 because the API key is missing, invalid, or revoked. Run `agenticml login --host https://...` on the client machine to supply a valid key, or check key status on the server with `agenticml server keys list --workspace <slug>`.

### HTTP 403 ("storage limit reached")

The workspace has reached its allocated storage quota. Additional uploads are rejected until an administrator raises the quota using `agenticml server workspaces set-plan <slug> custom --storage <size>` or deletes unneeded runs. Under `sync --follow`, the client automatically retries every 10 minutes and resumes once the limit is increased.

### Active runs display as "stale"

A run displays as `stale` if its status is `running` but no commit heartbeat has arrived within `AGENTICML_REMOTE_STALE_SECONDS` (default 600 seconds). Ensure the training machine runs `agenticml sync --follow`, which transmits heartbeats at least every 60 seconds while runs are active.

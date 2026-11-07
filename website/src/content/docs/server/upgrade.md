---
title: Upgrades and troubleshooting
description: Upgrade server versions and resolve operational issues.
sidebar:
  order: 6
---

## Upgrades

Back up the data directory first.

One server process per data directory; downgrades are not supported. Always review the release notes before upgrading.

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

Because the server acquires an exclusive lock on `<data>/server/server.lock`, only one server process may run against a data directory at a time.

## Troubleshooting

### "another agenticml server is using <data>"

The error means another server process has the same data directory open. Check `systemctl status agenticml-server` or look for running server processes on the host. Stop the other process and start again.

### HTTP 403 ("forbidden host") on dashboard

When accessed through a reverse proxy, the browser sends the proxy's hostname in the `Host` header. The dashboard rejects non-loopback hostnames unless `--public-url` is configured, returning HTTP 403 with `{"error": "forbidden host"}`. Set `AGENTICML_SERVER_PUBLIC_URL=https://<hostname>` in the server environment (or `/etc/agenticml-server.env` under systemd).

### `agenticml sync` exits with status 3 (`401 Unauthorized`)

When running `agenticml sync` (including `--follow` and `--watch`), the client exits with status code 3 if the server returns 401 because the API key is missing, invalid, or revoked. Run `agenticml login --host https://...` on the client machine to supply a valid key, or check key status on the server with `agenticml server keys list`.

### Active runs display as "stale"

A run displays as `stale` if its status is `running` but no commit has arrived within `AGENTICML_REMOTE_STALE_SECONDS` (default 600 seconds). Ensure the training machine runs `agenticml sync --follow`, which transmits heartbeats at least every 60 seconds while runs are active.

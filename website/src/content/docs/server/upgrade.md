---
title: Upgrades and troubleshooting
description: Upgrade server versions, verify image signatures, and resolve operational issues.
sidebar:
  order: 6
---

## Upgrades

To upgrade a self-hosted server:

1. Pull the target image tag:
   ```bash
   docker pull ghcr.io/agenticml/agenticml-server:<new-version>
   ```
2. Stop the running container cleanly:
   ```bash
   docker stop --time 30 agenticml-server
   ```
3. Start the container with the new image, targeting the existing data volume.
4. Verify server health:
   ```bash
   curl -s http://127.0.0.1:8080/healthz
   ```

Because the server acquires an exclusive lock on `<data>/server/server.lock`, the old container must be stopped before the new one is started. Downgrades are not supported. Always review the release notes before upgrading.

## Verifying image signatures

Server container images are signed keylessly with Cosign using GitHub Actions OpenID Connect (OIDC). Verify an image before deployment:

```bash
cosign verify \
  --certificate-identity-regexp '^https://github\.com/agenticML/agenticml-lite/\.github/workflows/image\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/agenticml/agenticml-server:2.0.0
```

Replace `2.0.0` with the specific tag or `@sha256:<digest>`.

## Troubleshooting

### "another agenticml server is using <data>"

The error means another server process (another container, or a process on the host) has the same data directory open. Stop it (`docker ps`, `docker stop`) and start again.

### HTTP 403 ("forbidden host") on dashboard

When accessed through a reverse proxy, the browser sends the proxy's hostname in the `Host` header. The dashboard rejects non-loopback hostnames unless `--public-url` is configured, returning HTTP 403 with `{"error": "forbidden host"}`. Set `AGENTICML_SERVER_PUBLIC_URL=https://<hostname>` in the container environment.

### `agenticml sync` exits with status 3 (`401 Unauthorized`)

When running `agenticml sync` (including `--follow` and `--watch`), the client exits with status code 3 if the server returns 401 because the API key is missing, invalid, or revoked. Run `agenticml login --host https://...` on the client machine to supply a valid key, or check key status on the server with `agenticml server keys list`.

### Active runs display as "stale"

A run displays as `stale` if its status is `running` but no commit has arrived within `AGENTICML_REMOTE_STALE_SECONDS` (default 600 seconds). Ensure the training machine runs `agenticml sync --follow`, which transmits heartbeats at least every 60 seconds while runs are active.

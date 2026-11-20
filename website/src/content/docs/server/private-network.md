---
title: Private networks
description: Run the AgenticML server and dashboard inside a private network without internet access.
sidebar:
  order: 2.5
---

Run the server and dashboard inside a private VPC or lab network without Tailscale or internet connectivity. All traffic originates inside the network and terminates at a local TLS reverse proxy with an internal certificate.

```
training machines ──HTTPS :443──▶ TLS proxy (internal cert) ──▶ agenticml server serve
lab users' browsers ──HTTPS :443──┘        Caddy or nginx            127.0.0.1:8080, SQLite on local disk
```

The server binds to loopback (`127.0.0.1:8080`) and speaks HTTP. TLS terminates at the reverse proxy.

## Architecture and prerequisites

The server needs:
- Python 3.10 or newer on the server and on every training machine.
- `git` on the server host to render lineage code diffs.
- Synchronized clocks (via NTP or chrony) on all machines. Clock skew causes TLS certificate validation failures.
- A private DNS name (or `/etc/hosts` entry) matching the server certificate's Subject Alternative Names (SAN).

For a complete step-by-step checklist, see the [private-network deployment guide](https://github.com/agenticML/agenticml-lite/blob/main/deploy/private-network/README.md).

## Installation

Install `agenticml-lite` on the server in a dedicated virtual environment. The server and every training machine need version 3.2 or newer.

```bash
sudo python3 -m venv /opt/agenticml
sudo /opt/agenticml/bin/python -m pip install 'agenticml-lite>=3.2'
```

For air-gapped hosts without internet access, pre-download wheels on a connected machine running the same OS and Python minor version:

```bash
python3 -m pip download -d wheelhouse 'agenticml-lite>=3.2'
```

Copy the `wheelhouse` directory to the target machine and install offline:

```bash
sudo /opt/agenticml/bin/python -m pip install --no-index --find-links wheelhouse 'agenticml-lite>=3.2'
```

Configuration templates for private networks are available in the repository under [`deploy/private-network/`](https://github.com/agenticML/agenticml-lite/tree/main/deploy/private-network).

## Server configuration

Create the service user and install the systemd service as described in [Installation and deployment](/agenticml-lite/server/install/#systemd-service).

Copy the environment template [`agenticml-server.env.example`](https://github.com/agenticML/agenticml-lite/blob/main/deploy/private-network/agenticml-server.env.example) to `/etc/agenticml-server.env` with restricted permissions:

```bash
sudo install -m 0600 -o root -g root agenticml-server.env.example /etc/agenticml-server.env
sudoedit /etc/agenticml-server.env
```

Configure the environment:
- Set `AGENTICML_SERVER_PUBLIC_URL` to the canonical HTTPS endpoint (e.g. `https://agenticml.lab.internal`). Include the port if using a non-standard port.
- Set `AGENTICML_SERVER_TRUSTED_PROXIES=127.0.0.1/32` (or the exact `/32` IP of the proxy). Never specify a wide CIDR block.
- Leave `AGENTICML_SERVER_KEY` unset so the one-time `/setup` registration link remains active.

Start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agenticml-server
```

## Reverse proxy setup

Place Caddy or nginx in front of `127.0.0.1:8080`. Ready-to-use configuration files are provided in the repository:
- [`Caddyfile`](https://github.com/agenticML/agenticml-lite/blob/main/deploy/private-network/Caddyfile): automated internal certificates using Caddy's internal CA (`tls internal`).
- [`nginx.conf`](https://github.com/agenticML/agenticml-lite/blob/main/deploy/private-network/nginx.conf): configuration for custom enterprise CA certificates.

### Proxy requirements

A reverse proxy in front of the server must adhere to these rules:

- **No redirects on sync paths**: The client rejects HTTP 3xx redirects to prevent sending API keys across hosts or schemes. Sync requests must reach the canonical HTTPS endpoint directly.
- **Pass Content-Length without re-chunking**: Streaming upload bodies must retain `Content-Length`. In nginx, configure `proxy_http_version 1.1;` and `proxy_request_buffering off;`. Re-chunked requests are rejected by the server with HTTP 411 Length Required.
- **Support 4 GiB request bodies**: Object uploads (large files and artifacts) can be up to 4 GiB in one request. Run files are capped at 16 MiB, appends at 64 MiB and lineage bundles at 256 MiB. In nginx, set `client_max_body_size 4g;`.
- **Long read and send timeouts**: Set proxy timeouts to at least 3 hours (`proxy_read_timeout 3h;` and `proxy_send_timeout 3h;`).
- **Pass the Host header unchanged**: The `Host` header must carry the public URL's host name (`proxy_set_header Host $http_host;` in nginx). The server does not compare its port. The port in `AGENTICML_SERVER_PUBLIC_URL` must be the one users type, because the browser's `Origin` (scheme, host and port) must equal it.
- **Strip identity headers**: Drop incoming `X-Forwarded-Email`, `X-Amzn-Oidc-Identity`, and `Tailscale-User-Login` headers from client requests.
- **Omit query strings from access logs**: Setup and invite tokens travel in query strings. Exclude query parameters from proxy access logs.

### SELinux configuration

On RHEL, Rocky Linux, or Fedora systems with SELinux enforcing, allow the reverse proxy to connect to local network ports:

```bash
sudo setsebool -P httpd_can_network_connect 1
```

Store certificates under `/etc/pki` or `/etc/ssl` and restore default security contexts:

```bash
sudo restorecon -Rv /etc/ssl /etc/pki
```

## Distributing the CA certificate

Clients must trust the certificate authority that signed the proxy certificate.

When using Caddy with `tls internal`, copy Caddy's root certificate (typically at `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`) to `/etc/ssl/certs/lab-ca.pem` on each training machine. If using an internal PKI, use your organization's root CA certificate.

1. Ensure the certificate file and directory are secure on the client:
   ```bash
   sudo chmod go-w /etc/ssl/certs/lab-ca.pem
   ```
2. Inspect and verify certificate details:
   ```bash
   openssl x509 -in /etc/ssl/certs/lab-ca.pem -noout -subject -enddate
   ```
3. Install the root CA into operating system or browser certificate stores on user workstations to avoid dashboard security warnings.
4. Back up Caddy's data directory (`/var/lib/caddy`). Rebuilding the proxy without the saved data generates a new root CA, requiring re-distribution to all machines.

## First administrator setup

Before opening port 443 to general lab users, locate the one-time administrator link in the server journal:

```bash
sudo journalctl -u agenticml-server | grep 'setup?token'
```

Open the link in a browser that trusts the CA to create the administrator account.

Alternatively, create an administrator using the CLI on the server host:

```bash
sudo -u agenticml /opt/agenticml/bin/agenticml server users add admin --role admin --public-url https://agenticml.lab.internal --data /var/lib/agenticml-server
```

Open the generated invite link to set the administrator password.

## Issuing sync keys

Issue sync keys from the dashboard Keys page or CLI:

```bash
sudo -u agenticml /opt/agenticml/bin/agenticml server keys create --name box-1 --scope sync --user NAME --data /var/lib/agenticml-server
```

The machine's runs are owned by that user; a key created on the dashboard Keys page belongs to the signed-in person.

## Training machine configuration

Install `agenticml-lite` on the training machine. Log in using the server's HTTPS URL and specify the custom CA bundle:

```bash
agenticml login --host https://agenticml.lab.internal --ca-bundle /etc/ssl/certs/lab-ca.pem
```

Enter the sync key at the prompt. The CLI validates the bundle file permissions and saves the bundle association for that exact host in `~/.config/agenticml/credentials.json`.

Verify connectivity:

```bash
agenticml whoami
```

Synchronize runs:

```bash
agenticml sync --all
```

For background synchronization on Linux, install the user systemd unit `agenticml-sync.service` as described in [Syncing runs](/agenticml-lite/server/sync/#background-service-with-systemd). Because `login --ca-bundle` records the bundle path per host, no additional service environment variables are needed.

### Trust order and CA bundles

When verifying TLS certificates, the client evaluates CA roots in this order:
1. `--ca-bundle PATH` command-line flag
2. CA bundle saved for the target host via `login --ca-bundle`
3. `AGENTICML_CA_BUNDLE` environment variable
4. System default CA roots (honours `REQUESTS_CA_BUNDLE`)

`login` ignores the bundle saved for the host: it uses `--ca-bundle`, then `AGENTICML_CA_BUNDLE`, then the system roots. A successful `login` replaces the saved entry for the host, so pass `--ca-bundle` on every `login`. The list above applies to `sync` and `whoami`.

A custom CA bundle replaces default system root certificates rather than appending to them. Machines that synchronize to both internal lab servers and public HTTPS endpoints should save bundles per host using `agenticml login --ca-bundle` rather than exporting `AGENTICML_CA_BUNDLE` globally.

Certificate verification can never be turned off. Custom CA bundle files and their containing directories must be owned by the current user or root, and cannot be writable by group or other users (`mode & 0o022 == 0`).

### Insecure HTTP consent

Connecting to a non-local `http://` host requires `agenticml login --insecure`. When specified, insecure consent is recorded for that exact host and port in local credentials. Subsequent commands (`sync`, `whoami`) to that saved host do not require `--insecure`. Every command transmitting an API key over unencrypted HTTP prints a warning to stderr. Re-running `login` replaces the host entry, requiring `--insecure` to be passed again.

### Proxy bypass (NO_PROXY)

If training machines define `HTTPS_PROXY` in their shell or service environment, add the server hostname to `NO_PROXY`:

```bash
export NO_PROXY=agenticml.lab.internal
```

Add the variable to `~/.config/systemd/user/agenticml-sync.service` so sync traffic reaches the internal server directly.

## Single sign-on with oauth2-proxy

By default, private-network deployments use local passwords and invite links. To integrate with a corporate identity provider, place oauth2-proxy between the TLS proxy and the server:

```
client ──▶ TLS proxy ──▶ oauth2-proxy ──▶ agenticml server serve
```

Requirements:
- Start the server with `--identity-header oauth2-proxy` and configure `AGENTICML_SERVER_TRUSTED_PROXIES` to oauth2-proxy's exact `/32` IP address.
- The outer TLS proxy must drop any client-supplied `X-Forwarded-Email` header.
- Configure oauth2-proxy to skip authentication for `/v2/` and `/healthz` endpoints (`--skip-auth-route` in oauth2-proxy). Sync clients authenticate using Bearer API keys and cannot negotiate interactive browser redirects.
- Associate user logins with their email identities:
  ```bash
  sudo -u agenticml /opt/agenticml/bin/agenticml server users set-identity LOGIN user@example.com --data /var/lib/agenticml-server
  ```

## Troubleshooting

### TLS certificate check failed

The client displays:
```
TLS certificate check failed (untrusted CA, wrong host name, expired, or a wrong clock); pass --ca-bundle PATH or set AGENTICML_CA_BUNDLE, and use the name in the certificate
```

Verify that:
- The CA bundle is specified via `--ca-bundle PATH` or was saved during `login`.
- The URL host matches a Subject Alternative Name (SAN) in the certificate.
- System clocks on both machines are synchronized.
- Test the TLS handshake directly:
  ```bash
  openssl s_client -connect agenticml.lab.internal:443 -CAfile /etc/ssl/certs/lab-ca.pem
  ```

One-shot commands do not retry a certificate failure, or an `https://` URL that points at a port that does not speak TLS. Continuous follow mode (`sync --follow`) retries with backoff and displays the error in its status line.

### TLS handshake failed

The client displays:
```
TLS handshake failed (the server closed the connection, or this is not an https port)
```

Other handshake failures (for example the peer closing the connection) are retried a few times before this hint appears. Verify that the proxy is actively listening on port 443 and terminating TLS, and that the client is not connecting to an unencrypted port with an `https://` scheme.

### Server redirect error

The client displays:
```
server redirected to another URL; use the final https:// URL as --host (refusing to send the API key on a redirect)
```

The client refuses to follow HTTP 3xx redirects to prevent exposing API keys. Update `--host` to the canonical `https://` URL.

### CA bundle permission errors

The client displays:
- `CA bundle not found or unreadable: <path>`
- `CA bundle is not owned by you or root: <path>`
- `CA bundle directory is not owned by you or root: <dir>`
- `CA bundle is writable by others: <path>; chmod go-w`
- `CA bundle directory is writable by others: <dir>; chmod go-w <dir>`
- ``CA bundle path in the credentials file is not absolute: <path>; run `agenticml login --ca-bundle PATH` again`` (the fix is to run login again with an absolute path)

Ensure the bundle file and its directory are owned by root or the current user and remove group/world write permissions:
```bash
chmod go-w /etc/ssl/certs/lab-ca.pem
```

### Proxy errors: HTTP 411, 413, or 408

- **HTTP 411 Length Required**: The proxy stripped `Content-Length`. In nginx, ensure `proxy_http_version 1.1;` and `proxy_request_buffering off;`.
- **HTTP 413 Payload Too Large**: In nginx, increase `client_max_body_size 4g;`.
- **HTTP 408 Request Timeout**: The server received the body too slowly. It allows about 30 s plus 2 s per MiB, so an upload slower than about 512 KiB/s fails. It also happens when the connection ended before the whole body arrived. Keep `proxy_request_buffering off;` and the 3 hour `proxy_read_timeout 3h;` and `proxy_send_timeout 3h;`, which prevent proxy-side 504 errors on long uploads.

### HTTP 403 forbidden host or cross-site request refused

- **HTTP 403 forbidden host**: The `Host` header received by the server does not carry the host name of `AGENTICML_SERVER_PUBLIC_URL` (or a loopback name); the port is not compared. Configure the proxy to pass the original `Host` unchanged (`proxy_set_header Host $http_host;` in nginx).
- **HTTP 403 cross-site request refused**: The browser's scheme, host or port differs from `AGENTICML_SERVER_PUBLIC_URL`. Ensure the browser address matches the configured public URL, including the port if it is not 443.

## Next

- [Authentication and keys](/agenticml-lite/server/keys/): manage workspace users, roles, and machine API keys.
- [Backups and restore](/agenticml-lite/server/backups/): configure automated backups for server data.

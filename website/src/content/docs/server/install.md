---
title: Installation and deployment
description: Run the AgenticML server on Linux or POSIX hosts.
sidebar:
  order: 2
---

The server is distributed as part of the `agenticml-lite` package on PyPI (Python 3.10+, POSIX only).

## Installation

Create a dedicated virtual environment and install `agenticml-lite`:

```bash
sudo python3 -m venv /opt/agenticml
sudo /opt/agenticml/bin/python -m pip install agenticml-lite
```

## Running the server

Start the server using `agenticml server serve` (a venv anywhere the user owns is fine, e.g. `python3 -m venv ~/agenticml-venv`):

```bash
/opt/agenticml/bin/agenticml server serve --data DIR --host 127.0.0.1 --port 8080 --public-url https://...
```

- `--data DIR` specifies the server data directory. If omitted, it falls back to `AGENTICML_SERVER_DATA`, then `~/.agenticml-server`.
- `--host` defaults to `127.0.0.1`.
- `--port` defaults to `8080`.
- `--public-url` specifies your canonical HTTPS address, or falls back to `AGENTICML_SERVER_PUBLIC_URL`.

## TLS proxy

The server serves plain HTTP over its configured port (typically loopback `127.0.0.1:8080`). In production or across a network, terminate TLS using a reverse proxy and configure the public URL:

### Laptop or workstation (Tailscale)

To share the server securely across your private Tailscale network:

1. Forward port 8080 to your Tailscale network:
   ```bash
   tailscale serve --bg 8080
   ```
2. Set your Tailscale machine URL and start the server:
   ```bash
   export AGENTICML_SERVER_PUBLIC_URL=https://<machine>.<tailnet>.ts.net
   /opt/agenticml/bin/agenticml server serve --port 8080
   ```

### Production reverse proxy

Use Caddy, Nginx, or a cloud load balancer (such as AWS ALB) to terminate TLS and forward requests to `127.0.0.1:8080`. Always configure `--public-url` (or `AGENTICML_SERVER_PUBLIC_URL`) to match the public HTTPS endpoint.

## Systemd service

For production deployments on Linux, run the server as a systemd system service using the unit provided in `deploy/systemd/agenticml-server.service` (see `deploy/systemd/README.md`):

1. Install the package and create the dedicated service user:
   ```bash
   sudo python3 -m venv /opt/agenticml
   sudo /opt/agenticml/bin/python -m pip install agenticml-lite
   sudo useradd --system --home /var/lib/agenticml-server --shell /usr/sbin/nologin agenticml
   sudo curl -fsSL -o /etc/systemd/system/agenticml-server.service https://raw.githubusercontent.com/agenticML/agenticml-lite/v2.2.0/deploy/systemd/agenticml-server.service
   ```
   Use the tag that matches the agenticml-lite version you installed (`pip show agenticml-lite`).
2. Edit `AGENTICML_SERVER_PUBLIC_URL` in the unit (or `sudo systemctl edit agenticml-server`), then `daemon-reload` and `enable --now`. Adjust `ExecStart` if the venv lives elsewhere.
3. Optionally set `AGENTICML_SERVER_KEY` in `/etc/agenticml-server.env` (root-owned, mode 0600); otherwise the server prints a first-admin setup line on first start.
4. Enable and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now agenticml-server
   ```
5. Check status and view logs:
   ```bash
   sudo systemctl status agenticml-server
   sudo journalctl -u agenticml-server -f
   ```

The unit's `StateDirectory=agenticml-server` automatically creates `/var/lib/agenticml-server` owned by the `agenticml` user.

## Configuration options

`agenticml server serve` accepts the following options:

| Flag | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--data DIR` | `AGENTICML_SERVER_DATA` | `~/.agenticml-server` | Path to server data directory. |
| `--host HOST` | | `127.0.0.1` | Network interface to bind. |
| `--port PORT` | | `8080` | Port to listen on. |
| `--public-url URL` | `AGENTICML_SERVER_PUBLIC_URL` | unset | Canonical public URL (e.g. `https://aml.example.com`). Required behind reverse proxies for Host/Origin checks and cookies. |
| | `AGENTICML_SERVER_KEY` | unset | Pre-configured admin API key named `env`. Stored in memory only. |
| | `AGENTICML_REMOTE_STALE_SECONDS` | `600` | Inactivity threshold before an active run is marked `stale`. |

## Next

Continue to [Authentication and keys](/agenticml-lite/server/keys/) to configure access.

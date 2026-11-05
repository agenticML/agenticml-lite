# Remove the Docker deployment (server ships via PyPI only)

Nov 5, 2026 · Supervisor plan · Release 2.0 · Base branch: `dev`

Decision (user, Nov 5, 2026): drop the container route from this repo. The
server is installed with `pip install agenticml-lite` and run with
`agenticml server serve`, usually under systemd behind a TLS proxy
(`tailscale serve`, Caddy, or a load balancer). This reverses the image, ECS
and image-workflow parts of package 6 ([6-packaging-release.md](6-packaging-release.md))
and backlog task C ([backlog-2.0-final.md](backlog-2.0-final.md)).

## Goal

No Dockerfile, image, compose file, ECS/Terraform example, image workflow or
Docker instructions remain. A self-hosted server is still easy to run and
is still checked end to end in CI.

## Tasks

| Task | Files | Who |
| --- | --- | --- |
| A | delete `docker/`, `.dockerignore`, `deploy/compose.yaml`, `deploy/ecs/`, `.github/workflows/image.yml`; edit `.github/dependabot.yml`, `.gitignore`, `src/agenticml/server/app.py` (comment only); rewrite `scripts/server_smoke.sh`; add a smoke step to `.github/workflows/test.yml`; add `deploy/systemd/agenticml-server.service`; update `deploy/systemd/README.md` | Codex |
| B | `README.md`, `docs/SERVER.md`, `docs/THREAT_MODEL.md`, `SECURITY.md`, `website/src/content/docs/server/*.md` | Antigravity, after A |
| C | `docs/plans/on-prem-roadmap.md`, `docs/plans/on-prem-server-plan.md` | Supervisor |

## A. Code and config (Codex)

1. **Delete**: `docker/Dockerfile`, `docker/requirements.txt` (the whole
   `docker/` directory), `.dockerignore`, `deploy/compose.yaml`,
   `deploy/ecs/` (task definition, Terraform, README),
   `.github/workflows/image.yml`.
2. **`.github/dependabot.yml`**: remove the `docker` and `pip` (`/docker`)
   entries; keep `github-actions`.
3. **`.gitignore`**: remove the Terraform block.
4. **`src/agenticml/server/app.py`**: reword the comment that mentions
   Docker's and ECS's stop timeouts to refer to a typical service manager
   stop timeout (systemd `TimeoutStopSec`). No code change.
5. **`scripts/server_smoke.sh`**: keep the same checks, without Docker.
   - Start `"$PYTHON" -m agenticml server serve --data "$temp_dir/data"
     --host 127.0.0.1 --port <free port>` in the background, with
     `AGENTICML_SERVER_KEY` set only for that process. Pick the free port
     with Python (`socket.bind(("127.0.0.1", 0))`) and pass it in;
     accept the small race, or retry once on "address in use".
   - Wait for `/healthz` (60 s); fail fast and print the server log if the
     process exits.
   - Keep the arbitrary-Host health check, the sync-key creation (now
     `"$PYTHON" -m agenticml server keys create --name smoke --scope sync
     --data "$temp_dir/data"`, which works while the server runs), the SDK
     run, `login`, `sync --all`, the `/v2/runs/<id>/state` size
     comparison, the admin dashboard login (with `Origin` and
     `X-AgenticML-Request`) and `/api/runs`, and the sync-key login 403.
   - Shutdown: send SIGTERM, wait up to 30 s, require exit status 0.
   - `trap` cleanup kills the server if still running and removes temp
     dirs. Drop `SMOKE_IMAGE` and every docker command. Keep the key
     handling (never echoed, never on a command line), the PYTHON/
     PYTHONPATH handling and `cd` to the repo root.
6. **`.github/workflows/test.yml`**: add a step to the `unit` job, only for
   the 3.12 matrix entry (`if: matrix.python == '3.12'`), that runs
   `PYTHON=python bash scripts/server_smoke.sh` after the unit tests. The
   job already installs the package.
7. **`deploy/systemd/agenticml-server.service`** (new): a system unit
   example for the server.
   - `User=agenticml`, `Group=agenticml` (comment: create with
     `useradd --system --home /var/lib/agenticml-server agenticml`),
     `ExecStart=/opt/agenticml/bin/agenticml server serve --data
     /var/lib/agenticml-server --host 127.0.0.1 --port 8080` (comment: a
     venv at /opt/agenticml; adjust the path),
     `Environment=AGENTICML_SERVER_PUBLIC_URL=https://aml.example.com`,
     optional `EnvironmentFile=-/etc/agenticml-server.env` for
     `AGENTICML_SERVER_KEY` (mode 0600, documented in a comment).
   - `Restart=on-failure`, `KillSignal=SIGTERM`, `TimeoutStopSec=30`.
   - Hardening: `NoNewPrivileges=yes`, `ProtectSystem=strict`,
     `ReadWritePaths=/var/lib/agenticml-server`, `StateDirectory=agenticml-server`
     (and drop the explicit ReadWritePaths if StateDirectory covers it),
     `PrivateTmp=yes`, `ProtectHome=yes`, `CapabilityBoundingSet=`,
     `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`.
   - Check with `systemd-analyze verify` if available.
8. **`deploy/systemd/README.md`**: two short sections, the existing sync
   user unit and the new server system unit (install the package into a
   venv, create the user, copy the unit, `systemctl enable --now`, read the
   first-admin setup line with `journalctl -u agenticml-server`, put
   `tailscale serve --bg 8080` or Caddy in front).

Checks: `pytest -q tests`; `bash -n` and shellcheck on the smoke script;
the smoke script prints `smoke: OK` locally; YAML of `test.yml` and
`dependabot.yml` parses; `git grep -n -i -w "docker\|ghcr\|cosign\|ecs\|terraform\|fargate"`
over non-plan files returns only lines that task B will rewrite (list them).

## B. Docs (Antigravity)

Rewrite every Docker/ECS/image instruction to the pip + systemd route:

- `README.md` "On-prem server": start the server with `pip install
  agenticml-lite` and `agenticml server serve`, or the systemd unit; put
  `tailscale serve --bg 8080` (laptop) or Caddy / a load balancer in front
  and set `AGENTICML_SERVER_PUBLIC_URL`; first admin key from the setup
  line (terminal or `journalctl -u agenticml-server`), the CLI
  `agenticml server keys create --data DIR`, or `AGENTICML_SERVER_KEY`
  (env or `EnvironmentFile`); sign in, sync keys, training machines,
  revoke (`agenticml server keys revoke NAME --data DIR`); back up (stop
  the service, copy the data dir, start); upgrade (`pip install -U
  agenticml-lite` in the venv, restart the service). Remove the image,
  cosign and AWS lines. "Releasing": remove the image, image.yml and GHCR
  bullets.
- `docs/SERVER.md`: same changes across the whole reference; drop the
  container image, signatures and ECS sections; add the systemd unit; keep
  the data layout, keys, sessions, client setup, statuses, limits,
  configuration (drop the "in Docker" defaults) and troubleshooting.
- `docs/THREAT_MODEL.md`: "host or container" → "host"; nothing else
  container-specific.
- `SECURITY.md`: ask for the `agenticml-lite` version and the deployment
  (OS, Python version, proxy), not an image tag or Docker/ECS.
- Website `server/` pages: same changes; `install.md` becomes pip +
  systemd + proxy.

Supervisor checks every command against the code and runs the site build.

## C. Plans (supervisor)

- Roadmap: 2.0 contents and package 6 description no longer mention the
  container image or ECS; add a dated note with this decision.
- `on-prem-server-plan.md`: decisions table (Image row), section 5 supply
  chain row, section 7 package 6, section 8 checklist (smoke runs the
  pip-installed server; no image or ECS checks).

## Done when

The deleted files are gone, CI runs the smoke script against a
pip-installed server and passes, the docs describe only the pip/systemd
route with commands that match the code, and the full suite is green.

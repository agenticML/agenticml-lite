# AgenticML Open-Source Server Plan

Nov 3, 2026 · @bharath

The self-hosted sync server ships inside the public, MIT-licensed `agenticml-lite` as `agenticml server`, with a free public Docker image. There is no private repo, no license file and no paid tier in release 1. The architecture and sync protocol v2 are the same as in the [On-Prem Server Plan](https://claude.ai/artifact/FPYx2JFx5vWSxfiBj7BCMx); this plan replaces the [Open-Core Server Plan](https://claude.ai/artifact/WCCrFX4Gbw5pPW4cY5ucV1).

## Decisions

The only new decision is the license and repo for the server; everything else carries over from the On-Prem plan.

| Topic | Position | Status |
| --- | --- | --- |
| Code location | Server lives in the public `agenticML/agenticml-lite` repo, MIT, next to the client | Decided (this request) |
| Deployment model | On-prem only. No hosted backend, so the sync protocol can change freely | Decided |
| Dashboards | The server hosts the shared dashboard; local `agenticml ui` keeps working unchanged | Decided |
| Users | Single user in release 1, one API key per machine. Teams in the next release | Decided |
| Storage | Server data directory is a plain v1 store. No database | Decided |
| Sync | Byte-for-byte copy of the v1 store over protocol v2 | Decided |
| Dependencies | Server uses the Python standard library only, like `agenticml ui`. `pip install agenticml-lite` gets it, no extra | Proposed |
| Licensing / paid tier | None in release 1. No license file, no tiers, no read-only lockout | Proposed |
| Version | `agenticml-lite` 2.0.0, because the hosted sync is removed | Proposed |
| Private `agenticml-server` repo | Left as is (the hosted FastAPI stack). Not used or changed by this plan | Proposed |

## What changes from the earlier plans

This plan is the On-Prem plan made public, plus the release hygiene an open-source server needs. Everything the Open-Core plan added for selling the server is dropped.

| Area | On-Prem plan | Open-Core plan | This plan |
| --- | --- | --- | --- |
| Server repo | `agenticml-lite` | New private `agenticml-onprem` | `agenticml-lite` (public) |
| Server license | Not discussed | Proprietary + EULA | MIT |
| CLI | `agenticml server` | `agenticml-server` | `agenticml server` |
| Dashboard hooks | Internal | Public API under semver | Internal; no cross-repo contract |
| License file, tiers, grace period | No | Yes (S4) | No |
| Image registry | Not decided | Private, per-customer pull tokens | Public GHCR, signed, multi-arch |
| Cross-repo CI | No | Yes | No (one repo) |
| Test CI in GitHub Actions | No | No | Yes, Linux + macOS (new package 0) |
| SECURITY.md + threat model | No | No | Yes |
| Contributor docs for the server | No | No | Yes |

Dropped from the Open-Core plan: S0 (private repo), S4 (licensing), the private registry, AWS Marketplace metering, and the CI check that blocked server code from the public repo.

## Where the code lives

All new code goes into `agenticml-lite` as a `server` subpackage. It imports the existing store reader and dashboard directly, so nothing is copied or exposed as a cross-repo API.

| Path | New or changed | Holds |
| --- | --- | --- |
| `src/agenticml/server/app.py` | New | Server class, startup, data-dir layout, single-writer lock |
| `src/agenticml/server/ingest.py` | New | The `/v2` endpoints, path allow-lists, offset appends |
| `src/agenticml/server/keys.py` | New | Key create / list / revoke, SHA-256 storage in `keys.json` (0600) |
| `src/agenticml/server/webauth.py` | New | Dashboard key login, session cookie, CSRF |
| `src/agenticml/ui/server.py` | Changed | Handler-class parameter and optional auth callback, so the server can mount the dashboard. `agenticml ui` behaviour unchanged |
| `src/agenticml/store/` | Changed | Run status from `remote.json` when present |
| `src/agenticml/sync.py`, `auth.py`, `client.py` | Rewritten | Sync client v2; the `api.agenticml.xyz` default host removed |
| `src/agenticml/cli.py` | Changed | `agenticml server serve`, `agenticml server keys …`, `sync --follow` |
| `docs/PROTOCOL.md`, `docs/SERVER.md` | New | Wire protocol spec; install and operations guide |
| `docker/Dockerfile`, `deploy/compose.yaml`, `deploy/ecs/` | New | Image, compose file, ECS task definition and Terraform example |
| `.github/workflows/test.yml`, `image.yml` | New | Test CI (Linux + macOS); image build, sign, push |
| `SECURITY.md`, `docs/THREAT_MODEL.md` | New | Disclosure policy; what the server defends against |

Data directory on the server: `<data>/store` (a v1 store), `<data>/server/keys.json` and `<data>/server/server.lock`.

## Architecture and sync protocol v2

Unchanged from the [On-Prem Server Plan](https://claude.ai/artifact/FPYx2JFx5vWSxfiBj7BCMx): one container, a v1 store on disk, the same dashboard code as `agenticml ui`. Training machines copy their store files to it; nothing is translated.

| Method and path | Purpose |
| --- | --- |
| `GET /healthz` | Health check, no auth |
| `GET /v2/whoami` | Key name, `protocol: 2`, and a `features` list (`lineage`, `byref`) |
| `POST /v2/objects/missing` | Which of up to 10,000 SHA-256 objects to upload |
| `PUT /v2/objects/{sha256}` | Streamed, hash-verified upload (422 on mismatch), 4 GiB cap |
| `GET /v2/runs/{id}/state` | Server-side sizes and hashes, for `--reset` or a lost cursor |
| `PUT /v2/runs/{id}/files/{path}` | Replace an atomic file (`meta.json`, indexes, overviews) |
| `POST /v2/runs/{id}/append/{path}?offset=N` | Append-only files; overlapping bytes must match (409), so replays are no-ops |
| `POST /v2/runs/{id}/commit` | End of pass and heartbeat; writes `remote.json` |

Server run status comes from `remote.json`: `running` turns `stale` after 10 minutes without a sync (`AGENTICML_REMOTE_STALE_SECONDS`). Never sent: `run.lock`, `sync.json`, `notes.md`, `remote.json`. Copying `meta.json` whole also carries `task`, `task_source` and `arch`, which closes the blocked task in `task.md`.

## Security for a public server

With the code and protocol public, anyone can read the attack surface, so the defaults must be safe without configuration. The controls from the On-Prem plan stay; four are added.

| Control | Design |
| --- | --- |
| API keys | Created on the server, shown once, stored as SHA-256 in `keys.json` (0600). Revoke takes effect without a restart. `AGENTICML_SERVER_KEY` can bootstrap one on ECS |
| Safe default bind | `serve` binds `127.0.0.1` by default. Binding another address needs `--host` and at least one key, or it refuses to start (new) |
| TLS | Plain HTTP behind an ALB, `tailscale serve` or Caddy. The client refuses plain HTTP to non-local hosts without `--insecure` |
| Single writer | Exclusive lock on `server.lock`; a second process refuses to start |
| Input hardening | Allow-listed paths only, no-follow opens, containment checks, per-request size limits and body-read deadlines |
| Dashboard login | API key exchanged for an HttpOnly, Secure, SameSite=Strict cookie; CSRF check on notes edits |
| Disclosure | `SECURITY.md` with a private reporting address and GitHub private advisories (new) |
| Threat model | `docs/THREAT_MODEL.md`: what is in scope (network attacker with no key, a leaked key, malicious run files) and what is not (a compromised host) (new) |
| Supply chain | Image signed with cosign, SBOM attached, base image pinned by digest, Dependabot for the image and Actions (new) |
| Review | Packages marked † get a second read-only review from the coder that did not write them, as in the earlier plans |

## Distribution

Release 1 ships as one PyPI release and one public image, both built from tags in `agenticml-lite`.

| Channel | What | Notes |
| --- | --- | --- |
| PyPI | `agenticml-lite` 2.0.0 | Client and server together; still only `requests` as a dependency |
| Container image | `ghcr.io/agenticml/agenticml-server:2.0.0` (+ `latest`) | Public, `linux/amd64` + `linux/arm64`, includes `git` for lineage, runs as non-root UID 10001, `/data` volume |
| Deploy examples | `deploy/compose.yaml`, `deploy/ecs/` | ECS: one Fargate task, `minimumHealthyPercent: 0`, EFS at `/data`, ALB health check on `/healthz` |
| Laptop guide | `docs/SERVER.md` | `docker run` + `tailscale serve`, as in the On-Prem plan |
| Docs site | New "Self-hosted server" pages on the existing Astro site | Install, keys, sync, backups, upgrade |

The existing `publish.yml` keeps publishing to PyPI; a new `image.yml` builds, signs and pushes the image on the same tag.

## Work plan

Release 1 is packages 0–6; 7 and 8 can follow as 2.1. Per `AGENTS.md`, the open macOS notes-408 task in `task.md` goes first, verified on the macOS runner that package 0 adds.

| # | Package | Depends on | Coder | Size | Done when |
| --- | --- | --- | --- | --- | --- |
| T | Fix macOS notes PUT 408 (open task) | 0 | Cursor | S | Test passes repeatedly on the macOS CI runner |
| 0 | Test CI: `pytest` on Linux + macOS, Python 3.9 and 3.12, plus the testbed UI suite | None | Codex | S | Workflow green on `main` and on PRs |
| 1 † | Server ingest: `agenticml server serve`, `keys create/list/revoke`, `/v2` endpoints, lock, safe default bind | None | Codex | L | Tests cover auth, allow-lists, traversal, idempotent appends, and a real run read back with `RunReader` |
| 2 | Run status from `remote.json` in the store reader, plus `FORMAT.md` section | None | Cursor | S | Every status mapping tested; falls back to today's logic when the file is missing or invalid |
| 3 | Sync client v2 + `docs/PROTOCOL.md`; keeps `--prune`, `--reset`, `--dry-run` and the gc synced rule; removes the hosted host | 1 | Codex | L | E2E syncs real runs to an in-process server and they match; replayed sync adds no duplicates; `task`/`arch` reach the server |
| 4 † | Server dashboard: key login, session cookie, CSRF, "last synced" | 1, 2 | Cursor | M | Login and `/api/*` session tests; local UI tests unchanged; no horizontal overflow at 375px |
| 5 | `sync --follow`: backoff, heartbeats, systemd and launchd examples | 3 | Cursor | M | Tests cover server down and back, and new runs picked up without restart |
| 6 | Packaging + release: Dockerfile, compose, ECS/Terraform example, `image.yml`, `SECURITY.md`, threat model, `docs/SERVER.md`, site pages | 1, 3, 4 | Codex; prose by Antigravity | M | `docker build` passes; smoke script syncs a run into the container and reads it back |
| 7 | Lineage: push a `git bundle` per run so code diff works on the server | 3 | Codex | M | Server code diff between two synced runs matches the local one |
| 8 | By-reference artifacts, opt-in with `sync --artifacts` | 3 | Cursor | S–M | By-ref artifact fetchable from the server; changed or missing source skipped with a warning |

Order: 0 → T, then 1 and 2 in parallel, then 3 and 4, then 5 and 6. Sonnet reviews every package; † packages also get the other coder's read-only review. Each package passes `pytest -q tests`; dashboard packages also pass the testbed UI suite.

## Compatibility and out of scope

Local-only use (training plus `agenticml ui`) is unaffected; only sync to the old hosted service breaks.

- Sync to `api.agenticml.xyz` is removed; `agenticml login` then requires `--host`.
- Old-host entries in `sync.json` are ignored, not deleted.
- `agenticml gc` still treats a run as synced when any host reports it complete with a matching fingerprint.

Not in release 1: multiple users, roles and SSO (next release); more than one server task; an S3 object backend; server-side gc and quotas; two-way notes sync.

## Open questions

Each has a recommendation; approving the plan as written accepts them.

- [ ] **License: MIT or Apache-2.0?** Recommended: keep MIT for one license across the repo. Apache-2.0 adds a patent grant if that matters to customers.
- [ ] **Do team features (next release) stay open source too?** Recommended: yes, same repo, so there is no second split later. Revenue, if any, from support or a managed install, not code.
- [ ] **Image name.** Recommended: `ghcr.io/agenticml/agenticml-server`. The private repo is also named `agenticml-server`; `agenticml-lite-server` avoids confusion if you prefer.
- [ ] **Version.** Recommended: 2.0.0, since removing hosted sync is breaking.
- [ ] **PR granularity.** Recommended: one PR per package, merged in the order above, so each can be reviewed and reverted alone.

# Package 25: Docs, threat model, version 3.0.0

Nov 9, 2026 · Supervisor plan · Release 3.0 · Coder: Codex; prose Antigravity · Size: M

Source: [on-prem-3.0-plan.md](../on-prem-3.0-plan.md), package summary
25, decisions D4 (client support window) and D8 (version 3.0.0), and the
release checklist. Runs after 16–24.

## Goal

The 3.0 server is documented for both ways of running it, the threat
model covers workspaces and the hosted service, the client support
window (D4) is enforced, and the version is 3.0.0. The release checklist
in the 3.0 plan is ticked except the owner-only items.

## 25.1 Code (Codex)

- **Client support window (D4).** Sync clients send
  `User-Agent: agenticml-lite/<version>` (`client.py`). The server gains
  `--min-client-version X.Y` (env `AGENTICML_SERVER_MIN_CLIENT_VERSION`,
  default `2.2`). On `/v2/*` requests (not `/healthz`, not dashboard
  routes), a User-Agent of the form `agenticml-lite/<major>.<minor>[...]`
  below the minimum gets `426 Upgrade Required` with the JSON error the
  client already prints (check `sync.py`'s error display for 4xx bodies
  and the 2.0/2.1 client code at the tags `v2.0.0`/`v2.1.0` to be sure
  those clients show the text) saying `this server needs agenticml-lite
  >= 2.2; run: pip install -U agenticml-lite`. A missing or foreign
  User-Agent is allowed (curl, other tools, the protocol stays open).
  Test: 2.0, 2.1 rejected with the text; 2.2, 3.0, missing, `curl/8`
  accepted; the flag raises/lowers the bar; `/healthz` unaffected.
- **Version 3.0.0** in `pyproject.toml` and `src/agenticml/__init__.py`;
  any test or doc that pins the version string.
- **Smoke on the base install:** `scripts/server_smoke.sh` already runs
  in CI; make sure it runs in a fresh venv with `pip install .` only (no
  extras) and exercises the default workspace URLs (`/w/default/...`),
  a sync, and `server status`/`verify`. Adjust the CI step if it installs
  `[dev]`.
- **2.2 client compatibility check:** a test that runs the 2.2.0 client
  (installed from the `v2.2.0` tag into a temporary venv, or vendored
  through `git show v2.2.0:src/agenticml/sync.py` + `client.py` if
  installing is not possible offline) against a 3.0 server and syncs a run
  (marked `slow` if it needs a venv build). If neither is possible, report
  it and the supervisor runs it by hand.

## 25.2 Docs (Antigravity drafts; supervisor checks every command and flag against the code)

- `docs/SERVER.md`: rewrite for 3.0 — the two modes; configuration table
  (`--data`, `--database`, `--objects`, `--open-signup`, `--limits`,
  Firebase flags, `--min-client-version`, trusted proxy); workspaces
  (`/w/<slug>` URLs, roles owner/admin/member/viewer, server admins,
  `server workspaces` and `server accounts` commands); keys bound to a
  workspace; Postgres and many processes; S3 objects; metric summaries and
  `server metrics rebuild`; limits and usage; backups (`server backup`,
  `pg_dump`, restore); `status`/`verify`; upgrading from 2.x (automatic
  conversion into `default`, `*.2x` files, refuse-twice message);
  troubleshooting. Keep the EFS section with the modelled numbers until
  the owner's real-EFS run replaces them.
- `docs/HOSTED.md` (new): operating the hosted service — architecture
  (ALB, ECS tasks, RDS, S3, EFS), deploys, first server admin, customer
  workspaces, plans and limits, suspending and deleting workspaces,
  support access (the customer adds us as a member; audited), backups and
  restore, monitoring and incidents; links to `deploy/aws/README.md` for
  the commands.
- `docs/THREAT_MODEL.md`: add workspace isolation (URL/key binding,
  membership checks, one 404, RLS, per-workspace object keys and stores,
  the isolation suite), operator access to customer data (server admins
  do not see runs unless members; AWS operators can; logged where), shared
  Git processing of hostile bundles on shared infrastructure, noisy
  neighbours (limits, request rate, upload caps), open sign-up abuse,
  Firebase trust (token verification, verified email), S3 (SSE, no
  pre-signed URLs, task-role scope), many processes (the paused-process
  residual from the package 20 Backlog entry), and secrets (RDS-managed
  password, no URL in logs).
- `docs/PROTOCOL.md`: client support window and the 426 answer; a key
  belongs to one workspace and `whoami` reports it; `/state` comes from
  the database (no `remote.json` on the server); runs-list metric
  parameters if the dashboard API is described there.
- `README.md` and `website/src/content/docs/server/*.md`: 3.0 install,
  `[postgres]`/`[s3]`/`[firebase]`/`[hosted]` extras, workspaces, backup
  page, upgrade page (2.x → 3.0).
- `SECURITY.md`: supported versions table (3.0.x supported; 2.2.x
  security fixes until a date the owner sets — write "until 3.1").

## 25.3 Release bookkeeping (supervisor)

- Roadmap: statuses for 16–25, the 3.0 release row, Backlog entries for
  owner-only items (real S3, real Firebase, AWS apply and exit criterion
  6, real-EFS benchmark, terms/privacy pages).
- 3.0 plan release checklist: tick what is done; leave owner items open.
- Tagging and PyPI publishing are owner steps after the PR merges (as in
  2.2); not done here.

## Tests and checks

- `testbed/.venv/bin/python -m pytest -q tests` on SQLite; the Postgres
  suite; the UI suite; `scripts/server_smoke.sh` on a base-install venv.
- Docs: every command, flag and env var named in the docs exists
  (supervisor greps `cli.py` for each); links resolve; the website builds
  if its toolchain is available (`npm run build` in `website/`),
  otherwise note it.

## Done when

The client window is enforced and tested, the version is 3.0.0, the docs
and threat model cover 3.0, the smoke test passes on a base install, and
the release checklist is ticked except owner items, which are in the
Backlog.

# Package 10b †: Server setup command (`agenticml server up`)

Nov 6, 2026 · Supervisor plan · Release 2.1 · Coder: Cursor · Size: M ·
Security-sensitive (second read-only review by Codex)

Source: [on-prem-roadmap.md](../on-prem-roadmap.md) (2.1, package 10b).
Base branch: `dev`. Ships in the single 2.1 PR, one commit per package.

## Goal

One command replaces the manual laptop setup (find the tailnet URL, create
the first admin key, `server serve --public-url`, `tailscale funnel --bg`):

```
agenticml server up [--data D] [--port 8080] [--install]
```

It:

1. Checks Tailscale is installed, running and logged in, that HTTPS
   certificates are enabled for the tailnet, and that this node may use
   Funnel. Each missing prerequisite exits 1 with one clear line saying the
   exact fix (these approvals stay manual).
2. Reads the public URL from `tailscale status --json`
   (`https://<Self.DNSName without the trailing dot>`).
3. Creates the first admin key if none exists, and prints it once, so the
   `/setup` link is never armed while the server is reachable from the
   internet.
4. Serves on loopback (`127.0.0.1:<port>`) with `--public-url` set.
5. Turns Funnel on (`tailscale funnel --bg --https=443
   http://127.0.0.1:<port>`) once the server is listening, and off
   (`tailscale funnel --https=443 off`) when it stops (SIGTERM, SIGINT, or an
   exception), in a `finally`.
6. `--install` writes a systemd user unit (Linux) or a launchd agent
   (macOS) that runs `agenticml server up --data D --port P` at login/boot,
   prints how to enable/start it, and exits without serving.

Done when a fresh machine with Tailscale logged in reaches a working HTTPS
server with one command, each missing prerequisite gives a clear error, and
Funnel is off after the server stops.

Out of scope: installing or logging in to Tailscale, approving HTTPS or
Funnel in the admin console, tailnet-only (`tailscale serve`) mode, Windows,
other tunnels (Cloudflare etc.).

## What exists today

- `cli.py` `cmd_server_serve` → `server.app.serve(data, host, port,
  public_url)`; `make_server` validates the public URL, prepares and locks
  the data dir; `IngestServer.__init__` calls `self.web.arm_setup(base)`,
  which logs a one-time `/setup?token=` link only when no admin key exists
  (`KeyStore.prepare_setup`). `serve()` installs a SIGTERM handler and runs
  `serve_forever`, then `server_close(drain=...)`.
- `server/keys.py` `KeyStore(server_dir).create(name, scope="admin",
  created_by=..., only_if_no_admin=True)`, `has_admin()`.
- `deploy/systemd/agenticml-server.service`: the system unit for
  `server serve` (stays as is; `up` is the laptop path).
- Tailscale CLI on this machine: 1.104; `tailscale status --json` has
  `BackendState` (`Running` when up, `NeedsLogin`, `Stopped`),
  `Self.DNSName`, `CertDomains` (non-empty when HTTPS is enabled), and
  `Self.CapMap` / `Self.Capabilities` (contains `funnel` when the node may
  use Funnel; `https` when HTTPS is enabled).

## Files to touch

- `src/agenticml/server/up.py` (new): prerequisite checks, key bootstrap,
  funnel on/off, unit writers.
- `src/agenticml/server/app.py`: let `serve()` accept an `on_ready(server)`
  callback (called after bind, before `serve_forever`) and keep stop/cleanup
  ordering such that the caller's `finally` runs after `server_close`. Keep
  the existing behaviour of `server serve` unchanged.
- `src/agenticml/cli.py`: `server up` subparser and `cmd_server_up`.
- `docs/SERVER.md`: a short "Quick start on a laptop: `server up`" section,
  linking the manual steps; `README.md` on-prem quick start only if it lists
  the manual Tailscale steps today.
- `tests/test_server_up.py` (new).

## Approach

### Tailscale wrapper

- Find the binary with `shutil.which("tailscale")`; on macOS also try
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale`. Missing →
  `Tailscale is not installed: see https://tailscale.com/download`.
- Every call is an argument list, no shell, `timeout=15`, stdin `/dev/null`,
  output captured; errors show only the first stderr line.
- `status = json.loads(tailscale status --json)`; validate shapes defensively
  (missing/odd fields → treat the prerequisite as missing, never crash).

### Checks, in order, with the exact messages

1. Not installed → above.
2. `BackendState != "Running"` → `Tailscale is not running or not logged in:
   run "tailscale up"` (include the state).
3. No `Self.DNSName` → `Tailscale has no MagicDNS name for this machine:
   enable MagicDNS in the admin console (DNS page)`.
4. `CertDomains` empty and `https` not in the node's caps → `HTTPS
   certificates are not enabled for this tailnet: enable HTTPS in the admin
   console (DNS page)`.
5. `funnel` not in `Self.CapMap` keys or `Self.Capabilities` → `this machine
   may not use Funnel: add the "funnel" node attribute in the tailnet policy
   (https://tailscale.com/kb/1223/funnel)`.

All checks run before the data directory is touched or a key is created.

### Key bootstrap

- Open `KeyStore(<data>/server)` the way `cmd_server_keys` does (create the
  data directory first with `prepare_data_dir`).
- If `has_admin()` is false: `create("admin", scope="admin",
  created_by="server up", only_if_no_admin=True)`; print exactly once:
  the key, `Sign in at <public-url>/login with this admin key. It is shown
  only once.` If the name `admin` is taken by a non-admin key, use
  `admin-<4 random hex>`.
- If an `AGENTICML_SERVER_KEY` env key is set, it already counts as an admin
  key; do not create one.
- Because an admin key exists before `make_server` runs, `arm_setup` never
  arms the setup token. Add a test that asserts no `/setup?token=` line is
  logged.

### Serving and Funnel

- Start the server with host `127.0.0.1`, `--port` (default 8080),
  `public_url` from step 2, through `serve(..., on_ready=...)`.
- `on_ready`: run `tailscale funnel --bg --https=443
  http://127.0.0.1:<bound port>`. If it fails: log the first stderr line,
  stop the server, exit 1 (Funnel off is still attempted in `finally`).
  Then print `AgenticML is live at <public-url>`.
- `finally` (after the server has stopped): `tailscale funnel --https=443
  off`; on failure print a warning with the manual command. Never call
  `tailscale funnel reset` (it would remove the user's other serve configs).
- A second `server up` on the same data dir fails on the existing
  single-writer lock before touching Funnel.

### `--install`

- Run the checks and key bootstrap first (so the key is printed to the
  terminal, not to a service log), then write the unit and exit 0:
  - Linux: `~/.config/systemd/user/agenticml-server-up.service`,
    `ExecStart=<sys.executable> -m agenticml server up --data <abs> --port
    <p>`, `Restart=on-failure`, `RestartSec=30` (a prerequisite failure at
    boot, such as Tailscale not up yet, retries later; do not set
    `RestartPreventExitStatus`), `WantedBy=default.target`. Print `systemctl --user
    daemon-reload && systemctl --user enable --now agenticml-server-up` and
    `loginctl enable-linger $USER` (to start at boot without login).
  - macOS: `~/Library/LaunchAgents/xyz.agenticml.server-up.plist` with
    `ProgramArguments`, `RunAtLoad`, `KeepAlive` (on failure), stdout/stderr
    to `~/Library/Logs/agenticml-server-up.log`. Print `launchctl load -w
    <plist>`.
  - Write files atomically, mode 0644; refuse to overwrite an existing file
    that is not ours (no marker comment / label) unless `--force`.
  - Values are quoted/escaped properly (systemd: quote paths with spaces;
    plist: `plistlib`).
- Other platforms: `--install is supported on Linux (systemd) and macOS`.

## Constraints and conventions

- Standard library only. No shell. Never log or print the setup token or key
  more than the one intended print.
- `server serve` behaviour is unchanged (existing tests pass untouched).

## Edge cases to test

Use a fake `tailscale` executable (a small Python script written to
`tmp_path/bin`, put first on `PATH`) whose `status --json` output and exit
codes come from env vars or a JSON file, and that records every invocation to
a log file.

1. Each failed prerequisite (not installed, `NeedsLogin`, `Stopped`, no
   DNSName, no HTTPS, no funnel cap, malformed JSON) exits 1 with its exact
   message, creates no data dir, no key, and never calls `funnel`.
2. Happy path: server starts on loopback with the right `public_url`;
   `funnel --bg --https=443 http://127.0.0.1:<port>` recorded; `/healthz`
   answers; after SIGTERM/stop, `funnel --https=443 off` recorded and the
   process exits 0.
3. First admin key printed once; no `/setup?token=` in logs; second start
   does not create another key.
4. Funnel-on failure: server stopped, exit 1, `off` still attempted.
5. Funnel-off failure: warning with the manual command; exit code still
   reflects the server outcome.
6. Data dir already locked by another server: exit 1 before any funnel call.
7. `--install` on Linux writes the unit with the right `ExecStart`
   (paths with spaces quoted), refuses to overwrite a foreign file, and does
   not serve. macOS writer tested by calling the plist writer directly with a
   temp home (no launchctl call).
8. `AGENTICML_SERVER_KEY` set: no key created.

## Checks

- `testbed/.venv/bin/python -m pytest -q tests` from the repo root.
- Manual check on a real tailnet is needed for the "fresh machine" part of
  "done when"; the supervisor records it in the Backlog if it cannot be done
  in this environment.

## Done when

- Tests above pass; docs updated.
- Second review by Codex finds no blockers.
- Roadmap status for package 10b is `done` in the 2.1 PR (with the manual
  real-tailnet check noted in the Backlog if not yet run).

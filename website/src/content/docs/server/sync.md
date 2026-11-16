---
title: Syncing runs
description: Configure training machines to synchronize runs to the server.
sidebar:
  order: 4
---

Training machines push runs and objects to the server using the client CLI over protocol v2.

## Client setup

Install `agenticml-lite` on the training machine:

```bash
pip install agenticml-lite
```

PyPI has only 1.0.0, which cannot sync to a server. 3.0 is not on PyPI yet, so until it is, install the client from GitHub (this follows the `dev` branch, so it moves as development continues):

```bash
pip install 'agenticml-lite @ git+https://github.com/agenticML/agenticml-lite@dev'
```

Using the hosted service at `app.agenticml.xyz`? Follow the [hosted quickstart](/agenticml-lite/hosted/quickstart/) instead.

Log in with your server URL:

```bash
agenticml login --host https://aml.example.com
```

Enter your API key at the prompt, or pipe it via standard input:

```bash
echo "$AGENTICML_API_KEY" | agenticml login --host https://aml.example.com
```

Do not pass keys with `--key` on the command line; CLI arguments are visible to other users in `ps`.

Credentials are stored in `~/.config/agenticml/credentials.json` (permissions `0600`). Verify the login:

```bash
agenticml whoami
```

The response confirms the key name, owning user, assigned role, and bound workspace slug. If the API key is invalid or revoked, `agenticml whoami` exits with status code `1`.

### Syncing to multiple workspaces

The CLI saves one API key per server URL. If a machine needs to sync runs to multiple workspaces on the same server, set the `AGENTICML_API_KEY` environment variable per process or command:

```bash
AGENTICML_API_KEY=aml_... agenticml sync --all
```

## Synchronizing runs

### One-shot sync

To push all unsynced or updated runs to the server:

```bash
agenticml sync --all
```

To sync a specific run:

```bash
agenticml sync RUN_ID
```

### Continuous follow mode

Use `--follow` to monitor the local store and continuously sync new records:

```bash
agenticml sync --follow
```

Behavior of `--follow`:
- Implies `--all`.
- Checks for new metrics and files every `--interval` seconds (defaults to 30 seconds).
- Sends commit heartbeats for active running runs at least every 60 seconds.
- Backs off up to 5 minutes when the server is unavailable.
- **Storage limits**: If the target workspace reaches its storage limit, the server answers HTTP `403 Forbidden` (`storage limit reached`). `--follow` pauses that run and automatically retries every 10 minutes, resuming synchronization once an administrator raises the quota.

### Background service with systemd

On Linux machines, run `sync --follow` as a user-level systemd service:

```bash
mkdir -p ~/.config/systemd/user
curl -fsSL -o ~/.config/systemd/user/agenticml-sync.service https://raw.githubusercontent.com/agenticML/agenticml-lite/main/deploy/systemd/agenticml-sync.service
```

The unit file is the same for 2.2 and 3.0, so `main` works for either version.

```bash
systemctl --user daemon-reload
systemctl --user enable --now agenticml-sync
loginctl enable-linger $USER
```

If the server rejects credentials (exit code 3), the service stops without restarting. Update credentials with `agenticml login` and restart the service.

## Server run status

The server updates run status based on commits recorded in the server database:

| Status | Condition |
| --- | --- |
| `finished` | Run was cleanly finished by the script. |
| `crashed` | Run process died without finishing. |
| `running` | Run is active and committed within `AGENTICML_REMOTE_STALE_SECONDS` (default 600 s). |
| `stale` | Run is marked active, but no commit has occurred within `AGENTICML_REMOTE_STALE_SECONDS`. |

`agenticml sync --follow` sends heartbeat commits every 60 seconds for active runs, ensuring runs remain in `running` status.

## Full fidelity features

- **Lineage code diffs**: When Git is installed on the server, `agenticml sync` replicates shadow Git bundles, allowing the web dashboard to render source code diffs and commit histories.
- **By-reference artifacts**: Use `agenticml sync --artifacts` to upload files logged with `copy=False`.
- **User ownership**: Runs are owned by the account holding the sync key. Any key belonging to that account can append to the run.

## Next

Continue to [Backups and restore](/agenticml-lite/server/backups/) to safeguard server data.

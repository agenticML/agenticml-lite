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

If the API key is rejected or revoked by the server, `agenticml whoami` exits with status code `1`.

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

### Background service with systemd

On Linux machines, run `sync --follow` as a user-level systemd service:

```bash
mkdir -p ~/.config/systemd/user
curl -fsSL -o ~/.config/systemd/user/agenticml-sync.service https://raw.githubusercontent.com/agenticML/agenticml-lite/v2.0.0/deploy/systemd/agenticml-sync.service
```

Use the tag that matches the agenticml-lite version you installed (`pip show agenticml-lite`).

```bash
systemctl --user daemon-reload
systemctl --user enable --now agenticml-sync
loginctl enable-linger $USER
```

If the server rejects credentials (exit code 3), the service stops without restarting. Update credentials with `agenticml login` and restart the service.

## Server run status

The server updates the status of each run based on commits recorded in `remote.json`:

| Status | Condition |
| --- | --- |
| `finished` | Run was cleanly finished by the script. |
| `crashed` | Run process died without finishing. |
| `running` | Run is active and committed within `AGENTICML_REMOTE_STALE_SECONDS` (default 600 s). |
| `stale` | Run is marked active, but no commit has occurred within `AGENTICML_REMOTE_STALE_SECONDS`. |

`agenticml sync --follow` sends heartbeat commits every 60 seconds for active runs, ensuring runs remain in `running` status.

## Limitations in Release 2.0

- **Run notes (`notes.md`)**: Notes are not synced to the server in 2.0.
- **By-reference artifacts**: Artifacts logged with `copy=False` are not transferred.
- **Lineage code diffs**: The server renders the lineage graph, but Git commit diffs are not uploaded in 2.0 (the dashboard displays "code diff not available on this server").
- **Key ownership**: A run is owned by the key that creates it. Pushing to an existing run ID using a different key is refused.

## Next

Continue to [Backups and restore](/agenticml-lite/server/backups/) to safeguard server data.

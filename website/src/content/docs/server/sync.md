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

Syncing to a server needs version 2.2 or newer. 1.0.0 cannot sync.

Using the hosted service at `app.agenticml.xyz`? Follow the [hosted quickstart](/hosted/quickstart/) instead. Syncing to a server on a private network with an internal CA? See [Private networks](/server/private-network/).

Log in with your server URL:

```bash
agenticml login --host https://aml.example.com
```

Enter your API key at the prompt, or pipe it via standard input:

```bash
echo "$AGENTICML_API_KEY" | agenticml login --host https://aml.example.com
```

Do not pass keys with `--key` on the command line; CLI arguments are visible to other users in `ps`.

`login` checks the key against the server unless you pass `--no-verify`. It needs `--host` unless a default host is already saved; `AGENTICML_HOST` has no default. Credentials are stored in `~/.config/agenticml/credentials.json` (permissions `0600`). The directory is `AGENTICML_CONFIG_DIR`, else `$XDG_CONFIG_HOME/agenticml`, else `~/.config/agenticml`. Verify the login:

```bash
agenticml whoami
```

The response confirms the key name, owning user, assigned role, and bound workspace slug. If the API key is invalid or revoked, `agenticml whoami` exits with status code `1`.

`sync` takes the key from `--api-key`, then `AGENTICML_API_KEY`, then the saved file. `whoami` uses `AGENTICML_API_KEY`, then the file. `whoami --offline` skips the server check. `agenticml logout` removes the saved key for one host. On `login`, `whoami`, and `sync`, `--insecure` allows `http://` to a non-local host; localhost does not need it.

### Syncing to multiple workspaces

The CLI saves one API key per server URL. If a machine needs to sync runs to multiple workspaces on the same server, set the `AGENTICML_API_KEY` environment variable per process or command:

```bash
AGENTICML_API_KEY=aml_... agenticml sync --all
```

The sync cursor (`runs/<id>/sync.json`) is kept per server and workspace, so one store can sync a run to several workspaces of one server, each with its own key.

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

### What sync does

Sync copies each run's store files byte for byte (`meta.json`, `events.jsonl`, scalars, overviews, indexes) plus the objects they reference (code, media, copied artifacts) to the server over [protocol v2](https://github.com/agenticML/agenticml-lite/blob/main/docs/PROTOCOL.md). A per-file cursor in `runs/<id>/sync.json` tracks accepted bytes, so an interrupted sync resumes from the last accepted request.

- `--all` selects runs that are incomplete or changed.
- `--watch N` repeats every N seconds until every selected run has synced, failed permanently, or gone stale.
- `--dry-run` prints counts without sending or saving state.
- `--reset RUN_ID ...` forgets those runs' cursors for this host, in every workspace, and the next sync resumes from the server's copy. It rejects `--all`.

Run notes (`notes.md`) are not uploaded. A run id already owned by another user on the server fails with an error naming that user (`run id <id> belongs to user <name> on this server`). Ownership is per user, so any key of the owning user may write to the run.

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
curl -fsSL -o ~/.config/systemd/user/agenticml-sync.service https://raw.githubusercontent.com/agenticML/agenticml-lite/v3.4.0/deploy/systemd/agenticml-sync.service
```

The unit file works with 2.2 and newer clients. Since 3.2 it also has commented settings for a private network with an internal CA; see [Private network](/server/private-network/).

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

- **Lineage code diffs**: When Git is installed on the server, `agenticml sync` replicates shadow Git bundles, allowing the web dashboard to render source code diffs and commit histories. Runs from several machines syncing to one server get correct diffs against a parent that ran on another machine, in either sync order, once both have synced (pass `--parent` with the parent's run id).
- **By-reference artifacts**: Use `agenticml sync --artifacts` to upload files logged with `copy=False`.
- **User ownership**: Runs are owned by the account holding the sync key. Any key belonging to that account can append to the run.

## Next

Continue to [Backups and restore](/server/backups/) to safeguard server data.

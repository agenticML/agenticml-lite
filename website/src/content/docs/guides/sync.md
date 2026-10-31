---
title: Sync to AgenticML
description: Optionally upload local runs to AgenticML with login and sync.
sidebar:
  order: 3
---

Uploading is optional. Runs stay local until you sync them to [AgenticML](https://agenticml.xyz).

```bash
agenticml login
agenticml whoami
agenticml sync RUN_ID [RUN_ID ...]
agenticml sync --all
agenticml sync --all --watch 30
agenticml logout
```

## Logging in

`agenticml login` prompts for your API key, checks it, and saves it to `~/.config/agenticml/credentials.json` with mode `0600`. The default host is `https://api.agenticml.xyz`; pass `--host` or set `AGENTICML_HOST` to use another.

For CI, set `AGENTICML_API_KEY` instead of logging in.

:::caution
Avoid passing `--key` or `--api-key` on the command line. Their values are visible to other users in `ps`.
:::

## How sync works

Sync is incremental and resumable. A cursor in `runs/<id>/sync.json` records what has been sent, so an interrupted sync retries at most the batch in flight.

- `--all` selects runs that are incomplete or have changed.
- `--watch N` repeats every N seconds until every selected run is done.
- `--dry-run` prints counts without sending anything.
- `--reset` forgets the remote copy of the given runs on this host.

## What is uploaded

Project, name, config, tags, notes, group, job type, environment, git info, the parent run, scalar points, code files, media, artifacts, and the final summary and status.

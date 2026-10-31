---
title: Dashboard
description: Browse runs, charts, media, and notes with agenticml ui.
sidebar:
  order: 2
---

```bash
agenticml ui                                   # http://127.0.0.1:6006
agenticml ui --open --port 6006 --host 127.0.0.1
agenticml ui --read-only
```

`agenticml ui` binds to `127.0.0.1` on port 6006 and reads straight from the local store. `--dir` picks a different store (otherwise `AGENTICML_DIR`, then `~/.agenticml`).

## Listing runs

```bash
agenticml ls                      # newest first
agenticml ls --project demo --json
```

`ls` prints the run id, project, name, status, created time, and last step.

## Run status

A run stays `running` while its process holds the run's lock, even if it logs rarely. If the process is gone, the run shows as `crashed`. On filesystems without locking, runs untouched for more than 120 seconds show as `stale`.

## Notes

Every run page has an autosaving Markdown notepad, and you can tick checklist items in view mode. The home page has a collapsible Notes notebook that groups each run's note under its name and follows the project tab and filter.

The dashboard is read-only except for notes. `--read-only` disables note editing. Use it if you expose the dashboard on a non-loopback `--host`.

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

## Inspecting and editing runs from the CLI

In addition to `agenticml ls`, the CLI provides commands to inspect details, export data, and update finished runs.

### Inspecting runs

```bash
agenticml show RUN_ID
agenticml show RUN_ID --json
agenticml export RUN_ID -o metrics.csv
agenticml export RUN_ID --key loss --key accuracy --format json
```

`agenticml show RUN_ID` displays the run header (ID, project, name, status, task, created time, parent, and tags/group/job type/notes when present), configuration (hiding internal keys starting with `_`), summary, a metrics table (`KEY`, `LAST`, `STEP`, `POINTS`), an artifacts table (`NAME`, `TYPE`, `VERSION`, `SIZE`, `PATH`), and media item counts. Pass `--json` to output a strict JSON document; non-finite floats are output as `null`. `LAST` and `STEP` reflect the last written record (or the overview's largest step and last finite value for series pruned by `sync --prune`). Missing runs, bad IDs, or corrupt metadata print an error on stderr and exit 1.

`agenticml export RUN_ID` dumps raw metric series. Default format is CSV (`key,step,value` in write order, float32 formatted exactly, non-finite values as `nan`, `inf`, `-inf`). `--format json` outputs `{"run_id": ..., "series": {key: [[step, value], ...]}}` with non-finite values as `null`. Filter series with repeatable `--key K`. Use `-o PATH` to write to a file. Unknown keys fail before writing (exit 1). Pruned series are skipped with a warning; if all chosen series were pruned, export exits 1.

### Editing ended runs

Ended runs can be updated directly from the CLI. `edit`, `artifact add`, and `finish` refuse live runs whose lock is held (exit 1). They do not mark runs as running and do not refresh heartbeats. Changes made with `edit`, `artifact add`, and `finish` are uploaded by the next `agenticml sync`, even for runs that were already synced. Runs pruned by `sync --prune` cannot be edited.

```bash
agenticml edit RUN_ID --tag best --summary test_acc=0.913 --group sweep-3
agenticml artifact add RUN_ID model.pt --name checkpoint --copy
agenticml finish RUN_ID --status crashed
agenticml note RUN_ID --set "Validation score confirmed"
```

- `agenticml edit RUN_ID`: Updates metadata on an ended run. `--tag T` and `--untag T` are repeatable (add if absent, remove every occurrence). `--notes TEXT` updates the launch note (`""` clears it). `--group NAME` and `--job-type NAME` set the group and job type (cannot be cleared). `--config KEY=VALUE` and `--summary KEY=VALUE` merge values using the same JSON/string parsing rules as `run --config`. `--unset-summary KEY` removes a summary key. Project, name, task, parent, and run ID cannot be changed.
- `agenticml artifact add RUN_ID PATH`: Records an artifact on an ended run (`--name` defaults to file stem, `--type` defaults to `file`, `--metadata` accepts a JSON object). `--copy` stores file bytes in the object store; otherwise only path, size, and mtime are recorded. Version increments per name.
- `agenticml finish RUN_ID --status finished|crashed`: Finalizes a run left `running`, `stale`, or `crashed` (or toggles finished/crashed), writing overviews and `finished_at` while preserving summary metrics. If the run is already in that status, exits 0.
- `agenticml note RUN_ID`: Reads or updates the local Markdown notepad (`runs/<id>/notes.md`) using `--set TEXT`, `--file PATH` (`-` for stdin), or `--clear`. The notepad is local and not synced. Unlike the other commands, `note` works even while the run is live. Without flags, it prints the current note.

Note that `agenticml note` modifies the local dashboard notepad (`notes.md`). To change the run's synced launch note, use `agenticml edit --notes TEXT`.

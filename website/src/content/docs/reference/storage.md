---
title: Storage and disk
description: Where runs are stored, how big they are, and how to clean up.
sidebar:
  order: 2
---

The store root is `init(dir=...)`, else `AGENTICML_DIR`, else `~/.agenticml`.

```
<root>/
  objects/<hh>/<sha256>     content-addressed bytes, shared by every run
  runs/<run_id>/
    meta.json               project, config, status, summary, env, git
    events.jsonl            config updates, code, artifacts, finish
    notes.md                run note (Markdown, up to 256 KiB)
    sync.json               per-host upload cursor, after a sync
    scalars/<id>.bin        8 bytes per point: uint32 step, float32 value
    scalars/index.json
    media/index.json
```

Run ids look like `20261027T021500-ab12cd34` (UTC). Each scalar point takes 8 bytes. Series longer than 65,536 points also get a small overview file at finish, so charts load fast.

Code snapshots and media are stored once, keyed by SHA-256, so the same bytes logged twice share one object. Snapshot caps default to 10 MB per file and 100 MB total. Artifacts are stored as a path, size, and mtime unless you pass `copy=True`.

The full on-disk format is documented in [FORMAT.md](https://github.com/agenticML/agenticml-lite/blob/main/docs/FORMAT.md).

## Cleaning up

```bash
agenticml du
agenticml gc --dry-run
agenticml gc --older-than DAYS --keep-last N
agenticml gc --include-unsynced
agenticml sync --all --prune
```

- `agenticml du` reports how much space the store uses.
- `agenticml gc` removes runs. Only synced runs are eligible unless you pass `--include-unsynced`. `--dry-run` prints the plan without deleting.
- `agenticml sync --prune` drops raw series files that already have an overview. A pruned run cannot be resumed or synced to another host.

agenticml warns when the store grows past `AGENTICML_MAX_BYTES` (default 5 GiB).

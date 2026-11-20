---
title: Storage and disk
description: Where runs are stored, how big they are, how fast logging is, and how to clean up.
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
    sync.json               upload cursor per server and workspace, after a sync
    scalars/<id>.bin        8 bytes per point: uint32 step, float32 value
    scalars/<id>.ovw        overview, at finish, if the series has > 65536 points
    scalars/_time.bin       one record per scalar flush
    scalars/index.json
    media/index.json
```

Run ids look like `20261027T021500-ab12cd34` (UTC). Each scalar point takes 8 bytes in `<id>.bin`. A series with more than 65,536 points also gets an overview file (`<id>.ovw`) at finish, so charts load fast. An overview is at most 40 + 36 x 2048 = 73,768 bytes. Worst-case overview overhead is about 14% of the raw series (the 73,768-byte cap next to a 65,537-point `<id>.bin`); the share falls as the series grows, to about 0.6 B/pt past roughly 120,000 points. At or below 65,536 points no overview is written, and the reader builds one from `<id>.bin` on demand. For comparison, TensorBoard tfevents take about 51 B/pt, and the old JSONL journal took about 57 B/pt.

Code snapshots and media are stored once, keyed by SHA-256, so the same bytes logged twice share one object. Snapshot caps default to 10 MB per file and 100 MB total. Media indexes keep at most 64 rows per key by default and thin older rows as new media arrives (`AGENTICML_MEDIA_MAX_PER_KEY` overrides the cap). Artifacts are stored as a path, size, and mtime unless you pass `copy=True`, which hashes the bytes into `objects/`.

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
- `agenticml gc` removes runs. Only synced runs are eligible unless you pass `--include-unsynced`. `--older-than` and `--keep-last` narrow the set. `--dry-run` prints the plan without deleting. Unreferenced objects have a one-hour grace period before gc removes them.
- `agenticml sync --prune` drops raw `<id>.bin` files for series that already have an overview. Series of 65,536 points or fewer keep their `.bin` (at most 512 KB each). A pruned run cannot be resumed, synced to another host, or uploaded again after `sync --reset`.

agenticml warns when the store grows past `AGENTICML_MAX_BYTES` (default 5 GiB).

## Performance

From `python scripts/bench.py --quick` on one machine. Timings and bytes per point move with CPU, disk, and Python.

| Metric | This run | Target |
| --- | --- | --- |
| `import agenticml` (median of 5) | 39.4 ms | < 100 ms |
| `log()` x10 scalars, median (10,000 calls) | 2.70 µs | < 10 µs |
| `log()` x10 scalars, p99 | 8.91 µs | |
| disk, whole `scalars/` dir (100,000 points) | 8.01 B/pt | <= 9.0 B/pt (8 B raw + overview) |
| UI cold start, `/api/runs` (101 runs) | 8 ms | < 1 s |
| scalar request, 100,000-point series (median of 20) | 14.16 ms | < 20 ms |
| TensorBoard tfevents | not installed | |

The disk row is ten series of 10,000 points. Each series is under 65,536 points, so finish writes no overview and the directory stays at 8.01 B/pt, inside the 9.0 B/pt check. A series past 65,536 points adds one overview of at most 73,768 bytes. `python scripts/bench.py` without `--quick` uses 100,000 log calls, a 1,000,000-point run, and 1,000 small runs.

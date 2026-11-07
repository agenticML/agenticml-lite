---
title: Backups and restore
description: Backup and restore server run data, objects, and key state.
sidebar:
  order: 5
---

The server stores all state in a single directory specified by `--data` (falls back to `AGENTICML_SERVER_DATA`, then `~/.agenticml-server`).

## Data directory structure

```
<data>/
├── store/                 # Standard v1 store layout
│   ├── runs/              # Run directories containing metrics and metadata
│   │   └── <run_id>/
│   │       ├── meta.json
│   │       ├── events.jsonl
│   │       ├── scalars/
│   │       ├── media/
│   │       └── remote.json
│   └── objects/           # Content-addressed blobs (code snapshots, media, copied artifacts)
└── server/                # Server operational state
    ├── keys.json          # Key metadata and SHA-256 hashes (permissions 0600)
    ├── keys.lock          # File lock for atomic key modifications
    ├── server.lock        # Exclusive single-writer lock
    └── tmp/               # Upload staging directory (must reside on same filesystem as store/)
```

Because `server/tmp/` and `store/` must perform atomic file renames during ingest, they must reside on the same filesystem mount.

## Backup methods

To make a consistent offline backup:

1. Stop the server service cleanly to release `server.lock` and flush all writes:
   ```bash
   sudo systemctl stop agenticml-server
   ```
2. Archive the data directory:
   ```bash
   sudo tar czf agenticml-backup.tgz -C /var/lib/agenticml-server .
   ```
3. Restart the server service:
   ```bash
   sudo systemctl start agenticml-server
   ```

## Restoring a backup

To restore from a backup:

1. Stop the server service:
   ```bash
   sudo systemctl stop agenticml-server
   ```
2. Extract the backup files into the data directory:
   ```bash
   sudo tar xzf agenticml-backup.tgz -C /var/lib/agenticml-server
   ```
3. Ensure the service user owns the restored files:
   ```bash
   sudo chown -R agenticml:agenticml /var/lib/agenticml-server
   ```
4. Start the server service:
   ```bash
   sudo systemctl start agenticml-server
   ```

## Next

Continue to [Upgrades and troubleshooting](/agenticml-lite/server/upgrade/) for maintenance procedures.

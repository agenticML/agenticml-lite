---
title: Backups and restore
description: Backup and restore server run data, objects, and key state.
sidebar:
  order: 5
---

The server stores all state in a single directory specified by `--data` (default `/data` in the container).

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

### Local or Docker deployment

To make a consistent offline backup:

1. Stop the server container to release `server.lock` and flush all writes:
   ```bash
   docker stop --time 30 agenticml-server
   ```
2. Archive the data directory:
   ```bash
   tar -czf agenticml-backup-$(date +%Y%m%d).tar.gz -C /path/to/data .
   ```
3. Restart the server:
   ```bash
   docker start agenticml-server
   ```

### AWS ECS with EFS

When deployed on AWS ECS using EFS, configure automated backups using AWS Backup. The provided Terraform example sets up daily snapshots of the EFS volume with a 35-day retention period.

## Restoring a backup

To restore from a backup:

1. Stop the server process or scale down the ECS service to 0 tasks:
   ```bash
   docker stop agenticml-server
   ```
2. Replace or extract the backup files into `<data>`.
3. If using host bind mounts, ensure UID and GID 10001 own the restored files:
   ```bash
   sudo chown -R 10001:10001 /path/to/data
   ```
4. Start the server container:
   ```bash
   docker start agenticml-server
   ```

## Next

Continue to [Upgrades and troubleshooting](/agenticml-lite/server/upgrade/) for maintenance procedures.

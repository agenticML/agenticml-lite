---
title: Backups and restore
description: Backup and restore server databases, run files, objects, and lineage.
sidebar:
  order: 5
---

In version 3.0, the server stores database state in SQL (SQLite or PostgreSQL) and experiment files on disk. The server provides an online backup tool that captures consistent snapshots while the server continues running.

## Data directory structure

```
<data>/
├── workspaces/
│   └── <workspace_id>/
│       ├── store/
│       │   ├── runs/
│       │   │   └── <run_id>/
│       │   │       ├── meta.json
│       │   │       ├── events.jsonl
│       │   │       ├── scalars/
│       │   │       ├── media/
│       │   │       └── artifacts.json
│       │   └── objects/       # Local blobs (when --objects file://)
│       └── lineage/           # Shadow Git repositories
└── server/
    ├── agenticml.db           # SQLite database (permissions 0600)
    ├── server.lock            # Single-process lock (SQLite only)
    └── tmp/                   # Ingest staging directory
```

When using Amazon S3 (`--objects s3://...`), content-addressed blobs live in the bucket under `{workspace_id}/objects/{hh}/{sha256}`.

## Consistent online backup: `server backup`

Run `agenticml server backup` while the server is live:

```bash
agenticml server backup --to /backups/20261109-0000
```

### Consistency rule

1. **Database snapshot first**:
   - **SQLite**: The command uses the SQLite Online Backup API to create a consistent point-in-time snapshot of `agenticml.db`.
   - **PostgreSQL**: Take a database dump first using `pg_dump`, then pass `--files-only`:
     ```bash
     pg_dump --format=custom "$DATABASE_URL" > /backups/20261109-0000/server.dump
     agenticml server backup --to /backups/20261109-0000 --files-only
     ```
2. **Committed run files second**: The backup tool copies each run file only up to the committed size recorded in the database snapshot (`run_files` table). Any trailing bytes appended after the snapshot are excluded, guaranteeing file consistency with the database.
3. **Objects and lineage**: Content-addressed objects on local disk and shadow Git lineage repositories are copied.
4. **S3 objects**: When using S3, blobs are not copied. S3 objects are protected via bucket versioning and AWS Backup.
5. **Manifest**: A summary manifest with file and object counts is written to `BACKUP.json`.

## Restoring a backup

### Restoring SQLite

1. Point `agenticml server serve` directly to the backup directory:
   ```bash
   agenticml server serve --data /backups/20261109-0000
   ```
2. Or copy the backup directory contents to your standard data directory and ensure proper file ownership:
   ```bash
   sudo systemctl stop agenticml-server
   sudo cp -a /backups/20261109-0000/* /var/lib/agenticml-server/
   sudo chown -R agenticml:agenticml /var/lib/agenticml-server
   sudo systemctl start agenticml-server
   ```
3. Run `agenticml server verify --data /var/lib/agenticml-server` to verify data integrity.

### Restoring PostgreSQL

1. Restore the PostgreSQL dump into an empty database:
   ```bash
   pg_restore --clean --if-exists -d "$DATABASE_URL" /backups/20261109-0000/server.dump
   ```
2. Start the server pointing to the restored data directory and database:
   ```bash
   agenticml server serve --data /backups/20261109-0000 --database "$DATABASE_URL"
   ```

## Next

Continue to [Upgrades and troubleshooting](/agenticml-lite/server/upgrade/) for maintenance and version migration procedures.

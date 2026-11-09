"""Workspace plans and exact storage accounting (no enforcement)."""

SCHEMA = '''
ALTER TABLE workspaces ADD COLUMN plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free','custom'));
ALTER TABLE workspaces ADD COLUMN custom_limits TEXT;
ALTER TABLE workspaces ADD COLUMN personal INTEGER NOT NULL DEFAULT 0;
CREATE TABLE objects (
 workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 sha256 TEXT NOT NULL, size BIGINT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY (workspace_id, sha256)
);
CREATE TABLE workspace_usage (
 workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
 stored_bytes BIGINT NOT NULL DEFAULT 0, upload_day TEXT,
 upload_bytes BIGINT NOT NULL DEFAULT 0, request_minute BIGINT,
 request_count INTEGER NOT NULL DEFAULT 0
);
INSERT INTO workspace_usage (workspace_id, stored_bytes)
 SELECT w.id, COALESCE((SELECT SUM(f.size) FROM run_files f WHERE f.workspace_id=w.id), 0)
 FROM workspaces w;
'''


def upgrade(tx, dialect):
    for statement in dialect.migration(SCHEMA).split(';'):
        if statement.strip():
            tx.execute(statement)

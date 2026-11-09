"""One summary row per run and scalar series."""

SCHEMA = '''
CREATE TABLE run_metrics (
  workspace_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  series_id INTEGER NOT NULL,
  name TEXT,
  last_value {{real}},
  min_value {{real}},
  max_value {{real}},
  count BIGINT NOT NULL,
  last_step BIGINT,
  decoded_bytes BIGINT NOT NULL,
  PRIMARY KEY (workspace_id, run_id, series_id),
  FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, run_id) ON DELETE CASCADE
);
CREATE INDEX run_metrics_name ON run_metrics (workspace_id, name);
'''


def upgrade(tx, dialect):
    for statement in dialect.migration(SCHEMA).split(';'):
        if statement.strip():
            tx.execute(statement)
    dialect.enable_workspace_rls(tx, 'run_metrics')

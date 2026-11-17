CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  project TEXT,
  created_at TEXT,
  owner_user TEXT,
  owner_key TEXT,
  status TEXT CHECK (status IS NULL OR status IN ('running','finished','crashed')),
  fingerprint TEXT,
  last_sync_at TEXT,
  last_sync_key TEXT,
  list_row TEXT NOT NULL,
  last_step INTEGER,
  launch_note TEXT,
  commit_sizes TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX runs_created ON runs(created_at, run_id);
CREATE INDEX runs_project ON runs(project);
CREATE INDEX runs_owner ON runs(owner_user);

CREATE TABLE run_files (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  size BIGINT NOT NULL,
  sha256 TEXT,
  PRIMARY KEY (run_id, path)
);

CREATE TABLE run_notes (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

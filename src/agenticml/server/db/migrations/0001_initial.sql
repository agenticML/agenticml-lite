CREATE TABLE server_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK (role IN ('viewer','member','admin')),
  identity TEXT,
  identity_folded TEXT UNIQUE,
  password TEXT,
  invite_sha256 TEXT UNIQUE,
  invite_expires_at TEXT,
  generation INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL
);
CREATE TABLE keys (
  name TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('sync','admin')),
  sha256 TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  last_used_at TEXT,
  user_name TEXT NOT NULL REFERENCES users(name) ON DELETE CASCADE
);
CREATE INDEX keys_user ON keys(user_name);
CREATE TABLE sessions (
  token_sha256 TEXT PRIMARY KEY,
  user_name TEXT NOT NULL,
  user_id TEXT,
  generation INTEGER NOT NULL,
  via TEXT NOT NULL,
  key_name TEXT,
  key_sha256 TEXT,
  created {{real}} NOT NULL,
  last_seen {{real}} NOT NULL,
  created_order INTEGER NOT NULL
);
CREATE INDEX sessions_last_seen ON sessions(last_seen);
CREATE INDEX sessions_key ON sessions(key_name, key_sha256);
CREATE TABLE login_attempts (
  bucket TEXT PRIMARY KEY,
  failures INTEGER NOT NULL,
  last_failure {{real}} NOT NULL
);
CREATE TABLE audit_events (
  id {{id}},
  ts TEXT NOT NULL,
  actor TEXT, via TEXT, action TEXT, target TEXT, client TEXT,
  detail TEXT
);
CREATE INDEX audit_events_ts ON audit_events(ts, id);

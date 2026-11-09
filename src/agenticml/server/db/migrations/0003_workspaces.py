"""Convert the single-team schema atomically, retaining account identities."""
import secrets

from agenticml.store import layout

SCHEMA = '''
CREATE TABLE accounts (
 id TEXT PRIMARY KEY, login TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
 identity TEXT, identity_folded TEXT UNIQUE, password TEXT,
 server_admin INTEGER NOT NULL DEFAULT 0 CHECK (server_admin IN (0,1)),
 generation INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, created_by TEXT NOT NULL
);
CREATE TABLE workspaces (
 id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 status TEXT NOT NULL CHECK (status IN ('active','suspended')),
 created_by TEXT REFERENCES accounts(id), created_at TEXT NOT NULL
);
CREATE TABLE memberships (
 workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 account_id TEXT NOT NULL REFERENCES accounts(id),
 role TEXT NOT NULL CHECK (role IN ('owner','admin','member','viewer')),
 PRIMARY KEY (workspace_id, account_id)
);
CREATE UNIQUE INDEX memberships_owner ON memberships(workspace_id) WHERE role='owner';
CREATE TABLE invites (
 workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 login TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin','member','viewer')),
 sha256 TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, invited_by TEXT NOT NULL,
 PRIMARY KEY (workspace_id, login)
);
CREATE TABLE keys (
 workspace_id TEXT NOT NULL, name TEXT NOT NULL, account_id TEXT NOT NULL,
 scope TEXT NOT NULL CHECK (scope IN ('sync','admin')), sha256 TEXT NOT NULL UNIQUE,
 prefix TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, last_used_at TEXT,
 PRIMARY KEY (workspace_id, name),
 FOREIGN KEY (workspace_id, account_id) REFERENCES memberships(workspace_id, account_id) ON DELETE CASCADE
);
CREATE INDEX keys_account ON keys(workspace_id, account_id);
CREATE TABLE runs (
 workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 run_id TEXT NOT NULL, project TEXT, created_at TEXT, owner_account_id TEXT REFERENCES accounts(id),
 owner_key TEXT, status TEXT CHECK (status IS NULL OR status IN ('running','finished','crashed')),
 fingerprint TEXT, last_sync_at TEXT, last_sync_key TEXT, list_row TEXT NOT NULL,
 last_step BIGINT, launch_note TEXT, commit_sizes TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY (workspace_id, run_id)
);
CREATE INDEX runs_created ON runs(workspace_id, created_at, run_id);
CREATE INDEX runs_project ON runs(workspace_id, project);
CREATE INDEX runs_owner ON runs(workspace_id, owner_account_id);
CREATE TABLE run_files (
 workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, path TEXT NOT NULL, size BIGINT NOT NULL, sha256 TEXT,
 PRIMARY KEY (workspace_id, run_id, path),
 FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, run_id) ON DELETE CASCADE
);
CREATE TABLE run_notes (
 workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY (workspace_id, run_id),
 FOREIGN KEY (workspace_id, run_id) REFERENCES runs(workspace_id, run_id) ON DELETE CASCADE
);
CREATE TABLE audit_events (
 id {{id}}, workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
 ts TEXT NOT NULL, actor TEXT, via TEXT, action TEXT, target TEXT, client TEXT, detail TEXT
);
CREATE INDEX audit_events_ts ON audit_events(workspace_id, ts, id);
CREATE TABLE sessions (
 token_sha256 TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(id),
 workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
 generation INTEGER NOT NULL, via TEXT NOT NULL, key_name TEXT, key_sha256 TEXT,
 created {{real}} NOT NULL, last_seen {{real}} NOT NULL, created_order INTEGER NOT NULL
);
CREATE INDEX sessions_last_seen ON sessions(last_seen);
CREATE INDEX sessions_key ON sessions(workspace_id, key_name, key_sha256);
'''


def upgrade(tx, dialect):
    old = {name: [dict(r) for r in tx.fetchall('SELECT * FROM ' + name)]
           for name in ('users', 'keys', 'runs', 'run_files', 'run_notes', 'audit_events', 'sessions')}
    for name in ('keys', 'run_files', 'run_notes', 'runs', 'audit_events', 'sessions', 'users'):
        tx.execute('DROP TABLE ' + name)
    for statement in dialect.migration(SCHEMA).split(';'):
        if statement.strip():
            tx.execute(statement)
    wid = secrets.token_hex(16)
    users = old['users']
    admins = sorted((u for u in users if u['role'] == 'admin'), key=lambda u: (u['created_at'], u['name']))
    owner = admins[0]['id'] if admins else None
    accounts = {}
    for user in users:
        accounts[user['name']] = user['id']
        tx.execute('INSERT INTO accounts (id, login, display_name, identity, identity_folded, password, server_admin, generation, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   (user['id'], user['name'], user['name'], user['identity'], user['identity_folded'],
                    user['password'], int(user['id'] == owner), user['generation'],
                    user['created_at'], user['created_by']))
    tx.execute('INSERT INTO workspaces (id, slug, name, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)',
               (wid, 'default', 'Default', 'active', owner, layout.format_timestamp()))
    for user in users:
        role = 'owner' if user['id'] == owner else user['role']
        tx.execute('INSERT INTO memberships (workspace_id, account_id, role) VALUES (?, ?, ?)', (wid, user['id'], role))
        if user['invite_sha256'] is not None:
            tx.execute('INSERT INTO invites (workspace_id, login, role, sha256, expires_at, invited_by) VALUES (?, ?, ?, ?, ?, ?)',
                       (wid, user['name'], 'admin' if role == 'owner' else role, user['invite_sha256'], user['invite_expires_at'], user['created_by']))
    for key in old['keys']:
        tx.execute('INSERT INTO keys (workspace_id, name, account_id, scope, sha256, prefix, created_at, created_by, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   (wid, key['name'], accounts[key['user_name']], key['scope'], key['sha256'],
                    key['prefix'], key['created_at'], key['created_by'], key['last_used_at']))
    for run in old['runs']:
        login = run.pop('owner_user')
        if login is not None and login not in accounts:
            aid = secrets.token_hex(16)
            accounts[login] = aid
            tx.execute('INSERT INTO accounts (id, login, display_name, created_at, created_by) VALUES (?, ?, ?, ?, ?)',
                       (aid, login, login, layout.format_timestamp(), 'legacy run owner'))
        run['owner_account_id'] = accounts.get(login)
        columns = tuple(run)
        tx.execute('INSERT INTO runs (workspace_id, ' + ', '.join(columns) + ') VALUES (' +
                   ', '.join('?' for _ in range(len(columns) + 1)) + ')', (wid, *run.values()))
    for name in ('run_files', 'run_notes', 'audit_events'):
        for row in old[name]:
            tx.execute('INSERT INTO ' + name + ' (workspace_id, ' + ', '.join(row) + ') VALUES (' +
                       ', '.join('?' for _ in range(len(row) + 1)) + ')', (wid, *row.values()))
    if dialect.name == 'postgres':
        tx.execute("SELECT setval(pg_get_serial_sequence('audit_events','id'), "
                   "COALESCE(MAX(id),1)) FROM audit_events")
    for row in old['sessions']:
        aid = accounts.get(row['user_name'])
        if row['user_id'] != aid:
            aid = None
        # Env sessions have no persisted account. Stale sessions remain invalid.
        tx.execute('INSERT INTO sessions (token_sha256, account_id, workspace_id, generation, via, key_name, key_sha256, created, last_seen, created_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   (row['token_sha256'], aid, wid if row['via'] == 'key' else None,
                    row['generation'], row['via'], row['key_name'], row['key_sha256'],
                    row['created'], row['last_seen'], row['created_order']))

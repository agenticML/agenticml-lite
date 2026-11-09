"""Workspace-bound usage accounting; all counter SQL lives here."""
from datetime import datetime, timezone

from agenticml.store import layout
from .workspaces import WorkspaceBound, require_workspace_id


def utc_day():
    return datetime.now(timezone.utc).date().isoformat()


class UsageRepo(WorkspaceBound):
    def __init__(self, db, workspace_id, *, clock=None):
        self.clock = clock
        self.db = db
        self.workspace_id = require_workspace_id(workspace_id)

    def lock(self, tx):
        # Writers take this before changing files/objects. Recount takes only
        # this lock, so it cannot observe a counter between a row and its delta.
        with self._bind(tx):
            tx.advisory_lock('usage:' + self.workspace_id)
            tx.execute('INSERT INTO workspace_usage (workspace_id) VALUES (?) '
                       'ON CONFLICT (workspace_id) DO NOTHING', (self.workspace_id,))

    def get(self, tx=None):
        if tx is None:
            with self._tx() as tx:
                return self.get(tx)
        with self._bind(tx):
            row = tx.fetchone('SELECT * FROM workspace_usage WHERE workspace_id=?',
                              (self.workspace_id,))
            return dict(row) if row is not None else {
                'workspace_id': self.workspace_id, 'stored_bytes': 0,
                'upload_day': None, 'upload_bytes': 0,
                'request_minute': None, 'request_count': 0}

    def add_stored(self, delta, tx):
        with self._bind(tx):
            self.lock(tx)
            tx.execute('UPDATE workspace_usage SET stored_bytes=stored_bytes+? WHERE workspace_id=?',
                       (delta, self.workspace_id))

    def record_object(self, sha, size, tx):
        with self._bind(tx):
            self.lock(tx)
            result = tx.execute(
                'INSERT INTO objects (workspace_id, sha256, size, created_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT (workspace_id, sha256) DO NOTHING',
                (self.workspace_id, sha, size, layout.format_timestamp()))
            if result.rowcount:
                self.add_stored(size, tx)
                return True
            return False

    def add_upload(self, nbytes, tx):
        with self._bind(tx):
            self.lock(tx)
            # Read the clock after acquiring the lock, including on midnight rollover.
            day = (self.clock or utc_day)()
            if not isinstance(day, str) or datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day:
                raise ValueError('upload bytes require a UTC day')
            tx.execute('UPDATE workspace_usage SET upload_bytes=CASE '
                       'WHEN upload_day IS NULL OR upload_day<? THEN ? '
                       'ELSE upload_bytes+? END, upload_day=CASE '
                       'WHEN upload_day IS NULL OR upload_day<? THEN ? ELSE upload_day END '
                       'WHERE workspace_id=?',
                       (day, nbytes, nbytes, day, day, self.workspace_id))

    def recount(self, tx=None):
        if tx is None:
            with self._tx(write=True) as tx:
                return self.recount(tx)
        with self._bind(tx):
            self.lock(tx)
            size = int(tx.fetchone(
                'SELECT COALESCE((SELECT SUM(size) FROM run_files WHERE workspace_id=?), 0) + '
                'COALESCE((SELECT SUM(size) FROM objects WHERE workspace_id=?), 0)',
                (self.workspace_id, self.workspace_id))[0])
            tx.execute('UPDATE workspace_usage SET stored_bytes=? WHERE workspace_id=?',
                       (size, self.workspace_id))
            return size

    def has_object(self, sha):
        with self._tx() as tx:
            return tx.fetchone('SELECT 1 FROM objects WHERE workspace_id=? AND sha256=?',
                               (self.workspace_id, sha)) is not None

    def people(self, tx=None, *, now=None):
        if tx is None:
            with self._tx() as tx:
                return self.people(tx, now=now)
        with self._bind(tx):
            stamp = layout.format_timestamp(now or datetime.now(timezone.utc))
            return int(tx.fetchone(
                'SELECT (SELECT COUNT(*) FROM memberships WHERE workspace_id=?) + '
                '(SELECT COUNT(*) FROM invites i LEFT JOIN accounts a ON a.login=i.login '
                'LEFT JOIN memberships m ON m.account_id=a.id AND m.workspace_id=i.workspace_id '
                'WHERE i.workspace_id=? AND i.expires_at>? AND m.account_id IS NULL)',
                (self.workspace_id, self.workspace_id, stamp))[0])

    def count_request(self, minute):
        import sqlite3
        sql = ('INSERT INTO workspace_usage (workspace_id, request_minute, request_count) '
               'VALUES (?, ?, 1) ON CONFLICT (workspace_id) DO UPDATE SET '
               'request_count=CASE WHEN workspace_usage.request_minute=excluded.request_minute '
               'THEN workspace_usage.request_count+1 ELSE 1 END, '
               'request_minute=excluded.request_minute')
        with self._tx(write=True) as tx:
            if self.db.dialect.name != 'sqlite' or sqlite3.sqlite_version_info >= (3, 35, 0):
                return int(tx.fetchone(sql + ' RETURNING request_count', (self.workspace_id, minute))[0])
            tx.execute(sql, (self.workspace_id, minute))
            return int(tx.fetchone('SELECT request_count FROM workspace_usage WHERE workspace_id=?',
                                   (self.workspace_id,))[0])

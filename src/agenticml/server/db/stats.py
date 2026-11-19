"""Server-wide totals and their daily history; aggregates only, no personal data."""
from datetime import datetime, timedelta, timezone

from agenticml.store import layout
from .usage import database_now

ACTIVE_RESOLUTION = 3600  # seconds; last_active_at moves at most this often
COLUMNS = ('day', 'taken_at', 'accounts', 'active_1d', 'active_7d', 'active_30d',
           'workspaces_active', 'workspaces_suspended', 'workspaces_archived',
           'workspaces_custom', 'stored_bytes', 'upload_bytes', 'runs')


def mark_account_active(tx, account_id, now):
    """now: epoch seconds. No-op for None (env key) accounts."""
    if account_id is None:
        return
    stamp = layout.format_timestamp(datetime.fromtimestamp(now, timezone.utc))
    cutoff = layout.format_timestamp(datetime.fromtimestamp(now - ACTIVE_RESOLUTION, timezone.utc))
    if tx.dialect.name == 'postgres':
        # best effort: never wait on an account row another transaction holds (no lock-order deadlocks,
        # no sign-in stalls); the next touch marks it
        tx.execute('UPDATE accounts SET last_active_at=? WHERE id=(SELECT id FROM accounts WHERE id=? '
                   'AND (last_active_at IS NULL OR last_active_at < ?) FOR NO KEY UPDATE SKIP LOCKED)',
                   (stamp, account_id, cutoff))
    else:
        tx.execute('UPDATE accounts SET last_active_at=? WHERE id=? '
                   'AND (last_active_at IS NULL OR last_active_at < ?)', (stamp, account_id, cutoff))


class StatsRepo:
    def __init__(self, db):
        self.db = db

    def _totals(self, tx, now):
        def cutoff(days):
            return layout.format_timestamp(now - timedelta(days=days))

        def count(sql, *params):
            return int(tx.fetchone(sql, params)[0])

        return {
            'as_of': layout.format_timestamp(now),
            'accounts': count('SELECT COUNT(*) FROM accounts'),
            'new_accounts_1d': count('SELECT COUNT(*) FROM accounts WHERE created_at >= ?', cutoff(1)),
            'new_accounts_7d': count('SELECT COUNT(*) FROM accounts WHERE created_at >= ?', cutoff(7)),
            'active_1d': count('SELECT COUNT(*) FROM accounts WHERE last_active_at >= ?', cutoff(1)),
            'active_7d': count('SELECT COUNT(*) FROM accounts WHERE last_active_at >= ?', cutoff(7)),
            'active_30d': count('SELECT COUNT(*) FROM accounts WHERE last_active_at >= ?', cutoff(30)),
            'workspaces_active': count("SELECT COUNT(*) FROM workspaces WHERE status='active'"),
            'workspaces_suspended': count("SELECT COUNT(*) FROM workspaces "
                                          "WHERE status='suspended' AND archived_at IS NULL"),
            'workspaces_archived': count('SELECT COUNT(*) FROM workspaces WHERE archived_at IS NOT NULL'),
            'workspaces_custom': count("SELECT COUNT(*) FROM workspaces "
                                       "WHERE plan='custom' AND archived_at IS NULL"),
            'stored_bytes': count('SELECT COALESCE(SUM(stored_bytes), 0) FROM workspace_usage'),
            'upload_bytes_today': count('SELECT COALESCE(SUM(upload_bytes), 0) FROM workspace_usage '
                                        'WHERE upload_day=?', now.date().isoformat()),
            'runs': count('SELECT COUNT(*) FROM runs'),
        }

    def totals(self, *, now=None, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.totals(now=now, tx=tx)
        tx.cross_workspace('server stats')
        return self._totals(tx, now or database_now(self.db, tx))

    def top_workspaces(self, limit=5, *, tx=None):
        if tx is None:
            with self.db.transaction() as tx:
                return self.top_workspaces(limit, tx=tx)
        tx.cross_workspace('server stats')
        return [{'slug': row['slug'], 'name': row['name'], 'stored_bytes': int(row['stored_bytes']),
                 'archived': row['archived_at'] is not None}
                for row in tx.fetchall(
                    'SELECT w.slug, w.name, w.archived_at, COALESCE(u.stored_bytes, 0) AS stored_bytes '
                    'FROM workspaces w LEFT JOIN workspace_usage u ON u.workspace_id=w.id '
                    'ORDER BY COALESCE(u.stored_bytes, 0) DESC, w.slug LIMIT ?', (limit,))]

    def snapshot(self, *, now=None, min_age=None):
        """Upsert today's row and return it; None when ``min_age`` seconds have not passed since the last one."""
        with self.db.transaction(write=True) as tx:
            tx.advisory_lock('stats_daily')
            tx.cross_workspace('server stats')
            now = now or database_now(self.db, tx)
            day = now.date().isoformat()
            if min_age is not None:
                row = tx.fetchone('SELECT taken_at FROM stats_daily WHERE day=?', (day,))
                if row is not None and row[0] > layout.format_timestamp(now - timedelta(seconds=min_age)):
                    return None
            totals = self._totals(tx, now)
            values = (day, totals['as_of'], totals['accounts'], totals['active_1d'],
                      totals['active_7d'], totals['active_30d'], totals['workspaces_active'],
                      totals['workspaces_suspended'], totals['workspaces_archived'],
                      totals['workspaces_custom'], totals['stored_bytes'],
                      totals['upload_bytes_today'], totals['runs'])
            tx.execute(self.db.dialect.upsert('stats_daily', COLUMNS, ('day',), COLUMNS[1:]), values)
            return dict(zip(COLUMNS, values))

    def history(self, days=90, *, now=None):
        """At most ``days`` rows, the last one being today."""
        if days < 1:
            raise ValueError('days must be at least 1')
        with self.db.transaction() as tx:
            now = now or database_now(self.db, tx)
            since = (now - timedelta(days=days - 1)).date().isoformat()
            return [dict(zip(COLUMNS, row)) for row in tx.fetchall(
                'SELECT ' + ', '.join(COLUMNS) + ' FROM stats_daily WHERE day >= ? ORDER BY day',
                (since,))]

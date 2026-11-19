"""Account last activity and one row of server-wide totals per UTC day."""
from datetime import datetime, timezone

from agenticml.store import layout

CHUNK = 500

SCHEMA = '''
ALTER TABLE accounts ADD COLUMN last_active_at TEXT;
CREATE INDEX accounts_last_active ON accounts(last_active_at);
CREATE TABLE stats_daily (
 day TEXT PRIMARY KEY, taken_at TEXT NOT NULL,
 accounts BIGINT NOT NULL,
 active_1d BIGINT NOT NULL, active_7d BIGINT NOT NULL, active_30d BIGINT NOT NULL,
 workspaces_active BIGINT NOT NULL, workspaces_suspended BIGINT NOT NULL,
 workspaces_archived BIGINT NOT NULL, workspaces_custom BIGINT NOT NULL,
 stored_bytes BIGINT NOT NULL, upload_bytes BIGINT NOT NULL,
 runs BIGINT NOT NULL
);
'''


def upgrade(tx, dialect):
    for statement in dialect.migration(SCHEMA).split(';'):
        if statement.strip():
            tx.execute(statement)
    latest = {}
    for account_id, stamp in tx.fetchall(
            'SELECT account_id, MAX(last_used_at) FROM keys '
            'WHERE last_used_at IS NOT NULL AND account_id IS NOT NULL GROUP BY account_id'):
        latest[account_id] = stamp
    for account_id, seen in tx.fetchall(
            'SELECT account_id, MAX(last_seen) FROM sessions WHERE account_id IS NOT NULL '
            'GROUP BY account_id'):
        stamp = layout.format_timestamp(datetime.fromtimestamp(seen, timezone.utc))
        if account_id not in latest or stamp > latest[account_id]:
            latest[account_id] = stamp
    # chunked, so the table lock from ALTER TABLE is held for few statements
    items = list(latest.items())
    for start in range(0, len(items), CHUNK):
        chunk = items[start:start + CHUNK]
        cases = ' '.join('WHEN ? THEN ?' for _ in chunk)
        marks = ', '.join('?' for _ in chunk)
        tx.execute(f'UPDATE accounts SET last_active_at = CASE id {cases} END WHERE id IN ({marks})',
                   tuple(value for item in chunk for value in item) + tuple(a for a, _ in chunk))

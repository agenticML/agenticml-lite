"""Apply numbered, packaged SQL migrations atomically and in order."""
from pathlib import Path
import re

from agenticml.store import layout
from .errors import SchemaTooNew

MIGRATIONS = Path(__file__).with_name('migrations')


def migrate(db):
    files = sorted(MIGRATIONS.glob('[0-9][0-9][0-9][0-9]_*.sql'))
    newest = max((int(path.name[:4]) for path in files), default=0)
    scripts = [(int(path.name[:4]), path.stem[5:], db.dialect.migration(path.read_text()))
               for path in files]
    with db.transaction(write=True) as tx:
        tx.execute('CREATE TABLE IF NOT EXISTS schema_migrations '
                   '(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)')
        version = tx.fetchone('SELECT MAX(version) FROM schema_migrations')[0] or 0
        if version > newest:
            raise SchemaTooNew(f'database schema version {version} is newer than this '
                               f'agenticml-lite (knows up to {newest}); upgrade agenticml-lite')
        for version, name, sql in scripts:
            if tx.fetchone('SELECT 1 FROM schema_migrations WHERE version=?', (version,)):
                continue
            for statement in re.split(r';\s*(?:\n|$)', sql):
                if statement.strip():
                    tx.execute(statement)
            tx.execute('INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)',
                       (version, name, layout.format_timestamp()))

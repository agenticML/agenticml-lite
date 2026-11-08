"""Apply numbered SQL and Python migrations atomically and in order."""
from pathlib import Path
import re
import runpy

from agenticml.store import layout
from .errors import SchemaTooNew

MIGRATIONS = Path(__file__).with_name('migrations')


def migrate(db):
    files = sorted(p for p in MIGRATIONS.glob('[0-9][0-9][0-9][0-9]_*')
                   if p.suffix in ('.sql', '.py'))
    versions = [int(p.name[:4]) for p in files]
    if len(versions) != len(set(versions)):
        raise RuntimeError('duplicate migration version')
    newest = max((int(path.name[:4]) for path in files), default=0)
    scripts = [(int(path.name[:4]), path.stem[5:], path) for path in files]
    with db.transaction(write=True) as tx:
        tx.advisory_lock('migrate')
        tx.execute('CREATE TABLE IF NOT EXISTS schema_migrations '
                   '(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)')
        version = tx.fetchone('SELECT MAX(version) FROM schema_migrations')[0] or 0
        if version > newest:
            raise SchemaTooNew(f'database schema version {version} is newer than this '
                               f'agenticml-lite (knows up to {newest}); upgrade agenticml-lite')
        for version, name, path in scripts:
            if tx.fetchone('SELECT 1 FROM schema_migrations WHERE version=?', (version,)):
                continue
            if path.suffix == '.py':
                runpy.run_path(str(path))['upgrade'](tx, db.dialect)
            else:
                sql = db.dialect.migration(path.read_text())
                for statement in re.split(r';\s*(?:\n|$)', sql):
                    if statement.strip():
                        tx.execute(statement)
            tx.execute('INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)',
                       (version, name, layout.format_timestamp()))

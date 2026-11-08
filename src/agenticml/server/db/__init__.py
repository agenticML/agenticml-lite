"""Database configuration and shared pools for server facades."""
import os
from pathlib import Path
import threading
import weakref

from .errors import DatabaseBusy, DatabaseError, DatabaseUrlError, SchemaTooNew
from .sqlite import SqliteDatabase
from .postgres import PostgresDatabase

_registry = weakref.WeakValueDictionary()
_registry_lock = threading.RLock()


def default_url(data_dir):
    return 'sqlite:///' + str(Path(data_dir).absolute() / 'server' / 'agenticml.db')


def resolve_url(data_dir, url=None):
    return url if url is not None else (os.environ.get('AGENTICML_SERVER_DATABASE_URL')
                                       or default_url(data_dir))


def _path(url):
    if not isinstance(url, str) or not url.startswith('sqlite:///'):
        raise DatabaseUrlError('unsupported database URL scheme; use sqlite:/// or postgresql:// (postgres://)')
    path = Path(url[len('sqlite:///'):])
    if not path.is_absolute():
        raise DatabaseUrlError('SQLite database path must be absolute')
    # Normalize parent aliases without resolving a planted file symlink.
    return path.parent.resolve() / path.name


def _is_postgres(url):
    return isinstance(url, str) and url.startswith(('postgresql://', 'postgres://'))


def connect(url):
    if _is_postgres(url):
        return PostgresDatabase(url)
    return SqliteDatabase(_path(url))


def for_server_dir(server_dir, url=None, *, create=True, actor='server'):
    directory = Path(server_dir)
    resolved = url if url is not None else (os.environ.get('AGENTICML_SERVER_DATABASE_URL')
                                               or 'sqlite:///' + str(directory.absolute() / 'agenticml.db'))
    path = None if _is_postgres(resolved) else _path(resolved)
    key = resolved if path is None else str(path)
    with _registry_lock:
        db = _registry.get(key)
        if not create and path is None and (db is None or db.closed) and not PostgresDatabase.exists(resolved):
            return None
        if not create and path is not None:
            try:
                path.lstat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise DatabaseError('could not inspect SQLite database') from exc
        db = _registry.get(key)
        if db is None or db.closed:
            db = connect(resolved)
            try:
                from .import2x import import_state
                import_state(db, directory, actor=actor)
            except BaseException:
                db.close()
                raise
            _registry[key] = db
        if create:
            from .backfill import backfill_objects
            backfill_objects(db, directory.parent)
        return db

"""Postgres transactions and a bounded idle connection pool."""
from contextlib import contextmanager
import logging
import threading
import time

from .dialect import PostgresDialect
from .errors import DatabaseBusy, DatabaseError
from .sqlite import Tx

logger = logging.getLogger(__name__)
_DEFAULT_CONNECT_TIMEOUT = '10'


def _conninfo_with_timeout(url):
    """Add connect_timeout=10 unless the URL already sets one. Never logs the URL."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(url)
    if 'connect_timeout' not in params:
        params['connect_timeout'] = _DEFAULT_CONNECT_TIMEOUT
    return make_conninfo(**params)


def _raise_plain(exc):
    """Raise ``exc`` with neither ``__cause__`` nor ``__context__`` (no driver message)."""
    try:
        raise exc
    except type(exc) as mapped:
        mapped.__cause__ = None
        mapped.__context__ = None
        mapped.__suppress_context__ = True
        raise mapped


class Row:
    """Sequence values with the mapping access provided by sqlite3.Row."""

    def __init__(self, names, values):
        self._names = names
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                key = self._names.index(key)
            except ValueError:
                raise IndexError('No item with that key') from None
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = tuple(column.name for column in cursor.description) if cursor.description else ()
    return lambda values: Row(names, values)


class PostgresDatabase:
    dialect = PostgresDialect()

    def __init__(self, url):
        self._lock = threading.Lock()
        self._local = threading.local()
        self._idle = []
        self._connections = set()
        self.closed = False
        self._url = url
        try:
            import psycopg
        except ImportError:
            raise DatabaseError("Postgres needs the [postgres] extra: pip install "
                                "'agenticml-lite[postgres]'") from None
        self._psycopg = psycopg
        try:
            initial = self._new_connection()
            self._connections.add(initial)
            self._idle.append(initial)
            with self.transaction() as tx:
                role = tx.fetchone('SELECT current_user AS name, rolsuper, rolbypassrls '
                                   'FROM pg_roles WHERE rolname = current_user')
                if role['rolsuper'] or role['rolbypassrls']:
                    logger.warning('row-level security is not enforced for database role %s; '
                                   'use a role without SUPERUSER or BYPASSRLS', role['name'])
            from .migrate import migrate
            deadline = time.monotonic() + 60
            while True:
                try:
                    migrate(self)
                    break
                except DatabaseBusy:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        except BaseException:
            self.close()
            raise

    def _new_connection(self):
        connection = None
        try:
            connection = self._psycopg.connect(
                _conninfo_with_timeout(self._url), autocommit=True,
                row_factory=_row_factory)
            for setting in ("lock_timeout = '5s'", "statement_timeout = '60s'",
                            "idle_in_transaction_session_timeout = '60s'",
                            "TIME ZONE 'UTC'", "application_name = 'agenticml-server'"):
                connection.execute('SET ' + setting)
        except self._psycopg.Error:
            if connection is not None:
                self._close_connection(connection)
        except BaseException:
            if connection is not None:
                self._close_connection(connection)
            raise
        else:
            return connection
        _raise_plain(DatabaseError('could not connect to Postgres'))

    @staticmethod
    def _connection_error(exc):
        state = exc.sqlstate
        return state is None or state.startswith('08') or state in ('57P01', '57P02', '57P03')

    @staticmethod
    def exists(url):
        """Probe the configured search path without migrating or importing."""
        try:
            import psycopg
        except ImportError:
            raise DatabaseError("Postgres needs the [postgres] extra: pip install "
                                "'agenticml-lite[postgres]'") from None
        try:
            with psycopg.connect(_conninfo_with_timeout(url), autocommit=True) as connection:
                found = connection.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is not None
        except psycopg.Error:
            pass
        else:
            return found
        _raise_plain(DatabaseError('could not connect to Postgres'))

    @contextmanager
    def transaction(self, write=False):
        if getattr(self._local, 'active', False):
            raise RuntimeError('nested database transaction')
        self._local.active = True
        connection = None
        discard = False
        mapped = None
        try:
            while True:
                with self._lock:
                    if self.closed:
                        raise DatabaseError('database is closed')
                    connection = self._idle.pop() if self._idle else None
                    if connection is not None and (connection.closed or connection.broken):
                        self._connections.discard(connection)
                        stale = connection
                    else:
                        stale = None
                if stale is None:
                    break
                self._close_connection(stale)
                connection = None
            pooled = connection is not None
            for attempt in range(2):
                if connection is None:
                    connection = self._new_connection()
                    with self._lock:
                        if self.closed:
                            raise DatabaseError('database is closed')
                        self._connections.add(connection)
                try:
                    connection.execute('BEGIN ISOLATION LEVEL READ COMMITTED' +
                                       ('' if write else ' READ ONLY'))
                    break
                except self._psycopg.Error as exc:
                    if not (pooled and attempt == 0 and self._connection_error(exc)):
                        raise
                    with self._lock:
                        self._connections.discard(connection)
                    self._close_connection(connection)
                    connection = None
            yield Tx(connection, self.dialect)
            connection.commit()
        except BaseException as exc:
            discard = isinstance(exc, self._psycopg.Error) and self._connection_error(exc)
            if connection is not None:
                try:
                    connection.rollback()
                except self._psycopg.Error:
                    discard = True
            if isinstance(exc, self._psycopg.Error):
                if exc.sqlstate in ('55P03', '40P01', '40001'):
                    mapped = DatabaseBusy('database is locked')
                else:
                    mapped = DatabaseError('could not read or write database')
            else:
                raise
        finally:
            self._local.active = False
            if connection is not None:
                with self._lock:
                    discard = (discard or connection.closed or connection.broken or self.closed
                               or connection.pgconn.transaction_status != self._psycopg.pq.TransactionStatus.IDLE
                               or len(self._idle) >= 16)
                    if discard:
                        self._connections.discard(connection)
                    else:
                        self._idle.append(connection)
                if discard:
                    self._close_connection(connection)
        if mapped is not None:
            _raise_plain(mapped)

    def _close_connection(self, connection):
        try:
            connection.close()
        except self._psycopg.Error:
            pass

    def close(self):
        with self._lock:
            self.closed = True
            # Active transactions finish before releasing their connections.
            idle, self._idle = self._idle, []
            self._connections.difference_update(idle)
        for connection in idle:
            self._close_connection(connection)

    def __del__(self):
        self.close()

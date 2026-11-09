"""Postgres transactions and a bounded idle connection pool."""
from contextlib import contextmanager
import hashlib
import logging
import threading
import time

from .dialect import PostgresDialect
from .errors import DatabaseBusy, DatabaseError
from .sqlite import Tx

logger = logging.getLogger(__name__)
_DEFAULT_CONNECT_TIMEOUT = '10'
SESSION_LOCK_LIMIT = 64
OUTER_LOCK_LIMIT = 48
INNER_LOCK_LIMIT = 16
_outer_slots = threading.BoundedSemaphore(OUTER_LOCK_LIMIT)
_inner_slots = threading.BoundedSemaphore(INNER_LOCK_LIMIT)
_held_lock_connections = threading.local()


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

    def _new_connection(self, *, lock=False):
        connection = None
        try:
            conninfo = _conninfo_with_timeout(self._url)
            if lock:
                from psycopg.conninfo import make_conninfo
                conninfo = make_conninfo(conninfo, keepalives=1, keepalives_idle=30)
            connection = self._psycopg.connect(
                conninfo, autocommit=True,
                row_factory=_row_factory)
            for setting in ("lock_timeout = '5s'", "statement_timeout = '60s'",
                            "idle_in_transaction_session_timeout = '60s'",
                            "TIME ZONE 'UTC'", "application_name = 'agenticml-server'"):
                connection.execute('SET ' + setting)
            if lock:
                # Advisory locks are held on idle autocommit sessions while the
                # writer runs; never arm idle_session_timeout on these connections.
                try:
                    connection.execute('SET idle_session_timeout = 0')
                except (self._psycopg.errors.UndefinedObject,
                        self._psycopg.errors.FeatureNotSupported):
                    pass
                for setting in ("tcp_keepalives_idle=30", "tcp_keepalives_interval=10",
                                "tcp_keepalives_count=3", "tcp_user_timeout='30s'"):
                    try:
                        connection.execute('SET ' + setting)
                    except (self._psycopg.errors.UndefinedObject,
                            self._psycopg.errors.FeatureNotSupported):
                        logger.warning('Postgres SET %s is unsupported', setting)
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

    @contextmanager
    def session_lock(self, key_string, timeout=30, *, shared=False, try_lock=False,
                     dedicated=False):
        """Hold a session lock on a dedicated, process-bounded connection.

        Lock connections never enter the transaction pool; closing them on
        every exit also guarantees release when an unlock fails.
        """
        infinite = timeout is None or timeout < 0
        deadline = None if infinite else time.monotonic() + timeout
        slots = (_outer_slots if key_string.split(':', 1)[0] in ('run', 'object')
                 else _inner_slots)
        if not dedicated:
            if try_lock:
                slot_timeout = 0
            elif infinite:
                slot_timeout = None
            else:
                slot_timeout = max(0, timeout)
            if not slots.acquire(timeout=slot_timeout):
                raise DatabaseBusy('database is locked')
        connection = None
        acquired = False
        mapped = None
        pushed = False
        stack = None
        try:
            if self.closed:
                raise DatabaseError('database is closed')
            connection = self._new_connection(lock=True)
            if not try_lock:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DatabaseBusy('database is locked')
                    wait_ms = str(max(1, int(remaining * 1000))) + 'ms'
                    connection.execute("SELECT set_config('lock_timeout', %s, false)", (wait_ms,))
                    # The default 60s statement timeout must not shorten long import waits.
                    statement_ms = str(max(60000, int(remaining * 1000) + 1000)) + 'ms'
                    connection.execute("SELECT set_config('statement_timeout', %s, false)",
                                       (statement_ms,))
                else:
                    connection.execute("SELECT set_config('lock_timeout', '0', false)")
                    connection.execute("SELECT set_config('statement_timeout', '0', false)")
            # Separate from row: and name: transaction keys. Independent
            # installations in separate schemas must not contend.
            scope = connection.execute('SELECT current_database(), current_schema()').fetchone()
            name = 'session:' + str(tuple(scope)) + ':' + key_string
            key = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], 'big', signed=True)
            suffix = '_shared' if shared else ''
            if try_lock:
                acquired = connection.execute(
                    'SELECT pg_try_advisory_lock' + suffix + '(%s)', (key,)).fetchone()[0]
                if not acquired:
                    if key_string == 'server':
                        raise ValueError('stop every agenticml server using this database first')
                    raise DatabaseBusy('database is locked')
            else:
                connection.execute('SELECT pg_advisory_lock' + suffix + '(%s)', (key,))
            acquired = True
            stack = getattr(_held_lock_connections, 'stack', None)
            if stack is None:
                stack = []
                _held_lock_connections.stack = stack
            stack.append(connection)
            pushed = True
            yield connection
        except self._psycopg.Error as exc:
            mapped = (DatabaseBusy('database is locked') if exc.sqlstate in
                      ('55P03', '40P01', '40001') else DatabaseError('could not acquire database lock'))
        finally:
            if pushed:
                try:
                    stack.remove(connection)
                except ValueError:
                    pass
            if connection is not None:
                try:
                    if acquired:
                        connection.execute('SELECT pg_advisory_unlock' + suffix + '(%s)', (key,))
                except self._psycopg.Error:
                    pass
                finally:
                    self._close_connection(connection)
            if not dedicated:
                slots.release()
        if mapped is not None:
            _raise_plain(mapped)

    def confirm_session_locks(self):
        """Abort if a session-lock connection held on this thread has died."""
        for connection in getattr(_held_lock_connections, 'stack', ()):
            try:
                if connection.closed or connection.broken:
                    raise DatabaseBusy('database is locked')
                connection.execute('SELECT 1')
            except DatabaseBusy:
                raise
            except Exception:
                raise DatabaseBusy('database is locked') from None

    def server_lock(self, *, shared=False):
        # One lifetime connection per server, outside request lock budgets.
        return self.session_lock('server', shared=shared, try_lock=not shared,
                                 dedicated=shared)

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

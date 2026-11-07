"""SQLite transactions and a bounded idle connection pool."""
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading

from .dialect import SqliteDialect
from .errors import DatabaseBusy, DatabaseError

logger = logging.getLogger('agenticml.server.db.sqlite')
MOUNTINFO_PATH = Path('/proc/self/mountinfo')
NETWORK_FILESYSTEMS = {'nfs', 'nfs4', 'cifs', 'smb3', 'smbfs', '9p', 'fuse.sshfs',
                       'ceph', 'glusterfs', 'lustre', 'afs'}


def _check_local(path):
    if not sys.platform.startswith('linux'):
        return
    try:
        lines = Path(MOUNTINFO_PATH).read_text().splitlines()
    except OSError:
        return
    directory = path.parent.resolve()
    best_len = -1
    fs = None
    for line in lines:
        try:
            left, right = line.split(' - ', 1)
            mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), left.split()[4])
            point = Path(mount)
            if directory == point or point in directory.parents:
                n = len(point.parts)
                if n >= best_len:
                    best_len = n
                    fs = right.split()[0]
        except (ValueError, IndexError):
            continue
    if fs in NETWORK_FILESYSTEMS:
        raise DatabaseError(f'{path} is on a network filesystem ({fs}); SQLite needs '
                            'a local disk. Put --database on local disk or use '
                            'Postgres (3.0 hosted mode).')


def _check_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return _check_file(path)
        else:
            os.close(fd)
            return
    if not stat.S_ISREG(info.st_mode):
        raise DatabaseError(f'{path} is a symlink or not a regular database file')
    if info.st_mode & 0o077:
        logger.warning('%s is group- or world-accessible', path)


class Tx:
    def __init__(self, connection, dialect):
        self.connection = connection
        self.dialect = dialect

    def execute(self, sql, params=()):
        return self.connection.execute(self.dialect.placeholders(sql), params)

    def fetchone(self, sql, params=()):
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql, params=()):
        return self.execute(sql, params).fetchall()

    def lock_row(self, table, where_sql, params=()):
        return self.dialect.lock_row(self, table, where_sql, params)

    def advisory_lock(self, name):
        return self.dialect.advisory_lock(self, name)


class SqliteDatabase:
    dialect = SqliteDialect()

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._idle = []
        self._connections = set()
        self.closed = False
        try:
            if not self.path.parent.is_dir():
                raise DatabaseError('database parent must exist and be a directory')
            _check_local(self.path)
            _check_file(self.path)
            connection = self._new_connection()
            self._idle.append(connection)
            mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if mode.lower() != 'wal':
                raise DatabaseError('SQLite could not enable WAL journal mode')
            from .migrate import migrate
            migrate(self)
        except sqlite3.Error as exc:
            self.close()
            if 'locked' in str(exc).lower():
                raise DatabaseBusy('database is locked') from exc
            raise DatabaseError('could not open SQLite database') from exc
        except OSError as exc:
            self.close()
            raise DatabaseError('could not open SQLite database') from exc
        except BaseException:
            self.close()
            raise

    def _new_connection(self):
        connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA busy_timeout=5000')
        except BaseException:
            connection.close()
            raise
        self._connections.add(connection)
        return connection

    @contextmanager
    def transaction(self, write=False):
        if getattr(self._local, 'active', False):
            raise RuntimeError('nested database transaction')
        self._local.active = True
        connection = None
        discard = False
        try:
            with self._lock:
                if self.closed:
                    raise DatabaseError('database is closed')
                connection = self._idle.pop() if self._idle else self._new_connection()
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield Tx(connection, self.dialect)
            connection.commit()
        except BaseException as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    # A failed rollback can leave an open transaction. Never
                    # lend this connection to another request.
                    discard = True
            if isinstance(exc, sqlite3.Error):
                cls = DatabaseBusy if 'locked' in str(exc).lower() else DatabaseError
                raise cls('database is locked' if cls is DatabaseBusy else
                          'could not read or write database') from exc
            raise
        finally:
            self._local.active = False
            if connection is not None:
                with self._lock:
                    if discard or self.closed or len(self._idle) >= 16:
                        self._connections.discard(connection)
                        try:
                            connection.close()
                        except sqlite3.Error:
                            pass
                    else:
                        self._idle.append(connection)

    def close(self):
        with self._lock:
            self.closed = True
            # Active transactions finish before releasing their connections.
            for connection in self._idle:
                self._connections.discard(connection)
                connection.close()
            self._idle.clear()

    def __del__(self):
        self.close()

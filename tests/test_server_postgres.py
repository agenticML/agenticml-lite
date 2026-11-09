"""Postgres backend checks using the shared non-superuser schema harness."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import threading
import traceback
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import pytest
from server_db_helpers import migration_versions

from agenticml.server.db import DatabaseBusy, DatabaseError, connect, for_server_dir
from agenticml.server.db.dialect import PostgresDialect, SqliteDialect
from agenticml.server.db.postgres import PostgresDatabase

pytestmark = pytest.mark.skipif(not os.environ.get('AGENTICML_TEST_DATABASE_URL'),
                                reason='AGENTICML_TEST_DATABASE_URL is not set')


@pytest.fixture
def pg_url(server_database):
    return server_database


@pytest.fixture
def open_db(pg_url):
    databases = []

    def create(url=None):
        db = connect(pg_url if url is None else url)
        databases.append(db)
        return db

    yield create
    for db in databases:
        db.close()


def test_migrate_on_open_and_reopen_is_idempotent(open_db):
    db = open_db()
    with db.transaction() as tx:
        before = [dict(r) for r in tx.fetchall('SELECT * FROM schema_migrations ORDER BY version')]
        wid = tx.fetchone('SELECT id FROM workspaces')[0]
    assert [r['version'] for r in before] == migration_versions()
    db.close()
    with open_db().transaction() as tx:
        assert [dict(r) for r in tx.fetchall('SELECT * FROM schema_migrations ORDER BY version')] == before
        assert tx.fetchone('SELECT id FROM workspaces')[0] == wid


def test_concurrent_first_open_migrates_once(open_db):
    barrier = threading.Barrier(2)

    def start(_):
        barrier.wait(timeout=10)
        return open_db()

    with ThreadPoolExecutor(2) as pool:
        databases = list(pool.map(start, range(2)))
    for db in databases:
        with db.transaction() as tx:
            assert tx.fetchone('SELECT COUNT(*) FROM schema_migrations')[0] == len(migration_versions())
            assert tx.fetchone('SELECT COUNT(*) FROM workspaces')[0] == 1


def test_placeholders_percent_rows_and_upsert(open_db):
    db = open_db()
    assert db.dialect.name == 'postgres' and SqliteDialect.name == 'sqlite'
    with db.transaction(write=True) as tx:
        row = tx.fetchone("SELECT ? AS number, '50%' AS label, 7 % 3 AS remainder", (42,))
        assert row[0] == row['number'] == 42
        assert row[-1] == 1 and row[:2] == (42, '50%')
        assert row.keys() == ['number', 'label', 'remainder']
        assert dict(row) == {'number': 42, 'label': '50%', 'remainder': 1}
        assert list(row) == [42, '50%', 1] and len(row) == 3
        with pytest.raises(IndexError):
            row['absent']
        statement = db.dialect.upsert('server_settings', ['name', 'value'], ['name'], ['value'])
        assert tx.execute(statement, ('test', 'first')).rowcount == 1
        tx.execute(statement, ('test', 'second'))
        assert tx.fetchone('SELECT value FROM server_settings WHERE name=?', ('test',))[0] == 'second'
        assert tx.fetchone("SELECT '100%' AS label")[0] == '100%'


def test_migration_tokens_and_sql_splitter(open_db, tmp_path, monkeypatch):
    from agenticml.server.db import migrate

    db = open_db()
    next_version = max(migration_versions()) + 1
    migrations = tmp_path / 'migrations'
    migrations.mkdir()
    (migrations / f'{next_version:04d}_types.sql').write_text(
        'CREATE TABLE types (id {{id}}, data {{blob}}, value {{real}});\n'
        "INSERT INTO types (data, value) VALUES (NULL, 1.5);\n")
    monkeypatch.setattr(migrate, 'MIGRATIONS', migrations)
    migrate.migrate(db)
    with db.transaction() as tx:
        row = tx.fetchone('SELECT * FROM types')
        assert dict(row) == {'id': 1, 'data': None, 'value': 1.5}


def test_pool_settings_read_only_rollback_nesting_and_close(open_db):
    db = open_db()
    with db.transaction() as tx:
        connection = tx.connection
        for setting, expected in [('lock_timeout', '5s'), ('statement_timeout', '1min'),
                                  ('idle_in_transaction_session_timeout', '1min'),
                                  ('TimeZone', 'UTC'), ('application_name', 'agenticml-server'),
                                  ('transaction_isolation', 'read committed'),
                                  ('transaction_read_only', 'on')]:
            assert tx.fetchone('SHOW ' + setting)[0] == expected
        assert connection.autocommit is True
        with pytest.raises(RuntimeError, match='nested database transaction'):
            with db.transaction():
                pass
    with pytest.raises(ValueError):
        with db.transaction(write=True) as tx:
            assert tx.connection is connection
            assert tx.fetchone('SHOW transaction_read_only')[0] == 'off'
            tx.execute("INSERT INTO server_settings VALUES ('abort', 'value')")
            raise ValueError('abort')
    with db.transaction() as tx:
        assert tx.fetchone("SELECT * FROM server_settings WHERE name='abort'") is None
    barrier = threading.Barrier(20)

    def read(_):
        with db.transaction():
            barrier.wait(timeout=15)

    with ThreadPoolExecutor(20) as pool:
        list(pool.map(read, range(20)))
    assert len(db._idle) == len(db._connections) == 16
    db.close()
    assert db.closed and not db._connections
    with pytest.raises(DatabaseError, match='database is closed'):
        with db.transaction():
            pass


@pytest.mark.parametrize('kind', ['advisory', 'row'])
def test_lock_timeout_maps_to_busy(open_db, kind):
    first, second = open_db(), open_db()

    def lock(tx):
        if kind == 'advisory':
            tx.advisory_lock('held')
        else:
            # No matching row exists: advisory locking still serializes inserts.
            tx.lock_row('server_settings', 'name=?', ('missing',))

    with first.transaction(write=True) as tx:
        lock(tx)
        with pytest.raises(DatabaseBusy, match='database is locked'):
            with second.transaction(write=True) as other:
                connection = other.connection
                other.execute("SET LOCAL lock_timeout = '50ms'")
                lock(other)
    assert connection in second._idle and not connection.closed
    with second.transaction(write=True) as tx:
        assert tx.connection is connection
        lock(tx)


def test_lock_key_is_signed_sha256_and_namespaced():
    class Capture:
        def execute(self, sql, params):
            self.sql, self.params = sql, params

    tx = Capture()
    dialect = PostgresDialect()
    for namespace in ('name:migrate', 'row:runs:' + json.dumps(('wid', 'rid'))):
        if namespace.startswith('name:'):
            dialect.advisory_lock(tx, 'migrate')
        else:
            dialect.lock_row(tx, 'runs', 'workspace_id=? AND run_id=?', ('wid', 'rid'))
        assert tx.sql == 'SELECT pg_advisory_xact_lock(?)'
        assert tx.params == (int.from_bytes(hashlib.sha256(namespace.encode()).digest()[:8],
                                           'big', signed=True),)


@pytest.mark.parametrize('failure', ['OperationalError', 'InterfaceError', 'DeadlockDetected',
                                     'SerializationFailure', 'DivisionByZero', 'QueryCanceled'])
def test_private_errors_and_broken_connections(open_db, failure):
    import psycopg

    db = open_db()
    error_type = getattr(psycopg.errors, failure)
    expected = DatabaseBusy if failure in ('DeadlockDetected', 'SerializationFailure') else DatabaseError
    with pytest.raises(expected) as error:
        with db.transaction(write=True) as tx:
            connection = tx.connection
            raise error_type('private host password details')
    assert 'private host password details' not in ''.join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None and error.value.__context__ is None
    if failure in ('OperationalError', 'InterfaceError'):
        assert connection.closed
        assert connection not in db._idle and connection not in db._connections
    else:
        assert connection in db._idle and not connection.closed
    with db.transaction() as tx:
        assert tx.fetchone('SELECT 1')[0] == 1


@pytest.mark.parametrize('failure', ['password', 'host'])
def test_connection_errors_do_not_leak_secrets(pg_url, caplog, failure):
    parts = urlsplit(pg_url)
    password = 'private-bad-password'
    host = parts.hostname
    if failure == 'password':
        netloc = parts.netloc.replace(':' + parts.password + '@', ':' + password + '@')
    else:
        netloc = parts.netloc.replace(':' + str(parts.port), ':1')
        password = parts.password
    query = dict(parse_qsl(parts.query), connect_timeout='1')
    bad_url = urlunsplit(parts._replace(netloc=netloc, query=urlencode(query, quote_via=quote)))
    with pytest.raises(DatabaseError, match='^could not connect to Postgres$') as error:
        connect(bad_url)
    rendered = ''.join(traceback.format_exception(error.value)) + caplog.text
    assert bad_url not in rendered and password not in rendered and host not in rendered
    assert error.value.__cause__ is None and error.value.__context__ is None


def test_connect_timeout_defaults_when_unset(pg_url, monkeypatch):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from agenticml.server.db.postgres import _conninfo_with_timeout, _DEFAULT_CONNECT_TIMEOUT
    from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

    parts = urlsplit(pg_url)
    query = {key: value for key, value in parse_qsl(parts.query) if key != 'connect_timeout'}
    unset = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
    params = conninfo_to_dict(_conninfo_with_timeout(unset))
    assert params.get('connect_timeout') == _DEFAULT_CONNECT_TIMEOUT
    seen = []
    original = psycopg.connect

    def spy(conninfo, *args, **kwargs):
        seen.append(conninfo_to_dict(conninfo))
        return original(conninfo, *args, **kwargs)

    monkeypatch.setattr(psycopg, 'connect', spy)
    db = connect(unset)
    db.close()
    assert seen and all(item.get('connect_timeout') == _DEFAULT_CONNECT_TIMEOUT for item in seen)
    kept = dict(parse_qsl(parts.query), connect_timeout='7')
    with_timeout = urlunsplit(parts._replace(query=urlencode(kept, quote_via=quote)))
    assert conninfo_to_dict(_conninfo_with_timeout(with_timeout)).get('connect_timeout') == '7'


def test_superuser_warning_names_only_role(open_db, pg_url, caplog):
    base = urlsplit(os.environ['AGENTICML_TEST_DATABASE_URL'])
    scoped = urlsplit(pg_url)
    db = open_db(urlunsplit(base._replace(query=scoped.query)))
    with db.transaction() as tx:
        role = tx.fetchone('SELECT current_user')[0]
    assert f'row-level security is not enforced for database role {role};' in caplog.text
    assert 'use a role without SUPERUSER or BYPASSRLS' in caplog.text
    parts = urlsplit(pg_url)
    assert pg_url not in caplog.text and parts.password not in caplog.text
    assert parts.hostname not in caplog.text


def test_postgres_alias_and_registry_import(open_db, pg_url, tmp_path):
    db = open_db('postgres://' + pg_url.removeprefix('postgresql://'))
    assert isinstance(db, PostgresDatabase)
    directory = tmp_path / 'legacy'
    directory.mkdir()
    (directory / 'users.json').write_text(json.dumps({'version': 1, 'users': []}))
    shared = for_server_dir(directory, pg_url, create=False)
    try:
        assert for_server_dir(tmp_path / 'another', pg_url) is shared
        assert (directory / 'users.json.2x').is_file()
        assert not (directory / 'agenticml.db').exists()
    finally:
        shared.close()


@pytest.mark.parametrize('stage', ['body', 'commit'])
def test_failed_rollback_discards_connection(open_db, monkeypatch, stage):
    import psycopg

    db = open_db()
    with db.transaction() as tx:
        broken = tx.connection
    original_commit = type(broken).commit
    original_rollback = type(broken).rollback

    def commit(connection):
        if connection is broken and stage == 'commit':
            raise psycopg.OperationalError('private commit details')
        return original_commit(connection)

    def rollback(connection):
        if connection is broken:
            raise psycopg.OperationalError('private rollback details')
        return original_rollback(connection)

    monkeypatch.setattr(type(broken), 'commit', commit)
    monkeypatch.setattr(type(broken), 'rollback', rollback)
    with pytest.raises(DatabaseError, match='could not read or write database') as error:
        with db.transaction(write=True) as tx:
            if stage == 'body':
                tx.execute('SELECT 1 / 0')
    assert 'private' not in str(error.value)
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert broken.closed and broken not in db._connections and broken not in db._idle
    with db.transaction() as tx:
        assert tx.fetchone('SELECT 1')[0] == 1


def test_server_terminated_connection_is_discarded(open_db):
    db, killer = open_db(), open_db()
    with pytest.raises(DatabaseError):
        with db.transaction() as tx:
            broken = tx.connection
            pid = tx.fetchone('SELECT pg_backend_pid()')[0]
            with killer.transaction(write=True) as other:
                other.execute('SELECT pg_terminate_backend(?)', (pid,))
            tx.execute('SELECT 1')
    assert broken.closed and broken not in db._idle and broken not in db._connections
    with db.transaction() as tx:
        assert tx.fetchone('SELECT 1')[0] == 1


def test_read_transaction_rejects_write_without_nested_begin_warning(open_db):
    db = open_db()
    notices = []
    with db.transaction() as tx:
        tx.connection.add_notice_handler(lambda diagnostic: notices.append(diagnostic.message_primary))
    with pytest.raises(DatabaseError):
        with db.transaction() as tx:
            tx.execute("INSERT INTO server_settings (name, value) VALUES ('readonly', 'no')")
    assert not any('already a transaction in progress' in notice for notice in notices)
    with db.transaction() as tx:
        assert tx.fetchone("SELECT 1 FROM server_settings WHERE name='readonly'") is None


def test_slow_connect_does_not_hold_pool_lock(open_db, monkeypatch):
    db = open_db()
    started, release = threading.Event(), threading.Event()
    original = db._new_connection

    def slow_connect():
        started.set()
        assert release.wait(timeout=10)
        return original()

    def read():
        with db.transaction() as tx:
            return tx.fetchone('SELECT 1')[0]

    monkeypatch.setattr(db, '_new_connection', slow_connect)
    with ThreadPoolExecutor(2) as pool:
        try:
            with db.transaction():
                slow = pool.submit(read)
                assert started.wait(timeout=5)
            # The initial connection is idle again while the new connect waits.
            assert pool.submit(read).result(timeout=3) == 1
        finally:
            release.set()
        assert slow.result(timeout=5) == 1


def test_shared_harness_uses_owned_schema_and_unprivileged_role(open_db):
    db = open_db()
    with db.transaction() as tx:
        role = tx.fetchone('SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user')
        assert tuple(role) == ('agenticml_test', False, False)
        schema = tx.fetchone('SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=current_schema()')
        assert schema[0].startswith('t_') and len(schema[0]) == 18
        assert schema[1] == 'agenticml_test'


def test_concurrent_file_records_ensure_same_missing_run(open_db):
    from agenticml.server.db.repos import RunRepo
    from agenticml.server.workspaces import default_workspace_id

    first, second = open_db(), open_db()
    wid = default_workspace_id(first)
    barrier = threading.Barrier(2)

    def record(pair):
        db, path = pair
        barrier.wait(timeout=5)
        RunRepo(db, wid).record_file('new-run', path, 7)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(record, [(first, 'first'), (second, 'second')]))
    with first.transaction() as tx:
        assert tx.fetchone('SELECT COUNT(*) FROM runs WHERE run_id=?', ('new-run',))[0] == 1
        assert {row[0] for row in tx.fetchall('SELECT path FROM run_files WHERE run_id=?', ('new-run',))} == {'first', 'second'}


def test_terminated_idle_backend_reconnects(open_db):
    db, killer = open_db(), open_db()
    with db.transaction() as tx:
        stale = tx.connection
        pid = tx.fetchone('SELECT pg_backend_pid()')[0]
    with killer.transaction(write=True) as tx:
        assert tx.fetchone('SELECT pg_terminate_backend(?)', (pid,))[0]
    with db.transaction() as tx:
        assert tx.fetchone('SELECT 1')[0] == 1
        assert tx.connection is not stale
    assert stale.closed and stale not in db._connections


def test_closed_idle_connection_is_skipped(open_db):
    db = open_db()
    stale = db._idle[-1]
    stale.close()
    with db.transaction() as tx:
        assert tx.fetchone('SELECT 1')[0] == 1
        assert tx.connection is not stale
    assert stale not in db._connections


def test_non_idle_connection_is_discarded(open_db, monkeypatch):
    db = open_db()
    connection = db._idle[-1]
    original = type(connection).commit

    def leave_transaction_open(current):
        if current is not connection:
            original(current)

    monkeypatch.setattr(type(connection), 'commit', leave_transaction_open)
    with db.transaction() as tx:
        assert tx.fetchone('SELECT 1')[0] == 1
    assert connection.closed and connection not in db._connections


def test_slow_close_does_not_hold_pool_lock(open_db, monkeypatch):
    db = open_db()
    stale = db._idle[-1]
    stale.close()
    started, release = threading.Event(), threading.Event()
    original = db._close_connection

    def slow_close(connection):
        if connection is stale:
            started.set()
            assert release.wait(timeout=10)
        original(connection)

    def read():
        with db.transaction() as tx:
            return tx.fetchone('SELECT 1')[0]

    monkeypatch.setattr(db, '_close_connection', slow_close)
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(read)
        try:
            assert started.wait(timeout=5)
            assert pool.submit(read).result(timeout=5) == 1
        finally:
            release.set()
        assert pending.result(timeout=5) == 1


def test_migration_busy_retries_until_deadline(pg_url, monkeypatch):
    from agenticml.server.db import migrate, postgres

    original = migrate.migrate
    calls = []
    clock = iter([0, 1, 61])
    monkeypatch.setattr(postgres.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(postgres.time, 'sleep', lambda _: None)

    def busy(db):
        calls.append(db)
        raise DatabaseBusy('database is locked')

    monkeypatch.setattr(migrate, 'migrate', busy)
    with pytest.raises(DatabaseBusy):
        connect(pg_url)
    assert len(calls) == 2 and calls[0].closed
    monkeypatch.setattr(postgres.time, 'monotonic', lambda: 0)
    calls.clear()

    def once_busy(db):
        calls.append(db)
        if len(calls) == 1:
            raise DatabaseBusy('database is locked')
        original(db)

    monkeypatch.setattr(migrate, 'migrate', once_busy)
    db = connect(pg_url)
    db.close()
    assert len(calls) == 2


@pytest.mark.parametrize('first_writer', ['file', 'meta'])
def test_record_file_and_claim_meta_serialize_missing_run(open_db, monkeypatch, first_writer):
    from agenticml.server.db.repos import RunRepo
    from agenticml.server.db.sqlite import Tx
    from agenticml.server.workspaces import default_workspace_id

    first, second = open_db(), open_db()
    wid = default_workspace_id(first)
    held, attempted, release = threading.Event(), threading.Event(), threading.Event()
    original = Tx.lock_row

    def lock(tx, table, where, params):
        if table == 'runs':
            if threading.current_thread().name.endswith('_0'):
                original(tx, table, where, params)
                held.set()
                assert release.wait(timeout=10)
                return
            attempted.set()
        original(tx, table, where, params)

    monkeypatch.setattr(Tx, 'lock_row', lock)

    def write(db, kind):
        repo = RunRepo(db, wid)
        if kind == 'file':
            repo.record_file('new-run', 'scalars/_time.bin', 7, last_step=2**40)
        else:
            repo.claim_meta('new-run', owner_user='alice', owner_key='box',
                            list_row={'run_id': 'new-run'}, project='demo', created_at=None,
                            launch_note=None, claim=True)

    with ThreadPoolExecutor(2) as pool:
        leading = pool.submit(write, first, first_writer)
        try:
            assert held.wait(timeout=5)
            following = pool.submit(write, second, 'meta' if first_writer == 'file' else 'file')
            assert attempted.wait(timeout=5)
            assert not following.done()
        finally:
            release.set()
        leading.result(timeout=5)
        following.result(timeout=5)
    repo = RunRepo(first, wid)
    assert repo.get('new-run')['owner_user'] == 'alice'
    assert repo.get('new-run')['last_step'] == 2**40
    assert repo.committed_size('new-run', 'scalars/_time.bin') == 7

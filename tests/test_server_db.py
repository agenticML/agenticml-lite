"""Database boundaries, durability, concurrency, and one-time 2.x import."""
from server_db_helpers import isolate_server_dir, database_url, on_postgres, migration_versions
from server_db_helpers import default_store
from server_db_helpers import default_workspace_id
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import threading

import pytest

from agenticml import cli
from agenticml.server.app import make_server, prepare_data_dir
from agenticml.server.db import (DatabaseBusy, DatabaseError, SchemaTooNew, connect,
                                default_url, for_server_dir, resolve_url)
from agenticml.server.db import sqlite as backend
from agenticml.server.db.repos import AuditRepo, RunRepo, SessionRepo, SettingsRepo
from agenticml.server.keys import KeyStore, KeyStoreUnavailable
from agenticml.server.users import UserStore
from agenticml.server.webauth import DbBuckets, LoginThrottle
from agenticml.store import layout
from test_server_signin import PASSWORD, password_login, running
from test_server_web import authed, login
from test_server_users import write_v1_keys, write_run


def url(path):
    return os.environ.get('AGENTICML_SERVER_DATABASE_URL') if os.environ.get('AGENTICML_TEST_DATABASE_URL') else 'sqlite:///' + str(path)


@pytest.mark.sqlite_only
def test_pool_pragmas_permissions_rollback_and_nesting(tmp_path):
    db = connect(url(tmp_path / 'state.db'))
    assert stat.S_IMODE(db.path.stat().st_mode) == 0o600
    with db.transaction() as tx:
        assert tx.fetchone('PRAGMA journal_mode')[0] == 'wal'
        assert tx.fetchone('PRAGMA foreign_keys')[0] == 1
        assert tx.fetchone('PRAGMA synchronous')[0] == 2
        assert tx.fetchone('PRAGMA busy_timeout')[0] == 5000
        connection = tx.connection
        with pytest.raises(RuntimeError, match='nested'):
            with db.transaction(): pass
    with pytest.raises(ValueError):
        with db.transaction(write=True) as tx:
            tx.execute("INSERT INTO server_settings VALUES ('rollback','no')")
            raise ValueError('abort')
    with db.transaction() as tx:
        assert tx.connection is connection
        assert tx.fetchone("SELECT value FROM server_settings WHERE name='rollback'") is None
    # A request in another thread reuses the idle connection.
    def read():
        with db.transaction() as tx: return tx.connection
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(read).result() is connection
    db.close()
    assert db.closed and not db._connections
    with pytest.raises(DatabaseError, match='closed'):
        with db.transaction(): pass


def test_pool_keeps_at_most_sixteen_idle(tmp_path):
    db = connect(url(tmp_path / 'state.db'))
    barrier = threading.Barrier(20)
    def read(_):
        with db.transaction(): barrier.wait(timeout=10)
    with ThreadPoolExecutor(20) as pool:
        list(pool.map(read, range(20)))
    assert len(db._idle) == len(db._connections) == 16
    db.close()


@pytest.mark.parametrize('kind', ['symlink','broken_symlink','fifo','directory'])
@pytest.mark.sqlite_only
def test_refuse_nonregular_before_connect(tmp_path, monkeypatch, kind):
    path = tmp_path / 'state.db'
    if kind in ('symlink','broken_symlink'):
        target = tmp_path / 'outside'
        if kind == 'symlink': target.write_bytes(b'private data')
        path.symlink_to(target)
    elif kind == 'fifo': os.mkfifo(path)
    else: path.mkdir()
    def forbidden(*args, **kwargs): pytest.fail('sqlite3.connect reached unsafe path')
    monkeypatch.setattr(backend.sqlite3, 'connect', forbidden)
    with pytest.raises(DatabaseError, match='not a regular database file'):
        connect(url(path))


@pytest.mark.sqlite_only
def test_nfs_longest_mount_and_escaped_paths(tmp_path, monkeypatch):
    directory = tmp_path / 'local disk'
    directory.mkdir()
    fixture = tmp_path / 'mountinfo'
    mount = str(directory).replace(' ', r'\040')
    fixture.write_text(f'1 0 0:1 / / rw - ext4 /dev/sda rw\n'
                       f'2 1 0:2 / {mount} rw - nfs4 server:/ rw\n')
    monkeypatch.setattr(backend, 'MOUNTINFO_PATH', fixture)
    with pytest.raises(DatabaseError, match=r'network filesystem \(nfs4\)'):
        connect(url(directory / 'state.db'))
    assert not (directory / 'state.db').exists()
    fixture.write_text(f'1 0 0:1 / / rw - nfs4 server:/ rw\n'
                       f'2 1 0:2 / {mount} rw - ext4 /dev/sda rw\n')
    connect(url(directory / 'state.db')).close()
    fixture.write_text(f'1 0 0:1 / {mount} rw - nfs4 server:/ rw\n'
                       f'2 1 0:2 / {mount} rw - ext4 /dev/sda rw\n')
    connect(url(directory / 'later-equal.db')).close()
    fixture.write_text(f'1 0 0:1 / {mount} rw - ext4 /dev/sda rw\n'
                       f'2 1 0:2 / {mount} rw - nfs4 server:/ rw\n')
    with pytest.raises(DatabaseError, match=r'network filesystem \(nfs4\)'):
        connect(url(directory / 'later-nfs.db'))
    fixture.unlink()
    connect(url(tmp_path / 'missing-mountinfo.db')).close()


def test_url_precedence_and_validation(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('AGENTICML_SERVER_DATABASE_URL', raising=False)
    literal_url = lambda path: 'sqlite:///' + str(path)
    expected = literal_url(tmp_path / 'server' / 'agenticml.db')
    assert default_url(tmp_path) == expected
    assert resolve_url(tmp_path) == expected
    monkeypatch.setenv('AGENTICML_SERVER_DATABASE_URL', literal_url(tmp_path / 'env.db'))
    assert resolve_url(tmp_path) == literal_url(tmp_path / 'env.db')
    assert resolve_url(tmp_path, 'explicit') == 'explicit'
    for bad in ['unsupported://user:private-password@host/db', 'sqlite:///relative.db']:
        with pytest.raises(DatabaseError) as error: connect(bad)
        assert bad not in str(error.value) and 'private-password' not in str(error.value)
        assert cli.main(['server','keys','list','--data',str(tmp_path),'--database',bad]) == 1
        assert 'private-password' not in capsys.readouterr().err
    with pytest.raises(DatabaseError, match='parent must exist'):
        connect(literal_url(tmp_path / 'absent' / 'state.db'))


def test_postgres_url_reports_connection_failure(tmp_path, capsys):
    pytest.importorskip('psycopg')
    bad = 'postgresql://user:private-password@127.0.0.1:1/db?connect_timeout=1'
    with pytest.raises(DatabaseError, match='^could not connect to Postgres$') as error:
        connect(bad)
    assert 'private-password' not in str(error.value) and '127.0.0.1' not in str(error.value)
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert cli.main(['server', 'keys', 'list', '--data', str(tmp_path), '--database', bad]) == 1
    assert 'private-password' not in capsys.readouterr().err


def test_postgres_conninfo_default_connect_timeout():
    pytest.importorskip('psycopg')
    from agenticml.server.db.postgres import _DEFAULT_CONNECT_TIMEOUT, _conninfo_with_timeout
    from psycopg.conninfo import conninfo_to_dict
    unset = conninfo_to_dict(_conninfo_with_timeout('postgresql://u:p@127.0.0.1:1/db'))
    assert unset.get('connect_timeout') == _DEFAULT_CONNECT_TIMEOUT
    kept = conninfo_to_dict(_conninfo_with_timeout('postgresql://u:p@127.0.0.1:1/db?connect_timeout=3'))
    assert kept.get('connect_timeout') == '3'


def test_postgres_dependency_is_lazy_and_missing_extra_is_clear(monkeypatch):
    import builtins
    import subprocess
    import sys

    subprocess.check_call([sys.executable, '-c',
                           'import agenticml.server.db, sys; assert "psycopg" not in sys.modules'])
    original = builtins.__import__

    def without_psycopg(name, *args, **kwargs):
        if name == 'psycopg':
            raise ImportError('not installed')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_psycopg)
    with pytest.raises(DatabaseError) as error:
        connect('postgresql://user:private-password@host/db')
    assert str(error.value) == ("Postgres needs the [postgres] extra: pip install "
                                "'agenticml-lite[postgres]'")


def test_migrations_concurrent_idempotent_and_ordered(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    # WAL initialization uses the same busy timeout as every transaction.
    def open_db(_): return connect(url(path))
    with ThreadPoolExecutor(2) as pool:
        databases = list(pool.map(open_db, range(2)))
    with databases[0].transaction() as tx:
        assert [r['version'] for r in tx.fetchall('SELECT * FROM schema_migrations ORDER BY version')] == migration_versions()
    for db in databases: db.close()
    from agenticml.server.db import migrate
    migrations = tmp_path / 'migrations'
    migrations.mkdir()
    applied = migration_versions()
    next_version = max(applied) + 1
    (migrations / f'{next_version:04d}_extra.sql').write_text('CREATE TABLE extra (id {{id}}, value {{blob}});\n')
    (migrations / f'{next_version + 1:04d}_last.sql').write_text('CREATE TABLE last (value {{real}});\n')
    monkeypatch.setattr(migrate, 'MIGRATIONS', migrations)
    db = connect(url(path))
    with db.transaction() as tx:
        assert [r[0] for r in tx.fetchall('SELECT version FROM schema_migrations ORDER BY version')] == applied + migration_versions()
        assert tx.fetchone('SELECT COUNT(*) FROM extra')[0] == 0
        assert tx.fetchone('SELECT COUNT(*) FROM last')[0] == 0
    db.close()
    connect(url(path)).close()


def test_failed_migration_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    from agenticml.server.db import migrate
    path = tmp_path / 'state.db'
    migrations = tmp_path / 'migrations'
    migrations.mkdir()
    (migrations / '0001_initial.sql').write_text('CREATE TABLE ok (id INTEGER);\n')
    (migrations / '0002_bad.sql').write_text('CREATE TABLE must_rollback (id INTEGER);\nINVALID SQL;\n')
    monkeypatch.setattr(migrate, 'MIGRATIONS', migrations)
    with pytest.raises(DatabaseError):
        connect(url(path))
    (migrations / '0002_bad.sql').unlink()
    db = connect(url(path))
    with db.transaction() as tx:
        if db.dialect.name == 'sqlite':
            assert tx.fetchone("SELECT name FROM sqlite_master WHERE name='must_rollback'") is None
        else:
            assert tx.fetchone("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='must_rollback'") is None
        assert tx.fetchone('SELECT MAX(version) FROM schema_migrations')[0] == max(migration_versions())
    db.close()


def test_schema_too_new_server_and_cli_refuse(tmp_path, capsys):
    prepare_data_dir(tmp_path)
    db = for_server_dir(tmp_path / 'server')
    newest = max(migration_versions())
    future = newest + 1
    with db.transaction(write=True) as tx:
        tx.execute('INSERT INTO schema_migrations VALUES (?, ?, ?)', (future,'future',layout.format_timestamp()))
    db.close()
    with pytest.raises(SchemaTooNew, match=f'knows up to {newest}'):
        make_server(tmp_path, port=0)
    for command in [('keys','list'),('users','list'),('audit',),('serve',),('status',),('verify',)]:
        assert cli.main(['server',*command,'--data',str(tmp_path)]) == 1
        assert f'database schema version {future}' in capsys.readouterr().err


def test_missing_status_does_not_create_database(tmp_path):
    from agenticml.server.admin import status, verify
    prepare_data_dir(tmp_path)
    assert for_server_dir(tmp_path / 'server', create=False) is None
    report = status(tmp_path)
    assert report['keys'] == report['users'] == []
    verify(tmp_path)
    assert for_server_dir(tmp_path / 'server', create=False) is None
    if on_postgres():
        from agenticml.server.db.postgres import PostgresDatabase
        assert not PostgresDatabase.exists(resolve_url(tmp_path))
    assert not (tmp_path / 'server' / 'agenticml.db').exists()


def test_busy_maps_to_store_unavailable_and_create_is_atomic(tmp_path):
    directory = tmp_path / 'server'
    directory.mkdir()
    keys = KeyStore(directory, workspace_id=default_workspace_id(directory))
    other = connect(url(directory / "agenticml.db"))
    # Shorten only the test connection's timeout (production is five seconds).
    with keys.db.transaction() as tx:
        tx.execute("SET lock_timeout = '50ms'" if on_postgres() else 'PRAGMA busy_timeout=1')
    with other.transaction(write=True) as tx:
        tx.advisory_lock('users')
        with pytest.raises(KeyStoreUnavailable) as error: keys.create('first','admin')
        assert isinstance(error.value.__cause__, DatabaseBusy)
    assert keys.list() == [] and UserStore(directory, workspace_id=default_workspace_id(directory)).list() == []
    assert not keys.setup_closed()
    other.close()


def test_registry_shared_and_closed_pool_reopened(tmp_path):
    keys = KeyStore(tmp_path, workspace_id=default_workspace_id(tmp_path))
    users = UserStore(tmp_path, workspace_id=default_workspace_id(tmp_path))
    assert keys.db is users.db is for_server_dir(tmp_path)
    keys.db.close()
    assert for_server_dir(tmp_path) is not keys.db


def test_import_v1_once_including_run_owners_and_audit(tmp_path):
    prepare_data_dir(tmp_path)
    directory = tmp_path / 'server'
    write_v1_keys(directory / 'keys.json',['old'], scopes=['admin'])
    write_run(default_store(tmp_path), 'run-1', 'old')
    row = {'ts':layout.format_timestamp(),'actor':'old','action':'login.ok'}
    (directory / 'audit.jsonl').write_text(json.dumps(row)+'\n'+json.dumps(row))
    (directory / 'setup-closed').touch()
    db = for_server_dir(directory)
    assert UserStore(directory, workspace_id=default_workspace_id(directory)).get('admin').role == 'owner'
    assert KeyStore(directory, workspace_id=default_workspace_id(directory)).list()[0].user == 'admin'
    assert RunRepo(db, default_workspace_id(db)).as_remote('run-1') == {'owner':'admin','owner_key':'old'}
    assert (default_store(tmp_path) / 'runs' / 'run-1' / 'remote.json.2x').exists()
    assert not (default_store(tmp_path) / 'runs' / 'run-1' / 'remote.json').exists()
    assert SettingsRepo(db).get('setup_closed') is not None
    assert {r['action'] for r in AuditRepo(db, default_workspace_id(db)).tail(10)} == {'migration','login.ok'}
    backups = {p:p.stat().st_mtime_ns for p in directory.glob('*.2x')}
    assert len(backups) == 4 and not (directory / 'keys.lock').exists()
    db.close()
    (directory / 'keys.json').write_text('invalid, deliberately ignored after import')
    db = for_server_dir(directory)
    assert len(KeyStore(directory, workspace_id=default_workspace_id(directory)).list()) == 1
    assert {p:p.stat().st_mtime_ns for p in backups} == backups
    assert len(AuditRepo(db, default_workspace_id(db)).tail(10)) == 2


def test_import_22_password_invite_and_rollback_on_bad_keys(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    users = UserStore(source, workspace_id=default_workspace_id(source))
    users.add('alice','admin',identity='Alice@example.com')
    users.set_password('alice',PASSWORD)
    users.add('bob')
    invite = users.create_invite('bob')
    key, secret = KeyStore(source, workspace_id=default_workspace_id(source)).create('laptop',user='alice')
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    isolate_server_dir(legacy, monkeypatch)
    (legacy / 'users.json').write_text(json.dumps({'version':1,'users':[dict(asdict(r), role='admin' if r.role == 'owner' else r.role) for r in users.list()]}))
    (legacy / 'keys.json').write_text('{')
    with pytest.raises(DatabaseError, match='could not import keys.json'): KeyStore(legacy, workspace_id=default_workspace_id(legacy))
    db = connect(url(legacy / 'agenticml.db'))
    with db.transaction() as tx:
        assert tx.fetchone('SELECT COUNT(*) FROM accounts')[0] == 0
        assert tx.fetchone("SELECT value FROM server_settings WHERE name='imported_2x'") is None
    db.close()
    (legacy / 'keys.json').write_text(json.dumps({'version':2,'keys':[{k: v for k, v in asdict(key).items() if k not in ('workspace_id', 'account_id')}]}))
    imported = UserStore(legacy, workspace_id=default_workspace_id(legacy))
    assert imported.verify_password('alice',PASSWORD) is not None
    assert imported.redeem_invite(invite,PASSWORD).name == 'bob'
    assert KeyStore(legacy, workspace_id=default_workspace_id(legacy)).verify(secret).user == 'alice'


@pytest.mark.parametrize('name',['users.json','keys.json'])
def test_import_rejects_planted_symlink(tmp_path,name):
    outside = tmp_path / 'outside'
    outside.write_text('private state')
    directory = tmp_path / 'server'
    directory.mkdir()
    (directory / name).symlink_to(outside)
    with pytest.raises(DatabaseError, match=f'could not import {name}'):
        KeyStore(directory, workspace_id=default_workspace_id(directory))
    assert outside.read_text() == 'private state'


def test_sessions_and_throttle_survive_restart_and_no_old_files(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY',raising=False)
    with running(tmp_path) as server:
        server.users.add('alice','admin')
        server.users.set_password('alice',PASSWORD)
        code, _, headers = password_login(server,'alice',PASSWORD)
        assert code == 200
        cookie = headers['Set-Cookie']
        _, secret = server.keys.create('admin','admin',user='alice')
        _, _, key_headers = login(server, secret)
        key_cookie = key_headers['Set-Cookie']
        for _ in range(5):
            _, ticket = server.web.throttle.begin('203.0.113.4')
            server.web.throttle.finish(ticket,'failed')
        db = server.db
    assert db.closed
    with running(tmp_path) as server:
        assert authed(server,'/api/w/default/runs',cookie=cookie)[0] == 200
        assert authed(server,'/api/w/default/runs',cookie=key_cookie)[0] == 200
        delay,ticket = server.web.throttle.begin('203.0.113.4')
        assert ticket is None and delay == 1
        server.users.set_password('alice',PASSWORD+'new')
        assert authed(server,'/api/w/default/runs',cookie=cookie)[0] == 401
        assert authed(server,'/api/w/default/runs',cookie=key_cookie)[0] == 401
    assert all(p.name.startswith('agenticml.db') or p.name in {'server.lock','tmp'}
               for p in (tmp_path / 'server').iterdir())


def test_custom_database_cli_live_server_audit_and_env_precedence(tmp_path, monkeypatch, capsys):
    data = tmp_path / 'data'
    if on_postgres():
        custom = isolate_server_dir(tmp_path / 'custom', monkeypatch)
        isolate_server_dir(tmp_path / 'unused', monkeypatch)
    else:
        custom = url(tmp_path / 'custom.db')
        monkeypatch.setenv('AGENTICML_SERVER_DATABASE_URL', url(tmp_path / 'unused.db'))
    server = make_server(data,port=0,database=custom)
    try:
        assert cli.main(['server','keys','create','--data',str(data),'--database',custom,
                         '--name','cli-key','--scope','admin']) == 0
        secret = capsys.readouterr().out.strip()
        assert server.keys.verify(secret).name == 'cli-key'
        assert cli.main(['server','audit','--data',str(data),'--database',custom,'--json']) == 0
        assert json.loads(capsys.readouterr().out)[0]['action'] == 'key.create'
        if on_postgres():
            import psycopg
            with psycopg.connect(os.environ['AGENTICML_SERVER_DATABASE_URL']) as unused:
                assert unused.execute("SELECT to_regclass('server_settings')").fetchone()[0] is None
        else:
            assert not (tmp_path / 'unused.db').exists()
        assert not (data / 'server' / 'agenticml.db').exists()
    finally:
        server.server_close()


@pytest.mark.sqlite_only
def test_wal_refusal_closes_initial_connection(tmp_path, monkeypatch):
    original = backend.SqliteDatabase._new_connection
    connections = []
    class RefuseWal:
        def __init__(self, connection): self.connection = connection
        def execute(self, sql):
            if sql == 'PRAGMA journal_mode=WAL':
                return self.connection.execute("SELECT 'delete'")
            return self.connection.execute(sql)
        def close(self): self.connection.close()
    def new_connection(db):
        connection = original(db)
        connections.append(connection)
        proxy = RefuseWal(connection)
        db._connections.remove(connection)
        db._connections.add(proxy)
        return proxy
    monkeypatch.setattr(backend.SqliteDatabase, '_new_connection', new_connection)
    with pytest.raises(DatabaseError, match='could not enable WAL'):
        connect(url(tmp_path / 'state.db'))
    import sqlite3
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')


@pytest.mark.parametrize('operation', ['save', 'load'])
def test_throttle_storage_failure_does_not_orphan_tickets(tmp_path, monkeypatch, operation):
    from agenticml.server.db.repos import ThrottleRepo
    db = connect(url(tmp_path / 'state.db'))
    throttle = LoginThrottle(clock=lambda: 0, store=DbBuckets(db))
    def fail(*args): raise DatabaseBusy('database is locked')
    with monkeypatch.context() as patch:
        patch.setattr(ThrottleRepo, operation, fail)
        with pytest.raises(DatabaseBusy): throttle.begin('a')
    assert not throttle._entries
    _, first = throttle.begin('a')
    _, second = throttle.begin('a')
    with monkeypatch.context() as patch:
        patch.setattr(ThrottleRepo, operation, fail)
        with pytest.raises(DatabaseBusy): throttle.finish(first, 'failed')
    assert first not in throttle._entries['a'][2]
    assert second in throttle._entries['a'][2]
    throttle.finish(second, 'ignored')
    assert not throttle._entries
    db.close()


def test_throttle_save_failure_does_not_lower_delay(tmp_path, monkeypatch):
    from agenticml.server.db.repos import ThrottleRepo
    db = connect(url(tmp_path / 'state.db'))
    clock = [0.0]
    throttle = LoginThrottle(clock=lambda: clock[0], store=DbBuckets(db))
    def fail_save(*args, **kwargs):
        raise DatabaseBusy('database is locked')

    for _ in range(LoginThrottle.FREE_FAILURES):
        retry, ticket = throttle.begin('203.0.113.8')
        assert ticket is not None and retry == 0
        with monkeypatch.context() as patch:
            patch.setattr(ThrottleRepo, 'save', fail_save)
            with pytest.raises(DatabaseBusy):
                throttle.finish(ticket, 'failed')
    delay, ticket = throttle.begin('203.0.113.8')
    assert ticket is None and delay >= 1
    db.close()


def test_throttle_load_failure_on_finish_does_not_lower_delay(tmp_path, monkeypatch):
    from agenticml.server.db.repos import ThrottleRepo
    db = connect(url(tmp_path / 'state.db'))
    clock = [0.0]
    throttle = LoginThrottle(clock=lambda: clock[0], store=DbBuckets(db))
    def fail_load(*args, **kwargs):
        raise DatabaseBusy('database is locked')

    for _ in range(LoginThrottle.FREE_FAILURES):
        retry, ticket = throttle.begin('203.0.113.8')
        assert ticket is not None and retry == 0
        with monkeypatch.context() as patch:
            patch.setattr(ThrottleRepo, 'load', fail_load)
            with pytest.raises(DatabaseBusy):
                throttle.finish(ticket, 'failed')
    delay, ticket = throttle.begin('203.0.113.8')
    assert ticket is None and delay >= 1
    db.close()


def test_webauth_uses_keys_database_for_default_users(tmp_path):
    from agenticml.server.webauth import WebAuth
    db = connect(url(tmp_path / 'custom.db'))
    keys = KeyStore(tmp_path, db=db, workspace_id=default_workspace_id(db))
    _, secret = keys.create('admin','admin')
    web = WebAuth(keys)
    assert web.users.db is db
    _, entry = web._create_session(keys.verify(secret))
    assert web._principal_from_session(entry).user == 'admin'
    assert not (tmp_path / 'agenticml.db').exists()
    db.close()


def test_import_skips_bad_audit_field_types_and_logs_one_count(tmp_path, caplog):
    row = {'ts': layout.format_timestamp(), 'actor': []}
    good = {**row, 'actor':'alice'}
    (tmp_path / 'audit.jsonl').write_text(json.dumps(row)+'\n'+json.dumps(good)+'\n')
    db = for_server_dir(tmp_path)
    assert [r['actor'] for r in AuditRepo(db, default_workspace_id(db)).tail(10)] == ['alice']
    assert 'skipped 1 malformed or unreadable legacy audit line(s)' in caplog.text


@pytest.mark.parametrize('failure', ['throttle_begin', 'throttle_finish', 'key_read', 'user_read'])
def test_bearer_database_failure_is_private_503(tmp_path, monkeypatch, failure, caplog):
    from test_server_ingest import request
    with running(tmp_path) as server:
        _, secret = server.keys.create('box')
        def fail(*args, **kwargs): raise DatabaseBusy('private SQL or database URL')
        if failure.startswith('throttle_'):
            monkeypatch.setattr(server.web.throttle, failure.removeprefix('throttle_'), fail)
        elif failure == 'key_read': monkeypatch.setattr(server.keys.verifier, 'by_digest', fail)
        else: monkeypatch.setattr(type(server.users.repo), 'get', fail)
        code, body, _ = request(server, '/v2/whoami', key=secret)
        assert code == 503 and body == {'error':'key store unavailable'}
        assert 'private SQL or database URL' not in caplog.text


def test_installed_unit_keeps_custom_database_without_printing_it(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_DATABASE_URL', raising=False)
    from agenticml.server.up import (
        _ENV_FILE_MARKER, launchd_plist_path, systemd_env_path, systemd_unit_path,
        write_launchd_plist, write_systemd_unit,
    )
    previous = os.umask(0o022)
    try:
        os.chmod(tmp_path, 0o755)
        database = 'sqlite:///' + str(tmp_path / 'private state.db')
        unit_path = systemd_unit_path(tmp_path)
        env_path = systemd_env_path(tmp_path)
        plist_path = launchd_plist_path(tmp_path)
        for stub in (unit_path, env_path, plist_path):
            stub.parent.mkdir(parents=True, exist_ok=True)
            stub.write_bytes(b'placeholder\n')
            stub.chmod(0o644)
        path = write_systemd_unit(
            tmp_path / 'data', 8080, home=tmp_path, force=True, database=database)
        text = path.read_text()
        assert '--database' not in text and database not in text
        env_line = 'EnvironmentFile=' + str(env_path).replace('%', '%%') + '\n'
        assert env_line in text
        assert f'EnvironmentFile="{env_path}"' not in text
        env_text = env_path.read_text()
        assert env_text.startswith(_ENV_FILE_MARKER + '\n')
        assert 'AGENTICML_SERVER_DATABASE_URL=' in env_text and database in env_text
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        assert database not in capsys.readouterr().out
        plist_path = write_launchd_plist(
            tmp_path / 'data', 8080, home=tmp_path, force=True, database=database)
        payload = plistlib.loads(plist_path.read_bytes())
        argv = payload['ProgramArguments']
        assert '--database' not in argv and database not in argv
        assert payload['EnvironmentVariables']['AGENTICML_SERVER_DATABASE_URL'] == database
        assert stat.S_IMODE(plist_path.stat().st_mode) == 0o600
        assert database not in capsys.readouterr().out
    finally:
        os.umask(previous)


@pytest.mark.parametrize('stage', ['body', 'commit'])
@pytest.mark.sqlite_only
def test_failed_rollback_discards_connection_and_preserves_safe_error(tmp_path, monkeypatch, stage):
    import sqlite3
    original = backend.sqlite3.connect
    connections = []
    class BrokenRollback(sqlite3.Connection):
        fail_rollback = False
        fail_commit = False
        def commit(self):
            if self.fail_commit:
                raise sqlite3.OperationalError('private commit details')
            return super().commit()
        def rollback(self):
            if self.fail_rollback:
                raise sqlite3.OperationalError('private rollback details')
            return super().rollback()
    def create(*args, **kwargs):
        connection = original(*args, **kwargs, factory=BrokenRollback)
        connections.append(connection)
        return connection
    monkeypatch.setattr(backend.sqlite3, 'connect', create)
    db = connect(url(tmp_path / 'state.db'))
    broken = connections[0]
    broken.fail_rollback = True
    broken.fail_commit = stage == 'commit'
    with pytest.raises(DatabaseError, match='could not read or write database') as error:
        with db.transaction(write=True):
            if stage == 'body': raise sqlite3.OperationalError('private body details')
    assert 'private' not in str(error.value)
    assert broken not in db._connections and broken not in db._idle
    with db.transaction() as tx: assert tx.fetchone('SELECT 1')[0] == 1
    assert len(connections) == 2
    db.close()


def test_schema_enforces_uniqueness_and_foreign_keys(tmp_path):
    from dataclasses import replace
    users = UserStore(tmp_path, workspace_id=default_workspace_id(tmp_path))
    alice = users.add('alice', 'admin', identity='Alice@example.com')
    key, _ = KeyStore(tmp_path, workspace_id=default_workspace_id(tmp_path)).create('one', user='alice')
    for duplicate in [replace(alice, name='other'),
                      replace(alice, name='other', id='0'*32, identity='ALICE@EXAMPLE.COM')]:
        with pytest.raises(DatabaseError):
            with users.db.transaction(write=True) as tx:
                users.repo.save(tx, duplicate)
    keys = KeyStore(tmp_path, workspace_id=default_workspace_id(tmp_path))
    for invalid in [replace(key,name='duplicate'), replace(key,name='orphan',sha256='0'*64,user='missing')]:
        with pytest.raises(DatabaseError):
            with keys.db.transaction(write=True) as tx:
                keys.repo.insert(tx,invalid)
    assert users.list() == [alice] and keys.list() == [key]


def test_discard_cascades_keys_without_deleting_reused_name(tmp_path):
    users = UserStore(tmp_path, workspace_id=default_workspace_id(tmp_path))
    original = users.add('alice','admin')
    keys = KeyStore(tmp_path, workspace_id=default_workspace_id(tmp_path))
    _, secret = keys.create('box',user='alice')
    users.discard('alice',original.id)
    assert keys.verify(secret) is None
    replacement = users.add('alice','admin')
    keys.create('new-box',user='alice')
    users.discard('alice',original.id)
    assert users.get('alice') == replacement and len(keys.list()) == 1


def test_import_skips_orphan_keys_and_logs_count(tmp_path, caplog, monkeypatch):
    from dataclasses import replace
    source = tmp_path / 'source'
    source.mkdir()
    users = UserStore(source, workspace_id=default_workspace_id(source))
    users.add('alice', 'admin')
    key, secret = KeyStore(source, workspace_id=default_workspace_id(source)).create('laptop', user='alice')
    orphan = replace(key, name='orphan', sha256='a' * 64, prefix='orphaned', user='missing')
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    isolate_server_dir(legacy, monkeypatch)
    (legacy / 'users.json').write_text(json.dumps({'version': 1, 'users': [dict(asdict(r), role='admin' if r.role == 'owner' else r.role) for r in users.list()]}))
    (legacy / 'keys.json').write_text(json.dumps({'version': 2, 'keys': [{k: v for k, v in asdict(key).items() if k not in ('workspace_id', 'account_id')}, {k: v for k, v in asdict(orphan).items() if k not in ('workspace_id', 'account_id')}]}))
    imported = KeyStore(legacy, workspace_id=default_workspace_id(legacy))
    assert {record.name for record in imported.list()} == {'laptop'}
    assert imported.verify(secret).user == 'alice'
    text = caplog.text
    assert 'skipped 1 key' in text
    assert secret not in text and 'a' * 64 not in text


def test_import_does_not_overwrite_existing_2x_archive(tmp_path, caplog):
    prepare_data_dir(tmp_path)
    directory = tmp_path / 'server'
    write_v1_keys(directory / 'keys.json', ['old'], scopes=['admin'])
    archive = directory / 'keys.json.2x'
    archive.write_text('keep me')
    for_server_dir(directory)
    assert archive.read_text() == 'keep me'
    assert (directory / 'keys.json').exists()
    assert 'already exists' in caplog.text


def test_import_refuses_expensive_scrypt_params(tmp_path, monkeypatch):
    from agenticml.server.users import _password_record
    source = tmp_path / 'source'
    source.mkdir()
    users = UserStore(source, workspace_id=default_workspace_id(source))
    users.add('alice', 'admin')
    record = dict(asdict(users.get('alice')), role='admin')
    hostile = dict(_password_record(PASSWORD), n=2 ** 20)
    record['password'] = hostile
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    isolate_server_dir(legacy, monkeypatch)
    (legacy / 'users.json').write_text(json.dumps({'version': 1, 'users': [record]}))
    with pytest.raises(DatabaseError, match='could not import users.json'):
        UserStore(legacy, workspace_id=default_workspace_id(legacy))


def test_advisory_lock_hooks_for_last_admin_and_session_create(tmp_path, monkeypatch):
    from agenticml.server.db.dialect import SqliteDialect
    from agenticml.server.db.repos import SessionRepo
    names = []
    from agenticml.server.db.dialect import PostgresDialect
    from server_db_helpers import on_postgres
    dialect = PostgresDialect if on_postgres() else SqliteDialect
    original = dialect.advisory_lock

    def spy(self, tx, name):
        names.append(name)
        return original(self, tx, name)

    monkeypatch.setattr(dialect, 'advisory_lock', spy)
    users = UserStore(tmp_path, workspace_id=default_workspace_id(tmp_path))
    users.add('alice', 'admin')
    users.add('bob', 'admin')
    names.clear()
    users.set_role('bob', 'member')
    assert names == ['users']
    keys = KeyStore(tmp_path, db=users.db, workspace_id=default_workspace_id(users.db))
    names.clear()
    keys.create('box', user='alice')
    assert names == ['users']
    names.clear()
    keys.revoke('box', keep_admin=True)
    assert names == ['users']
    token = users.create_invite('bob')
    names.clear()
    users.redeem_invite(token, PASSWORD)
    assert names == ['users']
    names.clear()
    SessionRepo(users.db).create('b' * 64, {
        'user': 'alice', 'id': users.get('alice').id, 'generation': 0, 'via': 'password',
        'key': None, 'key_sha256': None, 'created': 1.0, 'last_seen': 1.0,
    }, 1000)
    assert names == ['sessions']


def test_server_constructor_opens_database_and_close_does_not_raise(tmp_path):
    from agenticml.server.app import IngestServer
    prepare_data_dir(tmp_path)
    server = IngestServer(tmp_path, '127.0.0.1', 0)
    assert server.db is not None
    server.server_close()


@pytest.mark.sqlite_only
def test_warns_when_existing_database_is_group_accessible(tmp_path, caplog):
    path = tmp_path / 'state.db'
    connect(url(path)).close()
    os.chmod(path, 0o644)
    caplog.clear()
    connect(url(path)).close()
    assert 'group- or world-accessible' in caplog.text


def test_cli_module_does_not_import_server_package():
    import subprocess
    import sys
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    subprocess.check_call(
        [sys.executable, '-c',
         'import agenticml.cli, sys; assert "agenticml.server" not in sys.modules'],
        env=env)


def test_corrupt_password_json_is_store_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'data') as server:
        server.users.add('alice', 'admin')
        server.users.set_password('alice', PASSWORD)
        with server.db.transaction(write=True) as tx:
            tx.execute("UPDATE accounts SET password='not-json' WHERE login='alice'")
        with pytest.raises(KeyStoreUnavailable):
            server.users.get('alice')
        code, body, _ = password_login(server, 'alice', PASSWORD)
        assert code == 503 and body == {'error': 'key store unavailable'}


def sql_quoted_placeholders(sql):
    """`'...'` segments of SQL-looking text that contain a `?`."""
    import re
    if not re.search(r'\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER)\b', sql):
        return []
    return [segment for segment in re.findall(r"'[^']*'", sql) if '?' in segment]


def test_repositories_do_not_embed_question_marks_in_sql_literals():
    import ast
    assert sql_quoted_placeholders("SELECT * FROM t WHERE name=?") == []
    assert sql_quoted_placeholders("SELECT * FROM t WHERE name='?'") == ["'?'"]
    directory = Path(__file__).resolve().parents[1] / 'src/agenticml/server/db'
    bad = []
    for path in (*directory.glob('*.py'), *(directory / 'migrations').glob('*.py')):
        if path.name in ('dialect.py', 'sqlite.py', 'postgres.py'):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for literal in sql_quoted_placeholders(node.value):
                    bad.append(f'{path.name}: {literal}')
    assert bad == []

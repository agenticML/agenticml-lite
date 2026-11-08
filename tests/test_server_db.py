"""Database boundaries, durability, concurrency, and one-time 2.x import."""
from server_db_helpers import default_store
from server_db_helpers import default_workspace_id
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
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
    return 'sqlite:///' + str(path)


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
    expected = url(tmp_path / 'server' / 'agenticml.db')
    assert default_url(tmp_path) == expected
    assert resolve_url(tmp_path) == expected
    monkeypatch.setenv('AGENTICML_SERVER_DATABASE_URL', url(tmp_path / 'env.db'))
    assert resolve_url(tmp_path) == url(tmp_path / 'env.db')
    assert resolve_url(tmp_path, 'explicit') == 'explicit'
    for bad in ['postgresql://user:private-password@host/db', 'sqlite:///relative.db']:
        with pytest.raises(DatabaseError) as error: connect(bad)
        assert bad not in str(error.value) and 'private-password' not in str(error.value)
        assert cli.main(['server','keys','list','--data',str(tmp_path),'--database',bad]) == 1
        assert 'private-password' not in capsys.readouterr().err
    with pytest.raises(DatabaseError, match='parent must exist'):
        connect(url(tmp_path / 'absent' / 'state.db'))


def test_migrations_concurrent_idempotent_and_ordered(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    # WAL initialization uses the same busy timeout as every transaction.
    def open_db(_): return connect(url(path))
    with ThreadPoolExecutor(2) as pool:
        databases = list(pool.map(open_db, range(2)))
    with databases[0].transaction() as tx:
        assert [r['version'] for r in tx.fetchall('SELECT * FROM schema_migrations')] == [1, 2, 3]
    for db in databases: db.close()
    from agenticml.server.db import migrate
    migrations = tmp_path / 'migrations'
    migrations.mkdir()
    source = Path(migrate.MIGRATIONS) / '0001_initial.sql'
    (migrations / source.name).write_text(source.read_text())
    (migrations / '0002_second.sql').write_text('CREATE TABLE second (id {{id}}, value {{blob}});\n')
    (migrations / '0003_third.sql').write_text('CREATE TABLE third (value {{real}});\n')
    monkeypatch.setattr(migrate, 'MIGRATIONS', migrations)
    db = connect(url(path))
    with db.transaction() as tx:
        assert [r[0] for r in tx.fetchall('SELECT version FROM schema_migrations ORDER BY version')] == [1,2,3]
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
        assert tx.fetchone("SELECT name FROM sqlite_master WHERE name='must_rollback'") is None
        assert tx.fetchone('SELECT MAX(version) FROM schema_migrations')[0] == 1
    db.close()


def test_schema_too_new_server_and_cli_refuse(tmp_path, capsys):
    prepare_data_dir(tmp_path)
    db = for_server_dir(tmp_path / 'server')
    with db.transaction(write=True) as tx:
        tx.execute('INSERT INTO schema_migrations VALUES (?, ?, ?)', (999,'future',layout.format_timestamp()))
    db.close()
    with pytest.raises(SchemaTooNew, match='knows up to 3'):
        make_server(tmp_path, port=0)
    for command in [('keys','list'),('users','list'),('audit',),('serve',),('status',),('verify',)]:
        assert cli.main(['server',*command,'--data',str(tmp_path)]) == 1
        assert 'database schema version 999' in capsys.readouterr().err


def test_missing_status_does_not_create_database(tmp_path):
    from agenticml.server.admin import status
    prepare_data_dir(tmp_path)
    assert for_server_dir(tmp_path / 'server', create=False) is None
    report = status(tmp_path)
    assert report['keys'] == report['users'] == []
    assert not (tmp_path / 'server' / 'agenticml.db').exists()


def test_busy_maps_to_store_unavailable_and_create_is_atomic(tmp_path):
    directory = tmp_path / 'server'
    directory.mkdir()
    keys = KeyStore(directory, workspace_id=default_workspace_id(directory))
    other = connect(url(keys.db.path))
    # Shorten only the test connection's timeout (production is five seconds).
    with keys.db.transaction() as tx: tx.execute('PRAGMA busy_timeout=1')
    with other.transaction(write=True):
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


def test_import_22_password_invite_and_rollback_on_bad_keys(tmp_path):
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
        assert authed(server,'/api/runs',cookie=cookie)[0] == 200
        assert authed(server,'/api/runs',cookie=key_cookie)[0] == 200
        delay,ticket = server.web.throttle.begin('203.0.113.4')
        assert ticket is None and delay == 1
        server.users.set_password('alice',PASSWORD+'new')
        assert authed(server,'/api/runs',cookie=cookie)[0] == 401
        assert authed(server,'/api/runs',cookie=key_cookie)[0] == 401
    assert all(p.name.startswith('agenticml.db') or p.name in {'server.lock','tmp'}
               for p in (tmp_path / 'server').iterdir())


def test_custom_database_cli_live_server_audit_and_env_precedence(tmp_path, monkeypatch, capsys):
    data = tmp_path / 'data'
    custom = url(tmp_path / 'custom.db')
    monkeypatch.setenv('AGENTICML_SERVER_DATABASE_URL',url(tmp_path / 'unused.db'))
    server = make_server(data,port=0,database=custom)
    try:
        assert cli.main(['server','keys','create','--data',str(data),'--database',custom,
                         '--name','cli-key','--scope','admin']) == 0
        secret = capsys.readouterr().out.strip()
        assert server.keys.verify(secret).name == 'cli-key'
        assert cli.main(['server','audit','--data',str(data),'--database',custom,'--json']) == 0
        assert json.loads(capsys.readouterr().out)[0]['action'] == 'key.create'
        assert not (tmp_path / 'unused.db').exists()
        assert not (data / 'server' / 'agenticml.db').exists()
    finally:
        server.server_close()


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


def test_installed_unit_keeps_custom_database_without_printing_it(tmp_path, capsys):
    from agenticml.server.up import write_systemd_unit
    database = url(tmp_path / 'private state.db')
    path = write_systemd_unit(tmp_path / 'data', 8080, home=tmp_path, database=database)
    assert '--database' in path.read_text() and database in path.read_text()
    assert database not in capsys.readouterr().out


@pytest.mark.parametrize('stage', ['body', 'commit'])
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


def test_import_skips_orphan_keys_and_logs_count(tmp_path, caplog):
    from dataclasses import replace
    source = tmp_path / 'source'
    source.mkdir()
    users = UserStore(source, workspace_id=default_workspace_id(source))
    users.add('alice', 'admin')
    key, secret = KeyStore(source, workspace_id=default_workspace_id(source)).create('laptop', user='alice')
    orphan = replace(key, name='orphan', sha256='a' * 64, prefix='orphaned', user='missing')
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
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


def test_import_refuses_expensive_scrypt_params(tmp_path):
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
    (legacy / 'users.json').write_text(json.dumps({'version': 1, 'users': [record]}))
    with pytest.raises(DatabaseError, match='could not import users.json'):
        UserStore(legacy, workspace_id=default_workspace_id(legacy))


def test_advisory_lock_hooks_for_last_admin_and_session_create(tmp_path, monkeypatch):
    from agenticml.server.db.dialect import SqliteDialect
    from agenticml.server.db.repos import SessionRepo
    names = []
    original = SqliteDialect.advisory_lock

    def spy(self, tx, name):
        names.append(name)
        return original(self, tx, name)

    monkeypatch.setattr(SqliteDialect, 'advisory_lock', spy)
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

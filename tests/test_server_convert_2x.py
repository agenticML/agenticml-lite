"""A generated 2.2 data directory converts, syncs, refuses a second conversion and survives a kill."""
from pathlib import Path
import hashlib
import json
import shutil

import pytest

from agenticml import cli
from agenticml.client import Client
from agenticml.server import admin
from agenticml.server.app import make_server
from agenticml.server.db import ConvertedTwice, for_server_dir, import2x
from agenticml.server.db.repos import AuditRepo, NoteRepo, RunRepo, SettingsRepo
from agenticml.server.db.workspaces import AccountRepo, MembershipRepo
from agenticml.server.keys import KeyStore
from agenticml.server.users import UserStore
from agenticml.store import RunReader
from agenticml import sync
from server_db_helpers import default_store, default_workspace_id, on_postgres, write_22_data_dir
from test_server_signin import running
from test_server_web import authed, login
from test_sync import allowed


def converted_store(data):
    """Workspace store path without opening (and so without re-checking) the database."""
    return next((data / 'workspaces').glob('*/store'))


def assert_summaries_backfilled(db, wid, data):
    """Every committed series .bin of every run has a summary matching its bytes."""
    from agenticml.server.metrics import empty_summary, fold
    checked = 0
    repo = RunRepo(db, wid)
    for row in repo.all_rows():
        rows = {m['series_id']: m for m in repo.metrics(row['run_id'])}
        for path, info in repo.files(row['run_id']).items():
            if not (path.startswith('scalars/') and path.endswith('.bin') and path != 'scalars/_time.bin'):
                continue
            body = (default_store(data) / 'runs' / row['run_id'] / path).read_bytes()[:info['size']]
            expected = fold(empty_summary(), body)
            found = rows[int(path[8:-4])]
            assert expected['count'] > 0
            assert {k: found[k] for k in expected} == expected, (row['run_id'], path)
            checked += 1
    assert checked >= 2


def open_db(data):
    db = for_server_dir(data / 'server', create=False)
    return db, default_workspace_id(db)


def close_all(data):
    from agenticml.server import db as package
    with package._registry_lock:
        for database in list(package._registry.values()):
            database.close()
        package._registry.clear()


@pytest.fixture
def old(tmp_path):
    data = tmp_path / 'data'
    secrets, writers = write_22_data_dir(data, tmp_path / 'local')
    return data, secrets, writers


def test_conversion_end_to_end_and_sync(old, tmp_path, monkeypatch):
    data, secrets, writers = old
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    with running(data) as server:
        wid = default_workspace_id(server.db)
        users = {u.name: u.role for u in UserStore(data / 'server', db=server.db, workspace_id=wid).list()}
        assert users == {'alice': 'owner', 'dave': 'admin', 'bob': 'member', 'carol': 'viewer'}
        accounts = {row['login'] for row in AccountRepo(server.db).list()}
        assert {'alice', 'dave', 'bob', 'carol'} <= accounts
        members = {row['login']: row['role'] for row in MembershipRepo(server.db, wid).list()}
        assert members == {'alice': 'owner', 'dave': 'admin', 'bob': 'member', 'carol': 'viewer'}
        keys = KeyStore(data / 'server', db=server.db, workspace_id=wid)
        for name, (user, scope) in {'alice-admin': ('alice', 'admin'), 'bob-sync': ('bob', 'sync'),
                                    'carol-sync': ('carol', 'sync')}.items():
            record = keys.verify(secrets[name])
            assert (record.name, record.user, record.scope) == (name, user, scope)
        actions = {row['action'] for row in AuditRepo(server.db, wid).tail(20)}
        assert {'key.create', 'login.ok'} <= actions
        rows = {row['run_id']: row for row in RunRepo(server.db, wid).all_rows()}
        assert set(rows) == {'old-1', 'old-2'}
        assert all(row['owner_user'] == 'bob' and row['owner_key'] == 'bob-sync' and row['status'] == 'running'
                   for row in rows.values())
        for run_id in rows:
            committed = RunRepo(server.db, wid).files(run_id)
            assert committed and all(
                (default_store(data) / 'runs' / run_id / name).stat().st_size == info['size']
                for name, info in committed.items())
        assert NoteRepo(server.db, wid).read('old-1')['text'] == 'first imported note\n'
        settings = SettingsRepo(server.db)
        assert settings.get('imported_2x') and settings.get('imported_runs_2x')
        assert not (data / 'store').exists()
        for name in ('users.json', 'keys.json', 'audit.jsonl', 'setup-closed'):
            assert not (data / 'server' / name).exists() and (data / 'server' / (name + '.2x')).exists()
        for run_id in rows:
            run = default_store(data) / 'runs' / run_id
            assert (run / 'remote.json.2x').exists() and not (run / 'remote.json').exists()
        assert (default_store(data) / 'runs' / 'old-1' / 'notes.md.2x').exists()
        report = admin.verify(data)
        assert report['ok'] and not report['problems'], report

        # A sync client (protocol 2 is unchanged in 3.0) continues an imported run and starts a new one.
        url = 'http://127.0.0.1:%d' % server.server_address[1]
        with Client(url, secrets['bob-sync'], timeout=5, max_retries=0) as client:
            writer = writers['old-1']
            writer.write_scalars([(i, {'loss': 1 / (i + 1)}) for i in range(8, 20)])
            writer.finish('finished')
            result = sync.SyncResult('old-1')
            sync.sync_run(RunReader(writer.root, 'old-1'), url, client, result)
            assert result.complete
            from agenticml.store import RunWriter
            fresh = RunWriter.create(tmp_path / 'local', run_id='new-1', meta={'project': 'demo'})
            fresh.write_scalars([(i, {'loss': float(i)}) for i in range(5)])
            fresh.finish('finished')
            result = sync.SyncResult('new-1')
            sync.sync_run(RunReader(fresh.root, 'new-1'), url, client, result)
            assert result.complete
        for run_id, source in (('old-1', writers['old-1']), ('new-1', fresh)):
            assert allowed(default_store(data) / 'runs' / run_id) == allowed(source.run_dir)
        assert RunRepo(server.db, wid).as_remote('old-1')['status'] == 'finished'
        cookie = login(server, secrets['alice-admin'])[2]['Set-Cookie']
        code, _, body, _ = authed(server, '/api/w/default/runs', cookie=cookie)
        assert code == 200
        assert {row['run_id'] for row in json.loads(body)} == {'old-1', 'old-2', 'new-1'}
        report = admin.verify(data)
        assert report['ok'] and not report['problems'], report
        assert_summaries_backfilled(server.db, wid, data)


@pytest.mark.parametrize('name', ['users.json', 'keys.json', 'audit.jsonl'])
def test_second_conversion_is_refused_for_each_file(old, name, capsys):
    data, _, _ = old
    for_server_dir(data / 'server').close()
    close_all(data)
    planted = data / 'server' / name
    planted.write_text('{"version": 1}')
    with pytest.raises(ConvertedTwice, match=rf'found 2\.x .*{name} after this server was converted on '
                                              rf'\d{{4}}-\d\d-\d\d; move it away \(the converted copy is .*{name}\.2x\)'):
        for_server_dir(data / 'server')
    assert planted.read_text() == '{"version": 1}'
    before = {p: p.read_bytes() for p in (data / 'server').glob('*.2x')}
    for argv in (['status'], ['verify'], ['keys', 'list'], ['users', 'list'], ['reindex'],
                 ['workspaces', 'list'], ['audit'], ['backup', '--to', str(data.parent / 'bk'), *(['--files-only'] if on_postgres() else [])]):
        assert cli.main(['server', *argv, '--data', str(data)]) in (1,), argv
        assert 'after this server was converted' in capsys.readouterr().err, argv
    with pytest.raises(ConvertedTwice):
        make_server(data, port=0)
    assert cli.main(['server', 'serve', '--data', str(data), '--port', '0']) == 1
    assert 'after this server was converted' in capsys.readouterr().err
    assert {p: p.read_bytes() for p in (data / 'server').glob('*.2x')} == before
    assert planted.read_text() == '{"version": 1}'


@pytest.mark.parametrize('relative', ['store/runs', 'runs'])
def test_second_conversion_is_refused_for_reappearing_runs(old, relative):
    data, _, _ = old
    for_server_dir(data / 'server').close()
    close_all(data)
    run = data / relative / 'ghost-run'
    run.mkdir(parents=True)
    (run / 'meta.json').write_text('{}')
    with pytest.raises(ConvertedTwice, match='found 2.x .*after this server was converted on'):
        for_server_dir(data / 'server')
    assert (run / 'meta.json').read_text() == '{}'
    assert {p.name for p in (converted_store(data) / 'runs').iterdir()} == {'old-1', 'old-2'}


def test_empty_legacy_directories_do_not_refuse(old):
    data, _, _ = old
    for_server_dir(data / 'server').close()
    close_all(data)
    (data / 'store' / 'runs').mkdir(parents=True)
    assert for_server_dir(data / 'server') is not None


@pytest.mark.parametrize('stage', ['users', 'runs'])
def test_interrupted_conversion_completes_without_importing_twice(old, stage, monkeypatch):
    data, secrets, _ = old

    def killed(name):
        if name == stage:
            raise RuntimeError('killed between commit and rename')

    original = import2x._after_commit
    monkeypatch.setattr(import2x, '_after_commit', killed)
    with pytest.raises(RuntimeError, match='killed'):
        for_server_dir(data / 'server')
    close_all(data)
    if stage == 'users':
        assert (data / 'server' / 'users.json').exists() and not (data / 'server' / 'users.json.2x').exists()
    else:
        run = converted_store(data) / 'runs' / 'old-1'
        assert (run / 'remote.json').exists() and not (run / 'remote.json.2x').exists()
    monkeypatch.setattr(import2x, '_after_commit', original)
    db = for_server_dir(data / 'server')
    wid = default_workspace_id(db)
    for name in ('users.json', 'keys.json', 'audit.jsonl'):
        assert not (data / 'server' / name).exists() and (data / 'server' / (name + '.2x')).exists()
    for run_id in ('old-1', 'old-2'):
        run = default_store(data) / 'runs' / run_id
        assert not (run / 'remote.json').exists() and (run / 'remote.json.2x').exists()
    assert (default_store(data) / 'runs' / 'old-1' / 'notes.md.2x').exists()
    assert len(UserStore(data / 'server', db=db, workspace_id=wid).list()) == 4
    assert len(KeyStore(data / 'server', db=db, workspace_id=wid).list()) == 3
    assert len(AuditRepo(db, wid).tail(50)) == 2 + len([r for r in AuditRepo(db, wid).tail(50)
                                                       if r['action'] == 'migration'])
    assert {row['run_id'] for row in RunRepo(db, wid).all_rows()} == {'old-1', 'old-2'}
    assert admin.verify(data)['ok']
    assert_summaries_backfilled(db, wid, data)
    close_all(data)
    # A third start neither refuses nor changes anything.
    assert for_server_dir(data / 'server') is not None

"""18.1a storage migration, scoped repositories, sync boundaries and operator CLI."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import shutil

import pytest

from agenticml import cli, lineage
from agenticml.client import Client
from agenticml.server.access import (bind_sync, WorkspaceNotFound, require_workspace_member,
                                      require_workspace_manager)
from agenticml.server.app import make_server, prepare_data_dir
from agenticml.server.db import connect, for_server_dir, DatabaseError
from agenticml.server.db.repos import RunRepo, NoteRepo, KeyRepo, UserRepo, AuditRepo, SettingsRepo
from agenticml.server.db.workspaces import AccountRepo, WorkspaceRepo
from agenticml.server.keys import KeyStore, LastAdminError
from agenticml.server.users import UserStore
from agenticml.server.workspaces import store_root, prepare_store
from agenticml.store import RunWriter, RunReader, layout
from test_server_ingest import running, request, put_meta
from test_server_web import login, authed


@pytest.fixture
def two(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'data') as server:
        server.users.add('alice', 'admin')
        server.users.add('bob', 'admin')
        a = WorkspaceRepo(server.db).create('alpha', owner='alice')
        b = WorkspaceRepo(server.db).create('beta', owner='bob')
        for ws in (a, b):
            prepare_store(server.data_dir, ws['id'])
        keys_a = KeyStore(server.data_dir / 'server', db=server.db, workspace_id=a['id'])
        keys_b = KeyStore(server.data_dir / 'server', db=server.db, workspace_id=b['id'])
        ra, ka = keys_a.create('laptop', user='alice')
        rb, kb = keys_b.create('laptop', user='bob')
        server.test_key = ka
        yield server, a, b, keys_a, keys_b, ka, kb


def test_scoped_repositories_require_workspace(tmp_path):
    prepare_data_dir(tmp_path)
    db = for_server_dir(tmp_path / 'server')
    for repo, args in ((RunRepo, (db,)), (NoteRepo, (db,)), (KeyRepo, (db,)),
                       (UserRepo, (db,)), (AuditRepo, (db,))):
        with pytest.raises(TypeError):
            repo(*args)
        with pytest.raises(ValueError, match='workspace id'):
            repo(*args, '')
    db.close()


def test_key_digest_is_global_name_and_revocation_are_scoped(two):
    server, a, b, keys_a, keys_b, ka, kb = two
    assert server.keys.verify(ka).workspace_id == a['id']
    assert server.keys.verify(kb).workspace_id == b['id']
    assert [r.name for r in keys_a.list()] == [r.name for r in keys_b.list()] == ['laptop']
    assert request(server, '/v2/whoami', key=ka)[1]['workspace'] == 'alpha'
    assert request(server, '/v2/whoami', key=kb)[1]['workspace'] == 'beta'
    keys_a.revoke('laptop')
    assert request(server, '/v2/whoami', key=ka)[0] == 401
    assert request(server, '/v2/whoami', key=kb)[0] == 200
    from dataclasses import replace
    duplicate = replace(keys_b.list()[0], user='alice')
    with pytest.raises(DatabaseError):
        with server.db.transaction(write=True) as tx:
            keys_a.repo.insert(tx, duplicate)


def test_sync_state_append_commit_objects_and_same_id_stay_separate(two):
    server, a, b, _, _, ka, kb = two
    assert put_meta(server, 'secret', key=kb)[0] == 200
    for path, method, body in [
        ('/v2/runs/secret/state', 'GET', None),
        ('/v2/runs/secret/append/events.jsonl?offset=0', 'POST', b'B data'),
        ('/v2/runs/secret/commit', 'POST', b'{}'),
        ('/v2/runs/secret/lineage', 'GET', None),
        ('/v2/runs/secret/lineage', 'PUT', b'not a bundle'),
    ]:
        status, payload, _ = request(server, path, method, body, ka)
        assert status == 404 and payload == {'error': 'unknown run'}
    sha = hashlib.sha256(b'only beta knows').hexdigest()
    assert request(server, '/v2/objects/' + sha, 'PUT', b'only beta knows', kb)[0] in (200, 201)
    missing = json.dumps({'objects': [sha]}).encode()
    assert request(server, '/v2/objects/missing', 'POST', missing, ka)[1] == {'missing': [sha]}
    assert request(server, '/v2/objects/missing', 'POST', missing, kb)[1] == {'missing': []}
    assert not layout.object_path(store_root(server.data_dir, a['id']), sha).exists()

    def sync_one(key, text):
        assert put_meta(server, 'same', status='finished', key=key)[0] == 200
        status, payload, _ = request(server, '/v2/runs/same/append/events.jsonl?offset=0', 'POST', text, key)
        assert status == 200 and payload['size'] == len(text)
        state = request(server, '/v2/runs/same/state', key=key)[1]
        sizes = {name: info['size'] for name, info in state['files'].items()}
        payload = json.dumps({'status': 'finished', 'sizes': sizes, 'fingerprint': 'a' * 64}).encode()
        assert request(server, '/v2/runs/same/commit', 'POST', payload, key)[0] == 200
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda pair: sync_one(*pair), [(ka, b'alpha\n'), (kb, b'beta\n')]))
    for ws, text, owner in ((a, b'alpha\n', 'alice'), (b, b'beta\n', 'bob')):
        index = RunRepo(server.db, ws['id'])
        assert index.get('same')['owner_user'] == owner
        assert index.committed_size('same', 'events.jsonl') == len(text)
        assert (store_root(server.data_dir, ws['id']) / 'runs/same/events.jsonl').read_bytes() == text
        assert index.get('same')['status'] == 'finished'
        note = NoteRepo(server.db, ws['id'])
        note.write('same', owner, None)
        assert note.read('same')['text'] == owner
        assert {r['owner'] for r in index.dashboard_list()} == {owner}
    assert server.run_index.ids() == set()
    assert server.run_lock('same', workspace_id=a['id']) is not server.run_lock('same', workspace_id=b['id'])
    assert server.object_lock(sha, workspace_id=a['id']) is not server.object_lock(sha, workspace_id=b['id'])


def test_lineage_receive_and_state_are_scoped(two, tmp_path):
    if shutil.which('git') is None:
        pytest.skip('requires git')
    server, a, b, _, _, ka, kb = two
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'train.py').write_text('x = 1\n')
    local = tmp_path / 'local'
    writer = RunWriter.create(local, run_id='lineage-run', meta={'project': 'demo'})
    info = lineage.snapshot(local, 'demo', source, 'lineage-run')
    assert info['error'] is None
    writer._update_meta({'lineage': info})
    writer.finish('finished')
    writer.close()
    client_b = Client(f'http://127.0.0.1:{server.server_address[1]}', kb)
    client_a = Client(f'http://127.0.0.1:{server.server_address[1]}', ka)
    data = json.dumps(RunReader(local, 'lineage-run').meta()).encode()
    client_b.put_file('lineage-run', 'meta.json', data)
    bundle = tmp_path / 'snapshot.bundle'
    lineage.bundle_for_run(local, 'lineage-run', (), bundle)
    body = bundle.read_bytes()
    client_b.put_lineage('lineage-run', io.BytesIO(body), len(body))
    assert request(server, '/v2/runs/lineage-run/lineage', key=ka)[0] == 404
    client_a.put_file('lineage-run', 'meta.json', data)
    before = request(server, '/v2/runs/lineage-run/lineage', key=ka)[1]
    assert before['tips'] == []
    client_a.put_lineage('lineage-run', io.BytesIO(body), len(body))
    assert request(server, '/v2/runs/lineage-run/lineage', key=ka)[1] == request(
        server, '/v2/runs/lineage-run/lineage', key=kb)[1]
    for ws in (a, b):
        assert lineage._repo_path(store_root(server.data_dir, ws['id']), info['project_key']).is_dir()


def test_suspend_remove_member_rename_and_access(two):
    server, a, b, keys_a, _, ka, kb = two
    repo = WorkspaceRepo(server.db)
    account = AccountRepo(server.db).get('alice')
    assert require_workspace_member(server.db, a['id'], account['id'])['role'] == 'owner'
    assert require_workspace_manager(server.db, a['id'], account['id'])['role'] == 'owner'
    with pytest.raises(WorkspaceNotFound):
        require_workspace_member(server.db, b['id'], account['id'])
    with pytest.raises(WorkspaceNotFound):
        require_workspace_member(server.db, a['id'], account['id'], key_workspace_id=b['id'])
    repo.update('alpha', status='suspended')
    assert request(server, '/v2/whoami', key=ka)[:2] == (403, {'error': 'workspace suspended'})
    assert request(server, '/v2/whoami', key=kb)[0] == 200
    repo.update('alpha', status='active', new_slug='renamed')
    assert request(server, '/v2/whoami', key=ka)[1]['workspace'] == 'renamed'
    users = UserStore(server.data_dir / 'server', db=server.db, workspace_id=a['id'])
    with pytest.raises(LastAdminError):
        users.remove('alice')
    with pytest.raises(LastAdminError):
        users.set_role('alice', 'admin')
    users.add('bob', 'member')
    record, secret = keys_a.create('bob', user='bob')
    assert request(server, '/v2/whoami', key=secret)[0] == 200
    users.remove('bob')
    assert request(server, '/v2/whoami', key=secret)[0] == 401
    assert request(server, '/v2/whoami', key=kb)[0] == 200


def test_env_key_default_admin_never_server_admin_and_no_default(tmp_path, caplog):
    env = 'e' * 32
    with running(tmp_path, env) as server:
        record = server.keys.verify(env)
        context = bind_sync(server, record)
        assert context.slug == 'default' and context.principal.role == 'admin'
        assert context.principal.server_admin is False
        assert AccountRepo(server.db).list() == []
        assert request(server, '/v2/whoami', key=env)[1]['workspace'] == 'default'
    db = for_server_dir(tmp_path / 'server')
    with db.transaction(write=True) as tx:
        tx.execute("DELETE FROM workspaces WHERE slug='default'")
    db.close()
    with running(tmp_path, env) as server:
        assert request(server, '/v2/whoami', key=env)[0] == 401
        assert server.keys.list() == []
    assert caplog.text.count('AGENTICML_SERVER_KEY refused: default workspace does not exist') == 1


def test_cli_workspaces_accounts_scoped_keys_users_audit_reindex(tmp_path, capsys):
    def run(*args):
        result = cli.main(['server', *args, '--data', str(tmp_path)])
        output = capsys.readouterr()
        assert result == 0, output.err
        return output.out
    run('users', 'add', 'alice', '--role', 'admin', '--no-invite')
    run('users', 'add', 'bob', '--no-invite')
    assert json.loads(run('accounts', 'list', '--json'))[0]['server_admin'] == 1
    run('workspaces', 'create', 'alpha', '--owner', 'alice', '--name', 'Alpha')
    run('users', 'add', 'bob', '--workspace', 'alpha', '--no-invite')
    for slug in ('default', 'alpha'):
        run('keys', 'create', '--workspace', slug, '--name', 'same', '--user', 'bob')
        assert json.loads(run('keys', 'list', '--workspace', slug, '--json'))[0]['name'] == 'same'
    run('keys', 'revoke', 'same', '--workspace', 'alpha')
    assert json.loads(run('keys', 'list', '--workspace', 'alpha', '--json')) == []
    assert len(json.loads(run('keys', 'list', '--json'))) == 1
    assert {r['name'] for r in json.loads(run('users', 'list', '--workspace', 'alpha', '--json'))} == {'alice', 'bob'}
    run('workspaces', 'set-owner', 'alpha', '--owner', 'bob')
    run('workspaces', 'suspend', 'alpha')
    run('workspaces', 'resume', 'alpha')
    assert cli.main(['server', 'workspaces', 'suspend', 'default', '--data', str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert 'cannot suspend the default workspace' in err
    assert next(r['status'] for r in json.loads(run('workspaces', 'list', '--json'))
                if r['slug'] == 'default') == 'active'
    run('workspaces', 'rename', 'alpha', '--name', 'Renamed', '--slug', 'beta')
    run('accounts', 'set-server-admin', 'bob', 'on')
    run('accounts', 'set-server-admin', 'bob', 'off')
    assert json.loads(run('accounts', 'list', '--json'))[1]['server_admin'] == 0
    assert {r['slug'] for r in json.loads(run('workspaces', 'list', '--json'))} == {'default', 'beta'}
    run('reindex', '--workspace', 'beta')
    run('reindex')
    scoped = json.loads(run('audit', '--workspace', 'beta', '--json'))
    assert {'user.add', 'key.create', 'key.revoke'} <= {r['action'] for r in scoped}
    server_events = json.loads(run('audit', '--server', '--json'))
    assert 'workspace.create' in {r['action'] for r in server_events}
    assert not any(r['action'].startswith('workspace.') for r in scoped)
    assert cli.main(['server', 'workspaces', 'delete', 'beta', '--yes-delete', 'wrong', '--data', str(tmp_path)]) == 1
    capsys.readouterr()
    db = for_server_dir(tmp_path / 'server')
    root = store_root(tmp_path, WorkspaceRepo(db).require('beta')['id'])
    run('workspaces', 'delete', 'beta', '--yes-delete', 'beta')
    assert not root.parent.exists()
    assert {r['slug'] for r in json.loads(run('workspaces', 'list', '--json'))} == {'default'}
    assert json.loads(run('audit', '--server', '--json'))[0]['action'] == 'workspace.delete'


def test_package17_migration_preserves_every_row_and_moves_store(tmp_path, monkeypatch):
    from agenticml.server.db import migrate
    data = tmp_path / 'data'
    prepare_data_dir(data)
    migrations = tmp_path / 'old-migrations'
    migrations.mkdir()
    real = migrate.MIGRATIONS
    for path in real.glob('*.sql'):
        shutil.copyfile(path, migrations / path.name)
    with monkeypatch.context() as patch:
        patch.setattr(migrate, 'MIGRATIONS', migrations)
        db = connect('sqlite:///' + str(data / 'server/agenticml.db'))
    old_ids = {'zoe': 'a' * 32, 'anna': 'b' * 32, 'early': 'c' * 32, 'member': 'd' * 32, 'viewer': 'e' * 32}
    secret = 'aml_legacy' + 'x' * 32
    digest = hashlib.sha256(secret.encode()).hexdigest()
    invite = 'invite' + 'i' * 32
    with db.transaction(write=True) as tx:
        for name, aid in old_ids.items():
            tx.execute('INSERT INTO users (id, name, role, generation, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?)',
                       (aid, name, name if name in ('member', 'viewer') else 'admin', 5,
                        '2000-01-01T00:00:00Z' if name in ('early', 'anna') else '2020-01-01T00:00:00Z', 'cli'))
        tx.execute('UPDATE users SET invite_sha256=?, invite_expires_at=? WHERE name=?',
                   (hashlib.sha256(invite.encode()).hexdigest(), '2099-01-01T00:00:00Z', 'member'))
        tx.execute('INSERT INTO keys VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                   ('legacy', 'sync', digest, secret[:8], '2000-01-01T00:00:00Z', 'cli', None, 'anna'))
        tx.execute('INSERT INTO runs (run_id, owner_user, owner_key, list_row, updated_at) VALUES (?, ?, ?, ?, ?)',
                   ('old-run', 'anna', 'legacy', '{"run_id":"old-run"}', '2000-01-01T00:00:00Z'))
        tx.execute('INSERT INTO run_files VALUES (?, ?, ?, ?)', ('old-run', 'meta.json', 46, 'a' * 64))
        tx.execute('INSERT INTO run_notes VALUES (?, ?, ?)', ('old-run', 'legacy note', '2000-01-01T00:00:00Z'))
        tx.execute('INSERT INTO audit_events (ts, actor, action) VALUES (?, ?, ?)', ('2000-01-01T00:00:00Z', 'anna', 'login.ok'))
        tx.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   ('s' * 64, 'anna', old_ids['anna'], 5, 'key', 'legacy', digest, 1., 2., 1))
        tx.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   ('p' * 64, 'anna', old_ids['anna'], 5, 'password', None, None, 1., 2., 2))
        for name in ('imported_2x', 'imported_runs_2x'):
            tx.execute('INSERT INTO server_settings VALUES (?, ?)', (name, 'done'))
    legacy = data / 'store'
    (legacy / 'runs/old-run').mkdir(parents=True)
    (legacy / 'runs/old-run/meta.json').write_text('{"run_id":"old-run","status":"running"}')
    (legacy / 'objects/aa').mkdir(parents=True)
    (legacy / 'objects/aa/object').write_bytes(b'object bytes')
    (legacy / 'lineage/demo.git').mkdir(parents=True)
    (legacy / 'lineage/demo.git/marker').write_bytes(b'git bytes')
    db.close()
    with running(data) as server:
        default = WorkspaceRepo(server.db).require('default')
        assert default['created_by'] == old_ids['anna']  # tied timestamp, then login
        assert server.users.get('anna').role == 'owner'
        assert server.users.get('early').role == 'admin'
        assert server.users.get('member').role == 'member'
        assert server.users.get('viewer').role == 'viewer'
        accounts = AccountRepo(server.db).list()
        assert {r['login'] for r in accounts if r['server_admin']} == {'anna'}
        assert {r['login']: r['id'] for r in accounts} == old_ids
        assert all(r['generation'] == 5 for r in accounts)
        assert request(server, '/v2/whoami', key=secret)[1]['workspace'] == 'default'
        assert server.run_index.get('old-run')['owner_user'] == 'anna'
        assert server.run_index.committed_size('old-run', 'meta.json') == 46
        assert server.note_index.read('old-run')['text'] == 'legacy note'
        assert server.audit.repo.tail(1)[0]['actor'] == 'anna'
        from agenticml.server.db.repos import SessionRepo
        assert SessionRepo(server.db).get('s' * 64)['workspace_id'] == default['id']
        assert SessionRepo(server.db).get('p' * 64)['workspace_id'] is None
        assert server.users.redeem_invite(invite, 'password1234').name == 'member'
        assert (server.root / 'objects/aa/object').read_bytes() == b'object bytes'
        assert (server.root / 'lineage/demo.git/marker').read_bytes() == b'git bytes'
        assert not legacy.exists()
        wid = default['id']
    with running(data) as server:
        assert WorkspaceRepo(server.db).require('default')['id'] == wid
        assert server.note_index.read('old-run')['text'] == 'legacy note'


def test_2x_conversion_earliest_admin_and_all_state(tmp_path):
    from agenticml.server.users import _password_record
    from test_server_users import write_run
    from test_server_signin import PASSWORD
    prepare_data_dir(tmp_path)
    directory = tmp_path / 'server'
    users = []
    for name, role, date in [('later', 'admin', '2022'), ('first', 'admin', '2000'),
                             ('member', 'member', '2022'), ('viewer', 'viewer', '2022')]:
        users.append(dict(name=name, id=hashlib.md5(name.encode()).hexdigest(), role=role,
                          identity=name + '@example.com', password=_password_record(PASSWORD),
                          invite=None, generation=3, created_at=date + '-01-01T00:00:00Z', created_by='cli'))
    token = 'invite' + 'x' * 32
    users[2]['password'] = None
    users[2]['invite'] = {'sha256': hashlib.sha256(token.encode()).hexdigest(), 'expires_at': '2099-01-01T00:00:00Z'}
    (directory / 'users.json').write_text(json.dumps({'version': 1, 'users': users}))
    secret = 'aml_legacy' + 's' * 32
    key = dict(name='laptop', scope='sync', sha256=hashlib.sha256(secret.encode()).hexdigest(), prefix=secret[:8],
               created_at='2022-01-01T00:00:00Z', created_by='cli', last_used_at=None, user='first')
    (directory / 'keys.json').write_text(json.dumps({'version': 2, 'keys': [key]}))
    event = {'ts': '2022-01-01T00:00:00Z', 'actor': 'first', 'action': 'login.ok'}
    (directory / 'audit.jsonl').write_text(json.dumps(event) + '\n')
    run = write_run(tmp_path / 'store', 'run-1', 'first', extra={'owner_key': 'laptop'})
    (run / 'notes.md').write_text('saved note')
    with running(tmp_path) as server:
        assert server.users.get('first').role == 'owner'
        assert server.users.get('later').role == 'admin'
        assert server.users.get('viewer').role == 'viewer'
        accounts = AccountRepo(server.db)
        assert {a['login'] for a in accounts.list() if a['server_admin']} == {'first'}
        assert accounts.get('first')['id'] == users[1]['id']
        assert server.users.verify_password('first', PASSWORD).generation == 3
        assert server.users.redeem_invite(token, PASSWORD).name == 'member'
        assert server.run_index.as_remote('run-1')['owner'] == 'first'
        assert server.note_index.read('run-1')['text'] == 'saved note'
        assert server.audit.repo.tail(1)[0]['action'] == 'login.ok'
        assert request(server, '/v2/whoami', key=secret)[1]['workspace'] == 'default'
        assert (server.root / 'runs/run-1/notes.md.2x').exists()
        wid = server.default_workspace_id
    with running(tmp_path) as server:
        assert server.default_workspace_id == wid
        assert server.note_index.read('run-1')['text'] == 'saved note'
        assert server.audit.repo.tail(1)[0]['action'] == 'login.ok'


def test_membership_roles_invites_and_key_sessions(two):
    from agenticml.server.db.workspaces import InviteRepo, MembershipRepo
    from agenticml.server.db.repos import SessionRepo
    from agenticml.server.users import allowed
    from agenticml.server.keys import ClientError
    from agenticml.server.users import InviteInvalid

    server, a, b, keys_a, keys_b, ka, kb = two
    for repo, args in ((InviteRepo, (server.db,)), (MembershipRepo, (server.db,))):
        with pytest.raises(TypeError):
            repo(*args)
    alice = AccountRepo(server.db).get('alice')
    assert InviteRepo(server.db, b['id']).create('alice', 'viewer', 'bob') is None
    record, secret = keys_b.create('alice-view', user='alice')
    principal = bind_sync(server, record).principal
    assert principal.role == 'viewer' and allowed(principal, 'read') and not allowed(principal, 'sync')
    assert bind_sync(server, server.keys.verify(ka)).principal.role == 'owner'
    assert request(server, '/v2/objects/missing', 'POST', b'{"objects": []}', secret)[0] == 403
    account_before = AccountRepo(server.db).get('alice')
    assert account_before == alice  # Membership never rewrites the global account.
    token = InviteRepo(server.db, a['id']).create('NEW@Example.com', 'member', 'alice')
    assert InviteRepo(server.db, a['id']).list()[0]['login'] == 'new@example.com'
    with pytest.raises(InviteInvalid):
        InviteRepo(server.db, b['id']).redeem(token, 'password1234')
    aid = InviteRepo(server.db, a['id']).redeem(token, 'password1234')
    assert MembershipRepo(server.db, a['id']).get(aid)['role'] == 'member'
    assert MembershipRepo(server.db, b['id']).get(aid) is None
    with pytest.raises(InviteInvalid):
        InviteRepo(server.db, a['id']).redeem(token, 'password1234')
    token, entry = server.web._create_session(keys_a.list()[0])
    digest = hashlib.sha256(token.encode('ascii')).hexdigest()
    assert entry['workspace_id'] == a['id']
    assert SessionRepo(server.db).get(digest)['workspace_id'] == a['id']
    assert server.web._principal_from_session(entry) is None  # Legacy dashboard remains default-only.


def test_conversion_rename_crash_restarts_and_rejects_unsafe_parent(tmp_path, monkeypatch):
    from agenticml.server.workspaces import convert_store
    from test_server_users import write_run
    data = tmp_path / 'data'
    prepare_data_dir(data)
    write_run(data / 'store', 'run-1', 'alice')
    original = SettingsRepo.set
    def crash(self, name, value, tx=None):
        if name == 'workspace_store_converted':
            raise RuntimeError('crash after rename')
        return original(self, name, value, tx)
    with monkeypatch.context() as patch:
        patch.setattr(SettingsRepo, 'set', crash)
        with pytest.raises(RuntimeError, match='crash after rename'):
            for_server_dir(data / 'server')
    with running(data) as server:
        assert (server.root / 'runs/run-1/meta.json').is_file()
        assert server.run_index.get('run-1')['owner_user'] == 'alice'
        root = server.root
    root.parent.rename(tmp_path / 'moved-workspace')
    root.parent.symlink_to(tmp_path / 'moved-workspace')
    with pytest.raises(DatabaseError, match='symlink'):
        for_server_dir(data / 'server')


def test_env_key_collision_is_refused_without_disclosing_secret(two):
    server, a, b, _, _, ka, kb = two
    with pytest.raises(ValueError, match='conflicts with a stored key') as exc:
        KeyStore(server.data_dir / 'server', kb, db=server.db,
                 workspace_id=server.default_workspace_id)
    assert kb not in str(exc.value)


def test_sync_binding_ignores_workspace_headers_query_and_body(two):
    server, a, b, _, _, ka, kb = two
    payload = json.dumps({'run_id': 'attempt', 'status': 'running', 'workspace_id': b['id'],
                          'workspace': 'beta'}).encode()
    status, _, _ = request(server, '/v2/runs/attempt/files/meta.json?workspace=beta', 'PUT', payload, ka,
                           {'X-Workspace': 'beta', 'X-Workspace-Id': b['id']})
    assert status == 200
    assert RunRepo(server.db, a['id']).get('attempt')['owner_user'] == 'alice'
    assert RunRepo(server.db, b['id']).get('attempt') is None
    assert request(server, '/v2/runs/attempt/state', key=kb)[:2] == (404, {'error': 'unknown run'})


def test_reader_and_code_diff_caches_include_workspace(two, monkeypatch):
    from agenticml.ui import server as ui
    server, a, b, _, _, ka, kb = two
    for key in (ka, kb):
        assert put_meta(server, 'same', key=key)[0] == 200
    with server.run_reader('same', workspace_id=a['id']) as reader_a:
        with server.run_reader('same', workspace_id=b['id']) as reader_b:
            assert reader_a is not reader_b
            assert reader_a.remote()['owner'] == 'alice'
            assert reader_b.remote()['owner'] == 'bob'
            assert server.cached_reader('same', workspace_id=a['id']) is reader_a
            assert server.cached_reader('same', workspace_id=b['id']) is reader_b
    calls = []
    def diff(root, *args):
        calls.append(root)
        return {'files': [], 'workspace_store': str(root)}
    monkeypatch.setattr(ui, '_code_pair_diff', diff)
    class Budget:
        def exhausted(self):
            return False
    result_a = server.code_pair_diff('a', 'b', Budget(), workspace_id=a['id'])
    result_b = server.code_pair_diff('a', 'b', Budget(), workspace_id=b['id'])
    assert result_a != result_b
    assert server.code_pair_diff('a', 'b', Budget(), workspace_id=a['id']) == result_a
    assert calls == [store_root(server.data_dir, a['id']), store_root(server.data_dir, b['id'])]


CROSS_WORKSPACE_CREDENTIALS = (
    'this account belongs to other workspaces; only the person or a server admin can change it')


def test_cross_workspace_credentials_refused_except_operator_cli(tmp_path, monkeypatch, capsys):
    import io
    import secrets
    from datetime import datetime, timedelta, timezone

    from agenticml.server.keys import ClientError
    from test_server_keys_web import login as key_login
    from test_server_signin import PASSWORD
    from test_server_userspage import api

    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    data = tmp_path / 'data'
    with running(data) as server:
        _, admin_key = server.keys.create('admin', 'admin')
        server.cookie = key_login(server, admin_key)
        server.users.add('bob', 'member')
        server.users.set_password('bob', PASSWORD)
        WorkspaceRepo(server.db).create('beta', owner='bob')
        before = AccountRepo(server.db).get('bob')
        with pytest.raises(ClientError, match='belongs to other workspaces'):
            server.users.create_invite('bob')
        with pytest.raises(ClientError, match='belongs to other workspaces'):
            server.users.set_identity('bob', 'bob@example.com')
        with pytest.raises(ClientError, match='belongs to other workspaces'):
            server.users.set_password('bob', PASSWORD + 'x')
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        with server.db.transaction(write=True) as tx:
            tx.execute(
                'INSERT INTO invites (workspace_id, login, role, sha256, expires_at, invited_by) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (server.users.workspace_id, 'bob', 'member', digest,
                 layout.format_timestamp(datetime.now(timezone.utc) + timedelta(hours=72)), 'admin'))
        with pytest.raises(ClientError, match='belongs to other workspaces'):
            server.users.redeem_invite(token, PASSWORD + 'y')
        after = AccountRepo(server.db).get('bob')
        assert after['password'] == before['password']
        assert after['generation'] == before['generation']
        assert after['identity'] == before['identity']
        status, _, body = api(server, '/api/server/users/bob/invite', 'POST', {})
        assert status == 403 and body == {'error': CROSS_WORKSPACE_CREDENTIALS}
        status, _, body = api(server, '/api/server/users/bob', 'PATCH', {'identity': 'bob@example.com'})
        assert status == 403 and body == {'error': CROSS_WORKSPACE_CREDENTIALS}
        server.users.add('cara', 'member')
        server.users.set_password('cara', PASSWORD)
        server.users.set_identity('cara', 'cara@example.com')
        cara_token = server.users.create_invite('cara')
        assert server.users.redeem_invite(cara_token, PASSWORD).name == 'cara'
    monkeypatch.setattr('sys.stdin', io.StringIO(PASSWORD + '\n'))
    assert cli.main(['server', 'users', 'set-password', 'bob', '--stdin', '--data', str(data)]) == 0
    output = capsys.readouterr().out
    assert 'default' in output and 'beta' in output
    assert cli.main(['server', 'users', 'set-identity', 'bob', 'bob@example.com', '--data', str(data)]) == 0
    output = capsys.readouterr().out
    assert 'default' in output and 'beta' in output


def test_pending_invite_redeems_existing_account_without_password_change(two):
    from agenticml.server.db.workspaces import InviteRepo, MembershipRepo
    from agenticml.server.users import InviteExistingAccount
    from test_server_signin import PASSWORD

    server, a, b, _, _, _, _ = two
    token = InviteRepo(server.db, a['id']).create('newbie', 'member', 'alice')
    users_b = UserStore(server.data_dir / 'server', db=server.db, workspace_id=b['id'])
    users_b.add('newbie', 'viewer')
    users_b.set_password('newbie', PASSWORD)
    before = AccountRepo(server.db).get('newbie')
    with pytest.raises(InviteExistingAccount, match='sign in with your existing credentials') as exc:
        InviteRepo(server.db, a['id']).redeem(token, 'unrelated-pass')
    after = AccountRepo(server.db).get('newbie')
    assert exc.value.account_id == before['id']
    assert after['password'] == before['password']
    assert after['generation'] == before['generation']
    assert MembershipRepo(server.db, a['id']).get(after['id'])['role'] == 'member'
    assert InviteRepo(server.db, a['id']).list() == []
    assert UserStore(server.data_dir / 'server', db=server.db, workspace_id=a['id']).verify_password(
        'newbie', PASSWORD) is not None


def test_key_bootstrap_does_not_reset_existing_admin_credentials(tmp_path):
    from agenticml.server.users import _password_record

    prepare_data_dir(tmp_path)
    db = for_server_dir(tmp_path / 'server')
    default = WorkspaceRepo(db).require('default')
    aid = 'ab' * 16
    record = json.dumps(_password_record('password1234'))
    with db.transaction(write=True) as tx:
        tx.execute(
            'INSERT INTO accounts (id, login, display_name, password, generation, created_at, created_by) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (aid, 'admin', 'admin', record, 7, '2000-01-01T00:00:00Z', 'test'))
    _, _, bootstrap = KeyRepo(db, default['id']).create(
        'first', 'admin', 'cli', user=None, only_if_no_admin=False, env=False)
    after = AccountRepo(db).get('admin')
    assert bootstrap is None
    assert after['id'] == aid and after['generation'] == 7
    assert UserStore(tmp_path / 'server', db=db, workspace_id=default['id']).verify_password(
        'admin', 'password1234') is not None
    db.close()


def test_default_workspace_cannot_be_deleted_or_reslugged(tmp_path, capsys):
    from agenticml.server.keys import ClientError

    assert cli.main(['server', 'users', 'add', 'alice', '--role', 'admin', '--no-invite',
                     '--data', str(tmp_path)]) == 0
    capsys.readouterr()
    db = for_server_dir(tmp_path / 'server')
    with pytest.raises(ClientError, match='env key and the self-hosted default'):
        WorkspaceRepo(db).delete('default')
    with pytest.raises(ClientError, match='env key and the self-hosted default'):
        WorkspaceRepo(db).update('default', new_slug='home')
    WorkspaceRepo(db).update('default', name='Home')
    assert WorkspaceRepo(db).get('default')['name'] == 'Home' and WorkspaceRepo(db).get('default')['slug'] == 'default'
    db.close()
    assert cli.main(['server', 'workspaces', 'delete', 'default', '--yes-delete', 'default',
                     '--data', str(tmp_path)]) == 1
    assert 'env key' in capsys.readouterr().err
    assert cli.main(['server', 'workspaces', 'rename', 'default', '--name', 'HQ', '--slug', 'hq',
                     '--data', str(tmp_path)]) == 1
    assert 'env key' in capsys.readouterr().err
    assert cli.main(['server', 'workspaces', 'rename', 'default', '--name', 'HQ', '--data', str(tmp_path)]) == 0
    db = for_server_dir(tmp_path / 'server')
    assert WorkspaceRepo(db).get('default')['name'] == 'HQ'
    db.close()


def test_set_password_acting_account_id_of_other_account_refused(tmp_path, monkeypatch):
    from agenticml.server.keys import ClientError
    from test_server_signin import PASSWORD

    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'data') as server:
        server.users.add('bob', 'member')
        server.users.set_password('bob', PASSWORD)
        WorkspaceRepo(server.db).create('beta', owner='bob')
        cara = server.users.add('cara', 'member')
        before = AccountRepo(server.db).get('bob')
        with pytest.raises(ClientError, match='belongs to other workspaces'):
            server.users.set_password('bob', PASSWORD + 'x', acting_account_id=cara.id)
        after = AccountRepo(server.db).get('bob')
        assert after['password'] == before['password']
        assert after['generation'] == before['generation']
        bob = server.users.get('bob')
        server.users.set_password('bob', PASSWORD + 'y', acting_account_id=bob.id)
        assert server.users.verify_password('bob', PASSWORD + 'y') is not None


def test_account_password_self_service_across_workspaces(tmp_path, monkeypatch):
    from test_server_signin import PASSWORD, password_login, session_cookie
    from test_server_web import authed

    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'data') as server:
        server.users.add('bob', 'member')
        server.users.set_password('bob', PASSWORD)
        WorkspaceRepo(server.db).create('beta', owner='bob')
        status, _, hdrs = password_login(server, 'bob', PASSWORD)
        cookie = session_cookie(hdrs)
        new = PASSWORD + 'xy'
        status, _, body, _ = authed(
            server, '/api/server/account/password', 'POST',
            json.dumps({'current': PASSWORD, 'new': new}).encode(), cookie=cookie)
        assert status == 200
        assert password_login(server, 'bob', new)[0] == 200
        assert password_login(server, 'bob', PASSWORD)[0] == 401


def test_store_constructors_and_server_methods_require_workspace_id(tmp_path, monkeypatch):
    from agenticml.server.audit import AuditLog
    from agenticml.server.keys import KeyStore
    from agenticml.server.users import UserStore

    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'data') as server:
        directory = server.data_dir / 'server'
        for cls, args in ((UserStore, (directory,)), (KeyStore, (directory,)), (AuditLog, (directory,))):
            with pytest.raises(TypeError):
                cls(*args)
        with pytest.raises(TypeError):
            server.run_reader('run-1')
        with pytest.raises(TypeError):
            server.cached_reader('run-1')
        with pytest.raises(TypeError):
            server.code_pair_diff('a', 'b', None)
        with pytest.raises(TypeError):
            server.run_lock('run-1')
        with pytest.raises(TypeError):
            server.object_lock('ab')


def test_bind_sync_builds_stores_lazily(two, monkeypatch):
    from agenticml.server import audit as audit_mod
    from agenticml.server import keys as keys_mod
    from agenticml.server import users as users_mod

    server, a, _, _, _, ka, _ = two
    server._ws_stores.clear()
    counts = {'users': 0, 'keys': 0, 'audit': 0}
    real = {'users': users_mod.UserStore, 'keys': keys_mod.KeyStore, 'audit': audit_mod.AuditLog}

    def wrap(name, cls):
        def ctor(*args, **kwargs):
            counts[name] += 1
            return cls(*args, **kwargs)
        return ctor

    monkeypatch.setattr(users_mod, 'UserStore', wrap('users', real['users']))
    monkeypatch.setattr(keys_mod, 'KeyStore', wrap('keys', real['keys']))
    monkeypatch.setattr(audit_mod, 'AuditLog', wrap('audit', real['audit']))
    record = server.keys.verify(ka)
    context = bind_sync(server, record)
    assert counts == {'users': 1, 'keys': 0, 'audit': 0}
    bind_sync(server, record)
    assert counts == {'users': 1, 'keys': 0, 'audit': 0}
    context.audit.record('alice', 'key', 'run.create', 'r')
    assert counts['audit'] == 1
    _ = context.keys.list()
    assert counts['keys'] == 1
    server._ws_stores[a['id']] = ('suspended', server._ws_stores[a['id']][1])
    bind_sync(server, record)
    assert counts['users'] == 2


def test_workspace_stores_dropped_when_workspace_deleted(two):
    from agenticml.server.workspaces import delete_workspace

    server, _, b, _, _, _, kb = two
    record = server.keys.verify(kb)
    bind_sync(server, record)
    assert b['id'] in server._ws_stores
    delete_workspace(server.db, server.data_dir, 'beta')
    with pytest.raises(WorkspaceNotFound):
        bind_sync(server, record)
    assert b['id'] not in server._ws_stores


def test_convert_store_exdev_and_skips_flag_without_default(tmp_path, monkeypatch):
    import errno
    import os

    from agenticml.server.db import connect
    from agenticml.server.workspaces import convert_store
    from test_server_users import write_run

    data = tmp_path / 'data'
    prepare_data_dir(data)
    write_run(data / 'store', 'run-1', 'alice')
    db = connect('sqlite:///' + str((data / 'server' / 'agenticml.db').resolve()))

    def boom(src, dst):
        raise OSError(errno.EXDEV, 'Invalid cross-device link')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'rename', boom)
        with pytest.raises(DatabaseError, match='server/tmp and workspaces must be on the same filesystem'):
            convert_store(db, data)
    with db.transaction(write=True) as tx:
        tx.execute('DELETE FROM workspaces')
    convert_store(db, data)
    assert SettingsRepo(db).get('workspace_store_converted') is None
    db.close()


def test_legacy_run_owner_not_attached_without_reclaim_runs(two, tmp_path, capsys):
    from agenticml.server.keys import ClientError

    server, a, b, _, _, _, _ = two
    aid = 'cd' * 16
    with server.db.transaction(write=True) as tx:
        tx.execute(
            'INSERT INTO accounts (id, login, display_name, created_at, created_by) '
            'VALUES (?, ?, ?, ?, ?)',
            (aid, 'ghost', 'ghost', layout.format_timestamp(), 'legacy run owner'))
    users_b = UserStore(server.data_dir / 'server', db=server.db, workspace_id=b['id'])
    with pytest.raises(ClientError, match='reclaim-runs'):
        users_b.add('ghost')
    assert users_b.get('ghost') is None
    users_b.add('ghost', reclaim_runs=True)
    ghost = users_b.get('ghost')
    assert ghost.id == aid
    assert ghost.created_by == 'cli'
    assert AccountRepo(server.db).get('ghost')['created_by'] == 'cli'
    users_a = UserStore(server.data_dir / 'server', db=server.db, workspace_id=a['id'])
    users_a.add('ghost')
    assert users_a.get('ghost').id == aid
    data = tmp_path / 'other'
    with running(data) as other:
        other.users.add('alice', 'admin')
        with other.db.transaction(write=True) as tx:
            tx.execute(
                'INSERT INTO accounts (id, login, display_name, created_at, created_by) '
                'VALUES (?, ?, ?, ?, ?)',
                ('ef' * 16, 'shade', 'shade', layout.format_timestamp(), 'legacy run owner'))
        WorkspaceRepo(other.db).create('beta', owner='alice')
    assert cli.main(['server', 'users', 'add', 'shade', '--workspace', 'beta',
                     '--data', str(data)]) == 1
    assert 'reclaim-runs' in capsys.readouterr().err
    assert cli.main(['server', 'users', 'add', 'shade', '--workspace', 'beta',
                     '--reclaim-runs', '--no-invite', '--data', str(data)]) == 0
    db = for_server_dir(data / 'server')
    assert AccountRepo(db).get('shade')['created_by'] == 'cli'
    db.close()


def test_stale_22_password_session_invalid_after_0003(tmp_path, monkeypatch):
    import re
    import sqlite3
    import time

    from agenticml.server.db.dialect import SqliteDialect
    from agenticml.server.db.migrate import MIGRATIONS
    from agenticml.server.users import _password_record
    from test_server_signin import PASSWORD
    from test_server_web import authed

    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    data = tmp_path / 'data'
    prepare_data_dir(data)
    db_path = data / 'server' / 'agenticml.db'
    dialect = SqliteDialect()
    conn = sqlite3.connect(db_path)
    conn.execute('CREATE TABLE schema_migrations '
                 '(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)')
    for path in sorted(p for p in MIGRATIONS.glob('[0-9][0-9][0-9][0-9]_*')
                       if p.suffix in ('.sql', '.py') and int(p.name[:4]) <= 2):
        sql = dialect.migration(path.read_text()) if path.suffix == '.sql' else None
        if sql:
            for statement in re.split(r';\s*(?:\n|$)', sql):
                if statement.strip():
                    conn.execute(statement)
        conn.execute('INSERT INTO schema_migrations VALUES (?, ?, ?)',
                     (int(path.name[:4]), path.stem[5:], layout.format_timestamp()))
    aid = 'ab' * 16
    conn.execute(
        'INSERT INTO users (id, name, role, identity, identity_folded, password, '
        'invite_sha256, invite_expires_at, generation, created_at, created_by) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (aid, 'alice', 'admin', None, None, json.dumps(_password_record(PASSWORD)),
         None, None, 1, layout.format_timestamp(), 'cli'))
    token = 'stale-session-token-value'
    digest = hashlib.sha256(token.encode('ascii')).hexdigest()
    now = time.time()
    conn.execute(
        'INSERT INTO sessions (token_sha256, user_name, user_id, generation, via, '
        'key_name, key_sha256, created, last_seen, created_order) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (digest, 'alice', aid, 0, 'password', None, None, now, now, 1))
    conn.commit()
    conn.close()
    with running(data) as server:
        assert AccountRepo(server.db).get('alice')['generation'] == 1
        status, _, _, _ = authed(server, '/api/runs', cookie=f'aml_session={token}')
        assert status == 401

"""30.2: ``agenticml server accounts delete`` erases one account and redacts the server log."""
import io
import json

import pytest

from agenticml import cli
from agenticml.server.audit import AuditLog
from agenticml.server.db.repos import AuditRepo, RunRepo, SessionRepo
from agenticml.server.db.workspaces import AccountRepo, WorkspaceRepo
from agenticml.server.keys import KeyStore
from agenticml.server.runs import reconcile
from agenticml.server.users import UserStore
from agenticml.server.workspaces import prepare_store, store_root
from server_db_helpers import insert_run
from test_server_ingest import running

CAROL = 'carol@example.com'
PLACEHOLDER = '[deleted account]'


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Default workspace (root), alpha (alice), beta (bob); carol belongs to alpha and beta."""
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'data') as server:
        server.users.add('root', 'admin')
        server.users.add('alice', 'admin')
        server.users.add('bob', 'admin')
        repo = WorkspaceRepo(server.db)
        alpha = repo.create('alpha', owner='alice')
        beta = repo.create('beta', owner='bob')
        for ws in (alpha, beta):
            prepare_store(server.data_dir, ws['id'])
        directory = server.data_dir / 'server'
        server.alpha, server.beta = alpha, beta
        server.users_alpha = UserStore(directory, db=server.db, workspace_id=alpha['id'])
        server.users_beta = UserStore(directory, db=server.db, workspace_id=beta['id'])
        server.keys_beta = KeyStore(directory, db=server.db, workspace_id=beta['id'])
        server.users_alpha.add(CAROL, 'member')
        server.users_beta.add(CAROL, 'member')
        server.users_beta.add('dave@example.com', 'member')
        server.keys_beta.create('carol-laptop', user=CAROL)
        server.keys_beta.create('dave-laptop', user='dave@example.com')
        yield server


def server_log(server):
    return AuditRepo(server.db, None).tail(1000)


def log(server, actor, action, target=None, client=None, detail=None, via='dashboard'):
    AuditLog(server.data_dir / 'server', db=server.db, workspace_id=None).record(
        actor, via, action, target, client, detail)


def delete(server, login, *extra, yes=True):
    argv = ['server', 'accounts', 'delete', login, '--data', str(server.data_dir)]
    if yes is True:
        argv += ['--yes-delete', login]
    elif yes:
        argv += ['--yes-delete', yes]
    return cli.main(argv + list(extra))


def count(server, sql, params=()):
    with server.db.transaction() as tx:
        tx.cross_workspace('test count')
        return tx.fetchone(sql, params)[0]


def test_delete_removes_everything_and_leaves_other_members(world, capsys):
    server = world
    accounts = AccountRepo(server.db)
    carol = accounts.get(CAROL)
    dave = accounts.get('dave@example.com')
    key_hashes = [r.sha256 for r in server.keys_beta.list() if r.user == CAROL]
    assert len(key_hashes) == 1
    sessions = SessionRepo(server.db)
    entry = {'id': carol['id'], 'workspace_id': None, 'generation': 0, 'via': 'password', 'key': None,
             'key_sha256': None, 'created': 1.0, 'last_seen': 1.0}
    sessions.create('a' * 64, entry, 100)
    sessions.create('b' * 64, {**entry, 'id': None, 'workspace_id': server.beta['id'], 'via': 'key',
                               'key': 'carol-laptop', 'key_sha256': key_hashes[0]}, 100)
    sessions.create('c' * 64, {**entry, 'id': dave['id']}, 100)
    with server.db.transaction(write=True) as tx:
        tx.bind_workspace(server.alpha['id'])
        tx.execute('INSERT INTO invites (workspace_id, login, role, sha256, expires_at, invited_by) '
                   'VALUES (?, ?, ?, ?, ?, ?)',
                   (server.alpha['id'], CAROL, 'member', 'f' * 64, '2099-01-01T00:00:00.000000Z', 'alice'))
    other_alpha_before = [u.name for u in server.users_alpha.list()]
    assert delete(server, CAROL) == 0
    out = capsys.readouterr().out
    assert 'deleted account ' + CAROL in out and 'alpha, beta' in out
    assert accounts.get(CAROL) is None
    assert count(server, 'SELECT COUNT(*) FROM memberships WHERE account_id=?', (carol['id'],)) == 0
    assert count(server, 'SELECT COUNT(*) FROM keys WHERE account_id=?', (carol['id'],)) == 0
    assert count(server, 'SELECT COUNT(*) FROM invites WHERE login=?', (CAROL,)) == 0
    assert set(sessions.list()) == {'c' * 64}
    assert [u.name for u in server.users_alpha.list()] == [n for n in other_alpha_before if n != CAROL]
    assert sorted(u.name for u in server.users_beta.list()) == ['bob', 'dave@example.com']
    assert [r.name for r in server.keys_beta.list() if r.user == 'dave@example.com'] == ['dave-laptop']
    assert accounts.get('dave@example.com') is not None
    events = [e for e in server_log(server) if e['action'] == 'account.delete']
    assert len(events) == 1 and events[0]['target'] == carol['id']
    assert CAROL not in json.dumps(events[0])


def test_delete_redacts_server_level_audit(world):
    server = world
    carol = AccountRepo(server.db).get(CAROL)
    log(server, CAROL, 'login.ok', CAROL, '1.2.3.4', {'method': 'password'})
    log(server, 'root', 'account.create', CAROL, '9.9.9.9', {'pending': True})
    log(server, 'root', 'workspace.create', 'w1', '9.9.9.9',
        {'slug': 'x', 'owner': 'Carol@Example.com', 'name': CAROL,
         'nested': {'old_owner': CAROL, 'list': [{'user': CAROL}, 'invited carol@example.com as admin']}})
    log(server, 'root', 'workspace.rename', 'w2', None, {'slug': 'carol', 'role': 'admin', 'note': 'carolx@example.com'})
    unrelated = {'actor': 'root', 'action': 'login.ok', 'target': 'root', 'client': '5.6.7.8',
                 'detail': {'method': 'key'}}
    log(server, 'root', 'login.ok', 'root', '5.6.7.8', {'method': 'key'})
    # A workspace-level row naming carol belongs to its workspace and stays.
    AuditLog(server.data_dir / 'server', db=server.db, workspace_id=server.beta['id']).record(
        'bob', 'dashboard', 'user.add', CAROL, '7.7.7.7', {'role': 'member', 'owner': CAROL})
    before = {(e['action'], e['target']): e for e in server_log(server)}
    workspace_before = AuditRepo(server.db, server.beta['id']).tail(100)
    assert delete(server, CAROL) == 0
    after = server_log(server)
    text = json.dumps(after)
    assert CAROL not in text.replace('carolx@example.com', '') and 'Carol@Example.com' not in text
    by_action = {(e['action'], e['target']): e for e in after}
    login_ok = by_action[('login.ok', PLACEHOLDER)]
    assert login_ok['actor'] == PLACEHOLDER and login_ok['client'] is None
    created = by_action[('account.create', PLACEHOLDER)]
    assert created['actor'] == 'root' and created['client'] == '9.9.9.9'
    workspace_create = by_action[('workspace.create', 'w1')]
    assert workspace_create['detail'] == {
        'slug': 'x', 'owner': PLACEHOLDER, 'name': PLACEHOLDER,
        'nested': {'old_owner': PLACEHOLDER, 'list': [{'user': PLACEHOLDER}, 'invited ' + PLACEHOLDER + ' as admin']}}
    rename = by_action[('workspace.rename', 'w2')]
    assert rename['detail'] == {'slug': 'carol', 'role': 'admin', 'note': 'carolx@example.com'}
    same = before[('login.ok', 'root')]
    assert by_action[('login.ok', 'root')] == same and same['detail'] == unrelated['detail']
    assert AuditRepo(server.db, server.beta['id']).tail(100) == workspace_before
    assert any(e['target'] == CAROL for e in workspace_before)  # still names carol there
    deleted = [e for e in after if e['action'] == 'account.delete']
    assert len(deleted) == 1 and deleted[0]['target'] == carol['id']
    assert deleted[0]['detail'].keys() == {'memberships', 'keys', 'sessions', 'invites', 'audit_redacted'}


def test_short_login_does_not_touch_longer_names_or_other_keys(world):
    server = world
    server.users_alpha.add('bobby', 'member')
    log(server, 'bobby', 'login.ok', 'bobby', '1.1.1.1', {'owner': 'bobby', 'role': 'bob', 'note': 'bob was here'})
    log(server, 'root', 'user.role', 'x', None, {'owner': 'bob', 'role': 'bob', 'note': 'bob was here'})
    log(server, 'bob', 'login.ok', 'bob', '2.2.2.2', {'method': 'password'})
    # bob owns beta: hand it over first (set-owner), then delete the account.
    assert cli.main(['server', 'workspaces', 'set-owner', 'beta', '--owner', 'alice',
                     '--data', str(server.data_dir)]) == 0
    server.users_beta.remove('bob')
    assert delete(server, 'bob') == 0
    events = server_log(server)
    bobby = [e for e in events if e['actor'] == 'bobby'][0]
    assert bobby['target'] == 'bobby' and bobby['client'] == '1.1.1.1'
    assert bobby['detail'] == {'owner': 'bobby', 'role': 'bob', 'note': 'bob was here'}
    role = [e for e in events if e['action'] == 'user.role'][0]
    assert role['detail'] == {'owner': PLACEHOLDER, 'role': 'bob', 'note': 'bob was here'}
    ok = [e for e in events if e['action'] == 'login.ok' and e['target'] == PLACEHOLDER][0]
    assert ok['actor'] == PLACEHOLDER and ok['client'] is None and ok['detail'] == {'method': 'password'}
    assert AccountRepo(server.db).get('bobby') is not None


def test_non_ascii_email_is_redacted_in_json_detail(world):
    server = world
    login = 'jürgen@example.com'
    server.users_alpha.add(login, 'member')
    log(server, login, 'login.ok', login, '3.3.3.3', {'owner': login, 'note': 'invited JÜRGEN@example.com today'})
    assert delete(server, login) == 0
    text = json.dumps(server_log(server))
    assert '\\u00fcrgen' not in text.lower() and 'jürgen' not in text.lower()
    row = [e for e in server_log(server) if e['action'] == 'login.ok'][0]
    assert row['actor'] == PLACEHOLDER and row['target'] == PLACEHOLDER
    assert row['detail'] == {'owner': PLACEHOLDER, 'note': 'invited ' + PLACEHOLDER + ' today'}


@pytest.mark.parametrize('case', ['server_admin', 'owner', 'runs', 'reserved', 'unknown', 'confirmation'])
def test_refusals_change_nothing(world, capsys, case):
    server = world
    accounts = AccountRepo(server.db)
    login, expected = CAROL, ''
    yes = True
    if case == 'server_admin':
        accounts.set_server_admin(CAROL, True)
        expected = 'set-server-admin'
    elif case == 'owner':
        login, expected = 'alice', 'alpha'
    elif case == 'runs':
        insert_run(RunRepo(server.db, server.beta['id']), 'r-carol', owner_user=CAROL)
        expected = 'beta (1)'
    elif case == 'reserved':
        login, expected = 'cli', 'reserved'
        accounts.create('cli')
    elif case == 'unknown':
        login, expected = 'nobody@example.com', 'unknown account'
    else:
        yes, expected = 'someone-else@example.com', 'not confirmed'
    before = (accounts.list(), count(server, 'SELECT COUNT(*) FROM memberships'),
              count(server, 'SELECT COUNT(*) FROM keys'), len(server_log(server)))
    assert delete(server, login, yes=yes) == 1
    err = capsys.readouterr().err
    assert expected in err and 'Traceback' not in err
    assert err.startswith('agenticml server: ')
    assert (accounts.list(), count(server, 'SELECT COUNT(*) FROM memberships'),
            count(server, 'SELECT COUNT(*) FROM keys'), len(server_log(server))) == before


def test_eof_on_stdin_exits_one_without_traceback(world, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.StringIO(''))
    assert cli.main(['server', 'accounts', 'delete', CAROL, '--data', str(world.data_dir)]) == 1
    err = capsys.readouterr().err
    assert 'Traceback' not in err and err.startswith('agenticml server: ')
    assert AccountRepo(world.db).get(CAROL) is not None


def test_prompt_confirmation_accepts_case_insensitive_email(world, monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.StringIO('Carol@Example.com\n'))
    assert cli.main(['server', 'accounts', 'delete', CAROL, '--data', str(world.data_dir)]) == 0
    assert 'Type the account login to delete it:' in capsys.readouterr().out
    assert AccountRepo(world.db).get(CAROL) is None


def test_json_output_and_firebase_uid_line(world, capsys):
    server = world
    accounts = AccountRepo(server.db)
    carol_id = accounts.get(CAROL)['id']
    dave_id = accounts.get('dave@example.com')['id']
    with server.db.transaction(write=True) as tx:
        accounts.link_firebase(carol_id, 'uid-carol-1', CAROL, True, tx)
    assert delete(server, CAROL) == 0
    out = capsys.readouterr().out
    assert 'Firebase user still exists: uid uid-carol-1 (' + CAROL + ')' in out
    assert 'Authentication > Users' in out
    # A second account, JSON mode.
    with server.db.transaction(write=True) as tx:
        accounts.link_firebase(dave_id, 'uid-dave', 'dave@example.com', True, tx)
    assert delete(server, 'dave@example.com', '--json') == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['firebase_uid'] == 'uid-dave' and summary['email'] == 'dave@example.com'
    assert summary['memberships'] == ['beta'] and summary['keys'] == 1


def test_created_by_columns_are_scrubbed(world):
    server = world
    accounts = AccountRepo(server.db)
    accounts.create('invitee@example.com', created_by=CAROL)
    accounts.create('legacy-owner', created_by='legacy run owner')
    carol_id = accounts.get(CAROL)['id']
    with server.db.transaction(write=True) as tx:
        tx.cross_workspace('test')
        tx.execute('UPDATE keys SET created_by=? WHERE name=?', (CAROL, 'dave-laptop'))
        tx.execute('INSERT INTO invites (workspace_id, login, role, sha256, expires_at, invited_by) '
                   'VALUES (?, ?, ?, ?, ?, ?)',
                   (server.alpha['id'], 'new@example.com', 'member', 'e' * 64, '2099-01-01T00:00:00.000000Z', CAROL))
        tx.execute('UPDATE workspaces SET created_by=? WHERE id=?', (carol_id, server.alpha['id']))
    assert delete(server, CAROL) == 0
    assert accounts.get('invitee@example.com')['created_by'] == PLACEHOLDER
    assert accounts.get('legacy-owner')['created_by'] == 'legacy run owner'
    assert count(server, "SELECT COUNT(*) FROM keys WHERE created_by=?", (PLACEHOLDER,)) == 1
    assert count(server, "SELECT COUNT(*) FROM invites WHERE invited_by=?", (PLACEHOLDER,)) == 1
    assert count(server, 'SELECT COUNT(*) FROM workspaces WHERE created_by IS NULL') == 1
    assert count(server, 'SELECT COUNT(*) FROM workspaces WHERE id=?', (server.alpha['id'],)) == 1


def test_reconcile_does_not_resurrect_the_login(world):
    server = world
    assert delete(server, CAROL) == 0
    root = store_root(server.data_dir, server.beta['id'])
    reconcile(root, RunRepo(server.db, server.beta['id']))
    assert AccountRepo(server.db).get(CAROL) is None


def test_delete_clears_detached_marker(world):
    server = world
    server.users_alpha.remove(CAROL)
    server.users_beta.remove(CAROL)
    carol = AccountRepo(server.db).get(CAROL)
    with server.db.transaction() as tx:
        assert tx.fetchone('SELECT value FROM server_settings WHERE name=?', ('detached-from:' + carol['id'],))
    assert delete(server, CAROL) == 0
    with server.db.transaction() as tx:
        assert tx.fetchone('SELECT value FROM server_settings WHERE name=?', ('detached-from:' + carol['id'],)) is None


def test_failure_rolls_back_everything(world, monkeypatch):
    server = world
    accounts = AccountRepo(server.db)

    def boom(self, needles, tx, **kwargs):
        raise RuntimeError('redaction failed')

    monkeypatch.setattr(AuditRepo, 'redact_account', boom)
    with pytest.raises(RuntimeError):
        accounts.delete(CAROL)
    assert accounts.get(CAROL) is not None
    assert [u.name for u in server.users_beta.list()].count(CAROL) == 1
    assert len([r for r in server.keys_beta.list() if r.user == CAROL]) == 1


def test_account_named_admin_can_be_deleted(world, capsys):
    server = world
    server.users_alpha.add('admin', 'member')
    log(server, 'admin', 'login.ok', 'admin', '4.4.4.4', {'method': 'password', 'role': 'admin'})
    assert delete(server, 'admin') == 0
    capsys.readouterr()
    assert AccountRepo(server.db).get('admin') is None
    row = [e for e in server_log(server) if e['action'] == 'login.ok'][0]
    assert row['actor'] == PLACEHOLDER and row['client'] is None
    assert row['detail'] == {'method': 'password', 'role': 'admin'}  # role is not a people key
    # the actor words stay reserved
    for word in ('cli', 'setup', 'anonymous'):
        AccountRepo(server.db).create(word)
        assert delete(server, word) == 1
        assert 'reserved' in capsys.readouterr().err


def test_throttle_buckets_naming_the_account_are_deleted(world):
    server = world
    accounts = AccountRepo(server.db)
    carol_id = accounts.get(CAROL)['id']
    with server.db.transaction(write=True) as tx:
        accounts.link_firebase(carol_id, 'Uid-Carol-9', CAROL, True, tx)
        for bucket in ('user:' + CAROL, 'user:Carol@Example.com', 'uid:Uid-Carol-9',
                       'user:dave@example.com', 'uid:other', '1.2.3.4', '10.0.0.0/24'):
            tx.execute('INSERT INTO login_attempts (bucket, failures, last_failure) VALUES (?, ?, ?)',
                       (bucket, 2, 1000.0))
    assert delete(server, CAROL, '--json') == 0
    with server.db.transaction() as tx:
        left = sorted(r[0] for r in tx.fetchall('SELECT bucket FROM login_attempts'))
    assert left == ['1.2.3.4', '10.0.0.0/24', 'uid:other', 'user:dave@example.com']


def personal_workspace(server, email, display):
    """What open sign-up leaves behind: the account, its personal workspace and the audit rows."""
    from agenticml.server.db.workspaces import slug_from_email_local
    AccountRepo(server.db).create(email, display, 'signup')
    slug = slug_from_email_local(email)
    row = WorkspaceRepo(server.db).create(slug, display + "'s workspace", email, plan='free', personal=1)
    prepare_store(server.data_dir, row['id'])
    log(server, email, 'account.signup', email, '8.8.8.8', {}, via='signup')
    log(server, email, 'workspace.create', row['id'], '8.8.8.8',
        {'slug': slug, 'name': display + "'s workspace"}, via='signup')
    log(server, email, 'login.ok', email, '8.8.8.8', {'method': 'firebase', 'provider': 'google.com'}, via='firebase')
    return row


def test_display_name_and_personal_workspace_do_not_survive(world, capsys):
    server = world
    row = personal_workspace(server, 'jane.doe@example.com', 'Jane Doe')
    other_id = 'f' * 32
    # Someone else's rows: same common name, different workspace and person.
    log(server, 'root', 'workspace.create', other_id, '9.9.9.9', {'slug': 'jane-doe', 'name': 'Jane Doe'})
    log(server, 'root', 'account.create', 'other@example.com', None, {'display_name': 'Jane Doe', 'pending': True})
    assert cli.main(['server', 'workspaces', 'delete', row['slug'], '--yes-delete', row['slug'],
                     '--data', str(server.data_dir)]) == 0
    capsys.readouterr()
    assert delete(server, 'jane.doe@example.com') == 0
    capsys.readouterr()
    events = server_log(server)
    mine = json.dumps([e for e in events if e['target'] not in (other_id, 'other@example.com')]).lower()
    for text in ('jane', 'doe', 'example.com', '8.8.8.8'):
        assert text not in mine, text
    removed = [e for e in events if e['action'] == 'workspace.delete'][0]
    assert removed['target'] == row['id'] and removed['detail'] == {'slug': PLACEHOLDER}
    other = [e for e in events if e['target'] == other_id][0]
    assert other['actor'] == 'root' and other['detail'] == {'slug': 'jane-doe', 'name': 'Jane Doe'}
    other_account = [e for e in events if e['target'] == 'other@example.com'][0]
    assert other_account['detail'] == {'display_name': 'Jane Doe', 'pending': True}


def test_display_name_is_replaced_under_display_name_key_only(world):
    server = world
    AccountRepo(server.db).create('kim@example.com', 'Kim Lee')
    log(server, 'root', 'account.create', 'kim@example.com', None, {'display_name': 'Kim Lee', 'server_admin': False})
    log(server, 'root', 'workspace.create', 'w3', None, {'name': 'Kim Lee', 'owner': 'someone'})
    log(server, 'root', 'workspace.create', 'w4', None, {'name': "Kim Lee's workspace", 'slug': 'kim-lee'})
    log(server, 'kim@example.com', 'workspace.create', 'w5', None, {'name': "Kim Lee's workspace", 'slug': 'kim-lee'})
    assert delete(server, 'kim@example.com') == 0
    by_target = {e['target']: e for e in server_log(server) if e['action'].startswith(('workspace', 'account.create'))}
    assert by_target[PLACEHOLDER]['detail'] == {'display_name': PLACEHOLDER, 'server_admin': False}
    assert by_target['w3']['detail'] == {'name': 'Kim Lee', 'owner': 'someone'}
    # Another person's workspace that merely has the same name is untouched; the person's own row is not.
    assert by_target['w4']['detail'] == {'name': "Kim Lee's workspace", 'slug': 'kim-lee'}
    assert by_target['w5']['detail'] == {'name': PLACEHOLDER, 'slug': 'kim-lee'}

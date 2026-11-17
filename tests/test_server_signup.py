"""Self-serve sign-up (package 18c.2) on both database backends."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import plistlib

import pytest

from agenticml import cli
from agenticml.server.app import make_server
from agenticml.server.db.errors import DatabaseError
from agenticml.server.db.repos import AuditRepo, SettingsRepo
from agenticml.server.db.usage import UsageRepo
from agenticml.server.db.workspaces import (
    AccountRepo, MembershipRepo, WorkspaceRepo, slug_from_email_local)
from agenticml.server.firebase import (
    OPEN_SIGNUP_REQUIRES_FIREBASE, OPEN_SIGNUP_REQUIRES_LIMITS)
from agenticml.server.limits import Plans, UNLIMITED
from agenticml.server.up import write_launchd_plist, write_systemd_unit
from agenticml.server.webauth import LoginThrottle
from agenticml.server.workspaces import discard_store, prepare_store, store_root
from test_server_firebase import (
    FIREBASE_CFG, _live_token, _member_account, _post_firebase, _source,
    firebase_running)
from test_server_web import authed, call, cookie_parts, csrf


def _plans(free_workspaces=1):
    return Plans({
        'default_plan': 'free',
        'free_workspaces_per_account': free_workspaces,
        'plans': {'free': dict(UNLIMITED)},
    })


def _limits_path(tmp_path, free_workspaces=1):
    path = tmp_path / 'limits.json'
    path.write_text(json.dumps({
        'default_plan': 'free',
        'free_workspaces_per_account': free_workspaces,
        'plans': {'free': dict(UNLIMITED)},
    }))
    return path


@contextmanager
def signup_running(data, **kwargs):
    kwargs.setdefault('open_signup', True)
    kwargs.setdefault('plans', _plans())
    with firebase_running(data, **kwargs) as httpd:
        yield httpd


def _accounts(httpd):
    return AccountRepo(httpd.db).list()


def _personal(httpd, login):
    account = AccountRepo(httpd.db).get(login)
    owned = []
    for row in WorkspaceRepo(httpd.db).list():
        if row['created_by'] == account['id'] and row['personal']:
            owned.append(row)
    return owned


def test_open_signup_without_firebase_refused(tmp_path, capsys):
    with pytest.raises(ValueError, match='firebase-project'):
        make_server(tmp_path / 'data', port=0, open_signup=True)
    assert cli.main([
        'server', 'serve', '--data', str(tmp_path), '--port', '0', '--open-signup',
    ]) == 1
    assert OPEN_SIGNUP_REQUIRES_FIREBASE in capsys.readouterr().err


@pytest.mark.parametrize('value', ['1', 'true', 'yes'])
def test_cli_passes_open_signup(tmp_path, monkeypatch, value):
    limits = _limits_path(tmp_path)
    monkeypatch.setenv('AGENTICML_SERVER_FIREBASE_PROJECT', 'demo-project')
    monkeypatch.setenv('AGENTICML_SERVER_FIREBASE_API_KEY', 'AIzaSyEnvKey')
    monkeypatch.setenv('AGENTICML_SERVER_OPEN_SIGNUP', value)
    monkeypatch.setenv('AGENTICML_SERVER_LIMITS', str(limits))
    seen = {}

    def fake_serve(*args, **kwargs):
        seen['kwargs'] = kwargs

    monkeypatch.setattr('agenticml.server.serve', fake_serve)
    assert cli.main(['server', 'serve', '--data', str(tmp_path), '--port', '0']) == 0
    assert seen['kwargs']['open_signup'] is True
    assert seen['kwargs']['plans'].free_workspaces_per_account == 1
    monkeypatch.delenv('AGENTICML_SERVER_OPEN_SIGNUP')
    monkeypatch.delenv('AGENTICML_SERVER_LIMITS')
    seen.clear()
    assert cli.main([
        'server', 'serve', '--data', str(tmp_path), '--port', '0', '--open-signup',
        '--firebase-project', 'demo-project', '--firebase-api-key', 'AIzaSyCli',
        '--limits', str(limits),
    ]) == 0
    assert seen['kwargs']['open_signup'] is True
    monkeypatch.setattr('agenticml.server.up.serve', fake_serve)
    monkeypatch.setattr('agenticml.server.up.check_tailscale', lambda: 'https://ex.ts.net')
    monkeypatch.setattr('agenticml.server.up.bootstrap_admin', lambda *a, **k: tmp_path)
    monkeypatch.setattr('agenticml.server.up.ensure_funnel_443_free', lambda port: None)
    monkeypatch.setattr('agenticml.server.up.funnel_on', lambda port: None)
    monkeypatch.setattr('agenticml.server.up.funnel_off', lambda: None)
    seen.clear()
    assert cli.main([
        'server', 'up', '--data', str(tmp_path), '--port', '0', '--open-signup',
        '--firebase-project', 'demo-project', '--firebase-api-key', 'AIzaSyUp',
        '--limits', str(limits),
    ]) == 0
    assert seen['kwargs']['open_signup'] is True
    assert seen['kwargs']['plans'].free_workspaces_per_account == 1


def test_verified_email_creates_account_and_personal_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data') as httpd:
        before = {row['login'] for row in _accounts(httpd)}
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-ada', email='ada@example.com', name='Ada'))
        assert status == 200 and json.loads(body) == {'ok': True, 'user': 'ada@example.com'}
        assert 'Set-Cookie' in hdrs
        logins = {row['login'] for row in _accounts(httpd)}
        assert logins - before == {'ada@example.com'}
        account = AccountRepo(httpd.db).by_firebase_uid('uid-ada')
        assert account['email'] == 'ada@example.com'
        assert account['email_verified'] == 1
        assert account['display_name'] == 'Ada'
        assert account['created_by'] == 'signup'
        personal = _personal(httpd, 'ada@example.com')
        assert len(personal) == 1
        row = personal[0]
        assert row['slug'] == 'ada'
        assert row['name'] == "Ada's workspace"
        assert row['plan'] == 'free' and row['personal'] == 1
        assert UsageRepo(httpd.db, row['id']).get()['stored_bytes'] == 0
        assert store_root(httpd.data_dir, row['id']).is_dir()
        status, _, session, _ = authed(
            httpd, '/api/account/session', cookie=hdrs['Set-Cookie'])
        assert status == 200
        listed = {ws['slug']: ws for ws in json.loads(session)['workspaces']}
        assert listed['ada']['name'] == "Ada's workspace"
        events = AuditRepo(httpd.db, None).tail(20)
        signup = [e for e in events if e['action'] == 'account.signup']
        created = [e for e in events if e['action'] == 'workspace.create']
        assert len(signup) == 1 and signup[0]['via'] == 'signup'
        assert signup[0]['target'] == 'ada@example.com'
        assert len(created) == 1 and created[0]['via'] == 'signup'
        assert created[0]['target'] == row['id']
        status, _, again = _post_firebase(
            httpd, _live_token(sub='uid-ada', email='ada@example.com', name='Ada'))
        assert status == 200
        assert {row['login'] for row in _accounts(httpd)} == logins
        assert len(_personal(httpd, 'ada@example.com')) == 1
        assert len([e for e in AuditRepo(httpd.db, None).tail(20)
                    if e['action'] == 'account.signup']) == 1


def test_signup_slug_collision_reserved_and_empty(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data') as httpd:
        taken = WorkspaceRepo(httpd.db).create(
            'user-name-tag', 'Taken', owner=AccountRepo(httpd.db).create('owner')['login'])
        prepare_store(httpd.data_dir, taken['id'])
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-tag', email='User.Name+tag@example.com', name='Pat'))
        assert status == 200
        personal = _personal(httpd, 'user.name+tag@example.com')
        assert personal[0]['slug'] == 'user-name-tag-2'
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-admin', email='admin@example.com'))
        assert status == 200
        assert _personal(httpd, 'admin@example.com')[0]['slug'] == 'admin-2'
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-empty', email='...@example.com', name=''))
        assert status == 200
        empty = _personal(httpd, '...@example.com')[0]
        assert empty['slug'] == 'workspace'
        assert empty['name'] == "...'s workspace"


def test_unverified_and_untrusted_create_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data') as httpd:
        before = {(row['login'], row.get('firebase_uid')) for row in _accounts(httpd)}
        workspaces = {row['slug'] for row in WorkspaceRepo(httpd.db).list()}
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-unverified', email='new@example.com',
                               email_verified=False))
        assert status == 403 and json.loads(body) == {
            'error': 'verify your email', 'reason': 'email_unverified'}
        assert 'Set-Cookie' not in hdrs
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-github', email='gh@example.com',
                               firebase={'sign_in_provider': 'github.com'}))
        assert status == 403 and json.loads(body) == {
            'error': 'verify your email', 'reason': 'email_unverified'}
        assert {(row['login'], row.get('firebase_uid')) for row in _accounts(httpd)} == before
        assert {row['slug'] for row in WorkspaceRepo(httpd.db).list()} == workspaces


def test_signup_off_is_no_account(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with firebase_running(tmp_path / 'data') as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-off', email='off@example.com'))
        assert status == 403 and json.loads(body) == {
            'error': 'no access', 'reason': 'no_account'}
        assert 'Set-Cookie' not in hdrs
        assert AccountRepo(httpd.db).by_firebase_uid('uid-off') is None


def test_limit_zero_creates_account_without_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data', plans=_plans(0)) as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-zero', email='zero@example.com', name='Zero'))
        assert status == 200 and json.loads(body)['user'] == 'zero@example.com'
        assert AccountRepo(httpd.db).by_firebase_uid('uid-zero') is not None
        assert _personal(httpd, 'zero@example.com') == []
        status, _, session, _ = authed(
            httpd, '/api/account/session', cookie=hdrs['Set-Cookie'])
        assert status == 200
        assert json.loads(session)['workspaces'] == []


def test_account_workspaces_create_limit_and_custom(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data', plans=_plans(1)) as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-lab', email='lab@example.com', name='Lab'))
        assert status == 200
        cookie = hdrs['Set-Cookie']
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'blocked', 'name': 'Blocked'}).encode(), cookie=cookie)
        assert status == 403 and json.loads(body) == {
            'error': 'you already have a free workspace'}
        personal = _personal(httpd, 'lab@example.com')[0]
        WorkspaceRepo(httpd.db).set_plan(personal['slug'], 'custom', dict(UNLIMITED))
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'second', 'name': 'Second'}).encode(), cookie=cookie)
        assert status == 201 and json.loads(body) == {'slug': 'second', 'name': 'Second'}
        row = WorkspaceRepo(httpd.db).get('second')
        assert row['plan'] == 'free' and row['personal'] == 0
        assert UsageRepo(httpd.db, row['id']).get()['stored_bytes'] == 0
        assert store_root(httpd.data_dir, row['id']).is_dir()
        created = [e for e in AuditRepo(httpd.db, None).tail(10)
                   if e['action'] == 'workspace.create' and e['via'] == 'dashboard']
        assert created and created[0]['target'] == row['id']
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'third', 'name': 'Third'}).encode(), cookie=cookie)
        assert status == 403 and json.loads(body) == {
            'error': 'you already have a free workspace'}
        assert WorkspaceRepo(httpd.db).get('third') is None


def test_account_workspaces_unverified_bad_taken_csrf_and_off(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data', plans=_plans(2)) as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-form', email='form@example.com'))
        cookie = hdrs['Set-Cookie']
        with httpd.db.transaction(write=True) as tx:
            tx.execute('UPDATE accounts SET email_verified=0 WHERE login=?',
                       ('form@example.com',))
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'extra', 'name': 'Extra'}).encode(), cookie=cookie)
        assert status == 403 and json.loads(body) == {'error': 'verify your email'}
        with httpd.db.transaction(write=True) as tx:
            tx.execute('UPDATE accounts SET email_verified=1 WHERE login=?',
                       ('form@example.com',))
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'admin', 'name': 'Nope'}).encode(), cookie=cookie)
        assert status == 400
        assert 'reserved' in json.loads(body)['error']
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'default', 'name': 'Nope'}).encode(), cookie=cookie)
        assert status == 409 and json.loads(body) == {
            'error': 'workspace slug already exists'}
        name, token, _ = cookie_parts(cookie)
        headers = csrf(httpd)
        del headers['X-AgenticML-Request']
        headers['Cookie'] = f'{name}={token}'
        status, _, body = call(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'csrf', 'name': 'CSRF'}).encode(), headers)
        assert status == 403 and json.loads(body) == {
            'error': 'cross-site request refused'}
    with firebase_running(tmp_path / 'off') as httpd:
        _member_account(httpd, 'off@example.com', uid='uid-off-form')
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-off-form', email='off@example.com'))
        assert status == 200
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'lab', 'name': 'Lab'}).encode(),
            cookie=hdrs['Set-Cookie'])
        assert status == 404 and json.loads(body) == {'error': 'not found'}


def test_open_signup_without_limits_refused(tmp_path, monkeypatch, capsys):
    with pytest.raises(ValueError, match='--limits'):
        make_server(
            tmp_path / 'data', port=0, firebase=FIREBASE_CFG, cert_source=_source(),
            open_signup=True)
    flags = [
        '--firebase-project', 'demo-project', '--firebase-api-key', 'AIzaSyCli',
        '--open-signup',
    ]
    assert cli.main(['server', 'serve', '--data', str(tmp_path), '--port', '0', *flags]) == 1
    assert OPEN_SIGNUP_REQUIRES_LIMITS in capsys.readouterr().err
    monkeypatch.setattr('agenticml.server.up.check_tailscale', lambda: 'https://ex.ts.net')
    monkeypatch.setattr('agenticml.server.up.bootstrap_admin', lambda *a, **k: tmp_path)
    assert cli.main(['server', 'up', '--data', str(tmp_path), '--port', '0', *flags]) == 1
    assert OPEN_SIGNUP_REQUIRES_LIMITS in capsys.readouterr().err
    assert cli.main([
        'server', 'up', '--data', str(tmp_path), '--port', '8080', '--install', *flags,
    ]) == 1
    assert OPEN_SIGNUP_REQUIRES_LIMITS in capsys.readouterr().err


def test_server_up_limits_passthrough_and_units(tmp_path, monkeypatch):
    from test_server_up import install_fake, run_up

    limits = _limits_path(tmp_path)
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setattr('agenticml.server.up._stdout_is_tty', lambda: False)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    data = tmp_path / 'data'
    monkeypatch.chdir(tmp_path)
    assert run_up(
        data, '--install',
        '--firebase-project', 'demo-project',
        '--firebase-api-key', 'AIzaSyTestKey',
        '--open-signup', '--limits', limits.name,
    ) == 0
    resolved = str(limits.resolve())
    text = (home / '.config/systemd/user/agenticml-server-up.service').read_text()
    assert '--open-signup' in text
    assert '--limits ' + resolved in text
    linux_home = tmp_path / 'linux-home'
    linux_home.mkdir()
    path = write_systemd_unit(
        data, 8080, home=linux_home, firebase=FIREBASE_CFG,
        open_signup=True, limits=limits.name)
    assert '--limits ' + resolved in path.read_text()
    mac_home = tmp_path / 'mac-home'
    mac_home.mkdir()
    plist = write_launchd_plist(
        data, 8080, home=mac_home, firebase=FIREBASE_CFG,
        open_signup=True, limits=limits.name)
    args = plistlib.loads(plist.read_bytes())['ProgramArguments']
    assert args[args.index('--limits') + 1] == resolved
    seen = {}

    def fake_serve(*a, **kwargs):
        seen['kwargs'] = kwargs

    monkeypatch.setattr('agenticml.server.up.serve', fake_serve)
    monkeypatch.setattr('agenticml.server.up.check_tailscale', lambda: 'https://ex.ts.net')
    monkeypatch.setattr('agenticml.server.up.bootstrap_admin', lambda *a, **k: tmp_path)
    monkeypatch.setattr('agenticml.server.up.ensure_funnel_443_free', lambda port: None)
    monkeypatch.setattr('agenticml.server.up.funnel_on', lambda port: None)
    monkeypatch.setattr('agenticml.server.up.funnel_off', lambda: None)
    assert cli.main([
        'server', 'up', '--data', str(tmp_path), '--port', '0',
        '--firebase-project', 'demo-project', '--firebase-api-key', 'AIzaSyUp',
        '--limits', str(limits),
    ]) == 0
    assert seen['kwargs']['plans'].free_workspaces_per_account == 1
    assert 'open_signup' not in seen['kwargs'] or seen['kwargs'].get('open_signup') is not True


def _workspace_create_events(httpd):
    return [e for e in AuditRepo(httpd.db, None).tail(20) if e['action'] == 'workspace.create']


def _signup_pending(httpd, login):
    account = AccountRepo(httpd.db).get(login)
    return SettingsRepo(httpd.db).get(f'signup_pending:{account["id"]}')


def test_signup_prepare_store_databaseerror_drops_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)

    def boom(*args, **kwargs):
        raise DatabaseError('workspace store path is a symlink or not a directory; fix or move it away')

    with signup_running(tmp_path / 'data') as httpd:
        monkeypatch.setattr('agenticml.server.workspaces.prepare_store', boom)
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-disk', email='disk@example.com', name='Disk'))
        assert status == 200 and json.loads(body)['user'] == 'disk@example.com'
        assert 'Set-Cookie' in hdrs
        assert AccountRepo(httpd.db).by_firebase_uid('uid-disk') is not None
        assert WorkspaceRepo(httpd.db).get('disk') is None
        assert _personal(httpd, 'disk@example.com') == []
        assert _workspace_create_events(httpd) == []
        assert _signup_pending(httpd, 'disk@example.com') == '1'
        status, _, session, _ = authed(
            httpd, '/api/account/session', cookie=hdrs['Set-Cookie'])
        assert status == 200
        assert json.loads(session)['workspaces'] == []


def _workspace_dir_ids(data):
    parent = data / 'workspaces'
    if not parent.is_dir():
        return set()
    return {p.name for p in parent.iterdir()}


def test_session_includes_open_signup(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data') as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-flag', email='flag@example.com'))
        assert status == 200
        status, _, session, _ = authed(
            httpd, '/api/account/session', cookie=hdrs['Set-Cookie'])
        assert status == 200
        assert json.loads(session)['open_signup'] is True


def test_signup_prepare_store_failure_removes_workspace_directory(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    real = prepare_store

    def boom(data, workspace_id):
        real(data, workspace_id)
        raise DatabaseError('workspace store path is a symlink or not a directory; fix or move it away')

    with signup_running(tmp_path / 'data') as httpd:
        before = _workspace_dir_ids(httpd.data_dir)
        monkeypatch.setattr('agenticml.server.workspaces.prepare_store', boom)
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-leftover', email='leftover@example.com', name='Left'))
        assert status == 200
        assert WorkspaceRepo(httpd.db).get('leftover') is None
        assert _workspace_dir_ids(httpd.data_dir) == before


def test_account_workspaces_prepare_store_failure_removes_workspace_directory(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    real = prepare_store

    def boom(data, workspace_id):
        real(data, workspace_id)
        raise OSError('disk full')

    with signup_running(tmp_path / 'data', plans=_plans(2)) as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-dir', email='dir@example.com', name='Dir'))
        cookie = hdrs['Set-Cookie']
        before = _workspace_dir_ids(httpd.data_dir)
        monkeypatch.setattr('agenticml.server.workspaces.prepare_store', boom)
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'orphan-dir', 'name': 'Orphan'}).encode(), cookie=cookie)
        assert status == 503 and json.loads(body) == {'error': 'could not create workspace'}
        assert WorkspaceRepo(httpd.db).get('orphan-dir') is None
        assert _workspace_dir_ids(httpd.data_dir) == before


def test_discard_store_unlinks_symlink_without_following(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('keep')
    data = tmp_path / 'data'
    parent = data / 'workspaces'
    parent.mkdir(parents=True)
    wid = 'a' * 32
    (parent / wid).symlink_to(outside)
    nested = parent / ('b' * 32)
    nested.mkdir()
    (nested / 'store').symlink_to(outside, target_is_directory=True)
    discard_store(data, wid)
    discard_store(data, 'b' * 32)
    assert not (parent / wid).exists()
    assert not nested.exists()
    assert (outside / 'secret').read_text() == 'keep'


def test_discard_store_unlinks_id_symlink_not_followed(tmp_path):
    """A workspaces/<id> symlink is unlinked; its target is left intact."""
    target = tmp_path / 'payload.txt'
    target.write_text('keep')
    data = tmp_path / 'data'
    parent = data / 'workspaces'
    parent.mkdir(parents=True)
    wid = 'c' * 32
    (parent / wid).symlink_to(target)
    discard_store(data, wid)
    assert list(parent.iterdir()) == []
    assert target.read_text() == 'keep'


def test_account_workspaces_prepare_store_databaseerror_drops_row(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)

    def boom(*args, **kwargs):
        raise DatabaseError('workspace store path is a symlink or not a directory; fix or move it away')

    with signup_running(tmp_path / 'data', plans=_plans(2)) as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-row', email='row@example.com', name='Row'))
        assert status == 200
        cookie = hdrs['Set-Cookie']
        before = {row['slug'] for row in WorkspaceRepo(httpd.db).list()}
        before_create = _workspace_create_events(httpd)
        monkeypatch.setattr('agenticml.server.workspaces.prepare_store', boom)
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'orphan', 'name': 'Orphan'}).encode(), cookie=cookie)
        assert status == 503 and json.loads(body) == {'error': 'could not create workspace'}
        assert WorkspaceRepo(httpd.db).get('orphan') is None
        assert {row['slug'] for row in WorkspaceRepo(httpd.db).list()} == before
        assert _workspace_create_events(httpd) == before_create
        status, _, session, _ = authed(
            httpd, '/api/account/session', cookie=cookie)
        assert status == 200
        assert any(ws['slug'] == 'row' for ws in json.loads(session)['workspaces'])


def test_admin_delete_does_not_recover_signup_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data') as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-deleted', email='deleted@example.com', name='Deleted'))
        assert status == 200
        personal = _personal(httpd, 'deleted@example.com')[0]
        WorkspaceRepo(httpd.db).delete(personal['slug'])
        assert AccountRepo(httpd.db).workspace_slugs('deleted@example.com') == []
        assert _signup_pending(httpd, 'deleted@example.com') is None
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-deleted', email='deleted@example.com', name='Deleted'))
        assert status == 200 and json.loads(body)['user'] == 'deleted@example.com'
        assert _personal(httpd, 'deleted@example.com') == []
        assert _signup_pending(httpd, 'deleted@example.com') is None
        assert len(_workspace_create_events(httpd)) == 1
        status, _, session, _ = authed(
            httpd, '/api/account/session', cookie=hdrs['Set-Cookie'])
        assert status == 200
        assert json.loads(session)['workspaces'] == []


def test_signup_account_with_removed_membership_gets_no_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data', plans=_plans(2)) as httpd:
        assert _post_firebase(
            httpd, _live_token(sub='uid-rm', email='rm@example.com', name='Rm'))[0] == 200
        account = AccountRepo(httpd.db).get('rm@example.com')
        with httpd.db.transaction(write=True) as tx:
            tx.cross_workspace('test: remove membership')
            tx.execute('DELETE FROM memberships WHERE account_id=?', (account['id'],))
        assert AccountRepo(httpd.db).workspace_slugs('rm@example.com') == []
        assert _post_firebase(
            httpd, _live_token(sub='uid-rm', email='rm@example.com', name='Rm'))[0] == 200
        assert AccountRepo(httpd.db).workspace_slugs('rm@example.com') == []
        assert len(_personal(httpd, 'rm@example.com')) == 1
        assert len(_workspace_create_events(httpd)) == 1


def _linked_cli_account(httpd, login, uid):
    repo = AccountRepo(httpd.db)
    repo.create(login, created_by='cli')
    with httpd.db.transaction(write=True) as tx:
        repo.link_firebase(repo.get(login, tx)['id'], uid, login, True, tx)


def _personal_marker(httpd, login):
    account = AccountRepo(httpd.db).get(login)
    return SettingsRepo(httpd.db).get(f'personal_workspace:{account["id"]}')


def test_non_signup_account_without_workspace_gets_one_once(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    login = 'cliacct@example.com'
    with signup_running(tmp_path / 'data') as httpd:
        _linked_cli_account(httpd, login, 'uid-cli')
        assert _personal_marker(httpd, login) is None
        assert _post_firebase(httpd, _live_token(sub='uid-cli', email=login))[0] == 200
        made = _personal(httpd, login)
        assert len(made) == 1 and made[0]['slug'] == 'cliacct'
        assert _personal_marker(httpd, login) == '1'
        assert len(_workspace_create_events(httpd)) == 1
        # a later sign-in does not create a second one
        assert _post_firebase(httpd, _live_token(sub='uid-cli', email=login))[0] == 200
        assert len(_personal(httpd, login)) == 1
        # an admin deletes it: the next sign-in does not bring it back
        WorkspaceRepo(httpd.db).delete(made[0]['slug'])
        assert AccountRepo(httpd.db).workspace_slugs(login) == []
        assert _post_firebase(httpd, _live_token(sub='uid-cli', email=login))[0] == 200
        assert _personal(httpd, login) == []
        assert len(_workspace_create_events(httpd)) == 1


def test_non_signup_account_store_failure_clears_marker_and_retries(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    login = 'cliretry@example.com'

    def boom(*args, **kwargs):
        raise DatabaseError('workspace store path is a symlink or not a directory; fix or move it away')

    with signup_running(tmp_path / 'data') as httpd:
        _linked_cli_account(httpd, login, 'uid-retry')
        monkeypatch.setattr('agenticml.server.workspaces.prepare_store', boom)
        status, _, body = _post_firebase(httpd, _live_token(sub='uid-retry', email=login))
        assert status == 200 and json.loads(body)['user'] == login
        assert _personal(httpd, login) == []
        assert _personal_marker(httpd, login) is None
        assert _workspace_create_events(httpd) == []
        monkeypatch.setattr('agenticml.server.workspaces.prepare_store', prepare_store)
        assert _post_firebase(httpd, _live_token(sub='uid-retry', email=login))[0] == 200
        assert len(_personal(httpd, login)) == 1
        assert _personal_marker(httpd, login) == '1'
        assert len(_workspace_create_events(httpd)) == 1


def test_account_with_membership_gets_no_personal_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data') as httpd:
        _member_account(httpd, 'member@example.com', uid='uid-member')
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-member', email='member@example.com'))
        assert status == 200 and json.loads(body)['user'] == 'member@example.com'
        assert _personal(httpd, 'member@example.com') == []
        assert _workspace_create_events(httpd) == []


def test_free_workspaces_zero_gives_existing_account_no_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data', plans=_plans(0)) as httpd:
        AccountRepo(httpd.db).create('nolimit@example.com', created_by='cli')
        with httpd.db.transaction(write=True) as tx:
            repo = AccountRepo(httpd.db)
            repo.link_firebase(
                repo.get('nolimit@example.com', tx)['id'], 'uid-nolimit',
                'nolimit@example.com', True, tx)
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-nolimit', email='nolimit@example.com'))
        assert status == 200 and json.loads(body)['user'] == 'nolimit@example.com'
        assert _personal(httpd, 'nolimit@example.com') == []
        assert _workspace_create_events(httpd) == []


def test_unverified_sign_in_of_account_without_workspace_creates_none(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data') as httpd:
        AccountRepo(httpd.db).create('unv-none@example.com', created_by='cli')
        with httpd.db.transaction(write=True) as tx:
            repo = AccountRepo(httpd.db)
            repo.link_firebase(
                repo.get('unv-none@example.com', tx)['id'], 'uid-unv-none',
                'unv-none@example.com', True, tx)
        status, _, _ = _post_firebase(
            httpd, _live_token(sub='uid-unv-none', email='unv-none@example.com',
                               email_verified=False))
        assert status == 200
        assert _personal(httpd, 'unv-none@example.com') == []
        assert _workspace_create_events(httpd) == []


def test_signup_recovers_after_prepare_failure_and_clears_marker(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)

    def boom(*args, **kwargs):
        raise DatabaseError('workspace store path is a symlink or not a directory; fix or move it away')

    with signup_running(tmp_path / 'data') as httpd:
        monkeypatch.setattr('agenticml.server.workspaces.prepare_store', boom)
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-recover', email='recover@example.com', name='Recover'))
        assert status == 200 and json.loads(body)['user'] == 'recover@example.com'
        assert _personal(httpd, 'recover@example.com') == []
        assert _signup_pending(httpd, 'recover@example.com') == '1'
        monkeypatch.setattr(
            'agenticml.server.workspaces.prepare_store', prepare_store)
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-recover', email='recover@example.com', name='Recover'))
        assert status == 200 and json.loads(body)['user'] == 'recover@example.com'
        restored = _personal(httpd, 'recover@example.com')
        assert len(restored) == 1
        assert restored[0]['slug'] == 'recover'
        assert restored[0]['name'] == "Recover's workspace"
        assert restored[0]['plan'] == 'free' and restored[0]['personal'] == 1
        assert _signup_pending(httpd, 'recover@example.com') is None
        events = AuditRepo(httpd.db, None).tail(20)
        assert len([e for e in events if e['action'] == 'account.signup']) == 1
        created = [e for e in events if e['action'] == 'workspace.create']
        assert len(created) == 1 and created[0]['via'] == 'signup'
        assert created[0]['target'] == restored[0]['id']
        status, _, session, _ = authed(
            httpd, '/api/account/session', cookie=hdrs['Set-Cookie'])
        assert status == 200
        assert any(ws['slug'] == 'recover' for ws in json.loads(session)['workspaces'])


def test_unverified_email_does_not_recover_signup_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)

    def boom(*args, **kwargs):
        raise DatabaseError('workspace store path is a symlink or not a directory; fix or move it away')

    with signup_running(tmp_path / 'data') as httpd:
        monkeypatch.setattr('agenticml.server.workspaces.prepare_store', boom)
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-unv-rec', email='unvrec@example.com', name='Unv'))
        assert status == 200
        assert _personal(httpd, 'unvrec@example.com') == []
        assert _signup_pending(httpd, 'unvrec@example.com') == '1'
        monkeypatch.setattr(
            'agenticml.server.workspaces.prepare_store', prepare_store)
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-unv-rec', email='unvrec@example.com',
                               name='Unv', email_verified=False))
        assert status == 200 and json.loads(body)['user'] == 'unvrec@example.com'
        assert _personal(httpd, 'unvrec@example.com') == []
        assert _signup_pending(httpd, 'unvrec@example.com') == '1'
        assert _workspace_create_events(httpd) == []


def test_account_workspaces_slug_409_is_throttled(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data', plans=_plans(2)) as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-probe', email='probe@example.com'))
        cookie = hdrs['Set-Cookie']
        payload = json.dumps({'slug': 'default', 'name': 'Nope'}).encode()
        for _ in range(LoginThrottle.FREE_FAILURES):
            status, _, body, _ = authed(
                httpd, '/api/account/workspaces', 'POST', payload, cookie=cookie)
            assert status == 409 and json.loads(body) == {
                'error': 'workspace slug already exists'}
        status, hdrs, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST', payload, cookie=cookie)
        assert status == 429
        assert json.loads(body) == {'error': 'too many attempts; try again later'}
        assert hdrs.get('Retry-After')


def test_free_workspaces_zero_refuses_with_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data', plans=_plans(0)) as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-none', email='none@example.com', name='None'))
        assert status == 200
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'lab', 'name': 'Lab'}).encode(),
            cookie=hdrs['Set-Cookie'])
        assert status == 403 and json.loads(body) == {
            'error': 'free workspaces are not available'}
        assert WorkspaceRepo(httpd.db).get('lab') is None


def test_concurrent_first_signins_one_account_one_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    token = _live_token(sub='uid-race-signup', email='racesignup@example.com', name='Race')
    with signup_running(tmp_path / 'data') as httpd:
        def once(_):
            return _post_firebase(httpd, token)

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(once, range(2)))
        statuses = [status for status, _, _ in results]
        assert all(status in (200, 429) for status in statuses)
        assert 200 in statuses
        rows = [row for row in AccountRepo(httpd.db).list()
                if row['login'] == 'racesignup@example.com']
        assert len(rows) == 1 and rows[0]['firebase_uid'] == 'uid-race-signup'
        assert len(_personal(httpd, 'racesignup@example.com')) == 1


def test_signup_slug_truncation_non_ascii_and_suffix(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    local = 'a' * 40
    truncated = 'a' * 32
    assert slug_from_email_local(local + '@example.com') == truncated
    assert slug_from_email_local('ü@example.com') == 'workspace'
    with signup_running(tmp_path / 'data') as httpd:
        taken = WorkspaceRepo(httpd.db).create(
            truncated, 'Taken', owner=AccountRepo(httpd.db).create('owner-trunc')['login'])
        prepare_store(httpd.data_dir, taken['id'])
        taken2 = WorkspaceRepo(httpd.db).create(
            truncated + '-2', 'Taken2', owner=AccountRepo(httpd.db).create('owner-trunc-2')['login'])
        prepare_store(httpd.data_dir, taken2['id'])
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-long', email=local + '@example.com', name='Long'))
        assert status == 200
        assert _personal(httpd, local + '@example.com')[0]['slug'] == truncated + '-3'
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-umlaut', email='ü@example.com', name='Umlaut'))
        assert status == 200
        assert _personal(httpd, 'ü@example.com')[0]['slug'] == 'workspace'


def test_owned_free_count_ignores_custom_and_non_owned(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data', plans=_plans(1)) as httpd:
        status, hdrs, body = _post_firebase(
            httpd, _live_token(sub='uid-count', email='count@example.com', name='Count'))
        assert status == 200
        cookie = hdrs['Set-Cookie']
        account = AccountRepo(httpd.db).by_firebase_uid('uid-count')
        MembershipRepo(httpd.db, httpd.default_workspace_id).add(account['id'], 'member')
        with httpd.db.transaction() as tx:
            assert WorkspaceRepo(httpd.db).owned_free_count(account['id'], tx) == 1
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'blocked-count', 'name': 'Blocked'}).encode(), cookie=cookie)
        assert status == 403 and json.loads(body) == {
            'error': 'you already have a free workspace'}
        personal = _personal(httpd, 'count@example.com')[0]
        WorkspaceRepo(httpd.db).set_plan(personal['slug'], 'custom', dict(UNLIMITED))
        with httpd.db.transaction() as tx:
            assert WorkspaceRepo(httpd.db).owned_free_count(account['id'], tx) == 0
        status, _, body, _ = authed(
            httpd, '/api/account/workspaces', 'POST',
            json.dumps({'slug': 'second-count', 'name': 'Second'}).encode(), cookie=cookie)
        assert status == 201 and json.loads(body) == {
            'slug': 'second-count', 'name': 'Second'}

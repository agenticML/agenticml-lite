"""Package 24.7a: admin emails, source tracking and the last-server-admin guard."""
import json
import plistlib

import pytest

from agenticml import cli
from agenticml.server.app import make_server
from agenticml.server.db.repos import AuditRepo
from agenticml.server.db.workspaces import (
    AccountRepo, InviteRepo, LastServerAdminError, MembershipRepo, WorkspaceRepo)
from agenticml.server.up import write_launchd_plist, write_systemd_unit
from test_server_db import url
from test_server_firebase import (
    FIREBASE_CFG, _db, _live_token, _member_account, _post_firebase, firebase_running)
from test_server_web import authed

OWNER = 'owner@example.com'


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.delenv('AGENTICML_SERVER_ADMIN_EMAILS', raising=False)


def _admins(emails=(OWNER,)):
    return frozenset(emails)


def _signin(httpd, **overrides):
    return _post_firebase(httpd, _live_token(**overrides))


def _account(httpd, login):
    return AccountRepo(httpd.db).get(login)


def _give_credential(db, login):
    """Link a Firebase uid so the account can sign in (pending admins do not count)."""
    repo = AccountRepo(db)
    with db.transaction(write=True) as tx:
        repo.link_firebase(repo.get(login, tx)['id'], 'uid-' + login, login, True, tx)


def _seed_admin(httpd, login='root@example.com', credential=True):
    """A manual server admin, so list revocations are not the last admin."""
    repo = AccountRepo(httpd.db)
    repo.create(login)
    repo.set_server_admin(login, True)
    if credential:
        _give_credential(httpd.db, login)
    return login


def _actions(httpd):
    return AuditRepo(httpd.db, None).tail(50)


# --- repo -----------------------------------------------------------------

def test_set_server_admin_sources_and_guard(tmp_path):
    db = _db(tmp_path)
    repo = AccountRepo(db)
    for login in ('a@example.com', 'b@example.com'):
        repo.create(login)
        _give_credential(db, login)
    row = repo.set_server_admin('a@example.com', True)
    assert row['server_admin'] == 1 and row['server_admin_source'] == 'manual'
    # a list grant never downgrades a manual grant
    row = repo.set_server_admin('a@example.com', True, source='list')
    assert row['server_admin_source'] == 'manual'
    # list grant on a non-admin records the list
    row = repo.set_server_admin('b@example.com', True, source='list')
    assert row['server_admin'] == 1 and row['server_admin_source'] == 'list'
    # a manual grant upgrades a list grant
    row = repo.set_server_admin('b@example.com', True, source='manual')
    assert row['server_admin_source'] == 'manual'
    row = repo.set_server_admin('b@example.com', True, source='list')
    assert row['server_admin_source'] == 'manual'
    assert [r['login'] for r in repo.server_admins()] == ['a@example.com', 'b@example.com']
    # disable clears the source
    row = repo.set_server_admin('b@example.com', False)
    assert row['server_admin'] == 0 and row['server_admin_source'] is None
    listed = {r['login']: r for r in repo.list()}
    assert listed['a@example.com']['server_admin_source'] == 'manual'
    assert listed['b@example.com']['server_admin_source'] is None
    # the last admin cannot be removed, and the row is unchanged
    with pytest.raises(LastServerAdminError, match='cannot remove the last server admin'):
        repo.set_server_admin('a@example.com', False)
    assert repo.get('a@example.com')['server_admin'] == 1
    assert repo.get('a@example.com')['server_admin_source'] == 'manual'
    with pytest.raises(ValueError, match='source'):
        repo.set_server_admin('a@example.com', True, source='bogus')
    with pytest.raises(ValueError, match='unknown account'):
        repo.set_server_admin('missing@example.com', True)
    db.close()


def test_set_server_admin_inside_passed_transaction(tmp_path):
    db = _db(tmp_path)
    repo = AccountRepo(db)
    repo.create('a@example.com')
    repo.create('b@example.com')
    _give_credential(db, 'a@example.com')
    _give_credential(db, 'b@example.com')
    with db.transaction(write=True) as tx:
        repo.set_server_admin('a@example.com', True, tx=tx)
        row = repo.set_server_admin('b@example.com', True, source='list', tx=tx)
        assert row['server_admin_source'] == 'list'
        assert [r['login'] for r in repo.server_admins(tx)] == ['a@example.com', 'b@example.com']
        repo.set_server_admin('a@example.com', False, tx=tx)
        with pytest.raises(LastServerAdminError):
            repo.set_server_admin('b@example.com', False, tx=tx)
    assert [r['login'] for r in repo.server_admins()] == ['b@example.com']
    db.close()


def test_guard_counts_only_admins_that_can_sign_in(tmp_path):
    db = _db(tmp_path)
    repo = AccountRepo(db)
    for login in ('real@example.com', 'pending@example.com', 'pending2@example.com'):
        repo.create(login)
        repo.set_server_admin(login, True)
    _give_credential(db, 'real@example.com')
    # a pending admin does not satisfy the guard for the one that can sign in
    with pytest.raises(LastServerAdminError, match='cannot remove the last server admin'):
        repo.set_server_admin('real@example.com', False)
    assert repo.get('real@example.com')['server_admin'] == 1
    # a pending admin can always be removed, even when it is the only other row
    row = repo.set_server_admin('pending@example.com', False)
    assert row['server_admin'] == 0
    repo.set_server_admin('pending2@example.com', False)
    assert [r['login'] for r in repo.server_admins()] == ['real@example.com']
    # a second admin that can sign in (password or identity) does count
    repo.create('second@example.com')
    repo.set_server_admin('second@example.com', True)
    with db.transaction(write=True) as tx:
        tx.execute('UPDATE accounts SET identity=? WHERE login=?', ('second-id', 'second@example.com'))
    assert repo.set_server_admin('real@example.com', False)['server_admin'] == 0
    with pytest.raises(LastServerAdminError):
        repo.set_server_admin('second@example.com', False)
    db.close()


# --- migration ------------------------------------------------------------

def test_migration_0008_keeps_existing_admin_with_null_source(tmp_path):
    from agenticml.server.db import connect, migrate
    from agenticml.server.app import prepare_data_dir
    import shutil

    prepare_data_dir(tmp_path)
    old = tmp_path / 'old-migrations'
    old.mkdir()
    for path in migrate.MIGRATIONS.glob('[0-9][0-9][0-9][0-9]_*'):
        if int(path.name[:4]) <= 7 and path.suffix in ('.sql', '.py'):
            shutil.copyfile(path, old / path.name)
    target = url(tmp_path / 'server' / 'agenticml.db')
    real = migrate.MIGRATIONS
    migrate.MIGRATIONS = old
    try:
        db = connect(target)
    finally:
        migrate.MIGRATIONS = real
    with db.transaction(write=True) as tx:
        assert tx.fetchone('SELECT MAX(version) FROM schema_migrations')[0] == 7
        tx.execute('INSERT INTO accounts (id, login, display_name, created_at, created_by, server_admin) '
                   'VALUES (?, ?, ?, ?, ?, ?)', ('c' * 32, 'old-admin', 'Old', '2026-01-01T00:00:00Z', 'cli', 1))
    db.close()
    db = connect(target)
    with db.transaction() as tx:
        assert tx.fetchone('SELECT MAX(version) FROM schema_migrations')[0] >= 8
    row = AccountRepo(db).get('old-admin')
    assert row['server_admin'] == 1 and row['server_admin_source'] is None
    assert [r['server_admin_source'] for r in AccountRepo(db).list()] == [None]
    db.close()


# --- sign-in --------------------------------------------------------------

def test_listed_email_creates_server_admin_without_workspace(tmp_path):
    with firebase_running(tmp_path / 'data', admin_emails=_admins()) as httpd:
        workspaces_before = {w['slug'] for w in WorkspaceRepo(httpd.db).list()}
        status, hdrs, body = _signin(httpd, sub='uid-owner', email=OWNER, name='Own Er')
        assert status == 200 and json.loads(body) == {'ok': True, 'user': OWNER}
        account = _account(httpd, OWNER)
        assert account['created_by'] == 'admin-emails'
        assert account['server_admin'] == 1 and account['server_admin_source'] == 'list'
        assert account['display_name'] == 'Own Er'
        assert account['firebase_uid'] == 'uid-owner' and account['email_verified'] == 1
        assert {w['slug'] for w in WorkspaceRepo(httpd.db).list()} == workspaces_before
        assert AccountRepo(httpd.db).workspaces(OWNER) == []
        status, _, page, _ = authed(httpd, '/workspaces', cookie=hdrs['Set-Cookie'])
        assert status == 200 and b'href="/admin/"' in page
        events = _actions(httpd)
        assert any(e['action'] == 'account.create' and e['target'] == OWNER
                   and e['actor'] == OWNER and e['via'] == 'admin-emails'
                   and e['detail'] == {'created_by': 'admin-emails'} for e in events)
        assert any(e['action'] == 'account.server_admin' and e['target'] == OWNER
                   and e['via'] == 'admin-emails'
                   and e['detail'] == {'enabled': True, 'source': 'admin-emails'} for e in events)


def test_list_matches_email_case_insensitively(tmp_path):
    with firebase_running(tmp_path / 'data', admin_emails=_admins({'Owner@Example.COM'})) as httpd:
        assert httpd.web.admin_emails == frozenset({OWNER})
        status, _, _ = _signin(httpd, sub='uid-case', email='OWNER@example.com')
        assert status == 200
        assert _account(httpd, OWNER)['server_admin'] == 1


def test_unverified_or_untrusted_email_not_created_or_granted(tmp_path):
    with firebase_running(tmp_path / 'data', admin_emails=_admins()) as httpd:
        before = {r['login'] for r in AccountRepo(httpd.db).list()}
        status, _, body = _signin(httpd, sub='uid-unv', email=OWNER, email_verified=False)
        assert status == 403 and json.loads(body)['reason'] == 'no_account'
        status, _, body = _signin(
            httpd, sub='uid-gh', email=OWNER, firebase={'sign_in_provider': 'github.com'})
        assert status == 403 and json.loads(body)['reason'] == 'no_account'
        assert {r['login'] for r in AccountRepo(httpd.db).list()} == before
        assert not any(r['server_admin'] for r in AccountRepo(httpd.db).list())


def test_listed_email_with_pending_account_is_linked_and_granted(tmp_path):
    with firebase_running(tmp_path / 'data', admin_emails=_admins()) as httpd:
        pending = AccountRepo(httpd.db).create(OWNER, created_by='cli')
        status, _, _ = _signin(httpd, sub='uid-pending', email=OWNER)
        assert status == 200
        account = _account(httpd, OWNER)
        assert account['id'] == pending['id'] and account['created_by'] == 'cli'
        assert account['firebase_uid'] == 'uid-pending'
        assert account['server_admin'] == 1 and account['server_admin_source'] == 'list'


def test_listed_email_with_password_account_gets_nothing(tmp_path):
    with firebase_running(tmp_path / 'data', admin_emails=_admins()) as httpd:
        httpd.users.add(OWNER, role='member')
        from test_server_signin import PASSWORD
        httpd.users.set_password(OWNER, PASSWORD)
        status, _, body = _signin(httpd, sub='uid-pw', email=OWNER)
        assert status == 403 and json.loads(body)['reason'] == 'no_account'
        account = _account(httpd, OWNER)
        assert account['firebase_uid'] is None and account['server_admin'] == 0
        assert account['server_admin_source'] is None


def test_list_changes_grant_and_revoke_at_next_sign_in(tmp_path, caplog):
    data = tmp_path / 'data'
    with firebase_running(data) as httpd:
        _seed_admin(httpd)
        _member_account(httpd, 'ada@example.com', uid='uid-ada')
        assert _signin(httpd, sub='uid-ada', email='ada@example.com')[0] == 200
        assert _account(httpd, 'ada@example.com')['server_admin'] == 0
    with firebase_running(data, admin_emails=_admins({'ada@example.com'})) as httpd:
        assert _signin(httpd, sub='uid-ada', email='ada@example.com')[0] == 200
        account = _account(httpd, 'ada@example.com')
        assert account['server_admin'] == 1 and account['server_admin_source'] == 'list'
    with firebase_running(data) as httpd:
        assert _signin(httpd, sub='uid-ada', email='ada@example.com')[0] == 200
        account = _account(httpd, 'ada@example.com')
        assert account['server_admin'] == 0 and account['server_admin_source'] is None
        assert any(e['action'] == 'account.server_admin' and e['target'] == 'ada@example.com'
                   and e['detail'] == {'enabled': False, 'source': 'admin-emails'}
                   for e in _actions(httpd))
        assert _account(httpd, 'root@example.com')['server_admin'] == 1


def test_last_list_admin_is_kept_with_warning(tmp_path, caplog):
    data = tmp_path / 'data'
    with firebase_running(data, admin_emails=_admins()) as httpd:
        assert _signin(httpd, sub='uid-owner', email=OWNER)[0] == 200
    caplog.set_level('WARNING')
    with firebase_running(data) as httpd:
        status, hdrs, _ = _signin(httpd, sub='uid-owner', email=OWNER)
        assert status == 200 and hdrs.get('Set-Cookie')
        account = _account(httpd, OWNER)
        assert account['server_admin'] == 1 and account['server_admin_source'] == 'list'
    assert 'last one' in caplog.text


def test_manual_admin_not_on_list_is_kept(tmp_path):
    with firebase_running(tmp_path / 'data', admin_emails=_admins()) as httpd:
        _seed_admin(httpd, 'other@example.com')
        manual = _seed_admin(httpd, 'manual@example.com', credential=False)
        account_id = _account(httpd, manual)['id']
        with httpd.db.transaction(write=True) as tx:
            AccountRepo(httpd.db).link_firebase(account_id, 'uid-manual', manual, True, tx)
        assert _signin(httpd, sub='uid-manual', email=manual)[0] == 200
        account = _account(httpd, manual)
        assert account['server_admin'] == 1 and account['server_admin_source'] == 'manual'


def test_manual_grant_survives_list_membership_and_removal(tmp_path):
    data = tmp_path / 'data'
    with firebase_running(data, admin_emails=_admins()) as httpd:
        repo = AccountRepo(httpd.db)
        repo.create(OWNER)
        repo.set_server_admin(OWNER, True)  # manual
        assert _signin(httpd, sub='uid-owner', email=OWNER)[0] == 200
        assert _account(httpd, OWNER)['server_admin_source'] == 'manual'
    with firebase_running(data) as httpd:
        _seed_admin(httpd)
        assert _signin(httpd, sub='uid-owner', email=OWNER)[0] == 200
        assert _account(httpd, OWNER)['server_admin'] == 1


def test_invite_redemption_by_listed_email_grants_admin(tmp_path):
    with firebase_running(tmp_path / 'data', admin_emails=_admins()) as httpd:
        invite = InviteRepo(httpd.db, httpd.default_workspace_id).create(OWNER, 'member', 'admin')
        status, _, body = _post_firebase(
            httpd, _live_token(sub='uid-inv', email=OWNER), invite=invite)
        assert status == 200 and json.loads(body)['user'] == OWNER
        account = _account(httpd, OWNER)
        assert account['server_admin'] == 1 and account['server_admin_source'] == 'list'
        member = MembershipRepo(httpd.db, httpd.default_workspace_id).get(account['id'])
        assert member['role'] == 'member'


def test_open_signup_with_listed_email_uses_list_path(tmp_path):
    from test_server_signup import signup_running

    with signup_running(tmp_path / 'data', admin_emails=_admins()) as httpd:
        status, _, _ = _signin(httpd, sub='uid-owner', email=OWNER)
        assert status == 200
        account = _account(httpd, OWNER)
        assert account['created_by'] == 'admin-emails' and account['server_admin'] == 1
        assert [w for w in WorkspaceRepo(httpd.db).list() if w['personal']] == []
        # an unlisted address still signs up normally
        assert _signin(httpd, sub='uid-ada', email='ada@example.com')[0] == 200
        assert _account(httpd, 'ada@example.com')['created_by'] == 'signup'
        assert _account(httpd, 'ada@example.com')['server_admin'] == 0


def test_firebase_off_ignores_list_with_warning(tmp_path, caplog):
    caplog.set_level('WARNING')
    httpd = make_server(tmp_path / 'data', port=0, admin_emails=_admins())
    try:
        assert httpd.web.admin_emails == frozenset()
    finally:
        httpd.server_close()
    assert '--admin-emails is ignored without --firebase-project' in caplog.text


def test_startup_logs_count_not_addresses(tmp_path, caplog):
    caplog.set_level('INFO')
    with firebase_running(tmp_path / 'data', admin_emails=_admins({OWNER, 'two@example.com'})):
        pass
    assert 'server admin emails configured: 2' in caplog.text
    assert OWNER not in caplog.text


# --- configuration --------------------------------------------------------

def _fake_serve(seen):
    def fake(*args, **kwargs):
        seen['kwargs'] = kwargs
    return fake


def test_cli_serve_passes_admin_emails_from_flag_and_env(tmp_path, monkeypatch, caplog):
    seen = {}
    monkeypatch.setattr('agenticml.server.serve', _fake_serve(seen))
    monkeypatch.setenv('AGENTICML_SERVER_ADMIN_EMAILS', ' Env@Example.com, ,env2@example.com ')
    assert cli.main(['server', 'serve', '--data', str(tmp_path), '--port', '0']) == 0
    assert seen['kwargs']['admin_emails'] == frozenset({'env@example.com', 'env2@example.com'})
    seen.clear()
    assert cli.main(['server', 'serve', '--data', str(tmp_path), '--port', '0',
                     '--admin-emails', 'Flag@Example.com']) == 0
    assert seen['kwargs']['admin_emails'] == frozenset({'flag@example.com'})
    seen.clear()
    monkeypatch.delenv('AGENTICML_SERVER_ADMIN_EMAILS')
    assert cli.main(['server', 'serve', '--data', str(tmp_path), '--port', '0']) == 0
    assert 'admin_emails' not in seen['kwargs']
    seen.clear()
    caplog.set_level('WARNING')
    assert cli.main(['server', 'serve', '--data', str(tmp_path), '--port', '0',
                     '--admin-emails', 'good@example.com,not-an-email,@bad,']) == 0
    assert seen['kwargs']['admin_emails'] == frozenset({'good@example.com'})
    assert 'not-an-email' in caplog.text and '@bad' in caplog.text


def test_cli_up_passes_admin_emails(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr('agenticml.server.up.serve', _fake_serve(seen))
    monkeypatch.setattr('agenticml.server.up.check_tailscale', lambda: 'https://ex.ts.net')
    monkeypatch.setattr('agenticml.server.up.bootstrap_admin', lambda *a, **k: tmp_path)
    monkeypatch.setattr('agenticml.server.up.ensure_funnel_443_free', lambda port: None)
    monkeypatch.setattr('agenticml.server.up.funnel_on', lambda port: None)
    monkeypatch.setattr('agenticml.server.up.funnel_off', lambda: None)
    base = ['server', 'up', '--data', str(tmp_path), '--port', '0',
            '--firebase-project', 'demo-project', '--firebase-api-key', 'AIzaSyUp']
    assert cli.main([*base, '--admin-emails', 'Up@Example.com']) == 0
    assert seen['kwargs']['admin_emails'] == frozenset({'up@example.com'})
    seen.clear()
    monkeypatch.setenv('AGENTICML_SERVER_ADMIN_EMAILS', 'env@example.com')
    assert cli.main(base) == 0
    assert seen['kwargs']['admin_emails'] == frozenset({'env@example.com'})


def test_install_units_carry_admin_emails(tmp_path, monkeypatch):
    from test_server_up import install_fake, run_up

    install_fake(tmp_path, monkeypatch)
    monkeypatch.setattr('agenticml.server.up._stdout_is_tty', lambda: False)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    assert run_up(
        tmp_path / 'data', '--install',
        '--firebase-project', 'demo-project', '--firebase-api-key', 'AIzaSyTestKey',
        '--admin-emails', 'b@example.com,A@example.com') == 0
    text = (home / '.config/systemd/user/agenticml-server-up.service').read_text()
    assert '--admin-emails a@example.com,b@example.com' in text
    linux_home = tmp_path / 'linux-home'
    linux_home.mkdir()
    path = write_systemd_unit(
        tmp_path / 'data', 8080, home=linux_home, firebase=FIREBASE_CFG,
        admin_emails=frozenset({'x@example.com'}))
    assert '--admin-emails x@example.com' in path.read_text()
    assert '--admin-emails' not in write_systemd_unit(
        tmp_path / 'data', 8080, home=linux_home, force=True, firebase=FIREBASE_CFG).read_text()
    mac_home = tmp_path / 'mac-home'
    mac_home.mkdir()
    plist = write_launchd_plist(
        tmp_path / 'data', 8080, home=mac_home, firebase=FIREBASE_CFG,
        admin_emails=frozenset({'y@example.com', 'x@example.com'}))
    args = plistlib.loads(plist.read_bytes())['ProgramArguments']
    assert args[args.index('--admin-emails') + 1] == 'x@example.com,y@example.com'


# --- CLI ------------------------------------------------------------------

def test_cli_set_server_admin_off_refuses_last_admin_and_lists_source(tmp_path, capsys):
    data = str(tmp_path)
    assert cli.main(['server', 'accounts', 'add', 'op@example.com', '--server-admin',
                     '--data', data]) == 0
    capsys.readouterr()
    from agenticml.server.db import connect
    db = connect(url(tmp_path / 'server' / 'agenticml.db'))
    _give_credential(db, 'op@example.com')
    db.close()
    assert cli.main(['server', 'accounts', 'set-server-admin', 'op@example.com', 'off',
                     '--data', data]) == 1
    assert 'cannot remove the last server admin' in capsys.readouterr().err
    assert cli.main(['server', 'accounts', 'list', '--data', data]) == 0
    out = capsys.readouterr().out
    header, row = [line for line in out.splitlines() if line.strip()][:2]
    assert 'source' in header
    assert 'manual' in row
    assert cli.main(['server', 'accounts', 'list', '--json', '--data', data]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]['server_admin_source'] == 'manual'
    assert cli.main(['server', 'accounts', 'add', 'plain@example.com', '--data', data]) == 0
    assert cli.main(['server', 'accounts', 'list', '--data', data]) == 0
    plain = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith('plain@example.com')][0]
    assert ' - ' in plain
    db = connect(url(tmp_path / 'server' / 'agenticml.db'))
    _give_credential(db, 'plain@example.com')
    db.close()
    assert cli.main(['server', 'accounts', 'set-server-admin', 'plain@example.com', 'on',
                     '--data', data]) == 0
    assert cli.main(['server', 'accounts', 'set-server-admin', 'op@example.com', 'off',
                     '--data', data]) == 0

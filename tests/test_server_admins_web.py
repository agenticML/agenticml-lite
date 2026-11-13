"""Package 24.7b: server admins and workspaces for new owners on /admin."""
import json

import pytest

from agenticml.server.db.repos import AuditRepo
from agenticml.server.db.workspaces import AccountRepo, MembershipRepo, WorkspaceRepo
from test_server_firebase import _live_token, _member_account, _post_firebase, firebase_running
from test_server_web import authed, call, csrf

OWNER = 'owner@example.com'
API = '/api/admin/server-admins'


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.delenv('AGENTICML_SERVER_ADMIN_EMAILS', raising=False)


def _sign_in(httpd, email, sub):
    status, hdrs, body = _post_firebase(httpd, _live_token(sub=sub, email=email))
    assert status == 200, body
    return hdrs['Set-Cookie']


def _ask(httpd, path, method='GET', body=None, cookie=None, extra=None):
    payload = None if body is None else json.dumps(body).encode()
    headers = csrf(httpd) if method in ('POST', 'PATCH', 'DELETE') else {}
    headers.update(extra or {})
    status, _, raw, _ = authed(httpd, path, method, payload, extra=headers or None, cookie=cookie)
    return status, json.loads(raw) if raw else None


def _events(httpd):
    return AuditRepo(httpd.db, None).tail(100)


@pytest.fixture
def web(tmp_path):
    with firebase_running(tmp_path / 'data', admin_emails=frozenset({OWNER})) as httpd:
        cookie = _sign_in(httpd, OWNER, 'uid-owner')
        yield httpd, cookie


def test_list_add_existing_and_remove(web):
    httpd, cookie = web
    status, body = _ask(httpd, API, cookie=cookie)
    assert status == 200
    (row,) = body['server_admins']
    assert row['login'] == OWNER and row['source'] == 'list' and row['firebase'] is True
    assert row['signin'] == 'firebase' and row['pending'] is False and row['email'] == OWNER and 'created_at' in row

    _member_account(httpd, 'ada@example.com', uid='uid-ada')
    status, body = _ask(httpd, API, 'POST', {'login': 'Ada@Example.com'}, cookie)
    assert status == 200 and body['account_created'] is False
    assert body['server_admin']['login'] == 'ada@example.com'
    assert body['server_admin']['source'] == 'manual'
    assert AccountRepo(httpd.db).get('ada@example.com')['server_admin'] == 1
    # adding an existing list admin turns its source into manual
    status, body = _ask(httpd, API, 'POST', {'login': OWNER}, cookie)
    assert status == 200 and body['server_admin']['source'] == 'manual'
    status, body = _ask(httpd, API, cookie=cookie)
    assert [r['login'] for r in body['server_admins']] == ['ada@example.com', OWNER]

    status, body = _ask(httpd, API + '/ada%40example.com', 'DELETE', cookie=cookie)
    assert status == 200 and body['removed'] == 'ada@example.com'
    assert 'next sign-in' in body['note']
    assert AccountRepo(httpd.db).get('ada@example.com')['server_admin'] == 0
    assert AccountRepo(httpd.db).get('ada@example.com')['server_admin_source'] is None
    # not (or no longer) a server admin, or unknown
    status, body = _ask(httpd, API + '/ada@example.com', 'DELETE', cookie=cookie)
    assert status == 404 and body == {'error': 'unknown account'}
    status, body = _ask(httpd, API + '/nobody', 'DELETE', cookie=cookie)
    assert status == 404 and body == {'error': 'unknown account'}


def test_add_new_email_creates_pending_account_claimed_at_sign_in(web):
    httpd, cookie = web
    status, body = _ask(httpd, API, 'POST', {'login': 'New.Admin@Example.com'}, cookie)
    assert status == 201 and body['account_created'] is True
    row = body['server_admin']
    assert row['login'] == 'new.admin@example.com' and row['pending'] is True
    assert row['firebase'] is False and row['source'] == 'manual' and row['signin'] == 'pending'
    account = AccountRepo(httpd.db).get('new.admin@example.com')
    assert account['created_by'] == OWNER and account['server_admin'] == 1
    assert AccountRepo(httpd.db).workspaces('new.admin@example.com') == []

    new_cookie = _sign_in(httpd, 'new.admin@example.com', 'uid-new')
    claimed = AccountRepo(httpd.db).get('new.admin@example.com')
    assert claimed['id'] == account['id'] and claimed['firebase_uid'] == 'uid-new'
    assert claimed['server_admin'] == 1 and claimed['server_admin_source'] == 'manual'
    status, body = _ask(httpd, API, cookie=new_cookie)
    assert status == 200
    rows = {r['login']: r for r in body['server_admins']}
    assert rows['new.admin@example.com']['pending'] is False
    assert rows['new.admin@example.com']['firebase'] is True
    assert rows['new.admin@example.com']['signin'] == 'firebase'


def test_add_validation_and_unknown_non_email(web):
    httpd, cookie = web
    status, body = _ask(httpd, API, 'POST', {'login': 'nobody'}, cookie)
    assert status == 404 and body == {'error': 'unknown account'}
    assert AccountRepo(httpd.db).get('nobody') is None
    for payload in ({}, {'login': 5}, {'login': '  '}, {'login': 'x' * 300}):
        status, body = _ask(httpd, API, 'POST', payload, cookie)
        assert status == 400 and body == {'error': 'invalid request'}
    # a known non-email login can be promoted
    httpd.users.add('carol', role='member')
    status, body = _ask(httpd, API, 'POST', {'login': 'carol'}, cookie)
    assert status == 200 and body['server_admin']['login'] == 'carol'
    assert body['server_admin']['email'] is None
    assert body['server_admin']['signin'] == 'pending'
    httpd.users.set_password('carol', 'a-long-enough-password-1')
    rows = {r['login']: r for r in _ask(httpd, API, cookie=cookie)[1]['server_admins']}
    assert rows['carol']['signin'] == 'password' and rows['carol']['pending'] is False
    with httpd.db.transaction(write=True) as tx:
        tx.execute('UPDATE accounts SET password=NULL, identity=? WHERE login=?', ('carol-id', 'carol'))
    rows = {r['login']: r for r in _ask(httpd, API, cookie=cookie)[1]['server_admins']}
    assert rows['carol']['signin'] == 'identity'


def test_remove_last_admin_is_409_and_unchanged(web):
    httpd, cookie = web
    status, body = _ask(httpd, API + '/' + OWNER, 'DELETE', cookie=cookie)
    assert status == 409 and body == {'error': 'cannot remove the last server admin'}
    account = AccountRepo(httpd.db).get(OWNER)
    assert account['server_admin'] == 1 and account['server_admin_source'] == 'list'
    assert _ask(httpd, API, cookie=cookie)[0] == 200


def test_pending_admin_does_not_satisfy_the_last_admin_guard(web):
    httpd, cookie = web
    # a typo'd (pending) address must not let the only working admin be removed
    assert _ask(httpd, API, 'POST', {'login': 'typo@exmaple.com'}, cookie)[0] == 201
    status, body = _ask(httpd, API + '/' + OWNER, 'DELETE', cookie=cookie)
    assert status == 409 and body == {'error': 'cannot remove the last server admin'}
    assert AccountRepo(httpd.db).get(OWNER)['server_admin'] == 1
    # the pending admin itself can always be removed
    status, body = _ask(httpd, API + '/typo@exmaple.com', 'DELETE', cookie=cookie)
    assert status == 200 and body['removed'] == 'typo@exmaple.com'
    assert AccountRepo(httpd.db).get('typo@exmaple.com')['server_admin'] == 0


def test_self_removal_allowed_when_another_admin_exists(web):
    httpd, cookie = web
    _member_account(httpd, 'two@example.com', uid='uid-two')
    assert _ask(httpd, API, 'POST', {'login': 'two@example.com'}, cookie)[0] == 200
    status, body = _ask(httpd, API + '/' + OWNER, 'DELETE', cookie=cookie)
    assert status == 200 and body['removed'] == OWNER
    assert AccountRepo(httpd.db).get(OWNER)['server_admin'] == 0
    # the next request is no longer an admin request
    status, body = _ask(httpd, API, cookie=cookie)
    assert status == 404 and body == {'error': 'not found'}


def test_non_admin_and_anonymous_see_unknown_route_answers(web):
    httpd, cookie = web
    _member_account(httpd, 'bob@example.com', uid='uid-bob')
    bob = _sign_in(httpd, 'bob@example.com', 'uid-bob')
    cases = (
        ('GET', API, None, '/api/no-such'),
        ('POST', API, {'login': 'bob@example.com'}, '/api/no-such'),
        ('DELETE', API + '/' + OWNER, None, '/api/no-such'),
    )
    for method, path, body, unknown in cases:
        got = _ask(httpd, path, method, body, bob)
        assert got == _ask(httpd, unknown, method, body, bob) == (404, {'error': 'not found'})
        anon = call(httpd, path, method, None if body is None else json.dumps(body).encode(),
                    csrf(httpd) if method != 'GET' else {})
        anon_unknown = call(httpd, unknown, method, None if body is None else json.dumps(body).encode(),
                            csrf(httpd) if method != 'GET' else {})
        assert anon[0] == anon_unknown[0] == 401 and anon[2] == anon_unknown[2]
    assert AccountRepo(httpd.db).get('bob@example.com')['server_admin'] == 0
    assert AccountRepo(httpd.db).get(OWNER)['server_admin'] == 1


def test_csrf_required_for_add_and_remove(web):
    httpd, cookie = web
    _ask(httpd, API, 'POST', {'login': 'two@example.com'}, cookie)
    name = cookie.split(';', 1)[0]
    headers = {'Cookie': name, 'Content-Type': 'application/json'}
    status, _, raw = call(httpd, API, 'POST', json.dumps({'login': 'three@example.com'}).encode(), headers)
    assert status == 403 and json.loads(raw) == {'error': 'cross-site request refused'}
    assert AccountRepo(httpd.db).get('three@example.com') is None
    status, _, raw = call(httpd, API + '/two@example.com', 'DELETE', None, {'Cookie': name})
    assert status == 403 and json.loads(raw) == {'error': 'cross-site request refused'}
    assert AccountRepo(httpd.db).get('two@example.com')['server_admin'] == 1


def test_server_admin_changes_are_audited(web):
    httpd, cookie = web
    _ask(httpd, API, 'POST', {'login': 'two@example.com'}, cookie)
    _ask(httpd, API + '/two@example.com', 'DELETE', cookie=cookie)
    events = _events(httpd)
    assert any(e['action'] == 'account.create' and e['target'] == 'two@example.com'
               and e['actor'] == OWNER and e['detail'] == {'pending': True} for e in events)
    assert any(e['action'] == 'account.server_admin' and e['target'] == 'two@example.com'
               and e['actor'] == OWNER and e['detail'] == {'enabled': True, 'source': 'web'}
               for e in events)
    assert any(e['action'] == 'account.server_admin' and e['target'] == 'two@example.com'
               and e['detail'] == {'enabled': False, 'source': 'web'} for e in events)


# --- workspace for a new owner email ---------------------------------------

def _create_ws(httpd, cookie, slug, owner, name='Lab'):
    return _ask(httpd, '/api/admin/workspaces', 'POST',
                {'slug': slug, 'name': name, 'owner': owner}, cookie)


def test_workspace_for_new_owner_email(web):
    httpd, cookie = web
    status, body = _create_ws(httpd, cookie, 'newlab', 'Founder@Example.com')
    assert status == 201 and body['owner_created'] is True
    assert body['workspace']['slug'] == 'newlab' and body['workspace']['members'] == 1
    account = AccountRepo(httpd.db).get('founder@example.com')
    assert account['created_by'] == OWNER and account['firebase_uid'] is None
    assert account['server_admin'] == 0
    workspace = WorkspaceRepo(httpd.db).get('newlab')
    assert MembershipRepo(httpd.db, workspace['id']).get(account['id'])['role'] == 'owner'
    events = _events(httpd)
    names = [e['action'] for e in reversed(events)]
    assert names.index('account.create') < names.index('workspace.create')
    assert any(e['action'] == 'account.create' and e['target'] == 'founder@example.com'
               and e['detail'] == {'pending': True} for e in events)
    assert any(e['action'] == 'workspace.create' and e['detail']['owner'] == 'founder@example.com'
               for e in events)

    # their first verified sign-in claims the account and lands in the workspace
    founder = _sign_in(httpd, 'founder@example.com', 'uid-founder')
    assert AccountRepo(httpd.db).get('founder@example.com')['id'] == account['id']
    status, body = _ask(httpd, '/api/account/workspaces', cookie=founder)
    assert status == 200 and [w['slug'] for w in body['workspaces']] == ['newlab']


def test_workspace_for_existing_owner_reports_not_created(web):
    httpd, cookie = web
    _member_account(httpd, 'ada@example.com', uid='uid-ada')
    status, body = _create_ws(httpd, cookie, 'adalab', 'ada@example.com')
    assert status == 201 and body['owner_created'] is False
    assert not any(e['action'] == 'account.create' and e['target'] == 'ada@example.com'
                   for e in _events(httpd))


def test_workspace_failure_leaves_no_account_behind(web):
    httpd, cookie = web
    assert _create_ws(httpd, cookie, 'taken', OWNER)[0] == 201
    before = {r['login'] for r in AccountRepo(httpd.db).list()}
    status, body = _create_ws(httpd, cookie, 'taken', 'ghost@example.com')
    assert status == 400 and body == {'error': 'workspace slug already exists'}
    status, body = _create_ws(httpd, cookie, 'fresh', 'ghost@example.com', name=' ')
    assert status == 400 and body == {'error': 'invalid workspace name'}
    status, _ = _create_ws(httpd, cookie, 'api', 'ghost@example.com')
    assert status == 400
    assert {r['login'] for r in AccountRepo(httpd.db).list()} == before
    assert WorkspaceRepo(httpd.db).get('fresh') is None


def test_workspace_non_email_unknown_owner_still_400(web):
    httpd, cookie = web
    status, body = _create_ws(httpd, cookie, 'solo', 'nobody')
    assert status == 400 and body == {'error': 'workspace needs an existing owner account'}
    assert AccountRepo(httpd.db).get('nobody') is None


def test_workspace_new_owner_email_refused_without_firebase(tmp_path):
    from test_server_ingest import running
    from test_server_workspace_routes import _password_cookie

    with running(tmp_path / 'data') as httpd:
        httpd.users.add('boss', 'admin')
        AccountRepo(httpd.db).set_server_admin('boss', True)
        cookie = _password_cookie(httpd, 'boss')
        status, body = _create_ws(httpd, cookie, 'nolab', 'ghost@example.com')
        assert status == 400 and body == {'error': 'workspace needs an existing owner account'}
        assert AccountRepo(httpd.db).get('ghost@example.com') is None


def test_add_unknown_email_is_404_without_firebase(tmp_path):
    from test_server_ingest import running
    from test_server_workspace_routes import _password_cookie

    with running(tmp_path / 'data') as httpd:
        httpd.users.add('boss', 'admin')
        AccountRepo(httpd.db).set_server_admin('boss', True)
        cookie = _password_cookie(httpd, 'boss')
        status, body = _ask(httpd, API, 'POST', {'login': 'ghost@example.com'}, cookie)
        assert status == 404 and body == {'error': 'unknown account'}
        assert AccountRepo(httpd.db).get('ghost@example.com') is None
        assert not any(e['action'] == 'account.create' for e in _events(httpd))


def test_workspace_store_failure_removes_the_new_owner_account(web, monkeypatch):
    httpd, cookie = web
    import agenticml.server.workspaces as workspaces

    def boom(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(workspaces, 'prepare_store', boom)
    before = {r['login'] for r in AccountRepo(httpd.db).list()}
    status, body = _create_ws(httpd, cookie, 'broken', 'ghost@example.com')
    assert status == 503 and body == {'error': 'could not create workspace'}
    assert WorkspaceRepo(httpd.db).get('broken') is None
    assert AccountRepo(httpd.db).get('ghost@example.com') is None
    assert {r['login'] for r in AccountRepo(httpd.db).list()} == before
    # an existing owner is never removed by the cleanup
    _member_account(httpd, 'ada@example.com', uid='uid-ada')
    status, _ = _create_ws(httpd, cookie, 'broken2', 'ada@example.com')
    assert status == 503
    assert AccountRepo(httpd.db).get('ada@example.com') is not None

"""Package 24.10: server admins archive and restore workspaces on /admin."""
import json

import pytest

from agenticml.server.db.repos import AuditRepo
from agenticml.server.db.workspaces import AccountRepo, MembershipRepo, WorkspaceRepo
from agenticml.server.keys import KeyStore
from agenticml.server.workspaces import store_root
from test_server_admin_emails import _admins, _signin
from test_server_admins_web import OWNER, _ask, _create_ws, _no_env, _sign_in, web  # noqa: F401
from test_server_db import url
from test_server_firebase import _member_account, firebase_running
from test_server_ingest import put_meta
from test_server_signup import _plans, signup_running
from test_server_web import call, csrf

API = '/api/admin/workspaces'


def _post(httpd, cookie, slug, action, body=None):
    return _ask(httpd, f'{API}/{slug}/{action}', 'POST', {} if body is None else body, cookie)


def _events(httpd, action):
    return [e for e in AuditRepo(httpd.db, None).tail(200) if e['action'] == action]


def _make(httpd, cookie, slug='lab', owner=OWNER):
    status, _ = _create_ws(httpd, cookie, slug, owner)
    assert status == 201
    return WorkspaceRepo(httpd.db).get(slug)


def test_archive_suspends_keeps_data_and_is_audited(web):
    httpd, cookie = web
    row = _make(httpd, cookie)
    keys = KeyStore(httpd.data_dir / 'server', db=httpd.db, workspace_id=row['id'])
    _, key = keys.create('laptop', user=OWNER)
    assert put_meta(httpd, 'lab-run', key=key)[0] == 200

    status, body = _post(httpd, cookie, 'lab', 'archive')
    assert status == 200
    workspace = body['workspace']
    assert workspace['slug'] == 'lab' and workspace['status'] == 'suspended'
    assert workspace['archived_at']
    stored = WorkspaceRepo(httpd.db).get('lab')
    assert stored['status'] == 'suspended' and stored['archived_at'] == workspace['archived_at']
    # data, keys and memberships are kept; sync is rejected like for a suspended workspace
    assert store_root(httpd.data_dir, row['id']).parent.is_dir()
    account = AccountRepo(httpd.db).get(OWNER)
    assert MembershipRepo(httpd.db, row['id']).get(account['id']) is not None
    assert put_meta(httpd, 'lab-run-2', key=key)[0] == 403
    (event,) = _events(httpd, 'workspace.archive')
    assert event['actor'] == OWNER and event['target'] == row['id'] and event['detail'] == {'slug': 'lab'}
    # the admin list reports it
    status, listing = _ask(httpd, API, cookie=cookie)
    by_slug = {w['slug']: w for w in listing['workspaces']}
    assert by_slug['lab']['archived_at'] == workspace['archived_at']
    assert by_slug['default']['archived_at'] is None


def test_restore_clears_the_mark_and_stays_suspended(web):
    httpd, cookie = web
    row = _make(httpd, cookie)
    keys = KeyStore(httpd.data_dir / 'server', db=httpd.db, workspace_id=row['id'])
    _, key = keys.create('laptop', user=OWNER)
    assert _post(httpd, cookie, 'lab', 'archive')[0] == 200
    status, body = _post(httpd, cookie, 'lab', 'restore')
    assert status == 200
    assert body['workspace']['archived_at'] is None and body['workspace']['status'] == 'suspended'
    stored = WorkspaceRepo(httpd.db).get('lab')
    assert stored['archived_at'] is None and stored['status'] == 'suspended'
    assert put_meta(httpd, 'lab-run', key=key)[0] == 403
    (event,) = _events(httpd, 'workspace.restore')
    assert event['target'] == row['id'] and event['detail'] == {'slug': 'lab'}
    # Resume now works
    status, body = _ask(httpd, f'{API}/lab', 'PATCH', {'status': 'active'}, cookie)
    assert status == 200 and body['workspace']['status'] == 'active'
    assert put_meta(httpd, 'lab-run', key=key)[0] == 200


def test_refusals(web):
    httpd, cookie = web
    _make(httpd, cookie)
    status, body = _post(httpd, cookie, 'default', 'archive')
    assert status == 400 and 'default workspace' in body['error']
    assert WorkspaceRepo(httpd.db).get('default')['archived_at'] is None
    assert _post(httpd, cookie, 'ghost', 'archive') == (404, {'error': 'unknown workspace'})
    assert _post(httpd, cookie, 'ghost', 'restore') == (404, {'error': 'unknown workspace'})
    # restore of a workspace that is not archived
    assert _post(httpd, cookie, 'lab', 'restore') == (409, {'error': 'workspace is not archived'})
    assert _post(httpd, cookie, 'lab', 'archive')[0] == 200
    first = WorkspaceRepo(httpd.db).get('lab')['archived_at']
    assert _post(httpd, cookie, 'lab', 'archive') == (409, {'error': 'workspace is already archived'})
    assert WorkspaceRepo(httpd.db).get('lab')['archived_at'] == first
    # Resume of an archived workspace
    status, body = _ask(httpd, f'{API}/lab', 'PATCH', {'status': 'active'}, cookie)
    assert status == 409 and body == {'error': 'restore the workspace first'}
    assert WorkspaceRepo(httpd.db).get('lab')['status'] == 'suspended'
    # other edits of an archived workspace still work
    assert _ask(httpd, f'{API}/lab', 'PATCH', {'name': 'Old lab'}, cookie)[0] == 200
    assert len(_events(httpd, 'workspace.archive')) == 1


def test_non_admin_and_anonymous_see_unknown_route_answers(web):
    httpd, cookie = web
    _make(httpd, cookie)
    _member_account(httpd, 'bob@example.com', uid='uid-bob')
    bob = _sign_in(httpd, 'bob@example.com', 'uid-bob')
    for action in ('archive', 'restore'):
        path = f'{API}/lab/{action}'
        got = _ask(httpd, path, 'POST', {}, bob)
        assert got == _ask(httpd, '/api/no-such', 'POST', {}, bob) == (404, {'error': 'not found'})
        anon = call(httpd, path, 'POST', b'{}', csrf(httpd))
        anon_unknown = call(httpd, '/api/no-such', 'POST', b'{}', csrf(httpd))
        assert anon[0] == anon_unknown[0] == 401 and anon[2] == anon_unknown[2]
    assert WorkspaceRepo(httpd.db).get('lab')['archived_at'] is None


def test_csrf_is_required(web):
    httpd, cookie = web
    _make(httpd, cookie)
    name = cookie.split(';', 1)[0]
    status, _, out = call(httpd, f'{API}/lab/archive', 'POST', b'{}',
                          {'Cookie': name, 'Content-Type': 'application/json'})
    assert status == 403 and json.loads(out) == {'error': 'cross-site request refused'}
    assert WorkspaceRepo(httpd.db).get('lab')['archived_at'] is None


def test_restore_over_the_free_limit_is_refused(tmp_path):
    with firebase_running(tmp_path / 'data', admin_emails=_admins(), plans=_plans(1)) as httpd:
        status, hdrs, _ = _signin(httpd, sub='uid-owner', email=OWNER)
        assert status == 200
        cookie = hdrs['Set-Cookie']
        _member_account(httpd, 'ada@example.com', uid='uid-ada')
        _make(httpd, cookie, 'lab', 'ada@example.com')
        assert _post(httpd, cookie, 'lab', 'archive')[0] == 200
        # ada gets a new free workspace meanwhile
        WorkspaceRepo(httpd.db).create('ada-new', owner='ada@example.com', plan='free', personal=1)
        status, body = _post(httpd, cookie, 'lab', 'restore')
        assert status == 409 and 'free workspace' in body['error']
        assert WorkspaceRepo(httpd.db).get('lab')['archived_at'] is not None
        # archiving the other one frees the slot
        assert _post(httpd, cookie, 'ada-new', 'archive')[0] == 200
        assert _post(httpd, cookie, 'lab', 'restore')[0] == 200
        # a custom plan is not limited
        assert _post(httpd, cookie, 'ada-new', 'restore')[0] == 409
        status, _ = _ask(httpd, f'{API}/ada-new', 'PATCH', {'plan': 'custom', 'limits': {}}, cookie)
        assert status == 200
        assert _post(httpd, cookie, 'ada-new', 'restore')[0] == 200


def test_restore_with_no_free_workspaces_available(tmp_path):
    with firebase_running(tmp_path / 'data', admin_emails=_admins(), plans=_plans(0)) as httpd:
        status, hdrs, _ = _signin(httpd, sub='uid-owner', email=OWNER)
        cookie = hdrs['Set-Cookie']
        _make(httpd, cookie)
        assert _post(httpd, cookie, 'lab', 'archive')[0] == 200
        status, body = _post(httpd, cookie, 'lab', 'restore')
        assert status == 409 and 'not available' in body['error']


def test_archived_workspace_is_not_counted_as_free(tmp_path):
    from agenticml.server.db import connect
    db = connect(url(tmp_path / 'agenticml.db'))
    try:
        accounts = AccountRepo(db)
        accounts.create('owner')
        repo = WorkspaceRepo(db)
        repo.create('mine', owner='owner')
        account = accounts.get('owner')
        with db.transaction() as tx:
            assert repo.owned_free_count(account['id'], tx) == 1
        archived = repo.archive('mine')
        assert archived['status'] == 'suspended' and archived['archived_at']
        with db.transaction() as tx:
            assert repo.owned_free_count(account['id'], tx) == 0
        repo.restore('mine')
        with db.transaction() as tx:
            assert repo.owned_free_count(account['id'], tx) == 1
    finally:
        db.close()


def _personal(httpd, login):
    account_id = AccountRepo(httpd.db).get(login)['id']
    return [w for w in WorkspaceRepo(httpd.db).list()
            if w['personal'] and w['created_by'] == account_id]


def test_list_admin_with_only_an_archived_workspace_gets_a_personal_one(tmp_path):
    data = tmp_path / 'data'
    with firebase_running(data, admin_emails=_admins()) as httpd:
        status, hdrs, _ = _signin(httpd, sub='uid-owner', email=OWNER)
        assert status == 200
        cookie = hdrs['Set-Cookie']
        _make(httpd, cookie)
        assert AccountRepo(httpd.db).workspaces(OWNER) != []
    with signup_running(data, admin_emails=_admins()) as httpd:
        # still has an (unarchived) workspace: no personal one
        assert _signin(httpd, sub='uid-owner', email=OWNER)[0] == 200
        assert _personal(httpd, OWNER) == []
    with firebase_running(data, admin_emails=_admins()) as httpd:
        status, hdrs, _ = _signin(httpd, sub='uid-owner', email=OWNER)
        assert _post(httpd, hdrs['Set-Cookie'], 'lab', 'archive')[0] == 200
    with signup_running(data, admin_emails=_admins()) as httpd:
        assert _signin(httpd, sub='uid-owner', email=OWNER)[0] == 200
        (personal,) = _personal(httpd, OWNER)
        assert personal['archived_at'] is None and personal['plan'] == 'free'
        assert WorkspaceRepo(httpd.db).get('lab')['archived_at'] is not None
        # only once
        assert _signin(httpd, sub='uid-owner', email=OWNER)[0] == 200
        assert len(_personal(httpd, OWNER)) == 1


def test_signup_account_with_an_archived_workspace_gets_no_new_one(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with signup_running(tmp_path / 'data', admin_emails=_admins()) as httpd:
        assert _signin(httpd, sub='uid-ada', email='ada@example.com')[0] == 200
        (personal,) = _personal(httpd, 'ada@example.com')
        WorkspaceRepo(httpd.db).archive(personal['slug'])
        assert _signin(httpd, sub='uid-ada', email='ada@example.com')[0] == 200
        assert len(_personal(httpd, 'ada@example.com')) == 1
        assert AccountRepo(httpd.db).workspaces('ada@example.com') == []


def test_migration_0009_adds_archived_at_to_existing_workspaces(tmp_path):
    import shutil
    from agenticml.server.app import prepare_data_dir
    from agenticml.server.db import connect, migrate

    prepare_data_dir(tmp_path)
    old = tmp_path / 'old-migrations'
    old.mkdir()
    for path in migrate.MIGRATIONS.glob('[0-9][0-9][0-9][0-9]_*'):
        if int(path.name[:4]) <= 8 and path.suffix in ('.sql', '.py'):
            shutil.copyfile(path, old / path.name)
    target = url(tmp_path / 'server' / 'agenticml.db')
    real = migrate.MIGRATIONS
    migrate.MIGRATIONS = old
    try:
        db = connect(target)
    finally:
        migrate.MIGRATIONS = real
    with db.transaction(write=True) as tx:
        assert tx.fetchone('SELECT MAX(version) FROM schema_migrations')[0] == 8
        tx.execute('INSERT INTO accounts (id, login, display_name, created_at, created_by) '
                   'VALUES (?, ?, ?, ?, ?)', ('a' * 32, 'old', 'Old', '2026-01-01T00:00:00Z', 'cli'))
    WorkspaceRepo(db).create('older', owner='old')
    db.close()
    db = connect(target)
    try:
        with db.transaction() as tx:
            assert tx.fetchone('SELECT MAX(version) FROM schema_migrations')[0] >= 9
        rows = WorkspaceRepo(db).list()
        assert rows and all(r['archived_at'] is None for r in rows)
        assert WorkspaceRepo(db).archive('older')['archived_at']
    finally:
        db.close()

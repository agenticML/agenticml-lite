"""30.2: an account with no membership is re-attached only by an operator or the workspace it left."""
import json

import pytest

from agenticml import cli
from agenticml.server.db.workspaces import AccountRepo, MembershipRepo, WorkspaceRepo
from agenticml.server.keys import ClientError
from agenticml.server.users import DETACHED_ACCOUNT, UserStore
from agenticml.server.workspaces import delete_workspace
from test_server_keys_web import login as key_login
from test_server_signin import PASSWORD
from test_server_userspage import api
from test_server_web import call, csrf
from test_server_workspaces import two  # noqa: F401  (fixture)


@pytest.fixture
def world(two):  # noqa: F811
    server, a, b, keys_a, keys_b, ka, kb = two
    directory = server.data_dir / 'server'
    server.a, server.b = a, b
    server.users_a = UserStore(directory, db=server.db, workspace_id=a['id'])
    server.users_b = UserStore(directory, db=server.db, workspace_id=b['id'])
    _, token_a = keys_a.create('web', 'admin', user='alice')
    _, token_b = keys_b.create('web', 'admin', user='bob')
    server.cookie_a = key_login(server, token_a)
    server.cookie_b = key_login(server, token_b)
    return server


def detach_carol(server):
    """carol (with a password) joins alpha, then is removed: she belongs to no workspace."""
    server.users_a.add('carol', 'member')
    server.users_a.set_password('carol', PASSWORD)
    server.users_a.remove('carol')
    return AccountRepo(server.db).get('carol')


def memberships(server, account):
    with server.db.transaction() as tx:
        tx.cross_workspace('test')
        return tx.fetchone('SELECT COUNT(*) FROM memberships WHERE account_id=?', (account['id'],))[0]


def marker(server, account):
    with server.db.transaction() as tx:
        row = tx.fetchone('SELECT value FROM server_settings WHERE name=?', ('detached-from:' + account['id'],))
    return row[0] if row else None


def test_removed_member_cannot_be_readded_by_another_workspace(world):
    server = world
    before = detach_carol(server)
    assert marker(server, before) == server.a['id']
    with pytest.raises(ClientError, match='not in any workspace'):
        server.users_b.add('carol', 'member')
    status, _, body = api(server, '/api/w/beta/members', 'POST', {'name': 'carol', 'role': 'member'},
                          cookie=server.cookie_b)
    assert status == 403 and body == {'error': DETACHED_ACCOUNT}
    status, _, body = api(server, '/api/w/beta/invites', 'POST', {'login': 'carol', 'role': 'member'},
                          cookie=server.cookie_b)
    assert status == 403 and body == {'error': DETACHED_ACCOUNT}
    after = AccountRepo(server.db).get('carol')
    assert (after['password'], after['generation'], after['identity']) == (
        before['password'], before['generation'], before['identity'])
    assert memberships(server, after) == 0 and marker(server, after) == server.a['id']
    # The refusal comes before the people limit and before any change.
    with pytest.raises(ClientError, match='not in any workspace'):
        server.users_b.add('carol', 'member', plans=None)


def test_readding_by_the_workspace_it_left_is_allowed(world):
    server = world
    before = detach_carol(server)
    status, _, body = api(server, '/api/w/alpha/members', 'POST', {'name': 'carol', 'role': 'member'},
                          cookie=server.cookie_a)
    assert status == 201 and body['invite_url'] is None
    after = AccountRepo(server.db).get('carol')
    assert after['password'] == before['password'] and after['generation'] == before['generation']
    assert memberships(server, after) == 1 and marker(server, after) is None
    assert server.users_a.verify_password('carol', PASSWORD) is not None
    # Removed again: the marker follows the workspace she left last.
    server.users_a.remove('carol')
    assert marker(server, after) == server.a['id']
    status, _, body = api(server, '/api/w/alpha/invites', 'POST', {'login': 'carol', 'role': 'viewer'},
                          cookie=server.cookie_a)
    assert status == 201 and body['added'] is True and body['invite_url'] is None
    assert marker(server, after) is None


def test_marker_follows_the_last_workspace_left(world):
    server = world
    server.users_a.add('carol', 'member')
    server.users_b.add('carol', 'member')
    carol = AccountRepo(server.db).get('carol')
    server.users_a.remove('carol')
    assert marker(server, carol) is None  # still in beta
    server.users_b.remove('carol')
    assert marker(server, carol) == server.b['id']
    with pytest.raises(ClientError, match='not in any workspace'):
        server.users_a.add('carol', 'member')
    server.users_b.add('carol', 'member')
    assert marker(server, carol) is None


def test_operator_can_readd_detached_account(world, capsys):
    server = world
    before = detach_carol(server)
    server.users_b.add('carol', 'member', operator=True)
    assert AccountRepo(server.db).get('carol')['password'] == before['password']
    assert marker(server, before) is None
    server.users_b.remove('carol')
    assert cli.main(['server', 'users', 'add', 'carol', '--workspace', 'beta', '--data', str(server.data_dir)]) == 0
    capsys.readouterr()
    assert MembershipRepo(server.db, server.b['id']).get(before['id']) is not None
    assert AccountRepo(server.db).get('carol')['password'] == before['password']


def test_account_that_never_had_a_membership_needs_an_operator(world):
    server = world
    AccountRepo(server.db).create('ghost')
    with pytest.raises(ClientError, match='not in any workspace'):
        server.users_b.add('ghost', 'member')
    status, _, body = api(server, '/api/w/beta/invites', 'POST', {'login': 'ghost', 'role': 'member'},
                          cookie=server.cookie_b)
    assert status == 403 and body == {'error': DETACHED_ACCOUNT}
    assert server.users_b.add('ghost', 'member', operator=True).name == 'ghost'


def test_detached_server_admin_cannot_be_claimed_by_a_workspace_admin(world, capsys):
    server = world
    assert cli.main(['server', 'accounts', 'add', 'boss@example.com', '--server-admin',
                     '--data', str(server.data_dir)]) == 0
    capsys.readouterr()
    status, _, body = api(server, '/api/w/beta/invites', 'POST', {'login': 'boss@example.com', 'role': 'member'},
                          cookie=server.cookie_b)
    assert status == 403 and body == {'error': DETACHED_ACCOUNT}
    assert cli.main(['server', 'accounts', 'add', 'bossy', '--server-admin', '--data', str(server.data_dir)]) == 0
    capsys.readouterr()
    status, _, body = api(server, '/api/w/beta/members', 'POST', {'name': 'bossy', 'role': 'member'},
                          cookie=server.cookie_b)
    assert status == 403 and body == {'error': DETACHED_ACCOUNT}
    boss = AccountRepo(server.db).get('boss@example.com')
    assert memberships(server, boss) == 0 and boss['password'] is None
    # Even the workspace it left cannot take a server admin back.
    server.users_a.add('carol', 'member')
    server.users_a.remove('carol')
    carol = AccountRepo(server.db).get('carol')
    AccountRepo(server.db).set_server_admin('carol', True)
    with pytest.raises(ClientError, match='not in any workspace'):
        server.users_a.add('carol', 'member')
    assert marker(server, carol) == server.a['id']
    assert server.users_a.add('carol', 'member', operator=True).name == 'carol'


def test_account_with_a_membership_elsewhere_can_still_be_added(world):
    server = world
    server.users_a.add('dave', 'member')
    status, _, body = api(server, '/api/w/beta/members', 'POST', {'name': 'dave', 'role': 'member'},
                          cookie=server.cookie_b)
    assert status == 201 and body['invite_url'] is None
    server.users_a.add('erin', 'member')
    status, _, body = api(server, '/api/w/beta/invites', 'POST', {'login': 'erin', 'role': 'viewer'},
                          cookie=server.cookie_b)
    assert status == 201 and body['added'] is True


def test_marker_of_a_deleted_workspace_leaves_only_the_operator(world):
    server = world
    carol = detach_carol(server)
    # alpha's owner cannot be removed, so move the owner first; then delete alpha.
    delete_workspace(server.db, server.data_dir, 'alpha')
    with pytest.raises(ClientError, match='not in any workspace'):
        server.users_b.add('carol', 'member')
    assert marker(server, carol) == server.a['id']
    assert server.users_b.add('carol', 'member', operator=True).name == 'carol'
    assert marker(server, carol) is None


def test_setup_and_server_up_style_adds_still_attach_a_detached_admin(world):
    server = world
    AccountRepo(server.db).create('admin')
    assert server.users_b.add('admin', 'admin', created_by='setup', operator=True).name == 'admin'


def test_first_run_setup_attaches_an_existing_detached_account(tmp_path, monkeypatch):
    from test_server_ingest import running

    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'data') as server:
        AccountRepo(server.db).create('alice')
        status, _, _ = call(server, '/setup', 'POST', json.dumps({
            'token': server.web.setup_token, 'name': 'alice', 'password': PASSWORD}), csrf(server))
        assert status == 201
        assert server.users.get('alice').role == 'owner'


def post_invite(server, body):
    status, _, raw = call(server, '/invite', 'POST', json.dumps(body), csrf(server))
    return status, json.loads(raw) if raw else None


def test_pending_invite_does_not_reattach_an_account_detached_since(world):
    from agenticml.server.db.workspaces import InviteRepo
    server = world
    token = InviteRepo(server.db, server.b['id']).create('carol@example.com', 'member', 'bob')
    server.users_a.add('carol@example.com', 'member')
    server.users_a.remove('carol@example.com')
    carol = AccountRepo(server.db).get('carol@example.com')
    assert marker(server, carol) == server.a['id']
    with pytest.raises(ClientError, match='not in any workspace'):
        InviteRepo(server.db, server.b['id']).redeem(token)
    status, body = post_invite(server, {'token': token})
    assert status == 403 and body == {'error': DETACHED_ACCOUNT}
    assert memberships(server, carol) == 0
    assert InviteRepo(server.db, server.b['id']).list()  # token not consumed
    # A verified Firebase email proves the person is accepting, so that path still works.
    InviteRepo.redeem_firebase(server.db, token, 'uid-carol', 'carol@example.com', True)
    assert memberships(server, carol) == 1


def test_pending_invite_for_an_attached_account_still_redeems(world):
    from agenticml.server.db.workspaces import InviteRepo
    server = world
    token = InviteRepo(server.db, server.b['id']).create('erin@example.com', 'member', 'bob')
    server.users_a.add('erin@example.com', 'member')
    status, body = post_invite(server, {'token': token})
    assert status == 409  # "you already have an account"; she is now in beta too
    assert memberships(server, AccountRepo(server.db).get('erin@example.com')) == 2

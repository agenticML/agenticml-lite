"""18.1b part 1: route table, workspace dashboard binding, and /api/w/<slug> APIs."""
import json

import pytest

from agenticml.server.db.workspaces import (
    AccountRepo, InviteRepo, MembershipRepo, WorkspaceRepo,
)
from agenticml.server.keys import KeyStore
from agenticml.server.routes import route_table
from agenticml.server.users import UserStore
from agenticml.server.workspaces import prepare_store
from test_server_ingest import put_meta, running
from test_server_keys_users import principal_session
from test_server_signin import PASSWORD, password_login
from test_server_web import authed, call, cookie_parts, csrf, login


def _authed(server, path, method="GET", body=None, cookie=None):
    extra = csrf(server) if method in ("PATCH", "DELETE") else None
    return authed(server, path, method, body, extra=extra, cookie=cookie)


@pytest.fixture
def two(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as server:
        server.users.add("alice", "admin")
        server.users.add("bob", "admin")
        a = WorkspaceRepo(server.db).create("alpha", owner="alice")
        b = WorkspaceRepo(server.db).create("beta", owner="bob")
        for ws in (a, b):
            prepare_store(server.data_dir, ws["id"])
        keys_a = KeyStore(server.data_dir / "server", db=server.db, workspace_id=a["id"])
        keys_b = KeyStore(server.data_dir / "server", db=server.db, workspace_id=b["id"])
        _, ka = keys_a.create("laptop", user="alice")
        _, kb = keys_b.create("laptop", user="bob")
        yield server, a, b, keys_a, keys_b, ka, kb


def _snapshot(status, headers, body):
    skip = {"date", "set-cookie"}
    filtered = {key.lower(): value for key, value in headers.items() if key.lower() not in skip}
    return status, filtered, body


def test_route_table_lists_workspace_and_sync():
    table = route_table()
    assert table is route_table()
    names = []
    scopes = set()
    for route in table:
        assert route.method in ("GET", "POST", "PUT", "PATCH", "DELETE")
        assert route.scope in ("public", "account", "workspace", "server_admin", "sync")
        assert route.handler and route.name and route.pattern
        names.append(route.name)
        scopes.add(route.scope)
    assert len(names) == len(set(names))
    assert scopes >= {"public", "account", "workspace", "sync", "server_admin"}
    assert any(route.name == "runs_list" for route in table)
    assert all(not route.name.startswith("legacy_") for route in table)
    assert any(route.scope == "sync" and route.pattern.startswith("/v2/") for route in table)
    assert any("/api/w/" in route.pattern and "members" in route.pattern for route in table)
    assert any("/api/w/" in route.pattern and "invites" in route.pattern for route in table)
    assert any(route.name == "account_session" for route in table)
    assert any(route.name == "home" and route.pattern == r"/" for route in table)
    assert any(route.scope == "server_admin" and "/api/admin/workspaces" in route.pattern for route in table)
    ws_routes = {route.name: route for route in table
                 if route.scope == "server_admin" and "/api/admin/workspaces" in route.pattern}
    assert {name: route.method for name, route in ws_routes.items()} == {
        "admin_workspaces_list": "GET", "admin_workspaces_create": "POST",
        "admin_workspaces_patch": "PATCH", "admin_workspaces_archive": "POST",
        "admin_workspaces_restore": "POST"}
    assert all(route.handler == "admin_workspaces" for route in ws_routes.values())
    admins = {route.name: route for route in table if "/api/admin/server-admins" in route.pattern}
    assert {name: route.method for name, route in admins.items()} == {
        "admin_server_admins_list": "GET", "admin_server_admins_add": "POST",
        "admin_server_admins_remove": "DELETE"}
    assert all(route.scope == "server_admin" and route.handler == "admin_server_admins"
               for route in admins.values())


def test_second_workspace_routes_and_identical_404s(two):
    server, a, b, keys_a, keys_b, ka, kb = two
    assert put_meta(server, "alpha-run", key=ka)[0] == 200
    assert put_meta(server, "beta-run", key=kb)[0] == 200
    _, alice_key = keys_a.create("alice-ui", "admin", user="alice")
    _, _, hdrs = login(server, alice_key)
    cookie = hdrs["Set-Cookie"]

    status, headers, body, _ = _authed(server, "/w/alpha/", cookie=cookie)
    assert status == 200 and b"<!doctype html>" in body.lower()

    status, _, body, _ = _authed(server, "/api/w/alpha/runs", cookie=cookie)
    assert status == 200
    assert {row["run_id"] for row in json.loads(body)} == {"alpha-run"}
    status, _, body, _ = _authed(server, "/api/w/alpha/runs/alpha-run", cookie=cookie)
    assert status == 200 and json.loads(body)["run_id"] == "alpha-run"

    note = json.dumps({"text": "alpha note", "revision": None}).encode()
    status, _, body, _ = _authed(server, "/api/w/alpha/runs/alpha-run/notes", "PUT", note, cookie=cookie)
    assert status == 200 and json.loads(body)["text"] == "alpha note"
    status, _, body, _ = _authed(server, "/api/w/alpha/notes", cookie=cookie)
    assert status == 200 and json.loads(body)["notes"][0]["text"] == "alpha note"

    status, _, body, _ = _authed(server, "/api/w/alpha/keys", cookie=cookie)
    assert status == 200
    assert "alice-ui" in {row["name"] for row in json.loads(body)["keys"]}
    status, _, body, _ = _authed(
        server, "/api/w/alpha/keys", "POST",
        json.dumps({"name": "extra", "scope": "sync"}).encode(), cookie=cookie)
    assert status == 201 and json.loads(body)["record"]["name"] == "extra"

    status, _, body, _ = _authed(server, "/api/w/alpha/members", cookie=cookie)
    assert status == 200
    by_name = {row["name"]: row for row in json.loads(body)["users"]}
    assert by_name["alice"]["role"] == "owner"

    status, _, body, _ = _authed(
        server, "/api/w/alpha/invites", "POST",
        json.dumps({"login": "cara", "role": "member"}).encode(), cookie=cookie)
    assert status == 201 and json.loads(body)["added"] is False
    assert "/invite?token=" in json.loads(body)["invite_url"]
    status, _, body, _ = _authed(server, "/api/w/alpha/invites", cookie=cookie)
    assert status == 200 and json.loads(body)["invites"][0]["login"] == "cara"
    status, _, body, _ = _authed(server, "/api/w/alpha/invites/cara", "DELETE", cookie=cookie)
    assert status == 200 and json.loads(body) == {"revoked": "cara"}
    assert _authed(server, "/api/w/alpha/invites", cookie=cookie)[2] == b'{"invites":[]}'

    status, _, body, _ = _authed(server, "/api/w/alpha/audit", cookie=cookie)
    assert status == 200 and isinstance(json.loads(body)["events"], list)

    unknown_slug = _authed(server, "/api/w/no-such-ws/runs", cookie=cookie)
    unknown_route = _authed(server, "/api/w/alpha/does-not-exist", cookie=cookie)
    non_member = _authed(server, "/api/w/beta/runs", cookie=cookie)
    assert unknown_slug[0] == 404
    assert json.loads(unknown_slug[2]) == {"error": "not found"}
    assert _snapshot(*unknown_slug[:3]) == _snapshot(*unknown_route[:3]) == _snapshot(*non_member[:3])

    page_unknown = _authed(server, "/w/missing-ws/", cookie=cookie)
    page_missing = _authed(server, "/w/alpha/nope", cookie=cookie)
    assert _snapshot(*page_unknown[:3]) == _snapshot(*page_missing[:3])
    assert json.loads(page_unknown[2]) == {"error": "not found"}

    WorkspaceRepo(server.db).update("alpha", status="suspended")
    suspended = _authed(server, "/api/w/alpha/runs", cookie=cookie)
    assert _snapshot(*suspended[:3]) == _snapshot(*unknown_slug[:3])


def test_viewer_write_is_403_and_members_show_owner(two):
    server, a, b, keys_a, _, ka, _ = two
    assert put_meta(server, "alpha-run", key=ka)[0] == 200
    cookie = principal_session(server, "view", "viewer")
    UserStore(server.data_dir / "server", db=server.db, workspace_id=a["id"]).add("view", "viewer")
    status, _, body, _ = _authed(server, "/api/w/alpha/runs", cookie=cookie)
    assert status == 200
    note = json.dumps({"text": "nope", "revision": None}).encode()
    status, _, body, _ = _authed(
        server, "/api/w/alpha/runs/alpha-run/notes", "PUT", note, cookie=cookie)
    assert status == 403 and json.loads(body) == {"error": "read-only"}
    assert _authed(server, "/api/w/alpha/keys", cookie=cookie)[0] == 403
    assert _authed(server, "/api/w/alpha/members", cookie=cookie)[0] == 403
    assert _authed(server, "/api/w/alpha/invites", cookie=cookie)[0] == 403
    assert _authed(server, "/api/w/alpha/audit", cookie=cookie)[0] == 403

    _, alice_default = server.keys.create("alice-default", "admin", user="alice")
    _, _, hdrs = login(server, alice_default)
    admin_cookie = hdrs["Set-Cookie"]
    status, _, body, _ = _authed(server, "/api/w/default/members", cookie=admin_cookie)
    assert status == 200
    by_name = {row["name"]: row for row in json.loads(body)["users"]}
    assert by_name["alice"]["role"] == "owner"


def test_key_login_is_bound_to_its_workspace(two):
    server, a, _, keys_a, _, ka, _ = two
    assert put_meta(server, "alpha-run", key=ka)[0] == 200
    _, alice_default = server.keys.create("alice-default", "admin", user="alice")
    _, alice_alpha = keys_a.create("alice-ui", "admin", user="alice")

    _, _, default_hdrs = login(server, alice_default)
    default_cookie = default_hdrs["Set-Cookie"]
    assert _authed(server, "/api/w/default/runs", cookie=default_cookie)[0] == 200
    denied = _authed(server, "/api/w/alpha/runs", cookie=default_cookie)
    unknown = _authed(server, "/api/w/alpha/does-not-exist", cookie=default_cookie)
    assert denied[0] == 404 and json.loads(denied[2]) == {"error": "not found"}
    assert _snapshot(*denied[:3]) == _snapshot(*unknown[:3])

    _, _, alpha_hdrs = login(server, alice_alpha)
    alpha_cookie = alpha_hdrs["Set-Cookie"]
    assert _authed(server, "/api/w/alpha/runs", cookie=alpha_cookie)[0] == 200
    legacy = _authed(server, "/api/w/default/runs", cookie=alpha_cookie)
    missing = _authed(server, "/api/w/default/does-not-exist", cookie=alpha_cookie)
    assert legacy[0] == 404 and json.loads(legacy[2]) == {"error": "not found"}
    assert _snapshot(*legacy[:3]) == _snapshot(*missing[:3])


def test_owner_not_removed_or_demoted_on_members_api(two):
    server, a, _, keys_a, _, _, _ = two
    UserStore(server.data_dir / "server", db=server.db, workspace_id=a["id"]).add("bob", "admin")
    _, bob_key = keys_a.create("bob-ui", "admin", user="bob")
    _, _, hdrs = login(server, bob_key)
    cookie = hdrs["Set-Cookie"]
    status, _, body, _ = _authed(server, "/api/w/alpha/members/alice", "DELETE", cookie=cookie)
    assert status == 409 and json.loads(body) == {"error": "last admin"}
    status, _, body, _ = _authed(
        server, "/api/w/alpha/members/alice", "PATCH",
        json.dumps({"role": "member"}).encode(), cookie=cookie)
    assert status == 409 and json.loads(body) == {"error": "last admin"}
    status, _, body, _ = _authed(server, "/api/w/alpha/members", cookie=cookie)
    by_name = {row["name"]: row for row in json.loads(body)["users"]}
    assert by_name["alice"]["role"] == "owner"


def _session_cookie(cookie, **extra):
    name, token, _ = cookie_parts(cookie)
    parts = [f"{name}={token}"]
    parts.extend(f"{key}={value}" for key, value in extra.items())
    return "; ".join(parts)


def _password_cookie(server, login_name):
    server.users.set_password(login_name, PASSWORD, operator=True)
    status, _, hdrs = password_login(server, login_name, PASSWORD)
    assert status == 200
    return hdrs["Set-Cookie"]


def test_account_session_shape_and_password(two):
    server, a, b, _, _, _, _ = two
    cookie = _password_cookie(server, "alice")
    status, _, body, _ = _authed(server, "/api/account/session", cookie=cookie)
    assert status == 200
    payload = json.loads(body)
    assert payload["user"] == "alice" and payload["via"] == "password"
    assert payload["server_admin"] is True
    by_slug = {row["slug"]: row for row in payload["workspaces"]}
    assert set(by_slug) == {"default", "alpha"}
    assert by_slug["default"]["role"] == "owner" and by_slug["alpha"]["role"] == "owner"
    assert "name" in by_slug["alpha"]
    listed = _authed(server, "/api/account/workspaces", cookie=cookie)
    assert listed[0] == 200 and json.loads(listed[2]) == {"workspaces": payload["workspaces"]}

    body = json.dumps({"current": PASSWORD, "new": "new-password-1"}).encode()
    status, hdrs, raw, _ = _authed(
        server, "/api/account/password", "POST", body, cookie=cookie)
    assert status == 200 and json.loads(raw)["ok"] is True
    fresh = hdrs["Set-Cookie"]
    assert _authed(server, "/api/account/session", cookie=cookie)[0] == 401
    assert _authed(server, "/api/account/session", cookie=fresh)[0] == 200
    status, _, hdrs = password_login(server, "alice", "new-password-1")
    assert status == 200
    status, _, hdrs = password_login(server, "alice", PASSWORD)
    assert status == 401


def test_key_login_cannot_set_password_or_open_other_workspace(two):
    server, a, b, keys_a, _, _, _ = two
    users_a = UserStore(server.data_dir / "server", db=server.db, workspace_id=a["id"])
    users_b = UserStore(server.data_dir / "server", db=server.db, workspace_id=b["id"])
    users_b.add("victim", "member")
    users_a.add("victim", "admin")
    assert AccountRepo(server.db).get("victim")["password"] is None
    _, victim_key = keys_a.create("victim-ui", "admin", user="victim")
    status, _, hdrs = login(server, victim_key)
    assert status == 200
    cookie = hdrs["Set-Cookie"]
    before = AccountRepo(server.db).get("victim")
    new_password = json.dumps({"new": "new-password-1"}).encode()
    status, hdrs, raw, _ = _authed(
        server, "/api/account/password", "POST", new_password, cookie=cookie)
    assert status == 403
    assert json.loads(raw) == {"error": "this session cannot set a password"}
    assert "Set-Cookie" not in hdrs
    after = AccountRepo(server.db).get("victim")
    assert after["password"] == before["password"] and after["generation"] == before["generation"]
    session = json.loads(_authed(server, "/api/account/session", cookie=cookie)[2])
    assert session["via"] == "key" and session["password_set"] is False
    assert _authed(server, "/api/w/beta/runs", cookie=cookie)[0] == 404
    assert _authed(server, "/api/w/alpha/runs", cookie=cookie)[0] == 200

    name, token, _ = cookie_parts(cookie)
    extra = csrf(server, Cookie="%s=%s" % (name, token))
    status, hdrs, body = call(
        server, "/login", "POST", json.dumps({"key": victim_key}).encode(), extra)
    assert status == 200
    relog = hdrs["Set-Cookie"]
    assert json.loads(_authed(server, "/api/account/session", cookie=relog)[2])["via"] == "key"
    assert _authed(server, "/api/w/beta/runs", cookie=relog)[0] == 404
    assert call(server, "/setup", "POST", json.dumps({
        "token": "x", "name": "nope", "password": PASSWORD,
    }).encode(), extra)[0] == 404
    status, hdrs, _ = call(
        server, "/invite", "POST",
        json.dumps({"token": "x", "password": PASSWORD}).encode(), extra)
    assert status == 404
    assert "Set-Cookie" not in hdrs

    users_a.set_password("victim", PASSWORD, operator=True)
    status, _, hdrs = password_login(server, "victim", PASSWORD)
    assert status == 200
    pw_cookie = hdrs["Set-Cookie"]
    status, hdrs, raw, _ = _authed(
        server, "/api/account/password", "POST",
        json.dumps({"current": PASSWORD, "new": "new-password-1"}).encode(),
        cookie=pw_cookie)
    assert status == 200 and json.loads(raw)["ok"] is True
    unbound = hdrs["Set-Cookie"]
    assert _authed(server, "/api/w/beta/runs", cookie=unbound)[0] == 200
    assert password_login(server, "victim", "new-password-1")[0] == 200

    status, _, hdrs = login(server, victim_key)
    assert status == 200
    key_cookie = hdrs["Set-Cookie"]
    change = json.dumps({"current": "new-password-1", "new": "other-password1"}).encode()
    status, hdrs, raw, _ = _authed(
        server, "/api/account/password", "POST", change, cookie=key_cookie)
    assert status == 403
    assert json.loads(raw) == {"error": "this session cannot set a password"}
    assert "Set-Cookie" not in hdrs
    assert password_login(server, "victim", "new-password-1")[0] == 200
    assert password_login(server, "victim", "other-password1")[0] == 401
    assert _authed(server, "/api/w/beta/runs", cookie=key_cookie)[0] == 404


def test_home_redirect_and_workspaces_picker(two):
    server, a, b, _, _, _, _ = two
    cookie = _password_cookie(server, "alice")
    status, hdrs, _, _ = _authed(server, "/", cookie=cookie)
    assert status == 303 and hdrs["Location"] == "/workspaces"

    extra = {"Cookie": _session_cookie(cookie, aml_ws="alpha")}
    status, hdrs, _, _ = authed(server, "/", extra=extra, cookie=cookie)
    assert status == 303 and hdrs["Location"] == "/w/alpha/"
    extra = {"Cookie": _session_cookie(cookie, aml_ws="beta")}
    status, hdrs, _, _ = authed(server, "/", extra=extra, cookie=cookie)
    assert status == 303 and hdrs["Location"] == "/workspaces"
    extra = {"Cookie": _session_cookie(cookie, aml_ws="missing-ws")}
    status, hdrs, body, _ = authed(server, "/", extra=extra, cookie=cookie)
    assert status == 303 and hdrs["Location"] == "/workspaces"
    assert b"beta" not in body and b"missing" not in body.lower()

    status, _, body, _ = _authed(server, "/workspaces", cookie=cookie)
    assert status == 200
    assert b'href="/w/default/"' in body and b'href="/w/alpha/"' in body
    assert b'href="/admin/"' in body
    assert b"<script" not in body
    assert b"app.css" in body

    users_a = UserStore(server.data_dir / "server", db=server.db, workspace_id=a["id"])
    users_a.add("cara", "member")
    users_a.set_password("cara", PASSWORD)
    status, _, hdrs = password_login(server, "cara", PASSWORD)
    assert status == 200
    cara = hdrs["Set-Cookie"]
    MembershipRepo(server.db, a["id"]).remove(AccountRepo(server.db).get("cara")["id"])
    status, hdrs, _, _ = _authed(server, "/", cookie=cara)
    assert status == 303 and hdrs["Location"] == "/workspaces"
    status, _, body, _ = _authed(server, "/workspaces", cookie=cara)
    assert status == 200
    assert b"You are not a member of any workspace; ask an admin to invite you." in body
    assert b'href="/w/' not in body
    assert b'href="/admin/"' not in body

    _, admin_key = server.keys.create("alice-one", "admin", user="alice")
    _, _, hdrs = login(server, admin_key)
    key_cookie = hdrs["Set-Cookie"]
    status, hdrs, _, _ = _authed(server, "/", cookie=key_cookie)
    assert status == 303 and hdrs["Location"] == "/w/default/"


def test_one_workspace_redirects_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as server:
        _, key = server.keys.create("admin", "admin")
        _, _, hdrs = login(server, key)
        cookie = hdrs["Set-Cookie"]
        status, hdrs, _, _ = _authed(server, "/", cookie=cookie)
        assert status == 303 and hdrs["Location"] == "/w/default/"
        assert _authed(server, "/w/default/", cookie=cookie)[0] == 200


def test_admin_routes_404_for_non_admin_and_work_for_server_admin(two):
    server, a, b, _, _, _, _ = two
    alice = _password_cookie(server, "alice")
    bob = _password_cookie(server, "bob")
    unknown_page = _authed(server, "/no-such-page", cookie=bob)
    unknown_api = _authed(server, "/api/no-such", cookie=bob)
    admin_page = _authed(server, "/admin/", cookie=bob)
    admin_api = _authed(server, "/api/admin/workspaces", cookie=bob)
    admin_audit = _authed(server, "/api/admin/audit", cookie=bob)
    assert unknown_page[0] == 404 and json.loads(unknown_page[2]) == {"error": "not found"}
    assert _snapshot(*admin_page[:3]) == _snapshot(*unknown_page[:3])
    assert _snapshot(*admin_api[:3]) == _snapshot(*unknown_api[:3])
    assert _snapshot(*admin_audit[:3]) == _snapshot(*unknown_api[:3])
    anon_page = call(server, "/admin/")
    anon_unknown = call(server, "/no-such-page")
    assert anon_page[0] == anon_unknown[0] == 303
    assert anon_page[1]["Location"] == anon_unknown[1]["Location"] == "/login"
    anon_api = call(server, "/api/admin/workspaces")
    anon_missing = call(server, "/api/no-such")
    assert _snapshot(*anon_api) == _snapshot(*anon_missing)
    assert json.loads(anon_api[2]) == {"error": "login required"}

    status, _, body, _ = _authed(server, "/admin/", cookie=alice)
    assert status == 200 and b"<!doctype html>" in body.lower()
    status, _, body, _ = _authed(server, "/api/admin/workspaces", cookie=alice)
    assert status == 200
    by_slug = {row["slug"]: row for row in json.loads(body)["workspaces"]}
    assert set(by_slug) >= {"default", "alpha", "beta"}
    assert by_slug["alpha"]["status"] == "active" and by_slug["alpha"]["members"] >= 1
    assert "created_at" in by_slug["alpha"]
    status, _, body, _ = _authed(
        server, "/api/admin/workspaces", "POST",
        json.dumps({"slug": "gamma", "name": "Gamma", "owner": "bob"}).encode(), cookie=alice)
    assert status == 201
    created = json.loads(body)["workspace"]
    assert created["slug"] == "gamma" and created["name"] == "Gamma" and created["status"] == "active"
    status, _, body, _ = _authed(
        server, "/api/admin/workspaces/gamma", "PATCH",
        json.dumps({"name": "Gamma Lab", "status": "suspended"}).encode(), cookie=alice)
    assert status == 200
    updated = json.loads(body)["workspace"]
    assert updated["name"] == "Gamma Lab" and updated["status"] == "suspended"
    status, _, body, _ = _authed(
        server, "/api/admin/workspaces/default", "PATCH",
        json.dumps({"slug": "main"}).encode(), cookie=alice)
    assert status == 400
    status, _, body, _ = _authed(server, "/api/admin/audit", cookie=alice)
    assert status == 200
    actions = {event["action"] for event in json.loads(body)["events"]}
    assert {"workspace.create", "workspace.suspend"} <= actions
    denied = _authed(server, "/w/beta/", cookie=alice)
    missing = _authed(server, "/w/beta/nope", cookie=alice)
    assert denied[0] == 404 and json.loads(denied[2]) == {"error": "not found"}
    assert _snapshot(*denied[:3]) == _snapshot(*missing[:3])
    assert _authed(server, "/api/w/beta/runs", cookie=alice)[0] == 404
    create_as_bob = _authed(
        server, "/api/admin/workspaces", "POST",
        json.dumps({"slug": "delta", "name": "Delta", "owner": "alice"}).encode(), cookie=bob)
    unknown_post = _authed(server, "/api/no-such", "POST", b"{}", cookie=bob)
    assert _snapshot(*create_as_bob[:3]) == _snapshot(*unknown_post[:3])


def test_invite_existing_account_and_workspace_token_flow(two):
    server, a, b, _, _, _, _ = two
    token = InviteRepo(server.db, a["id"]).create("newbie", "member", "alice")
    UserStore(server.data_dir / "server", db=server.db, workspace_id=b["id"]).add("newbie", "viewer")
    UserStore(server.data_dir / "server", db=server.db, workspace_id=b["id"]).set_password(
        "newbie", PASSWORD)
    before = AccountRepo(server.db).get("newbie")
    status, hdrs, page = call(server, "/invite?token=" + token)
    assert status == 200
    assert b"you already have an account" in page.lower()
    assert b'type="password"' not in page.lower()
    assert b"<button" in page.lower()
    assert b"newbie" not in page
    after = AccountRepo(server.db).get("newbie")
    assert after["password"] == before["password"] and after["generation"] == before["generation"]
    assert MembershipRepo(server.db, a["id"]).get(after["id"]) is None
    refused = call(server, "/invite", "POST", json.dumps({"token": token}).encode())
    assert refused[0] == 403
    assert MembershipRepo(server.db, a["id"]).get(after["id"]) is None
    status, hdrs, body = call(
        server, "/invite", "POST", json.dumps({"token": token}).encode(), csrf(server))
    assert status == 409
    assert json.loads(body) == {"error": "you already have an account; sign in with it"}
    assert "Set-Cookie" not in hdrs
    after = AccountRepo(server.db).get("newbie")
    assert MembershipRepo(server.db, a["id"]).get(after["id"])["role"] == "member"

    token = InviteRepo(server.db, a["id"]).create("later", "viewer", "alice")
    UserStore(server.data_dir / "server", db=server.db, workspace_id=b["id"]).add("later", "member")
    UserStore(server.data_dir / "server", db=server.db, workspace_id=b["id"]).set_password(
        "later", PASSWORD)
    before = AccountRepo(server.db).get("later")
    status, hdrs, body = call(
        server, "/invite", "POST",
        json.dumps({"token": token, "password": "unrelated-pass"}).encode(), csrf(server))
    assert status == 409
    assert json.loads(body) == {"error": "you already have an account; sign in with it"}
    assert "Set-Cookie" not in hdrs
    after = AccountRepo(server.db).get("later")
    assert after["password"] == before["password"] and after["generation"] == before["generation"]
    assert MembershipRepo(server.db, a["id"]).get(after["id"])["role"] == "viewer"

    token = InviteRepo(server.db, a["id"]).create("fresh", "member", "alice")
    status, _, page = call(server, "/invite?token=" + token)
    assert status == 200 and b"Choose a password" in page
    status, hdrs, body = call(
        server, "/invite", "POST",
        json.dumps({"token": token, "password": PASSWORD}).encode(), csrf(server))
    payload = json.loads(body)
    assert status == 200 and payload == {"ok": True, "user": "fresh", "role": "member"}
    cookie = hdrs["Set-Cookie"]
    assert _authed(server, "/api/w/alpha/runs", cookie=cookie)[0] == 200
    assert _authed(server, "/api/w/beta/runs", cookie=cookie)[0] == 404


def test_note_run_id_does_not_parse_workspace_slug_as_runs():
    from agenticml.ui.server import _note_run_id
    run_id = "shared-run"
    raw = "/api/w/alpha/runs/%s/notes" % run_id
    assert _note_run_id(raw) is None
    assert _note_run_id(raw, "/api/runs/%s/notes" % run_id) == run_id
    assert _note_run_id("/api/runs/%s/notes" % run_id) == run_id


def test_cannot_suspend_default_workspace(two):
    server, _, _, _, _, _, _ = two
    alice = _password_cookie(server, "alice")
    status, _, body, _ = _authed(
        server, "/api/admin/workspaces/default", "PATCH",
        json.dumps({"status": "suspended"}).encode(), cookie=alice)
    assert status == 400
    assert "cannot suspend the default workspace" in json.loads(body)["error"]
    assert WorkspaceRepo(server.db).get("default")["status"] == "active"


def test_create_workspace_store_oserror_does_not_leave_row(two, monkeypatch):
    server, _, _, _, _, _, _ = two
    alice = _password_cookie(server, "alice")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("agenticml.server.workspaces.prepare_store", boom)
    status, _, body, _ = _authed(
        server, "/api/admin/workspaces", "POST",
        json.dumps({"slug": "omega", "name": "Omega", "owner": "alice"}).encode(),
        cookie=alice)
    assert status == 503
    assert json.loads(body) == {"error": "could not create workspace"}
    assert WorkspaceRepo(server.db).get("omega") is None

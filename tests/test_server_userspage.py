"""Dashboard /api/server/users routes: authz, lifecycle, and audit lines."""
import json

import pytest

from agenticml.server import userspage
from agenticml.server.keys import KeyStoreUnavailable
from agenticml.store import RunWriter
from test_server_keys_users import principal_session
from test_server_keys_web import call, csrf, login, running, server  # noqa: F401 (fixture)


def api(server, path="/api/server/users", method="GET", payload=None, cookie=None, extra=None):
    headers = csrf(server) if method in ("POST", "PATCH", "DELETE") else {}
    headers["Cookie"] = server.cookie if cookie is None else cookie
    headers.update(extra or {})
    status, hdrs, body = call(
        server, path, method, None if payload is None else json.dumps(payload), headers)
    return status, hdrs, json.loads(body) if body else None


def audit_events(server):
    path = server.data_dir / "server" / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


@pytest.mark.parametrize("role", ["member", "viewer"])
@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/api/server/users", None),
    ("POST", "/api/server/users", {"name": "x", "role": "member"}),
    ("PATCH", "/api/server/users/admin", {"role": "member"}),
    ("DELETE", "/api/server/users/admin", None),
    ("POST", "/api/server/users/admin/invite", {}),
])
def test_member_and_viewer_forbidden(server, role, method, path, payload):
    cookie = principal_session(server, role + "-user", role)
    status, _, body = api(server, path, method, payload, cookie=cookie)
    assert status == 403 and body == {"error": "forbidden"}


def test_list_me_keys_and_runs(server):
    server.users.add("bob", role="member")
    server.keys.create("laptop", user="bob")
    writer = RunWriter.create(server.root, run_id="bob-run", meta={"project": "p", "name": "n"})
    writer.finish("finished")
    writer.close()
    (server.root / "runs" / "bob-run" / "remote.json").write_text(
        json.dumps({"owner": "bob"}))
    status, _, body = api(server)
    assert status == 200 and body["me"] == "admin"
    by_name = {row["name"]: row for row in body["users"]}
    assert by_name["admin"]["role"] == "admin"
    assert by_name["admin"]["keys"] >= 1
    assert by_name["bob"]["role"] == "member" and by_name["bob"]["keys"] == 1
    assert by_name["bob"]["runs"] == 1
    assert by_name["bob"]["password_set"] is False
    assert "password" not in by_name["bob"] and "invite" not in by_name["bob"]


def test_run_count_scan_cap(server, monkeypatch):
    monkeypatch.setattr(userspage, "RUN_SCAN_MAX", 2)
    runs = server.root / "runs"
    runs.mkdir(exist_ok=True)
    for name in ("a", "b"):
        (runs / name).mkdir()
        (runs / name / "remote.json").write_text(json.dumps({"owner": "admin"}))
    assert api(server)[2]["users"][0]["runs"] == 2
    (runs / "c").mkdir()
    (runs / "c" / "remote.json").write_text(json.dumps({"owner": "admin"}))
    assert api(server)[2]["users"][0]["runs"] is None


def test_add_user_invite_and_audit(server):
    status, headers, body = api(server, method="POST", payload={"name": "bob", "role": "member"})
    assert status == 201 and headers["Cache-Control"] == "no-store"
    assert body["user"]["name"] == "bob" and body["user"]["role"] == "member"
    assert body["user"]["invite_pending"] is True and body["user"]["password_set"] is False
    assert "/invite?token=" in body["invite_url"] and body["expires_at"]
    token = body["invite_url"].split("token=", 1)[1]
    assert token not in json.dumps(api(server)[2])
    events = audit_events(server)
    assert [event["action"] for event in events[-2:]] == ["user.add", "user.invite"]
    assert events[-2]["target"] == "bob" and events[-2]["actor"] == "admin"
    assert events[-2]["detail"] == {"role": "member", "identity": None}
    assert events[-1]["target"] == "bob" and events[-1]["detail"]["expires_at"] == body["expires_at"]
    assert token not in json.dumps(events)


def test_add_with_identity_and_conflicts(server):
    status, _, body = api(server, method="POST", payload={
        "name": "bob", "role": "viewer", "identity": "bob@example.com",
    })
    assert status == 201 and body["user"]["identity"] == "bob@example.com"
    status, _, body = api(server, method="POST", payload={"name": "bob", "role": "member"})
    assert status == 409 and body == {"error": "user name already exists"}
    status, _, body = api(server, method="POST", payload={
        "name": "cara", "role": "member", "identity": "bob@example.com",
    })
    assert status == 409 and body == {"error": "identity already in use"}
    status, _, body = api(server, method="POST", payload={"name": "env", "role": "member"})
    assert status == 400 and body == {"error": "invalid or reserved user name"}
    status, _, body = api(server, "/api/server/users/..", "PATCH", {"role": "member"})
    assert status == 400 and body == {"error": "invalid or reserved user name"}
    status, _, body = api(server, method="POST", payload={"name": "dave", "role": "owner"})
    assert status == 400 and body == {"error": "invalid role"}


def test_add_refuses_name_that_owns_runs(server):
    server.users.add("ghost", role="member")
    writer = RunWriter.create(server.root, run_id="old", meta={"project": "p", "name": "n"})
    writer.finish("finished")
    writer.close()
    (server.root / "runs" / "old" / "remote.json").write_text(json.dumps({"owner": "ghost"}))
    server.users.remove("ghost")
    status, _, body = api(server, method="POST", payload={"name": "ghost", "role": "member"})
    assert status == 400 and "reclaim-runs" in body["error"]


def test_patch_role_identity_last_admin_and_unknown(server):
    api(server, method="POST", payload={"name": "bob", "role": "member"})
    status, _, body = api(server, "/api/server/users/bob", "PATCH", {"role": "viewer"})
    assert status == 200 and body["user"]["role"] == "viewer" and body["signed_out"] is False
    assert audit_events(server)[-1]["action"] == "user.role"
    assert audit_events(server)[-1]["detail"] == {"from": "member", "to": "viewer"}
    status, _, body = api(server, "/api/server/users/bob", "PATCH", {"identity": "bob@x.com"})
    assert status == 200 and body["user"]["identity"] == "bob@x.com"
    assert audit_events(server)[-1]["action"] == "user.identity"
    status, _, body = api(server, "/api/server/users/bob", "PATCH", {"identity": None})
    assert status == 200 and body["user"]["identity"] is None
    status, _, body = api(server, "/api/server/users/admin", "PATCH", {"role": "member"})
    assert status == 409 and body == {"error": "last admin"}
    status, _, body = api(server, "/api/server/users/missing", "PATCH", {"role": "member"})
    assert status == 404 and body == {"error": "not found"}
    status, _, body = api(server, "/api/server/users/bob", "PATCH", {})
    assert status == 400 and body == {"error": "invalid request"}


def test_patch_own_role_signs_out_when_another_admin_remains(server):
    api(server, method="POST", payload={"name": "bob", "role": "admin"})
    status, headers, body = api(server, "/api/server/users/admin", "PATCH", {"role": "member"})
    assert status == 200 and body["signed_out"] is True and body["user"]["role"] == "member"
    assert "Set-Cookie" in headers and "Max-Age=0" in headers["Set-Cookie"]
    assert api(server)[0] == 401


def test_patch_role_errors_do_not_change_identity(server):
    api(server, method="POST", payload={"name": "bob", "role": "member"})
    status, _, body = api(server, "/api/server/users/admin", "PATCH", {
        "role": "member", "identity": "admin@x.com",
    })
    assert status == 409 and body == {"error": "last admin"}
    admin = server.users.get("admin")
    assert admin.role == "admin" and admin.identity is None
    status, _, body = api(server, "/api/server/users/bob", "PATCH", {
        "role": "owner", "identity": "bob@x.com",
    })
    assert status == 400 and body == {"error": "invalid role"}
    bob = server.users.get("bob")
    assert bob.role == "member" and bob.identity is None


def test_patch_identity_error_does_not_change_role(server):
    api(server, method="POST", payload={"name": "bob", "role": "member"})
    api(server, method="POST", payload={"name": "cara", "role": "member", "identity": "taken@x.com"})
    status, _, body = api(server, "/api/server/users/bob", "PATCH", {
        "role": "admin", "identity": "taken@x.com",
    })
    assert status == 409 and body == {"error": "identity already in use"}
    assert server.users.get("bob").role == "member"
    status, _, body = api(server, "/api/server/users/bob", "PATCH", {
        "role": "admin", "identity": "x" * 300,
    })
    assert status == 400 and body == {"error": "invalid identity"}
    assert server.users.get("bob").role == "member"


def test_patch_own_role_and_identity_signs_out(server):
    api(server, method="POST", payload={"name": "bob", "role": "admin"})
    status, headers, body = api(server, "/api/server/users/admin", "PATCH", {
        "role": "member", "identity": "admin@x.com",
    })
    assert status == 200 and body["signed_out"] is True
    assert body["user"]["role"] == "member" and body["user"]["identity"] == "admin@x.com"
    assert "Set-Cookie" in headers and "Max-Age=0" in headers["Set-Cookie"]
    assert api(server)[0] == 401


def test_delete_self_last_admin_and_keys(server):
    api(server, method="POST", payload={"name": "bob", "role": "member"})
    server.keys.create("laptop", user="bob")
    status, _, body = api(server, "/api/server/users/admin", "DELETE")
    assert status == 409 and body == {"error": "you cannot remove yourself"}
    status, _, body = api(server, "/api/server/users/missing", "DELETE")
    assert status == 404 and body == {"error": "not found"}
    status, _, body = api(server, "/api/server/users/bob", "DELETE")
    assert status == 200 and body == {"removed": "bob", "keys_revoked": 1}
    assert server.users.get("bob") is None
    assert server.keys.for_user("bob") == []
    events = audit_events(server)
    assert events[-2]["action"] == "user.remove" and events[-2]["detail"] == {"keys_revoked": 1}
    assert events[-1]["action"] == "key.revoke" and events[-1]["target"] == "laptop"
    assert events[-1]["detail"] == {"user": "bob", "scope": "sync"}


def test_delete_last_admin_as_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", env_key="e" * 32) as httpd:
        httpd.cookie = login(httpd, "e" * 32)
        httpd.users.add("alice", role="admin")
        status, _, body = api(httpd, "/api/server/users/alice", "DELETE")
        assert status == 409 and body == {"error": "last admin"}


def test_reinvite(server):
    api(server, method="POST", payload={"name": "bob", "role": "member"})
    server.users.set_password("bob", "password1234")
    assert server.users.get("bob").password is not None
    status, _, body = api(server, "/api/server/users/bob/invite", "POST", {})
    assert status == 200 and "/invite?token=" in body["invite_url"] and body["expires_at"]
    bob = server.users.get("bob")
    assert bob.password is None and bob.invite is not None
    assert audit_events(server)[-1]["action"] == "user.invite"
    status, _, body = api(server, "/api/server/users/missing/invite", "POST", {})
    assert status == 404 and body == {"error": "not found"}


def test_reinvite_self_refused(server):
    status, _, body = api(server, "/api/server/users/admin/invite", "POST", {})
    assert status == 409 and body == {
        "error": "you cannot re-invite yourself; change your password from the Account menu"}
    admin = server.users.get("admin")
    assert admin.invite is None


def test_csrf_and_methods(server):
    status, _, body = call(server, "/api/server/users", "POST", b"{}", {"Cookie": server.cookie})
    assert status == 403 and json.loads(body)["error"] == "cross-site request refused"
    status, _, body = api(server, method="PUT", payload={"name": "x"})
    assert status == 405 and body == {"error": "method not allowed"}
    assert api(server, "/api/server/users/admin")[0] == 405
    assert api(server, "/api/server/users/admin/invite")[0] == 405


def test_add_rolls_back_if_invite_fails(server, monkeypatch):
    def boom(name):
        raise KeyStoreUnavailable("private failure")

    monkeypatch.setattr(server.users, "create_invite", boom)
    status, _, body = api(server, method="POST", payload={"name": "bob", "role": "member"})
    assert status == 503 and body == {"error": "key store unavailable"}
    assert server.users.get("bob") is None


@pytest.mark.parametrize("exc", [KeyStoreUnavailable, OSError])
def test_add_rolls_back_if_get_after_invite_fails(server, monkeypatch, exc):
    original = server.users.get

    def boom(name):
        if name == "bob":
            raise exc("private failure")
        return original(name)

    monkeypatch.setattr(server.users, "get", boom)
    status, _, body = api(server, method="POST", payload={"name": "bob", "role": "member"})
    assert status == 503 and body == {"error": "key store unavailable"}
    monkeypatch.setattr(server.users, "get", original)
    assert server.users.get("bob") is None


@pytest.mark.parametrize("exc", [KeyStoreUnavailable, OSError])
def test_reinvite_get_failure_is_503_and_audited(server, monkeypatch, exc):
    server.users.add("bob", role="member")
    server.users.set_password("bob", "password1234")
    original = server.users.get
    before = original("bob")
    events = audit_events(server)

    def boom(name):
        if name == "bob":
            raise exc("private failure")
        return original(name)

    monkeypatch.setattr(server.users, "get", boom)
    status, _, body = api(server, "/api/server/users/bob/invite", "POST", {})
    assert status == 503 and body == {"error": "key store unavailable"}
    bob = original("bob")
    assert bob.invite is not None and bob.password is None
    assert bob.generation == before.generation + 1
    added = audit_events(server)[len(events):]
    assert [(event["action"], event["actor"], event["target"]) for event in added] == [
        ("user.invite", "admin", "bob")]
    assert added[0]["detail"] == {"expires_at": None}


@pytest.mark.parametrize("exc", [KeyStoreUnavailable, OSError])
def test_reinvite_create_failure_is_503_without_audit(server, monkeypatch, exc):
    server.users.add("bob", role="member")
    before = server.users.get("bob")
    events = audit_events(server)

    def boom(name):
        raise exc("private failure")

    monkeypatch.setattr(server.users, "create_invite", boom)
    status, _, body = api(server, "/api/server/users/bob/invite", "POST", {})
    assert status == 503 and body == {"error": "key store unavailable"}
    assert server.users.get("bob") == before
    assert audit_events(server) == events


@pytest.mark.parametrize("exc", [KeyStoreUnavailable, OSError])
def test_patch_get_failure_is_503_before_mutation(server, monkeypatch, exc):
    server.users.add("bob", role="member")
    original = server.users.get
    before = original("bob")
    events = audit_events(server)

    def boom(name):
        if name == "bob":
            raise exc("private failure")
        return original(name)

    monkeypatch.setattr(server.users, "get", boom)
    status, _, body = api(server, "/api/server/users/bob", "PATCH", {
        "role": "viewer", "identity": "bob@example.com",
    })
    assert status == 503 and body == {"error": "key store unavailable"}
    assert original("bob") == before
    assert audit_events(server) == events


@pytest.mark.parametrize("store,attr,method,path,payload", [
    ("users", "list", "GET", "/api/server/users", None),
    ("keys", "list", "GET", "/api/server/users", None),
    ("keys", "list", "DELETE", "/api/server/users/bob", None),
    ("users", "get", "PATCH", "/api/server/users/bob", {"role": "viewer"}),
    ("users", "get", "DELETE", "/api/server/users/bob", None),
    ("users", "get", "POST", "/api/server/users/bob/invite", {}),
    ("users", "list", "PATCH", "/api/server/users/admin", {"role": "member"}),
])
def test_list_get_unavailable_is_503(server, monkeypatch, store, attr, method, path, payload):
    server.users.add("bob", role="member")

    def boom(*args, **kwargs):
        raise KeyStoreUnavailable("private failure")

    monkeypatch.setattr(getattr(server, store), attr, boom)
    status, _, body = api(server, path, method, payload)
    assert status == 503 and body == {"error": "key store unavailable"}


@pytest.mark.parametrize("store,attr,method,path,payload", [
    ("users", "list", "GET", "/api/server/users", None),
    ("keys", "list", "GET", "/api/server/users", None),
    ("keys", "list", "DELETE", "/api/server/users/bob", None),
    ("users", "list", "PATCH", "/api/server/users/admin", {"role": "member"}),
])
def test_list_oserror_is_503(server, monkeypatch, store, attr, method, path, payload):
    server.users.add("bob", role="member")
    cookie = server.cookie
    if store == "keys":
        # Key sessions call keys.list() during auth; a password session does not.
        server.users.set_password("admin", "password1234")
        status, hdrs, _ = call(
            server, "/login", "POST",
            json.dumps({"user": "admin", "password": "password1234"}).encode(),
            csrf(server))
        assert status == 200
        cookie = hdrs["Set-Cookie"].split(";", 1)[0]

    def boom(*args, **kwargs):
        raise OSError("private failure")

    monkeypatch.setattr(getattr(server, store), attr, boom)
    status, _, body = api(server, path, method, payload, cookie=cookie)
    assert status == 503 and body == {"error": "key store unavailable"}


def test_audit_route_admin_and_forbidden(server):
    api(server, method="POST", payload={"name": "bob", "role": "member"})
    status, headers, body = api(server, "/api/server/audit")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert isinstance(body["events"], list) and body["events"][0]["action"] in (
        "user.add", "user.invite")
    cookie = principal_session(server, "reader", "viewer")
    status, _, body = api(server, "/api/server/audit", cookie=cookie)
    assert status == 403 and body == {"error": "forbidden"}
    cookie = principal_session(server, "mem", "member")
    status, _, body = api(server, "/api/server/audit", cookie=cookie)
    assert status == 403 and body == {"error": "forbidden"}

"""Per-user keys and attribution through real dashboard sign-in."""
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace

import pytest

from agenticml import cli, sync
from agenticml.client import Client
from agenticml.server import admin, ingest, keys, keyspage
from agenticml.server.users import Principal
from agenticml.store import layout
from agenticml.store import reader
from agenticml.store.reader import RunReader, read_remote
from agenticml.ui import server as ui_server
from test_server_keys_web import api, call, csrf, login, running, server  # noqa: F401 (fixture)
from test_sync import command, push, state, writer_at


@pytest.mark.parametrize("role", ["viewer", "member", "admin"])
@pytest.mark.parametrize("own", [True, False])
@pytest.mark.parametrize("scope", ["sync", "admin"])
def test_permission_helpers(role, own, scope):
    principal = Principal("alice", role, "password")
    user = "alice" if own else "bob"
    record = SimpleNamespace(user=user, scope=scope)
    visible = role == "admin" or (role == "member" and own)
    assert keyspage.can_see(principal, record) == visible
    assert keyspage.can_revoke(principal, record) == visible
    error = keyspage.can_create(principal, user, scope)
    if role == "viewer":
        assert error == "forbidden"
    elif role != "admin" and not own:
        assert error == "you can only create keys for yourself"
    elif role != "admin" and scope == "admin":
        assert error == "only admins can create admin keys"
    else:
        assert error is None


def principal_session(server, user, role):
    """Create a user and sign in with a password through the HTTP endpoint."""
    password = "password1234"
    server.web.users.add(user, role=role)
    server.web.users.set_password(user, password)
    status, headers, body = call(
        server, "/login", "POST", json.dumps({"user": user, "password": password}), csrf(server))
    assert status == 200 and json.loads(body) == {"ok": True, "user": user, "role": role}
    return headers["Set-Cookie"].split(";", 1)[0]


def test_admin_keys_for_users(server):
    server.web.users.add("alice")
    server.keys.create("laptop", user="alice")
    status, _, body = api(server)
    assert status == 200 and body["user"] == "admin" and body["role"] == "admin"
    assert {r["user"] for r in body["keys"]} == {"admin", "alice"}
    assert all(r["active"] and r["revocable"] for r in body["keys"])
    status, _, body = api(server, method="POST", payload={"name": "desktop", "scope": "sync", "user": "alice"})
    assert status == 201 and body["record"]["user"] == "alice"
    assert body["record"]["created_by"] == "admin"
    status, _, body = api(server, method="POST", payload={"name": "bad", "scope": "admin", "user": "alice"})
    assert status == 400 and body == {"error": "admin keys need an admin user"}
    status, _, body = api(server, method="POST", payload={"name": "bad", "scope": "sync", "user": "missing"})
    assert status == 404 and body == {"error": "unknown user"}
    assert {r["name"] for r in api(server, "/api/server/keys?user=alice")[2]["keys"]} == {"desktop", "laptop"}
    assert api(server, "/api/server/keys?user=missing")[2]["keys"] == []
    assert api(server, "/api/server/keys/desktop", "DELETE")[0] == 200
    for query in ("?user=", "?user=..", "?user=bad%20name"):
        assert api(server, "/api/server/keys" + query)[0] == 400


def test_member_keys(server):
    cookie = principal_session(server, "alice", "member")
    server.keys.create("alice-session", user="alice")
    status, _, body = api(server, cookie=cookie)
    assert status == 200 and body["user"] == "alice" and body["role"] == "member"
    assert [r["name"] for r in body["keys"]] == ["alice-session"]
    assert api(server, "/api/server/keys?user=admin", cookie=cookie)[2]["keys"] == []
    status, _, body = api(server, method="POST", cookie=cookie, payload={"name": "laptop", "scope": "sync"})
    assert status == 201 and body["record"]["user"] == body["record"]["created_by"] == "alice"
    for payload, message in (
        ({"name": "bad", "scope": "admin"}, "only admins can create admin keys"),
        ({"name": "bad", "scope": "sync", "user": "admin"}, "you can only create keys for yourself"),
        ({"name": "bad", "scope": "sync", "user": "missing"}, "you can only create keys for yourself"),
    ):
        status, _, body = api(server, method="POST", cookie=cookie, payload=payload)
        assert status == 403 and body == {"error": message}
    assert api(server, "/api/server/keys/admin", "DELETE", cookie=cookie)[0] == 404
    assert api(server, "/api/server/keys/laptop", "DELETE", cookie=cookie)[0] == 200
    status, headers, body = api(server, "/api/server/keys/alice-session", "DELETE", cookie=cookie)
    assert status == 200 and body == {"revoked": "alice-session", "signed_out": False}
    assert "Set-Cookie" not in headers
    assert server.keys.for_user("alice") == []
    status, _, body = api(server, "/api/server/session", cookie=cookie)
    assert status == 200 and body["user"] == "alice" and body["role"] == "member"
    assert body["via"] == "password" and body["key"] is None


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/api/server/keys", None),
    ("POST", "/api/server/keys", {"name": "no", "scope": "sync"}),
    ("DELETE", "/api/server/keys/admin", None),
])
def test_viewer_forbidden(server, method, path, payload):
    cookie = principal_session(server, "reader", "viewer")
    status, _, body = api(server, path, method, payload=payload, cookie=cookie)
    assert status == 403 and body == {"error": "forbidden"}


def test_inactive_keys_and_removed_user(server, monkeypatch):
    users = server.web.users
    users.add("demoted", role="admin")
    server.keys.create("old-admin", scope="admin", user="demoted")
    server.keys.create("old-sync", user="demoted")
    users.set_role("demoted", "member")
    rows = {r["name"]: r for r in api(server)[2]["keys"]}
    assert rows["old-sync"]["active"] and not rows["old-admin"]["active"]
    users.set_role("demoted", "viewer")
    assert not any(r["active"] for r in api(server, "/api/server/keys?user=demoted")[2]["keys"])
    # A stale record can survive a concurrent removal. It is inactive and a
    # DELETE must not report success, even when list() still returns it.
    stale = server.keys.list()
    users.remove("demoted")
    monkeypatch.setattr(server.keys, "list", lambda: stale)
    rows = {r["name"]: r for r in api(server)[2]["keys"]}
    assert not rows["old-sync"]["active"]
    assert api(server, "/api/server/keys/old-sync", "DELETE")[0] == 404


def test_sync_attribution(server, tmp_path, monkeypatch):
    server.web.users.add("alice")
    _, first = server.keys.create("laptop", user="alice")
    _, second = server.keys.create("desktop", user="alice")
    url = f"http://127.0.0.1:{server.server_address[1]}"
    writer = writer_at(tmp_path, status=None)
    # Fixed times distinguish per-key activity, independent of test speed.
    with Client(url, first) as client:
        monkeypatch.setattr(layout, "format_timestamp", lambda: "2026-01-01T00:00:00Z")
        result = push((server, client), writer)
        assert not result.failed
    first_report = admin.status(server.data_dir)
    assert next(r for r in first_report["keys"] if r["name"] == "laptop")["last_sync_at"] == "2026-01-01T00:00:00Z"
    writer.write_scalars([(99, {"loss": 0.1})])
    writer.finish("finished")
    # Rotate keys on the same machine, retaining its original sync cache.
    with Client(url, second) as client:
        monkeypatch.setattr(layout, "format_timestamp", lambda: "2026-01-02T00:00:00Z")
        assert push((server, client), writer).complete
    path = server.root / "runs" / writer.run_id / "remote.json"
    remote = json.loads(path.read_bytes())
    assert remote["owner"] == "alice" and remote["owner_key"] == "laptop"
    assert remote["last_sync_key"] == "desktop"
    report = admin.status(server.data_dir)
    rows = {r["name"]: r for r in report["keys"]}
    assert rows["laptop"]["runs"] == 1 and rows["laptop"]["last_sync_at"] is None
    assert rows["desktop"]["runs"] == 0 and rows["desktop"]["last_sync_at"] == remote["last_sync_at"]
    alice = next(r for r in report["users"] if r["name"] == "alice")
    assert alice == {"name": "alice", "role": "member", "keys": 2, "runs": 1, "last_sync_at": remote["last_sync_at"]}
    text = admin.format_status(report)
    assert text.index("USER  ROLE") < text.index("KEY  SCOPE")
    server.keys.revoke("desktop")
    revoked = next(r for r in admin.status(server.data_dir)["keys"] if r["name"] == "desktop")
    assert revoked["revoked"] and revoked["user"] == "alice" and revoked["last_sync_at"] == remote["last_sync_at"]
    counts = {r["name"]: r["runs"] for r in api(server)[2]["keys"]}
    assert counts["laptop"] == 1


@contextmanager
def local_dashboard(root):
    httpd = ui_server.make_server(root, port=0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_owner_only_in_server_dashboard(server, tmp_path):
    writer = writer_at(tmp_path)
    directory = server.root / "runs" / writer.run_id
    directory.mkdir(parents=True)
    (directory / "meta.json").write_bytes((writer.run_dir / "meta.json").read_bytes())
    (directory / "remote.json").write_text(json.dumps({"owner": "alice", "owner_key": "laptop"}))
    for path in ("/api/runs", "/api/runs/" + writer.run_id):
        status, _, body = call(server, path, headers={"Cookie": server.cookie})
        assert status == 200
        result = json.loads(body)
        assert (result[0] if isinstance(result, list) else result)["owner"] == "alice"
    with local_dashboard(server.root) as local:
        for path in ("/api/runs", "/api/runs/" + writer.run_id):
            status, _, body = call(local, path)
            assert status == 200
            result = json.loads(body)
            assert "owner" not in (result[0] if isinstance(result, list) else result)
    (directory / "remote.json").unlink()
    assert json.loads(call(server, "/api/runs", headers={"Cookie": server.cookie})[2])[0]["owner"] is None


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("value,expected", [(None, None), (12, None), ([], None), ("laptop", "laptop")])
def test_reader_attribution(tmp_path, committed, value, expected):
    payload = {"owner": "alice", "owner_key": value, "last_sync_key": value}
    if committed:
        payload.update(status="finished", last_sync_at="2026-01-01T00:00:00Z")
    (tmp_path / "remote.json").write_text(json.dumps(payload))
    assert read_remote(tmp_path)["owner_key"] == read_remote(tmp_path)["last_sync_key"] == expected


def test_keys_list_user_filter(server, capsys):
    server.web.users.add("alice")
    server.keys.create("laptop", user="alice")
    args = ["server", "keys", "list", "--data", str(server.data_dir), "--user", "alice"]
    assert cli.main(args + ["--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["laptop"] and rows[0]["user"] == "alice"
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "laptop" in out and "alice" in out and "admin" not in out
    assert cli.main(args[:-1] + ["missing"]) == 1
    assert "unknown user" in capsys.readouterr().err


@pytest.mark.parametrize("identity,extra", [
    ({}, ""),
    ({"user": "alice", "role": "member"}, "user: alice (member)\n"),
    ({"user": "\x1b[31malice\x07", "role": "member\x1b[0m"}, "user: alice (member)\n"),
])
def test_whoami_cli_keeps_old_output(tmp_path, monkeypatch, capsys, identity, extra):
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGENTICML_API_KEY", "abcdefgh-very-secret")
    monkeypatch.setattr(Client, "whoami", lambda self: {"key": "laptop", "scope": "sync", **identity})
    assert cli.main(["whoami", "--host", "http://127.0.0.1:12345"]) == 0
    out = capsys.readouterr()
    assert not out.err
    assert out.out == ("http://127.0.0.1:12345  key=abcdefgh…  name=laptop  "
                       "scope=sync  source=env  valid\n" + extra)


def test_whoami_cli_real_server(server, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGENTICML_API_KEY", server.admin_key)
    assert cli.main(["whoami", "--host", f"http://127.0.0.1:{server.server_address[1]}"]) == 0
    assert "user: admin (admin)\n" in capsys.readouterr().out


def test_status_owner_key_fallback_and_unknown_writer(server):
    server.web.users.add("alice")
    server.keys.create("laptop", user="alice")
    directory = server.root / "runs" / "fallback"
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(json.dumps({"run_id": "fallback", "status": "finished"}))
    payload = {"owner": "alice", "owner_key": "laptop", "status": "finished",
               "last_sync_at": "2026-01-01T00:00:00Z"}
    (directory / "remote.json").write_text(json.dumps(payload))
    report = admin.status(server.data_dir)
    row = next(r for r in report["keys"] if r["name"] == "laptop")
    assert row["last_sync_at"] == payload["last_sync_at"] and row["runs"] == 1
    payload["last_sync_key"] = "unknown"
    (directory / "remote.json").write_text(json.dumps(payload))
    report = admin.status(server.data_dir)
    row = next(r for r in report["keys"] if r["name"] == "unknown")
    assert row["revoked"] and row["user"] == "alice" and row["runs"] == 0
    assert row["last_sync_at"] == payload["last_sync_at"]


def test_member_cannot_revoke_reused_key_name(server, monkeypatch):
    cookie = principal_session(server, "alice", "member")
    server.keys.create("laptop", user="alice")
    original = server.keys.revoke

    def concurrent_replacement(name, **kwargs):
        original(name)
        server.keys.create(name, user="admin")
        return original(name, **kwargs)

    monkeypatch.setattr(server.keys, "revoke", concurrent_replacement)
    status, _, body = api(server, "/api/server/keys/laptop", "DELETE", cookie=cookie)
    assert status == 404 and body == {"error": "not found"}
    assert server.keys.for_user("admin")[-1].name == "laptop"


def test_revoke_uses_principal_without_cookie_session(server, monkeypatch):
    server.keys.create("laptop", user="admin")
    original = server.web._attach

    def attach(handler, session, principal=None):
        result = original(handler, session, principal)
        # A trusted proxy request in package 13 supplies a principal without a
        # cookie session. Only test the key API's contract here.
        handler.principal = Principal("admin", "admin", "proxy")
        handler.session = None
        return result

    monkeypatch.setattr(server.web, "_attach", attach)
    status, _, body = api(server, "/api/server/keys/laptop", "DELETE")
    assert status == 200 and body == {"revoked": "laptop", "signed_out": False}


def test_reused_key_name_counts_are_per_user(server):
    cookie = principal_session(server, "alice", "member")
    server.web.users.add("bob")
    server.keys.create("laptop", user="bob")
    # Two former owners of this name must remain separate revoked rows.
    for user in ("admin", "alice"):
        directory = server.root / "runs" / user
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(json.dumps({"run_id": user, "status": "finished"}))
        (directory / "remote.json").write_text(json.dumps({
            "owner": user, "owner_key": "laptop", "last_sync_key": "laptop",
            "status": "finished", "last_sync_at": "2026-01-01T00:00:00Z"}))
    assert next(r for r in api(server)[2]["keys"] if r["name"] == "laptop")["runs"] == 0
    # Reuse for the member as well: their key row cannot inherit admin's run.
    server.keys.revoke("laptop")
    server.keys.create("laptop", user="alice")
    assert next(r for r in api(server, cookie=cookie)[2]["keys"] if r["name"] == "laptop")["runs"] == 1
    server.keys.revoke("laptop")
    server.keys.create("laptop", user="bob")
    rows = {(r["name"], r["user"]): r for r in admin.status(server.data_dir)["keys"]}
    assert rows["laptop", "bob"]["runs"] == 0
    assert rows["laptop", "bob"]["last_sync_at"] is None
    for user in ("admin", "alice"):
        assert rows["laptop", user]["runs"] == 1
        assert rows["laptop", user]["revoked"] is True
        assert rows["laptop", user]["last_sync_at"] == "2026-01-01T00:00:00Z"


def test_no_owner_key_counts_only_toward_user(server):
    directory = server.root / "runs" / "legacy"
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(json.dumps({"run_id": "legacy", "status": "finished"}))
    (directory / "remote.json").write_text(json.dumps({
        "owner": "admin", "status": "finished", "last_sync_at": "2026-01-01T00:00:00Z"}))
    assert next(r for r in api(server)[2]["keys"] if r["name"] == "admin")["runs"] == 0
    report = admin.status(server.data_dir)
    assert report["users"][0]["runs"] == 1
    assert report["keys"][0]["runs"] == 0 and report["keys"][0]["last_sync_at"] is None


@pytest.mark.parametrize("cached_user", [True, False])
@pytest.mark.parametrize("idle", [True, False])
def test_same_machine_key_rotation(server, tmp_path, cached_user, idle):
    server.web.users.add("alice")
    _, first = server.keys.create("laptop", user="alice")
    _, second = server.keys.create("desktop", user="alice")
    writer = writer_at(tmp_path)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    with Client(url, first) as client:
        assert push((server, client), writer).complete
    if not cached_user:
        path = writer.run_dir / "sync.json"
        payload = json.loads(path.read_bytes())
        payload["hosts"][url].pop("owner_user")
        path.write_text(json.dumps(payload))
    if not idle:
        meta_path = writer.run_dir / "meta.json"
        meta = json.loads(meta_path.read_bytes())
        meta["name"] = "renamed"
        meta_path.write_text(json.dumps(meta))
    with Client(url, second) as client:
        pair = (server, client)
        selected = sync._selected_ids(writer.root, url, [], True, key="desktop", user="alice")
        assert selected == [writer.run_id]  # Visit once to refresh the key cache.
        assert push(pair, writer).complete
        saved = state(pair, writer)
        assert saved["owner"] == "desktop" and saved["owner_user"] == "alice"
        assert sync._sync_idle(RunReader(writer.root, writer.run_id), saved, saved["source_hash"],
                               key="laptop", user="alice")
        assert sync._selected_ids(writer.root, url, [], True, key="desktop", user="alice") == []
    _, other = server.keys.create("foreign", user="admin")
    before = (writer.run_dir / "sync.json").read_bytes()
    with Client(url, other) as client:
        with pytest.raises(sync.OwnershipError, match="belongs to user alice"):
            push((server, client), writer)
    assert (writer.run_dir / "sync.json").read_bytes() == before


def test_key_rotation_without_whoami_user_keeps_key_ownership(server, tmp_path, monkeypatch):
    _, first = server.keys.create("laptop", user="admin")
    _, second = server.keys.create("desktop", user="admin")
    original = Client.whoami

    def old_whoami(client):
        identity = original(client)
        identity.pop("user")
        identity.pop("role")
        return identity

    monkeypatch.setattr(Client, "whoami", old_whoami)
    writer = writer_at(tmp_path)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    with Client(url, first) as client:
        assert push((server, client), writer).complete
    with Client(url, second) as client:
        with pytest.raises(sync.OwnershipError, match="belongs to key laptop"):
            push((server, client), writer)
        saved = state((server, client), writer)
        assert not sync._sync_idle(RunReader(writer.root, writer.run_id), saved, saved["source_hash"],
                                   key="desktop")


def test_owner_user_overrides_colliding_key_name():
    client = SimpleNamespace(_sync_identity={"key": "alice", "user": "bob"})
    with pytest.raises(sync.OwnershipError, match="belongs to user alice"):
        sync._check_owner(SimpleNamespace(run_id="run"), client, {"owner": "alice", "owner_user": "alice"})


@pytest.mark.parametrize("replacement_user", ["alice", "admin"])
def test_legacy_cache_checks_reused_key_name_via_all(server, tmp_path, monkeypatch, capsys,
                                                    replacement_user):
    server.web.users.add("alice")
    _, first = server.keys.create("laptop", user="alice")
    writer = writer_at(tmp_path)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    with Client(url, first) as client:
        assert push((server, client), writer).complete
    path = writer.run_dir / "sync.json"
    payload = json.loads(path.read_bytes())
    payload["hosts"][url].pop("owner_user")
    path.write_text(json.dumps(payload))
    server.keys.revoke("laptop")
    _, replacement = server.keys.create("laptop", user=replacement_user)
    before = path.read_bytes()
    original = Client.run_state
    checked = []

    def counted(client, run_id):
        checked.append(run_id)
        return original(client, run_id)

    monkeypatch.setattr(Client, "run_state", counted)
    with Client(url, replacement) as client:
        assert command((server, client), writer, "--all") == (0 if replacement_user == "alice" else 1)
        assert checked == [writer.run_id]
        output = capsys.readouterr()
        if replacement_user == "alice":
            assert state((server, client), writer)["owner_user"] == "alice"
            assert command((server, client), writer, "--all") == 0
            assert checked == [writer.run_id]  # Verified cache can now be skipped.
        else:
            assert "belongs to user alice" in output.out + output.err
            assert path.read_bytes() == before


@pytest.mark.parametrize("unchecked", ["no_cached_owner", "missing_remote", "ownerless_remote"])
def test_up_to_date_does_not_stamp_unchecked_identity(server, tmp_path, monkeypatch, unchecked):
    writer = writer_at(tmp_path)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    with Client(url, server.admin_key) as client:
        pair = (server, client)
        assert push(pair, writer).complete
        path = writer.run_dir / "sync.json"
        payload = json.loads(path.read_bytes())
        cached = payload["hosts"][url]
        cached.pop("owner_user")
        if unchecked == "no_cached_owner":
            cached.pop("owner")
        path.write_text(json.dumps(payload))
        before = path.read_bytes()
        checked = []

        def run_state(run_id):
            checked.append(run_id)
            return {"owner": None} if unchecked == "ownerless_remote" else None

        monkeypatch.setattr(client, "run_state", run_state)
        result = push(pair, writer)
        assert result.complete and result.status == "up to date"
        assert checked == ([] if unchecked == "no_cached_owner" else [writer.run_id])
        assert path.read_bytes() == before


@pytest.mark.parametrize("owner_fields", [{}, {"owner": None}, {"owner": 42}])
def test_status_owner_key_without_owner_has_no_attribution(server, owner_fields):
    directory = server.root / "runs" / "ownerless"
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(json.dumps({"run_id": "ownerless", "status": "finished"}))
    (directory / "remote.json").write_text(json.dumps({
        **owner_fields, "owner_key": "admin", "last_sync_key": "unknown",
        "status": "finished", "last_sync_at": "2026-01-01T00:00:00Z"}))
    report = admin.status(server.data_dir)
    assert report["runs"]["total"] == 1
    assert [(row["name"], row["user"]) for row in report["keys"]] == [("admin", "admin")]
    assert report["keys"][0]["runs"] == 0 and report["keys"][0]["last_sync_at"] is None
    assert report["users"][0]["runs"] == 0 and report["users"][0]["last_sync_at"] is None


def test_key_store_requires_user_with_typed_error(server):
    server.web.users.add("alice")
    with pytest.raises(keys.UserRequired, match="--user is required when the server has more than one user"):
        server.keys.create("laptop")


@pytest.mark.parametrize("error,expected", [
    (keys.UserRequired("choose a user explicitly"),
     "user is required when the server has more than one user"),
    (keys.ClientError("--user is required when the server has more than one user"),
     "--user is required when the server has more than one user"),
])
def test_key_api_user_required_message_uses_error_type(server, monkeypatch, error, expected):
    def create(*args, **kwargs):
        raise error

    monkeypatch.setattr(server.keys, "create", create)
    status, _, body = api(server, method="POST", payload={"name": "laptop", "scope": "sync"})
    assert status == 400 and body == {"error": expected}


@pytest.mark.parametrize("own", [True, False])
def test_member_key_name_unavailable(server, own):
    cookie = principal_session(server, "alice", "member")
    server.keys.create("taken", user="alice" if own else "admin")
    status, _, body = api(server, method="POST", cookie=cookie,
                          payload={"name": "taken", "scope": "sync"})
    assert status == 409 and body == {"error": "key name is not available"}
    assert api(server, method="POST", payload={"name": "taken", "scope": "sync"})[2] == {
        "error": "key name already exists"}


def test_env_admin_requires_user_in_api_and_cannot_delete_env(tmp_path):
    with running(tmp_path / "data", env_key="e" * 32) as httpd:
        httpd.cookie = login(httpd, "e" * 32)
        httpd.web.users.add("alice")
        httpd.web.users.add("bob")
        status, _, body = api(httpd, method="POST", payload={"name": "laptop", "scope": "sync"})
        assert status == 400 and body == {"error": "user is required when the server has more than one user"}
        status, _, body = api(httpd, "/api/server/keys/env", "DELETE")
        assert status == 409 and "environment key" in body["error"]
        assert httpd.keys.verify("e" * 32) is not None


def test_commit_without_principal_key(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"status": "finished"}))
    written = {}
    handler = SimpleNamespace(
        principal=Principal("alice", "member", "proxy", key=None),
        _target=lambda directory, name: directory / name,
        _write_json=lambda directory, name, payload: written.update(payload))
    result = ingest.IngestHandler._commit(handler, tmp_path, json.dumps({
        "status": "finished", "fingerprint": "a" * 64, "sizes": {}}).encode(),
        {"owner": "alice", "owner_key": "laptop"})
    assert result[1] == 200
    assert written["last_sync_key"] is None
    assert written["owner"] == "alice" and written["owner_key"] == "laptop"


def test_runs_list_reads_remote_once_per_run(server, tmp_path, monkeypatch):
    writer = writer_at(tmp_path)
    with Client(f"http://127.0.0.1:{server.server_address[1]}", server.admin_key) as client:
        assert push((server, client), writer).complete
    original = reader.read_remote
    calls = []

    def counted(directory):
        calls.append(directory)
        return original(directory)

    monkeypatch.setattr(reader, "read_remote", counted)
    status, _, raw = call(server, "/api/runs", headers={"Cookie": server.cookie})
    assert status == 200 and json.loads(raw)[0]["owner"] == "admin"
    assert len(calls) == 1
    assert "_remote_owner" not in json.loads(raw)[0]

"""Users, roles, migration, ownership-by-user, and the users CLI."""
from __future__ import annotations
from server_db_helpers import imported_remote, key_object, user_object
from agenticml.server.db import DatabaseError

import hashlib
import json
import os
import stat

import pytest

from agenticml import cli, sync
from agenticml.client import Client
from agenticml.server import admin
from agenticml.server import up as up_mod
from agenticml.server import webauth as webauth_mod
from agenticml.server.app import make_server, prepare_data_dir
from agenticml.server.keys import ClientError, KeyStore, KeyStoreUnavailable, LastAdminError
from agenticml.server.up import UpError, bootstrap_admin
from agenticml.server.users import (
    ACTIONS, IdentityTaken, Principal, ROLES, UserNameTaken, UserNotFound, UserStore,
    allowed, migrate, resolve,
)
from agenticml.store import RunReader, layout
from test_server_ingest import append, foreign_key, put_meta, raw, request, running
from test_server_web import authed, call, login
from test_sync import writer_at


def server_dir(tmp_path):
    directory = tmp_path / "server"
    directory.mkdir()
    return directory


def write_v1_keys(path, names, scopes=None):
    records = []
    for i, name in enumerate(names):
        secret = "s" + name + "x" * 32
        records.append({
            "name": name, "scope": scopes[i] if scopes else "sync",
            "sha256": hashlib.sha256(secret.encode()).hexdigest(),
            "prefix": secret[:8], "created_at": layout.format_timestamp(),
            "created_by": "cli", "last_used_at": None,
        })
    path.write_text(json.dumps({"version": 1, "keys": records}, indent=2) + "\n")


def write_run(root, run_id, owner, extra=None):
    directory = root / "runs" / run_id
    directory.mkdir(parents=True)
    payload = {"owner": owner}
    if extra:
        payload.update(extra)
    (directory / "remote.json").write_text(json.dumps(payload))
    (directory / "meta.json").write_text(json.dumps({"run_id": run_id, "status": "running"}))
    from server_db_helpers import index_if_open
    index_if_open(root, directory, remote=payload)
    return directory


# --- 1. UserStore CRUD and strict parse ---

def test_userstore_crud_and_public(tmp_path):
    users = UserStore(server_dir(tmp_path))
    alice = users.add("alice", role="admin", identity="alice@example.com")
    bob = users.add("bob")
    assert bob.role == "member" and bob.identity is None and bob.generation == 0
    assert alice.id != bob.id
    assert len(alice.id) == 32 and all(ch in "0123456789abcdef" for ch in alice.id)
    assert alice.password is None and alice.invite is None
    public = alice.to_public()
    assert public["password_set"] is False and public["invite_pending"] is False
    assert "password" not in public and "invite" not in public
    assert users.get("alice") == alice
    assert {u.name for u in users.list()} == {"alice", "bob"}
    users.set_role("bob", "viewer")
    assert users.get("bob").role == "viewer" and users.get("bob").generation == 1
    users.set_identity("bob", "bob@example.com")
    assert users.get("bob").identity == "bob@example.com"
    users.set_identity("bob", None)
    assert users.get("bob").identity is None
    assert users.by_identity("alice@example.com").name == "alice"
    assert users.by_identity("missing") is None
    with pytest.raises(UserNameTaken):
        users.add("alice")
    with pytest.raises(IdentityTaken):
        users.add("carol", identity="alice@example.com")
    with pytest.raises(IdentityTaken):
        users.add("carol", identity="Alice@Example.com")
    users.set_identity("bob", "Bob@Example.com")
    with pytest.raises(IdentityTaken):
        users.set_identity("alice", "bob@example.com")
    users.set_identity("bob", None)
    users.add("carol", role="admin")
    assert users.remove("bob") == 0
    assert users.get("bob") is None
    assert stat.S_IMODE(users.db.path.stat().st_mode) == 0o600


def test_discard_removes_only_matching_id(tmp_path):
    users = UserStore(server_dir(tmp_path))
    alice = users.add("alice", role="admin")
    users.discard("alice", "0" * 32)
    assert users.get("alice") is not None
    users.discard("bob", alice.id)
    assert users.get("alice") is not None
    users.discard("alice", alice.id)
    assert users.get("alice") is None


@pytest.mark.parametrize("change", [
    "extra", "missing", "role", "duplicate_name", "duplicate_identity",
    "duplicate_identity_case", "duplicate_id", "id_bad",
    "generation_bool", "generation_negative",
])
def test_userstore_strict_parse_rejects(tmp_path, change):
    users = UserStore(server_dir(tmp_path))
    users.add("alice", role="admin", identity="a@x.com")
    users.add("bob", role="admin", identity="b@x.com")
    obj = user_object(users)
    if change == "extra":
        obj["users"][0]["extra"] = 1
    elif change == "missing":
        obj["users"][0].pop("created_by")
    elif change == "role":
        obj["users"][0]["role"] = "owner"
    elif change == "duplicate_name":
        obj["users"][1]["name"] = "alice"
    elif change == "duplicate_identity":
        obj["users"][1]["identity"] = "a@x.com"
    elif change == "duplicate_identity_case":
        obj["users"][1]["identity"] = "A@X.COM"
    elif change == "duplicate_id":
        obj["users"][1]["id"] = obj["users"][0]["id"]
    elif change == "id_bad":
        obj["users"][0]["id"] = "A" * 32
    elif change == "generation_bool":
        obj["users"][0]["generation"] = True
    else:
        obj["users"][0]["generation"] = -1
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    (legacy / 'users.json').write_text(json.dumps(obj))
    with pytest.raises(DatabaseError, match='could not import users.json'):
        UserStore(legacy)


def test_userstore_corrupt_fails_closed_once_per_change(tmp_path, caplog):
    directory = server_dir(tmp_path)
    path = directory / 'users.json'
    path.write_bytes(b'not JSON')
    for _ in range(3):
        with pytest.raises(DatabaseError, match='could not import users.json'):
            UserStore(directory)
    path.write_text(json.dumps({'version':1, 'users':[]}))
    assert UserStore(directory).list() == []


@pytest.mark.parametrize("value", ["", " x", "x ", "a" * 257, "a\nb", "a\x00b"])
def test_invalid_identity(tmp_path, value):
    users = UserStore(server_dir(tmp_path))
    with pytest.raises(ClientError):
        users.add("alice", identity=value)


def test_allowed_and_resolve(tmp_path):
    directory = server_dir(tmp_path)
    users = UserStore(directory)
    users.add("alice", role="admin")
    users.add("view", role="viewer")
    keys = KeyStore(directory)
    _, secret = keys.create("k", user="alice")
    record = keys.verify(secret)
    principal = resolve(keys, users, record)
    alice = users.get("alice")
    assert principal == Principal(
        "alice", "admin", "key", "k", alice.id, alice.generation)
    assert allowed(principal, "sync") and allowed(principal, "users")
    with pytest.raises(KeyError):
        allowed(principal, "unknown")
    view = Principal("view", "viewer", "key", "v")
    assert allowed(view, "read") and not allowed(view, "sync")
    assert resolve(keys, users, keys.verify("missing")) is None
    env = KeyStore(directory, "e" * 32)
    assert resolve(env, users, env.verify("e" * 32)) == Principal("env", "admin", "key", "env")


# --- 2. Last-admin ---

def test_last_admin_refusal_env_not_counted(tmp_path):
    directory = server_dir(tmp_path)
    users = UserStore(directory)
    users.add("alice", role="admin")
    KeyStore(directory, "e" * 32)
    with pytest.raises(LastAdminError):
        users.remove("alice")
    with pytest.raises(LastAdminError):
        users.set_role("alice", "member")
    users.add("bob", role="admin")
    users.set_role("alice", "member")
    assert users.get("alice").role == "member"
    users.set_role("alice", "admin")
    assert users.remove("alice") == 0
    with pytest.raises(LastAdminError):
        users.remove("bob")


# --- 3. remove deletes keys; running server 401 ---

def test_remove_deletes_keys_and_running_server_rejects(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("bob", role="member")
        _, secret = httpd.keys.create("box", user="bob")
        assert request(httpd, "/v2/whoami", key=secret)[0] == 200
        assert httpd.users.remove("bob") == 1
        assert httpd.keys.verify(secret) is None
        assert request(httpd, "/v2/whoami", key=secret)[0] == 401


# --- 4. create default-user rule ---

def test_create_default_user_rule(tmp_path):
    directory = server_dir(tmp_path)
    keys = KeyStore(directory)
    users = UserStore(directory)
    record, _ = keys.create("first")
    assert record.user == "admin"
    assert users.get("admin").role == "admin"
    assert users.get("admin").created_by == "cli"
    record, _ = keys.create("second")
    assert record.user == "admin"
    users.add("bob")
    with pytest.raises(ClientError, match="--user is required"):
        keys.create("third")
    with pytest.raises(UserNotFound):
        keys.create("third", user="missing")
    with pytest.raises(ClientError, match="admin keys need an admin user"):
        keys.create("third", scope="admin", user="bob")
    record, _ = keys.create("third", user="bob")
    assert record.user == "bob"


# --- 5. Migration ---

def test_migration_v1_keys_runs_and_idempotent(tmp_path, caplog):
    caplog.set_level("INFO")
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["alpha", "beta"])
    root = data / "store"
    write_run(root, "run-a", "alpha")
    write_run(root, "run-b", "beta")
    env_dir = write_run(root, "run-env", "env")
    env_before = (env_dir / "remote.json").read_bytes()
    bad = root / "runs" / "run-bad"
    bad.mkdir(parents=True)
    (bad / "remote.json").write_text("broken")
    assert migrate(data) is True
    users = user_object(UserStore(data / "server"))
    assert (data / "server" / "users.json.2x").is_file()
    assert [u["name"] for u in users["users"]] == ["admin"]
    assert users["users"][0]["role"] == "admin"
    assert users["users"][0]["created_by"] == "migration"
    admin_id = users["users"][0]["id"]
    assert len(admin_id) == 32 and all(ch in "0123456789abcdef" for ch in admin_id)
    keys_obj = key_object(KeyStore(data / "server"))
    assert json.loads((data / "server" / "keys.json.2x").read_text())["version"] == 2
    assert {row["name"]: row["user"] for row in keys_obj["keys"]} == {
        "alpha": "admin", "beta": "admin"}
    from agenticml.server.db import for_server_dir
    from agenticml.server.db.repos import RunRepo
    index = RunRepo(for_server_dir(data / "server", create=False))
    assert index.as_remote("run-a") == {"owner": "admin", "owner_key": "alpha"}
    assert index.as_remote("run-b") == {"owner": "admin", "owner_key": "beta"}
    assert imported_remote(root / "runs" / "run-a")[0] == {"owner": "admin", "owner_key": "alpha"}
    assert imported_remote(root / "runs" / "run-b")[0] == {"owner": "admin", "owner_key": "beta"}
    assert imported_remote(env_dir)[1].read_bytes() == env_before
    assert "skipped unreadable remote.json in run-bad" in caplog.text
    assert ("migration skipped 1 corrupt or unreadable remote.json file(s); "
            "those runs keep their old owner") in caplog.text
    assert "migrated 2.0 keys to user admin (2 keys, 2 runs)" in caplog.text
    mtimes = {
        path: path.stat().st_mtime_ns
        for path in (root / "runs").glob("*/remote.json*")
    }
    caplog.clear()
    assert migrate(data) is True
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in mtimes.items())
    assert "migrated 2.0 keys" not in caplog.text


def test_migration_crash_and_empty(tmp_path):
    data = tmp_path / 'data'
    prepare_data_dir(data)
    write_v1_keys(data / 'server' / 'keys.json', ['alpha'])
    write_run(data / 'store', 'run-a', 'alpha')
    # Model a crash after legacy conversion, before the DB import commit.
    from agenticml.server.db.legacy2x import migrate as convert
    from agenticml.server.db import connect, default_url
    db = connect(default_url(data))
    assert convert(data, db=db)
    (data / 'server' / 'users.json').unlink()
    db.close()
    assert migrate(data)
    assert UserStore(data / 'server').get('admin').role == 'admin'
    assert KeyStore(data / 'server').list()[0].user == 'admin'
    empty = tmp_path / 'empty'
    prepare_data_dir(empty)
    assert migrate(empty)
    assert UserStore(empty / 'server').list() == []
    assert not (empty / 'server' / 'users.json').exists()
    assert not (empty / 'server' / 'keys.json').exists()


def test_migrate_empty_v1_keys_rewrites_to_v2(tmp_path, capsys):
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", [])
    assert json.loads((data / "server" / "keys.json").read_text()) == {
        "version": 1, "keys": []}
    assert migrate(data) is True
    keys_obj = key_object(KeyStore(data / "server"))
    assert json.loads((data / "server" / "keys.json.2x").read_text()) == {"version":2,"keys":[]}
    assert KeyStore(data / "server").list() == []
    assert UserStore(data / 'server').list() == []
    assert cli.main(["server", "keys", "create", "--name", "first",
                     "--data", str(data)]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("aml_")
    keys = KeyStore(data / "server")
    assert [record.name for record in keys.list()] == ["first"]
    assert keys.list()[0].user == "admin"


def test_migrate_logs_skipped_remote_summary(tmp_path, caplog):
    caplog.set_level("WARNING")
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["alpha"])
    write_run(data / "store", "run-a", "alpha")
    for name, payload in (("run-bad1", "broken"), ("run-bad2", "[1]")):
        directory = data / "store" / "runs" / name
        directory.mkdir()
        (directory / "remote.json").write_text(payload)
    assert migrate(data) is True
    assert "skipped unreadable remote.json in run-bad1" in caplog.text
    assert "skipped unreadable remote.json in run-bad2" in caplog.text
    assert ("migration skipped 2 corrupt or unreadable remote.json file(s); "
            "those runs keep their old owner") in caplog.text
    from agenticml.server.db import for_server_dir
    from agenticml.server.db.repos import RunRepo
    index = RunRepo(for_server_dir(data / "server", create=False))
    assert index.as_remote("run-a") == {"owner": "admin", "owner_key": "alpha"}
    assert imported_remote(data / "store" / "runs" / "run-a")[0] == {
        "owner": "admin", "owner_key": "alpha"}


def test_v1_keys_with_users_json_fails_closed(tmp_path, caplog):
    directory = server_dir(tmp_path)
    (directory / 'users.json').write_text(json.dumps({'version':1, 'users':[]}))
    write_v1_keys(directory / 'keys.json', ['alpha'])
    with pytest.raises(DatabaseError, match='could not import keys.json'):
        KeyStore(directory)


# --- 6. make_server and CLI migrate ---

def test_make_server_and_cli_keys_list_migrate(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["alpha"])
    write_run(data / "store", "run-a", "alpha")
    httpd = make_server(data, port=0)
    try:
        users = user_object(UserStore(data / "server"))
        assert [u["name"] for u in users["users"]] == ["admin"]
        assert json.loads((data / "server" / "keys.json.2x").read_text())["version"] == 2
    finally:
        httpd.server_close()

    other = tmp_path / "cli"
    prepare_data_dir(other)
    write_v1_keys(other / "server" / "keys.json", ["alpha"])
    assert cli.main(["server", "keys", "list", "--data", str(other), "--json"]) == 0
    capsys.readouterr()
    assert user_object(UserStore(other / "server"))["users"][0]["name"] == "admin"
    assert json.loads((other / "server" / "keys.json.2x").read_text())["version"] == 2


# --- 7–8. Ingest roles, ownership, compatibility ---

def test_viewer_whoami_and_write_routes_403(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("view", role="viewer")
        _, secret = httpd.keys.create("vkey", user="view")
        status, payload, _ = request(httpd, "/v2/whoami", key=secret)
        assert status == 200 and payload["role"] == "viewer" and payload["user"] == "view"
        body = b'{"run_id":"run-1","status":"running"}'
        routes = [
            ("POST", "/v2/objects/missing", b'{"objects":[]}'),
            ("PUT", "/v2/objects/" + "a" * 64, b"x"),
            ("PUT", "/v2/runs/run-1/files/meta.json", body),
            ("POST", "/v2/runs/run-1/append/events.jsonl?offset=0", b"{}\n"),
            ("POST", "/v2/runs/run-1/commit", b"{}"),
            ("PUT", "/v2/runs/run-1/lineage", b"x"),
        ]
        for method, path, payload_body in routes:
            status, obj, headers = raw(
                httpd, method, path,
                [("Authorization", "Bearer " + secret), ("Content-Length", "1000")])
            assert status == 403
            assert obj == {"error": "role viewer cannot sync"}
            assert headers["Connection"] == "close"
        for path in ("/v2/runs/run-1/state", "/v2/runs/run-1/lineage"):
            status, obj, _ = raw(
                httpd, "GET", path, [("Authorization", "Bearer " + secret)])
            assert status == 403
            assert obj == {"error": "role viewer cannot sync"}
        assert not list(httpd.tmp_dir.iterdir())


def test_member_ownership_and_compat_state(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="member")
        _, first = httpd.keys.create("one", user="alice")
        _, second = httpd.keys.create("two", user="alice")
        httpd.test_key = first
        assert put_meta(httpd)[0] == 200
        remote = httpd.run_index.as_remote("run-1")
        assert remote == {"owner": "alice", "owner_key": "one"}
        assert append(httpd, b"{}\n", name="events.jsonl", key=second)[0] == 200
        size = (httpd.root / "runs" / "run-1" / "meta.json").stat().st_size
        events = (httpd.root / "runs" / "run-1" / "events.jsonl").stat().st_size
        assert request(httpd, "/v2/runs/run-1/commit", "POST", json.dumps({
            "status": "running", "fingerprint": "a" * 64,
            "sizes": {"meta.json": size, "events.jsonl": events},
        }).encode(), key=second)[0] == 200
        status, state, _ = request(httpd, "/v2/runs/run-1/state", key=second)
        assert status == 200
        assert state["owner"] == "two" and state["owner_user"] == "alice"
        assert state["remote"]["owner"] == "two" and state["remote"]["owner_user"] == "alice"
        other = foreign_key(httpd, "other", user="bob")
        status, payload, _ = request(httpd, "/v2/runs/run-1/files/meta.json", "PUT", b"{}", other)
        assert status == 409
        assert payload["error"] == "run owned by another user"
        assert payload["owner"] == "alice" and payload["owner_user"] == "alice"
        row = httpd.run_index.get("run-1")
        httpd.run_index.commit_sync(
            "run-1", row["status"], row["fingerprint"], row["last_sync_at"], "one")
        status, state, _ = request(httpd, "/v2/runs/run-1/state", key=other)
        assert status == 200
        assert "owner_key" not in state["remote"]
        assert "last_sync_key" not in state["remote"]
        status, owned, _ = request(httpd, "/v2/runs/run-1/state", key=second)
        assert status == 200
        assert owned["remote"]["owner_key"] == "one"
        assert owned["remote"]["last_sync_key"] == "one"


def test_compat_sync_client_two_keys_and_foreign_user(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    with running(tmp_path / "data") as httpd:
        _, first = httpd.keys.create("one")
        _, second = httpd.keys.create("two")
        url = "http://127.0.0.1:%d" % httpd.server_address[1]
        writer = writer_at(tmp_path)
        with Client(url, first, timeout=2, max_retries=0) as client:
            result = sync.SyncResult(writer.run_id)
            sync.sync_run(RunReader(writer.root, writer.run_id), url, client, result)
            assert result.complete
        (writer.run_dir / "sync.json").unlink()
        with Client(url, second, timeout=2, max_retries=0) as client:
            result = sync.SyncResult(writer.run_id)
            sync.sync_run(RunReader(writer.root, writer.run_id), url, client, result)
            assert result.complete
        other = foreign_key(httpd, "three")
        with Client(url, other, timeout=2, max_retries=0) as client:
            result = sync.SyncResult(writer.run_id)
            with pytest.raises(sync.OwnershipError):
                sync.sync_run(RunReader(writer.root, writer.run_id), url, client, result)


# --- 9. Dashboard session ---

def test_session_includes_user_and_ends_on_demote(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        httpd.users.add("other", role="admin")
        status, _, hdrs = login(httpd)
        assert status == 200
        cookie = hdrs["Set-Cookie"]
        status, _, body, _ = authed(httpd, "/api/server/session", cookie=cookie)
        assert status == 200
        payload = json.loads(body)
        assert payload["user"] == "admin" and payload["role"] == "admin"
        httpd.users.set_role("admin", "member")
        status, _, body = call(
            httpd, "/api/server/session",
            headers={"Cookie": cookie.split(";", 1)[0]})
        assert status == 401


def test_keys_post_created_by_is_session_user(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="admin")
        _, secret = httpd.keys.create("alice-laptop", "admin", user="alice")
        status, _, hdrs = login(httpd, secret)
        assert status == 200
        cookie = hdrs["Set-Cookie"]
        status, _, body, _ = authed(
            httpd, "/api/server/keys", "POST",
            json.dumps({"name": "alice-box", "scope": "sync"}).encode(),
            cookie=cookie)
        assert status == 201
        payload = json.loads(body)
        assert payload["record"]["created_by"] == "alice"
        assert payload["record"]["user"] == "alice"
        record = next(item for item in httpd.keys.list() if item.name == "alice-box")
        assert record.created_by == "alice" and record.user == "alice"


# --- 10. CLI ---

def test_users_cli_and_keys_create_user(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = ["--data", str(tmp_path)]
    assert cli.main(["server", "users", "add", "alice", "--role", "admin", "--no-invite", *data]) == 0
    assert cli.main(["server", "users", "add", "bob", "--identity", "bob@x.com", "--no-invite", *data]) == 0
    assert cli.main(["server", "users", "list", "--json", *data]) == 0
    rows = json.loads(capsys.readouterr().out)
    by_name = {row["name"]: row for row in rows}
    assert by_name["alice"]["role"] == "admin" and by_name["bob"]["identity"] == "bob@x.com"
    assert by_name["bob"]["keys"] == 0
    assert cli.main(["server", "users", "list", *data]) == 0
    text = capsys.readouterr().out
    assert all(label in text for label in ("name", "role", "identity", "keys", "created_at", "created_by"))
    assert cli.main(["server", "users", "set-role", "bob", "viewer", *data]) == 0
    assert cli.main(["server", "users", "set-identity", "bob", "new@x.com", *data]) == 0
    assert cli.main(["server", "keys", "create", "--name", "k1", "--user", "alice", *data]) == 0
    capsys.readouterr()
    assert cli.main(["server", "users", "set-identity", "bob", *data]) == 0
    assert cli.main(["server", "users", "list", "--json", *data]) == 0
    rows = json.loads(capsys.readouterr().out)
    by_name = {row["name"]: row for row in rows}
    assert by_name["bob"]["role"] == "viewer" and by_name["bob"]["identity"] is None
    assert by_name["alice"]["keys"] == 1
    assert cli.main(["server", "users", "remove", "bob", *data]) == 0
    assert "removed bob (0 keys)" in capsys.readouterr().out
    assert cli.main(["server", "users", "remove", "missing", *data]) == 1
    assert "unknown user" in capsys.readouterr().err
    assert cli.main(["server", "users", "add", "alice", *data]) == 1
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["server", "users", "remove", "alice", *data]) == 1
    assert "last admin" in capsys.readouterr().err
    assert cli.main(["server", "keys", "create", "--name", "k2", *data]) == 0
    capsys.readouterr()
    assert cli.main(["server", "users", "add", "carol", "--no-invite", *data]) == 0
    capsys.readouterr()
    assert cli.main(["server", "keys", "create", "--name", "k3", *data]) == 1
    assert "--user is required" in capsys.readouterr().err


# --- 11. server status v1 and v2 ---

def test_status_v1_and_v2_owner_key(tmp_path):
    data = tmp_path / "v1"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["box"])
    write_run(data / "store", "run-a", "box", extra={
        "status": "finished", "last_sync_at": layout.format_timestamp(), "sizes": {}})
    report = admin.status(data)
    assert report['keys'] == report['users'] == []
    assert not (data / 'server' / 'agenticml.db').exists()
    assert migrate(data)
    report = admin.status(data)
    row = next(r for r in report['keys'] if r['name'] == 'box')
    assert row['runs'] == 1 and row['last_sync_at'] is not None
    assert report['users'][0]['name'] == 'admin'

    data = tmp_path / "v2"
    prepare_data_dir(data)
    keys = KeyStore(data / "server")
    keys.create("box")
    write_run(data / "store", "run-a", "admin", extra={
        "owner_key": "box", "status": "finished",
        "last_sync_at": layout.format_timestamp(), "sizes": {}})
    write_run(data / "store", "run-b", "admin", extra={
        "owner_key": "missing", "status": "finished",
        "last_sync_at": layout.format_timestamp(), "sizes": {}})
    report = admin.status(data)
    by_name = {r["name"]: r for r in report["keys"]}
    assert by_name["box"]["runs"] == 1
    assert by_name["missing"]["runs"] == 1 and by_name["missing"].get("revoked")
    users = {r["name"]: r for r in report["users"]}
    assert users["admin"]["keys"] == 1 and users["admin"]["runs"] == 2
    text = admin.format_status(report)
    assert "USER  ROLE  KEYS  RUNS" in text and "admin" in text


def test_actions_cover_role_table():
    assert ROLES == ("viewer", "member", "admin")
    assert set(ACTIONS) >= {
        "read", "password", "notes", "sync", "own_keys",
        "admin_keys", "all_keys", "users", "audit",
    }


def test_bootstrap_admin_non_admin_named_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(up_mod, "_stdout_is_tty", lambda: True)
    data = tmp_path / "data"
    prepare_data_dir(data)
    UserStore(data / "server").add("admin", role="member")
    with pytest.raises(UpError, match="not an admin"):
        bootstrap_admin(data, "https://example.test")


def test_bootstrap_admin_prints_invite_guidance_without_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(up_mod, "_stdout_is_tty", lambda: True)
    data = tmp_path / "data"
    prepare_data_dir(data)
    UserStore(data / "server").add("admin", role="admin")
    assert bootstrap_admin(data, "https://example.test") == data.resolve()
    assert "run `agenticml server users invite admin`" in capsys.readouterr().out


def test_bootstrap_admin_migrates_v1_admin_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(up_mod, "_stdout_is_tty", lambda: False)
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["admin"], scopes=["admin"])
    assert bootstrap_admin(data, "https://example.test") == data.resolve()
    keys = KeyStore(data / "server")
    assert [record.name for record in keys.list()] == ["admin"]
    assert keys.list()[0].scope == "admin" and keys.list()[0].user == "admin"
    users = user_object(UserStore(data / "server"))
    assert [row["name"] for row in users["users"]] == ["admin"]
    assert not any(line.startswith("aml_") for line in capsys.readouterr().out.splitlines())


def test_bootstrap_admin_raises_when_migrate_fails(tmp_path):
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["admin"], scopes=["admin"])
    (data / "server" / "keys.lock").mkdir()
    with pytest.raises(DatabaseError, match="could not import keys.json"):
        bootstrap_admin(data, "https://example.test")
    assert not (data / "server" / "users.json").exists()


def test_migrate_lock_failure_logs_and_cli_aborts(tmp_path, caplog, capsys):
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["alpha"])
    (data / "server" / "keys.lock").mkdir()
    assert migrate(data) is False
    assert "could not lock keys store" in caplog.text
    assert not (data / "server" / "users.json").exists()
    assert cli.main(["server", "users", "add", "alice", "--data", str(data)]) == 1
    assert "could not import keys.json" in capsys.readouterr().err
    assert not (data / "server" / "users.json").exists()
    capsys.readouterr()
    assert cli.main(["server", "keys", "list", "--data", str(data)]) == 1
    assert "could not import keys.json" in capsys.readouterr().err


@pytest.mark.parametrize("target", ["users.json", "keys.json"])
def test_migrate_lstat_permission_error(tmp_path, monkeypatch, caplog, target):
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["alpha"])
    users_path = data / "server" / "users.json"
    if target == "users.json":
        users_path.write_text(json.dumps({"version": 1, "users": []}) + "\n")
    blocked = os.path.abspath(users_path if target == "users.json" else data / "server" / "keys.json")
    real_lstat = os.lstat

    def boom(path, *args, **kwargs):
        if os.path.abspath(path) == blocked:
            raise PermissionError("injected")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", boom)
    assert migrate(data) is False
    assert "could not import legacy server state" in caplog.text
    if target == "users.json":
        assert json.loads((data / "server" / "keys.json").read_text())["version"] == 1
    else:
        assert not users_path.exists()
        assert json.loads((data / "server" / "keys.json").read_text())["version"] == 1


def test_migrate_unreadable_keys_logs_and_cli_aborts(tmp_path, caplog, capsys):
    data = tmp_path / "data"
    prepare_data_dir(data)
    (data / "server" / "keys.json").write_text("{")
    assert migrate(data) is False
    assert "could not import legacy server state" in caplog.text
    assert not (data / "server" / "users.json").exists()
    assert cli.main(["server", "users", "add", "alice", "--data", str(data)]) == 1
    assert "could not import keys.json" in capsys.readouterr().err
    assert not (data / "server" / "users.json").exists()
    capsys.readouterr()
    assert cli.main(["server", "keys", "list", "--json", "--data", str(data)]) == 1
    assert "could not import keys.json" in capsys.readouterr().err


def test_migrate_aborts_on_remote_write_failure(tmp_path, monkeypatch, caplog):
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["alpha"])
    write_run(data / "store", "run-a", "alpha")
    original = layout.atomic_write

    def boom(path, payload, *, fsync=False):
        if path.name == "remote.json":
            raise OSError("injected write failure")
        return original(path, payload, fsync=fsync)

    monkeypatch.setattr("agenticml.server.users.layout.atomic_write", boom)
    assert migrate(data) is False
    assert "failed writing remote.json" in caplog.text
    assert json.loads((data / "server" / "keys.json").read_text())["version"] == 1
    assert not (data / "server" / "users.json").exists()


def test_migrate_refuses_symlinked_runs_dir(tmp_path, caplog):
    outside = tmp_path / "outside"
    write_run(outside, "run-out", "alpha")
    before = (outside / "runs" / "run-out" / "remote.json").read_bytes()
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["alpha"])
    (data / "store" / "runs").symlink_to(outside / "runs")
    assert migrate(data) is False
    assert "symlink or not a directory" in caplog.text
    assert (outside / "runs" / "run-out" / "remote.json").read_bytes() == before
    assert json.loads((data / "server" / "keys.json").read_text())["version"] == 1
    assert not (data / "server" / "users.json").exists()


def test_add_scans_runs_before_write_lock(tmp_path, monkeypatch):
    data = tmp_path / "data"
    prepare_data_dir(data)
    users = UserStore(data / "server")
    order = []

    from agenticml.server.db.repos import RunRepo

    def scan(self, name, tx=None):
        order.append("scan")
        if tx is None:
            with self.db.transaction() as tx:
                return tx.fetchone('SELECT 1 FROM runs WHERE owner_user=?', (name,)) is not None
        return tx.fetchone('SELECT 1 FROM runs WHERE owner_user=?', (name,)) is not None

    monkeypatch.setattr(RunRepo, "name_owns", scan)
    real_tx = users.db.transaction

    def wrapped(write=False):
        if write:
            order.append("write")
        return real_tx(write=write)

    monkeypatch.setattr(users.db, "transaction", wrapped)
    users.add("alice", role="admin")
    assert "scan" in order and "write" in order
    assert order.index("scan") < order.index("write")


def test_add_refuses_name_that_owns_runs(tmp_path, capsys):
    data = tmp_path / "data"
    prepare_data_dir(data)
    users = UserStore(data / "server")
    users.add("alice", role="admin")
    users.add("bob", role="admin")
    write_run(data / "store", "run-a", "alice")
    users.remove("alice")
    with pytest.raises(ClientError, match="runs are still owned by alice; pass --reclaim-runs"):
        users.add("alice")
    assert users.get("alice") is None
    users.add("alice", reclaim_runs=True)
    assert users.get("alice") is not None
    users.remove("alice")
    assert cli.main(["server", "users", "add", "alice", "--data", str(data)]) == 1
    assert "runs are still owned by alice" in capsys.readouterr().err
    assert users.get("alice") is None
    assert cli.main(["server", "users", "add", "alice", "--reclaim-runs", "--data", str(data)]) == 0
    assert UserStore(data / "server").get("alice") is not None


def test_add_refuses_when_run_scan_truncated(tmp_path, monkeypatch):
    data = tmp_path / "data"
    prepare_data_dir(data)
    users = UserStore(data / "server")
    users.add("admin", role="admin")
    write_run(data / "store", "run-a", "zzz")
    write_run(data / "store", "run-b", "zzz")
    users.add("alice")
    assert users.get("alice") is not None
    with pytest.raises(ClientError, match="reclaim-runs"):
        users.add("zzz")
    users.add("zzz", reclaim_runs=True)
    assert users.get("zzz") is not None


def test_resolve_none_after_session_is_401(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    real = webauth_mod.resolve
    with running(tmp_path / "data") as httpd:
        _, httpd.admin_key = httpd.keys.create("admin", "admin")

        def check(path):
            status, _, hdrs = login(httpd)
            assert status == 200
            cookie = hdrs["Set-Cookie"].split(";", 1)[0]
            n = {"c": 0}

            def flaky(*args, **kwargs):
                n["c"] += 1
                return real(*args, **kwargs) if n["c"] <= 1 else None

            monkeypatch.setattr(webauth_mod, "resolve", flaky)
            status, _, body = call(httpd, path, headers={"Cookie": cookie})
            assert status == 401
            assert json.loads(body) == {"error": "login required"}
            monkeypatch.setattr(webauth_mod, "resolve", real)

        check("/api/server/session")
        check("/api/server/keys")


def test_login_demoted_admin_key_message(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="admin")
        _, secret = httpd.keys.create("alice-admin", "admin", user="alice")
        httpd.users.add("bob", role="admin")
        httpd.users.set_role("alice", "member")
        status, payload, hdrs = login(httpd, secret)
        assert status == 403
        assert payload == {"error": "this key cannot sign in"}
        assert "Set-Cookie" not in hdrs


def test_migrate_refuses_v2_non_admin_users(tmp_path, caplog):
    source = tmp_path / 'source'
    source.mkdir()
    keys = KeyStore(source)
    keys.create('k1')
    UserStore(source).add('bob')
    keys.create('k2', user='bob')
    data = tmp_path / 'data'
    prepare_data_dir(data)
    path = data / 'server' / 'keys.json'
    path.write_text(json.dumps(key_object(keys)))
    before = path.read_bytes()
    assert not migrate(data)
    assert 'non-admin users' in caplog.text
    assert not (data / 'server' / 'users.json').exists()
    assert path.read_bytes() == before


def test_promote_viewer_restores_writes(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("view", role="viewer")
        _, secret = httpd.keys.create("vkey", user="view")
        assert put_meta(httpd, key=secret)[0] == 403
        httpd.users.set_role("view", "member")
        assert put_meta(httpd, key=secret)[0] == 200
        remote = httpd.run_index.as_remote("run-1")
        assert remote == {"owner": "view", "owner_key": "vkey"}


def test_http_append_commit_second_key_same_user(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="member")
        _, first = httpd.keys.create("one", user="alice")
        _, second = httpd.keys.create("two", user="alice")
        body = json.dumps({"run_id": "run-1", "status": "running"}).encode()
        status, _, _ = raw(
            httpd, "PUT", "/v2/runs/run-1/files/meta.json",
            [("Authorization", "Bearer " + first), ("Content-Length", str(len(body)))],
            body)
        assert status == 200
        events = b"{}\n"
        status, _, _ = raw(
            httpd, "POST", "/v2/runs/run-1/append/events.jsonl?offset=0",
            [("Authorization", "Bearer " + second), ("Content-Length", str(len(events)))],
            events)
        assert status == 200
        size = (httpd.root / "runs" / "run-1" / "meta.json").stat().st_size
        event_size = (httpd.root / "runs" / "run-1" / "events.jsonl").stat().st_size
        commit = json.dumps({
            "status": "running", "fingerprint": "a" * 64,
            "sizes": {"meta.json": size, "events.jsonl": event_size},
        }).encode()
        status, _, _ = raw(
            httpd, "POST", "/v2/runs/run-1/commit",
            [("Authorization", "Bearer " + second), ("Content-Length", str(len(commit)))],
            commit)
        assert status == 200


def test_migrate_interrupted_partial_remote_rewrite(tmp_path):
    data = tmp_path / "data"
    prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["alpha", "beta"])
    write_run(data / "store", "run-a", "admin", extra={"owner_key": "alpha"})
    write_run(data / "store", "run-b", "beta")
    assert migrate(data) is True
    from agenticml.server.db import for_server_dir
    from agenticml.server.db.repos import RunRepo
    index = RunRepo(for_server_dir(data / "server", create=False))
    assert index.as_remote("run-a") == {"owner": "admin", "owner_key": "alpha"}
    assert index.as_remote("run-b") == {"owner": "admin", "owner_key": "beta"}
    assert imported_remote(data / "store" / "runs" / "run-a")[0] == {
        "owner": "admin", "owner_key": "alpha"}
    assert imported_remote(data / "store" / "runs" / "run-b")[0] == {
        "owner": "admin", "owner_key": "beta"}
    keys_obj = key_object(KeyStore(data / "server"))
    assert json.loads((data / "server" / "keys.json.2x").read_text())["version"] == 2
    assert {row["name"]: row["user"] for row in keys_obj["keys"]} == {
        "alpha": "admin", "beta": "admin"}


def test_env_key_dashboard_post_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", env_key="e" * 32) as httpd:
        status, _, hdrs = login(httpd, "e" * 32)
        assert status == 200
        cookie = hdrs["Set-Cookie"]
        status, _, body, _ = authed(
            httpd, "/api/server/keys", "POST",
            json.dumps({"name": "box", "scope": "sync"}).encode(),
            cookie=cookie)
        assert status == 201
        payload = json.loads(body)
        assert payload["record"]["created_by"] == "env"
        assert payload["record"]["user"] == "admin"
        record = next(item for item in httpd.keys.list() if item.name == "box")
        assert record.user == "admin" and record.created_by == "env"

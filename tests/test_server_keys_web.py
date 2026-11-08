"""First-admin setup and admin key lifecycle over the real HTTP server."""
from server_db_helpers import key_object, user_object
from agenticml.server.users import UserStore
from agenticml.server.db import DatabaseError
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import http.client
import json
import logging
import os
import socket
import stat
import threading

import pytest

from agenticml import cli
from agenticml.server import keyspage
from agenticml.server.app import make_server, prepare_data_dir
from agenticml.server.keys import (
    KeyNameTaken, KeyStore, KeyStoreUnavailable, LastAdminError, SetupClosed, validate_name,
)
from agenticml.server.users import UserNameTaken
from agenticml.server.webauth import WebAuth
from agenticml.store import RunReader, RunWriter
from agenticml.ui import server as ui_server
from server_db_helpers import default_workspace_id


# On Python 3.12+, the C json scanner's limit is separate from sys.getrecursionlimit().
DEEP_JSON_DEPTH = 100_000


@contextmanager
def running(data, env_key=None, public_url=None):
    httpd = make_server(data, port=0, env_key=env_key, public_url=public_url)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
        assert not thread.is_alive()


def call(server, path, method="GET", body=None, headers=None):
    conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def csrf(server, **extra):
    headers = {"Origin": f"http://127.0.0.1:{server.server_address[1]}",
               "X-AgenticML-Request": "1", "Content-Type": "application/json"}
    headers.update(extra)
    return headers


def login(server, key):
    status, headers, _ = call(server, "/login", "POST", json.dumps({"key": key}), csrf(server))
    assert status == 200
    return headers["Set-Cookie"].split(";", 1)[0]


def api(server, path="/api/server/keys", method="GET", payload=None, cookie=None, extra=None):
    headers = csrf(server) if method in ("POST", "DELETE") else {}
    headers["Cookie"] = server.cookie if cookie is None else cookie
    headers.update(extra or {})
    status, hdrs, body = call(
        server, path, method, None if payload is None else json.dumps(payload), headers)
    return status, hdrs, json.loads(body)


SETUP_PASSWORD = "password1234"


def setup(server, payload=None, headers=None):
    if payload is None:
        payload = {"token": server.web.setup_token, "password": SETUP_PASSWORD}
    elif isinstance(payload, dict) and "password" not in payload:
        payload = {**payload, "password": SETUP_PASSWORD}
    status, hdrs, body = call(server, "/setup", "POST", json.dumps(payload),
                              csrf(server) if headers is None else headers)
    return status, hdrs, json.loads(body)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    prepare_data_dir(data)
    _, key = KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).create("admin", "admin")
    with running(data) as httpd:
        httpd.admin_key = key
        httpd.cookie = login(httpd, key)
        yield httpd


def test_setup_startup_page_and_single_use(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    with running(tmp_path / "data", public_url="https://ml.example.com/") as httpd:
        token = httpd.web.setup_token
        warnings = [record for record in caplog.records if token in record.getMessage()]
        assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
        startup_warning = warnings[0]
        assert startup_warning.getMessage() == (
            f"No admin user yet. Open https://ml.example.com/setup?token={token} "
            "to create one, or run: agenticml server users add admin --role admin")
        httpd.verbose = True
        for query in ("", "?token=wrong", f"?token={token}&token={token}", "?token=%C3%A4"):
            status, _, body = call(httpd, "/setup" + query)
            assert status == 404 and json.loads(body) == {"error": "not found"}
        status, hdrs, page = call(httpd, "/setup?token=" + token)
        assert status == 200 and b'/static/setup.js' in page
        assert hdrs["Cache-Control"] == "no-store"
        assert hdrs["Referrer-Policy"] == "no-referrer"
        assert token.encode() not in page
        status, _, script = call(httpd, "/static/setup.js")
        assert status == 200 and b'history.replaceState(null, "", "/setup")' in script
        assert b"innerHTML" not in script
        status, hdrs, payload = setup(httpd)
        assert status == 201 and payload == {"ok": True, "user": "admin", "role": "admin"}
        assert hdrs["Cache-Control"] == "no-store"
        assert "Secure" in hdrs["Set-Cookie"] and hdrs["Set-Cookie"].startswith("__Host-")
        admin = httpd.users.get("admin")
        assert admin is not None and admin.role == "owner" and admin.password is not None
        cookie = hdrs["Set-Cookie"].split(";", 1)[0]
        assert call(httpd, "/api/server/session", headers={"Cookie": cookie})[0] == 200
        assert setup(httpd, {"token": token})[0] == 404
        assert call(httpd, "/setup?token=" + token)[0] == 404
        assert all(token not in record.getMessage()
                   for record in caplog.records if record is not startup_warning)


@pytest.mark.parametrize("failure,expected,message", [
    ("csrf", 403, None), ("type", 415, None), ("json", 400, "invalid JSON body"),
    ("non-object", 400, "invalid JSON body"), ("token-type", 400, "invalid setup body"),
    ("name-type", 400, "invalid setup body"), ("name", 400, "invalid or reserved key name"),
    ("taken", 409, None), ("large", 413, None),
])
def test_setup_validation_does_not_consume(tmp_path, failure, expected, message):
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        payload = {"token": token, "password": SETUP_PASSWORD}
        headers = csrf(httpd)
        if failure == "csrf":
            del headers["Origin"]
        elif failure == "type":
            headers["Content-Type"] = "text/plain"
        elif failure == "token-type":
            payload["token"] = 5
        elif failure == "name-type":
            payload["name"] = 5
        elif failure == "name":
            payload["name"] = ".."
        elif failure == "taken":
            httpd.users.add("taken")
            payload["name"] = "taken"
        elif failure == "non-object":
            payload = []
        body = b"{" if failure == "json" else json.dumps(payload).encode()
        if failure == "large":
            body = b"x" * 4097
        status, _, response = call(httpd, "/setup", "POST", body, headers)
        assert status == expected
        if message is not None:
            assert json.loads(response) == {"error": message}
        assert not httpd.web._setup_used
        assert setup(httpd)[0] == 201


def test_setup_concurrent(tmp_path):
    with running(tmp_path / "race") as httpd:
        barrier = threading.Barrier(2)
        def submit(_):
            barrier.wait(timeout=5)
            return setup(httpd)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(submit, range(2))) == [201, 404]
        assert [user.name for user in httpd.users.list()] == ["admin"]


@pytest.mark.parametrize("first_method", ["GET", "POST"])
def test_setup_cli_disarm_is_permanent(tmp_path, monkeypatch, capsys, first_method):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "cli"
    with running(data) as httpd:
        token = httpd.web.setup_token
        assert cli.main(["server", "keys", "create", "--data", str(data),
                         "--name", "operator", "--scope", "admin"]) == 0
        capsys.readouterr()
        if first_method == "GET":
            status, _, body = call(httpd, "/setup?token=" + token)
            assert json.loads(body) == {"error": "not found"}
        else:
            status, _, body = setup(httpd, {"token": token})
            assert body == {"error": "not found"}
        assert status == 404
        assert httpd.web._setup_used and httpd.web.setup_token is None
        assert cli.main(["server", "keys", "revoke", "operator", "--data", str(data)]) == 0
        assert not httpd.keys.has_admin()
        status, _, body = call(httpd, "/setup?token=" + token)
        assert status == 404 and json.loads(body) == {"error": "not found"}
        status, _, body = setup(httpd, {"token": token})
        assert status == 404 and body == {"error": "not found"}
        assert not httpd.keys.list()


def test_setup_closed_marker_survives_cli_revoke_without_setup_hit(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    with running(data) as httpd:
        token = httpd.web.setup_token
        assert cli.main(["server", "keys", "create", "--data", str(data),
                         "--name", "operator", "--scope", "admin"]) == 0
        capsys.readouterr()
        assert cli.main(["server", "keys", "revoke", "operator", "--data", str(data)]) == 0
        capsys.readouterr()
        assert not httpd.keys.has_admin()
        status, _, body = call(httpd, "/setup?token=" + token)
        assert status == 404 and json.loads(body) == {"error": "not found"}
        status, _, body = setup(httpd, {"token": token})
        assert status == 404 and body == {"error": "not found"}


def test_setup_restart_clears_stale_marker(tmp_path):
    data = tmp_path / "data"
    prepare_data_dir(data)
    marker = data / "server" / "setup-closed"
    marker.write_bytes(b"")
    with running(data) as httpd:
        token = httpd.web.setup_token
        assert token is not None
        assert not httpd.keys.setup_closed()
        status, _, payload = setup(httpd)
        assert status == 201 and payload == {"ok": True, "user": "admin", "role": "admin"}


def test_setup_closed_marker_mode_and_no_follow(tmp_path):
    data = tmp_path / 'data'
    prepare_data_dir(data)
    target = tmp_path / 'outside'
    target.write_bytes(b'secret')
    marker = data / 'server' / 'setup-closed'
    marker.symlink_to(target)
    keys = KeyStore(data / 'server', workspace_id=default_workspace_id(data / 'server'))
    assert keys.setup_closed()
    assert (data / 'server' / 'setup-closed.2x').is_symlink()
    keys.create('admin', 'admin')
    assert keys.setup_closed()
    assert stat.S_IMODE(keys.db.path.stat().st_mode) == 0o600
    assert target.read_bytes() == b'secret'


def test_setup_marker_write_failure_keeps_token(tmp_path, monkeypatch):
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        before = httpd.keys.list()

        def fail(*_args, **_kwargs):
            raise OSError("disk full")

        with monkeypatch.context() as patch:
            patch.setattr(httpd.keys, "_write_setup_closed", fail)
            status, _, body = setup(httpd, {"token": token})
            assert status == 503 and body == {
                "error": "setup failed; retry or use agenticml server users"}
        assert (httpd.keys.list()) == before
        assert not httpd.keys.has_admin()
        assert httpd.users.get("admin") is None
        status, _, payload = setup(httpd, {"token": token})
        assert status == 201 and payload == {"ok": True, "user": "admin", "role": "admin"}


def test_sync_create_writes_no_marker(tmp_path):
    prepare_data_dir(tmp_path)
    keys = KeyStore(tmp_path / 'server', workspace_id=default_workspace_id(tmp_path / 'server'))
    keys.create('box')
    assert not keys.setup_closed()


def test_api_admin_create_writes_marker(server):
    server.keys.settings.delete('setup_closed')
    assert api(server, method='POST', payload={'name': 'second', 'scope': 'admin'})[0] == 201
    assert server.keys.setup_closed()


def test_keys_lock_symlink_does_not_crash_startup(tmp_path, caplog):
    data = tmp_path / 'data'
    prepare_data_dir(data)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'unchanged')
    (data / 'server' / 'agenticml.db').symlink_to(outside)
    with pytest.raises(DatabaseError, match='not a regular database file'):
        make_server(data, port=0)
    assert outside.read_bytes() == b'unchanged'
    assert '/setup?token=' not in caplog.text


@pytest.mark.parametrize('reader', [False, True])
@pytest.mark.parametrize('operation', ['startup', 'create'])
def test_keys_lock_fifo_fails_promptly(tmp_path, caplog, operation, reader):
    data = tmp_path / 'data'
    prepare_data_dir(data)
    path = data / 'server' / 'agenticml.db'
    os.mkfifo(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK) if reader else None
    errors = []
    def attempt():
        try:
            make_server(data, port=0) if operation == 'startup' else KeyStore(data / 'server', workspace_id=default_workspace_id(data / 'server'))
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=attempt, daemon=True)
    try:
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], DatabaseError)
    finally:
        if fd is not None: os.close(fd)


@pytest.mark.skipif(not os.path.isdir('/proc/self/fd'), reason='requires procfs fd counts')
def test_keys_lock_fifo_with_reader_does_not_leak_fd(tmp_path):
    prepare_data_dir(tmp_path)
    os.mkfifo(tmp_path / 'server' / 'agenticml.db')
    before = len(os.listdir('/proc/self/fd'))
    with pytest.raises(DatabaseError):
        KeyStore(tmp_path / 'server', workspace_id=default_workspace_id(tmp_path / 'server'))
    assert len(os.listdir('/proc/self/fd')) == before


@pytest.mark.skipif(not os.path.isdir('/proc/self/fd'), reason='requires procfs fd counts')
@pytest.mark.parametrize('error', [OSError, KeyboardInterrupt])
def test_keys_lock_fdopen_failure_does_not_leak_fd(tmp_path, monkeypatch, error):
    from agenticml.server.db import sqlite
    prepare_data_dir(tmp_path)
    def fail(*args, **kwargs): raise error('connect failed')
    monkeypatch.setattr(sqlite.sqlite3, 'connect', fail)
    before = len(os.listdir('/proc/self/fd'))
    with pytest.raises(DatabaseError if error is OSError else error):
        KeyStore(tmp_path / 'server', workspace_id=default_workspace_id(tmp_path / 'server'))
    assert len(os.listdir('/proc/self/fd')) == before


def test_undeletable_marker_at_startup_disarms_setup(tmp_path, caplog, monkeypatch):
    from agenticml.server.db.repos import SettingsRepo
    prepare_data_dir(tmp_path)
    keys = KeyStore(tmp_path / 'server', workspace_id=default_workspace_id(tmp_path / 'server'))
    keys._write_setup_closed()
    def fail(*args): raise DatabaseError('private failure')
    monkeypatch.setattr(SettingsRepo, 'delete', fail)
    with running(tmp_path) as httpd:
        assert httpd.web.setup_token is None
        assert '/setup?token=' not in caplog.text


def test_clear_setup_closed_removes_symlink_only(tmp_path):
    prepare_data_dir(tmp_path)
    target = tmp_path / 'outside'
    target.write_bytes(b'keep')
    (tmp_path / 'server' / 'setup-closed').symlink_to(target)
    keys = KeyStore(tmp_path / 'server', workspace_id=default_workspace_id(tmp_path / 'server'))
    keys.clear_setup_closed()
    assert not keys.setup_closed()
    with pytest.raises(FileNotFoundError): keys.clear_setup_closed()
    assert target.read_bytes() == b'keep'


def test_setup_closed_true_on_non_enoent(tmp_path, monkeypatch):
    prepare_data_dir(tmp_path)
    keys = KeyStore(tmp_path / 'server', workspace_id=default_workspace_id(tmp_path / 'server'))
    def fail(*args): raise DatabaseError('private failure')
    monkeypatch.setattr(keys.settings, 'get', fail)
    assert keys.setup_closed() is True


def test_create_only_if_no_admin_closed_after_external_revoke(tmp_path):
    prepare_data_dir(tmp_path / "data")
    keys = KeyStore(tmp_path / "data" / "server", workspace_id=default_workspace_id(tmp_path / "data" / "server"))
    external = KeyStore(tmp_path / "data" / "server", workspace_id=default_workspace_id(tmp_path / "data" / "server"))
    external.create("operator", "admin")
    external.revoke("operator")
    before = keys.list()
    with pytest.raises(SetupClosed):
        keys.create("second", "admin", only_if_no_admin=True)
    assert (keys.list()) == before


def test_arm_setup_waits_on_store_lock(tmp_path):
    prepare_data_dir(tmp_path)
    keys = KeyStore(tmp_path / 'server', workspace_id=default_workspace_id(tmp_path / 'server'))
    keys._write_setup_closed()
    web = WebAuth(keys)
    holding, release = threading.Event(), threading.Event()
    def hold():
        with keys.db.transaction(write=True):
            holding.set()
            assert release.wait(timeout=5)
    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(timeout=5)
    worker = threading.Thread(target=lambda: web.arm_setup('http://127.0.0.1'))
    worker.start()
    worker.join(timeout=.3)
    assert worker.is_alive() and web.setup_token is None
    assert keys.setup_closed()
    release.set()
    worker.join(timeout=5)
    holder.join(timeout=5)
    assert web.setup_token is not None and not keys.setup_closed()


def test_prepare_setup_holds_lock_across_clear_and_check(tmp_path, monkeypatch):
    prepare_data_dir(tmp_path)
    keys = KeyStore(tmp_path / 'server', workspace_id=default_workspace_id(tmp_path / 'server'))
    keys._write_setup_closed()
    seen = []
    real_clear = keys.repo.settings.delete
    real_read = keys.repo.list
    def clear(name, tx=None):
        seen.append(tx)
        return real_clear(name, tx)
    def read(tx=None):
        seen.append(tx)
        return real_read(tx)
    monkeypatch.setattr(keys.repo.settings, 'delete', clear)
    monkeypatch.setattr(keys.repo, 'list', read)
    assert keys.prepare_setup()
    assert len(seen) == 2 and seen[0] is not None and seen[0] is seen[1]
    assert not keys.setup_closed()
    keys.create('operator', 'admin')
    assert not keys.prepare_setup()


def test_prepare_setup_false_with_env_key(tmp_path):
    prepare_data_dir(tmp_path / "data")
    keys = KeyStore(tmp_path / "data" / "server", env_key="e" * 32, workspace_id=default_workspace_id(tmp_path / "data" / "server"))
    assert keys.prepare_setup() is False


@pytest.mark.parametrize('nested', [False, True])
def test_setup_corrupt_at_startup_stays_disabled(tmp_path, caplog, nested):
    prepare_data_dir(tmp_path)
    path = tmp_path / 'server' / 'keys.json'
    contents = '['*DEEP_JSON_DEPTH+'0'+']'*DEEP_JSON_DEPTH if nested else 'private corrupt contents'
    path.write_text(contents)
    with pytest.raises(DatabaseError, match='could not import keys.json'):
        make_server(tmp_path, port=0)
    assert contents not in caplog.text and '/setup?token=' not in caplog.text
    path.unlink()
    with running(tmp_path) as httpd: assert httpd.web.setup_token is not None


@pytest.mark.parametrize('field', ['created_at', 'last_used_at'])
def test_timestamp_overflow_fails_closed_and_disarms_setup(tmp_path, caplog, field):
    directory = tmp_path / 'source'
    directory.mkdir()
    keys = KeyStore(directory, workspace_id=default_workspace_id(directory))
    record, secret = keys.create('admin', 'admin')
    obj = key_object(keys)
    obj['keys'][0][field] = '0001-01-01T00:00:00+01:00'
    data = tmp_path / 'legacy'
    prepare_data_dir(data)
    (data / 'server' / 'users.json').write_text(json.dumps(user_object(UserStore(directory, workspace_id=default_workspace_id(directory)))))
    (data / 'server' / 'keys.json').write_text(json.dumps(obj))
    with pytest.raises(DatabaseError, match='could not import keys.json'):
        make_server(data, port=0)
    assert secret not in caplog.text and record.sha256 not in caplog.text


@pytest.mark.parametrize("marker", [True, False], ids=["with-marker", "markerless"])
def test_setup_strict_create_closes_after_false_negative(tmp_path, monkeypatch, marker):
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        # A separate store models the CLI writing while the server runs.
        external = KeyStore(httpd.keys.server_dir, workspace_id=default_workspace_id(httpd.keys.server_dir))
        external.create("operator", "admin")
        if not marker:
            external.clear_setup_closed()
        before = httpd.keys.list()
        with monkeypatch.context() as patch:
            patch.setattr(httpd.keys, "has_admin", lambda: False)
            status, _, body = setup(httpd, {"token": token, "name": "second"})
            assert status == 404 and body == {"error": "not found"}
        assert httpd.keys.list() == before
        assert [record.name for record in external.list()] == ["operator"]
        assert httpd.web._setup_used and httpd.web.setup_token is None
        external.revoke("operator")
        assert call(httpd, "/setup?token=" + token)[0] == 404
        assert setup(httpd, {"token": token})[0] == 404


@pytest.mark.parametrize("env_key,marker", [
    (None, True), (None, False), ("e" * 32, False),
], ids=["file-admin", "file-admin-markerless", "env-admin"])
def test_store_first_admin_guard(tmp_path, env_key, marker):
    data = tmp_path / "data"
    prepare_data_dir(data)
    keys = KeyStore(data / "server", env_key=env_key, workspace_id=default_workspace_id(data / "server"))
    if env_key is None:
        keys.create("sync")
        keys.create("operator", "admin", only_if_no_admin=True)
        if not marker:
            keys.clear_setup_closed()
    before = keys.list()
    with pytest.raises(SetupClosed):
        keys.create("second", "admin", only_if_no_admin=True)
    assert (keys.list()) == before
    # Ordinary CLI/API creates still permit another admin.
    keys.create("second", "admin")


def test_setup_unarmed_and_restart(tmp_path):
    data = tmp_path / "data"
    with running(data) as httpd:
        old = httpd.web.setup_token
    with running(data) as httpd:
        assert old != httpd.web.setup_token
        assert call(httpd, "/setup?token=" + old)[0] == 404
        httpd.keys.create("admin", "admin")
    for env in (None, "e" * 32):
        with running(data if env is None else tmp_path / "env", env_key=env) as httpd:
            assert httpd.web.setup_token is None
            assert call(httpd, "/setup?token=anything")[0] == 404
            assert setup(httpd, {"token": "anything"})[0] == 404


@pytest.mark.parametrize("error", [KeyStoreUnavailable, OSError])
def test_setup_create_store_error_keeps_token(tmp_path, monkeypatch, error):
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token

        def failed(*_args, **_kwargs):
            raise error("private failure")

        with monkeypatch.context() as patch:
            patch.setattr(httpd.users, "add", failed)
            status, _, body = setup(httpd, {"token": token})
            assert status == 503 and body == {"error": "key store unavailable"}
            assert not httpd.web._setup_used
        status, _, payload = setup(httpd, {"token": token})
        assert status == 201 and payload == {"ok": True, "user": "admin", "role": "admin"}


def test_setup_create_name_taken_keeps_token(tmp_path, monkeypatch):
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token

        def failed(*_args, **_kwargs):
            raise UserNameTaken("user name already exists")

        with monkeypatch.context() as patch:
            patch.setattr(httpd.users, "add", failed)
            status, _, body = setup(httpd, {"token": token, "name": "admin"})
            assert status == 409 and body == {"error": "user name already exists"}
            assert not httpd.web._setup_used
        status, _, payload = setup(httpd, {"token": token, "name": "operator"})
        assert status == 201 and payload == {"ok": True, "user": "operator", "role": "admin"}


def test_setup_create_value_error_is_private(tmp_path, monkeypatch):
    with running(tmp_path / "data") as httpd:
        def failed(*args, **kwargs):
            raise ValueError("secret detail aml_x")
        monkeypatch.setattr(httpd.users.repo, "save", failed)
        status, _, body = call(httpd, "/setup", "POST",
                               json.dumps({"token": httpd.web.setup_token,
                                           "password": SETUP_PASSWORD}), csrf(httpd))
        assert status == 400
        assert b"secret detail aml_x" not in body
        assert json.loads(body) == {"error": "invalid request"}


def test_setup_short_body_does_not_consume_token(tmp_path):
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        assert token is not None and not httpd.keys.has_admin()
        body = json.dumps({"token": token}).encode()
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=5)
        try:
            conn.request("POST", "/setup", body=body[:-1], headers=csrf(
                httpd, **{"Content-Length": str(len(body))}))
            conn.sock.shutdown(socket.SHUT_WR)
            response = conn.getresponse()
            assert response.status == 400
            assert response.getheader("Connection") == "close"
            assert json.loads(response.read()) == {"error": "short body"}
        finally:
            conn.close()
        assert not httpd.web._setup_used
        assert not httpd.keys.has_admin()
        assert setup(httpd, {"token": token})[0] == 201


def test_list_public_fields_runs_order_and_env(tmp_path):
    with running(tmp_path / "data", env_key="e" * 32) as httpd:
        httpd.cookie = login(httpd, "e" * 32)
        first, _ = httpd.keys.create("z-box")
        httpd.keys.create("a-box")
        # Fix timestamps so the secondary name ordering is exercised.
        records = [replace(record, created_at=first.created_at) for record in httpd.keys.list() if record.name != "env"]
        with httpd.keys.db.transaction(write=True) as tx:
            for record in records:
                tx.execute('UPDATE keys SET created_at=? WHERE name=?', (record.created_at, record.name))
        from server_db_helpers import insert_run
        for name, owner in (("one", "z-box"), ("two", "z-box"), ("three", "a-box")):
            insert_run(httpd.run_index, name, owner_user="admin", owner_key=owner)
        status, _, payload = api(httpd)
        assert status == 200
        assert payload["host"] == f"http://127.0.0.1:{httpd.server_address[1]}"
        assert payload["host_source"] == "request"
        rows = payload["keys"]
        assert [row["name"] for row in rows] == ["a-box", "z-box", "env"]
        fields = {"name", "scope", "user", "prefix", "created_at", "created_by", "last_used_at",
                  "runs", "revocable", "active"}
        assert all(set(row) == fields for row in rows)
        assert [row["runs"] for row in rows] == [1, 2, 0]
        assert [row["revocable"] for row in rows] == [True, True, False]
        assert all(record.sha256 not in json.dumps(payload) for record in httpd.keys.list())


def test_run_count_scan_cap(server, monkeypatch):
    from server_db_helpers import insert_run
    for name in ("a", "b", "c"):
        insert_run(server.run_index, name, owner_user="admin", owner_key="admin")
    assert api(server)[2]["keys"][0]["runs"] == 3


@pytest.mark.parametrize("state", ["missing", "file", "unreadable"])
def test_run_counts_unavailable(server, state):
    if state == "unreadable" and os.geteuid() == 0:
        pytest.skip("root can read directories without read permission")
    server.keys.create("box")
    runs = server.root / "runs"
    if runs.exists():
        runs.rmdir()
    if state == "file":
        runs.write_text("not a directory")
    elif state == "unreadable":
        runs.mkdir(mode=0o000)
    try:
        status, _, payload = api(server)
        assert status == 200
        assert len(payload["keys"]) == 2
        assert all(row["runs"] == 0 for row in payload["keys"])
    finally:
        if state == "unreadable":
            runs.chmod(0o700)


@pytest.mark.parametrize("scope", ["sync", "admin"])
def test_create_once_and_public_host(server, scope, caplog):
    caplog.set_level("INFO")
    status, headers, payload = api(server, method="POST", payload={"name": "box", "scope": scope})
    assert status == 201 and headers["Cache-Control"] == "no-store"
    record = server.keys.verify(payload["key"])
    assert payload["record"] == record.to_public()
    assert record.scope == scope and record.created_by == "admin"
    listing = json.dumps(api(server)[2])
    assert payload["key"] not in listing + caplog.text
    assert record.sha256 not in listing + caplog.text
    assert "created key box scope " + scope in caplog.text
    assert api(server, method="POST", payload={"name": "box", "scope": scope})[0] == 409
    bearer = {"Authorization": "Bearer " + payload["key"]}
    assert call(server, "/v2/whoami", headers=bearer)[0] == 200
    if scope == "sync":
        assert call(server, "/login", "POST", json.dumps({"key": payload["key"]}), csrf(server))[0] == 403


def test_api_public_url(tmp_path):
    with running(tmp_path / "data", env_key="e" * 32, public_url="https://ml.example.com") as httpd:
        httpd.cookie = login(httpd, "e" * 32)
        for method, payload in (("GET", None), ("POST", {"name": "box", "scope": "sync"})):
            response = api(httpd, method=method, payload=payload)[2]
            assert response["host"] == "https://ml.example.com"
            assert response["host_source"] == "public_url"


@pytest.mark.parametrize("payload,message", [
    ({"name": name, "scope": "sync"}, "invalid or reserved key name")
    for name in ("", ".", "..", "env", "a/b", "ä", "x" * 65, None)
] + [({"name": "box", "scope": scope}, "invalid key scope") for scope in ("viewer", 1)]
  + [({}, "invalid or reserved key name"), ([], "invalid JSON body"), (None, "invalid JSON body")])
def test_create_invalid(server, payload, message):
    # Explicit JSON null still needs a body.
    status, _, body = call(server, "/api/server/keys", "POST", json.dumps(payload),
                           csrf(server, Cookie=server.cookie))
    assert status == 400 and json.loads(body) == {"error": message}


def test_create_value_error_is_private(server, monkeypatch):
    def failed(*args, **kwargs):
        raise ValueError("secret detail aml_x")
    monkeypatch.setattr(server.keys.repo, "insert", failed)
    status, _, body = call(server, "/api/server/keys", "POST",
                           json.dumps({"name": "box", "scope": "sync"}),
                           csrf(server, Cookie=server.cookie))
    assert status == 400
    assert b"secret detail aml_x" not in body
    assert json.loads(body) == {"error": "invalid request"}


def test_create_body_protocol(server):
    headers = csrf(server, Cookie=server.cookie)
    assert call(server, "/api/server/keys", "POST", b"{}", {**headers, "Content-Type": "text/plain"})[0] == 415
    assert call(server, "/api/server/keys", "POST", b"x" * 4097, headers)[0] == 413
    assert call(server, "/api/server/keys", "POST", b"", {**headers, "Transfer-Encoding": "chunked"})[0] == 411
    status, _, body = call(server, "/api/server/keys", "POST", b"{", headers)
    assert status == 400 and json.loads(body) == {"error": "invalid JSON body"}


@pytest.mark.parametrize("path", ["/api/server/keys", "/setup"])
def test_validation_short_body_closes_connection(server, path):
    conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    try:
        conn.request("POST", path, body=b"{", headers=csrf(
            server, Cookie=server.cookie, **{"Content-Length": "10"}))
        conn.sock.shutdown(socket.SHUT_WR)
        response = conn.getresponse()
        assert response.status == 400
        assert response.getheader("Connection") == "close"
        assert json.loads(response.read()) == {"error": "short body"}
    finally:
        conn.close()


def test_revoke_keys_sessions_and_logs(server, caplog):
    caplog.set_level(logging.DEBUG)
    server.verbose = True
    sync_record, sync = server.keys.create("box")
    admin_record, admin = server.keys.create("second", "admin")
    cookie = login(server, admin)
    other_cookie = login(server, admin)
    assert api(server, "/api/server/keys/box", "DELETE")[2] == {"revoked": "box", "signed_out": False}
    assert call(server, "/v2/whoami", headers={"Authorization": "Bearer " + sync})[0] == 401
    assert api(server, "/api/server/keys/second", "DELETE")[0] == 200
    assert not any(entry["name"] == "second" for entry in server.web.sessions.list().values())
    for old in (cookie, other_cookie):
        assert api(server, cookie=old)[0] == 401
    _, new_key = server.keys.create("second", "admin")
    assert api(server, cookie=login(server, new_key))[0] == 200
    status, hdrs, payload = api(server, "/api/server/keys/admin", "DELETE")
    assert status == 200 and payload == {"revoked": "admin", "signed_out": True}
    assert "Max-Age=0" in hdrs["Set-Cookie"] and hdrs["Set-Cookie"].startswith("aml_session=;")
    assert api(server)[0] == 401
    secrets = (sync, sync_record.sha256, admin, admin_record.sha256,
               server.admin_key, hashlib.sha256(server.admin_key.encode()).hexdigest())
    assert all(secret not in record.getMessage() for record in caplog.records for secret in secrets)


def test_recreated_name_does_not_revive_session(server):
    record, key = server.keys.create("second", "admin")
    cookie = login(server, key)
    digest = hashlib.sha256(cookie.split("=", 1)[1].encode("ascii")).hexdigest()
    assert server.keys.revoke("second").sha256 == record.sha256
    _, new_key = server.keys.create("second", "admin")
    # Direct store mutations leave the session intact until the request gate checks its hash.
    assert server.web.sessions.list()[digest]["name"] == "second"
    assert server.web.sessions.list()[digest]["sha256"] == record.sha256
    assert api(server, cookie=cookie)[0] == 401
    assert digest not in server.web.sessions.list()
    assert api(server, cookie=login(server, new_key))[0] == 200


def test_drop_sessions_matches_name_and_hash(server):
    old = server.keys.verify(server.admin_key)
    other = replace(old, name="other")
    newer = replace(old, sha256="a" * 64)
    for record in (old, other, newer):
        server.web._create_session(record)
    server.web.drop_sessions(old.name, old.sha256)
    assert {(entry["name"], entry["sha256"]) for entry in server.web.sessions.list().values()} == {
        (other.name, other.sha256), (newer.name, newer.sha256)}


def test_last_admin_guard_and_cli_recovery(server, monkeypatch):
    before = server.keys.list()
    status, _, payload = api(server, "/api/server/keys/admin", "DELETE")
    assert status == 409 and payload == {"error": "last admin key; create another admin key first, or use the CLI"}
    assert server.keys.list() == before
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    assert cli.main(["server", "keys", "revoke", "admin", "--data", str(server.data_dir)]) == 0
    assert not server.keys.has_admin()


def test_env_allows_file_admin_revoke(tmp_path):
    with running(tmp_path / "data", env_key="e" * 32) as httpd:
        httpd.keys.create("admin", "admin")
        httpd.cookie = login(httpd, "e" * 32)
        assert api(httpd, "/api/server/keys/admin", "DELETE")[0] == 200
        status, _, payload = api(httpd, "/api/server/keys/env", "DELETE")
        assert status == 409 and payload == {"error": "environment key: remove AGENTICML_SERVER_KEY and restart"}


def test_concurrent_last_admin_revoke(server):
    _, key = server.keys.create("second", "admin")
    second = login(server, key)
    barrier = threading.Barrier(2)
    def revoke(args):
        name, cookie = args
        barrier.wait(timeout=5)
        return api(server, "/api/server/keys/" + name, "DELETE", cookie=cookie)[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(revoke, [("admin", server.cookie), ("second", second)])) == [200, 409]
    assert len(server.keys.list()) == 1 and server.keys.has_admin()


def test_api_gate_paths_and_bodyless_connections(server):
    status, _, body = call(server, "/api/server/keys", headers={
        "Authorization": "Bearer " + server.admin_key,
    })
    assert status == 401 and json.loads(body) == {"error": "login required"}
    for method in ("POST", "DELETE"):
        path = "/api/server/keys" + ("/box" if method == "DELETE" else "")
        status, _, body = call(server, path, method, b"{}", {"Cookie": server.cookie})
        assert status == 403 and json.loads(body)["error"] == "cross-site request refused"
        status, _, body = call(server, path, method, b"{}", csrf(server))
        assert status == 401 and json.loads(body) == {"error": "login required"}
    for name in ("%2e%2e", "%2e"):
        assert api(server, "/api/server/keys/" + name, "DELETE")[0] == 400
    for name in ("a%2Fb", "a/b", ""):
        assert api(server, "/api/server/keys/" + name, "DELETE")[0] == 404
    assert api(server, "/api/server/keys/missing", "DELETE")[0] == 404
    assert api(server, "/api/server/unknown")[0] == 404
    assert api(server, "/api/server/session", "POST")[0] == 405
    assert api(server, method="PATCH")[0] == 405
    assert api(server, "/api/server/keys/box")[0] == 405
    status, hdrs, _ = api(server, extra={"Content-Length": "3"})
    assert status == 200 and hdrs["Connection"] == "close"
    status, hdrs, _ = api(server, "/api/server/keys/missing", "DELETE", extra={"Transfer-Encoding": "chunked"})
    assert status == 404 and hdrs["Connection"] == "close"


@pytest.mark.parametrize("method,path,header,expected", [
    ("GET", "/setup?token={token}", "Content-Length", 200),
    ("GET", "/setup?token=wrong", "Content-Length", 404),
    ("GET", "/login", "Transfer-Encoding", 200),
    ("GET", "/", "Content-Length", 303),
    ("GET", "/static/app.css", "Content-Length", 200),
    ("GET", "/api/server/keys", "Content-Length", 200),
    ("HEAD", "/login", "Content-Length", 404),
    ("DELETE", "/api/server/keys/missing", "Transfer-Encoding", 404),
])
def test_server_bodyless_response_has_one_close_header(tmp_path, method, path, header, expected):
    with running(tmp_path / "data") as httpd:
        path = path.format(token=httpd.web.setup_token)
        headers = {"Connection": "keep-alive"}
        if path == "/" or path.startswith("/api/server/keys") or method == "HEAD":
            _, key = httpd.keys.create("admin", "admin")
            headers["Cookie"] = login(httpd, key)
        if method == "DELETE":
            headers.update(csrf(httpd))
        headers[header] = "3" if header == "Content-Length" else "chunked"
        body = b"bad" if header == "Content-Length" else b"3\r\nbad\r\n0\r\n\r\n"
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            assert response.status == expected
            assert [value for name, value in response.getheaders()
                    if name.lower() == "connection"] == ["close"]
            response.read()
            assert response.will_close
        finally:
            conn.close()


@pytest.mark.parametrize("method,path,expected,auth", [
    ("GET", "/healthz", 200, False),
    ("GET", "/v2/whoami", 200, True),
    ("PUT", "/v2/whoami", 405, False),
])
def test_ingest_bodyless_response_has_one_close_header(tmp_path, method, path, expected, auth):
    with running(tmp_path / "data") as httpd:
        headers = {"Connection": "keep-alive", "Content-Length": "3"}
        if auth:
            _, key = httpd.keys.create("box", "sync")
            headers["Authorization"] = "Bearer " + key
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=5)
        try:
            conn.request(method, path, body=b"bad", headers=headers)
            response = conn.getresponse()
            assert response.status == expected
            assert [value for name, value in response.getheaders()
                    if name.lower() == "connection"] == ["close"]
            response.read()
            assert response.will_close
        finally:
            conn.close()


def test_sync_key_cannot_reach_api(server):
    _, key = server.keys.create("box")
    for headers in ({"Authorization": "Bearer " + key},
                    {"Cookie": server.web._cookie_name() + "=" + key}):
        status, _, body = call(server, "/api/server/keys", headers=headers)
        assert status == 401 and json.loads(body) == {"error": "login required"}


@pytest.mark.parametrize('nested', [False, True])
def test_store_corruption_503_and_cli(tmp_path, capsys, monkeypatch, nested):
    from agenticml.server.db.repos import KeyRepo
    with running(tmp_path, env_key='e'*32) as httpd:
        httpd.cookie = login(httpd, 'e'*32)
        httpd.keys.create('box')
        before = httpd.keys.list()
        def fail(*args, **kwargs): raise DatabaseError('private failure')
        with monkeypatch.context() as patch:
            patch.setattr(KeyRepo, 'create', fail)
            patch.setattr(KeyRepo, 'revoke', fail)
            for method, path, payload in [('POST', '/api/server/keys', {'name':'new', 'scope':'sync'}),
                                          ('DELETE', '/api/server/keys/box', None)]:
                status, _, body = api(httpd, path, method, payload)
                assert status == 503 and body == {'error':'key store unavailable'}
            for action in (['create','--name','new'], ['revoke','box']):
                assert cli.main(['server','keys',*action,'--data',str(tmp_path)]) == 1
                assert 'private failure' not in capsys.readouterr().err
        assert httpd.keys.list() == before
        monkeypatch.setattr(httpd.keys, 'current', fail)
        assert api(httpd)[0] == 503


@pytest.mark.parametrize('error', [PermissionError, ValueError, TypeError, RecursionError, KeyStoreUnavailable])
def test_touch_read_failure_is_advisory(server, monkeypatch, error):
    record, secret = server.keys.create('box')
    def unreadable(*args): raise error('private failure')
    monkeypatch.setattr(server.keys.repo, 'touch', unreadable)
    server.keys.touch(record)
    status, _, body = call(server, '/v2/whoami', headers={'Authorization':'Bearer '+secret})
    assert status == 200 and json.loads(body)['key'] == 'box'


def test_touch_write_errors_are_throttled(tmp_path, monkeypatch, caplog):
    prepare_data_dir(tmp_path)
    keys = KeyStore(tmp_path / "server", workspace_id=default_workspace_id(tmp_path / "server"))
    record, secret = keys.create("worker")
    assert keys.verify(secret) == record
    now = [0]
    monkeypatch.setattr("agenticml.server.keys.time.monotonic", lambda: now[0])
    def unwritable(*args):
        raise PermissionError("private failure")
    monkeypatch.setattr(keys.repo, "touch", unwritable)
    for moment in (0, 1, 59):
        now[0] = moment
        keys.touch(record)
    messages = lambda: [row.getMessage() for row in caplog.records
                        if row.name == "agenticml.server.keys"]
    assert messages() == ["could not update key last_used_at"]
    now[0] = 60
    keys.touch(record)
    assert messages() == ["could not update key last_used_at"] * 2


def test_store_name_errors_and_guard(server):
    for name in (".", "..", "env", 1):
        with pytest.raises(ValueError, match="invalid or reserved key name"):
            validate_name(name)
    with pytest.raises(KeyNameTaken):
        server.keys.create("admin")
    with pytest.raises(LastAdminError):
        server.keys.revoke("admin", keep_admin=True)
    assert server.keys.revoke("missing") is None
    record = server.keys.verify(server.admin_key)
    assert server.keys.revoke("admin") == record


def test_local_ui_has_no_key_routes(tmp_path):
    httpd = ui_server.make_server(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        for path in ("/api/server/keys", "/setup", "/static/setup.js"):
            assert call(httpd, path)[0] == 404
        assert call(httpd, "/api/server/keys/box", "DELETE")[0] == 405
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_api_key_cli_login_and_sync(server, tmp_path, monkeypatch):
    for name in ("AGENTICML_HOST", "AGENTICML_API_KEY", "AGENTICML_SERVER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    status, _, payload = api(server, method="POST", payload={"name": "box", "scope": "sync"})
    assert status == 201
    assert cli.main(["login", "--host", payload["host"], "--key", payload["key"]]) == 0
    writer = RunWriter.create(tmp_path / "local", run_id="train", meta={"project": "demo", "name": "train"})
    writer.write_scalars([(i, {"loss": 1 / (i + 1)}) for i in range(5)])
    writer.add_media("sample", 1, b"preview", "text/plain", "caption")
    writer.finish("finished")
    assert cli.main(["sync", writer.run_id, "--host", payload["host"], "--dir", str(writer.root)]) == 0
    source = RunReader(writer.root, writer.run_id)
    remote = RunReader(server.root, writer.run_id)
    assert remote.meta() == source.meta()
    assert remote.keys() == source.keys()
    for name in source.keys():
        assert remote.series(name) == source.series(name)
    assert (remote.run_dir / "events.jsonl").read_bytes() == (source.run_dir / "events.jsonl").read_bytes()
    assert (remote.run_dir / "media/index.json").read_bytes() == (source.run_dir / "media/index.json").read_bytes()
    remote_obj = server.run_index.as_remote(writer.run_id)
    assert remote_obj["owner"] == "admin" and remote_obj["owner_key"] == "box"

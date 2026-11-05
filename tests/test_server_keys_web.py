"""First-admin setup and admin key lifecycle over the real HTTP server."""
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
from agenticml.server.webauth import WebAuth
from agenticml.store import RunReader, RunWriter
from agenticml.ui import server as ui_server


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


def setup(server, payload=None, headers=None):
    if payload is None:
        payload = {"token": server.web.setup_token}
    status, hdrs, body = call(server, "/setup", "POST", json.dumps(payload),
                              csrf(server) if headers is None else headers)
    return status, hdrs, json.loads(body)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    prepare_data_dir(data)
    _, key = KeyStore(data / "server").create("admin", "admin")
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
            f"No admin key yet. Open https://ml.example.com/setup?token={token} "
            "to create one, or run: agenticml server keys create --name admin --scope admin")
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
        assert status == 201 and payload["ok"] and payload["key"] == "admin"
        assert payload["host"] == "https://ml.example.com/" and payload["host_source"] == "public_url"
        assert hdrs["Cache-Control"] == "no-store"
        assert "Secure" in hdrs["Set-Cookie"] and hdrs["Set-Cookie"].startswith("__Host-")
        assert httpd.keys.verify(payload["value"]).scope == "admin"
        cookie = hdrs["Set-Cookie"].split(";", 1)[0]
        assert call(httpd, "/api/server/session", headers={"Cookie": cookie})[0] == 200
        assert setup(httpd)[0] == 404
        assert call(httpd, "/setup?token=" + token)[0] == 404
        secrets = (token, payload["value"], httpd.keys.verify(payload["value"]).sha256)
        assert all(secret not in record.getMessage()
                   for record in caplog.records if record is not startup_warning
                   for secret in secrets)


@pytest.mark.parametrize("failure,expected,message", [
    ("csrf", 403, None), ("type", 415, None), ("json", 400, "invalid JSON body"),
    ("non-object", 400, "invalid JSON body"), ("token-type", 400, "invalid setup body"),
    ("name-type", 400, "invalid setup body"), ("name", 400, "invalid or reserved key name"),
    ("taken", 409, None), ("large", 413, None),
])
def test_setup_validation_does_not_consume(tmp_path, failure, expected, message):
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        httpd.keys.create("taken")
        payload = {"token": token}
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
        assert len(httpd.keys.list()) == 1


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
    with running(data) as httpd:
        httpd.keys.create("admin", "admin")
        marker = httpd.keys.server_dir / "setup-closed"
        assert marker.is_file()
        httpd.keys.revoke("admin")
        assert not httpd.keys.has_admin()
        assert marker.is_file()
    with running(data) as httpd:
        token = httpd.web.setup_token
        assert token is not None
        assert not (httpd.keys.server_dir / "setup-closed").exists()
        status, _, payload = setup(httpd)
        assert status == 201 and payload["ok"] and payload["key"] == "admin"


def test_setup_closed_marker_mode_and_no_follow(tmp_path):
    prepare_data_dir(tmp_path / "data")
    keys = KeyStore(tmp_path / "data" / "server")
    target = tmp_path / "outside"
    target.write_bytes(b"secret")
    marker = keys.server_dir / "setup-closed"
    marker.symlink_to(target)
    keys.create("admin", "admin")
    info = marker.lstat()
    assert stat.S_ISREG(info.st_mode) and not marker.is_symlink()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert target.read_bytes() == b"secret"


def test_setup_marker_write_failure_keeps_token(tmp_path, monkeypatch):
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        before = httpd.keys.path.read_bytes() if httpd.keys.path.exists() else None

        def fail(*_args, **_kwargs):
            raise OSError("disk full")

        with monkeypatch.context() as patch:
            patch.setattr(httpd.keys, "_write_setup_closed", fail)
            status, _, body = setup(httpd, {"token": token})
            assert status == 503 and body == {"error": "key store unavailable"}
        assert (httpd.keys.path.read_bytes() if httpd.keys.path.exists() else None) == before
        assert not httpd.keys.has_admin()
        assert not httpd.web._setup_used
        status, _, payload = setup(httpd, {"token": token})
        assert status == 201 and payload["ok"] and payload["key"] == "admin"


def test_sync_create_writes_no_marker(tmp_path):
    prepare_data_dir(tmp_path / "data")
    keys = KeyStore(tmp_path / "data" / "server")
    keys.create("box", "sync")
    assert not (keys.server_dir / "setup-closed").exists()


def test_api_admin_create_writes_marker(server):
    marker = server.keys.server_dir / "setup-closed"
    marker.unlink(missing_ok=True)
    assert api(server, method="POST", payload={"name": "second", "scope": "admin"})[0] == 201
    assert marker.is_file()


def test_keys_lock_symlink_does_not_crash_startup(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    data = tmp_path / "data"
    prepare_data_dir(data)
    (data / "server" / "keys.lock").symlink_to(tmp_path / "outside")
    httpd = make_server(data, port=0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        assert httpd.web.setup_token is None
        errors = [record for record in caplog.records if record.levelno == logging.ERROR]
        assert any(record.getMessage() == (
            "could not lock keys store; setup disabled until the "
            "file is fixed and the server restarts") for record in errors)
        assert not any("/setup?token=" in record.getMessage() for record in caplog.records)
        assert call(httpd, "/setup")[0] == 404
        assert call(httpd, "/setup?token=anything")[0] == 404
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
        assert not thread.is_alive()


@pytest.mark.parametrize("reader", [False, True], ids=["no-reader", "with-reader"])
@pytest.mark.parametrize("operation", ["startup", "create"])
def test_keys_lock_fifo_fails_promptly(tmp_path, caplog, operation, reader):
    data = tmp_path / "data"
    prepare_data_dir(data)
    path = data / "server" / "keys.lock"
    os.mkfifo(path, 0o600)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK) if reader else None
    results, errors = [], []

    def attempt():
        try:
            if operation == "startup":
                results.append(make_server(data, port=0))
            else:
                results.append(KeyStore(data / "server").create("worker"))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=attempt, daemon=True)
    try:
        thread.start()
        thread.join(timeout=3)
        assert not thread.is_alive(), "keys.lock FIFO blocked the operation"
        if operation == "startup":
            assert not errors and len(results) == 1
            assert results[0].web.setup_token is None
            assert [record.getMessage() for record in caplog.records
                    if record.levelno == logging.ERROR] == [
                "could not lock keys store; setup disabled until the "
                "file is fixed and the server restarts"]
            assert not any("/setup?token=" in record.getMessage() for record in caplog.records)
        else:
            assert not results and len(errors) == 1 and isinstance(errors[0], OSError)
    finally:
        if fd is not None:
            os.close(fd)
        for httpd in results if operation == "startup" else []:
            httpd.server_close()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="requires procfs fd counts")
def test_keys_lock_fifo_with_reader_does_not_leak_fd(tmp_path):
    prepare_data_dir(tmp_path)
    path = tmp_path / "server" / "keys.lock"
    os.mkfifo(path, 0o600)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        before = len(os.listdir("/proc/self/fd"))
        with pytest.raises(OSError, match="keys.lock is not a regular file"):
            KeyStore(tmp_path / "server").create("worker")
        assert len(os.listdir("/proc/self/fd")) == before
    finally:
        os.close(fd)


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="requires procfs fd counts")
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_keys_lock_fdopen_failure_does_not_leak_fd(tmp_path, monkeypatch, error):
    prepare_data_dir(tmp_path)
    def fail(*args):
        raise error("fdopen failed")
    monkeypatch.setattr(os, "fdopen", fail)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(error, match="fdopen failed"):
        KeyStore(tmp_path / "server").create("worker")
    assert len(os.listdir("/proc/self/fd")) == before


def test_undeletable_marker_at_startup_disarms_setup(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    data = tmp_path / "data"
    prepare_data_dir(data)
    (data / "server" / "setup-closed").mkdir()
    with running(data) as httpd:
        assert httpd.web.setup_token is None
        assert not any("/setup?token=" in record.getMessage() for record in caplog.records)
        assert all("aml_" not in record.getMessage() for record in caplog.records)


def test_clear_setup_closed_removes_symlink_only(tmp_path):
    prepare_data_dir(tmp_path / "data")
    keys = KeyStore(tmp_path / "data" / "server")
    target = tmp_path / "outside"
    target.write_bytes(b"keep")
    marker = keys.server_dir / "setup-closed"
    marker.symlink_to(target)
    keys.clear_setup_closed()
    assert not marker.exists() and not marker.is_symlink()
    assert target.read_bytes() == b"keep"


def test_setup_closed_true_on_non_enoent(tmp_path, monkeypatch):
    prepare_data_dir(tmp_path / "data")
    keys = KeyStore(tmp_path / "data" / "server")

    def fail(_path):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(os, "lstat", fail)
    assert keys.setup_closed() is True


def test_create_only_if_no_admin_closed_after_external_revoke(tmp_path):
    prepare_data_dir(tmp_path / "data")
    keys = KeyStore(tmp_path / "data" / "server")
    external = KeyStore(tmp_path / "data" / "server")
    external.create("operator", "admin")
    external.revoke("operator")
    before = keys.path.read_bytes() if keys.path.exists() else None
    with pytest.raises(SetupClosed):
        keys.create("second", "admin", only_if_no_admin=True)
    assert (keys.path.read_bytes() if keys.path.exists() else None) == before


def test_arm_setup_waits_on_store_lock(tmp_path):
    prepare_data_dir(tmp_path / "data")
    keys = KeyStore(tmp_path / "data" / "server")
    (keys.server_dir / "setup-closed").write_bytes(b"")
    web = WebAuth(keys)
    release = threading.Event()
    holding = threading.Event()

    def hold():
        with keys._lock:
            holding.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(timeout=5)
    worker = threading.Thread(target=lambda: web.arm_setup("http://127.0.0.1"))
    worker.start()
    worker.join(timeout=0.3)
    assert worker.is_alive()
    assert web.setup_token is None
    assert (keys.server_dir / "setup-closed").exists()
    release.set()
    worker.join(timeout=5)
    holder.join(timeout=5)
    assert web.setup_token is not None
    assert not (keys.server_dir / "setup-closed").exists()


def test_prepare_setup_holds_lock_across_clear_and_check(tmp_path):
    prepare_data_dir(tmp_path / "data")
    keys = KeyStore(tmp_path / "data" / "server")
    (keys.server_dir / "setup-closed").write_bytes(b"")
    held = []
    real_clear = keys.clear_setup_closed
    real_read = keys._read
    lock_path = keys.server_dir / "keys.lock"

    def assert_keys_lock_held():
        with open(lock_path, "ab") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def clear():
        held.append(keys._lock.locked())
        assert_keys_lock_held()
        return real_clear()

    def read():
        held.append(keys._lock.locked())
        assert_keys_lock_held()
        return real_read()

    keys.clear_setup_closed = clear
    keys._read = read
    assert keys.prepare_setup() is True
    assert held == [True, True]
    assert not (keys.server_dir / "setup-closed").exists()
    keys.create("operator", "admin")
    assert keys.prepare_setup() is False


def test_prepare_setup_false_with_env_key(tmp_path):
    prepare_data_dir(tmp_path / "data")
    keys = KeyStore(tmp_path / "data" / "server", env_key="e" * 32)
    assert keys.prepare_setup() is False


@pytest.mark.parametrize("nested", [False, True], ids=["malformed", "deeply-nested"])
def test_setup_corrupt_at_startup_stays_disabled(tmp_path, caplog, nested):
    data = tmp_path / "data"
    prepare_data_dir(data)
    path = data / "server" / "keys.json"
    depth = DEEP_JSON_DEPTH
    contents = "[" * depth + "0" + "]" * depth if nested else "private corrupt contents"
    if nested:
        with pytest.raises(RecursionError):
            json.loads(contents)
    path.write_text(contents)
    with running(data) as httpd:
        assert httpd.web.setup_token is None
        errors = [record for record in caplog.records if record.levelno == logging.ERROR]
        assert any("setup disabled until the file is fixed and the server restarts"
                   in record.getMessage() for record in errors)
        assert all(contents not in record.getMessage() for record in caplog.records)
        assert not any("/setup?token=" in record.getMessage() for record in caplog.records)
        # Fixing the file does not enable setup until restart.
        path.unlink()
        assert call(httpd, "/setup?token=anything")[0] == 404
        assert setup(httpd, {"token": "anything"})[0] == 404
        assert httpd.web.setup_token is None
    with running(data) as httpd:
        assert httpd.web.setup_token is not None


@pytest.mark.parametrize("field", ["created_at", "last_used_at"])
def test_timestamp_overflow_fails_closed_and_disarms_setup(tmp_path, caplog, field):
    caplog.set_level(logging.DEBUG)
    data = tmp_path / "data"
    prepare_data_dir(data)
    keys = KeyStore(data / "server")
    record, secret = keys.create("admin", "admin")
    obj = json.loads(keys.path.read_text())
    obj["keys"][0][field] = "0001-01-01T00:00:00+01:00"
    keys.path.write_text(json.dumps(obj))
    with pytest.raises(ValueError, match="^invalid key record$"):
        keys._read()
    for _ in range(3):
        assert keys.list() == []
    assert [record.getMessage() for record in caplog.records
            if record.levelno == logging.ERROR] == [
        "could not load keys.json; file keys disabled until it changes"]
    caplog.clear()
    with running(data) as httpd:
        assert httpd.web.setup_token is None
        assert [record.getMessage() for record in caplog.records
                if record.levelno == logging.ERROR] == [
            "could not load keys.json; setup disabled until the file is fixed "
            "and the server restarts"]
        for bearer in (secret, "anything"):
            assert call(httpd, "/v2/whoami", headers={"Authorization": "Bearer " + bearer})[0] == 401
        assert call(httpd, "/setup?token=anything")[0] == 404
        assert setup(httpd, {"token": "anything"})[0] == 404
        assert all(secret not in message and record.sha256 not in message
                   and obj["keys"][0][field] not in message and "/setup?token=" not in message
                   for message in (item.getMessage() for item in caplog.records))


@pytest.mark.parametrize("marker", [True, False], ids=["with-marker", "markerless"])
def test_setup_strict_create_closes_after_false_negative(tmp_path, monkeypatch, marker):
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        # A separate store models the CLI writing while the server runs.
        external = KeyStore(httpd.keys.server_dir)
        external.create("operator", "admin")
        if not marker:
            (external.server_dir / "setup-closed").unlink()
        before = httpd.keys.path.read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(httpd.keys, "has_admin", lambda: False)
            status, _, body = setup(httpd, {"token": token, "name": "second"})
            assert status == 404 and body == {"error": "not found"}
        assert httpd.keys.path.read_bytes() == before
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
    keys = KeyStore(data / "server", env_key=env_key)
    if env_key is None:
        keys.create("sync")
        keys.create("operator", "admin", only_if_no_admin=True)
        if not marker:
            (keys.server_dir / "setup-closed").unlink()
    before = keys.path.read_bytes() if keys.path.exists() else None
    with pytest.raises(SetupClosed):
        keys.create("second", "admin", only_if_no_admin=True)
    assert (keys.path.read_bytes() if keys.path.exists() else None) == before
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
            patch.setattr(httpd.keys, "create", failed)
            status, _, body = setup(httpd, {"token": token})
            assert status == 503 and body == {"error": "key store unavailable"}
            assert not httpd.web._setup_used
        status, _, payload = setup(httpd, {"token": token})
        assert status == 201 and payload["ok"] and payload["key"] == "admin"


def test_setup_create_name_taken_keeps_token(tmp_path, monkeypatch):
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token

        def failed(*_args, **_kwargs):
            raise KeyNameTaken("key name already exists")

        with monkeypatch.context() as patch:
            patch.setattr(httpd.keys, "create", failed)
            status, _, body = setup(httpd, {"token": token, "name": "admin"})
            assert status == 409 and body == {"error": "key name already exists"}
            assert not httpd.web._setup_used
        status, _, payload = setup(httpd, {"token": token, "name": "operator"})
        assert status == 201 and payload["ok"] and payload["key"] == "operator"


def test_setup_create_value_error_is_private(tmp_path, monkeypatch):
    with running(tmp_path / "data") as httpd:
        def failed(*args, **kwargs):
            raise ValueError("secret detail aml_x")
        monkeypatch.setattr(httpd.keys, "_write", failed)
        status, _, body = call(httpd, "/setup", "POST",
                               json.dumps({"token": httpd.web.setup_token}), csrf(httpd))
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
        httpd.keys._write(records)
        runs = httpd.root / "runs"
        runs.mkdir(exist_ok=True)
        for name, owner in (("one", "z-box"), ("two", "z-box"), ("three", "a-box")):
            path = runs / name
            path.mkdir()
            (path / "remote.json").write_text(json.dumps({"owner": owner}))
        (runs / "linked").symlink_to(runs / "one", target_is_directory=True)
        (runs / "bad").mkdir()
        (runs / "bad" / "remote.json").write_text("broken")
        status, _, payload = api(httpd)
        assert status == 200
        assert payload["host"] == f"http://127.0.0.1:{httpd.server_address[1]}"
        assert payload["host_source"] == "request"
        rows = payload["keys"]
        assert [row["name"] for row in rows] == ["a-box", "z-box", "env"]
        fields = {"name", "scope", "prefix", "created_at", "created_by", "last_used_at", "runs", "revocable"}
        assert all(set(row) == fields for row in rows)
        assert [row["runs"] for row in rows] == [1, 2, 0]
        assert [row["revocable"] for row in rows] == [True, True, False]
        assert all(record.sha256 not in json.dumps(payload) for record in httpd.keys.list())


def test_run_count_scan_cap(server, monkeypatch):
    monkeypatch.setattr(keyspage, "RUN_SCAN_MAX", 2)
    runs = server.root / "runs"
    runs.mkdir(exist_ok=True)
    for name in ("a", "b"):
        (runs / name).mkdir()
    assert api(server)[2]["keys"][0]["runs"] == 0
    (runs / "c").mkdir()
    assert api(server)[2]["keys"][0]["runs"] is None


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
        assert all(row["runs"] == (0 if state == "missing" else None) for row in payload["keys"])
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
    monkeypatch.setattr(server.keys, "_write", failed)
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
    assert not any(entry["name"] == "second" for entry in server.web._sessions.values())
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
    assert server.web._sessions[digest]["name"] == "second"
    assert server.web._sessions[digest]["sha256"] == record.sha256
    assert api(server, cookie=cookie)[0] == 401
    assert digest not in server.web._sessions
    assert api(server, cookie=login(server, new_key))[0] == 200


def test_drop_sessions_matches_name_and_hash(server):
    old = server.keys.verify(server.admin_key)
    other = replace(old, name="other")
    newer = replace(old, sha256="a" * 64)
    for record in (old, other, newer):
        server.web._create_session(record)
    server.web.drop_sessions(old.name, old.sha256)
    assert {(entry["name"], entry["sha256"]) for entry in server.web._sessions.values()} == {
        (other.name, other.sha256), (newer.name, newer.sha256)}


def test_last_admin_guard_and_cli_recovery(server, monkeypatch):
    before = server.keys.path.read_bytes()
    status, _, payload = api(server, "/api/server/keys/admin", "DELETE")
    assert status == 409 and payload == {"error": "last admin key; create another admin key first, or use the CLI"}
    assert server.keys.path.read_bytes() == before
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
    for name in ("%2e%2e", "%2e", "a%2Fb", "a/b", ""):
        assert api(server, "/api/server/keys/" + name, "DELETE")[0] == 400
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
    ("GET", "/", "Content-Length", 200),
    ("GET", "/static/app.css", "Content-Length", 200),
    ("GET", "/api/server/keys", "Content-Length", 200),
    ("HEAD", "/login", "Content-Length", 405),
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


@pytest.mark.parametrize("nested", [False, True], ids=["malformed", "deeply-nested"])
def test_store_corruption_503_and_cli(tmp_path, capsys, monkeypatch, nested):
    with running(tmp_path / "data", env_key="e" * 32) as httpd:
        httpd.cookie = login(httpd, "e" * 32)
        httpd.keys.create("box")
        depth = DEEP_JSON_DEPTH
        contents = "[" * depth + "0" + "]" * depth if nested else "private corrupt contents"
        if nested:
            with pytest.raises(RecursionError):
                json.loads(contents)
        httpd.keys.path.write_text(contents)
        for method, path, payload in (("POST", "/api/server/keys", {"name": "new", "scope": "sync"}),
                                      ("DELETE", "/api/server/keys/box", None)):
            status, _, body = api(httpd, path, method, payload)
            assert status == 503 and body == {"error": "key store unavailable"}
        for action in (["create", "--name", "new"], ["revoke", "box"]):
            assert cli.main(["server", "keys", *action, "--data", str(httpd.data_dir)]) == 1
            assert contents not in capsys.readouterr().err
        assert httpd.keys.path.read_text() == contents
        # Also exercise the gate's central exception mapping.
        def unavailable(*args):
            raise KeyStoreUnavailable("private failure")
        monkeypatch.setattr(httpd.keys, "current", unavailable)
        assert api(httpd)[0] == 503


@pytest.mark.parametrize("error", [PermissionError, ValueError, TypeError, RecursionError, KeyStoreUnavailable])
def test_touch_read_failure_is_advisory(server, monkeypatch, error):
    record, secret = server.keys.create("box")
    assert server.keys.verify(secret) == record  # Reload before making the file unreadable.
    def unreadable():
        raise error("private failure")
    monkeypatch.setattr(server.keys, "_read", unreadable)
    server.keys.touch(record)
    status, _, body = call(server, "/v2/whoami", headers={"Authorization": "Bearer " + secret})
    assert status == 200 and json.loads(body)["key"] == "box"
    for mutate in (lambda: server.keys.create("new"), lambda: server.keys.revoke("box")):
        with pytest.raises(KeyStoreUnavailable):
            mutate()


def test_touch_write_errors_are_throttled(tmp_path, monkeypatch, caplog):
    prepare_data_dir(tmp_path)
    keys = KeyStore(tmp_path / "server")
    record, secret = keys.create("worker")
    assert keys.verify(secret) == record
    now = [0]
    monkeypatch.setattr("agenticml.server.keys.time.monotonic", lambda: now[0])
    def unwritable(records):
        raise PermissionError("private failure")
    monkeypatch.setattr(keys, "_write", unwritable)
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
    assert json.loads((remote.run_dir / "remote.json").read_bytes())["owner"] == "box"

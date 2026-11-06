"""Protocol v2 integration tests over raw HTTP and a real v1 store."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import stat
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agenticml import cli
from agenticml.server import app, ingest
from agenticml.server.app import make_server
from agenticml.server.keys import KeyStore
from agenticml.store import RunReader, RunWriter, layout
from agenticml.store.gc import run_fingerprint
from agenticml.store.overview import Overview


@contextmanager
def running(data, env_key=None):
    httpd = make_server(data, port=0, env_key=env_key)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        _, httpd.test_key = httpd.keys.create("one")
        yield httpd


def request(server, path, method="GET", body=None, key=None, headers=None):
    conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    supplied = {"Authorization": "Bearer " + (server.test_key if key is None else key)}
    supplied.update(headers or {})
    try:
        conn.request(method, path, body=body, headers=supplied)
        response = conn.getresponse()
        return response.status, json.loads(response.read()), dict(response.getheaders())
    finally:
        conn.close()


def raw(server, method, path, headers=(), body=b""):
    client = socket.create_connection(server.server_address[:2], timeout=3)
    try:
        head = f"{method} {path} HTTP/1.1\r\nHost: arbitrary.example\r\n".encode()
        head += b"".join(f"{name}: {value}\r\n".encode("iso-8859-1") for name, value in headers)
        client.sendall(head + b"\r\n" + body)
        response = http.client.HTTPResponse(client, method=method)
        response.begin()
        payload = response.read()
        return response.status, json.loads(payload) if payload else None, dict(response.getheaders())
    finally:
        client.close()


def auth(server):
    return [("Authorization", "Bearer " + server.test_key)]


def put_meta(server, run_id="run-1", status="running", key=None):
    return request(server, f"/v2/runs/{run_id}/files/meta.json", "PUT",
                   json.dumps({"run_id": run_id, "status": status}).encode(), key)


def append(server, data, offset=0, name="scalars/0.bin", query="", key=None):
    return request(server, f"/v2/runs/run-1/append/{name}?offset={offset}{query}", "POST", data, key)


def commit(server, status="running", sizes=None, fingerprint="a" * 64):
    return request(server, "/v2/runs/run-1/commit", "POST", json.dumps({
        "status": status, "sizes": {} if sizes is None else sizes, "fingerprint": fingerprint,
    }).encode())


def run_path(server, name=""):
    return server.root / "runs" / "run-1" / name


def foreign_key(server, name="other", user="bob", role="member"):
    from agenticml.server.users import UserStore

    store = UserStore(server.data_dir / "server")
    if store.get(user) is None:
        store.add(user, role=role)
    return server.keys.create(name, user=user)[1]


def test_health_auth_scopes_and_revocation(server):
    assert raw(server, "GET", "/healthz")[0:2] == (200, {"ok": True})
    for headers in ([], [("Authorization", "Basic abc")], [("Authorization", "Bearer bad")],
                    [("Authorization", "Bearer")], [("Authorization", "Bearer  " + server.test_key)],
                    auth(server) * 2):
        status, obj, response_headers = raw(server, "GET", "/v2/whoami", headers)
        assert status == 401 and obj == {"error": "unauthorized"}
        assert response_headers["WWW-Authenticate"] == "Bearer"
    assert raw(server, "GET", "/v2/whoami", [("Authorization", "bEaReR " + server.test_key)])[0] == 200
    assert request(server, "/v2/whoami")[1] == {
        "key": "one", "scope": "sync", "user": "admin", "role": "admin", "protocol": 2,
        "features": (["lineage"] if server.lineage_enabled else []) + ["byref"],
    }
    _, admin = server.keys.create("admin", "admin")
    assert request(server, "/v2/whoami", key=admin)[1]["scope"] == "admin"
    assert put_meta(server, "admin-run", key=admin)[0] == 200
    KeyStore(server.data_dir / "server").revoke("one")
    assert request(server, "/v2/whoami")[0] == 401


def test_zero_keys_and_environment(tmp_path):
    with running(tmp_path / "empty") as httpd:
        assert raw(httpd, "GET", "/healthz")[0] == 200
        assert raw(httpd, "GET", "/v2/whoami")[0] == 401
    with running(tmp_path / "env", "a" * 32) as httpd:
        assert raw(httpd, "GET", "/v2/whoami", [("Authorization", "Bearer " + "a" * 32)])[1] == {
            "key": "env", "scope": "admin", "user": "env", "role": "admin", "protocol": 2,
            "features": (["lineage"] if httpd.lineage_enabled else []) + ["byref"],
        }
        assert not (httpd.data_dir / "server" / "keys.json").exists()


def test_default_bind_cli_and_shutdown(tmp_path, monkeypatch, capsys):
    httpd = make_server(tmp_path, port=0)
    assert httpd.server_address[0] == "127.0.0.1"
    httpd.server_close()
    original = app.make_server
    seen = []
    def construct(data, host, port, public_url, env_key, trusted_proxies=(), identity_header=None):
        httpd = original(data, host, port, public_url, env_key, trusted_proxies, identity_header)
        seen.append(httpd)
        def run_once():
            def health():
                assert raw(httpd, "GET", "/healthz")[0] == 200
            thread = threading.Thread(target=health)
            thread.start()
            httpd.handle_request()
            thread.join(timeout=5)
            raise KeyboardInterrupt
        httpd.serve_forever = run_once
        return httpd
    monkeypatch.setattr(app, "make_server", construct)
    assert cli.main(["server", "serve", "--data", str(tmp_path), "--port", "0"]) == 0
    assert seen[0].bind_host == "127.0.0.1"
    assert seen[0]._server_lock is None
    output = capsys.readouterr().out
    assert "AgenticML server: http://127.0.0.1:" in output and "stopped" in output
    with original(tmp_path, port=0):
        pass


def test_lock_tmp_cleanup_and_permissions(tmp_path, caplog):
    app.prepare_data_dir(tmp_path)
    tmp = tmp_path / "server" / "tmp"
    (tmp / "abandoned").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.write_bytes(b"safe")
    (tmp / "link").symlink_to(outside)
    (tmp / "dir").mkdir()
    httpd = make_server(tmp_path, port=0)
    try:
        assert sorted(p.name for p in tmp.iterdir()) == ["dir"]
        assert outside.read_bytes() == b"safe"
        assert "ignoring subdirectory" in caplog.text
        with pytest.raises(ValueError, match="another agenticml server"):
            make_server(tmp_path, port=0)
        for name in ("store", "server", "server/tmp"):
            assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o700
    finally:
        httpd.server_close()
    with make_server(tmp_path, port=0):
        pass


def test_startup_removes_abandoned_lineage_quarantines(tmp_path, caplog):
    app.prepare_data_dir(tmp_path)
    tmp = tmp_path / "server" / "tmp"
    pack = tmp / "lineage-abandoned" / "pack"
    pack.mkdir(parents=True)
    (pack / "partial.pack").write_bytes(b"partial")
    (tmp / "other").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "safe").write_bytes(b"safe")
    (tmp / "lineage-link").symlink_to(outside, target_is_directory=True)
    with make_server(tmp_path, port=0):
        assert sorted(p.name for p in tmp.iterdir()) == ["other"]
        assert (outside / "safe").read_bytes() == b"safe"
        assert "ignoring subdirectory in server/tmp: " + str(tmp / "other") in caplog.text
        assert "ignoring subdirectory in server/tmp: " + str(tmp / "lineage-abandoned") not in caplog.text


@pytest.mark.parametrize("reader", [False, True], ids=["no-reader", "with-reader"])
def test_server_lock_fifo_fails_promptly(tmp_path, reader):
    app.prepare_data_dir(tmp_path)
    path = tmp_path / "server" / "server.lock"
    os.mkfifo(path, 0o600)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK) if reader else None
    results, errors = [], []
    def attempt():
        try:
            results.append(make_server(tmp_path, port=0))
        except Exception as exc:
            errors.append(exc)
    before = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    thread = threading.Thread(target=attempt, daemon=True)
    try:
        thread.start()
        thread.join(timeout=3)
        assert not thread.is_alive(), "server.lock FIFO blocked startup"
        assert not results and len(errors) == 1
        assert isinstance(errors[0], ValueError)
        assert str(errors[0]) == "server.lock is not a regular file"
        if before is not None:
            assert len(os.listdir("/proc/self/fd")) == before
    finally:
        if fd is not None:
            os.close(fd)
        for httpd in results:
            httpd.server_close()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="requires procfs fd counts")
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_server_lock_fdopen_failure_does_not_leak_fd(tmp_path, monkeypatch, error):
    app.prepare_data_dir(tmp_path)
    def fail(*args):
        raise error("fdopen failed")
    monkeypatch.setattr(os, "fdopen", fail)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(error, match="fdopen failed"):
        make_server(tmp_path, port=0)
    assert len(os.listdir("/proc/self/fd")) == before


def test_close_holds_lock_until_active_handler_finishes(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = ingest.IngestHandler._object
    def paused(self, *args):
        entered.set()
        assert release.wait(5)
        return original(self, *args)
    monkeypatch.setattr(ingest.IngestHandler, "_object", paused)
    httpd = make_server(tmp_path, port=0)
    _, httpd.test_key = httpd.keys.create("one")
    serving = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01})
    serving.start()
    def upload():
        try:
            request(httpd, "/v2/objects/" + hashlib.sha256(b"x").hexdigest(), "PUT", b"x")
        except (OSError, http.client.HTTPException):
            pass  # server_close shuts down the client before waiting for apply.
    client = threading.Thread(target=upload)
    client.start()
    closing = threading.Thread(target=httpd.server_close)
    try:
        assert entered.wait(3)
        httpd.shutdown()
        serving.join(timeout=3)
        closing.start()
        client.join(timeout=3)
        assert closing.is_alive()
        with pytest.raises(ValueError, match="another agenticml server"):
            make_server(tmp_path, port=0)
    finally:
        release.set()
        httpd.shutdown()
        serving.join(timeout=5)
        client.join(timeout=5)
        if closing.ident is not None:
            closing.join(timeout=5)
        httpd.server_close()
    assert not closing.is_alive()
    with make_server(tmp_path, port=0):
        pass


def test_interrupted_close_keeps_server_lock(tmp_path, monkeypatch):
    httpd = make_server(tmp_path, port=0)
    def interrupted(self):
        raise KeyboardInterrupt
    try:
        with monkeypatch.context() as patch:
            patch.setattr(app.DashboardServer, "server_close", interrupted)
            with pytest.raises(KeyboardInterrupt):
                httpd.server_close()
            assert not httpd._server_lock.closed
            with pytest.raises(ValueError, match="another agenticml server"):
                make_server(tmp_path, port=0)
    finally:
        httpd.server_close()


def test_retried_close_waits_after_interrupted_worker_join(tmp_path, monkeypatch):
    entered, release, rejoining = threading.Event(), threading.Event(), threading.Event()
    original = ingest.IngestHandler._object
    def paused(self, *args):
        entered.set()
        assert release.wait(5)
        return original(self, *args)
    monkeypatch.setattr(ingest.IngestHandler, "_object", paused)
    httpd = make_server(tmp_path, port=0)
    _, httpd.test_key = httpd.keys.create("one")
    serving = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01})
    serving.start()
    def upload():
        try:
            request(httpd, "/v2/objects/" + hashlib.sha256(b"x").hexdigest(), "PUT", b"x")
        except (OSError, http.client.HTTPException):
            pass
    client = threading.Thread(target=upload)
    client.start()
    closing = threading.Thread(target=httpd.server_close)
    try:
        assert entered.wait(3)
        httpd.shutdown()
        serving.join(timeout=3)
        worker = next(thread for thread in httpd._threads if thread.is_alive())
        join = worker.join
        calls = []
        def interrupted_once(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise KeyboardInterrupt
            rejoining.set()
            return join(*args, **kwargs)
        monkeypatch.setattr(worker, "join", interrupted_once)
        with pytest.raises(KeyboardInterrupt):
            httpd.server_close()
        assert not httpd._server_lock.closed
        closing.start()
        assert rejoining.wait(3)
        assert closing.is_alive()
        with pytest.raises(ValueError, match="another agenticml server"):
            make_server(tmp_path, port=0)
    finally:
        release.set()
        httpd.shutdown()
        serving.join(timeout=5)
        client.join(timeout=5)
        if closing.ident is not None:
            closing.join(timeout=5)
        httpd.server_close()
    assert not closing.is_alive()


def test_cross_device_tmp_refused(tmp_path, monkeypatch):
    app.prepare_data_dir(tmp_path)
    original = Path.stat
    def cross_device(self, *args, **kwargs):
        info = original(self, *args, **kwargs)
        if self == tmp_path / "server" / "tmp":
            return SimpleNamespace(st_dev=info.st_dev + 1, st_mode=info.st_mode)
        return info
    monkeypatch.setattr(Path, "stat", cross_device)
    with pytest.raises(ValueError, match="same filesystem"):
        make_server(tmp_path, port=0)


def test_missing_fcntl_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "fcntl", None)
    with pytest.raises(ValueError, match="POSIX fcntl"):
        make_server(tmp_path, port=0)
    assert cli.main(["server", "serve", "--data", str(tmp_path), "--port", "0"]) == 1
    assert "the server needs POSIX fcntl" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["store", "server", "server/tmp"])
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_unsafe_data_directories(tmp_path, name, kind):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        target.write_bytes(b"x")
    else:
        dest = tmp_path / "destination"
        dest.mkdir()
        target.symlink_to(dest, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        make_server(tmp_path, port=0)


@pytest.mark.parametrize("err", [errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP])
def test_unsupported_lock_refused(tmp_path, monkeypatch, err):
    import fcntl
    def failed(*args):
        raise OSError(err, "unsupported")
    monkeypatch.setattr(fcntl, "flock", failed)
    with pytest.raises(ValueError, match="filesystem.*flock"):
        make_server(tmp_path, port=0)


@pytest.mark.parametrize("url", ["ftp://host", "https://", "https://host?q=1", "https://host#frag", "http://host:bad"])
def test_invalid_public_url(tmp_path, url):
    with pytest.raises(ValueError, match="public URL"):
        make_server(tmp_path, port=0, public_url=url)


def test_public_url_and_directory_warning(tmp_path, caplog):
    app.prepare_data_dir(tmp_path)
    (tmp_path / "server").chmod(0o755)
    with make_server(tmp_path, port=0, public_url="https://ml.example.com/base") as httpd:
        assert httpd.public_url == "https://ml.example.com/base"
    assert "group- or world-accessible" in caplog.text


@pytest.mark.parametrize("path", ["/", "/api/runs", "/index.html", "/v1/runs", "/unknown",
    "/v2/runs/run-1/files/../meta.json", "/v2/runs/run-1/files/./meta.json",
    "/v2/runs/run-1/files/%2e%2e/meta.json", "/v2/runs/run-1/files/%2fetc",
    "/v2/runs/run-1/files/meta%2ejson", "/v2/runs/run-1/files/a\\b", "//v2/whoami",
    "http://localhost/v2/whoami", "/v2/runs/.bad/files/meta.json", "/v2/runs/../state",
    "/v2/runs/run-1/files//meta.json", "/v2/runs/" + "a" * 129 + "/state",
    *["/v2/runs/run-1/files/" + name for name in ("run.lock", "notes.md", "sync.json", "remote.json", "scalars/01.ovw", "scalars/1000000000.ovw")]])
def test_routes_and_traversal(server, path):
    status = raw(server, "PUT", path, auth(server) + [("Content-Length", "2")], b"{}")[0]
    ingest = path == "/v2" or path.startswith("/v2/")
    # Dashboard paths see Host: arbitrary.example from raw() and 403 before auth.
    assert status == (404 if ingest else 403)
    assert not (server.root / "runs").exists()


def test_wrong_method_and_unknown_method(server):
    for method in ("PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT", "CUSTOM"):
        status, _, headers = raw(server, method, "/v2/whoami", auth(server))
        assert status == 405
        assert headers["Allow"] == "GET"
    assert raw(server, "GET", "/v2/runs/run-1/commit", auth(server))[0] == 405
    assert raw(server, "GET", "/v2/runs/run-1/files/meta.json", auth(server))[2]["Allow"] == "PUT"


def test_malformed_http_error_is_json(server):
    with socket.create_connection(server.server_address[:2], timeout=3) as client:
        client.sendall(b"BAD\r\n")
        body = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            body += chunk
    # No HTTP version was supplied, so the stdlib answers in HTTP/0.9 form.
    assert json.loads(body) == {"error": "invalid request"}


@pytest.mark.parametrize("name", ["runs", "runs/run-1", "runs/run-1/scalars", "runs/run-1/media"])
@pytest.mark.parametrize("outside", [False, True])
def test_symlink_directories_rejected(server, tmp_path, name, outside):
    target = server.root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    dest = (tmp_path if outside else server.root) / "redirect"
    dest.mkdir()
    target.symlink_to(dest, target_is_directory=True)
    assert put_meta(server)[0] == 404
    assert not list(dest.iterdir())


@pytest.mark.parametrize("headers", [[], [("Transfer-Encoding", "chunked")],
    [("Content-Length", "2"), ("Content-Length", "2")], [("Content-Length", "²")],
    [("Content-Length", "-1")], [("Content-Length", "+2")], [("Content-Length", "2, 2")]])
def test_body_framing(server, headers):
    status, _, _ = raw(server, "PUT", "/v2/runs/run-1/files/meta.json", auth(server) + headers)
    assert status == 411
    assert not (server.root / "runs").exists()


@pytest.mark.parametrize("route", ["atomic", "append", "object", "missing", "commit"])
def test_oversize_rejected_before_read(server, route):
    setattr(server.limits, route + "_body", 10)
    path, method = {
        "atomic": ("/v2/runs/run-1/files/meta.json", "PUT"),
        "append": ("/v2/runs/run-1/append/events.jsonl?offset=0", "POST"),
        "object": ("/v2/objects/" + "a" * 64, "PUT"),
        "missing": ("/v2/objects/missing", "POST"), "commit": ("/v2/runs/run-1/commit", "POST"),
    }[route]
    started = time.monotonic()
    assert raw(server, method, path, auth(server) + [("Content-Length", "11")])[0] == 413
    assert time.monotonic() - started < 1
    assert raw(server, method, path, auth(server) + [("Content-Length", "9" * 5000)])[0] == 413


def test_unauthorized_body_not_read(server):
    status, body, headers = raw(server, "PUT", "/v2/runs/run-1/files/meta.json", [("Content-Length", "100")])
    assert status == 401 and body == {"error": "unauthorized"}
    assert headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("route", ["/v2/objects/" + "a" * 64, "/v2/runs/run-1/files/meta.json"])
def test_body_deadline_and_cleanup(server, route):
    server.limits.body_deadline = .15
    server.limits.body_bytes_per_second = 10**9
    started = time.monotonic()
    assert raw(server, "PUT", route, auth(server) + [("Content-Length", "100")], b"x")[0] == 408
    assert time.monotonic() - started < 1
    deadline = time.monotonic() + 2
    while list(server.tmp_dir.iterdir()) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not list(server.tmp_dir.iterdir())
    assert not (server.root / "runs").exists()


def test_short_body_cleanup(server):
    client = socket.create_connection(server.server_address[:2], timeout=3)
    try:
        client.sendall(("PUT /v2/objects/" + "a" * 64 + " HTTP/1.1\r\nHost: x\r\n"
                        "Authorization: Bearer " + server.test_key + "\r\nContent-Length: 100\r\n\r\nx").encode())
        client.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(client)
        response.begin()
        assert response.status == 408
        response.read()
    finally:
        client.close()
    deadline = time.monotonic() + 2
    while list(server.tmp_dir.iterdir()) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not list(server.tmp_dir.iterdir())


def test_body_concurrency_limit(server):
    server.limits.concurrent_bodies = 1
    client = socket.create_connection(server.server_address[:2], timeout=3)
    try:
        client.sendall(("PUT /v2/objects/" + "a" * 64 + " HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                        + server.test_key + "\r\nContent-Length: 100\r\n\r\n").encode())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with server.connection_lock:
                if "body" in server.connections.values():
                    break
            time.sleep(.01)
        else:
            pytest.fail("body not admitted")
        status, _, headers = raw(server, "PUT", "/v2/objects/" + "b" * 64,
                                 auth(server) + [("Content-Length", "100")])
        assert status == 503 and headers["Retry-After"] == "5"
        assert request(server, "/v2/whoami")[0] == 200
    finally:
        client.close()


def test_meta_validation_creation_and_ownership(server):
    for obj in ({"id": "run-1", "status": "running"}, {"run_id": "other", "status": "running"},
                {"run_id": "run-1", "status": "bad"}, [], "string"):
        assert request(server, "/v2/runs/run-1/files/meta.json", "PUT", json.dumps(obj).encode())[0] == 422
    assert not (server.root / "runs").exists()
    for path, method in (("files/scalars/index.json", "PUT"), ("append/events.jsonl?offset=0", "POST"), ("commit", "POST")):
        assert request(server, "/v2/runs/unknown/" + path, method, b"{}")[0:2] == (404, {"error": "unknown run"})
    assert put_meta(server)[0] == 200
    assert json.loads(run_path(server, "remote.json").read_text()) == {"owner": "admin", "owner_key": "one"}
    second = foreign_key(server, "two")
    for path, method, body in (("files/meta.json", "PUT", b"{}"), ("files/scalars/index.json", "PUT", b"{}"),
                               ("append/events.jsonl?offset=0", "POST", b"{}\n"), ("commit", "POST", b"{}")):
        assert request(server, "/v2/runs/run-1/" + path, method, body, second)[0:2] == (
            409, {"error": "run owned by another user", "owner": "admin", "owner_user": "admin"})
    assert not run_path(server, "events.jsonl").exists()
    server.keys.revoke("one")
    _, server.test_key = server.keys.create("one", user="admin")
    assert put_meta(server)[0] == 200


@pytest.mark.parametrize("remote", [None, b"broken", b"{}", b'{"owner":4}'])
def test_claim_unowned_run(server, remote):
    directory = run_path(server)
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text('{"run_id":"run-1","status":"running"}')
    if remote is not None:
        (directory / "remote.json").write_bytes(remote)
    assert put_meta(server)[0] == 200
    assert json.loads((directory / "remote.json").read_bytes()) == {"owner": "admin", "owner_key": "one"}


def test_atomic_validation_and_state(server):
    assert put_meta(server)[0] == 200
    for name in ("scalars/index.json", "media/index.json"):
        assert request(server, "/v2/runs/run-1/files/" + name, "PUT", b"[]")[0] == 422
        assert request(server, "/v2/runs/run-1/files/" + name, "PUT", b"{}")[0] == 200
    assert request(server, "/v2/runs/run-1/files/scalars/0.ovw", "PUT", b"bad")[0] == 422
    overview = Overview()
    overview.add(0, 1.)
    body = overview.to_bytes()
    assert request(server, "/v2/runs/run-1/files/scalars/0.ovw", "PUT", body)[0:2] == (200, {"size": len(body)})
    assert append(server, b"12345678")[0] == 200
    linked = run_path(server, "scalars/1.bin")
    linked.symlink_to(run_path(server, "scalars/0.bin"))
    _, other = server.keys.create("reader")
    status, state, _ = request(server, "/v2/runs/run-1/state", key=other)
    assert status == 200 and state["owner"] == "reader"
    assert state["owner_user"] == "admin"
    assert state["remote"]["owner"] == "reader"
    assert state["remote"]["owner_user"] == "admin"
    assert "scalars/1.bin" not in state["files"]
    for name, info in state["files"].items():
        data = run_path(server, name).read_bytes()
        assert info == {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    assert request(server, "/v2/runs/missing/state")[0] == 404
    assert append(server, b"87654321", name="scalars/1.bin")[0] == 404
    assert run_path(server, "scalars/0.bin").read_bytes() == b"12345678"
    for name in ("run.lock", "notes.md", "sync.json"):
        assert not run_path(server, name).exists()


@pytest.mark.parametrize("name,data,offset", [("scalars/0.bin", b"short", 0), ("scalars/0.bin", b"12345678", 1),
    ("scalars/_time.bin", b"short", 0), ("events.jsonl", b"{}", 0)])
def test_torn_records_unchanged(server, name, data, offset):
    put_meta(server)
    path = run_path(server, name)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"original")
    assert append(server, data, offset, name)[0:2] == (422, {"error": "torn record"})
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize("query", ["", "offset=-1", "offset=+1", "offset=%30", "offset=0&offset=0",
    "offset=0&truncate=1&truncate=1", "offset=0&truncate=0", "offset=0&extra=1", "offset=0&", "truncate=1"])
def test_invalid_append_query(server, query):
    put_meta(server)
    assert request(server, "/v2/runs/run-1/append/events.jsonl?" + query, "POST", b"{}\n")[0] == 422


def test_append_replay_overlap_and_concurrency(server):
    put_meta(server)
    first, second, third = b"12345678", b"abcdefgh", b"ABCDEFGH"
    assert append(server, first + second)[1] == {"size": 16}
    path = run_path(server, "scalars/0.bin")
    before = path.stat().st_mtime_ns
    assert append(server, first + second)[1] == {"size": 16}
    assert path.stat().st_mtime_ns == before
    assert append(server, first, 24)[0:2] == (409, {"size": 16})
    assert append(server, first, 8)[0:2] == (409, {"size": 16, "mismatch": True})
    assert append(server, second + third, 8)[1] == {"size": 24}
    assert path.read_bytes() == first + second + third
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: append(server, third, 24), range(2)))
    assert all(result[0] == 200 and result[1] == {"size": 32} for result in results)
    assert path.read_bytes() == first + second + third * 2
    assert append(server, b"{}\n", name="events.jsonl")[0] == 200


def test_truncate_recovery_and_terminal_status(server):
    put_meta(server)
    assert append(server, b"12345678", query="&truncate=1")[1] == {"size": 8}
    path = run_path(server, "scalars/0.bin")
    path.write_bytes(b"torn!")
    assert append(server, b"abcdefgh", query="&truncate=1")[1] == {"size": 8}
    assert path.read_bytes() == b"abcdefgh"
    assert append(server, b"12345678", 8, query="&truncate=1")[0] == 422
    assert commit(server)[0] == 200
    assert append(server, b"12345678", query="&truncate=1")[0] == 200
    assert put_meta(server, status="finished")[0] == 200
    assert commit(server, status="finished")[0] == 200
    assert append(server, b"abcdefgh", query="&truncate=1")[0:2] == (
        409, {"error": "run is not running", "status": "finished"})
    assert path.read_bytes() == b"12345678"


def test_append_write_failure_rolls_back(server, monkeypatch):
    put_meta(server)
    assert append(server, b"12345678")[0] == 200
    original = ingest.os.fsync
    calls = []
    def fail_once(fd):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError("injected fsync failure")
        return original(fd)
    # touch is already throttled, so the first fsync belongs to the append.
    monkeypatch.setattr(ingest.os, "fsync", fail_once)
    assert append(server, b"abcdefgh", 8)[0] == 500
    assert run_path(server, "scalars/0.bin").read_bytes() == b"12345678"


def test_append_directory_fsync_failure_rolls_back(server, monkeypatch):
    put_meta(server)
    assert append(server, b"12345678")[0] == 200
    def failed(path):
        raise OSError("injected directory fsync failure")
    monkeypatch.setattr(layout, "fsync_dir", failed)
    assert append(server, b"abcdefgh", 8)[0] == 500
    assert run_path(server, "scalars/0.bin").read_bytes() == b"12345678"


def _wait_tmp_empty(tmp_dir, timeout=5):
    deadline = time.monotonic() + timeout
    while list(tmp_dir.iterdir()) and time.monotonic() < deadline:
        time.sleep(.01)
    leftover = list(tmp_dir.iterdir())
    assert not leftover, leftover


def test_objects_validation_upload_and_concurrency(server):
    data = b"object bytes"
    sha = hashlib.sha256(data).hexdigest()
    missing = "/v2/objects/missing"
    for obj in ({"objects": "bad"}, {"objects": ["bad"]}, {"objects": [None]}, {}, []):
        assert request(server, missing, "POST", json.dumps(obj).encode())[0] == 422
    assert request(server, missing, "POST", json.dumps({"objects": [sha] * 10001}).encode())[0] == 422
    assert request(server, missing, "POST", json.dumps({"objects": [sha, sha]}).encode())[1] == {"missing": [sha]}
    path = "/v2/objects/" + sha
    assert request(server, path, "PUT", b"wrong")[0:2] == (
        422, {"error": "hash mismatch", "sha256": hashlib.sha256(b"wrong").hexdigest()})
    assert not layout.object_path(server.root, sha).exists()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(server, path, "PUT", data), range(2)))
    assert sorted(result[0] for result in results) == [200, 201]
    stored = layout.object_path(server.root, sha)
    assert stored.read_bytes() == data
    os.utime(stored, (1, 1))
    assert request(server, path, "PUT", data)[1] == {"sha256": sha, "created": False}
    assert stored.stat().st_mtime > 1
    assert request(server, missing, "POST", json.dumps({"objects": [sha]}).encode())[1] == {"missing": []}
    _wait_tmp_empty(server.tmp_dir)


def _is_object_prefix_dir(path):
    path = Path(path)
    return path.parent.name == layout.OBJECTS_DIRNAME and len(path.name) == 2


def _payloads_same_prefix(n=2):
    found = {}
    for index in range(1, 10000):
        data = b"same-pfx-" + str(index).encode()
        prefix = hashlib.sha256(data).hexdigest()[:2]
        found.setdefault(prefix, []).append(data)
        if len(found[prefix]) == n:
            return found[prefix]
    raise AssertionError("could not find payloads with the same object prefix")


def _payloads_distinct_prefixes(n=2):
    found = []
    seen = set()
    for index in range(1, 1000):
        data = b"obj-" + str(index).encode()
        prefix = hashlib.sha256(data).hexdigest()[:2]
        if prefix in seen:
            continue
        seen.add(prefix)
        found.append(data)
        if len(found) == n:
            return found
    raise AssertionError("could not find payloads with distinct object prefixes")


def test_concurrent_puts_distinct_prefixes_overlap_fsync(server, monkeypatch):
    first, second = _payloads_distinct_prefixes()
    assert hashlib.sha256(first).hexdigest()[:2] != hashlib.sha256(second).hexdigest()[:2]
    # keys.touch fsyncs on first use; consume that so the patched fsync is the
    # object temp file (same pattern as test_append_write_failure_rolls_back).
    assert request(server, "/v2/whoami")[0] == 200
    arrived = threading.Event()
    count_lock = threading.Lock()
    inside = [0]
    original = ingest.os.fsync

    def gated(fd):
        with count_lock:
            inside[0] += 1
            if inside[0] >= 2:
                arrived.set()
        if not arrived.wait(5):
            raise TimeoutError("object fsyncs did not overlap")
        return original(fd)

    monkeypatch.setattr(ingest.os, "fsync", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda data: request(server, "/v2/objects/" + hashlib.sha256(data).hexdigest(), "PUT", data),
            (first, second),
        ))
    assert [result[0] for result in results] == [201, 201]
    assert arrived.is_set()
    for data in (first, second):
        stored = layout.object_path(server.root, hashlib.sha256(data).hexdigest())
        assert stored.read_bytes() == data


def test_concurrent_puts_distinct_prefixes_overlap_dir_fsync(server, monkeypatch):
    first, second = _payloads_distinct_prefixes()
    assert hashlib.sha256(first).hexdigest()[:2] != hashlib.sha256(second).hexdigest()[:2]
    arrived = threading.Event()
    count_lock = threading.Lock()
    inside = [0]
    original = layout.fsync_dir

    def gated(path):
        if _is_object_prefix_dir(path):
            with count_lock:
                inside[0] += 1
                if inside[0] >= 2:
                    arrived.set()
            if not arrived.wait(3):
                raise TimeoutError("prefix directory fsyncs did not overlap")
        return original(path)

    monkeypatch.setattr(layout, "fsync_dir", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda data: request(server, "/v2/objects/" + hashlib.sha256(data).hexdigest(), "PUT", data),
            (first, second),
        ))
    assert [result[0] for result in results] == [201, 201]
    assert arrived.is_set()
    for data in (first, second):
        stored = layout.object_path(server.root, hashlib.sha256(data).hexdigest())
        assert stored.read_bytes() == data


def test_concurrent_puts_same_prefix_serialize_dir_fsync(server, monkeypatch):
    first, second = _payloads_same_prefix()
    assert hashlib.sha256(first).hexdigest()[:2] == hashlib.sha256(second).hexdigest()[:2]
    assert hashlib.sha256(first).hexdigest() != hashlib.sha256(second).hexdigest()
    overlapped = threading.Event()
    count_lock = threading.Lock()
    inside = [0]
    original = layout.fsync_dir

    def gated(path):
        if _is_object_prefix_dir(path):
            with count_lock:
                inside[0] += 1
                if inside[0] > 1:
                    overlapped.set()
            try:
                overlapped.wait(0.2)
                return original(path)
            finally:
                with count_lock:
                    inside[0] -= 1
        return original(path)

    monkeypatch.setattr(layout, "fsync_dir", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda data: request(server, "/v2/objects/" + hashlib.sha256(data).hexdigest(), "PUT", data),
            (first, second),
        ))
    assert sorted(result[0] for result in results) == [201, 201]
    assert not overlapped.is_set()
    for data in (first, second):
        stored = layout.object_path(server.root, hashlib.sha256(data).hexdigest())
        assert stored.read_bytes() == data


def test_concurrent_puts_same_digest_one_created(server):
    data = b"same-digest-object"
    sha = hashlib.sha256(data).hexdigest()
    path = "/v2/objects/" + sha
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(server, path, "PUT", data), range(2)))
    assert sorted(result[0] for result in results) == [200, 201]
    stored = layout.object_path(server.root, sha)
    assert stored.is_file()
    assert stored.read_bytes() == data
    _wait_tmp_empty(server.tmp_dir)


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_unsafe_object_destination(server, tmp_path, kind):
    data = b"safe bytes"
    sha = hashlib.sha256(data).hexdigest()
    path = layout.object_path(server.root, sha)
    path.parent.mkdir(parents=True)
    outside = tmp_path / "target"
    outside.write_bytes(b"unchanged")
    if kind == "symlink":
        path.symlink_to(outside)
    else:
        path.mkdir()
    assert request(server, "/v2/objects/" + sha, "PUT", data)[0] == 500
    assert outside.read_bytes() == b"unchanged"
    assert path.is_symlink() if kind == "symlink" else path.is_dir()


def test_commit_validation_sizes_status_and_success(server):
    put_meta(server)
    for obj in ({}, {"status": "running", "fingerprint": "bad", "sizes": {}},
                {"status": "running", "fingerprint": "a" * 64, "sizes": {"notes.md": 0}},
                {"status": "running", "fingerprint": "a" * 64, "sizes": {"meta.json": True}}):
        assert request(server, "/v2/runs/run-1/commit", "POST", json.dumps(obj).encode())[0] == 422
    size = run_path(server, "meta.json").stat().st_size
    status, obj, _ = commit(server, sizes={"meta.json": 0, "events.jsonl": 2})
    assert status == 409 and obj == {"error": "size mismatch", "sizes": {"meta.json": size, "events.jsonl": None}}
    assert commit(server, status="finished")[0:2] == (
        409, {"error": "status does not match meta.json", "status": "running"})
    assert commit(server, sizes={"meta.json": size})[1] == {"complete": True, "fingerprint": "a" * 64}
    remote = json.loads(run_path(server, "remote.json").read_bytes())
    assert remote == {"owner": "admin", "owner_key": "one", "status": "running",
                      "fingerprint": "a" * 64, "last_sync_at": remote["last_sync_at"],
                      "sizes": {"meta.json": size}}
    layout.parse_timestamp(remote["last_sync_at"])


def test_real_writer_round_trip(server, tmp_path):
    source = tmp_path / "source"
    writer = RunWriter.create(source, run_id="run-1", meta={"project": "demo", "config": {"lr": .1}})
    try:
        for step in range(12):
            writer.write_scalars([(step, {"loss": 1 / (step + 1), "accuracy": step / 12, "extra": step * 2})])
        writer.add_media("sample", 1, b"media bytes", "text/plain", "caption")
        artifact = tmp_path / "weights.bin"
        artifact.write_bytes(b"checkpoint")
        writer.add_artifact(artifact, "weights", "model", {}, copy=True)
        writer.finish("finished", {"loss": .1})
    finally:
        writer.close()
    src = source / "runs" / "run-1"
    # Include a valid overview explicitly even for this small run.
    overview = Overview()
    for step in range(12):
        overview.add(step, 1 / (step + 1))
    (src / "scalars" / "0.ovw").write_bytes(overview.to_bytes())
    blobs = {path.name: path.read_bytes() for path in (source / "objects").glob("*/*") if layout.valid_sha(path.name)}
    assert len(blobs) == 2
    hashes = list(blobs)
    assert request(server, "/v2/objects/missing", "POST", json.dumps({"objects": hashes}).encode())[1] == {"missing": hashes}
    for sha, data in blobs.items():
        assert request(server, "/v2/objects/" + sha, "PUT", data)[0] == 201
    assert request(server, "/v2/runs/run-1/files/meta.json", "PUT", (src / "meta.json").read_bytes())[0] == 200
    files = {path.relative_to(src).as_posix(): path.read_bytes() for path in src.rglob("*")
             if path.is_file() and (ingest._ATOMIC.fullmatch(path.relative_to(src).as_posix())
                                    or ingest._APPEND.fullmatch(path.relative_to(src).as_posix()))}
    for name, data in files.items():
        if ingest._APPEND.fullmatch(name):
            assert append(server, data, name=name)[0] == 200
    for name, data in files.items():
        if ingest._ATOMIC.fullmatch(name):
            assert request(server, "/v2/runs/run-1/files/" + name, "PUT", data)[0] == 200
    fingerprint = run_fingerprint(source, "run-1")
    assert commit(server, "finished", {name: len(data) for name, data in files.items()}, fingerprint)[0] == 200
    original, replica = RunReader(source, "run-1"), RunReader(server.root, "run-1")
    assert original.meta() == replica.meta()
    assert original.keys() == replica.keys()
    for key in original.keys():
        assert original.series(key) == replica.series(key)
    assert original.events() == replica.events()
    assert original.media() == replica.media()
    assert original.artifacts() == replica.artifacts()
    assert run_fingerprint(server.root, "run-1") == fingerprint
    for name, data in files.items():
        assert run_path(server, name).read_bytes() == data
    for sha, data in blobs.items():
        assert layout.object_path(server.root, sha).read_bytes() == data
    assert not run_path(server, "run.lock").exists()


@pytest.mark.parametrize("change", ["append", "truncate", "atomic"])
def test_state_hashes_snapshot_without_blocking_writes(server, monkeypatch, change):
    put_meta(server)
    original = b"{}\n" * (1024 * 1024)
    assert append(server, original, name="events.jsonl")[0] == 200
    before = {name: run_path(server, name).read_bytes() for name in ("meta.json", "events.jsonl")}
    old_remote = json.loads(run_path(server, "remote.json").read_bytes())
    entered, release = threading.Event(), threading.Event()
    handles = []
    hash_prefix = ingest._hash_prefix
    send_json = ingest.IngestHandler._json

    def paused(handle, size):
        handles.append(handle)
        entered.set()
        assert release.wait(5)
        return hash_prefix(handle, size)

    def check_closed(self, payload, *args, **kwargs):
        if "files" in payload:
            assert handles and all(handle.closed for handle in handles)
        return send_json(self, payload, *args, **kwargs)

    monkeypatch.setattr(ingest, "_hash_prefix", paused)
    monkeypatch.setattr(ingest.IngestHandler, "_json", check_closed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        state = pool.submit(request, server, "/v2/runs/run-1/state")
        try:
            assert entered.wait(3)
            if change == "append":
                assert append(server, b'{"new":true}\n', len(original), name="events.jsonl")[0] == 200
            elif change == "truncate":
                assert append(server, b"[]\n", name="events.jsonl", query="&truncate=1")[0] == 200
            else:
                assert put_meta(server, status="finished")[0] == 200
            assert commit(server, status="finished" if change == "atomic" else "running")[0] == 200
            assert not state.done()
        finally:
            release.set()
        status, payload, _ = state.result(timeout=5)
    assert status == 200
    assert payload["remote"] == {**old_remote, "owner": "one", "owner_user": "admin"}
    assert payload["files"] == {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                                for name, data in before.items()}


def test_state_batches_under_file_descriptor_limit(server, monkeypatch):
    resource = pytest.importorskip("resource")
    put_meta(server)
    directory = run_path(server, "scalars")
    directory.mkdir()
    contents = {"meta.json": run_path(server, "meta.json").read_bytes()}
    for index in range(400):
        name = f"scalars/{index}.bin"
        contents[name] = index.to_bytes(8, "little")
        run_path(server, name).write_bytes(contents[name])

    opened, counts = [], []
    original = layout.open_nofollow
    lock = server.run_lock("run-1")
    def tracked(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        if kwargs.get("buffering") == 0:
            assert lock.locked()
            opened.append(handle)
            counts.append(sum(not item.closed for item in opened))
            assert counts[-1] <= 64
        return handle
    monkeypatch.setattr(layout, "open_nofollow", tracked)
    previous = resource.getrlimit(resource.RLIMIT_NOFILE)
    limit = 200 if previous[0] == resource.RLIM_INFINITY else min(200, previous[0])
    if limit < 100:
        pytest.skip("descriptor limit leaves too little room for 64-file batches and pytest")
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (limit, previous[1]))
        status, payload, _ = request(server, "/v2/runs/run-1/state")
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, previous)
    assert status == 200
    assert payload["files"] == {
        name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in contents.items()
    }
    assert max(counts) == 64
    assert all(handle.closed for handle in opened)


def test_responses_sent_after_write_locks_released(server, monkeypatch):
    run_lock = server.run_lock("run-1")
    original = ingest.IngestHandler._json
    sent = []

    def checked(self, payload, status=200, headers=None):
        # Keep a strong reference to the per-run lock, which is weakly cached.
        assert not run_lock.locked()
        with server.object_locks_lock:
            assert not any(lock.locked() for lock in server.object_locks.values())
        sent.append(status)
        return original(self, payload, status, headers)

    monkeypatch.setattr(ingest.IngestHandler, "_json", checked)
    assert put_meta(server)[0] == 200
    assert append(server, b"12345678")[0] == 200
    assert append(server, b"abcdefgh")[0] == 409  # overlap mismatch under lock
    assert append(server, b"abcdefgh", query="&truncate=1")[0] == 200
    assert commit(server)[0] == 200
    assert commit(server, sizes={"events.jsonl": 1})[0] == 409
    assert request(server, "/v2/runs/run-1/state")[0] == 200
    data = b"object"
    path = "/v2/objects/" + hashlib.sha256(data).hexdigest()
    assert request(server, path, "PUT", data)[0] == 201
    assert request(server, path, "PUT", data)[0] == 200
    assert sent == [200, 200, 409, 200, 200, 409, 200, 201, 200]


@pytest.mark.parametrize("name,data", [("events.jsonl", b"{}\n"), ("scalars/0.bin", b"12345678")])
@pytest.mark.parametrize("truncate", [False, True])
def test_hard_linked_append_refused_and_state_skips(server, tmp_path, caplog, name, data, truncate):
    put_meta(server)
    outside = tmp_path / "outside"
    outside.write_bytes(data)
    path = run_path(server, name)
    path.parent.mkdir(exist_ok=True)
    os.link(outside, path)
    assert append(server, data, name=name, query="&truncate=1" if truncate else "")[0:2] == (
        409, {"error": "refusing hard-linked file on server", "path": name})
    assert path.read_bytes() == outside.read_bytes() == data
    assert path.stat().st_ino == outside.stat().st_ino
    status, payload, _ = request(server, "/v2/runs/run-1/state")
    assert status == 200 and name not in payload["files"]
    assert "refusing hard-linked append target" in caplog.text
    assert "skipping hard-linked state file" in caplog.text


@pytest.mark.parametrize("name", ["meta.json", "scalars/index.json", "media/index.json", "scalars/0.ovw"])
def test_state_skips_hard_linked_atomic_files(server, tmp_path, name):
    put_meta(server)
    path = run_path(server, name)
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        path.write_bytes(b"{}")
    os.link(path, tmp_path / "linked")
    status, payload, _ = request(server, "/v2/runs/run-1/state")
    assert status == 200 and name not in payload["files"]


@pytest.mark.parametrize("name,data", [("events.jsonl", b"{}\n"), ("scalars/0.bin", b"12345678")])
def test_missing_append_with_gap_creates_nothing(server, name, data):
    put_meta(server)
    assert append(server, data, offset=8, name=name)[0:2] == (409, {"size": 0})
    assert not run_path(server, name).exists()
    assert not run_path(server, "scalars").exists()
    assert append(server, data, offset=0, name=name)[0:2] == (200, {"size": len(data)})
    assert run_path(server, name).read_bytes() == data


def test_claim_keeps_existing_remote_metadata(server):
    put_meta(server)
    remote = {"owner": None, "status": "running", "sizes": {"meta.json": 42},
              "fingerprint": "a" * 64, "last_sync_at": layout.format_timestamp(), "extra": {"x": 1}}
    run_path(server, "remote.json").write_text(json.dumps(remote))
    assert put_meta(server)[0] == 200
    assert json.loads(run_path(server, "remote.json").read_bytes()) == {
        **remote, "owner": "admin", "owner_key": "one"}


@pytest.mark.parametrize("owner", [None, "other"])
def test_new_run_discards_leftover_remote_metadata(server, owner):
    run_path(server).mkdir(parents=True)
    remote = {"owner": owner, "status": "finished", "sizes": {"meta.json": 42},
              "fingerprint": "a" * 64, "last_sync_at": layout.format_timestamp()}
    run_path(server, "remote.json").write_text(json.dumps(remote))
    assert put_meta(server)[0] == 200
    assert json.loads(run_path(server, "remote.json").read_bytes()) == {"owner": "admin", "owner_key": "one"}


@pytest.mark.parametrize("path", ["/healthz", "/v2/whoami", "/v2/runs/run-1/state"])
@pytest.mark.parametrize("header", [("Content-Length", "100"), ("Content-Length", "0"),
                                    ("Transfer-Encoding", "chunked")])
def test_body_on_read_route_closes_keep_alive(server, monkeypatch, path, header):
    put_meta(server)
    # Exercise the future keep-alive case, not just today's HTTP/1.0 default.
    monkeypatch.setattr(ingest.IngestHandler, "protocol_version", "HTTP/1.1")
    with socket.create_connection(server.server_address[:2], timeout=3) as client:
        headers = auth(server) + [header, ("Connection", "keep-alive")]
        head = b"".join(f"{name}: {value}\r\n".encode() for name, value in headers)
        client.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n".encode() + head + b"\r\n")
        response = http.client.HTTPResponse(client)
        response.begin()
        assert response.status == 200
        assert response.getheader("Connection") == "close"
        response.read()
        assert client.recv(1) == b""


@pytest.mark.parametrize("verbose", [False, True])
def test_internal_error_logs_type_message_without_request(server, monkeypatch, caplog, verbose):
    server.verbose = verbose

    def failed(*args):
        raise OSError("disk write failed")

    monkeypatch.setattr(ingest.IngestHandler, "_atomic", failed)
    assert request(server, "/v2/runs/run-1/files/meta.json", "PUT", b"PRIVATE BODY",
                   headers={"X-Private": "PRIVATE HEADER"})[0:2] == (500, {"error": "internal error"})
    entry = next(r for r in caplog.records if "ingest request failed" in r.message)
    assert "OSError: disk write failed" in entry.message
    assert bool(entry.exc_info) == verbose
    assert all(secret not in caplog.text for secret in (server.test_key, "PRIVATE BODY", "PRIVATE HEADER"))


@pytest.mark.parametrize("error", [ValueError, TypeError])
def test_atomic_only_overview_maps_validation_exceptions(server, monkeypatch, error):
    put_meta(server)

    def failed(*args):
        raise error("validation failed")

    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_json_object", failed)
        assert request(server, "/v2/runs/run-1/files/scalars/index.json", "PUT", b"{}")[0:2] == (
            500, {"error": "internal error"})
    monkeypatch.setattr(Overview, "from_bytes", failed)
    assert request(server, "/v2/runs/run-1/files/scalars/0.ovw", "PUT", b"bad")[0:2] == (
        422, {"error": "invalid overview"})


@pytest.mark.parametrize("mask", [0o022, 0o027, 0o077])
def test_uploaded_store_modes_follow_startup_umask(tmp_path, mask):
    previous = os.umask(mask)
    try:
        with running(tmp_path / "data") as httpd:
            _, httpd.test_key = httpd.keys.create("one")
            assert put_meta(httpd)[0] == 200
            assert append(httpd, b"12345678")[0] == 200
            assert append(httpd, b"{}\n", name="events.jsonl", query="&truncate=1")[0] == 200
            assert append(httpd, b"abcdefgh", query="&truncate=1")[0] == 200
            data = b"object"
            sha = hashlib.sha256(data).hexdigest()
            assert request(httpd, "/v2/objects/" + sha, "PUT", data)[0] == 201
            paths = [run_path(httpd, name) for name in ("meta.json", "remote.json", "scalars/0.bin", "events.jsonl")]
            paths.append(layout.object_path(httpd.root, sha))
            assert all(stat.S_IMODE(path.stat().st_mode) == 0o666 & ~mask for path in paths)
    finally:
        os.umask(previous)


def test_startup_reads_proc_umask_without_changing_it(tmp_path, monkeypatch):
    original = Path.read_text
    reads = []
    def read_status(path, *args, **kwargs):
        if path == Path("/proc/self/status"):
            reads.append(path)
            return "Name:\tpython\nUmask:\t0027\nState:\tR (running)\n"
        return original(path, *args, **kwargs)
    def forbidden(mask):
        pytest.fail("startup changed the process umask instead of reading procfs")
    monkeypatch.setattr(Path, "read_text", read_status)
    monkeypatch.setattr(os, "umask", forbidden)
    with make_server(tmp_path / "data", port=0) as httpd:
        assert httpd.store_file_mode == 0o640
    assert reads == [Path("/proc/self/status")]


@pytest.mark.parametrize("status", [None, "Name:\tpython\n", "Umask:\tinvalid\n"])
def test_umask_falls_back_when_proc_unavailable(monkeypatch, status):
    def read_status(path):
        assert path == Path("/proc/self/status")
        if status is None:
            raise FileNotFoundError(path)
        return status
    calls = []
    def umask(mask):
        calls.append(mask)
        return 0o077 if len(calls) == 1 else 0
    monkeypatch.setattr(Path, "read_text", read_status)
    monkeypatch.setattr(os, "umask", umask)
    assert app._read_umask() == 0o077
    assert calls == [0, 0o077]


@pytest.mark.parametrize("exists,route,method,length", [
    (True, "files/meta.json", "PUT", 16 * 1024 * 1024),
    (True, "files/scalars/index.json", "PUT", 16 * 1024 * 1024),
    (True, "append/events.jsonl?offset=0", "POST", 64 * 1024 * 1024),
    (True, "commit", "POST", 1024 * 1024),
    (False, "files/scalars/index.json", "PUT", 16 * 1024 * 1024),
    (False, "append/events.jsonl?offset=0", "POST", 64 * 1024 * 1024),
    (False, "commit", "POST", 1024 * 1024),
])
def test_ownership_rejected_without_receiving_body(server, route, method, length, exists):
    other_key = foreign_key(server)
    if exists:
        put_meta(server)
    headers = [("Authorization", "Bearer " + other_key), ("Content-Length", str(length))]
    status, payload, headers = raw(server, method, "/v2/runs/run-1/" + route, headers)
    assert (status, payload) == ((409, {"error": "run owned by another user",
                                        "owner": "admin", "owner_user": "admin"}) if exists
                                 else (404, {"error": "unknown run"}))
    assert headers["Connection"] == "close"
    assert not list(server.tmp_dir.iterdir())


@pytest.mark.parametrize("route,method,body", [
    ("files/meta.json", "PUT", b'{"run_id":"run-1","status":"running"}'),
    ("files/scalars/index.json", "PUT", b"{}"),
    ("append/events.jsonl?offset=0", "POST", b"{}\n"),
    ("commit", "POST", b'{"status":"running","fingerprint":"' + b"a" * 64 + b'","sizes":{}}'),
])
def test_ownership_rechecked_after_body(server, monkeypatch, route, method, body):
    put_meta(server)
    read_body = ingest.IngestHandler._read_body

    def change_owner(self, *args, **kwargs):
        body = read_body(self, *args, **kwargs)
        run_path(server, "remote.json").write_text('{"owner":"other"}')
        return body

    monkeypatch.setattr(ingest.IngestHandler, "_read_body", change_owner)
    assert request(server, "/v2/runs/run-1/" + route, method, body)[0:2] == (
        409, {"error": "run owned by another user", "owner": "other", "owner_user": "other"})
    assert not run_path(server, "events.jsonl").exists()

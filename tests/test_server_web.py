"""Dashboard login, sessions, CSRF, and routing on the ingest server."""
from contextlib import contextmanager
import hashlib
import http.client
import json
import threading

import pytest

from agenticml.server import webauth as webauth_mod
from agenticml.server.app import make_server
from agenticml.server.keys import KeyStore
from agenticml.store import layout
from agenticml.store.objects import put_bytes
from agenticml.ui import server as ui_server
from server_db_helpers import default_workspace_id


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


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        _, httpd.sync_key = httpd.keys.create("sync-box", "sync")
        yield httpd


def call(server, path, method="GET", body=None, headers=None):
    conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def csrf(server, **extra):
    host = f"127.0.0.1:{server.server_address[1]}"
    headers = {
        "Origin": f"http://{host}",
        "X-AgenticML-Request": "1",
        "Content-Type": "application/json",
    }
    headers.update(extra)
    return headers


def cookie_parts(header):
    first, *attrs = [part.strip() for part in header.split(";")]
    name, value = first.split("=", 1)
    flags = {}
    for attr in attrs:
        if "=" in attr:
            key, val = attr.split("=", 1)
            flags[key.lower()] = val
        else:
            flags[attr.lower()] = True
    return name, value, flags


def login(server, key=None, **headers):
    secret = server.admin_key if key is None else key
    status, hdrs, body = call(
        server, "/login", "POST", json.dumps({"key": secret}).encode(), csrf(server, **headers))
    payload = json.loads(body) if body else None
    return status, payload, hdrs


def authed(server, path, method="GET", body=None, extra=None, cookie=None):
    if cookie is None:
        _, _, hdrs = login(server)
        cookie = hdrs["Set-Cookie"]
    name, token, _ = cookie_parts(cookie)
    headers = {"Cookie": f"{name}={token}"}
    if extra:
        headers.update(extra)
    if method in ("POST", "PUT") and "X-AgenticML-Request" not in headers:
        headers.update(csrf(server))
    return call(server, path, method, body, headers) + (cookie,)


def test_login_admin_cookie_and_failures(server, caplog):
    caplog.set_level("INFO", logger="agenticml.server.webauth")
    status, payload, hdrs = login(server)
    assert status == 200
    assert payload == {"ok": True, "user": "admin", "role": "admin"}
    name, token, flags = cookie_parts(hdrs["Set-Cookie"])
    assert name == "aml_session"
    assert token
    assert flags["httponly"] is True
    assert flags["samesite"] == "Strict"
    assert flags["path"] == "/"
    assert flags["max-age"] == "604800"
    assert "secure" not in flags
    assert server.admin_key not in token
    assert server.admin_key not in json.dumps(payload)
    assert hdrs.get("Cache-Control") == "no-store"
    assert "Content-Security-Policy" in hdrs
    assert hdrs.get("X-Content-Type-Options") == "nosniff"

    status, payload, hdrs = login(server, "wrong-key")
    assert status == 401 and payload == {"error": "invalid key"}
    assert "Set-Cookie" not in hdrs
    assert server.admin_key not in json.dumps(payload)
    assert server.admin_key not in caplog.text

    status, payload, hdrs = login(server, server.sync_key)
    assert status == 403
    assert payload == {"error": "this is a sync key; sign in with an admin key"}
    assert "Set-Cookie" not in hdrs
    assert server.sync_key not in json.dumps(payload)
    assert server.sync_key not in caplog.text

    missing = csrf(server)
    del missing["X-AgenticML-Request"]
    status, _, body = call(server, "/login", "POST", json.dumps({"key": server.admin_key}).encode(), missing)
    assert status == 403
    assert json.loads(body) == {"error": "cross-site request refused"}

    wrong_origin = csrf(server, Origin="http://evil.example")
    status, _, body = call(
        server, "/login", "POST", json.dumps({"key": server.admin_key}).encode(), wrong_origin)
    assert status == 403
    assert json.loads(body) == {"error": "cross-site request refused"}

    no_origin = csrf(server)
    del no_origin["Origin"]
    status, _, body = call(
        server, "/login", "POST", json.dumps({"key": server.admin_key}).encode(), no_origin)
    assert status == 403

    status, _, body = call(server, "/login", "POST", b"x" * 4097, csrf(server))
    assert status == 413
    assert json.loads(body) == {"error": "payload too large"}
    assert server.admin_key not in body.decode("utf-8", "replace")


def test_https_cookie_flags(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", public_url="https://ml.example.com") as httpd:
        _, key = httpd.keys.create("admin", "admin")
        status, payload, hdrs = login(httpd, key)
        assert status == 200
        name, _, flags = cookie_parts(hdrs["Set-Cookie"])
        assert name == "__Host-aml_session"
        assert flags["secure"] is True
        assert flags["httponly"] is True
        assert flags["samesite"] == "Strict"
        assert flags["path"] == "/"
        assert flags["max-age"] == "604800"


def test_gate_and_login_assets(server):
    status, hdrs, _ = call(server, "/")
    assert status == 303
    assert hdrs["Location"] == "/login"
    assert call(server, "/static/app.js")[0] == 303
    status, _, body = call(server, "/api/w/default/runs")
    assert status == 401
    assert json.loads(body) == {"error": "login required"}
    sha = "a" * 64
    status, _, body = call(server, "/api/w/default/objects/" + sha)
    assert status == 401
    assert json.loads(body) == {"error": "login required"}

    status, hdrs, body = call(server, "/login")
    assert status == 200
    assert b"Sign in" in body
    assert hdrs.get("Cache-Control") == "no-store"
    assert hdrs.get("Content-Security-Policy")
    assert call(server, "/static/login.js")[0] == 200
    assert call(server, "/static/app.css")[0] == 200
    assert call(server, "/static/InstrumentSans.woff2")[0] == 200
    assert call(server, "/static/JetBrainsMono.woff2")[0] == 200

    status, _, body, cookie = authed(server, "/api/w/default/runs")
    assert status == 200
    assert json.loads(body) == []
    status, hdrs, _, _ = authed(server, "/", cookie=cookie)
    assert status == 303 and hdrs["Location"] == "/w/default/"
    assert authed(server, "/w/default/", cookie=cookie)[0] == 200
    assert authed(server, "/static/app.js", cookie=cookie)[0] == 200
    status, hdrs, _ = authed(server, "/login", cookie=cookie)[:3]
    assert status == 303 and hdrs["Location"] == "/"
    status, _, body, _ = authed(server, "/missing-page", cookie=cookie)
    assert status == 404
    assert json.loads(body) == {"error": "not found"}


def test_removed_22_aliases_are_unknown_path_404(server):
    status, headers, body, cookie = authed(server, "/api/no-such")
    unknown = (status, headers, body)
    assert status == 404 and json.loads(body) == {"error": "not found"}
    skip = {"date", "set-cookie"}
    def snapshot(status, headers, body):
        filtered = {key.lower(): value for key, value in headers.items() if key.lower() not in skip}
        return status, filtered, body
    for path in (
        "/api/runs", "/api/server/keys", "/api/server/users",
        "/api/server/audit", "/api/server/session", "/api/server/account/password",
    ):
        got = authed(server, path, cookie=cookie)[:3]
        assert snapshot(*got) == snapshot(*unknown), path


def test_sessions_expiry_cap_revoke_logout(server, monkeypatch):
    clock = [1_000.0]
    server.web.clock = lambda: clock[0]
    monkeypatch.setattr(webauth_mod, "SESSION_IDLE", 100)
    monkeypatch.setattr(webauth_mod, "SESSION_MAX_AGE", 10_000)
    monkeypatch.setattr(webauth_mod, "SESSION_CAP", 2)

    status, _, hdrs = login(server)
    cookie = hdrs["Set-Cookie"]
    name, token, _ = cookie_parts(cookie)
    assert authed(server, "/api/w/default/runs", cookie=cookie)[0] == 200
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    first_seen = server.web.sessions.list()[digest]["last_seen"]
    clock[0] += 30
    assert authed(server, "/api/w/default/runs", cookie=cookie)[0] == 200
    assert server.web.sessions.list()[digest]["last_seen"] == first_seen
    clock[0] += 31
    assert authed(server, "/api/w/default/runs", cookie=cookie)[0] == 200
    assert server.web.sessions.list()[digest]["last_seen"] == clock[0]

    clock[0] += 100
    status, _, body, _ = authed(server, "/api/w/default/runs", cookie=cookie)
    assert status == 401
    assert json.loads(body) == {"error": "login required"}

    monkeypatch.setattr(webauth_mod, "SESSION_MAX_AGE", 50)
    status, _, hdrs = login(server)
    cookie = hdrs["Set-Cookie"]
    created = server.web.sessions.list()[hashlib.sha256(
        cookie_parts(cookie)[1].encode("ascii")).hexdigest()]["created"]
    clock[0] = created + 51
    status, hdrs, _ = authed(server, "/", cookie=cookie)[:3]
    assert status == 303
    assert hdrs["Location"] == "/login"

    cookies = []
    for _ in range(3):
        _, _, hdrs = login(server)
        cookies.append(hdrs["Set-Cookie"])
    assert len(server.web.sessions.list()) == 2
    assert authed(server, "/api/w/default/runs", cookie=cookies[0])[0] == 401
    assert authed(server, "/api/w/default/runs", cookie=cookies[2])[0] == 200

    _, _, hdrs = login(server)
    cookie = hdrs["Set-Cookie"]
    KeyStore(server.data_dir / "server", workspace_id=default_workspace_id(server.data_dir / "server")).revoke("admin")
    status, _, body, _ = authed(server, "/api/w/default/runs", cookie=cookie)
    assert status == 401
    _, server.admin_key = server.keys.create("admin", "admin")
    assert authed(server, "/api/w/default/runs", cookie=cookie)[0] == 401

    status, _, hdrs = login(server)
    cookie = hdrs["Set-Cookie"]
    status, hdrs, _, _ = authed(server, "/logout", "POST", cookie=cookie)
    assert status == 204
    name, _, flags = cookie_parts(hdrs["Set-Cookie"])
    assert flags["max-age"] == "0"
    assert authed(server, "/api/w/default/runs", cookie=cookie)[0] == 401
    status, _, _, _ = authed(server, "/logout", "POST", cookie=cookie)
    assert status == 204


def test_session_endpoint_and_local_ui(server, tmp_path):
    status, _, body = call(server, "/api/account/session")
    assert status == 401
    assert json.loads(body) == {"error": "login required"}
    status, _, body, _ = authed(server, "/api/account/session")
    assert status == 200
    assert json.loads(body) == {
        "server": True, "user": "admin", "role": "admin", "via": "key", "auth": "password",
        "key": "admin",
        "scope": "admin",
        "can": {"notes": True, "own_keys": True, "all_keys": True, "users": True, "audit": True},
        "password_set": False, "identity_proxy": False,
        "workspaces": [{"slug": "default", "name": "Default", "role": "owner"}],
        "server_admin": True, "open_signup": False}

    httpd = ui_server.make_server(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        assert call(httpd, "/login")[0] == 404
        assert call(httpd, "/api/account/session")[0] == 404
        assert call(httpd, "/api/runs")[0] == 200
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_csrf_and_host_with_public_url(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    public = "https://ml.example.com"
    with running(tmp_path / "data", public_url=public) as httpd:
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        meta = json.dumps({"run_id": "run-1", "status": "running"}).encode()
        auth = {"Authorization": "Bearer " + httpd.admin_key}
        assert call(httpd, "/v2/runs/run-1/files/meta.json", "PUT", meta, auth)[0] == 200
        _, _, hdrs = login(httpd)
        cookie = hdrs["Set-Cookie"]
        name, token, _ = cookie_parts(cookie)
        note = json.dumps({"text": "hello", "revision": None}).encode()

        def put(extra):
            headers = {
                "Cookie": f"{name}={token}",
                "Content-Type": "application/json",
                "X-AgenticML-Request": "1",
                "Origin": "https://ml.example.com",
                "Host": "ml.example.com",
            }
            headers.update(extra)
            return call(httpd, "/api/w/default/runs/run-1/notes", "PUT", note, headers)

        assert put({})[0] == 200
        assert put({"Origin": "http://evil.example"})[0] == 403
        missing_origin = {
            "Cookie": f"{name}={token}",
            "Content-Type": "application/json",
            "X-AgenticML-Request": "1",
            "Host": "ml.example.com",
        }
        assert call(httpd, "/api/w/default/runs/run-1/notes", "PUT", note, missing_origin)[0] == 403
        assert put({"X-AgenticML-Request": "0"})[0] == 403
        assert put({"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert call(httpd, "/api/w/default/runs", headers={"Host": "ml.example.com", "Cookie": f"{name}={token}"})[0] == 200
        assert call(httpd, "/api/w/default/runs", headers={"Host": "localhost", "Cookie": f"{name}={token}"})[0] == 200
        assert call(httpd, "/api/w/default/runs", headers={"Host": "evil.example", "Cookie": f"{name}={token}"})[0] == 403


def test_routing_healthz_v2_and_bearer_ignored(server):
    assert call(server, "/healthz")[0] == 200
    assert json.loads(call(server, "/healthz")[2]) == {"ok": True}
    status, _, body = call(server, "/v2/whoami")
    assert status == 401
    _, _, hdrs = login(server)
    name, token, _ = cookie_parts(hdrs["Set-Cookie"])
    status, _, body = call(server, "/v2/whoami", headers={"Cookie": f"{name}={token}"})
    assert status == 401
    status, _, body = call(server, "/v2/whoami", headers={"Authorization": "Bearer " + server.admin_key})
    assert status == 200
    assert json.loads(body)["key"] == "admin"
    status, _, body = call(
        server, "/api/w/default/runs", headers={"Authorization": "Bearer " + server.admin_key})
    assert status == 401
    assert json.loads(body) == {"error": "login required"}


def test_last_sync_lineage_and_notes(server):
    meta = json.dumps({
        "run_id": "run-1", "status": "finished", "name": "demo", "project": "demo",
        "lineage": {"commit": "a" * 40, "project_key": "demo"},
    }).encode()
    headers = {"Authorization": "Bearer " + server.sync_key}
    assert call(server, "/v2/runs/run-1/files/meta.json", "PUT", meta, headers)[0] == 200
    size = (server.root / "runs" / "run-1" / "meta.json").stat().st_size
    commit = json.dumps({
        "status": "finished", "fingerprint": "b" * 64, "sizes": {"meta.json": size},
    }).encode()
    assert call(server, "/v2/runs/run-1/commit", "POST", commit, headers)[0] == 200

    status, _, body, cookie = authed(server, "/api/w/default/runs")
    rows = json.loads(body)
    assert rows[0]["last_sync_at"]
    layout.parse_timestamp(rows[0]["last_sync_at"])
    status, _, body, _ = authed(server, "/api/w/default/runs/run-1", cookie=cookie)
    detail = json.loads(body)
    assert detail["last_sync_at"] == rows[0]["last_sync_at"]

    status, _, body, _ = authed(server, "/api/w/default/runs/run-1/lineage", cookie=cookie)
    payload = json.loads(body)
    assert payload["error"] == "code diff not available on this server"
    assert payload["files"] == []

    note = json.dumps({"text": "server note", "revision": None}).encode()
    status, _, body, _ = authed(server, "/api/w/default/runs/run-1/notes", "PUT", note, cookie=cookie)
    assert status == 200
    assert server.note_index.read("run-1")["text"] == "server note"
    assert not (server.root / "runs" / "run-1" / "notes.md").exists()


def test_env_key_signs_in(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    env = "e" * 32
    with running(tmp_path / "data", env_key=env) as httpd:
        httpd.admin_key = env
        status, payload, hdrs = login(httpd, env)
        assert status == 200
        assert payload == {"ok": True, "user": "env", "role": "admin"}
        status, _, body, _ = authed(httpd, "/api/account/session", cookie=hdrs["Set-Cookie"])
        assert status == 200
        assert json.loads(body) == {
            "server": True, "user": "env", "role": "admin", "via": "key", "auth": "password",
            "key": "env",
            "scope": "admin",
            "can": {"notes": True, "own_keys": True, "all_keys": True, "users": True, "audit": True},
            "password_set": False, "identity_proxy": False,
            "workspaces": [{"slug": "default", "name": "Default", "role": "admin"}],
            "server_admin": False, "open_signup": False}


def test_current_matches_name_and_hash(server):
    record = server.keys.verify(server.admin_key)
    assert server.keys.current(record.name, record.sha256) == record
    assert server.keys.current(record.name, "0" * 64) is None
    assert server.keys.current("missing", record.sha256) is None
    KeyStore(server.data_dir / "server", workspace_id=default_workspace_id(server.data_dir / "server")).revoke("admin")
    assert server.keys.current(record.name, record.sha256) is None


def test_authenticated_put_non_note_is_404(server):
    status, hdrs, body, cookie = authed(server, "/foo", "PUT", b"")
    assert status == 404
    assert json.loads(body) == {"error": "not found"}
    status, hdrs, body, _ = authed(server, "/login", "PUT", b"", cookie=cookie)
    assert status == 404
    assert json.loads(body) == {"error": "not found"}
    assert authed(server, "/api/w/default/runs", cookie=cookie)[0] == 200


def test_forged_logout_keeps_session(server):
    _, _, hdrs = login(server)
    cookie = hdrs["Set-Cookie"]
    name, token, _ = cookie_parts(cookie)
    jar = {"Cookie": f"{name}={token}"}

    missing_header = csrf(server)
    del missing_header["X-AgenticML-Request"]
    missing_header.update(jar)
    status, _, body = call(server, "/logout", "POST", b"", missing_header)
    assert status == 403
    assert json.loads(body) == {"error": "cross-site request refused"}
    assert authed(server, "/api/w/default/runs", cookie=cookie)[0] == 200

    status, _, body = call(
        server, "/logout", "POST", b"", {**csrf(server, Origin="http://evil.example"), **jar})
    assert status == 403
    assert json.loads(body) == {"error": "cross-site request refused"}
    assert authed(server, "/api/w/default/runs", cookie=cookie)[0] == 200

    missing_origin = csrf(server)
    del missing_origin["Origin"]
    missing_origin.update(jar)
    status, _, body = call(server, "/logout", "POST", b"", missing_origin)
    assert status == 403
    assert json.loads(body) == {"error": "cross-site request refused"}
    assert authed(server, "/api/w/default/runs", cookie=cookie)[0] == 200


def test_unauthenticated_non_get_gate(server):
    for method in ("HEAD", "OPTIONS", "PUT", "POST"):
        for path in ("/", "/api/w/default/runs"):
            status, hdrs, body = call(server, path, method)
            assert status == 401, (method, path, status)
            assert b"<!DOCTYPE" not in body and b"<html" not in body
            if method != "HEAD":
                assert json.loads(body) == {"error": "login required"}


def test_https_public_url_is_normalized(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", public_url="HTTPS://Example.COM") as httpd:
        assert httpd.public_url == "https://example.com"
        assert httpd.web.public_url == "https://example.com"
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        status, payload, hdrs = login(httpd)
        assert status == 200
        name, _, flags = cookie_parts(hdrs["Set-Cookie"])
        assert name == "__Host-aml_session"
        assert flags["secure"] is True
        cookie = hdrs["Set-Cookie"]
        token = cookie_parts(cookie)[1]
        status, _, body = call(
            httpd, "/api/w/default/runs",
            headers={"Host": "example.com", "Cookie": f"{name}={token}"},
        )
        assert status == 200
        assert json.loads(body) == []


def test_server_mode_cache_headers(server, tmp_path, monkeypatch):
    blob = b"object-bytes"
    sha = hashlib.sha256(blob).hexdigest()
    auth = {"Authorization": "Bearer " + server.sync_key}
    assert call(server, "/v2/objects/" + sha, "PUT", blob, auth)[0] == 201

    status, hdrs, body, cookie = authed(server, "/api/w/default/objects/" + sha)
    assert status == 200
    assert body == blob
    assert hdrs.get("Cache-Control") == "no-store"
    assert "Cookie" in {part.strip() for part in hdrs.get("Vary", "").split(",")}

    status, hdrs, _, _ = authed(server, "/api/w/default/runs", cookie=cookie)
    assert status == 200
    assert hdrs.get("Cache-Control") == "no-store"
    assert "Cookie" in {part.strip() for part in hdrs.get("Vary", "").split(",")}

    status, hdrs, _, cookie = authed(server, "/w/default/", cookie=cookie)
    assert status == 200
    assert hdrs.get("Cache-Control") == "private, no-cache"
    etag = hdrs.get("ETag")
    assert etag
    assert "Cookie" in {part.strip() for part in hdrs.get("Vary", "").split(",")}

    status, hdrs, _, _ = authed(
        server, "/w/default/", extra={"If-None-Match": etag}, cookie=cookie)
    assert status == 304
    assert hdrs.get("Cache-Control") == "private, no-cache"
    assert "Cookie" in {part.strip() for part in hdrs.get("Vary", "").split(",")}

    monkeypatch.setattr(server, "_admit_body", lambda request, limit: False)
    status, hdrs, _ = call(
        server, "/login", "POST", json.dumps({"key": server.admin_key}).encode(),
        csrf(server))
    assert status == 503
    assert hdrs.get("Cache-Control") == "no-store"
    assert "Cookie" in {part.strip() for part in hdrs.get("Vary", "").split(",")}

    status, hdrs, _ = call(server, "/healthz")
    assert status == 200
    assert "Cookie" not in {part.strip() for part in hdrs.get("Vary", "").split(",") if part}

    httpd = ui_server.make_server(tmp_path, "127.0.0.1", 0)
    put_bytes(tmp_path, blob)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        status, hdrs, body = call(httpd, "/api/objects/" + sha)
        assert status == 200
        assert body == blob
        assert hdrs.get("Cache-Control") == "immutable, max-age=31536000"
        assert "Cookie" not in {part.strip() for part in hdrs.get("Vary", "").split(",") if part}
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_hostile_cookie_headers(server):
    _, _, hdrs = login(server)
    cookie = hdrs["Set-Cookie"]
    name, token, _ = cookie_parts(cookie)

    status, _, body = call(
        server, "/api/w/default/runs", headers={"Cookie": f"{name}={token}; {name}=forged"})
    assert status == 401
    assert json.loads(body) == {"error": "login required"}

    status, _, body = call(
        server, "/api/w/default/runs", headers={"Cookie": f"{name}=forged; {name}={token}"})
    assert status == 200
    assert json.loads(body) == []

    status, _, body = call(server, "/api/w/default/runs", headers={"Cookie": "not a cookie at all !!!!"})
    assert status == 401
    assert json.loads(body) == {"error": "login required"}

    status, _, body = call(server, "/api/w/default/runs", headers={"Cookie": f"{name}={token}; bad"})
    assert status == 401
    assert json.loads(body) == {"error": "login required"}

"""Password login, invites, identity proxy, and forwarded-address throttle."""
from server_db_helpers import user_object
from agenticml.server.users import UserStore
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
import hashlib
import http.client
import json
import threading

import pytest

from agenticml import cli
from agenticml.server.app import make_server
from agenticml.server.runs import reconcile
from agenticml.server.up import bootstrap_admin
from agenticml.server.keys import ClientError, KeyStore
from agenticml.server.users import (
    SCRYPT_DKLEN, SCRYPT_N, SCRYPT_P, SCRYPT_R, UserStore, validate_password,
)
from agenticml.server.webauth import (
    LoginThrottle, WebAuth, parse_identity_header, parse_trusted_proxies,
)
from agenticml.store import RunWriter, layout
from test_server_login_throttle import freeze_throttle, whoami
from test_server_web import authed, call, cookie_parts, csrf, login
from server_db_helpers import default_workspace_id


PASSWORD = "password1234"


@contextmanager
def running(data, **kwargs):
    httpd = make_server(data, port=0, **kwargs)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def password_login(server, user, password, **headers):
    status, hdrs, body = call(
        server, "/login", "POST",
        json.dumps({"user": user, "password": password}).encode(),
        csrf(server, **headers))
    payload = json.loads(body) if body else None
    return status, payload, hdrs


def session_cookie(hdrs):
    return hdrs["Set-Cookie"]


# --- 1. Password set/verify ---

def test_password_set_verify_wrong_and_unknown_calls_scrypt(tmp_path, monkeypatch):
    (tmp_path / "server").mkdir()
    users = UserStore(tmp_path / "server", workspace_id=default_workspace_id(tmp_path / "server"))
    users.add("alice", role="admin")
    users.set_password("alice", PASSWORD)
    record = users.get("alice").password
    assert record["alg"] == "scrypt"
    assert record["n"] == SCRYPT_N == 16384
    assert record["r"] == SCRYPT_R == 8
    assert record["p"] == SCRYPT_P == 1
    salt = __import__("base64").b64decode(record["salt"])
    digest = __import__("base64").b64decode(record["hash"])
    assert len(salt) == 16 and len(digest) == SCRYPT_DKLEN
    assert users.verify_password("alice", PASSWORD).name == "alice"
    assert users.verify_password("alice", "wrong-password") is None
    assert users.get("alice").invite is None

    calls = []
    real = hashlib.scrypt

    def wrapped(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(hashlib, "scrypt", wrapped)
    assert users.verify_password("missing", PASSWORD) is None
    assert calls, "unknown user path must still run scrypt"
    with pytest.raises(ClientError):
        validate_password("short")
    users.set_password("alice", "  leading spaces ok")
    assert users.verify_password("alice", "  leading spaces ok") is not None
    assert users.verify_password("alice", "leading spaces ok") is None


def test_check_password_rejects_expensive_scrypt(tmp_path, monkeypatch):
    from agenticml.server.users import _password_record
    (tmp_path / "server").mkdir()
    users = UserStore(tmp_path / "server", workspace_id=default_workspace_id(tmp_path / "server"))
    users.add("alice", role="admin")
    users.set_password("alice", PASSWORD)
    hostile = dict(_password_record(PASSWORD), n=2 ** 20)
    with users.db.transaction(write=True) as tx:
        tx.execute("UPDATE accounts SET password=? WHERE login=?", (json.dumps(hostile), "alice"))
    calls = []
    real = hashlib.scrypt

    def wrapped(*args, **kwargs):
        calls.append(kwargs.get("n"))
        return real(*args, **kwargs)

    monkeypatch.setattr(hashlib, "scrypt", wrapped)
    assert users.verify_password("alice", PASSWORD) is None
    assert calls and all(n is None or n <= SCRYPT_N for n in calls)


# --- 2. Viewer/member notes gate ---

def test_viewer_reads_member_writes_notes(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("view", role="viewer")
        httpd.users.add("mem", role="member")
        httpd.users.set_password("view", PASSWORD)
        httpd.users.set_password("mem", PASSWORD)
        writer = RunWriter.create(httpd.root, run_id="run-1", meta={"project": "p", "name": "n"})
        writer.finish("finished")
        writer.close()
        reconcile(httpd.root, httpd.run_index)

        status, payload, hdrs = password_login(httpd, "view", PASSWORD)
        assert status == 200 and payload == {"ok": True, "user": "view", "role": "viewer"}
        cookie = session_cookie(hdrs)
        status, _, body, _ = authed(httpd, "/api/w/default/runs", cookie=cookie)
        assert status == 200 and any(row["run_id"] == "run-1" for row in json.loads(body))
        status, _, body, _ = authed(httpd, "/api/w/default/notes", cookie=cookie)
        assert status == 200 and json.loads(body)["writable"] is False
        status, _, body, _ = authed(httpd, "/api/w/default/runs/run-1/notes", cookie=cookie)
        assert status == 200 and json.loads(body)["writable"] is False

        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=2)
        try:
            name, token, _ = cookie_parts(cookie)
            headers = csrf(httpd)
            headers["Cookie"] = f"{name}={token}"
            headers["Content-Length"] = "80"
            conn.request("PUT", "/api/w/default/runs/run-1/notes", headers=headers)
            response = conn.getresponse()
            assert response.status == 403
            assert json.loads(response.read()) == {"error": "read-only"}
        finally:
            conn.close()

        status, _, hdrs = password_login(httpd, "mem", PASSWORD)
        cookie = session_cookie(hdrs)
        note = json.dumps({"text": "hello", "revision": None}).encode()
        status, _, body, _ = authed(httpd, "/api/w/default/runs/run-1/notes", "PUT", note, cookie=cookie)
        assert status == 200
        status, _, body, _ = authed(httpd, "/api/w/default/notes", cookie=cookie)
        assert json.loads(body)["writable"] is True


# --- 3. Key login ---

def test_key_login_admin_sync_and_demoted(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        _, admin = httpd.keys.create("admin", "admin")
        _, sync = httpd.keys.create("box", "sync")
        httpd.users.add("alice", role="admin")
        _, alice_key = httpd.keys.create("alice-admin", "admin", user="alice")
        httpd.users.add("other", role="admin")
        httpd.admin_key = admin
        status, payload, hdrs = login(httpd, admin)
        assert status == 200 and payload["user"] == "admin"
        assert session_cookie(hdrs)
        assert login(httpd, sync)[0] == 403
        httpd.users.set_role("alice", "member")
        status, payload, hdrs = login(httpd, alice_key)
        assert status == 403 and payload == {"error": "this key cannot sign in"}
        assert "Set-Cookie" not in hdrs
        httpd.users.set_role("alice", "admin")
        status, _, hdrs = login(httpd, alice_key)
        cookie = session_cookie(hdrs)
        httpd.users.set_role("alice", "member")
        status, _, body, _ = authed(httpd, "/api/account/session", cookie=cookie)
        assert status == 401


def test_key_login_replaced_generation_is_401(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="admin")
        _, alice_key = httpd.keys.create("alice-admin", "admin", user="alice")

        def bump(principal):
            httpd.users.create_invite(principal.user)

        monkeypatch.setattr(httpd.web, "_between_verify_and_session", bump)
        status, payload, hdrs = login(httpd, alice_key)
        assert status == 401 and payload == {"error": "invalid user name or password"}
        assert "Set-Cookie" not in hdrs


# --- 4. Generation ---

def test_password_change_and_reinvite_end_sessions(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="member")
        httpd.users.set_password("alice", PASSWORD)
        status, _, hdrs_a = password_login(httpd, "alice", PASSWORD)
        status, _, hdrs_b = password_login(httpd, "alice", PASSWORD)
        cookie_a, cookie_b = session_cookie(hdrs_a), session_cookie(hdrs_b)
        body = json.dumps({"current": PASSWORD, "new": "new-password-1"}).encode()
        status, hdrs, _, _ = authed(
            httpd, "/api/account/password", "POST", body, cookie=cookie_a)
        assert status == 200
        cookie_fresh = hdrs["Set-Cookie"]
        assert authed(httpd, "/api/w/default/runs", cookie=cookie_b)[0] == 401
        assert authed(httpd, "/api/w/default/runs", cookie=cookie_a)[0] == 401
        assert authed(httpd, "/api/w/default/runs", cookie=cookie_fresh)[0] == 200

        httpd.users.create_invite("alice")
        assert authed(httpd, "/api/w/default/runs", cookie=cookie_fresh)[0] == 401


# --- 5. Invites ---

def test_invite_cli_get_post_and_reuse(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    data = tmp_path / "data"
    assert cli.main(["server", "users", "add", "bob", "--role", "member",
                     "--public-url", "https://ml.example.com", "--data", str(data)]) == 0
    line = capsys.readouterr().out.strip().splitlines()[0]
    assert line.startswith("https://ml.example.com/invite?token=")
    token = line.split("token=", 1)[1]
    with running(data) as httpd:
        assert call(httpd, "/invite?token=nope")[0] == 404
        status, hdrs, page = call(httpd, "/invite?token=" + token)
        assert status == 200 and b"Choose a password" in page
        assert hdrs.get("Referrer-Policy") == "no-referrer"
        assert hdrs.get("Cache-Control") == "no-store"
        status, hdrs, body = call(
            httpd, "/invite", "POST",
            json.dumps({"token": token, "password": PASSWORD}).encode(), csrf(httpd))
        payload = json.loads(body)
        assert status == 200 and payload == {"ok": True, "user": "bob", "role": "member"}
        cookie = session_cookie(hdrs)
        assert authed(httpd, "/api/w/default/runs", cookie=cookie)[0] == 200
        status, _, body = call(
            httpd, "/invite", "POST",
            json.dumps({"token": token, "password": PASSWORD}).encode(), csrf(httpd))
        assert status == 404 and json.loads(body) == {"error": "not found"}

        stale = httpd.users.create_invite("bob")
        expired = layout.format_timestamp(datetime.now(timezone.utc) - timedelta(hours=1))
        from dataclasses import replace
        user = httpd.users.get('bob')
        with httpd.users.db.transaction(write=True) as tx:
            httpd.users.repo.save(tx, replace(user, invite={**user.invite, 'expires_at':expired}))
        assert call(httpd, "/invite?token=" + stale)[0] == 404


def test_invite_cli_without_public_url(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTICML_SERVER_PUBLIC_URL", raising=False)
    data = tmp_path / "data"
    assert cli.main(["server", "users", "add", "cara", "--no-invite", "--data", str(data)]) == 0
    assert cli.main(["server", "users", "invite", "cara", "--data", str(data)]) == 0
    out = capsys.readouterr().out
    assert "/invite?token=" in out
    assert "open it on your server's address" in out


# --- 6. Setup ---

def test_setup_creates_admin_user_and_closes(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        status, hdrs, body = call(
            httpd, "/setup", "POST",
            json.dumps({"token": token, "name": "root", "password": PASSWORD}).encode(),
            csrf(httpd))
        payload = json.loads(body)
        assert status == 201 and payload == {"ok": True, "user": "root", "role": "admin"}
        user = httpd.users.get("root")
        assert user.role == "owner" and user.password is not None
        assert httpd.keys.setup_closed()
        assert httpd.keys.list() == []
        cookie = session_cookie(hdrs)
        status, _, body, _ = authed(httpd, "/api/account/session", cookie=cookie)
        info = json.loads(body)
        assert info["via"] == "setup" and info["user"] == "root" and info["key"] is None
        assert call(httpd, "/setup?token=" + token)[0] == 404


# --- 7. Proxy identity ---

def test_proxy_identity_trusted_untrusted_unknown_funnel_and_match(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", trusted_proxies=("127.0.0.1",),
                 identity_header="tailscale") as httpd:
        httpd.users.add("alice", role="member", identity="alice@example.com")
        httpd.users.set_password("alice", PASSWORD)
        headers = {"Tailscale-User-Login": "alice@example.com"}
        status, _, body = call(httpd, "/api/account/session", headers=headers)
        info = json.loads(body)
        assert status == 200
        assert info["user"] == "alice" and info["via"] == "proxy" and info["key"] is None
        assert info["identity_proxy"] is True
        # Cookie for bob is ignored when the proxy says alice.
        status, _, hdrs = password_login(httpd, "alice", PASSWORD)
        cookie = session_cookie(hdrs)
        httpd.users.add("bob", role="member", identity="bob@example.com")
        mixed = {"Cookie": cookie.split(";", 1)[0], "Tailscale-User-Login": "bob@example.com"}
        status, _, body = call(httpd, "/api/account/session", headers=mixed)
        assert json.loads(body)["user"] == "bob"

        status, hdrs, body = call(httpd, "/", headers={"Tailscale-User-Login": "nobody@x.com"})
        assert status == 403 and b"Signed in as nobody@x.com" in body
        assert b"set-identity NAME nobody@x.com" in body
        status, _, body = call(
            httpd, "/api/w/default/runs", headers={"Tailscale-User-Login": "nobody@x.com"})
        assert status == 403 and json.loads(body) == {"error": "no user for this identity"}

        funnel = {"Tailscale-User-Login": "alice@example.com",
                  "Tailscale-Funnel-Request": "1"}
        status, hdrs, _ = call(httpd, "/")
        # Funnel header strips identity; unauthenticated → login.
        status, hdrs, _ = call(httpd, "/", headers=funnel)
        assert status == 303 and hdrs["Location"] == "/login"

        two = [("Tailscale-User-Login", "alice@example.com"),
               ("Tailscale-User-Login", "alice@example.com")]
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=5)
        try:
            conn.putrequest("GET", "/api/account/session", skip_host=True)
            conn.putheader("Host", f"127.0.0.1:{httpd.server_address[1]}")
            conn.putheader("Tailscale-User-Login", "alice@example.com")
            conn.putheader("Tailscale-User-Login", "alice@example.com")
            conn.endheaders()
            response = conn.getresponse()
            assert response.status == 401
            response.read()
        finally:
            conn.close()

        # casefold match
        status, _, body = call(
            httpd, "/api/account/session",
            headers={"Tailscale-User-Login": "Alice@Example.com"})
        assert json.loads(body)["user"] == "alice"

    with running(tmp_path / "alb", trusted_proxies=("127.0.0.1",),
                 identity_header="alb") as httpd:
        httpd.users.add("alice", role="member", identity="Alice@Example.com")
        status, _, body = call(
            httpd, "/api/account/session",
            headers={"X-Amzn-Oidc-Identity": "Alice@Example.com"})
        assert json.loads(body)["user"] == "alice"
        status, _, body = call(
            httpd, "/api/account/session",
            headers={"X-Amzn-Oidc-Identity": "alice@example.com"})
        assert status == 403

    with running(tmp_path / "untrusted", trusted_proxies=("10.0.0.1",),
                 identity_header="tailscale") as httpd:
        httpd.users.add("alice", role="member", identity="alice@example.com")
        status, hdrs, _ = call(
            httpd, "/", headers={"Tailscale-User-Login": "alice@example.com"})
        assert status == 303 and hdrs["Location"] == "/login"


# --- 8. Forwarded throttle ---

def test_forwarded_throttle_two_clients_and_untrusted_xff(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", trusted_proxies=("127.0.0.1",)) as httpd:
        freeze_throttle(httpd)
        wrong = "not-a-key"
        proxy = {"X-Forwarded-For": "10.0.0.1"}
        for _ in range(LoginThrottle.FREE_FAILURES):
            assert login(httpd, wrong, **proxy)[0] == 401
        assert login(httpd, wrong, **{"X-Forwarded-For": "10.0.0.1"})[0] == 429
        other = {"X-Forwarded-For": "10.0.0.2"}
        status, payload, hdrs = login(httpd, wrong, **other)
        assert status == 401 and payload == {"error": "invalid key"}

        freeze_throttle(httpd)
        # Untrusted peer: X-Forwarded-For ignored. This process is 127.0.0.1,
        # which is trusted, so use a server that does not trust loopback.
    with running(tmp_path / "plain") as httpd:
        freeze_throttle(httpd)
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        wrong = "not-a-key"
        spoof = {"X-Forwarded-For": "10.9.9.9"}
        for _ in range(LoginThrottle.FREE_FAILURES):
            assert login(httpd, wrong, **spoof)[0] == 401
        # Same TCP peer, different spoofed address, still one bucket.
        assert login(httpd, wrong, **{"X-Forwarded-For": "10.8.8.8"})[0] == 429

    with running(tmp_path / "hops", trusted_proxies=("127.0.0.1", "10.1.0.0/16")) as httpd:
        freeze_throttle(httpd)
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        # Right-to-left: skip trusted 10.1.0.8, client is 198.51.100.2
        hops = {"X-Forwarded-For": "198.51.100.2, 10.1.0.8"}
        for _ in range(LoginThrottle.FREE_FAILURES):
            assert login(httpd, "bad", **hops)[0] == 401
        assert login(httpd, "bad", **hops)[0] == 429
        other = {"X-Forwarded-For": "198.51.100.3, 10.1.0.8"}
        assert login(httpd, "bad", **other)[0] == 401
        # Unparsable before an untrusted hop → peer bucket (shared).
        freeze_throttle(httpd)
        bad = {"X-Forwarded-For": "198.51.100.9, not-an-ip"}
        for _ in range(LoginThrottle.FREE_FAILURES):
            assert login(httpd, "bad", **bad)[0] == 401
        assert login(httpd, "bad")[0] == 429

    with running(tmp_path / "v2", trusted_proxies=("127.0.0.1",)) as httpd:
        freeze_throttle(httpd)
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        wrong = "not-a-key"
        for _ in range(LoginThrottle.FREE_FAILURES):
            status, payload, hdrs = whoami(httpd, wrong, headers={"X-Forwarded-For": "10.2.0.1"})
            assert status == 401
        status, payload, hdrs = whoami(httpd, wrong, headers={"X-Forwarded-For": "10.2.0.1"})
        assert status == 429
        status, payload, hdrs = whoami(httpd, wrong, headers={"X-Forwarded-For": "10.2.0.2"})
        assert status == 401


# --- 9. Per-user bucket ---

def test_per_user_password_bucket(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        freeze_throttle(httpd)
        httpd.users.add("alice", role="member")
        httpd.users.add("bob", role="member")
        httpd.users.set_password("alice", PASSWORD)
        httpd.users.set_password("bob", PASSWORD)
        addresses = ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.6"]
        n = {"i": 0}

        def fake_address(_handler):
            i = n["i"]
            n["i"] += 1
            return addresses[i % len(addresses)]

        monkeypatch.setattr(httpd.web, "client_address", fake_address)
        for _ in range(LoginThrottle.FREE_FAILURES):
            assert password_login(httpd, "alice", "wrong-password")[0] == 401
        assert password_login(httpd, "alice", "wrong-password")[0] == 429
        assert password_login(httpd, "bob", "wrong-password")[0] == 401
        assert password_login(httpd, "bob", PASSWORD)[0] == 200


# --- 10. Config ---

def test_config_identity_requires_proxy_and_refuses_world():
    with pytest.raises(ValueError, match="refusing to trust every address"):
        parse_trusted_proxies(["0.0.0.0/0"])
    with pytest.raises(ValueError, match="refusing to trust every address"):
        parse_trusted_proxies(["::/0"])
    nets = parse_trusted_proxies(["127.0.0.1", "::1"])
    assert str(nets[0]) == "127.0.0.1/32"
    assert str(nets[1]) == "::1/128"
    mapped = parse_trusted_proxies(["::ffff:127.0.0.1/128"])
    assert str(mapped[0]) == "127.0.0.1/32"
    with pytest.raises(ValueError, match="refusing to trust every address"):
        parse_trusted_proxies(["::ffff:10.0.0.0/96"])
    with pytest.raises(ValueError, match="identity header must be"):
        parse_identity_header("unknown")
    assert parse_identity_header(None) is None
    assert parse_identity_header("alb") == "alb"


def test_config_make_server_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with pytest.raises(ValueError, match="identity header requires"):
        make_server(tmp_path / "a", port=0, identity_header="alb")
    with pytest.raises(ValueError, match="refusing to trust every address"):
        make_server(tmp_path / "b", port=0, trusted_proxies=("0.0.0.0/0",))
    with running(tmp_path / "ok", trusted_proxies=("10.0.0.1",),
                 identity_header="oauth2-proxy") as httpd:
        assert httpd.web.identity_header == "oauth2-proxy"
        assert httpd.web._identity_name == "X-Forwarded-Email"


# --- 11. server up ---

def test_server_up_prints_invite_and_trusts_loopback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("agenticml.server.up._stdout_is_tty", lambda: True)
    data = tmp_path / "data"
    bootstrap_admin(data, "https://machine.tailnet.ts.net")
    out = capsys.readouterr().out
    assert "Open https://machine.tailnet.ts.net/invite?token=" in out
    assert "72 hours" in out
    users = user_object(UserStore(data / "server", workspace_id=default_workspace_id(data / "server")))
    assert users["users"][0]["name"] == "admin"
    assert users["users"][0]["invite"] is not None
    # Second call is a no-op.
    bootstrap_admin(data, "https://machine.tailnet.ts.net")
    assert "invite?token=" not in capsys.readouterr().out


# --- 12. account password ---

def test_account_password_rules(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="member")
        httpd.users.set_password("alice", PASSWORD)
        status, _, hdrs = password_login(httpd, "alice", PASSWORD)
        cookie = session_cookie(hdrs)
        status, _, body, _ = authed(
            httpd, "/api/account/password", "POST",
            json.dumps({"new": "another-pass1"}).encode(), cookie=cookie)
        assert status == 401
        status, _, body, _ = authed(
            httpd, "/api/account/password", "POST",
            json.dumps({"current": "wrong-password", "new": "another-pass1"}).encode(),
            cookie=cookie)
        assert status == 401
        status, hdrs, body, cookie = authed(
            httpd, "/api/account/password", "POST",
            json.dumps({"current": PASSWORD, "new": "another-pass1"}).encode(),
            cookie=cookie)
        assert status == 200
        assert password_login(httpd, "alice", "another-pass1")[0] == 200

        httpd.users.add("pending", role="viewer")
        token = httpd.users.create_invite("pending")
        status, hdrs, body = call(
            httpd, "/invite", "POST",
            json.dumps({"token": token, "password": PASSWORD}).encode(), csrf(httpd))
        cookie = session_cookie(hdrs)
        # Has a password now; current required.
        status, _, body, _ = authed(
            httpd, "/api/account/password", "POST",
            json.dumps({"new": "newer-pass12"}).encode(), cookie=cookie)
        assert status == 401

    with running(tmp_path / "env", env_key="e" * 32) as httpd:
        httpd.admin_key = "e" * 32
        status, _, hdrs = login(httpd, "e" * 32)
        cookie = session_cookie(hdrs)
        status, _, body, _ = authed(
            httpd, "/api/account/password", "POST",
            json.dumps({"new": "another-pass1"}).encode(), cookie=cookie)
        assert status == 403


def test_account_password_refuses_replaced_user(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="member")
        httpd.users.set_password("alice", PASSWORD)
        old_id = httpd.users.get("alice").id
        status, _, hdrs = password_login(httpd, "alice", PASSWORD)
        cookie = session_cookie(hdrs)
        real_bind = httpd.web._bind

        def bind_and_replace(handler, principal):
            real_bind(handler, principal)
            current = httpd.users.get("alice")
            if current is not None and current.id == old_id:
                httpd.users.remove("alice")
                httpd.users.add("alice", role="member")
                httpd.users.set_password("alice", PASSWORD)

        monkeypatch.setattr(httpd.web, "_bind", bind_and_replace)
        status, _, body, _ = authed(
            httpd, "/api/account/password", "POST",
            json.dumps({"current": PASSWORD, "new": "another-pass1"}).encode(),
            cookie=cookie)
        assert status == 401
        assert httpd.users.verify_password("alice", PASSWORD) is not None
        assert httpd.users.verify_password("alice", "another-pass1") is None


def test_account_password_proxy_skips_session_match(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", trusted_proxies=("127.0.0.1",),
                 identity_header="tailscale") as httpd:
        httpd.users.add("alice", role="member", identity="alice@example.com")
        httpd.users.set_password("alice", PASSWORD)
        headers = csrf(httpd)
        headers["Tailscale-User-Login"] = "alice@example.com"
        status, _, body = call(
            httpd, "/api/account/password", "POST",
            json.dumps({"current": PASSWORD, "new": "newer-pass12"}).encode(),
            headers)
        assert status == 200 and json.loads(body)["ok"] is True
        assert httpd.users.verify_password("alice", "newer-pass12") is not None


def test_cli_serve_reads_proxy_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICML_SERVER_TRUSTED_PROXIES", "10.0.0.1, ::1")
    monkeypatch.setenv("AGENTICML_SERVER_IDENTITY_HEADER", "alb")
    seen = {}

    def fake_serve(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs

    monkeypatch.setattr("agenticml.server.serve", fake_serve)
    assert cli.main(["server", "serve", "--data", str(tmp_path / "data"), "--port", "0"]) == 0
    nets = seen["kwargs"]["trusted_proxies"]
    assert [str(net) for net in nets] == ["10.0.0.1/32", "::1/128"]
    assert seen["kwargs"]["identity_header"] == "alb"


def test_cli_set_password_stdin(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    assert cli.main(["server", "users", "add", "dana", "--no-invite", "--data", str(data)]) == 0
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(PASSWORD + "\n"))
    assert cli.main(["server", "users", "set-password", "dana", "--stdin", "--data", str(data)]) == 0
    users = UserStore(data / "server", workspace_id=default_workspace_id(data / "server"))
    assert users.verify_password("dana", PASSWORD) is not None


def _fake_handler(peer, **headers):
    message = Message()
    for key, value in headers.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                message.add_header(key, item)
        else:
            message[key] = value
    handler = type("H", (), {})()
    handler.client_address = (peer, 1)
    handler.headers = message
    return handler


def _webauth(tmp_path, **kwargs):
    directory = tmp_path / "server"
    directory.mkdir(exist_ok=True)
    return WebAuth(KeyStore(directory, workspace_id=default_workspace_id(directory)), users=UserStore(directory, workspace_id=default_workspace_id(directory)), **kwargs)


# --- Review fixes ---

def test_deleted_user_cookie_does_not_revive_on_recreate(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="member")
        httpd.users.set_password("alice", PASSWORD)
        old_id = httpd.users.get("alice").id
        status, _, hdrs = password_login(httpd, "alice", PASSWORD)
        cookie = session_cookie(hdrs)
        assert authed(httpd, "/api/w/default/runs", cookie=cookie)[0] == 200
        httpd.users.remove("alice")
        httpd.users.add("alice", role="member")
        assert httpd.users.get("alice").id == old_id
        httpd.users.set_password("alice", PASSWORD)
        assert authed(httpd, "/api/w/default/runs", cookie=cookie)[0] == 401
        token = httpd.users.create_invite("alice")
        status, hdrs, body = call(
            httpd, "/invite", "POST",
            json.dumps({"token": token, "password": PASSWORD}).encode(), csrf(httpd))
        assert status == 200
        assert authed(httpd, "/api/w/default/runs", cookie=cookie)[0] == 401
        fresh = session_cookie(hdrs)
        assert authed(httpd, "/api/w/default/runs", cookie=fresh)[0] == 200


def test_reinvite_between_verify_and_session_is_401(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="member")
        httpd.users.set_password("alice", PASSWORD)

        def reinvite(user):
            httpd.users.create_invite(user.name)

        monkeypatch.setattr(httpd.web, "_between_verify_and_session", reinvite)
        status, payload, hdrs = password_login(httpd, "alice", PASSWORD)
        assert status == 401 and payload == {"error": "invalid user name or password"}
        assert "Set-Cookie" not in hdrs

        token = httpd.users.create_invite("alice")
        status, hdrs, body = call(
            httpd, "/invite", "POST",
            json.dumps({"token": token, "password": PASSWORD}).encode(), csrf(httpd))
        assert status == 401 and json.loads(body) == {"error": "invalid user name or password"}
        assert "Set-Cookie" not in hdrs

    with running(tmp_path / "setup") as httpd:
        setup_token = httpd.web.setup_token

        def reinvite_setup(user):
            httpd.users.create_invite(user.name)

        monkeypatch.setattr(httpd.web, "_between_verify_and_session", reinvite_setup)
        status, hdrs, body = call(
            httpd, "/setup", "POST",
            json.dumps({"token": setup_token, "name": "root", "password": PASSWORD}).encode(),
            csrf(httpd))
        assert status == 401
        assert json.loads(body) == {"error": "invalid user name or password"}
        assert "Set-Cookie" not in hdrs


def test_identity_collision_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", trusted_proxies=("127.0.0.1",),
                 identity_header="tailscale") as httpd:
        httpd.users.add("alice", role="member", identity="alice@example.com")
        httpd.users.add("bob", role="member", identity="bob@example.com")
        colliding = [
            replace(httpd.users.get("alice"), identity="Alice@x.com"),
            replace(httpd.users.get("bob"), identity="alice@x.com"),
        ]
        monkeypatch.setattr(httpd.users, "list", lambda: colliding)
        status, _, body = call(
            httpd, "/api/account/session",
            headers={"Tailscale-User-Login": "alice@x.com"})
        assert status == 403 and json.loads(body) == {"error": "no user for this identity"}


def test_unknown_identity_falls_back_to_session_cookie(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", trusted_proxies=("127.0.0.1",),
                 identity_header="tailscale") as httpd:
        httpd.users.add("admin", role="admin")
        _, admin_key = httpd.keys.create("k", "admin", user="admin")
        httpd.admin_key = admin_key
        status, _, hdrs = login(httpd, admin_key)
        cookie = session_cookie(hdrs)
        headers = {
            "Cookie": cookie.split(";", 1)[0],
            "Tailscale-User-Login": "nobody@x.com",
        }
        status, _, body = call(httpd, "/api/account/session", headers=headers)
        info = json.loads(body)
        assert status == 200
        assert info["user"] == "admin" and info["via"] == "key"
        status, hdrs, body = call(
            httpd, "/", headers={"Tailscale-User-Login": "nobody@x.com"})
        assert status == 403 and b"Signed in as nobody@x.com" in body


def test_account_password_current_uses_login_throttle(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        freeze_throttle(httpd)
        httpd.users.add("alice", role="member")
        httpd.users.add("bob", role="member")
        httpd.users.set_password("alice", PASSWORD)
        httpd.users.set_password("bob", PASSWORD)
        status, _, hdrs = password_login(httpd, "alice", PASSWORD)
        cookie = session_cookie(hdrs)
        addresses = [f"10.1.0.{i}" for i in range(1, 10)]
        n = {"i": 0}

        def fake_address(_handler):
            i = n["i"]
            n["i"] += 1
            return addresses[i % len(addresses)]

        monkeypatch.setattr(httpd.web, "client_address", fake_address)
        body = json.dumps({"current": "wrong-password", "new": "another-pass1"}).encode()
        for _ in range(LoginThrottle.FREE_FAILURES):
            assert authed(
                httpd, "/api/account/password", "POST", body, cookie=cookie)[0] == 401
        assert authed(
            httpd, "/api/account/password", "POST", body, cookie=cookie)[0] == 429
        assert password_login(httpd, "alice", "wrong-password")[0] == 429
        assert password_login(httpd, "bob", "wrong-password")[0] == 401


def test_scrypt_lone_surrogate_is_invalid_password_and_failed_login(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    (tmp_path / "server").mkdir()
    users = UserStore(tmp_path / "server", workspace_id=default_workspace_id(tmp_path / "server"))
    users.add("alice", role="member")
    users.set_password("alice", PASSWORD)
    with pytest.raises(ClientError, match="invalid password"):
        users.verify_password("alice", PASSWORD[:-1] + "\ud800")
    with running(tmp_path / "data") as httpd:
        freeze_throttle(httpd)
        httpd.users.add("alice", role="member")
        httpd.users.set_password("alice", PASSWORD)
        bad = PASSWORD[:-1] + "\ud800"
        for _ in range(LoginThrottle.FREE_FAILURES):
            status, payload, hdrs = password_login(httpd, "alice", bad)
            assert status == 400 and payload == {"error": "invalid password"}
        assert password_login(httpd, "alice", bad)[0] == 429


def test_setup_wrong_token_counts_as_failed_attempt(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        freeze_throttle(httpd)
        body = json.dumps(
            {"token": "nope", "name": "root", "password": PASSWORD}).encode()
        for _ in range(LoginThrottle.FREE_FAILURES):
            status, _, payload = call(httpd, "/setup", "POST", body, csrf(httpd))
            assert status == 404
        assert call(httpd, "/setup", "POST", body, csrf(httpd))[0] == 429


def test_setup_partial_failure_rolls_back_user(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        real_set = httpd.users.set_password

        def boom(name, password, **kwargs):
            real_set(name, password, **kwargs)
            raise OSError("disk full")

        monkeypatch.setattr(httpd.users, "set_password", boom)
        status, _, body = call(
            httpd, "/setup", "POST",
            json.dumps({"token": token, "name": "root", "password": PASSWORD}).encode(),
            csrf(httpd))
        assert status == 503
        assert json.loads(body) == {
            "error": "setup failed; retry or use agenticml server users"}
        assert httpd.users.get("root") is None
        assert httpd.web.setup_token == token
        monkeypatch.setattr(httpd.users, "set_password", real_set)
        status, _, body = call(
            httpd, "/setup", "POST",
            json.dumps({"token": token, "name": "root", "password": PASSWORD}).encode(),
            csrf(httpd))
        assert status == 201 and json.loads(body)["user"] == "root"


def test_setup_rollback_keeps_replaced_same_name_user(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        created_id = {"id": None, "replacement": None}
        real_add = httpd.users.add

        def tracking_add(*args, **kwargs):
            record = real_add(*args, **kwargs)
            if created_id["id"] is None:
                created_id["id"] = record.id
            return record

        real_set = httpd.users.set_password

        def boom(name, password, **kwargs):
            real_set(name, password, **kwargs)
            httpd.users.discard(name, created_id["id"])
            created_id["replacement"] = real_add(name, role="admin").id
            raise OSError("disk full")

        monkeypatch.setattr(httpd.users, "add", tracking_add)
        monkeypatch.setattr(httpd.users, "set_password", boom)
        status, _, body = call(
            httpd, "/setup", "POST",
            json.dumps({"token": token, "name": "root", "password": PASSWORD}).encode(),
            csrf(httpd))
        assert status == 503
        leftover = httpd.users.get("root")
        assert leftover is not None
        assert leftover.id == created_id["replacement"] != created_id["id"]
        assert not httpd.keys.setup_closed()


def test_setup_failure_after_closed_marker_rolls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        token = httpd.web.setup_token
        real_write = httpd.keys._write_setup_closed

        def boom():
            real_write()
            raise OSError("disk full")

        monkeypatch.setattr(httpd.keys, "_write_setup_closed", boom)
        status, _, body = call(
            httpd, "/setup", "POST",
            json.dumps({"token": token, "name": "root", "password": PASSWORD}).encode(),
            csrf(httpd))
        assert status == 503
        assert json.loads(body) == {
            "error": "setup failed; retry or use agenticml server users"}
        assert httpd.users.get("root") is None
        assert not httpd.keys.setup_closed()
        assert httpd.web.setup_token == token
        monkeypatch.setattr(httpd.keys, "_write_setup_closed", real_write)
        status, _, body = call(
            httpd, "/setup", "POST",
            json.dumps({"token": token, "name": "root", "password": PASSWORD}).encode(),
            csrf(httpd))
        assert status == 201 and json.loads(body)["user"] == "root"
        assert httpd.keys.setup_closed()


def test_hostile_identity_escaped_in_403_page(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", trusted_proxies=("127.0.0.1",),
                 identity_header="tailscale") as httpd:
        identity = '<script>"&'
        status, _, body = call(httpd, "/", headers={"Tailscale-User-Login": identity})
        assert status == 403
        assert b"<script>" not in body
        assert b"&lt;script&gt;" in body
        assert b"&quot;" in body
        assert b"&amp;" in body


def test_mapped_peer_and_forwarded_port_and_multiple_xff(tmp_path):
    web = _webauth(tmp_path, trusted_proxies=("127.0.0.1", "10.1.0.0/16"))
    mapped = _fake_handler("::ffff:127.0.0.1", **{"X-Forwarded-For": "198.51.100.8"})
    assert web._peer_trusted(mapped)
    assert web.client_address(mapped) == "198.51.100.8"
    ported = _fake_handler(
        "127.0.0.1", **{"X-Forwarded-For": "198.51.100.9:12345, 10.1.0.8"})
    assert web.client_address(ported) == "198.51.100.9"
    v6 = _fake_handler("127.0.0.1", **{"X-Forwarded-For": "[2001:db8::1]:443"})
    assert web.client_address(v6) == "2001:db8::1"


def test_multiple_xff_header_lines_select_rightmost_untrusted(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data", trusted_proxies=("127.0.0.1",)) as httpd:
        freeze_throttle(httpd)
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        host = f"127.0.0.1:{httpd.server_address[1]}"
        origin = f"http://{host}"

        def login_xff(*values):
            conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=5)
            try:
                conn.putrequest("POST", "/login", skip_host=True)
                conn.putheader("Host", host)
                conn.putheader("Origin", origin)
                conn.putheader("X-AgenticML-Request", "1")
                conn.putheader("Content-Type", "application/json")
                for value in values:
                    conn.putheader("X-Forwarded-For", value)
                body = json.dumps({"key": "not-a-key"}).encode()
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders()
                conn.send(body)
                response = conn.getresponse()
                payload = json.loads(response.read())
                return response.status, payload
            finally:
                conn.close()

        for _ in range(LoginThrottle.FREE_FAILURES):
            assert login_xff("10.0.0.1", "10.0.0.2")[0] == 401
        assert login_xff("10.0.0.1", "10.0.0.2")[0] == 429
        assert login_xff("10.0.0.1")[0] == 401


def test_csrf_refused_on_login_invite_setup_and_account_password(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        httpd.users.add("alice", role="member")
        httpd.users.set_password("alice", PASSWORD)
        httpd.users.add("bob", role="member")
        token = httpd.users.create_invite("bob")
        setup_token = httpd.web.setup_token
        login_body = json.dumps({"user": "alice", "password": PASSWORD}).encode()
        invite_body = json.dumps({"token": token, "password": PASSWORD}).encode()
        setup_body = json.dumps(
            {"token": setup_token, "name": "root", "password": PASSWORD}).encode()
        pw_body = json.dumps({"current": PASSWORD, "new": "another-pass1"}).encode()
        status, _, hdrs = password_login(httpd, "alice", PASSWORD)
        cookie = session_cookie(hdrs)
        name, value, _ = cookie_parts(cookie)

        def refused(path, body, extra=None):
            headers = csrf(httpd)
            if extra:
                headers.update(extra)
            status, _, payload = call(httpd, path, "POST", body, headers)
            assert status == 403
            assert json.loads(payload) == {"error": "cross-site request refused"}

        for path, body in (("/login", login_body), ("/invite", invite_body),
                           ("/setup", setup_body)):
            missing = csrf(httpd)
            del missing["X-AgenticML-Request"]
            status, _, payload = call(httpd, path, "POST", body, missing)
            assert status == 403 and json.loads(payload) == {
                "error": "cross-site request refused"}
            refused(path, body, {"Origin": "http://evil.example"})

        extra = {"Cookie": f"{name}={value}"}
        missing = csrf(httpd)
        del missing["X-AgenticML-Request"]
        missing.update(extra)
        status, _, payload = call(
            httpd, "/api/account/password", "POST", pw_body, missing)
        assert status == 403 and json.loads(payload) == {
            "error": "cross-site request refused"}
        headers = csrf(httpd, Origin="http://evil.example")
        headers.update(extra)
        status, _, payload = call(
            httpd, "/api/account/password", "POST", pw_body, headers)
        assert status == 403 and json.loads(payload) == {
            "error": "cross-site request refused"}


def test_expired_invite_post_counts_against_address_throttle(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        freeze_throttle(httpd)
        httpd.users.add("bob", role="member")
        token = httpd.users.create_invite("bob")
        expired = layout.format_timestamp(datetime.now(timezone.utc) - timedelta(hours=1))
        from dataclasses import replace
        user = httpd.users.get('bob')
        with httpd.users.db.transaction(write=True) as tx:
            httpd.users.repo.save(tx, replace(user, invite={**user.invite, 'expires_at':expired}))
        body = json.dumps({"token": token, "password": PASSWORD}).encode()
        for _ in range(LoginThrottle.FREE_FAILURES):
            status, _, payload = call(httpd, "/invite", "POST", body, csrf(httpd))
            assert status == 404
        assert call(httpd, "/invite", "POST", body, csrf(httpd))[0] == 429

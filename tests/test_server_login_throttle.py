"""Per-address login throttle and weak environment-key warning."""
import http.client
import json
import secrets
import sys

import pytest

from agenticml.server.app import make_server
from agenticml.server.keys import env_key_looks_weak
from agenticml.server.webauth import LoginThrottle

from test_server_web import call, csrf, login, running


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        _, httpd.sync_key = httpd.keys.create("sync-box", "sync")
        yield httpd


def freeze_throttle(server, now=0.0):
    clock = [now]
    server.web.throttle = LoginThrottle(clock=lambda: clock[0])
    return clock


def login_from(server, source, key):
    conn = http.client.HTTPConnection(
        *server.server_address[:2], timeout=5, source_address=(source, 0))
    try:
        conn.request("POST", "/login", json.dumps({"key": key}).encode(), csrf(server))
        response = conn.getresponse()
        body = response.read()
        payload = json.loads(body) if body else None
        return response.status, payload, dict(response.getheaders())
    finally:
        conn.close()


def test_throttle_free_failures_backoff_and_cap():
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        throttle.record_failure("a")
        assert throttle.retry_after("a") == 0
    throttle.record_failure("a")
    delays = []
    for n in range(LoginThrottle.FREE_FAILURES, LoginThrottle.FREE_FAILURES + 8):
        assert throttle.retry_after("a") == min(2 ** (n - LoginThrottle.FREE_FAILURES), 60)
        delays.append(throttle.retry_after("a"))
        throttle.record_failure("a")
    assert delays[:6] == [1, 2, 4, 8, 16, 32]
    assert delays[6:] == [60, 60]


def test_throttle_expires_success_clears_and_evicts_oldest():
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(LoginThrottle.FREE_FAILURES):
        throttle.record_failure("a")
    assert throttle.retry_after("a") == 1
    throttle.clear("a")
    assert throttle.retry_after("a") == 0
    for _ in range(LoginThrottle.FREE_FAILURES):
        throttle.record_failure("a")
    now[0] = LoginThrottle.EXPIRE_AFTER
    assert throttle.retry_after("a") == 0
    for _ in range(LoginThrottle.FREE_FAILURES):
        throttle.record_failure("a")
    assert throttle.retry_after("a") == 1

    now[0] = 0.0
    throttle = LoginThrottle(clock=lambda: now[0])
    for index in range(LoginThrottle.MAX_ENTRIES):
        for _ in range(LoginThrottle.FREE_FAILURES):
            throttle.record_failure(f"addr-{index}")
    assert throttle.retry_after("addr-0") == 1
    assert throttle.retry_after("addr-1023") == 1
    throttle.record_failure("new")
    assert throttle.retry_after("addr-0") == 0
    assert throttle.retry_after("addr-1023") == 1
    assert throttle.retry_after("new") == 0
    assert throttle.retry_after("other") == 0


def test_throttle_buckets_mapped_ipv4_ipv6_slash64_and_scope():
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(LoginThrottle.FREE_FAILURES):
        throttle.record_failure("::ffff:127.0.0.1")
    assert throttle.retry_after("127.0.0.1") == 1
    assert throttle.retry_after("::ffff:127.0.0.1") == 1

    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(LoginThrottle.FREE_FAILURES):
        throttle.record_failure("2001:db8:1:2::1")
    assert throttle.retry_after("2001:db8:1:2::ffff") == 1
    assert throttle.retry_after("2001:db8:1:3::1") == 0

    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(LoginThrottle.FREE_FAILURES):
        throttle.record_failure("fe80::1%eth0")
    assert throttle.retry_after("fe80::1") == 1
    assert throttle.retry_after("fe80::1%lo") == 1


def test_http_throttle_refuses_even_a_good_key_other_address_signs_in(server):
    freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(LoginThrottle.FREE_FAILURES):
        status, payload, hdrs = login(server, wrong)
        assert status == 401
        assert payload == {"error": "invalid key"}
        assert "Retry-After" not in hdrs
    status, payload, hdrs = login(server, wrong)
    assert status == 429
    assert payload == {"error": "too many failed logins; try again later"}
    assert hdrs.get("Cache-Control") == "no-store"
    assert hdrs.get("Connection") == "close"
    assert int(hdrs["Retry-After"]) >= 1
    status, payload, hdrs = login(server, server.admin_key)
    assert status == 429
    assert payload == {"error": "too many failed logins; try again later"}
    assert hdrs.get("Connection") == "close"
    assert "Set-Cookie" not in hdrs
    assert server.web.throttle.retry_after("127.0.0.2") == 0
    try:
        status, payload, hdrs = login_from(server, "127.0.0.2", server.admin_key)
    except OSError:
        if sys.platform.startswith("linux"):
            raise
        pytest.skip("cannot bind 127.0.0.2")
    assert status == 200
    assert payload == {"ok": True, "key": "admin"}
    assert "Set-Cookie" in hdrs


def test_sync_key_and_malformed_body_do_not_count(server):
    freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(4):
        assert login(server, wrong)[0] == 401
    assert login(server, server.sync_key)[0] == 403
    status, _, body = call(server, "/login", "POST", b"{", csrf(server))
    assert status == 400
    assert json.loads(body) == {"error": "invalid request"}
    assert login(server, wrong)[0] == 401
    assert login(server, wrong)[0] == 429
    assert login(server, server.sync_key)[0] == 429


def test_env_key_looks_weak():
    assert env_key_looks_weak("a" * 64)
    assert env_key_looks_weak("password" * 4)
    assert not env_key_looks_weak(secrets.token_urlsafe(32))


def test_make_server_warns_on_weak_env_key(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    caplog.set_level("WARNING", logger="agenticml.server")
    weak = "a" * 64
    httpd = make_server(tmp_path / "data", port=0, env_key=weak)
    try:
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "AGENTICML_SERVER_KEY looks low-entropy; generate one with agenticml "
            "server keys create or secrets.token_urlsafe(32)" in message
            for message in messages)
        for record in caplog.records:
            assert weak not in record.getMessage()
            assert weak not in str(record.args)
        assert weak not in caplog.text
    finally:
        httpd.server_close()

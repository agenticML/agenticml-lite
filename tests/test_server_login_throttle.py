"""Per-address login throttle and weak environment-key warning."""
import http.client
import json
import secrets
import socket
import sys
import threading
import time

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


def record_failed(throttle, address="a"):
    retry, ticket = throttle.begin(address)
    assert ticket is not None
    throttle.finish(ticket, "failed")
    return retry


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


def whoami(server, key=None, headers=None, source=None):
    supplied = {} if headers is None else dict(headers)
    if key is not None:
        supplied["Authorization"] = "Bearer " + key
    if source is None:
        status, hdrs, body = call(server, "/v2/whoami", headers=supplied)
        payload = json.loads(body) if body else None
        return status, payload, hdrs
    conn = http.client.HTTPConnection(
        *server.server_address[:2], timeout=5, source_address=(source, 0))
    try:
        conn.request("GET", "/v2/whoami", headers=supplied)
        response = conn.getresponse()
        body = response.read()
        payload = json.loads(body) if body else None
        return response.status, payload, dict(response.getheaders())
    finally:
        conn.close()


def test_throttle_free_failures_backoff_and_cap():
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(LoginThrottle.FREE_FAILURES):
        retry, ticket = throttle.begin("a")
        assert retry == 0
        assert ticket is not None
        throttle.finish(ticket, "failed")
    delays = []
    for n in range(LoginThrottle.FREE_FAILURES, LoginThrottle.FREE_FAILURES + 8):
        expected = min(2 ** (n - LoginThrottle.FREE_FAILURES), 60)
        retry, ticket = throttle.begin("a")
        assert ticket is None
        assert retry == expected
        delays.append(retry)
        now[0] += retry
        record_failed(throttle)
    assert delays[:6] == [1, 2, 4, 8, 16, 32]
    assert delays[6:] == [60, 60]


def test_throttle_expires_success_clears_and_evicts_oldest():
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        record_failed(throttle)
    retry, ticket = throttle.begin("a")
    assert ticket is not None
    throttle.finish(ticket, "success")
    retry, ticket = throttle.begin("a")
    assert retry == 0
    assert ticket is not None
    throttle.finish(ticket, "ignored")
    for _ in range(LoginThrottle.FREE_FAILURES):
        record_failed(throttle)
    retry, ticket = throttle.begin("a")
    assert ticket is None
    assert retry == 1
    # now == EXPIRE_AFTER drops the stale bucket, so the next FREE_FAILURES
    # start from zero (retry == 1). If the entry had only aged past its delay,
    # those failures would raise the backoff above 1.
    now[0] = LoginThrottle.EXPIRE_AFTER
    for _ in range(LoginThrottle.FREE_FAILURES):
        record_failed(throttle)
    retry, ticket = throttle.begin("a")
    assert ticket is None
    assert retry == 1

    now[0] = 0.0
    throttle = LoginThrottle(clock=lambda: now[0])
    for index in range(LoginThrottle.MAX_ENTRIES):
        for _ in range(LoginThrottle.FREE_FAILURES):
            record_failed(throttle, f"addr-{index}")
    retry, ticket = throttle.begin("addr-0")
    assert ticket is None
    assert retry == 1
    retry, ticket = throttle.begin("addr-1023")
    assert ticket is None
    assert retry == 1
    record_failed(throttle, "new")
    retry, ticket = throttle.begin("addr-0")
    assert retry == 0
    assert ticket is not None
    throttle.finish(ticket, "ignored")
    retry, ticket = throttle.begin("addr-1023")
    assert ticket is None
    retry, ticket = throttle.begin("new")
    assert retry == 0
    assert ticket is not None
    throttle.finish(ticket, "ignored")
    retry, ticket = throttle.begin("other")
    assert retry == 0
    assert ticket is not None
    throttle.finish(ticket, "ignored")


def test_throttle_buckets_mapped_ipv4_ipv6_slash64_and_scope():
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(LoginThrottle.FREE_FAILURES):
        record_failed(throttle, "::ffff:127.0.0.1")
    retry, ticket = throttle.begin("127.0.0.1")
    assert ticket is None
    assert retry == 1
    retry, ticket = throttle.begin("::ffff:127.0.0.1")
    assert ticket is None
    assert retry == 1

    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(LoginThrottle.FREE_FAILURES):
        record_failed(throttle, "2001:db8:1:2::1")
    retry, ticket = throttle.begin("2001:db8:1:2::ffff")
    assert ticket is None
    assert retry == 1
    retry, ticket = throttle.begin("2001:db8:1:3::1")
    assert retry == 0
    assert ticket is not None
    throttle.finish(ticket, "ignored")

    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(LoginThrottle.FREE_FAILURES):
        record_failed(throttle, "fe80::1%eth0")
    retry, ticket = throttle.begin("fe80::1")
    assert ticket is None
    assert retry == 1
    retry, ticket = throttle.begin("fe80::1%lo")
    assert ticket is None
    assert retry == 1


def test_ignored_with_other_attempt_pending():
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        record_failed(throttle)
    now[0] = 100.0
    _, ignored = throttle.begin("a")
    assert ignored is not None
    now[0] = 101.0
    _, guess = throttle.begin("a")
    assert guess is not None
    throttle.finish(ignored, "ignored")
    retry, second = throttle.begin("a")
    assert second is None
    assert retry >= 1
    throttle.finish(guess, "failed")


def test_success_with_other_attempt_pending():
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        record_failed(throttle)
    now[0] = 100.0
    _, success = throttle.begin("a")
    now[0] = 101.0
    _, pending = throttle.begin("a")
    assert pending is not None
    throttle.finish(success, "success")
    assert throttle._entries["a"][2]
    retry, extra = throttle.begin("a")
    assert extra is not None
    throttle.finish(extra, "ignored")
    throttle.finish(pending, "failed")
    for _ in range(4):
        record_failed(throttle)
    retry, ticket = throttle.begin("a")
    assert ticket is None
    assert retry >= 1


def test_holds_never_expire_or_evict():
    now = [0.0]
    throttle = LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        record_failed(throttle, "held")
    _, held = throttle.begin("held")
    assert held is not None
    now[0] = LoginThrottle.EXPIRE_AFTER + 1
    _, extra = throttle.begin("held")
    assert extra is not None
    throttle.finish(held, "failed")
    throttle.finish(extra, "failed")
    retry, ticket = throttle.begin("held")
    assert ticket is None

    now[0] = 0.0
    throttle = LoginThrottle(clock=lambda: now[0])
    _, held = throttle.begin("held")
    for index in range(LoginThrottle.MAX_ENTRIES):
        for _ in range(LoginThrottle.FREE_FAILURES):
            record_failed(throttle, f"addr-{index}")
    throttle.finish(held, "failed")
    for _ in range(4):
        record_failed(throttle, "held")
    retry, ticket = throttle.begin("held")
    assert ticket is None


def test_drop_at_zero_and_unknown_ticket():
    throttle = LoginThrottle(clock=lambda: 0.0)
    throttle.finish(None, "failed")
    throttle.finish(("missing", 0), "failed")
    assert throttle._entries == {}
    _, ticket = throttle.begin("a")
    throttle.finish(("a", -1), "failed")
    throttle.finish(ticket, "ignored")
    assert throttle._entries == {}
    _, ticket = throttle.begin("a")
    throttle.finish(ticket, "success")
    assert throttle._entries == {}
    _, ticket = throttle.begin("a")
    throttle.finish(ticket, "failed")
    assert list(throttle._entries) == ["a"]
    _, ticket = throttle.begin("a")
    throttle.finish(ticket, "success")
    assert throttle._entries == {}


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
    retry, ticket = server.web.throttle.begin("127.0.0.2")
    assert retry == 0
    assert ticket is not None
    server.web.throttle.finish(ticket, "ignored")
    try:
        status, payload, hdrs = login_from(server, "127.0.0.2", server.admin_key)
    except OSError:
        if sys.platform.startswith("linux"):
            raise
        pytest.skip("cannot bind 127.0.0.2")
    assert status == 200
    assert payload == {"ok": True, "user": "admin", "role": "admin"}
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


def test_bearer_failures_throttle_whoami_and_login(server):
    freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(LoginThrottle.FREE_FAILURES):
        status, payload, hdrs = whoami(server, wrong)
        assert status == 401
        assert payload == {"error": "unauthorized"}
        assert "Retry-After" not in hdrs
    status, payload, hdrs = whoami(server, server.admin_key)
    assert status == 429
    assert payload == {"error": "too many failed authentications; try again later"}
    assert hdrs.get("Connection") == "close"
    assert int(hdrs["Retry-After"]) >= 1
    status, payload, hdrs = login(server, server.admin_key)
    assert status == 429
    assert payload == {"error": "too many failed logins; try again later"}
    assert hdrs.get("Connection") == "close"
    assert int(hdrs["Retry-After"]) >= 1


def test_login_failures_throttle_whoami(server):
    freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(LoginThrottle.FREE_FAILURES):
        assert login(server, wrong)[0] == 401
    status, payload, hdrs = whoami(server, server.admin_key)
    assert status == 429
    assert payload == {"error": "too many failed authentications; try again later"}
    assert hdrs.get("Connection") == "close"
    assert int(hdrs["Retry-After"]) >= 1


def test_missing_or_malformed_authorization_does_not_count(server):
    freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(4):
        assert whoami(server, wrong)[0] == 401
    assert whoami(server)[0] == 401
    for headers in (
            {"Authorization": "Basic abc"},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer  " + server.admin_key}):
        assert whoami(server, headers=headers)[0] == 401
    assert whoami(server, wrong)[0] == 401
    assert whoami(server, wrong)[0] == 429


def test_correct_bearer_does_not_clear_bucket(server):
    freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(4):
        assert whoami(server, wrong)[0] == 401
    status, payload, _ = whoami(server, server.admin_key)
    assert status == 200
    assert payload["key"] == "admin"
    assert whoami(server, wrong)[0] == 401
    status, payload, hdrs = whoami(server, server.admin_key)
    assert status == 429
    assert payload == {"error": "too many failed authentications; try again later"}
    assert hdrs.get("Connection") == "close"
    assert int(hdrs["Retry-After"]) >= 1


def _gate_verify(server, monkeypatch):
    entered = threading.Event()
    released = threading.Event()
    admitted = []
    original = server.keys.verify

    def gated(value):
        admitted.append(1)
        entered.set()
        assert released.wait(timeout=5)
        return original(value)

    monkeypatch.setattr(server.keys, "verify", gated)
    return entered, released, admitted


def _run_concurrent(n, fn):
    results = []
    lock = threading.Lock()
    reached = threading.Event()

    def worker():
        status = fn()
        with lock:
            results.append(status)
            if len(results) >= n - 1:
                reached.set()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for thread in threads:
        thread.start()
    return threads, results, lock, reached


def _join(threads, timeout=5):
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(timeout=max(deadline - time.monotonic(), 0))
        assert not thread.is_alive()


def test_concurrent_bearer_admits_exactly_one(server, monkeypatch):
    freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(4):
        assert whoami(server, wrong)[0] == 401
    entered, released, admitted = _gate_verify(server, monkeypatch)
    threads, results, lock, reached = _run_concurrent(
        16, lambda: whoami(server, wrong)[0])
    assert entered.wait(timeout=5)
    assert reached.wait(timeout=5)
    with lock:
        assert results == [429] * 15
    assert admitted == [1]
    released.set()
    _join(threads)
    with lock:
        assert results.count(429) == 15
        assert results.count(401) == 1


def test_concurrent_login_admits_exactly_one(server, monkeypatch):
    freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(4):
        assert login(server, wrong)[0] == 401
    entered, released, admitted = _gate_verify(server, monkeypatch)
    threads, results, lock, reached = _run_concurrent(
        16, lambda: login(server, wrong)[0])
    assert entered.wait(timeout=5)
    assert reached.wait(timeout=5)
    with lock:
        assert results == [429] * 15
    assert admitted == [1]
    released.set()
    _join(threads)
    with lock:
        assert results.count(429) == 15
        assert results.count(401) == 1


def test_mixed_completion_login_does_not_admit_second_guess(server, monkeypatch):
    clock = freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(4):
        assert login(server, wrong)[0] == 401
    clock[0] = 100.0

    malformed_entered = threading.Event()
    malformed_released = threading.Event()
    original_loads = json.loads

    def gated_loads(value, *args, **kwargs):
        if value == "{":
            malformed_entered.set()
            assert malformed_released.wait(timeout=5)
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr("agenticml.server.webauth.json.loads", gated_loads)
    entered, released, admitted = _gate_verify(server, monkeypatch)

    mal_status = []

    def mal_worker():
        mal_status.append(call(server, "/login", "POST", b"{", csrf(server))[0])

    mal_thread = threading.Thread(target=mal_worker)
    mal_thread.start()
    assert malformed_entered.wait(timeout=5)

    clock[0] = 101.0
    guess_status = []

    def guess_worker():
        guess_status.append(login(server, wrong)[0])

    guess_thread = threading.Thread(target=guess_worker)
    guess_thread.start()
    assert entered.wait(timeout=5)
    assert admitted == [1]

    malformed_released.set()
    _join([mal_thread])
    assert mal_status == [400]

    status, payload, hdrs = login(server, wrong)
    assert status == 429
    assert payload == {"error": "too many failed logins; try again later"}
    assert hdrs.get("Connection") == "close"
    assert admitted == [1]

    released.set()
    _join([guess_thread])
    assert guess_status == [401]


def test_mixed_completion_bearer_does_not_admit_second_guess(server, monkeypatch):
    clock = freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(4):
        assert whoami(server, wrong)[0] == 401
    clock[0] = 100.0

    malformed_entered = threading.Event()
    malformed_released = threading.Event()
    import agenticml.server.ingest as ingest_mod
    original_fullmatch = ingest_mod.re.fullmatch

    def gated_fullmatch(pattern, string, *args, **kwargs):
        result = original_fullmatch(pattern, string, *args, **kwargs)
        if isinstance(pattern, str) and "Bearer" in pattern and result is None:
            malformed_entered.set()
            assert malformed_released.wait(timeout=5)
        return result

    monkeypatch.setattr(ingest_mod.re, "fullmatch", gated_fullmatch)
    entered, released, admitted = _gate_verify(server, monkeypatch)

    mal_status = []

    def mal_worker():
        mal_status.append(whoami(server, headers={"Authorization": "Basic abc"})[0])

    mal_thread = threading.Thread(target=mal_worker)
    mal_thread.start()
    assert malformed_entered.wait(timeout=5)

    clock[0] = 101.0
    guess_status = []

    def guess_worker():
        guess_status.append(whoami(server, wrong)[0])

    guess_thread = threading.Thread(target=guess_worker)
    guess_thread.start()
    assert entered.wait(timeout=5)
    assert admitted == [1]

    malformed_released.set()
    _join([mal_thread])
    assert mal_status == [401]

    status, payload, hdrs = whoami(server, wrong)
    assert status == 429
    assert payload == {"error": "too many failed authentications; try again later"}
    assert hdrs.get("Connection") == "close"
    assert admitted == [1]

    released.set()
    _join([guess_thread])
    assert guess_status == [401]


def test_admitted_exception_releases_slot(server, monkeypatch):
    freeze_throttle(server)
    wrong = "not-a-key"
    original = server.keys.verify
    for _ in range(4):
        assert whoami(server, wrong)[0] == 401

    def boom(_value):
        raise RuntimeError("verify exploded")

    monkeypatch.setattr(server.keys, "verify", boom)
    status, payload, _ = whoami(server, wrong)
    assert status == 500
    assert payload == {"error": "internal error"}
    monkeypatch.setattr(server.keys, "verify", original)
    assert whoami(server, wrong)[0] == 401
    assert whoami(server, wrong)[0] == 429

    freeze_throttle(server)
    for _ in range(4):
        assert login(server, wrong)[0] == 401

    def boom_value(_value):
        raise ValueError("verify exploded")

    monkeypatch.setattr(server.keys, "verify", boom_value)
    assert login(server, wrong)[0] == 400
    monkeypatch.setattr(server.keys, "verify", original)
    assert login(server, wrong)[0] == 401
    assert login(server, wrong)[0] == 429


def test_throttled_put_does_not_read_body(server):
    freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(LoginThrottle.FREE_FAILURES):
        assert whoami(server, wrong)[0] == 401
    client = socket.create_connection(server.server_address[:2], timeout=2)
    try:
        client.sendall((
            "PUT /v2/objects/" + "a" * 64 + " HTTP/1.1\r\n"
            "Host: x\r\n"
            "Authorization: Bearer " + wrong + "\r\n"
            "Content-Length: 1048576\r\n"
            "\r\n"
        ).encode())
        response = http.client.HTTPResponse(client, method="PUT")
        response.begin()
        body = response.read()
        headers = dict(response.getheaders())
        assert response.status == 429
        assert json.loads(body) == {"error": "too many failed authentications; try again later"}
        assert headers.get("Connection") == "close"
        assert int(headers["Retry-After"]) >= 1
    finally:
        client.close()


def test_bearer_other_address_not_throttled(server):
    freeze_throttle(server)
    wrong = "not-a-key"
    for _ in range(LoginThrottle.FREE_FAILURES):
        assert whoami(server, wrong)[0] == 401
    assert whoami(server, server.admin_key)[0] == 429
    try:
        status, payload, _ = whoami(server, server.admin_key, source="127.0.0.2")
    except OSError:
        if sys.platform.startswith("linux"):
            raise
        pytest.skip("cannot bind 127.0.0.2")
    assert status == 200
    assert payload["key"] == "admin"


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

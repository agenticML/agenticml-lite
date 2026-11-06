"""HTTP-level tests for the protocol v2 Client."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import hashlib
import io
import json
import socket
import threading
import time

import pytest
import requests

from agenticml.client import Client, Conflict, ProtocolError, SyncStopped
from agenticml.server.app import make_server
from agenticml.server.keys import KeyStore


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


def origin(httpd):
    host, port = httpd.server_address[:2]
    return f"http://{host}:{port}"


def client_for(server, key=None, **kwargs):
    return Client(base_url=origin(server), api_key=server.test_key if key is None else key, **kwargs)


class _FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length)

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append(("GET", self.path, self.headers, b""))
        self._dispatch()

    def do_POST(self):
        body = self._read_body()
        self.server.requests.append(("POST", self.path, self.headers, body))
        self._dispatch()

    def do_PUT(self):
        body = self._read_body()
        self.server.requests.append(("PUT", self.path, self.headers, body))
        self._dispatch()

    def _dispatch(self):
        self.server.handler(self)


@contextmanager
def fake_http(handler):
    httpd = HTTPServer(("127.0.0.1", 0), _FakeHandler)
    httpd.handler = handler
    httpd.requests = []
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_whoami_sync_and_admin_and_revoked(server):
    with client_for(server) as client:
        assert client.whoami() == {
            "key": "one", "scope": "sync", "protocol": 2,
            "features": ["lineage"] if server.lineage_enabled else [],
        }
    _, admin = server.keys.create("boss", "admin")
    with client_for(server, admin) as client:
        assert client.whoami()["scope"] == "admin"
    with client_for(server, "wrong-key") as client:
        with pytest.raises(requests.HTTPError) as caught:
            client.whoami()
        assert caught.value.response.status_code == 401
        assert "wrong-key" not in str(caught.value)
    KeyStore(server.data_dir / "server").revoke("one")
    with client_for(server) as client:
        with pytest.raises(requests.HTTPError) as caught:
            client.whoami()
        assert caught.value.response.status_code == 401
        assert server.test_key not in str(caught.value)
        assert server.test_key not in repr(client)


@pytest.mark.parametrize("status,body", [
    (404, b'{"error":"not found"}'),
    (200, b"not json"),
    (200, b'{"protocol": 1}'),
])
def test_whoami_protocol_error(status, body):
    def handler(request):
        request._send(status, body, {"Content-Type": "application/json"})

    with fake_http(handler) as (_httpd, url):
        with Client(base_url=url, api_key="secret-key") as client:
            with pytest.raises(ProtocolError, match="is not an agenticml server with protocol 2") as caught:
                client.whoami()
            assert "secret-key" not in str(caught.value)


@pytest.mark.parametrize("status", [408, 429, 501, 520])
def test_whoami_unreachable_http_error(status):
    def handler(request):
        request._send(status, b'{"error":"busy"}', {"Content-Type": "application/json"})

    with fake_http(handler) as (_httpd, url):
        with Client(base_url=url, api_key="secret-key", max_retries=0) as client:
            with pytest.raises(requests.HTTPError) as caught:
                client.whoami()
            assert caught.value.response.status_code == status
            assert "secret-key" not in str(caught.value)


def test_round_trip_missing_object_file_append_state_commit(server):
    data = b"object bytes"
    sha = hashlib.sha256(data).hexdigest()
    meta = json.dumps({"run_id": "run-1", "status": "running"}).encode()
    events = b'{"t":1}\n'
    with client_for(server) as client:
        assert client.missing([sha]) == [sha]
        assert client.put_object(sha, io.BytesIO(data), len(data)) is True
        assert client.put_object(sha, io.BytesIO(data), len(data)) is False
        assert client.missing([sha]) == []
        assert client.put_file("run-1", "meta.json", meta) == {"size": len(meta)}
        assert client.append("run-1", "events.jsonl", 0, events) == len(events)
        state = client.run_state("run-1")
        assert state["owner"] == "one"
        assert "meta.json" in state["files"]
        assert "events.jsonl" in state["files"]
        fingerprint = "a" * 64
        assert client.commit("run-1", "running", fingerprint, {
            "meta.json": len(meta), "events.jsonl": len(events),
        }) == {"complete": True, "fingerprint": fingerprint}
        with pytest.raises(Conflict) as caught:
            client.commit("run-1", "running", fingerprint, {
                "meta.json": 0, "events.jsonl": 2,
            })
        assert "sizes" in caught.value.payload
        assert server.test_key not in str(caught.value)
        assert client.run_state("missing-run") is None


def test_missing_splits_25000_hashes_into_three_requests(server):
    hashes = [f"{index:064x}" for index in range(25000)]
    with client_for(server) as client:
        seen = []
        original = client.session.request

        def wrapped(method, url, **kwargs):
            if method == "POST" and url.endswith("/v2/objects/missing"):
                seen.append(kwargs["json"]["objects"])
            return original(method, url, **kwargs)

        client.session.request = wrapped
        assert client.missing(hashes) == hashes
        assert [len(chunk) for chunk in seen] == [10000, 10000, 5000]


def test_put_object_retries_503_and_rewinds(monkeypatch):
    monkeypatch.setattr("agenticml.client.time.sleep", lambda _delay: None)
    bodies = []

    def handler(request):
        bodies.append(request.server.requests[-1][3])
        if len(bodies) == 1:
            request._send(503, b'{"error":"busy"}', {"Retry-After": "0"})
            return
        request._send(201, b'{"created":true}')

    payload = b"full-object-body"
    sha = hashlib.sha256(payload).hexdigest()
    with fake_http(handler) as (_httpd, url):
        with Client(base_url=url, api_key="secret-key") as client:
            assert client.put_object(sha, io.BytesIO(payload), len(payload)) is True
    assert bodies == [payload, payload]


def test_put_lineage_retries_503_and_rewinds():
    bodies = []

    def handler(request):
        bodies.append(request.server.requests[-1][3])
        assert request.path == "/v2/runs/run-1/lineage"
        if len(bodies) == 1:
            request._send(503, b'{"error":"busy"}', {"Retry-After": "0"})
        else:
            request._send(201, b'{"created":true,"commit":"accepted"}')

    payload = b"full-bundle-body"
    with fake_http(handler) as (_httpd, url):
        with Client(base_url=url, api_key="secret-key") as client:
            assert client.put_lineage("run-1", io.BytesIO(payload), len(payload)) == {
                "created": True, "commit": "accepted",
            }
    assert bodies == [payload, payload]


def test_connection_refused_retries_then_raises(monkeypatch):
    monkeypatch.setattr("agenticml.client.time.sleep", lambda _delay: None)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with Client(base_url=f"http://127.0.0.1:{port}", api_key="secret-key", max_retries=2) as client:
        seen = []
        original = client.session.request

        def wrapped(*args, **kwargs):
            seen.append(1)
            return original(*args, **kwargs)

        client.session.request = wrapped
        with pytest.raises(requests.ConnectionError) as caught:
            client.whoami()
        assert len(seen) == 3
        assert "secret-key" not in str(caught.value)


def test_4xx_is_not_retried():
    def handler(request):
        request._send(400, b'{"error":"bad request"}')

    with fake_http(handler) as (httpd, url):
        with Client(base_url=url, api_key="secret-key") as client:
            with pytest.raises(requests.HTTPError) as caught:
                client.missing(["a" * 64])
            assert caught.value.response.status_code == 400
        assert len(httpd.requests) == 1


def test_persistent_503_retries_then_http_error(monkeypatch):
    monkeypatch.setattr("agenticml.client.time.sleep", lambda _delay: None)

    def handler(request):
        request._send(503, b'{"error":"busy"}')

    with fake_http(handler) as (httpd, url):
        with Client(base_url=url, api_key="secret-key", max_retries=2) as client:
            with pytest.raises(requests.HTTPError) as caught:
                client.whoami()
            assert caught.value.response.status_code == 503
        assert len(httpd.requests) == 3


def test_retry_after_capped_and_nan_falls_back(monkeypatch):
    sleeps = []
    monkeypatch.setattr("agenticml.client.time.sleep", sleeps.append)

    def handler(request):
        count = len(request.server.requests)
        if count == 1:
            request._send(503, b'{"error":"busy"}', {"Retry-After": "999"})
            return
        if count == 2:
            request._send(503, b'{"error":"busy"}', {"Retry-After": "nan"})
            return
        request._send(200, b'{"protocol":2,"key":"one","scope":"sync","features":[]}')

    with fake_http(handler) as (_httpd, url):
        with Client(base_url=url, api_key="secret-key") as client:
            assert client.whoami()["protocol"] == 2
    assert sleeps == [30.0, 1.0]


def test_chunked_encoding_error_is_retried(monkeypatch):
    monkeypatch.setattr("agenticml.client.time.sleep", lambda _delay: None)
    seen = []

    def handler(request):
        request._send(200, b'{"protocol":2,"key":"one","scope":"sync","features":[]}')

    with fake_http(handler) as (_httpd, url):
        with Client(base_url=url, api_key="secret-key", max_retries=2) as client:
            original = client.session.request

            def wrapped(*args, **kwargs):
                seen.append(1)
                if len(seen) < 3:
                    raise requests.exceptions.ChunkedEncodingError("truncated")
                return original(*args, **kwargs)

            client.session.request = wrapped
            assert client.whoami()["protocol"] == 2
    assert len(seen) == 3


def test_redirect_refuses_to_follow_or_send_key():
    stolen = []

    def sink(request):
        stolen.append(dict(request.headers))
        request._send(200, b'{"ok":true}')

    def bounce(location):
        def handler(request):
            request._send(302, b"", {"Location": location})
        return handler

    with fake_http(sink) as (_sink, sink_url):
        with fake_http(bounce(sink_url + "/steal")) as (_httpd, url):
            with Client(base_url=url, api_key="secret-key") as client:
                with pytest.raises(ValueError, match="server redirected; refusing to send the API key") as caught:
                    client.whoami()
                assert "steal" not in str(caught.value)
                assert "secret-key" not in str(caught.value)
                assert "secret-key" not in repr(client)
    assert stolen == []


def test_key_not_in_repr_or_exceptions(server):
    secret = "super-secret-api-key"
    with Client(base_url=origin(server), api_key=secret) as client:
        assert secret not in repr(client)
        with pytest.raises(requests.HTTPError) as caught:
            client.whoami()
        text = str(caught.value) + repr(caught.value)
        assert secret not in text
        assert secret not in repr(client)


def test_no_host_raises(monkeypatch):
    monkeypatch.delenv("AGENTICML_HOST", raising=False)
    with pytest.raises(ValueError, match="no server host"):
        Client(api_key="test")


def test_env_and_explicit_host(monkeypatch):
    monkeypatch.setenv("AGENTICML_HOST", "https://override.example")
    monkeypatch.setenv("AGENTICML_API_KEY", "envkey")
    with Client() as client:
        assert client.base_url == "https://override.example"
        assert client.api_key == "envkey"
    with Client(base_url="https://explicit.example") as client:
        assert client.base_url == "https://explicit.example"


def test_empty_env_key_treated_as_unset(monkeypatch):
    monkeypatch.setenv("AGENTICML_HOST", "https://example.com")
    monkeypatch.setenv("AGENTICML_API_KEY", "")
    with Client() as client:
        assert client.api_key is None
        assert "Authorization" not in client.session.headers
    monkeypatch.setenv("AGENTICML_HOST", "")
    with pytest.raises(ValueError, match="no server host"):
        Client()


def test_host_is_normalised_and_remote_http_needs_insecure():
    assert Client(base_url="HTTPS://API.test.local:443/").base_url == "https://api.test.local"
    assert Client(base_url="http://127.0.0.1:80/").base_url == "http://127.0.0.1"
    assert Client(base_url="http://example.com", insecure=True).base_url == "http://example.com"
    with pytest.raises(ValueError, match="insecure"):
        Client(base_url="http://example.com")
    with pytest.raises(ValueError, match="scheme"):
        Client(base_url="ftp://example.com")


def test_stop_event_blocks_request_and_wakes_backoff():
    sent = []

    def handler(request):
        sent.append(request.path)
        request._send(200, b'{"protocol":2,"key":"one","scope":"sync","features":[]}')

    stop = threading.Event()
    stop.set()
    with fake_http(handler) as (_httpd, url):
        with Client(base_url=url, api_key="secret-key", stop=stop) as client:
            with pytest.raises(SyncStopped):
                client.whoami()
    assert sent == []

    stop = threading.Event()
    box = []
    with Client(base_url="http://127.0.0.1:1", api_key="secret-key", stop=stop) as client:
        def run():
            try:
                client._backoff(0, retry_after=30)
            except SyncStopped as exc:
                box.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.05)
        stop.set()
        thread.join(1)
        assert not thread.is_alive()
        assert box
        assert "secret-key" not in str(box[0])

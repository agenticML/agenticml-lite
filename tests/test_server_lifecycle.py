"""Graceful signal shutdown through the CLI and connection drain."""
from server_db_helpers import default_store
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from agenticml.server import app
from agenticml.server.keys import KeyStore
from server_db_helpers import default_workspace_id

pytest.importorskip("fcntl")
if sys.platform != "linux" or not hasattr(signal, "SIGTERM"):
    pytest.skip("requires Linux and SIGTERM", allow_module_level=True)


@contextmanager
def running_cli(data, keep_alive=False):
    app.prepare_data_dir(data)
    _, key = KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).create("admin", "admin")
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("AGENTICML_SERVER_KEY", None)
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    # pytest may ignore SIGINT; exec keeps that, so restore the default.
    entry = (
        "import signal; "
        "signal.signal(signal.SIGINT, signal.default_int_handler); "
    )
    if keep_alive:
        entry += (
            "from agenticml.server.ingest import IngestHandler; "
            "IngestHandler.protocol_version = 'HTTP/1.1'; "
        )
    entry += "from agenticml.cli import main; raise SystemExit(main())"
    process = subprocess.Popen(
        [sys.executable, "-c", entry, "server", "serve",
         "--data", str(data), "--port", "0"],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert select.select([process.stdout], [], [], 15)[0], "server did not announce its address"
        line = process.stdout.readline()
        assert line.startswith("AgenticML server: http://127.0.0.1:"), line
        address = urlsplit(line.split()[2])
        yield process, (address.hostname, address.port), key
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def read_response(client):
    response = http.client.HTTPResponse(client)
    try:
        response.begin()
        body = response.read()
        return response.status, json.loads(body), response.will_close
    finally:
        response.close()


@contextmanager
def idle_connection(address):
    with socket.create_connection(address, timeout=3) as client:
        client.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert read_response(client) == (200, {"ok": True}, False)
        yield client


def assert_stopped(process):
    output, _ = process.communicate(timeout=5)
    assert process.returncode == 0, output
    assert "stopped" in output


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "ctrl-c"])
def test_signal_drains_append_and_releases_lock(tmp_path, stop_signal):
    data = tmp_path / "data"
    body = b"01234567" * 8192
    with running_cli(data) as (process, address, key):
        conn = http.client.HTTPConnection(*address, timeout=3)
        try:
            conn.request(
                "PUT", "/v2/runs/run-1/files/meta.json",
                json.dumps({"run_id": "run-1", "status": "running"}).encode(),
                {"Authorization": "Bearer " + key},
            )
            response = conn.getresponse()
            assert response.status == 200, response.read()
            response.read()
        finally:
            conn.close()
        with socket.create_connection(address, timeout=3) as idle, socket.create_connection(address, timeout=3) as client:
            client.sendall(
                ("POST /v2/runs/run-1/append/scalars/0.bin?offset=0 HTTP/1.1\r\n"
                 "Host: localhost\r\n"
                 f"Authorization: Bearer {key}\r\n"
                 f"Content-Length: {len(body)}\r\n\r\n").encode()
                + body[:len(body) // 2]
            )
            deadline = time.monotonic() + 3
            while not list((data / "server" / "tmp").glob(".body.*.tmp")):
                assert time.monotonic() < deadline, "append did not enter the body state"
                time.sleep(0.01)
            process.send_signal(stop_signal)
            # Idle EOF proves server_close is draining, past the accept loop's poll.
            assert idle.recv(1) == b""
            time.sleep(0.2)
            assert process.poll() is None
            if stop_signal == signal.SIGTERM:
                process.send_signal(signal.SIGTERM)
            client.sendall(body[len(body) // 2:])
            status, _, _ = read_response(client)
            assert 200 <= status < 300
        assert_stopped(process)
    assert (default_store(data) / "runs" / "run-1" / "scalars" / "0.bin").read_bytes() == body
    with app.make_server(data, port=0):
        pass


def test_sigterm_closes_idle_keep_alive_promptly(tmp_path):
    with running_cli(tmp_path / "data", keep_alive=True) as (process, address, _):
        with idle_connection(address) as idle:
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            assert_stopped(process)
            assert time.monotonic() - started < 3
            assert idle.recv(1) == b""


@pytest.mark.parametrize("interrupt_close", [False, True])
def test_serve_restores_handler_and_ignores_repeated_sigterm(monkeypatch, interrupt_close):
    previous = signal.getsignal(signal.SIGTERM)
    stopped = threading.Event()
    workers = []
    closed = []
    installed = []

    def shutdown():
        workers.append(threading.current_thread())
        stopped.set()

    def serve_forever():
        handler = signal.getsignal(signal.SIGTERM)
        installed.append(handler)
        handler(signal.SIGTERM, None)
        handler(signal.SIGTERM, None)
        assert stopped.wait(3)

    def server_close(*, drain):
        assert signal.getsignal(signal.SIGTERM) is installed[0]
        closed.append(drain)
        if interrupt_close:
            raise KeyboardInterrupt

    server = SimpleNamespace(
        keys=SimpleNamespace(list=lambda: ["admin"]),
        server_address=("127.0.0.1", 12345), data_dir="data", public_url=None,
        serve_forever=serve_forever, shutdown=shutdown, server_close=server_close,
    )
    monkeypatch.setattr(app, "make_server", lambda *args: server)
    if interrupt_close:
        with pytest.raises(KeyboardInterrupt):
            app.serve()
    else:
        app.serve()
    assert signal.getsignal(signal.SIGTERM) is previous
    assert closed == [app.SHUTDOWN_DRAIN]
    assert len(workers) == 1
    assert workers[0] is not threading.main_thread()
    assert workers[0].daemon


def test_serve_off_main_thread_skips_signal_install(monkeypatch):
    calls = []
    errors = []

    def forbidden(*args):
        raise AssertionError("signal handler installed off the main thread")

    server = SimpleNamespace(
        keys=SimpleNamespace(list=lambda: ["admin"]),
        server_address=("127.0.0.1", 12345), data_dir="data", public_url=None,
        serve_forever=lambda: calls.append("serve"),
        server_close=lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(app, "make_server", lambda *args: server)
    monkeypatch.setattr(signal, "signal", forbidden)

    def run():
        try:
            app.serve()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert not errors
    assert calls == ["serve", {"drain": app.SHUTDOWN_DRAIN}]

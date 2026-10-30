"""Connection admission and absolute header deadlines over real TCP sockets."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import socket
import threading
import time

import pytest

from agenticml.ui import server


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "server did not reach the expected state"


@pytest.fixture
def limited_dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(server, '_MAX_CONNECTIONS', 4)
    monkeypatch.setattr(server, '_HEADER_DEADLINE', 5.0)
    monkeypatch.setattr(server, '_CONNECTION_TIMEOUT', 3.0)
    run = tmp_path / 'runs' / 'run-1'
    run.mkdir(parents=True)
    (run / 'meta.json').write_text(json.dumps({
        'format_version': 1, 'run_id': 'run-1', 'project': 'demo', 'name': 'test',
        'status': 'finished',
    }))
    httpd = server.make_server(tmp_path, '127.0.0.1', 0)
    thread = threading.Thread(target=httpd.serve_forever,
                              kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    clients = []

    def connect():
        client = socket.create_connection(httpd.server_address[:2], timeout=3)
        clients.append(client)
        return client

    yield httpd, connect
    for client in clients:
        client.close()
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()
    wait_until(lambda: not connections(httpd))


def connections(httpd):
    with httpd.connection_lock:
        result = []
        for conn, state in httpd.connections.items():
            try:
                port = conn.getpeername()[1]
            except OSError:
                # shutdown_request closes the socket before finally releases
                # its slot. Keep counting that slot until it is removed.
                port = None
            result.append((port, state))
        return result


def is_waiting(httpd, client):
    return (client.getsockname()[1], 'headers') in connections(httpd)


def get_runs(httpd):
    conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=2)
    try:
        conn.request('GET', '/api/runs')
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def assert_closed(client):
    try:
        assert client.recv(1) == b''
    except ConnectionResetError:
        pass


def test_idle_connections_are_evicted_for_normal_get(limited_dashboard):
    httpd, connect = limited_dashboard
    clients = []
    for _ in range(10):
        client = connect()
        clients.append(client)
        wait_until(lambda: is_waiting(httpd, client))
        assert len(connections(httpd)) <= 4
    for client in clients[:6]:
        assert_closed(client)
    started = time.monotonic()
    status, body = get_runs(httpd)
    assert status == 200
    assert json.loads(body)[0]['run_id'] == 'run-1'
    assert time.monotonic() - started < 2
    # Admission evicts the oldest remaining connection, not the newest one.
    assert_closed(clients[6])
    assert is_waiting(httpd, clients[-1])


@pytest.mark.parametrize('prefix', [b'G', b'GET /api/runs HTTP/1.1\r\nHost: localhost\r\nX-Slow: '])
def test_trickled_request_line_and_headers_hit_total_deadline(
        limited_dashboard, monkeypatch, prefix):
    httpd, connect = limited_dashboard
    monkeypatch.setattr(server, '_HEADER_DEADLINE', 0.5)
    client = connect()
    wait_until(lambda: is_waiting(httpd, client))
    stopped = threading.Event()

    def trickle():
        try:
            client.sendall(prefix)
            while not stopped.wait(0.2):
                client.sendall(b'x')
        except OSError:
            pass

    started = time.monotonic()
    thread = threading.Thread(target=trickle, daemon=True)
    thread.start()
    try:
        assert_closed(client)
        assert time.monotonic() - started < 1.5
        wait_until(lambda: not connections(httpd))
        assert get_runs(httpd)[0] == 200
    finally:
        stopped.set()
        thread.join(timeout=3)


def test_header_deadline_includes_wait_for_first_byte(limited_dashboard, monkeypatch):
    httpd, connect = limited_dashboard
    monkeypatch.setattr(server, '_HEADER_DEADLINE', 0.5)
    started = time.monotonic()
    client = connect()
    assert_closed(client)
    assert time.monotonic() - started < 1.5
    wait_until(lambda: not connections(httpd))


def test_keep_alive_wait_is_evicted_and_deadline_resets(limited_dashboard, monkeypatch):
    httpd, connect = limited_dashboard
    monkeypatch.setattr(server, '_MAX_CONNECTIONS', 2)
    monkeypatch.setattr(server, '_HEADER_DEADLINE', 2.0)
    monkeypatch.setattr(server.DashboardHandler, 'protocol_version', 'HTTP/1.1')
    client = connect()
    for request in range(3):
        if request:
            time.sleep(1.1)
        client.sendall(b'GET /api/runs HTTP/1.1\r\nHost: localhost\r\n\r\n')
        response = http.client.HTTPResponse(client)
        response.begin()
        assert response.status == 200
        response.read()
        response.close()
        wait_until(lambda: is_waiting(httpd, client))
    # Total connection age exceeds the deadline; each request gets a new budget.
    idle = connect()
    wait_until(lambda: is_waiting(httpd, idle))
    assert get_runs(httpd)[0] == 200
    assert_closed(client)
    assert is_waiting(httpd, idle)


def test_keep_alive_activity_refreshes_idle_eviction_order(limited_dashboard, monkeypatch):
    httpd, connect = limited_dashboard
    monkeypatch.setattr(server, '_MAX_CONNECTIONS', 2)
    monkeypatch.setattr(server.DashboardHandler, 'protocol_version', 'HTTP/1.1')
    client = connect()
    wait_until(lambda: is_waiting(httpd, client))
    idle = connect()
    wait_until(lambda: is_waiting(httpd, idle))

    client.sendall(b'GET /api/runs HTTP/1.1\r\nHost: localhost\r\n\r\n')
    response = http.client.HTTPResponse(client)
    response.begin()
    assert response.status == 200
    response.read()
    response.close()
    wait_until(lambda: is_waiting(httpd, client))

    assert get_runs(httpd)[0] == 200
    assert_closed(idle)
    assert is_waiting(httpd, client)


def test_busy_puts_are_not_evicted_and_refusal_releases_no_slots(
        limited_dashboard, monkeypatch):
    httpd, connect = limited_dashboard
    monkeypatch.setattr(server, '_MAX_CONNECTIONS', 2)
    clients = [connect(), connect()]
    for client in clients:
        client.sendall(
            b'PUT /api/runs/run-1/notes HTTP/1.1\r\nHost: localhost\r\n'
            b'Content-Type: application/json\r\n'
            b'X-AgenticML-Request: 1\r\nContent-Length: 100\r\n\r\n')
    wait_until(lambda: len(connections(httpd)) == 2 and
               all(state == 'busy' for _, state in connections(httpd)))
    before = connections(httpd)
    started = time.monotonic()
    for _ in range(8):
        status, body = get_runs(httpd)
        assert status == 503
        assert body == b''
    assert time.monotonic() - started < 2
    assert connections(httpd) == before
    clients[0].close()
    wait_until(lambda: len(connections(httpd)) == 1)
    assert get_runs(httpd)[0] == 200


def test_body_reads_restore_connection_timeout(limited_dashboard, monkeypatch):
    httpd, connect = limited_dashboard
    monkeypatch.setattr(server, '_HEADER_DEADLINE', 0.3)
    client = connect()
    body = b'{"text":"saved after header deadline","revision":null}'
    client.sendall(
        b'PUT /api/runs/run-1/notes HTTP/1.1\r\nHost: localhost\r\n'
        b'Content-Type: application/json\r\n'
        b'X-AgenticML-Request: 1\r\nConnection: close\r\n' +
        f'Content-Length: {len(body)}\r\n\r\n'.encode())
    wait_until(lambda: any(state == 'busy' for _, state in connections(httpd)))
    with httpd.connection_lock:
        assert next(iter(httpd.connections)).gettimeout() > 1
    time.sleep(0.6)
    client.sendall(body)
    response = http.client.HTTPResponse(client)
    response.begin()
    assert response.status == 200
    assert json.loads(response.read())['text'] == 'saved after header deadline'
    response.close()


def test_concurrent_normal_requests_release_slots(limited_dashboard):
    httpd, _ = limited_dashboard
    barrier = threading.Barrier(3)

    def request(_):
        barrier.wait(timeout=5)
        return get_runs(httpd)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(request, range(3)))
    assert all(status == 200 for status, _ in results)
    wait_until(lambda: not connections(httpd))
    assert get_runs(httpd)[0] == 200


def test_handler_exception_releases_slot_after_shutdown(limited_dashboard, monkeypatch):
    httpd, connect = limited_dashboard
    monkeypatch.setattr(server, '_MAX_CONNECTIONS', 1)
    original = httpd.finish_request
    shut_down = threading.Event()
    shutdown_request = httpd.shutdown_request
    seen = []

    def fail(request, address):
        raise RuntimeError('handler failed')

    def shutdown(request):
        shutdown_request(request)
        with httpd.connection_lock:
            seen.append(request in httpd.connections)
        shut_down.set()

    monkeypatch.setattr(httpd, 'finish_request', fail)
    monkeypatch.setattr(httpd, 'shutdown_request', shutdown)
    client = connect()
    assert_closed(client)
    assert shut_down.wait(3)
    assert seen == [True]
    wait_until(lambda: not connections(httpd))
    monkeypatch.setattr(httpd, 'finish_request', original)
    assert get_runs(httpd)[0] == 200

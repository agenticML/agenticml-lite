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


def assert_server_closed(client):
    """The server shut the socket down. Bytes already queued may arrive first."""
    client.settimeout(1)
    try:
        while client.recv(65536):
            pass
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        pass


def note_put(client, length, body=b''):
    client.sendall(
        b'PUT /api/runs/run-1/notes HTTP/1.1\r\nHost: localhost\r\n'
        b'Content-Type: application/json\r\n'
        b'X-AgenticML-Request: 1\r\n'
        + f'Content-Length: {length}\r\n\r\n'.encode() + body)


def response_queued(client):
    """True once the peer has sent a byte or hung up. Does not consume it."""
    client.settimeout(0)
    try:
        client.recv(1, socket.MSG_PEEK)
    except (BlockingIOError, TimeoutError, socket.timeout):
        return False
    except OSError:
        return True
    return True


def read_response(client, timeout=1):
    client.settimeout(timeout)
    response = http.client.HTTPResponse(client)
    response.begin()
    try:
        return response.status, response.read()
    finally:
        response.close()


def note_body_limit():
    return min(server._MAX_NOTE_PUTS, max(1, server._MAX_CONNECTIONS - 1))


def fill_stalled_note_bodies(httpd, connect):
    """Hold every note-body slot with a PUT that stopped after the headers.

    Callers set the caps so one handler slot stays free. Each PUT is
    finished into "body" before the next connect; a peer left in "headers"
    would be evicted with no response.
    """
    cap = note_body_limit()
    assert cap < server._MAX_CONNECTIONS
    clients = []
    for _ in range(cap):
        wait_until(lambda: len(connections(httpd)) < server._MAX_CONNECTIONS)
        client = connect()
        clients.append(client)
        note_put(client, 200000)
        port = client.getsockname()[1]

        def settled(port=port):
            return (port, 'body') in connections(httpd)

        wait_until(settled)
    wait_until(lambda: [state for _, state in connections(httpd)].count('body') == cap)
    return clients


def limit_send_buffer(httpd, monkeypatch):
    # Linux grows SO_SNDBUF to a few megabytes after accept. A response that
    # fits there returns from sendall even when the client never reads, so a
    # modest object would not stay in "writing".
    original = httpd.get_request

    def get_request():
        request, address = original()
        try:
            request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        except OSError:
            pass
        return request, address

    monkeypatch.setattr(httpd, 'get_request', get_request)


def large_object(httpd, size=8 * 1024 * 1024):
    # Big enough that macOS loopback buffers cannot absorb the whole response.
    sha = 'a' * 64
    directory = httpd.root / 'objects' / sha[:2]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / sha).write_bytes(b'x' * size)
    return sha


def connect_slow_reader(httpd):
    client = socket.socket()
    # Small receive window, set before connect, so an unread response fills
    # the server send buffer and blocks in sendall.
    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    client.settimeout(3)
    client.connect(httpd.server_address[:2])
    return client


def request_object(client, sha):
    client.sendall(
        f'GET /api/objects/{sha} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())


def stalled_writers(httpd, count, stall):
    with httpd.connection_lock:
        if len(httpd.connections) != count:
            return False
        now = time.monotonic()
        for conn, state in httpd.connections.items():
            if state != 'writing':
                return False
            started = httpd.write_started.get(conn)
            if started is None or now - started < stall:
                return False
        return True


def close_all(clients):
    for client in clients:
        try:
            client.close()
        except OSError:
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
        note_put(client, 100)
    # One body is admitted. The other is refused immediately and its slot
    # is released, instead of both sitting in "busy" until the put deadline.
    wait_until(lambda: [state for _, state in connections(httpd)] == ['body'])
    before = connections(httpd)
    held = before[0][0]
    refused = [client for client in clients if client.getsockname()[1] != held]
    assert len(refused) == 1
    started = time.monotonic()
    status, payload = read_response(refused[0])
    assert status == 503
    assert payload == b''
    assert time.monotonic() - started < 1
    status, body = get_runs(httpd)
    assert status == 200
    assert json.loads(body)[0]['run_id'] == 'run-1'
    assert time.monotonic() - started < 2
    # The in-flight PUT is not evicted to make room for the GET. The GET
    # itself can still be in "writing" for the moment after its sendall.
    assert (held, 'body') in connections(httpd)
    assert sum(state == 'body' for _, state in connections(httpd)) == 1


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
    wait_until(lambda: any(state == 'body' for _, state in connections(httpd)))
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


def test_stalled_put_bodies_cannot_fill_every_slot(limited_dashboard, monkeypatch):
    httpd, connect = limited_dashboard
    monkeypatch.setattr(server, '_MAX_CONNECTIONS', 4)
    monkeypatch.setattr(server, '_MAX_NOTE_PUTS', 4)
    cap = note_body_limit()
    assert cap == 3
    # Finish each PUT before the next connect. A peer still in "headers"
    # would be evicted with no response, which is not the body-cap 503.
    clients = []
    observed = []

    def snapshot():
        current = connections(httpd)
        observed.append(sum(state == 'body' for _, state in current))
        return current

    for _ in range(8):
        # One free slot, then this PUT only. A second connect while the
        # previous one is still in "headers" would evict it with no response.
        wait_until(lambda: len(connections(httpd)) < server._MAX_CONNECTIONS)
        client = connect()
        clients.append(client)
        note_put(client, 200000)
        port = client.getsockname()[1]

        def settled(port=port, client=client):
            for seen, state in snapshot():
                if seen == port:
                    return state == 'body'
            # Not accepted yet looks the same as "already gone". A queued
            # response (or a close) is what shows the handler finished.
            return response_queued(client)

        wait_until(settled)
    assert max(observed) <= cap

    def only_bodies():
        current = connections(httpd)
        return (len(current) == cap and
                all(state == 'body' for _, state in current))

    wait_until(only_bodies)
    started = time.monotonic()
    status, body = get_runs(httpd)
    assert status == 200
    assert json.loads(body)[0]['run_id'] == 'run-1'
    assert time.monotonic() - started < 3
    current = connections(httpd)
    assert sum(state == 'body' for _, state in current) == cap
    held = {port for port, state in current if state == 'body'}
    refused = [client for client in clients if client.getsockname()[1] not in held]
    assert len(refused) == len(clients) - cap
    for client in refused:
        status, payload = read_response(client)
        assert status == 503
        assert payload == b''


def test_zero_length_put_is_not_refused_when_body_cap_is_full(
        limited_dashboard, monkeypatch):
    httpd, connect = limited_dashboard
    monkeypatch.setattr(server, '_MAX_CONNECTIONS', 4)
    monkeypatch.setattr(server, '_MAX_NOTE_PUTS', 4)
    held = fill_stalled_note_bodies(httpd, connect)
    held_ports = {client.getsockname()[1] for client in held}
    client = connect()
    note_put(client, 0)
    started = time.monotonic()
    status, _payload = read_response(client)
    # Empty body fails note validation. It must not be the body-cap 503.
    assert status != 503
    assert time.monotonic() - started < 1
    current = connections(httpd)
    assert {port for port, state in current if state == 'body'} == held_ports


def test_over_cap_put_with_body_sent_gets_empty_503(limited_dashboard, monkeypatch):
    httpd, connect = limited_dashboard
    monkeypatch.setattr(server, '_MAX_CONNECTIONS', 4)
    monkeypatch.setattr(server, '_MAX_NOTE_PUTS', 4)
    fill_stalled_note_bodies(httpd, connect)
    client = connect()
    body = b'x' * (200 * 1024)
    started = time.monotonic()
    try:
        note_put(client, len(body), body)
    except OSError:
        pass
    status, payload = read_response(client)
    assert status == 503
    assert payload == b''
    assert time.monotonic() - started < 1


def test_nonreading_clients_are_evicted_when_full(limited_dashboard, monkeypatch):
    httpd, _connect = limited_dashboard
    monkeypatch.setattr(server, '_MAX_CONNECTIONS', 4)
    monkeypatch.setattr(server, '_WRITE_STALL', 0.3)
    limit_send_buffer(httpd, monkeypatch)
    sha = large_object(httpd)
    clients = []
    try:
        for _ in range(4):
            client = connect_slow_reader(httpd)
            clients.append(client)
            request_object(client, sha)
        # Past the stall threshold, so the next admit must evict rather than 503.
        wait_until(lambda: stalled_writers(httpd, 4, 0.35))
        started = time.monotonic()
        status, body = get_runs(httpd)
        assert status == 200
        assert json.loads(body)[0]['run_id'] == 'run-1'
        assert time.monotonic() - started < 3
        ports = {client.getsockname()[1] for client in clients}
        remaining = {port for port, _state in connections(httpd)}
        evicted = [client for client in clients if client.getsockname()[1] not in remaining]
        assert len(evicted) == 1
        assert_server_closed(evicted[0])
        # Ignore the GET if its sendall has not been marked finished yet.
        writing = {port for port, state in connections(httpd)
                   if state == 'writing' and port in ports}
        assert writing == ports - {evicted[0].getsockname()[1]}
    finally:
        close_all(clients)


def test_slow_writer_is_not_evicted_below_capacity(limited_dashboard, monkeypatch):
    httpd, _connect = limited_dashboard
    monkeypatch.setattr(server, '_MAX_CONNECTIONS', 4)
    monkeypatch.setattr(server, '_WRITE_STALL', 0.3)
    limit_send_buffer(httpd, monkeypatch)
    sha = large_object(httpd)
    client = connect_slow_reader(httpd)
    try:
        request_object(client, sha)
        wait_until(lambda: stalled_writers(httpd, 1, 0.35))
        before = connections(httpd)
        assert len(before) == 1 and before[0][1] == 'writing'
        port = before[0][0]
        started = time.monotonic()
        status, body = get_runs(httpd)
        assert status == 200
        assert json.loads(body)[0]['run_id'] == 'run-1'
        assert time.monotonic() - started < 3
        # The GET can still be leaving its slot. The stalled write stays.
        assert (port, 'writing') in connections(httpd)
        client.settimeout(0.2)
        try:
            data = client.recv(1, socket.MSG_PEEK)
        except (socket.timeout, TimeoutError):
            data = b'pending'
        assert data != b''
    finally:
        close_all([client])

"""Package 32: capacity metrics (request counters, pool peaks, CloudWatch publisher)."""
import hashlib
import http.client
import logging
import math
import socket
import sys
import threading
import time

import pytest
from server_db_helpers import on_postgres

from agenticml.server import app, capacity
from agenticml.server.capacity import (MetricsPublisher, NAMESPACE, RequestMetrics, USAGE_EVERY,
                                       _bucket)
from agenticml import cli
from agenticml.server.db import connect
from agenticml.server.db.errors import DatabaseBusy
from agenticml.ui import server as ui_server
from test_server_db import url
from test_server_web import call, running


class FakeClient:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def put_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


def by_name(call_args):
    result = {}
    for datum in call_args['MetricData']:
        result.setdefault(datum['MetricName'], []).append(datum)
    return result


@pytest.fixture
def db(tmp_path):
    database = connect(url(tmp_path / 'state.db'))
    yield database
    database.close()


class FakeServer:
    def __init__(self, db):
        self.db = db
        self.request_metrics = RequestMetrics()


# RequestMetrics


def test_counts_and_error_classes():
    metrics = RequestMetrics()
    for status in (200, 204, 301, 404, 429, 500, 503, None):
        metrics.request_started()
        metrics.request_finished(status, 0.01)
    metrics.rejected()
    metrics.rejected()
    taken = metrics.take()
    assert taken['requests'] == 8
    assert taken['errors_4xx'] == 2
    assert taken['errors_5xx'] == 2
    assert taken['rejected'] == 2
    assert sum(taken['latency'].values()) == 8
    assert metrics.take() == {'requests': 0, 'errors_4xx': 0, 'errors_5xx': 0, 'rejected': 0,
                              'latency': {}, 'in_flight_peak': 0}


def test_uncounted_request_still_ends_in_flight():
    metrics = RequestMetrics()
    metrics.request_started()
    metrics.request_started()
    metrics.request_finished(200, 0.01, count=False)
    taken = metrics.take()
    assert taken['requests'] == 0
    assert taken['latency'] == {}
    assert taken['in_flight_peak'] == 2
    # one request is still running and counts toward the next peak
    assert metrics.take()['in_flight_peak'] == 1


def test_in_flight_peak_carries_over_take():
    metrics = RequestMetrics()
    for _ in range(3):
        metrics.request_started()
    metrics.request_finished(200, 0.001)
    assert metrics.take()['in_flight_peak'] == 3
    assert metrics.take()['in_flight_peak'] == 2
    metrics.request_finished(200, 0.001)
    metrics.request_finished(200, 0.001)
    assert metrics.take()['in_flight_peak'] == 2
    assert metrics.take()['in_flight_peak'] == 0


def test_latency_buckets_are_few_and_close():
    metrics = RequestMetrics()
    durations = [0.0001 * 1.0007 ** n for n in range(20000)]
    assert durations[-1] > 100
    for seconds in durations:
        metrics.request_started()
        metrics.request_finished(200, seconds)
    latency = metrics.take()['latency']
    assert len(latency) < 150
    assert sum(latency.values()) == len(durations)
    ordered = sorted(latency.items())
    for fraction in (0.5, 0.95, 0.99):
        wanted = fraction * len(durations)
        seen = 0
        for value, count in ordered:
            seen += count
            if seen >= wanted:
                break
        true_ms = durations[int(wanted) - 1] * 1000
        assert 1 / 1.27 <= value / true_ms <= 1.27


def test_bucket_edges():
    assert _bucket(0) == 1
    assert _bucket(0.0004) == 1
    assert _bucket(0.001) == 1
    assert _bucket(0.1) == 100
    assert _bucket(3600) == pytest.approx(3.6e6, rel=0.13)
    assert all(math.isfinite(_bucket(s)) for s in (0, 1e-9, 1e6))


def test_threads_keep_totals_exact():
    metrics = RequestMetrics()
    barrier = threading.Barrier(16)

    def work():
        barrier.wait()
        for n in range(500):
            metrics.request_started()
            metrics.request_finished(500 if n % 5 == 0 else 200, 0.002)
        for _ in range(100):
            metrics.rejected()

    threads = [threading.Thread(target=work) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    taken = metrics.take()
    assert taken['requests'] == 16 * 500
    assert taken['errors_5xx'] == 16 * 100
    assert taken['rejected'] == 1600
    assert sum(taken['latency'].values()) == 8000
    assert 1 <= taken['in_flight_peak'] <= 16


# through a running server


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    with running(tmp_path / 'data') as httpd:
        _, httpd.sync_key = httpd.keys.create('sync-box', 'sync')
        yield httpd


def settled_take(httpd):
    """take() once the handler threads have recorded their finished requests."""
    deadline = time.monotonic() + 3
    while httpd.request_metrics._in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    return httpd.request_metrics.take()


def whoami(httpd, key=None):
    headers = {} if key is None else {'Authorization': 'Bearer ' + key}
    return call(httpd, '/v2/whoami', headers=headers)[0]


def test_requests_are_counted_and_health_checks_are_not(served):
    served.request_metrics.take()
    assert whoami(served, served.sync_key) == 200
    assert whoami(served) == 401
    assert call(served, '/v2/nothing/here', headers={'Authorization': 'Bearer ' + served.sync_key})[0] == 404
    for _ in range(3):
        assert call(served, '/healthz')[0] == 200
    taken = settled_take(served)
    assert taken['requests'] == 3
    assert taken['errors_4xx'] == 2
    assert taken['errors_5xx'] == 0
    assert sum(taken['latency'].values()) == 3
    assert taken['in_flight_peak'] >= 1
    assert served.request_metrics._in_flight == 0


def test_server_error_is_counted(served, monkeypatch):
    def boom(record):
        raise RuntimeError('boom')

    monkeypatch.setattr(served.keys, 'touch', boom)
    served.request_metrics.take()
    assert whoami(served, served.sync_key) == 500
    taken = settled_take(served)
    assert (taken['requests'], taken['errors_4xx'], taken['errors_5xx']) == (1, 0, 1)


def test_malformed_and_oversized_request_lines_are_counted(served):
    served.request_metrics.take()
    for raw in (b'GET / HTTP/9.9\r\nHost: x\r\n\r\n', b'GET /' + b'a' * 70000 + b' HTTP/1.1\r\nHost: x\r\n\r\n'):
        with socket.create_connection(served.server_address[:2], timeout=5) as client:
            client.sendall(raw)
            while client.recv(4096):
                pass
    taken = settled_take(served)
    # 505 for the unknown version, 414 for the long request line
    assert (taken['requests'], taken['errors_4xx'], taken['errors_5xx']) == (2, 1, 1)
    assert served.request_metrics._in_flight == 0


def test_no_request_detail_in_the_metrics(served):
    served.request_metrics.take()
    whoami(served)
    call(served, '/v2/secret-path-xyz?token=abc123', headers={'Authorization': 'Bearer nope'})
    taken = settled_take(served)
    text = repr(taken)
    assert 'secret' not in text and 'abc123' not in text


def test_connection_cap_reject_is_counted(served, monkeypatch):
    monkeypatch.setattr(ui_server, '_MAX_CONNECTIONS', 1)
    served.request_metrics.take()
    holder = object()
    with served.connection_lock:
        served.connections[holder] = 'busy'
    client, peer = socket.socketpair()
    try:
        served.process_request(client, ('127.0.0.1', 1))
        peer.settimeout(2)
        assert peer.recv(1024).startswith(b'HTTP/1.1 503')
    finally:
        peer.close()
        with served.connection_lock:
            served.connections.pop(holder, None)
    taken = served.request_metrics.take()
    assert taken['rejected'] == 1
    assert taken['requests'] == 0


# pool_stats


def test_pool_stats_follow_transactions(db):
    assert db.pool_stats() == {'in_use': 0, 'peak': 0} or db.pool_stats()['in_use'] == 0
    db.pool_stats(reset=True)
    with db.transaction():
        assert db.pool_stats()['in_use'] == 1
    stats = db.pool_stats()
    assert stats == {'in_use': 0, 'peak': 1}
    assert db.pool_stats(reset=True) == {'in_use': 0, 'peak': 1}
    assert db.pool_stats() == {'in_use': 0, 'peak': 0}


def test_pool_stats_peak_counts_concurrent_transactions(db):
    db.pool_stats(reset=True)
    inside = threading.Barrier(4)
    release = threading.Event()

    def hold():
        with db.transaction():
            inside.wait(5)
            release.wait(5)

    threads = [threading.Thread(target=hold) for _ in range(3)]
    for thread in threads:
        thread.start()
    inside.wait(5)
    assert db.pool_stats()['in_use'] == 3
    release.set()
    for thread in threads:
        thread.join()
    stats = db.pool_stats(reset=True)
    assert stats['in_use'] == 0 and stats['peak'] == 3
    # the reset restarts the peak from what is held now
    with db.transaction():
        assert db.pool_stats(reset=True) == {'in_use': 1, 'peak': 1}
    assert db.pool_stats() == {'in_use': 0, 'peak': 1}
    assert db.pool_stats(reset=True)['peak'] == 1
    assert db.pool_stats() == {'in_use': 0, 'peak': 0}


@pytest.mark.skipif(not on_postgres(), reason='needs AGENTICML_TEST_DATABASE_URL')
def test_pool_stats_count_session_lock_connections(db):
    db.pool_stats(reset=True)
    with db.session_lock('run:a'):
        assert db.pool_stats()['in_use'] == 1
        with db.session_lock('quota:a'):
            assert db.pool_stats()['in_use'] == 2
            with db.transaction():
                assert db.pool_stats()['in_use'] == 3
    stats = db.pool_stats()
    assert stats == {'in_use': 0, 'peak': 3}
    with db.session_lock('lifetime', shared=True, dedicated=True):
        assert db.pool_stats(reset=True) == {'in_use': 1, 'peak': 3}
        assert db.pool_stats()['peak'] == 1
    assert db.pool_stats() == {'in_use': 0, 'peak': 1}


@pytest.mark.skipif(not on_postgres(), reason='needs AGENTICML_TEST_DATABASE_URL')
def test_session_lock_count_returns_to_zero_after_every_exit(tmp_path):
    holder, other = connect(url(tmp_path / 'a.db')), connect(url(tmp_path / 'a.db'))
    try:
        with holder.session_lock('quota:busy'):
            assert holder.pool_stats()['in_use'] == 1
            with pytest.raises(DatabaseBusy):
                with other.session_lock('quota:busy', try_lock=True):
                    pass
            assert other.pool_stats()['in_use'] == 0
            with pytest.raises(DatabaseBusy):
                with other.session_lock('quota:busy', timeout=0.3):
                    pass
            assert other.pool_stats()['in_use'] == 0
        with pytest.raises(RuntimeError):
            with other.session_lock('quota:free'):
                assert other.pool_stats()['in_use'] == 1
                raise RuntimeError('inside')
        assert other.pool_stats()['in_use'] == 0
        assert holder.pool_stats()['in_use'] == 0
    finally:
        other.close()
        holder.close()


# MetricsPublisher


def test_one_flush_publishes_the_request_and_resource_data(db):
    server = FakeServer(db)
    for status in (200, 200, 404, 500):
        server.request_metrics.request_started()
        server.request_metrics.request_finished(status, 0.05)
    server.request_metrics.rejected()
    client = FakeClient()
    MetricsPublisher(server, client).flush()
    (sent,) = client.calls
    assert sent['Namespace'] == NAMESPACE == 'AgenticML/Server'
    data = by_name(sent)
    values = {name: items[0]['Value'] for name, items in data.items() if name != 'Latency'}
    assert values['Requests'] == 4
    assert values['Errors4xx'] == 1
    assert values['Errors5xx'] == 1
    assert values['Rejected'] == 1
    assert values['InFlightPeak'] == 1
    assert values['DbConnectionsPeak'] >= 0
    for name in ('Requests', 'Errors4xx', 'Errors5xx', 'Rejected', 'InFlightPeak', 'DbConnectionsPeak'):
        assert data[name][0]['Unit'] == 'Count'
    (latency,) = data['Latency']
    assert latency['Unit'] == 'Milliseconds'
    assert sum(latency['Counts']) == 4
    assert len(latency['Values']) == len(latency['Counts'])
    assert 'Value' not in latency
    stamps = {datum['Timestamp'] for datum in sent['MetricData']}
    assert len(stamps) == 1 and stamps.pop().tzinfo is not None


def test_idle_minute_sends_zero_counts_and_no_latency(db):
    server = FakeServer(db)
    client = FakeClient()
    publisher = MetricsPublisher(server, client)
    publisher.flush()
    publisher.flush()
    for sent in client.calls:
        data = by_name(sent)
        assert 'Latency' not in data
        assert data['Requests'][0]['Value'] == 0
        assert data['Rejected'][0]['Value'] == 0


def test_usage_totals_on_the_first_and_every_fifteenth_flush(db):
    server = FakeServer(db)
    client = FakeClient()
    publisher = MetricsPublisher(server, client)
    for _ in range(USAGE_EVERY + 1):
        publisher.flush()
    usage = ('Accounts', 'ActiveUsers7d', 'WorkspacesActive', 'StoredBytes', 'UploadBytesToday')
    for number, sent in enumerate(client.calls):
        data = by_name(sent)
        has = [name in data for name in usage]
        assert all(has) if number in (0, USAGE_EVERY) else not any(has)
    first = by_name(client.calls[0])
    assert first['Accounts'][0]['Unit'] == 'Count'
    assert first['WorkspacesActive'][0]['Value'] == 1
    assert first['StoredBytes'][0]['Unit'] == 'Bytes'
    assert first['UploadBytesToday'][0]['Unit'] == 'Bytes'
    assert len(first) == 11


class RecordingDb:
    """The real database with a scripted pool_stats."""

    def __init__(self, db, peaks):
        self._db = db
        self.peaks = list(peaks)
        self.kwargs = []

    def pool_stats(self, **kwargs):
        self.kwargs.append(kwargs)
        return {'in_use': 0, 'peak': self.peaks.pop(0)}

    def __getattr__(self, name):
        return getattr(self._db, name)


def test_db_connections_peak_is_read_and_reset_each_flush(db):
    recording = RecordingDb(db, [4, 0])
    client = FakeClient()
    publisher = MetricsPublisher(FakeServer(recording), client)
    publisher.flush()
    publisher.flush()
    assert recording.kwargs == [{'reset': True}, {'reset': True}]
    first, second = (by_name(sent)['DbConnectionsPeak'][0] for sent in client.calls)
    assert (first['Value'], first['Unit']) == (4, 'Count')
    assert second['Value'] == 0


def test_db_connections_peak_counts_concurrent_transactions(db):
    db.pool_stats(reset=True)
    inside = threading.Barrier(3)
    release = threading.Event()

    def hold():
        with db.transaction():
            inside.wait(5)
            release.wait(5)

    threads = [threading.Thread(target=hold) for _ in range(2)]
    for thread in threads:
        thread.start()
    inside.wait(5)
    release.set()
    for thread in threads:
        thread.join()
    client = FakeClient()
    MetricsPublisher(FakeServer(db), client).flush()
    assert by_name(client.calls[0])['DbConnectionsPeak'][0]['Value'] == 2


def test_usage_totals_are_published_with_the_right_values_and_units(db, monkeypatch):
    totals = {'accounts': 11, 'active_7d': 7, 'workspaces_active': 3, 'stored_bytes': 12345,
              'upload_bytes_today': 678}
    monkeypatch.setattr(capacity.StatsRepo, 'totals', lambda self, **kwargs: dict(totals))
    server = FakeServer(db)
    server.request_metrics.request_started()
    server.request_metrics.request_finished(200, 0.01)
    client = FakeClient()
    MetricsPublisher(server, client).flush()
    data = by_name(client.calls[0])
    assert set(data) == set(capacity.METRIC_NAMES) and len(capacity.METRIC_NAMES) == 12
    assert {name: (items[0]['Value'], items[0]['Unit']) for name, items in data.items()
            if name in ('Accounts', 'ActiveUsers7d', 'WorkspacesActive', 'StoredBytes',
                        'UploadBytesToday')} == {
        'Accounts': (11, 'Count'), 'ActiveUsers7d': (7, 'Count'), 'WorkspacesActive': (3, 'Count'),
        'StoredBytes': (12345, 'Bytes'), 'UploadBytesToday': (678, 'Bytes')}
    # no requests: every name except Latency
    MetricsPublisher(FakeServer(db), client).flush()
    assert set(by_name(client.calls[1])) == set(capacity.METRIC_NAMES) - {'Latency'}


def test_many_latency_values_are_split(db, monkeypatch):
    monkeypatch.setattr(capacity, 'MAX_VALUES', 10)
    server = FakeServer(db)
    for n in range(40):
        server.request_metrics.request_started()
        server.request_metrics.request_finished(200, 0.001 * 1.3 ** n)
    client = FakeClient()
    MetricsPublisher(server, client).flush()
    parts = by_name(client.calls[0])['Latency']
    assert len(parts) > 1
    assert all(len(part['Values']) <= 10 for part in parts)
    assert sum(sum(part['Counts']) for part in parts) == 40


def test_a_client_error_is_logged_once_and_the_next_flush_runs(db, caplog):
    server = FakeServer(db)
    client = FakeClient(error=RuntimeError('throttled with secret-token'))
    publisher = MetricsPublisher(server, client)
    with caplog.at_level(logging.WARNING, logger='agenticml.server.capacity'):
        publisher._flush_safely()
        publisher._flush_safely()
        client.error = None
        publisher._flush_safely()
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None
    assert len(client.calls) == 3


def test_usage_read_failure_does_not_stop_the_other_metrics(db, monkeypatch):
    def broken(self, **kwargs):
        raise RuntimeError('db gone')

    monkeypatch.setattr(capacity.StatsRepo, 'totals', broken)
    client = FakeClient()
    MetricsPublisher(FakeServer(db), client).flush()
    assert 'Requests' in by_name(client.calls[0]) and 'Accounts' not in by_name(client.calls[0])


def test_the_thread_flushes_each_interval_and_stop_flushes_once_more(db):
    server = FakeServer(db)
    client = FakeClient()
    publisher = MetricsPublisher(server, client, interval=0.05)
    publisher.start()
    deadline = time.monotonic() + 5
    while len(client.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(client.calls) >= 2
    server.request_metrics.rejected()
    started = time.monotonic()
    assert publisher.stop() is True
    assert time.monotonic() - started < 2
    assert not publisher._thread.is_alive()
    done = len(client.calls)
    time.sleep(0.2)
    assert len(client.calls) == done
    assert sum(by_name(call_args)['Rejected'][0]['Value'] for call_args in client.calls) == 1


def test_stop_without_start_is_a_no_op(db):
    client = FakeClient()
    assert MetricsPublisher(FakeServer(db), client).stop() is True
    assert client.calls == []


# serve()


def test_cloudwatch_needs_boto3(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'boto3', None)
    with pytest.raises(ValueError, match=r"needs the \[cloudwatch\] extra.*agenticml-lite\[cloudwatch\]"):
        app.serve(tmp_path / 'data', port=0, metrics='cloudwatch')
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('mode', ['bogus', 'CloudWatch', ''])
def test_serve_rejects_other_metrics_modes(tmp_path, mode):
    with pytest.raises(ValueError, match='AGENTICML_SERVER_METRICS must be off or cloudwatch'):
        app.serve(tmp_path / 'data', port=0, metrics=mode)


@pytest.mark.parametrize('mode', [None, 'off'])
def test_serve_without_a_publisher_does_not_touch_metrics(tmp_path, monkeypatch, mode):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.setattr(app, 'cloudwatch_client', lambda: pytest.fail('client created'))
    monkeypatch.setattr(app, 'MetricsPublisher', lambda *a, **k: pytest.fail('publisher created'))
    app.serve(tmp_path / 'data', port=0, metrics=mode,
              on_ready=lambda s: threading.Thread(target=s.shutdown).start())


def fake_clients(monkeypatch, client, quick=None):
    monkeypatch.setattr(app, 'cloudwatch_client',
                        lambda final=False: (client if quick is None else quick) if final else client)


def test_serve_publishes_and_stops_after_the_drain_before_closing_the_db(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    events = []

    class Recording(FakeClient):
        def put_metric_data(self, **kwargs):
            events.append('put')
            super().put_metric_data(**kwargs)

    client = Recording()
    fake_clients(monkeypatch, client)
    for owner, name in ((app.StatsRefresher, 'stop'), (capacity.MetricsPublisher, 'stop')):
        original = getattr(owner, name)
        monkeypatch.setattr(owner, name, lambda self, *a, _o=original, _n=owner.__name__, **k: (
            events.append(_n + '.stop'), _o(self, *a, **k))[1])
    original_close = ui_server.DashboardServer.server_close
    monkeypatch.setattr(ui_server.DashboardServer, 'server_close',
                        lambda self: (events.append('handlers'), original_close(self))[1])

    def ready(server):
        close = server.db.close
        server.db.close = lambda: (events.append('db.close'), close())[1]
        threading.Thread(target=server.shutdown).start()

    app.serve(tmp_path / 'data', port=0, metrics='cloudwatch', on_ready=ready)
    assert capsys.readouterr().out.startswith('AgenticML server: ')
    assert events == ['handlers', 'StatsRefresher.stop', 'MetricsPublisher.stop', 'put', 'db.close']
    assert len(client.calls) == 1
    assert 'Requests' in by_name(client.calls[0])


def test_server_close_hook_failure_still_closes_the_db_and_the_lock(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    server = app.make_server(tmp_path / 'data', port=0)
    closed = []
    close = server.db.close
    server.db.close = lambda: (closed.append(True), close())[1]

    def boom():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        server.server_close(drain=0.1, before_db_close=boom)
    assert closed == [True]
    assert server._server_lock is None


def test_a_request_in_flight_at_shutdown_is_in_the_final_flush(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    client = FakeClient()
    fake_clients(monkeypatch, client)
    entered, gate = threading.Event(), threading.Event()
    answer = []

    def ready(server):
        _, key = server.keys.create('sync-box', 'sync')
        touch = server.keys.touch

        def slow(record):
            entered.set()
            gate.wait(10)
            return touch(record)

        monkeypatch.setattr(server.keys, 'touch', slow)
        threading.Thread(target=lambda: answer.append(
            call(server, '/v2/whoami', headers={'Authorization': 'Bearer ' + key})[0])).start()

        def stop_later():
            assert entered.wait(10)
            threading.Thread(target=server.shutdown).start()
            time.sleep(0.3)
            gate.set()

        threading.Thread(target=stop_later).start()

    app.serve(tmp_path / 'data', port=0, metrics='cloudwatch', on_ready=ready, stats_interval=0)
    assert answer == [200]
    (sent,) = client.calls
    assert by_name(sent)['Requests'][0]['Value'] == 1
    assert sum(by_name(sent)['Latency'][0]['Counts']) == 1


def test_the_final_flush_uses_its_own_quick_client(db):
    class Slow(FakeClient):
        def put_metric_data(self, **kwargs):
            time.sleep(3)
            super().put_metric_data(**kwargs)

    slow, quick = Slow(), FakeClient()
    publisher = MetricsPublisher(FakeServer(db), slow, interval=60, final_client=quick)
    publisher.start()
    started = time.monotonic()
    assert publisher.stop() is True
    assert time.monotonic() - started < 1.5
    assert slow.calls == [] and len(quick.calls) == 1


def test_serve_warns_once_when_the_final_flush_cannot_finish(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.setattr(app, 'FINAL_JOIN', 0.3)
    entered, gate = threading.Event(), threading.Event()

    class Blocking(FakeClient):
        def put_metric_data(self, **kwargs):
            entered.set()
            gate.wait(30)
            super().put_metric_data(**kwargs)

    client = Blocking()
    fake_clients(monkeypatch, client, quick=FakeClient())
    monkeypatch.setattr(capacity, 'FLUSH', 0.05)
    original = capacity.MetricsPublisher.__init__
    monkeypatch.setattr(capacity.MetricsPublisher, '__init__',
                        lambda self, server, client=None, interval=0.05, final_client=None: original(
                            self, server, client, interval, final_client))

    def ready(server):
        def stop_later():
            assert entered.wait(10)
            server.shutdown()

        threading.Thread(target=stop_later).start()

    stopped = {}
    original_stop = capacity.MetricsPublisher.stop

    def timed_stop(self, timeout=None):
        started = time.monotonic()
        result = original_stop(self, timeout)
        stopped['seconds'] = time.monotonic() - started
        stopped['result'] = result
        return result

    monkeypatch.setattr(capacity.MetricsPublisher, 'stop', timed_stop)
    try:
        with caplog.at_level(logging.WARNING, logger='agenticml.server.capacity'):
            app.serve(tmp_path / 'data', port=0, metrics='cloudwatch', on_ready=ready, stats_interval=0)
    finally:
        gate.set()
    assert stopped['result'] is False
    assert stopped['seconds'] < 2
    records = [r for r in caplog.records if r.name == 'agenticml.server.capacity']
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert 'the last minute was not published' in records[0].getMessage()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(
            t.name == 'agenticml-metrics' and t.is_alive() for t in threading.enumerate()):
        time.sleep(0.02)


@pytest.mark.parametrize('work, skipped', [(0.1, False), (0.5, True)])
def test_flushes_keep_a_fixed_cadence(db, work, skipped):
    starts = []

    class Slow(FakeClient):
        def put_metric_data(self, **kwargs):
            starts.append(time.monotonic())
            time.sleep(work)

    interval = 0.3
    publisher = MetricsPublisher(FakeServer(db), Slow(), interval=interval, final_client=FakeClient())
    publisher.start()
    deadline = time.monotonic() + 10
    while len(starts) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    publisher.stop()
    assert len(starts) >= 5
    gaps = [b - a for a, b in zip(starts, starts[1:5])]
    if skipped:
        # each overrun skips to the next slot on the same grid
        assert all(0.55 < gap < 0.7 for gap in gaps), gaps
    else:
        assert all(0.25 < gap < 0.38 for gap in gaps), gaps
        assert 0.27 < (starts[4] - starts[0]) / 4 < 0.33


# the AWS client


@pytest.fixture
def aws_env(tmp_path, monkeypatch):
    empty = tmp_path / 'empty'
    empty.write_text('')
    monkeypatch.setenv('AWS_CONFIG_FILE', str(empty))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(empty))
    for name in ('AWS_REGION', 'AWS_DEFAULT_REGION', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'test')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'test')
    pytest.importorskip('boto3')


def test_the_client_takes_its_region_from_aws_region(aws_env, monkeypatch):
    monkeypatch.setenv('AWS_REGION', 'eu-north-1')
    client = capacity.cloudwatch_client()
    assert client.meta.region_name == 'eu-north-1'
    assert (client.meta.config.connect_timeout, client.meta.config.read_timeout) == (5, 10)
    quick = capacity.cloudwatch_client(final=True)
    assert quick.meta.region_name == 'eu-north-1'
    assert (quick.meta.config.connect_timeout, quick.meta.config.read_timeout) == (2, 3)
    assert quick.meta.config.retries['total_max_attempts'] == 1


def test_the_client_falls_back_to_aws_default_region(aws_env, monkeypatch):
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-west-2')
    assert capacity.cloudwatch_client().meta.region_name == 'us-west-2'


def test_a_missing_region_is_a_one_line_error(aws_env, tmp_path, capsys):
    assert cli.main(['server', 'serve', '--metrics', 'cloudwatch', '--data', str(tmp_path / 'data'),
                     '--port', '0']) == 1
    err = capsys.readouterr().err
    assert err.startswith('agenticml server: --metrics cloudwatch needs an AWS region')
    assert len(err.splitlines()) == 1
    assert not (tmp_path / 'data').exists()


def test_other_botocore_errors_are_one_line_errors(aws_env, monkeypatch):
    monkeypatch.setenv('AWS_REGION', 'eu-north-1')
    monkeypatch.setenv('AWS_PROFILE', 'no-such-profile')
    with pytest.raises(ValueError, match='^--metrics cloudwatch: '):
        capacity.cloudwatch_client()


# health checks, latency and in-flight through a running server


def test_health_checks_alone_leave_every_number_at_zero(served):
    served.request_metrics.take()
    for _ in range(3):
        assert call(served, '/healthz')[0] == 200
    assert call(served, '/healthz?probe=1')[0] == 200
    taken = settled_take(served)
    assert taken['requests'] == 0 and taken['in_flight_peak'] == 0
    assert taken['latency'] == {}


def test_a_health_check_during_a_request_does_not_raise_the_peak(served):
    served.request_metrics.take()
    entered, gate = threading.Event(), threading.Event()
    touch = served.keys.touch

    def slow(record):
        entered.set()
        gate.wait(10)
        return touch(record)

    served.keys.touch = slow
    answer = []
    thread = threading.Thread(target=lambda: answer.append(whoami(served, served.sync_key)))
    thread.start()
    try:
        assert entered.wait(5)
        for _ in range(3):
            assert call(served, '/healthz')[0] == 200
    finally:
        gate.set()
        thread.join(10)
        served.keys.touch = touch
    assert answer == [200]
    taken = settled_take(served)
    assert taken['requests'] == 1 and taken['in_flight_peak'] == 1


def put_slowly(httpd, pieces, pause, key):
    body = b''.join(pieces)
    digest = hashlib.sha256(body).hexdigest()
    with socket.create_connection(httpd.server_address[:2], timeout=10) as client:
        client.sendall((f'PUT /v2/objects/{digest} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {key}\r\n'
                        f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode())
        for piece in pieces:
            client.sendall(piece)
            time.sleep(pause)
        reply = b''
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            reply += chunk
    return reply


def test_latency_leaves_out_the_time_the_client_spends_sending(served):
    served.request_metrics.take()
    started = time.monotonic()
    reply = put_slowly(served, [b'a' * 100, b'b' * 100, b'c' * 100], 0.5, served.sync_key)
    wall = time.monotonic() - started
    assert b' 201 ' in reply.split(b'\r\n')[0], reply[:80]
    assert wall > 1.4
    taken = settled_take(served)
    assert taken['requests'] == 1
    (latency,) = taken['latency']
    assert 0 < latency < 300, latency


def test_latency_leaves_out_slow_headers(served):
    served.request_metrics.take()
    with socket.create_connection(served.server_address[:2], timeout=10) as client:
        client.sendall(b'GET /v2/whoami HTTP/1.1\r\n')
        time.sleep(0.7)
        client.sendall(b'Host: x\r\nConnection: close\r\n\r\n')
        while client.recv(4096):
            pass
    taken = settled_take(served)
    assert taken['requests'] == 1
    (latency,) = taken['latency']
    assert 0 < latency < 300, latency


def test_a_small_request_records_a_small_positive_latency(served):
    served.request_metrics.take()
    assert whoami(served, served.sync_key) == 200
    taken = settled_take(served)
    (latency,) = taken['latency']
    assert 0 < latency < 1000


def test_a_header_deadline_timeout_counts_without_an_error_class(served, monkeypatch):
    monkeypatch.setattr(ui_server, '_HEADER_DEADLINE', 0.3)
    served.request_metrics.take()
    with socket.create_connection(served.server_address[:2], timeout=5) as client:
        client.sendall(b'GET /v2/whoami HTTP/1.1\r\n')
        client.settimeout(5)
        try:
            while client.recv(4096):
                pass
        except OSError:
            pass
    taken = settled_take(served)
    assert (taken['requests'], taken['errors_4xx'], taken['errors_5xx']) == (1, 0, 0)
    assert served.request_metrics._in_flight == 0


def test_partial_headers_then_close_leave_nothing_in_flight(served):
    served.request_metrics.take()
    with socket.create_connection(served.server_address[:2], timeout=5) as client:
        client.sendall(b'GET /v2/whoami HTTP/1.1\r\nHost: x\r\n')
    settled_take(served)
    assert served.request_metrics._in_flight == 0


def test_an_idle_keep_alive_connection_is_not_in_flight(served):
    served.request_metrics.take()
    conn = http.client.HTTPConnection(*served.server_address[:2], timeout=5)
    try:
        for _ in range(3):
            conn.request('GET', '/v2/whoami', headers={'Authorization': 'Bearer ' + served.sync_key})
            response = conn.getresponse()
            assert response.status == 200
            response.read()
        taken = settled_take(served)
        assert served.request_metrics._in_flight == 0
        assert taken['requests'] == 3
    finally:
        conn.close()

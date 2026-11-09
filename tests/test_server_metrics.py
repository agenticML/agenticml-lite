"""Package 22: metric summaries, sort/filter API, rebuild, and backfill."""
import builtins
from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path

import pytest

from agenticml import cli
from agenticml.client import Client
from agenticml.server.app import prepare_data_dir
from agenticml.server.db import connect, for_server_dir
from agenticml.server.db.repos import RunRepo, SettingsRepo
from agenticml.server.runs import reconcile
from agenticml.server.workspaces import default_workspace_id
from agenticml.server.metrics import (
    RunsQueryError, empty_summary, fold, parse_runs_query,
)
from agenticml.store import RunReader, RunWriter, layout
from agenticml.store.layout import RECORD_STRUCT
from agenticml.store.overview import OVERVIEW_MIN_POINTS
from test_server_ingest import append, put_meta, request, running
from test_server_web import authed
from test_sync import push, writer_at

_postgres = pytest.mark.skipif(
    not os.environ.get('AGENTICML_TEST_DATABASE_URL'),
    reason='AGENTICML_TEST_DATABASE_URL is not set')


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    with running(tmp_path / "data") as httpd:
        _, httpd.test_key = httpd.keys.create("one")
        _, httpd.admin_key = httpd.keys.create("admin", "admin")
        yield httpd


def _summary_from_reader(reader, key):
    summary = empty_summary()
    offset = 0
    while True:
        records, offset = reader.read_records(key, offset, limit=8192)
        if not records:
            break
        packed = b"".join(RECORD_STRUCT.pack(step, value) for step, value in records)
        summary = fold(summary, packed)
    return summary


def _same_number(left, right):
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        return left == right
    return left == right


def _assert_row_matches(row, expected, name=None):
    assert row['count'] == expected['count']
    assert row['last_step'] == expected['last_step']
    assert row['decoded_bytes'] == expected['decoded_bytes']
    assert _same_number(row['last_value'], expected['last_value'])
    assert _same_number(row['min_value'], expected['min_value'])
    assert _same_number(row['max_value'], expected['max_value'])
    if name is not None:
        assert row['name'] == name


def test_fold_nan_inf_and_restart_semantics():
    summary = fold(empty_summary(), RECORD_STRUCT.pack(1, 2.0) + RECORD_STRUCT.pack(0, float('nan')))
    assert summary['count'] == 2
    assert summary['last_value'] is None
    assert summary['last_step'] == 0
    assert summary['min_value'] == 2.0 and summary['max_value'] == 2.0
    summary = fold(empty_summary(), RECORD_STRUCT.pack(1, float('inf')) + RECORD_STRUCT.pack(2, float('-inf')))
    assert math.isinf(summary['max_value']) and summary['max_value'] > 0
    assert math.isinf(summary['min_value']) and summary['min_value'] < 0
    assert math.isinf(summary['last_value']) and summary['last_value'] < 0


def test_parse_runs_query_names_with_colons_and_errors():
    metrics, sort, order, filters = parse_runs_query({
        'metrics': ['a,b:c'],
        'sort': ['metric:foo:bar:last'],
        'order': ['desc'],
        'filter': ['metric:x:y:min:gt:1.5'],
    })
    assert metrics == ['a', 'b:c']
    assert sort == ('foo:bar', 'last')
    assert order == 'desc'
    assert filters == [('x:y', 'min', 'gt', 1.5)]
    with pytest.raises(RunsQueryError):
        parse_runs_query({'sort': ['created']})
    with pytest.raises(RunsQueryError):
        parse_runs_query({'filter': ['metric:loss:last:gt:nan']})
    with pytest.raises(RunsQueryError):
        parse_runs_query({'filter': ['metric:loss:last:gt:inf']})
    with pytest.raises(RunsQueryError):
        parse_runs_query({'metrics': [','.join('m%d' % i for i in range(9))]})
    with pytest.raises(RunsQueryError):
        parse_runs_query({'filter': ['metric:loss:last:gt:1_0']})
    with pytest.raises(RunsQueryError):
        parse_runs_query({'filter': ['metric:loss:last:gt: 1']})
    with pytest.raises(RunsQueryError):
        parse_runs_query({'filter': ['metric:loss:last:gt:1 ']})
    with pytest.raises(RunsQueryError):
        parse_runs_query({'filter': ['metric:loss:last:gt:١٢']})


def _add_empty_series(writer):
    """Named series with a 0-byte ``.bin`` (0 points) that sync still creates."""
    index_path = writer.run_dir / 'scalars' / 'index.json'
    obj = json.loads(index_path.read_text())
    next_id = max(info['id'] for info in obj['series'].values()) + 1
    obj['series']['empty'] = {'id': next_id, 'monotonic': True}
    index_path.write_text(json.dumps(obj, indent=2) + '\n')
    (writer.run_dir / 'scalars' / ('%d.bin' % next_id)).write_bytes(b'')
    return next_id


def _push_corpus(tmp_path, remote):
    root = tmp_path / 'local'
    writer = RunWriter.create(root, run_id='corpus', meta={'project': 'demo', 'name': 'corpus'})
    writer.write_scalars([
        (0, {'loss': 1.0, 'acc': 0.1}),
        (1, {'loss': 0.5, 'acc': 0.2}),
    ])
    writer.write_scalars([
        (2, {'loss': float('nan'), 'acc': 0.3, 'edge': float('inf')}),
        (3, {'loss': float('-inf'), 'acc': 0.4, 'edge': float('-inf')}),
    ])
    writer.write_scalars([(10, {'restart': 1.0}), (5, {'restart': 0.25})])
    n = OVERVIEW_MIN_POINTS + 1
    for start in range(0, n, 20000):
        writer.write_scalars(
            [(i, {'big': float(i)}) for i in range(start, min(start + 20000, n))])
    writer.finish('finished')
    empty_id = _add_empty_series(writer)
    index = json.loads((writer.run_dir / 'scalars' / 'index.json').read_text())
    assert (writer.run_dir / 'scalars' / ('%d.ovw' % index['series']['big']['id'])).is_file()
    assert not push(remote, writer).failed
    return writer, empty_id


def test_corpus_summaries_match_reader(tmp_path, monkeypatch, server):
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    url = 'http://127.0.0.1:%d' % server.server_address[1]
    with Client(url, server.test_key, timeout=10, max_retries=0) as client:
        remote = (server, client)
        writer, empty_id = _push_corpus(tmp_path, remote)
        try:
            reader = RunReader(writer.root, writer.run_id)
            rows = {row['name']: row for row in server.run_index.metrics(writer.run_id)}
            for key in reader.keys():
                expected = _summary_from_reader(reader, key)
                _assert_row_matches(rows[key], expected, name=key)
            assert set(reader.keys()) == {'loss', 'acc', 'edge', 'restart', 'big', 'empty'}
            assert rows['empty']['series_id'] == empty_id
            assert rows['empty']['count'] == 0
            assert rows['empty']['last_value'] is None and rows['empty']['last_step'] is None
            assert math.isinf(rows['loss']['last_value']) and rows['loss']['last_value'] < 0
            assert math.isinf(rows['loss']['min_value']) and rows['loss']['min_value'] < 0
            assert math.isinf(rows['edge']['max_value']) and rows['edge']['max_value'] > 0
            assert rows['restart']['last_value'] == pytest.approx(0.25)
            assert rows['restart']['last_step'] == 5
            assert rows['big']['count'] == OVERVIEW_MIN_POINTS + 1
        finally:
            writer.close()


def test_incremental_append_and_truncate(tmp_path, monkeypatch, server):
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    url = 'http://127.0.0.1:%d' % server.server_address[1]
    writer = writer_at(tmp_path, status=None, count=4)
    try:
        with Client(url, server.test_key, timeout=5, max_retries=0) as client:
            remote = (server, client)
            assert not push(remote, writer).failed
            reader = RunReader(writer.root, writer.run_id)
            rows = {row['name']: row for row in server.run_index.metrics(writer.run_id)}
            _assert_row_matches(rows['loss'], _summary_from_reader(reader, 'loss'), name='loss')
            writer.write_scalars([(4, {'loss': 0.01, 'acc': 0.9, 'extra': 4.0})])
            assert not push(remote, writer).failed
            reader = RunReader(writer.root, writer.run_id)
            rows = {row['name']: row for row in server.run_index.metrics(writer.run_id)}
            _assert_row_matches(rows['loss'], _summary_from_reader(reader, 'loss'), name='loss')
            packed = RECORD_STRUCT.pack(0, 9.0)
            status, _, _ = request(
                server,
                f'/v2/runs/{writer.run_id}/append/scalars/0.bin?offset=0&truncate=1',
                'POST', packed)
            assert status == 200
            row = [item for item in server.run_index.metrics(writer.run_id) if item['series_id'] == 0][0]
            _assert_row_matches(row, fold(empty_summary(), packed))
    finally:
        writer.close()


def test_bin_before_index_leaves_name_null_then_sets_it(server):
    put_meta(server)
    packed = RECORD_STRUCT.pack(0, 1.5)
    assert append(server, packed, name='scalars/0.bin')[0] == 200
    rows = server.run_index.metrics('run-1')
    assert len(rows) == 1 and rows[0]['name'] is None and rows[0]['count'] == 1
    body = json.dumps({'version': 1, 'series': {'loss': {'id': 0, 'monotonic': True}}}).encode()
    assert request(server, '/v2/runs/run-1/files/scalars/index.json', 'PUT', body)[0] == 200
    assert server.run_index.metrics('run-1')[0]['name'] == 'loss'
    assert append(server, b'', name='scalars/1.bin')[0] == 200
    empty = [row for row in server.run_index.metrics('run-1') if row['series_id'] == 1][0]
    assert empty['count'] == 0 and empty['last_value'] is None and empty['name'] is None


def test_crash_between_write_and_commit_does_not_change_summary(tmp_path, monkeypatch, server):
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    url = 'http://127.0.0.1:%d' % server.server_address[1]
    writer = writer_at(tmp_path, status=None, count=2)
    try:
        with Client(url, server.test_key, timeout=5, max_retries=0) as client:
            remote = (server, client)
            assert not push(remote, writer).failed
            before = {row['name']: dict(row) for row in server.run_index.metrics(writer.run_id)}
            writer.write_scalars([(2, {'loss': 0.1, 'acc': 0.8, 'extra': 2.0})])

            def crash(run_id, path):
                raise RuntimeError('injected crash')

            server._after_append_write = crash
            path = 'scalars/0.bin'
            committed = server.run_index.committed_size(writer.run_id, path)
            chunk = (writer.run_dir / path).read_bytes()[committed:]
            status, _, _ = request(
                server, f'/v2/runs/{writer.run_id}/append/{path}?offset={committed}',
                'POST', chunk)
            assert status == 500
            after = {row['name']: dict(row) for row in server.run_index.metrics(writer.run_id)}
            assert after == before
            server._after_append_write = lambda *a: None
            assert not push(remote, writer).failed
            reader = RunReader(writer.root, writer.run_id)
            rows = {row['name']: row for row in server.run_index.metrics(writer.run_id)}
            _assert_row_matches(rows['loss'], _summary_from_reader(reader, 'loss'), name='loss')
    finally:
        writer.close()


def test_runs_api_metrics_sort_filter_and_no_file_reads(tmp_path, monkeypatch, server):
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    url = 'http://127.0.0.1:%d' % server.server_address[1]
    writers = []
    try:
        with Client(url, server.test_key, timeout=5, max_retries=0) as client:
            remote = (server, client)
            for index, loss in enumerate((0.9, 0.3, 0.5)):
                writer = RunWriter.create(
                    tmp_path / 'local', run_id='api-%d' % index,
                    meta={'project': 'demo', 'name': 'run-%d' % index})
                writer.write_scalars([(0, {'loss': loss, 'acc': 1.0 - loss})])
                writer.finish('finished')
                writers.append(writer)
                assert not push(remote, writer).failed
            missing = RunWriter.create(
                tmp_path / 'local', run_id='api-none',
                meta={'project': 'demo', 'name': 'none'})
            missing.finish('finished')
            writers.append(missing)
            assert not push(remote, missing).failed

        _, _, bare, cookie = authed(server, '/api/w/default/runs')
        assert json.loads(bare)
        status, _, again, _ = authed(server, '/api/w/default/runs', cookie=cookie)
        assert status == 200 and again == bare
        assert all('metrics' not in row for row in json.loads(bare))
        status, _, with_metrics, _ = authed(
            server, '/api/w/default/runs?metrics=loss', cookie=cookie)
        assert status == 200
        payload = json.loads(with_metrics)
        by_id = {row['run_id']: row for row in payload}
        assert by_id['api-1']['metrics']['loss']['last'] == pytest.approx(0.3)
        assert 'loss' not in by_id['api-none'].get('metrics', {})

        def ids(path):
            status, _, body, _ = authed(server, path, cookie=cookie)
            assert status == 200
            return [row['run_id'] for row in json.loads(body)]

        assert ids('/api/w/default/runs?sort=metric:loss:last&order=asc')[-1] == 'api-none'
        desc = ids('/api/w/default/runs?sort=metric:loss:last&order=desc')
        assert desc[-1] == 'api-none'
        assert desc[0] == 'api-0'
        assert ids('/api/w/default/runs?sort=metric:loss:last&order=asc')[0] == 'api-1'

        values = {'api-0': 0.9, 'api-1': 0.3, 'api-2': 0.5}

        def filtered(op, number):
            matched = ids(f'/api/w/default/runs?filter=metric:loss:last:{op}:{number}')
            cmp = {'lt': lambda a, b: a < b, 'le': lambda a, b: a <= b,
                   'gt': lambda a, b: a > b, 'ge': lambda a, b: a >= b,
                   'eq': lambda a, b: a == b}[op]
            wanted = {run_id for run_id, value in values.items() if cmp(value, number)}
            assert set(matched) == wanted, (op, number, matched, wanted)
            return matched

        boundary = 0.5
        for op in ('lt', 'le', 'gt', 'ge', 'eq'):
            filtered(op, boundary)
            filtered(op, boundary - 0.01)
            filtered(op, boundary + 0.01)

        both = ids(
            '/api/w/default/runs?filter=metric:loss:last:gt:0.4&filter=metric:loss:last:lt:0.6')
        assert both == ['api-2']

        for path in (
                '/api/w/default/runs?sort=nope',
                '/api/w/default/runs?filter=metric:loss:last:gt:nan',
                '/api/w/default/runs?filter=metric:loss:last:gt:inf',
                '/api/w/default/runs?metrics=' + ','.join('n%d' % i for i in range(9)),
                '/api/w/default/runs?' + '&'.join(
                    'filter=metric:loss:last:gt:%d' % i for i in range(9)),
        ):
            status, _, body, _ = authed(server, path, cookie=cookie)
            assert status == 422
            assert json.loads(body)['error']

        probe = Path(server.data_dir) / 'metrics-open-probe'
        probe.write_text('probe')
        data_root = Path(server.data_dir).resolve()

        def in_data(path):
            if isinstance(path, int):
                return False
            try:
                resolved = Path(os.fspath(path)).resolve()
            except (OSError, TypeError, ValueError):
                return False
            return resolved == data_root or data_root in resolved.parents

        real_open = builtins.open
        real_os_open = os.open
        real_path_open = Path.open

        def boom_open(file, *args, **kwargs):
            if in_data(file):
                raise AssertionError('file read during runs list')
            return real_open(file, *args, **kwargs)

        def boom_os_open(path, *args, **kwargs):
            if in_data(path):
                raise AssertionError('file read during runs list')
            return real_os_open(path, *args, **kwargs)

        def boom_path_open(self, *args, **kwargs):
            if in_data(self):
                raise AssertionError('file read during runs list')
            return real_path_open(self, *args, **kwargs)

        with monkeypatch.context() as ctx:
            ctx.setattr(builtins, 'open', boom_open)
            ctx.setattr(os, 'open', boom_os_open)
            ctx.setattr(Path, 'open', boom_path_open)
            with pytest.raises(AssertionError, match='file read during runs list'):
                probe.open()
            with pytest.raises(AssertionError, match='file read during runs list'):
                builtins.open(probe)
            with pytest.raises(AssertionError, match='file read during runs list'):
                os.open(probe, os.O_RDONLY)
            for path in (
                    '/api/w/default/runs?metrics=loss',
                    '/api/w/default/runs?sort=metric:loss:last&order=desc',
                    '/api/w/default/runs?filter=metric:loss:last:gt:0.4',
            ):
                status, _, body, _ = authed(server, path, cookie=cookie)
                assert status == 200 and json.loads(body)
    finally:
        for writer in writers:
            writer.close()


def test_metrics_route_lists_workspace_names(server):
    put_meta(server)
    packed = RECORD_STRUCT.pack(0, 1.0)
    append(server, packed, name='scalars/0.bin')
    body = json.dumps({'version': 1, 'series': {'loss': {'id': 0, 'monotonic': True}}}).encode()
    request(server, '/v2/runs/run-1/files/scalars/index.json', 'PUT', body)
    _, _, payload, cookie = authed(server, '/api/w/default/metrics')
    assert json.loads(payload) == {'metrics': ['loss']}
    status, _, missing, _ = authed(server, '/api/w/missing/metrics', cookie=cookie)
    assert status == 404


def test_rebuild_and_reindex_restore_deleted_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    data = tmp_path / 'data'
    with running(data) as httpd:
        _, key = httpd.keys.create('one')
        url = 'http://127.0.0.1:%d' % httpd.server_address[1]
        writer = writer_at(tmp_path, status='finished', count=3)
        try:
            with Client(url, key, timeout=5, max_retries=0) as client:
                assert not push((httpd, client), writer).failed
            run_id = writer.run_id
            assert httpd.run_index.metrics(run_id)
            with httpd.db.transaction(write=True) as tx:
                tx.bind_workspace(httpd.default_workspace_id)
                tx.execute('DELETE FROM run_metrics WHERE run_id=?', (run_id,))
            assert httpd.run_index.metrics(run_id) == []
        finally:
            writer.close()
    assert cli.main([
        'server', 'metrics', 'rebuild', '--data', str(data),
        '--workspace', 'default', '--run', run_id,
    ]) == 0
    assert 'rebuilt 1' in capsys.readouterr().out
    db = for_server_dir(data / 'server')
    restored = RunRepo(db, default_workspace_id(db)).metrics(run_id)
    assert restored and restored[0]['count'] > 0


def test_reindex_restores_metrics_offline(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    data = tmp_path / 'data'
    with running(data) as httpd:
        _, key = httpd.keys.create('one')
        url = 'http://127.0.0.1:%d' % httpd.server_address[1]
        writer = writer_at(tmp_path, status='finished', count=3)
        try:
            with Client(url, key, timeout=5, max_retries=0) as client:
                assert not push((httpd, client), writer).failed
            run_id = writer.run_id
            with httpd.db.transaction(write=True) as tx:
                tx.bind_workspace(httpd.default_workspace_id)
                tx.execute('DELETE FROM run_metrics WHERE run_id=?', (run_id,))
        finally:
            writer.close()
    assert cli.main(['server', 'reindex', '--data', str(data)]) == 0
    db = for_server_dir(data / 'server')
    repo = RunRepo(db, default_workspace_id(db))
    assert repo.metrics(writer.run_id)


def test_first_start_backfill_rebuilds_once(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    data = tmp_path / 'data'
    with running(data) as httpd:
        _, key = httpd.keys.create('one')
        url = 'http://127.0.0.1:%d' % httpd.server_address[1]
        writer = writer_at(tmp_path, status='finished', count=2)
        try:
            with Client(url, key, timeout=5, max_retries=0) as client:
                assert not push((httpd, client), writer).failed
            run_id = writer.run_id
            SettingsRepo(httpd.db).delete('metrics_backfilled')
            with httpd.db.transaction(write=True) as tx:
                tx.bind_workspace(httpd.default_workspace_id)
                tx.execute('DELETE FROM run_metrics WHERE run_id=?', (run_id,))
        finally:
            writer.close()
    with running(data) as httpd:
        repo = RunRepo(httpd.db, httpd.default_workspace_id)
        assert repo.metrics(run_id)
        assert SettingsRepo(httpd.db).get('metrics_backfilled') is not None
        calls = []
        original = RunRepo.rebuild_metrics

        def wrapped(self, run_id, root, tx=None):
            calls.append(run_id)
            return original(self, run_id, root, tx)

        monkeypatch.setattr(RunRepo, 'rebuild_metrics', wrapped)
    with running(data):
        assert calls == []


def test_import_and_reconcile_adopt_build_metric_summaries(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    data = tmp_path / 'data'
    prepare_data_dir(data)
    writer = RunWriter.create(
        data / 'store', run_id='imported', meta={'project': 'demo', 'name': 'imported'})
    try:
        writer.write_scalars([(0, {'loss': 1.25}), (1, {'loss': 0.5})])
        writer.finish('finished')
        db = for_server_dir(data / 'server')
        from server_db_helpers import default_store
        root = default_store(data)
        repo = RunRepo(db, default_workspace_id(db))
        reader = RunReader(root, 'imported')
        rows = {row['name']: row for row in repo.metrics('imported')}
        _assert_row_matches(rows['loss'], _summary_from_reader(reader, 'loss'), name='loss')
    finally:
        writer.close()

    with running(data) as httpd:
        adopted = RunWriter.create(
            httpd.root, run_id='reconciled',
            meta={'project': 'demo', 'name': 'reconciled'})
        try:
            adopted.write_scalars([(0, {'acc': 0.9}), (1, {'acc': 0.95})])
            adopted.finish('finished')
            assert reconcile(httpd.root, httpd.run_index)['adopted'] == 1
            reader = RunReader(httpd.root, 'reconciled')
            rows = {row['name']: row for row in httpd.run_index.metrics('reconciled')}
            _assert_row_matches(rows['acc'], _summary_from_reader(reader, 'acc'), name='acc')
        finally:
            adopted.close()


def test_decoded_bytes_mismatch_fully_recomputes(server):
    put_meta(server)
    packed = RECORD_STRUCT.pack(0, 1.0) + RECORD_STRUCT.pack(1, 2.0)
    assert append(server, packed, name='scalars/0.bin')[0] == 200
    with server.db.transaction(write=True) as tx:
        tx.bind_workspace(server.default_workspace_id)
        tx.execute(
            'UPDATE run_metrics SET decoded_bytes=0, count=99, last_value=0.0 WHERE run_id=?',
            ('run-1',))
    extra = RECORD_STRUCT.pack(2, 3.0)
    committed = server.run_index.committed_size('run-1', 'scalars/0.bin')
    assert append(server, extra, name='scalars/0.bin', offset=committed)[0] == 200
    row = server.run_index.metrics('run-1')[0]
    _assert_row_matches(row, fold(empty_summary(), packed + extra))
    assert row['count'] == 3


def test_index_drop_unparsable_and_truncate_keeps_name(server):
    put_meta(server)
    packed = RECORD_STRUCT.pack(0, 1.0)
    assert append(server, packed, name='scalars/0.bin')[0] == 200
    body = json.dumps({'version': 1, 'series': {'loss': {'id': 0, 'monotonic': True}}}).encode()
    assert request(server, '/v2/runs/run-1/files/scalars/index.json', 'PUT', body)[0] == 200
    assert server.run_index.metrics('run-1')[0]['name'] == 'loss'

    dropped = json.dumps({'version': 1, 'series': {'acc': {'id': 1, 'monotonic': True}}}).encode()
    assert request(server, '/v2/runs/run-1/files/scalars/index.json', 'PUT', dropped)[0] == 200
    assert server.run_index.metrics('run-1')[0]['name'] is None

    assert request(server, '/v2/runs/run-1/files/scalars/index.json', 'PUT', body)[0] == 200
    assert server.run_index.metrics('run-1')[0]['name'] == 'loss'

    bad = json.dumps({'version': 1, 'series': 'nope'}).encode()
    assert request(server, '/v2/runs/run-1/files/scalars/index.json', 'PUT', bad)[0] == 200
    assert server.run_index.metrics('run-1')[0]['name'] == 'loss'

    replaced = RECORD_STRUCT.pack(0, 9.0)
    status, _, _ = request(
        server, '/v2/runs/run-1/append/scalars/0.bin?offset=0&truncate=1', 'POST', replaced)
    assert status == 200
    row = server.run_index.metrics('run-1')[0]
    _assert_row_matches(row, fold(empty_summary(), replaced), name='loss')


def test_sort_tie_break_created_at_then_run_id(tmp_path, monkeypatch, server):
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    url = 'http://127.0.0.1:%d' % server.server_address[1]
    writers = []
    try:
        with Client(url, server.test_key, timeout=5, max_retries=0) as client:
            remote = (server, client)
            for run_id in ('tie-a', 'tie-b', 'tie-c'):
                writer = RunWriter.create(
                    tmp_path / 'local', run_id=run_id,
                    meta={'project': 'demo', 'name': run_id})
                writer.write_scalars([(0, {'loss': 0.5})])
                writer.finish('finished')
                writers.append(writer)
                assert not push(remote, writer).failed
            missing = RunWriter.create(
                tmp_path / 'local', run_id='tie-none',
                meta={'project': 'demo', 'name': 'none'})
            missing.finish('finished')
            writers.append(missing)
            assert not push(remote, missing).failed
        with server.db.transaction(write=True) as tx:
            tx.bind_workspace(server.default_workspace_id)
            tx.execute("UPDATE runs SET created_at=? WHERE run_id=?",
                       ('2026-01-02T00:00:00Z', 'tie-b'))
            tx.execute("UPDATE runs SET created_at=? WHERE run_id=?",
                       ('2026-01-01T00:00:00Z', 'tie-a'))
            tx.execute("UPDATE runs SET created_at=? WHERE run_id=?",
                       ('2026-01-01T00:00:00Z', 'tie-c'))
        _, _, _, cookie = authed(server, '/api/w/default/runs')

        def ids(order):
            status, _, body, _ = authed(
                server,
                '/api/w/default/runs?sort=metric:loss:last&order=' + order,
                cookie=cookie)
            assert status == 200
            return [row['run_id'] for row in json.loads(body)]

        assert ids('asc') == ['tie-b', 'tie-c', 'tie-a', 'tie-none']
        assert ids('desc') == ['tie-b', 'tie-c', 'tie-a', 'tie-none']
    finally:
        for writer in writers:
            writer.close()


def test_rebuild_metrics_retries_then_keeps_rows_when_sizes_change(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    data = tmp_path / 'data'
    prepare_data_dir(data)
    writer = RunWriter.create(
        data / 'store', run_id='shift', meta={'project': 'demo', 'name': 'shift'})
    try:
        writer.write_scalars([(0, {'loss': 1.0}), (1, {'loss': 0.5})])
        writer.finish('finished')
        db = for_server_dir(data / 'server')
        from server_db_helpers import default_store
        repo = RunRepo(db, default_workspace_id(db))
        before = [dict(row) for row in repo.metrics('shift')]
        assert before and before[0]['count'] == 2
        original_tx = RunRepo._tx

        @contextmanager
        def bump_between_read_and_write(self, write=False):
            if write:
                with original_tx(self, write=True) as extra:
                    extra.execute(
                        'UPDATE run_files SET size = size + 8 '
                        'WHERE workspace_id=? AND run_id=? AND path=?',
                        (self.workspace_id, 'shift', 'scalars/0.bin'))
            with original_tx(self, write=write) as tx:
                yield tx

        monkeypatch.setattr(RunRepo, '_tx', bump_between_read_and_write)
        with caplog.at_level(logging.WARNING, logger='agenticml.server.db.repos'):
            repo.rebuild_metrics('shift', default_store(data))
        assert 'leaving metric summaries for run shift unchanged' in caplog.text
        after = [dict(row) for row in repo.metrics('shift')]
        assert after == before
    finally:
        writer.close()


def test_metrics_rebuild_skips_locked_run(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    data = tmp_path / 'data'
    prepare_data_dir(data)
    writers = []
    try:
        for run_id in ('locked-run', 'free-run'):
            writer = RunWriter.create(
                data / 'store', run_id=run_id,
                meta={'project': 'demo', 'name': run_id})
            writer.write_scalars([(0, {'loss': 1.0})])
            writer.finish('finished')
            writers.append(writer)
        db = for_server_dir(data / 'server')
        wid = default_workspace_id(db)
        repo = RunRepo(db, wid)
        with db.transaction(write=True) as tx:
            tx.bind_workspace(wid)
            tx.execute('DELETE FROM run_metrics')
        assert repo.metrics('locked-run') == []
        assert repo.metrics('free-run') == []

        from agenticml.server.db.backfill import lock_run as real_lock
        from agenticml.server.db.errors import DatabaseBusy

        def busy_for_locked(db, workspace_id, run_id, server=None):
            if run_id == 'locked-run':
                raise DatabaseBusy('database is locked')
            return real_lock(db, workspace_id, run_id, server)

        monkeypatch.setattr('agenticml.server.db.backfill.lock_run', busy_for_locked)
        assert cli.main(['server', 'metrics', 'rebuild', '--data', str(data)]) == 1
        captured = capsys.readouterr()
        assert 'rebuilt 1' in captured.out
        assert 'skipped locked run locked-run' in captured.err
        assert repo.metrics('free-run') and repo.metrics('free-run')[0]['count'] == 1
        assert repo.metrics('locked-run') == []
    finally:
        for writer in writers:
            writer.close()


def test_metrics_rebuild_unknown_run(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    data = tmp_path / 'data'
    prepare_data_dir(data)
    for_server_dir(data / 'server')
    assert cli.main([
        'server', 'metrics', 'rebuild', '--data', str(data), '--run', 'missing-run',
    ]) == 2
    assert 'unknown run' in capsys.readouterr().err


@pytest.mark.sqlite_only
def test_metrics_rebuild_cli_refuses_while_server_holds_lock(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    data = tmp_path / 'data'
    with running(data):
        assert cli.main(['server', 'metrics', 'rebuild', '--data', str(data)]) == 2
    assert 'stop the server first' in capsys.readouterr().err


def test_metrics_backfill_skips_unreadable_run(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    data = tmp_path / 'data'
    with running(data) as httpd:
        _, key = httpd.keys.create('one')
        url = 'http://127.0.0.1:%d' % httpd.server_address[1]
        writer = writer_at(tmp_path, status='finished', count=2)
        try:
            with Client(url, key, timeout=5, max_retries=0) as client:
                assert not push((httpd, client), writer).failed
            run_id = writer.run_id
            SettingsRepo(httpd.db).delete('metrics_backfilled')
            with httpd.db.transaction(write=True) as tx:
                tx.bind_workspace(httpd.default_workspace_id)
                tx.execute('DELETE FROM run_metrics WHERE run_id=?', (run_id,))
        finally:
            writer.close()

    def boom(self, run_id, root, tx=None):
        raise OSError('injected unreadable')

    monkeypatch.setattr(RunRepo, 'rebuild_metrics', boom)
    with caplog.at_level(logging.WARNING, logger='agenticml.server.db.backfill'):
        with running(data) as httpd:
            assert SettingsRepo(httpd.db).get('metrics_backfilled') is not None
    assert run_id in caplog.text
    assert 'injected unreadable' in caplog.text


@_postgres
def test_unbound_run_metrics_query_is_empty(server_database):
    db = connect(server_database)
    try:
        from agenticml.server.workspaces import default_workspace_id
        from agenticml.server.db.repos import RunRepo
        wid = default_workspace_id(db)
        RunRepo(db, wid).insert({
            'run_id': 'metric-rls', 'project': None, 'created_at': None, 'owner_user': None,
            'owner_key': None, 'status': None, 'fingerprint': None, 'last_sync_at': None,
            'last_sync_key': None, 'list_row': '{"run_id":"metric-rls"}', 'last_step': None,
            'launch_note': None, 'commit_sizes': None, 'updated_at': layout.format_timestamp(),
        })
        with db.transaction(write=True) as tx:
            tx.bind_workspace(wid)
            tx.execute(
                'INSERT INTO run_metrics (workspace_id, run_id, series_id, name, last_value, '
                'min_value, max_value, count, last_step, decoded_bytes) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (wid, 'metric-rls', 0, 'loss', 1.0, 1.0, 1.0, 1, 0, 8))
        with db.transaction() as tx:
            tx.bind_workspace(wid)
            assert tx.fetchone('SELECT COUNT(*) FROM run_metrics')[0] >= 1
        with db.transaction() as tx:
            assert tx.fetchone('SELECT COUNT(*) FROM run_metrics')[0] == 0
    finally:
        db.close()

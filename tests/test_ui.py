"""HTTP integration tests using the real reader and directly written v1 files."""
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import threading

import pytest

import agenticml
from agenticml import Run
from agenticml.code_tracking import default_project

pytest.importorskip("agenticml.store")
from agenticml.store import reader
from agenticml.ui import server


def write_store(root):
    """No writer dependency: exercise the documented storage/reader boundary."""
    run = root / 'runs' / 'run-1'
    scalars = run / 'scalars'
    scalars.mkdir(parents=True)
    meta = dict(format_version=1, run_id='run-1', project='demo', name='<script>run</script>',
                config={'lr': .01}, tags=['test'], notes='', group='group', job_type='train',
                parent_run_id=None, env={'python':'3.11','platform':'linux','packages':{'x':'1'}},
                git={'commit':'abc','branch':'main','dirty':True}, status='running',
                summary={'loss': .5,'label':'ok','nested':{'x':1},'array':[1]},
                created_at='2026-10-27T00:00:00Z', finished_at=None, pid=123, hostname='host')
    (run / 'meta.json').write_text(json.dumps(meta))
    (scalars / 'index.json').write_text(json.dumps({'version':1,'series':{'loss':{'id':0,'monotonic':True}}}))
    (scalars / '0.bin').write_bytes(b''.join(struct.pack('<If',i,i/10) for i in range(120)))
    (scalars / '_time.bin').write_bytes(b''.join(struct.pack('<If',i,i*2) for i in range(120)))
    blob = b'<html><script>alert("snapshot")</script></html>'
    sha = hashlib.sha256(blob).hexdigest()
    obj = root / 'objects' / sha[:2] / sha
    obj.parent.mkdir(parents=True)
    obj.write_bytes(blob)
    manifest = [dict(path='src/example.html',sha256=sha,size=len(blob),content_type='text/html',source='code')]
    artifact = dict(kind='artifact',name='weights',version=1,type='model',size=123,path='/tmp/weights')
    (run / 'events.jsonl').write_text(json.dumps({'kind':'code','manifest':manifest})+'\n'+json.dumps(artifact)+'\n')
    (run / 'media').mkdir()
    (run / 'media' / 'index.json').write_text(json.dumps({'images':[dict(step=1,sha256=sha,content_type='image/png',caption='<img>')]}))
    return sha, blob


@pytest.fixture
def dashboard(tmp_path):
    sha, blob = write_store(tmp_path)
    httpd = server.make_server(tmp_path,'127.0.0.1',0)
    thread = threading.Thread(target=httpd.serve_forever,kwargs={"poll_interval":.01},daemon=True)
    thread.start()
    def request(path, method='GET', headers=None):
        conn = http.client.HTTPConnection(*httpd.server_address[:2],timeout=5)
        conn.request(method,path,headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result
    yield request, httpd, sha, blob
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()


def load(dashboard,path):
    status, headers, body = dashboard[0](path)
    assert status == 200
    assert headers['Content-Type'] == 'application/json'
    return json.loads(body)


def test_routes(dashboard):
    runs = load(dashboard,'/api/runs')
    assert len(runs) == 1
    assert runs[0]['status'] == 'crashed'
    assert runs[0]['summary'] == {'loss':.5,'label':'ok'}
    assert set(runs[0]) == {
        'run_id','project','name','status','created_at','finished_at','tags','group',
        'summary','config','last_step',
    }
    assert runs[0]['config'] == {'lr': .01}
    assert runs[0]['last_step'] == 119
    run = load(dashboard,'/api/runs/run-1')
    assert run['keys'] == ['loss']
    assert run['code_manifest_length'] == 1
    assert run['artifacts'][0]['name'] == 'weights'
    assert run['config']['lr'] == .01
    data = load(dashboard,'/api/runs/run-1/scalars?key=loss&key=missing&max_points=1000&start=&end=')
    assert data['truncated'] is False
    assert list(data['series']) == ['loss']
    assert data['series']['loss']['mode'] == 'raw'
    assert data['series']['loss']['total'] == 120
    assert data['series']['loss']['points'][0] == [0,0]
    ranged = load(dashboard,'/api/runs/run-1/scalars?key=loss&start=10&end=12')
    assert [p[0] for p in ranged['series']['loss']['points']] == [10,11,12]
    assert load(dashboard,'/api/runs/run-1/time')[:2] == [[0,0],[1,2]]
    assert load(dashboard,'/api/runs/run-1/code')[0]['path'] == 'src/example.html'
    assert 'images' in load(dashboard,'/api/runs/run-1/media')
    assert load(dashboard,'/api/runs/run-1/media?key=images')[0]['step'] == 1
    assert load(dashboard,'/api/runs/run-1/media?key=unknown') == []


@pytest.mark.parametrize(('limit','expected'),[('-100',10),('1',10),('42',42),('999999',5000)])
def test_max_points_clamped(dashboard,monkeypatch,limit,expected):
    calls = []
    original = reader.RunReader.series
    def spy(self,key,**kwargs):
        calls.append(kwargs['max_points'])
        return original(self,key,**kwargs)
    monkeypatch.setattr(reader.RunReader,'series',spy)
    load(dashboard,f'/api/runs/run-1/scalars?key=loss&max_points={limit}')
    assert calls == [expected]


@pytest.mark.parametrize('path',[
    '/api/runs/..%2Fx','/api/runs/%2e%2e','/api/runs/%00','/api/runs/valid/extra/path',
    '/api/runs/%252e%252e%252f','/api/runs/%5c','/static/%252e%252e%252fserver.py','/static/%5c',
    '/static/../server.py','/static/%2e%2e%2fserver.py','/static/%2Fetc%2Fpasswd',
    '/api/objects/'+'z'*64,'/api/objects/'+'a'*63,'/api/objects/'+'A'*64,
    '/api/objects/..%2Fx','/api/runs/missing','/api/runs/run-1/unknown','/unknown',
    '/api/runs/run-1/scalars?max_points=bad','/api/runs/run-1/scalars?start=1.2',
])
def test_invalid_requests(dashboard,path):
    status, headers, body = dashboard[0](path)
    assert status in (400,404)
    assert headers['Content-Type'] == 'application/json'
    assert isinstance(json.loads(body)['error'],str)


@pytest.mark.parametrize(('hint','expected'),[
    ('','application/octet-stream'),('?type=text/plain','text/plain'),
    ('?type=text/html','text/plain'),('?type=text/xml','text/plain'),
    ('?type=application/json','application/json'),('?type=image/png','image/png'),
    ('?type=image/svg%2Bxml','application/octet-stream'),
    ('?type=application/javascript','application/octet-stream'),
    ('?type=image/png%0D%0AX-Evil%3Ayes','application/octet-stream'),
])
def test_objects(dashboard,hint,expected):
    request, _, sha, blob = dashboard
    status, headers, body = request('/api/objects/'+sha+hint)
    assert status == 200
    assert body == blob
    assert headers['Content-Type'] == expected
    assert headers['Cache-Control'] == 'immutable, max-age=31536000'
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert 'X-Evil' not in headers


def test_missing_object_symlink_and_hardlink(dashboard,tmp_path):
    request, _, sha, _ = dashboard
    assert request('/api/objects/'+'0'*64)[0] == 404
    obj = tmp_path / 'objects' / sha[:2] / sha
    extra = obj.with_name('extra-link')
    os.link(obj, extra)
    status, _, body = request('/api/objects/'+sha)
    assert status == 404
    assert json.loads(body) == {'error':'not found'}
    assert str(obj).encode() not in body
    obj.unlink()
    obj.symlink_to(Path(__file__))
    status, _, body = request('/api/objects/'+sha)
    assert status == 404
    assert json.loads(body) == {'error':'not found'}
    assert str(obj).encode() not in body


@pytest.mark.parametrize('method',['POST','PUT','PATCH','DELETE','OPTIONS','HEAD','TRACE','CUSTOM'])
def test_get_only(dashboard,method):
    status, headers, body = dashboard[0]('/api/runs',method)
    assert status == 405
    assert headers['Allow'] == 'GET'
    if method != 'HEAD':
        assert json.loads(body) == {'error':'method not allowed'}


@pytest.mark.parametrize('name',['index.html','app.js','app.css','charts.js','code.js'])
def test_static(dashboard,name):
    path = '/' if name == 'index.html' else '/static/'+name
    status, headers, body = dashboard[0](path)
    assert status == 200 and body
    assert headers['Content-Type'] == server._FILES[name]
    assert headers['ETag']
    assert dashboard[0](path,headers={'If-None-Match':headers['ETag']})[0] == 304


def test_static_allowlist():
    assert list(server._FILES) == ['index.html','app.js','app.css','charts.js','code.js']
    assert server._FILES['charts.js'] == 'text/javascript; charset=utf-8'
    assert server._FILES['code.js'] == 'text/javascript; charset=utf-8'


@pytest.mark.parametrize('name',['uPlot.iife.min.js','uPlot.min.css','uPlot.LICENSE'])
def test_uplot_static_removed(dashboard,name):
    status, _, body = dashboard[0]('/static/'+name)
    assert status == 404
    assert json.loads(body) == {'error':'not found'}
    assert name not in server._FILES


def test_gzip_and_nonfinite(dashboard):
    request, httpd, _, _ = dashboard
    path='/api/runs/run-1/scalars?key=loss'
    status, headers, body=request(path,headers={'Accept-Encoding':'gzip'})
    assert status == 200 and headers['Content-Encoding'] == 'gzip'
    assert json.loads(gzip.decompress(body))['series']['loss']['total'] == 120
    assert request(path,headers={'Accept-Encoding':'gzip;q=0'})[1].get('Content-Encoding') is None
    with (httpd.root/'runs/run-1/scalars/0.bin').open('ab') as file:
        file.write(struct.pack('<If',120,float('nan')))
    value=load(dashboard,path)['series']['loss']
    assert value['nonfinite'] == 1 and value['points'][-1] == [120,None]
    # The small JSON error is not compressed.
    assert request('/missing',headers={'Accept-Encoding':'gzip'})[1].get('Content-Encoding') is None


def test_reader_reuse_and_incremental_updates(dashboard,monkeypatch):
    request, httpd, _, _ = dashboard
    original=reader.RunReader
    calls=[]
    def factory(root,run_id):
        calls.append(run_id)
        return original(root,run_id)
    monkeypatch.setattr(reader,'RunReader',factory)
    load(dashboard,'/api/runs/run-1/scalars?max_points=10')
    with (httpd.root/'runs/run-1/scalars/0.bin').open('ab') as file:
        file.write(struct.pack('<If',120,.25))
    assert load(dashboard,'/api/runs/run-1/scalars?max_points=10')['series']['loss']['total'] == 121
    load(dashboard,'/api/runs/run-1')
    assert calls == ['run-1']


def test_lru_bound(tmp_path):
    class Stub:
        pass
    httpd=server.make_server(tmp_path,'127.0.0.1',0)
    try:
        for i in range(65):
            httpd.readers[str(i)]=Stub()
        with httpd.run_reader('64'):
            pass
        assert len(httpd.readers)==64 and '0' not in httpd.readers
    finally:
        httpd.server_close()


def test_symlink_run_and_effective_status(dashboard, tmp_path):
    import os
    request, httpd, _, _ = dashboard
    alias=tmp_path/'runs'/'outside'
    alias.symlink_to(tmp_path, target_is_directory=True)
    assert request('/api/runs/outside')[0] == 404
    run=tmp_path/'runs'/'run-1'
    os.utime(run/'meta.json',(0,0))
    os.utime(run/'scalars'/'_time.bin',(0,0))
    assert load(dashboard,'/api/runs')[0]['status'] == 'crashed'


def test_accept_encoding_negotiation(dashboard):
    path='/api/runs/run-1/scalars'
    for accept in ['*', 'gzip,gzip']:
        _, headers, body=dashboard[0](path,headers={'Accept-Encoding':accept})
        assert headers['Content-Encoding'] == 'gzip'
        assert json.loads(gzip.decompress(body))['series']['loss']['total'] == 120
    assert dashboard[0](path,headers={'Accept-Encoding':'gzip;q=0, *;q=1'})[1].get('Content-Encoding') is None


def test_forbidden_host(dashboard):
    request, httpd, sha, _ = dashboard
    port = httpd.server_address[1]
    status, headers, body = request('/api/runs', headers={'Host': 'evil.example'})
    assert status == 403
    assert headers['Content-Type'] == 'application/json'
    assert json.loads(body) == {'error': 'forbidden host'}
    assert request('/api/objects/'+sha, headers={'Host': 'evil.example'})[0] == 403
    assert request('/api/runs', 'POST', headers={'Host': 'evil.example'})[0] == 403
    assert request('/api/runs', headers={'Host': '8.8.8.8'})[0] == 403
    status, _, body = request('/api/runs', headers={'Host': f'localhost:{port}'})
    assert status == 200
    assert len(json.loads(body)) == 1
    assert request('/api/runs', headers={'Host': f'[::1]:{port}'})[0] == 200


def test_host_allowlist_off_loopback():
    assert server._host_permitted('evil.example', '127.0.0.1', '127.0.0.1') is False
    assert server._host_permitted('localhost:9', '127.0.0.1', '127.0.0.1') is True
    assert server._host_permitted('[::1]', '127.0.0.1', '127.0.0.1') is True
    assert server._host_permitted('192.168.1.9', '0.0.0.0', '0.0.0.0') is True
    assert server._host_permitted('evil.example', '0.0.0.0', '0.0.0.0') is False
    assert server._host_permitted('10.1.2.3:6006', '192.168.0.4', '192.168.0.4') is True
    assert server._host_permitted('192.168.0.4', '192.168.0.4', '192.168.0.4') is True
    name = socket.gethostname()
    if name:
        assert server._host_permitted(name, '0.0.0.0', '0.0.0.0') is True


def test_comma_in_metric_key(dashboard):
    _, httpd, _, _ = dashboard
    index_path = httpd.root / 'runs' / 'run-1' / 'scalars' / 'index.json'
    index = json.loads(index_path.read_text())
    index['series']['loss,val'] = {'id': 1, 'monotonic': True}
    index_path.write_text(json.dumps(index))
    (httpd.root / 'runs' / 'run-1' / 'scalars' / '1.bin').write_bytes(struct.pack('<If', 3, 1.5))
    one = load(dashboard, '/api/runs/run-1/scalars?key=loss,val')
    assert list(one['series']) == ['loss,val']
    assert one['series']['loss,val']['points'] == [[3, 1.5]]
    assert one['truncated'] is False
    both = load(dashboard, '/api/runs/run-1/scalars?key=loss&key=loss,val')
    assert set(both['series']) == {'loss', 'loss,val'}
    assert both['truncated'] is False
    ignored = load(dashboard, '/api/runs/run-1/scalars?keys=loss,val')
    assert set(ignored['series']) == {'loss', 'loss,val'}
    assert ignored['truncated'] is False
    assert 'truncated' not in ignored['series']


def test_run_addressed_by_directory_name(dashboard):
    request, httpd, _, _ = dashboard
    meta_path = httpd.root / 'runs' / 'run-1' / 'meta.json'
    meta = json.loads(meta_path.read_text())
    meta['run_id'] = 'other'
    meta_path.write_text(json.dumps(meta))
    run = load(dashboard, '/api/runs/run-1')
    assert 'loss' in run['keys']
    assert load(dashboard, '/api/runs/run-1/scalars?key=loss')['series']['loss']['total'] == 120
    assert request('/api/runs/other')[0] == 404


def test_internal_error_hides_paths(dashboard, monkeypatch, capfd):
    def boom(self):
        raise RuntimeError('/secret/path')
    monkeypatch.setattr(reader.RunReader, 'meta', boom)
    status, _, body = dashboard[0]('/api/runs/run-1')
    assert status == 500
    assert json.loads(body) == {'error': 'internal error'}
    assert b'/secret' not in body
    captured = capfd.readouterr()
    assert '/secret' not in captured.err
    assert '/secret' not in captured.out


def test_vanished_series_key_omitted(dashboard, monkeypatch):
    original = reader.RunReader.series
    def series(self, key, **kwargs):
        if key == 'gone':
            raise KeyError(key)
        return original(self, key, **kwargs)
    monkeypatch.setattr(reader.RunReader, 'keys', lambda self: ['loss', 'gone'])
    monkeypatch.setattr(reader.RunReader, 'series', series)
    payload = load(dashboard, '/api/runs/run-1/scalars')
    assert payload['truncated'] is False
    assert list(payload['series']) == ['loss']


def test_unfiltered_series_truncated(dashboard, monkeypatch):
    names = [f'm{i:03d}' for i in range(201)]
    monkeypatch.setattr(reader.RunReader, 'keys', lambda self: names)
    monkeypatch.setattr(reader.RunReader, 'series', lambda self, key, **kwargs: {
        'key': key, 'total': 0, 'nonfinite': 0, 'mode': 'raw', 'points': []})
    data = load(dashboard, '/api/runs/run-1/scalars')
    assert data['truncated'] is True
    assert list(data['series']) == names[:200]
    assert 'truncated' not in data['series']
    one = load(dashboard, '/api/runs/run-1/scalars?key=m200')
    assert one['truncated'] is False
    assert list(one['series']) == ['m200']


def test_reader_lock_not_held_during_series(dashboard, monkeypatch):
    _, httpd, _, _ = dashboard
    original = reader.RunReader.series
    held = []
    def series(self, key, **kwargs):
        held.append(httpd.reader_lock.locked())
        return original(self, key, **kwargs)
    monkeypatch.setattr(reader.RunReader, 'series', series)
    assert load(dashboard, '/api/runs/run-1/scalars?key=loss')['series']['loss']['total'] == 120
    assert held == [False]


def test_concurrent_scalar_totals(dashboard):
    request = dashboard[0]
    barrier = threading.Barrier(8)
    totals = []
    errors = []
    def worker():
        try:
            barrier.wait(timeout=5)
            status, _, body = request('/api/runs/run-1/scalars?key=loss')
            if status != 200:
                errors.append(body)
                return
            totals.append(json.loads(body)['series']['loss']['total'])
        except Exception as exc:
            errors.append(repr(exc))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert totals == [120] * 8


def test_symlink_loop_hides_path(dashboard, tmp_path):
    left = tmp_path / 'runs' / 'loop-a'
    right = tmp_path / 'runs' / 'loop-b'
    left.symlink_to('loop-b')
    right.symlink_to('loop-a')
    status, _, body = dashboard[0]('/api/runs/loop-a')
    assert status == 500
    assert json.loads(body) == {'error': 'internal error'}
    assert b'loop-a' not in body
    assert str(tmp_path).encode() not in body


def test_malformed_summary_tags_and_media_index(dashboard):
    request, httpd, _, _ = dashboard
    run = httpd.root / 'runs' / 'run-1'
    meta_path = run / 'meta.json'
    meta = json.loads(meta_path.read_text())
    meta['summary'] = ['loss', 0.5]
    meta['tags'] = 'beta'
    meta['name'] = 3
    meta['project'] = {'nested': True}
    meta_path.write_text(json.dumps(meta))
    (run / 'media' / 'index.json').write_text(json.dumps({
        'images': {'step': 1},
        'plots': [{'step': 2, 'sha256': 'ab', 'content_type': 'image/png'}],
    }))
    runs = load(dashboard, '/api/runs')
    assert runs[0]['summary'] == {}
    assert runs[0]['tags'] == []
    assert runs[0]['name'] == ''
    assert runs[0]['project'] == ''
    media = load(dashboard, '/api/runs/run-1/media')
    assert 'images' not in media
    assert media['plots'][0]['step'] == 2
    assert request('/api/runs/run-1')[0] == 200


def test_fifo_object_is_not_opened(dashboard, tmp_path):
    sha = 'ab' * 32
    path = tmp_path / 'objects' / sha[:2] / sha
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    status, _, body = dashboard[0]('/api/objects/' + sha)
    assert status == 404
    assert json.loads(body) == {'error': 'not found'}


def test_scalar_keys_are_deduped_and_capped(dashboard):
    repeated = '&'.join(['key=loss'] * 201)
    data = load(dashboard, '/api/runs/run-1/scalars?' + repeated)
    assert list(data['series']) == ['loss']
    assert data['truncated'] is False
    many = '&'.join(f'key=k{i}' for i in range(201))
    status, _, body = dashboard[0]('/api/runs/run-1/scalars?' + many)
    assert status == 400
    assert json.loads(body)['error']


def test_ipv6_bind(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server.DashboardServer, 'serve_forever', lambda self: None)
    try:
        httpd = server.make_server(tmp_path, '::1', 0)
    except OSError:
        pytest.skip('IPv6 unavailable')
    try:
        assert httpd.address_family == socket.AF_INET6
        assert ':' in httpd.server_address[0]
    finally:
        httpd.server_close()
    try:
        server.serve(tmp_path, '::1', 0)
    except OSError:
        pytest.skip('IPv6 unavailable')
    assert 'http://[::1]:' in capsys.readouterr().out


def test_accepted_connection_has_timeout(tmp_path):
    httpd = server.make_server(tmp_path, '127.0.0.1', 0)
    seen = threading.Event()
    timeouts = []

    def finish(request, client_address):
        timeouts.append(request.gettimeout())
        seen.set()

    httpd.finish_request = finish
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        with socket.create_connection(('127.0.0.1', httpd.server_address[1]), timeout=2):
            assert seen.wait(2)
        assert timeouts == [60]
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


@pytest.mark.parametrize('host,warn', [('0.0.0.0', True), ('::', True),
                                      ('127.0.0.1', False), ('127.0.0.2', False),
                                      ('localhost', False), ('::1', False)])
def test_network_bind_warning(tmp_path, monkeypatch, capsys, host, warn):
    from contextlib import contextmanager
    from types import SimpleNamespace

    @contextmanager
    def fake_server(*args):
        yield SimpleNamespace(server_address=(host, 6006), serve_forever=lambda: None)

    monkeypatch.setattr(server, 'make_server', fake_server)
    server.serve(tmp_path, host)
    output = capsys.readouterr().out
    assert ('no authentication' in output) == warn
    if warn:
        assert 'configs, env and code' in output


def test_summary_keeps_scalar_string_keys_in_order():
    value = {1: 'no', 'a': 1, 'b': {'n': 1}, 'c': True, 'd': None, 'e': 'x', 'f': 1.5}
    assert server._summary(value) == {'a': 1, 'c': True, 'd': None, 'e': 'x', 'f': 1.5}
    limited = server._summary(value, limit=3)
    assert list(limited) == ['a', 'c', 'd']
    assert limited['c'] is True
    assert server._summary(['nope']) == {}
    assert server._summary(None) == {}


def test_runs_list_config_scalars_and_nonfinite(dashboard):
    request, httpd, _, _ = dashboard
    meta_path = httpd.root / 'runs' / 'run-1' / 'meta.json'
    meta = json.loads(meta_path.read_text())
    config = {'nested': {'x': 1}, 'items': [1, 2, 3]}
    config['lr'] = 0.01
    config['label'] = 'ok' * 500
    config['epochs'] = 3
    config['flag'] = True
    config['optional'] = None
    config['nan'] = float('nan')
    config['posinf'] = float('inf')
    config['neginf'] = float('-inf')
    for i in range(50):
        config[f'k{i:02d}'] = i
    config['late'] = 'dropped'
    meta['config'] = config
    summary = {f's{i:02d}': i for i in range(51)}
    summary['nested'] = {'a': 1}
    meta['summary'] = summary
    meta_path.write_text(json.dumps(meta))
    status, headers, body = request('/api/runs', headers={'Accept-Encoding': 'gzip'})
    assert status == 200
    assert headers['Content-Encoding'] == 'gzip'
    runs = json.loads(gzip.decompress(body))
    row = runs[0]
    kept = ['lr', 'label', 'epochs', 'flag', 'optional', 'nan', 'posinf', 'neginf']
    kept += [f'k{i:02d}' for i in range(42)]
    assert list(row['config']) == kept
    assert len(row['config']) == 50
    assert row['config']['flag'] is True
    assert row['config']['epochs'] == 3
    assert row['config']['optional'] is None
    assert row['config']['lr'] == 0.01
    assert row['config']['nan'] is None
    assert row['config']['posinf'] is None
    assert row['config']['neginf'] is None
    assert 'nested' not in row['config']
    assert 'items' not in row['config']
    assert 'k42' not in row['config']
    assert 'late' not in row['config']
    assert len(row['summary']) == 51
    assert 'nested' not in row['summary']
    assert row['summary']['s50'] == 50
    assert row['last_step'] == 119
    assert row['tags'] == ['test']
    assert row['name'] == '<script>run</script>'


def _put_run(root, run_id, *, step, config, time_bytes=None):
    run = root / 'runs' / run_id
    scalars = run / 'scalars'
    scalars.mkdir(parents=True)
    meta = {
        'format_version': 1,
        'run_id': run_id,
        'project': 'demo',
        'name': run_id,
        'config': config,
        'tags': ['t'],
        'notes': '',
        'group': None,
        'status': 'finished',
        'summary': {'loss': 1.0, 'nested': {'a': 1}},
        'created_at': '2026-10-27T00:00:00Z',
        'finished_at': '2026-10-27T00:01:00Z',
    }
    (run / 'meta.json').write_text(json.dumps(meta))
    if time_bytes is not None:
        (scalars / '_time.bin').write_bytes(time_bytes)
    elif step is not None:
        (scalars / '_time.bin').write_bytes(struct.pack('<If', step, 1.0))


def test_list_last_step_for_many_runs(tmp_path, monkeypatch):
    def refuse(self, *args, **kwargs):
        raise AssertionError('list scanned series records')

    monkeypatch.setattr(reader.RunReader, 'series', refuse)
    monkeypatch.setattr(reader.RunReader, 'read_records', refuse)
    monkeypatch.setattr(reader.RunReader, 'time_index', refuse)
    monkeypatch.setattr(reader.RunReader, 'keys', refuse)
    expected = {}
    for i in range(100):
        run_id = f'm{i:03d}'
        time_bytes = None
        if i == 1:
            step = 0
        elif i == 3:
            step = 4
            time_bytes = struct.pack('<If', 9, 1.0) + struct.pack('<If', 4, 1.0)
        elif i % 11 == 0:
            step = None
        else:
            step = i
        _put_run(
            tmp_path, run_id, step=step, config={'i': i, 'skip': {'a': 1}}, time_bytes=time_bytes,
        )
        expected[run_id] = step
    broken = tmp_path / 'runs' / 'm002' / 'scalars' / '_time.bin'
    broken.unlink()
    broken.symlink_to(tmp_path / 'runs' / 'm002' / 'meta.json')
    expected['m002'] = None
    httpd = server.make_server(tmp_path, '127.0.0.1', 0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=10)
        conn.request('GET', '/api/runs')
        response = conn.getresponse()
        body = response.read()
        status = response.status
        conn.close()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert status == 200
    runs = json.loads(body)
    assert len(runs) == 100
    assert {row['run_id']: row['last_step'] for row in runs} == expected
    by_id = {row['run_id']: row for row in runs}
    assert by_id['m001']['last_step'] == 0
    assert by_id['m003']['last_step'] == 4
    assert by_id['m003']['config'] == {'i': 3}
    assert by_id['m003']['summary'] == {'loss': 1.0}
    assert by_id['m002']['last_step'] is None
    assert by_id['m000']['last_step'] is None


def test_list_last_step_does_not_evict_chart_reader(tmp_path, monkeypatch):
    n = 20
    records = b''.join(struct.pack('<If', i, float(i)) for i in range(n))
    _put_run(tmp_path, 'charted', step=None, config={'lr': 0.1}, time_bytes=records)
    scalars = tmp_path / 'runs' / 'charted' / 'scalars'
    (scalars / 'index.json').write_text(json.dumps({
        'version': 1, 'series': {'loss': {'id': 0, 'monotonic': True}},
    }))
    (scalars / '0.bin').write_bytes(records)
    httpd = server.make_server(tmp_path, '127.0.0.1', 0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()

    def request(path):
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=10)
        conn.request('GET', path)
        response = conn.getresponse()
        status, body = response.status, response.read()
        conn.close()
        return status, body

    try:
        status, body = request('/api/runs/charted/scalars?key=loss&max_points=10')
        assert status == 200
        series = json.loads(body)['series']['loss']
        assert series['mode'] == 'overview'
        assert series['total'] == n
        cached = httpd.readers['charted']
        incremental = cached._cache['loss']
        assert incremental[0] == 'inc'
        assert incremental[2] == n * reader.RECORD_SIZE
        others = 65
        for i in range(others):
            run_id = f'o{i:03d}'
            if i == 4:
                _put_run(
                    tmp_path, run_id, step=4, config={'i': i},
                    time_bytes=struct.pack('<If', 9, 1.0) + struct.pack('<If', 4, 1.0),
                )
            else:
                _put_run(tmp_path, run_id, step=i, config={'i': i})
        reads = []
        real_open = reader.open_nofollow

        class _Handle:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def __getattr__(self, key):
                return getattr(self.handle, key)

            def read(self, size=-1):
                reads.append(size)
                return self.handle.read(size)

        def tracking_open(path, mode='rb', buffering=-1):
            return _Handle(real_open(path, mode, buffering))

        monkeypatch.setattr(reader, 'open_nofollow', tracking_open)
        status, body = request('/api/runs')
        assert status == 200
        runs = json.loads(body)
        by_id = {row['run_id']: row for row in runs}
        assert len(runs) == others + 1
        assert by_id['charted']['last_step'] == n - 1
        assert by_id['o004']['last_step'] == 4
        assert by_id['o000']['last_step'] == 0
        assert by_id['o010']['last_step'] == 10
        with httpd.reader_lock:
            assert httpd.readers.get('charted') is cached
            assert set(httpd.readers) == {'charted'}
        assert cached._cache['loss'] is incremental
        assert reads == [reader.RECORD_SIZE] * (others + 1)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _init_run(directory, **kwargs):
    options = dict(
        dir=directory, track_source=False, capture_env=False,
        system_metrics=False, auto_track=False,
    )
    options.update(kwargs)
    return agenticml.init(**options)


def test_init_project_defaults_to_repo_directory(tmp_path, monkeypatch):
    repo = tmp_path / 'widgetlab'
    nested = repo / 'pkg'
    nested.mkdir(parents=True)
    (repo / 'pyproject.toml').write_text("[project]\nname = 'widgetlab'\n")
    script = nested / 'train.py'
    script.write_text('pass\n')
    monkeypatch.delenv('AGENTICML_PROJECT', raising=False)
    monkeypatch.setattr(sys.modules['__main__'], '__file__', str(script), raising=False)
    store = tmp_path / 'store'
    run = _init_run(store, name='exp')
    try:
        assert run.project == 'widgetlab'
        assert run.name == 'exp'
    finally:
        agenticml.finish()

    monkeypatch.setenv('AGENTICML_PROJECT', '')
    run = _init_run(store, name='exp')
    try:
        assert run.project == 'widgetlab'
    finally:
        agenticml.finish()

    monkeypatch.setenv('AGENTICML_PROJECT', 'from-env')
    run = _init_run(store)
    try:
        assert run.project == 'from-env'
        assert run.name is None
    finally:
        agenticml.finish()

    run = _init_run(store, project='explicit', name='named')
    try:
        assert run.project == 'explicit'
        assert run.name == 'named'
    finally:
        agenticml.finish()


def test_init_project_uses_cwd_without_main_file(tmp_path, monkeypatch):
    repo = tmp_path / 'cwdproj'
    repo.mkdir()
    (repo / 'pyproject.toml').write_text("[project]\nname = 'cwdproj'\n")
    monkeypatch.delenv('AGENTICML_PROJECT', raising=False)
    monkeypatch.delattr(sys.modules['__main__'], '__file__', raising=False)
    monkeypatch.chdir(repo)
    run = _init_run(tmp_path / 'store')
    try:
        assert run.project == 'cwdproj'
    finally:
        agenticml.finish()


def test_resume_keeps_stored_project(tmp_path, monkeypatch):
    repo = tmp_path / 'widgetlab'
    repo.mkdir()
    (repo / 'pyproject.toml').write_text("[project]\nname = 'widgetlab'\n")
    script = repo / 'train.py'
    script.write_text('pass\n')
    monkeypatch.delenv('AGENTICML_PROJECT', raising=False)
    monkeypatch.setattr(sys.modules['__main__'], '__file__', str(script), raising=False)
    store = tmp_path / 'store'
    options = dict(
        dir=store, track_source=False, capture_env=False,
        system_metrics=False, auto_track=False,
    )
    with Run(None, 'kept', id='resume-me', resume='allow', **options) as run:
        assert run.project == 'widgetlab'
        assert run.name == 'kept'
    monkeypatch.setenv('AGENTICML_PROJECT', 'changed')
    with Run('explicit', 'other', id='resume-me', resume='allow', **options) as resumed:
        assert resumed.project == 'widgetlab'
        assert resumed.name == 'kept'
    assert reader.RunReader(store, 'resume-me').meta()['project'] == 'widgetlab'
    assert reader.RunReader(store, 'resume-me').meta()['name'] == 'kept'


def test_default_project_empty_directory_name(monkeypatch):
    monkeypatch.delenv('AGENTICML_PROJECT', raising=False)
    monkeypatch.setattr('agenticml.code_tracking.find_repo_root', lambda start: Path('/'))
    assert default_project(Path('/tmp')) == 'default'
    monkeypatch.setenv('AGENTICML_PROJECT', 'from-env')
    assert default_project(Path('/tmp')) == 'from-env'


_NODE_KEYS = {
    'run_id', 'name', 'project', 'status', 'created_at', 'commit', 'tree',
    'base_commit', 'base_branch', 'base_dirty', 'parent_run_id', 'parent_inference',
    'same_tree_as_parent', 'error',
}
_EDGE_KEYS = {'from', 'to', 'inferred'}
_DIFF_KEYS = {
    'run_id', 'commit', 'parent_run_id', 'parent_commit', 'files', 'truncated',
    'checkout', 'error',
}
_TREE = 'ab' * 20
_COMMIT = {
    'a': 'a' * 40,
    'b': 'b' * 40,
    'c': 'c' * 40,
    'd': 'd' * 40,
}


def _stamp(second):
    return f'2026-10-27T00:00:{second:02d}Z'


def _seed_run(root, run_id, *, project, name, second, lineage=None, parent_run_id=None, git=None):
    run = root / 'runs' / run_id
    run.mkdir(parents=True)
    meta = {
        'format_version': 1,
        'run_id': run_id,
        'project': project,
        'name': name,
        'status': 'finished',
        'created_at': _stamp(second),
        'parent_run_id': parent_run_id,
        'git': git or {},
    }
    if lineage is not None:
        meta['lineage'] = lineage
    (run / 'meta.json').write_text(json.dumps(meta))


def _lineage_meta(**overrides):
    payload = {
        'version': 1,
        'commit': None,
        'tree': None,
        'parent_commit': None,
        'parent_run_id': None,
        'parent_inference': None,
        'project_key': 'alpha-0123abcd',
        'repo_root': '/src/alpha',
        'base_commit': '1234abcd' * 5,
        'base_branch': 'main',
        'base_dirty': False,
        'error': None,
    }
    payload.update(overrides)
    return payload


def _serve(root):
    httpd = server.make_server(root, '127.0.0.1', 0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    return httpd, thread


def _stop(httpd, thread):
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()


def _get(httpd, path):
    conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=5)
    conn.request('GET', path)
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return result


def _body(httpd, path, status=200):
    code, headers, raw = _get(httpd, path)
    assert code == status, raw
    if status == 200:
        assert headers['Content-Type'] == 'application/json'
    return json.loads(raw)


def _seed_lineage_store(root):
    _seed_run(root, 'run-a', project='alpha', name='root', second=0, git={'repo_root': '/src/alpha'})
    _seed_run(
        root, 'run-b', project='alpha', name='explicit-child', second=2, parent_run_id='run-a',
        lineage=_lineage_meta(
            commit=_COMMIT['b'], tree=_TREE, parent_commit=_COMMIT['a'],
            parent_run_id='run-a', parent_inference='explicit',
        ),
    )
    _seed_run(
        root, 'run-c', project='alpha', name='inferred-child', second=3,
        lineage=_lineage_meta(
            commit=_COMMIT['c'], tree=_TREE, parent_commit=_COMMIT['b'],
            parent_run_id='run-b', parent_inference='repo_root', base_dirty=True,
        ),
    )
    _seed_run(root, 'run-d', project='alpha', name='read-time-repo', second=4, git={'repo_root': '/src/alpha'})
    _seed_run(root, 'run-e', project='alpha', name='read-time-project', second=5, git={'repo_root': '/src/other'})
    _seed_run(
        root, 'run-f', project='alpha', name='no-parent', second=6,
        lineage=_lineage_meta(parent_run_id=None, parent_inference=None, commit=_COMMIT['d'], tree=_TREE),
    )
    _seed_run(
        root, 'run-z', project='alpha', name='failed', second=7,
        lineage=_lineage_meta(commit=None, tree=None, parent_run_id=None, error='snapshot failed'),
    )
    _seed_run(root, 'run-h', project='', name='blank-root', second=1)
    _seed_run(root, 'run-i', project='', name='blank-child', second=2)
    _seed_run(
        root, 'run-g', project='other', name='cross', second=1,
        lineage=_lineage_meta(parent_run_id='run-a', parent_inference='explicit', repo_root='/src/other'),
    )
    _seed_run(
        root, 'run-j', project='cycle', name='cycle-old', second=1,
        lineage=_lineage_meta(parent_run_id='run-k', parent_inference='explicit', project_key='cycle-0123abcd'),
    )
    _seed_run(
        root, 'run-k', project='cycle', name='cycle-new', second=2,
        lineage=_lineage_meta(parent_run_id='run-j', parent_inference='explicit', project_key='cycle-0123abcd'),
    )
    _seed_run(
        root, 'run-s', project='loop', name='self', second=1,
        lineage=_lineage_meta(parent_run_id='run-s', parent_inference='explicit', project_key='loop-0123abcd'),
    )


def _index(payload):
    return {node['run_id']: node for node in payload['nodes']}


def _edges(payload):
    return {(edge['from'], edge['to']): edge for edge in payload['edges']}


def _assert_acyclic(edges):
    children = {}
    for edge in edges:
        children.setdefault(edge['from'], []).append(edge['to'])
    seen = set()

    def walk(node, stack):
        assert node not in stack
        if node in seen:
            return
        stack.add(node)
        for child in children.get(node, []):
            walk(child, stack)
        stack.remove(node)
        seen.add(node)

    for node in list(children):
        walk(node, set())


def test_lineage_legacy_run_and_filters(dashboard):
    data = load(dashboard, '/api/lineage')
    assert data['edges'] == []
    assert set(data['nodes'][0]) == _NODE_KEYS
    assert data['nodes'][0]['run_id'] == 'run-1'
    assert data['nodes'][0]['parent_run_id'] is None
    assert data['nodes'][0]['parent_inference'] is None
    assert data['nodes'][0]['same_tree_as_parent'] is False
    assert data['nodes'][0]['commit'] is None
    demo = load(dashboard, '/api/lineage?project=demo')
    assert [node['run_id'] for node in demo['nodes']] == ['run-1']
    assert load(dashboard, '/api/lineage?project=') == {'nodes': [], 'edges': []}
    assert load(dashboard, '/api/lineage?project=missing') == {'nodes': [], 'edges': []}
    diff = load(dashboard, '/api/runs/run-1/lineage')
    assert set(diff) == _DIFF_KEYS
    assert diff['error'] == 'no lineage for this run'
    assert diff['files'] == []
    assert diff['truncated'] is False
    assert diff['checkout'] == 'agenticml lineage checkout run-1'
    assert diff['commit'] is None


def test_lineage_invalid_run_id(dashboard):
    for path in ('/api/runs/../lineage', '/api/runs/%2e%2e/lineage', '/api/runs/%00/lineage'):
        status, headers, body = dashboard[0](path)
        assert status == 400
        assert headers['Content-Type'] == 'application/json'
        assert isinstance(json.loads(body)['error'], str)
    status, _, body = dashboard[0]('/api/runs/missing-run/lineage')
    assert status == 404
    assert json.loads(body) == {'error': 'not found'}


def test_lineage_graph_inference_filter_and_cycles(tmp_path):
    _seed_lineage_store(tmp_path)
    httpd, thread = _serve(tmp_path)
    try:
        whole = _body(httpd, '/api/lineage')
        alpha = _body(httpd, '/api/lineage?project=alpha')
        blank = _body(httpd, '/api/lineage?project=')
        other = _body(httpd, '/api/lineage?project=other')
        cycle = _body(httpd, '/api/lineage?project=cycle')
        loop = _body(httpd, '/api/lineage?project=loop')
    finally:
        _stop(httpd, thread)
    for payload in (whole, alpha, blank, other, cycle, loop):
        assert set(payload) == {'nodes', 'edges'}
        ids = {node['run_id'] for node in payload['nodes']}
        for node in payload['nodes']:
            assert set(node) == _NODE_KEYS
        for edge in payload['edges']:
            assert set(edge) == _EDGE_KEYS
            assert isinstance(edge['inferred'], bool)
            assert edge['from'] in ids and edge['to'] in ids
            assert edge['from'] != edge['to']
        _assert_acyclic(payload['edges'])
    assert {node['run_id'] for node in whole['nodes']} == {
        'run-a', 'run-b', 'run-c', 'run-d', 'run-e', 'run-f', 'run-z',
        'run-h', 'run-i', 'run-g', 'run-j', 'run-k', 'run-s',
    }
    nodes = _index(alpha)
    edges = _edges(alpha)
    assert set(nodes) == {'run-a', 'run-b', 'run-c', 'run-d', 'run-e', 'run-f', 'run-z'}
    assert nodes['run-a']['parent_run_id'] is None
    assert nodes['run-a']['commit'] is None
    assert nodes['run-a']['base_dirty'] is None
    assert nodes['run-b']['parent_run_id'] == 'run-a'
    assert nodes['run-b']['parent_inference'] == 'explicit'
    assert nodes['run-b']['same_tree_as_parent'] is False
    assert nodes['run-b']['base_dirty'] is False
    assert nodes['run-b']['commit'] == _COMMIT['b']
    assert nodes['run-c']['parent_run_id'] == 'run-b'
    assert nodes['run-c']['parent_inference'] == 'repo_root'
    assert nodes['run-c']['same_tree_as_parent'] is True
    assert nodes['run-c']['base_dirty'] is True
    assert nodes['run-c']['base_branch'] == 'main'
    assert nodes['run-d']['parent_run_id'] == 'run-c'
    assert nodes['run-d']['parent_inference'] == 'repo_root'
    assert nodes['run-e']['parent_run_id'] == 'run-d'
    assert nodes['run-e']['parent_inference'] == 'project'
    assert nodes['run-f']['parent_run_id'] is None
    assert nodes['run-f']['parent_inference'] is None
    assert nodes['run-z']['error'] == 'snapshot failed'
    assert nodes['run-z']['commit'] is None
    assert edges[('run-a', 'run-b')]['inferred'] is False
    assert edges[('run-b', 'run-c')]['inferred'] is True
    assert edges[('run-c', 'run-d')]['inferred'] is True
    assert edges[('run-d', 'run-e')]['inferred'] is True
    assert ('run-a', 'run-g') not in edges
    assert all(node['status'] == 'finished' for node in nodes.values())
    blank_nodes = _index(blank)
    assert set(blank_nodes) == {'run-h', 'run-i'}
    assert blank_nodes['run-i']['parent_run_id'] == 'run-h'
    assert blank_nodes['run-i']['parent_inference'] == 'project'
    assert _edges(blank)[('run-h', 'run-i')]['inferred'] is True
    assert _index(other)['run-g']['parent_run_id'] == 'run-a'
    assert other['edges'] == []
    assert ('run-a', 'run-g') in _edges(whole)
    assert _edges(whole)[('run-a', 'run-g')]['inferred'] is False
    assert len(cycle['edges']) == 1
    assert {edge['from'], edge['to']} == {'run-j', 'run-k'}
    assert _index(cycle)['run-j']['parent_run_id'] == 'run-k'
    assert _index(cycle)['run-k']['parent_run_id'] == 'run-j'
    assert loop['edges'] == []
    assert _index(loop)['run-s']['parent_run_id'] == 'run-s'


def test_lineage_diff_missing_repo_and_git_failures(tmp_path, monkeypatch):
    _seed_lineage_store(tmp_path)
    outside = tmp_path / 'outside-repo'
    outside.mkdir()
    key = 'alpha-0123abcd'
    link_parent = tmp_path / 'lineage'
    link_parent.mkdir()
    # run-b uses this key and a real commit, but the repo is absent until the symlink test.
    httpd, thread = _serve(tmp_path)
    try:
        missing = _body(httpd, '/api/runs/run-b/lineage')
        assert missing['error'] == 'lineage repository not found'
        assert missing['files'] == []
        assert missing['truncated'] is False
        assert missing['commit'] == _COMMIT['b']
        assert missing['parent_commit'] == _COMMIT['a']
        assert missing['parent_run_id'] == 'run-a'
        assert missing['checkout'] == 'agenticml lineage checkout run-b'
        failed = _body(httpd, '/api/runs/run-z/lineage')
        assert failed['error'] == 'snapshot failed'
        assert failed['files'] == []
        none = _body(httpd, '/api/runs/run-a/lineage')
        assert none['error'] == 'no lineage for this run'
        assert none['files'] == []
    finally:
        _stop(httpd, thread)

    escaped = tmp_path / 'escape-store'
    _seed_run(
        escaped, 'run-x', project='alpha', name='escape', second=1,
        lineage=_lineage_meta(commit=_COMMIT['a'], project_key=key),
    )
    repo_link = escaped / 'lineage' / f'{key}.git'
    repo_link.parent.mkdir(parents=True)
    repo_link.symlink_to(outside)
    httpd, thread = _serve(escaped)
    try:
        payload = _body(httpd, '/api/runs/run-x/lineage')
        raw = json.dumps(payload).encode()
    finally:
        _stop(httpd, thread)
    assert payload['error'] == 'invalid lineage repository'
    assert payload['files'] == []
    assert str(outside).encode() not in raw

    bad = tmp_path / 'bad-key'
    _seed_run(
        bad, 'run-y', project='alpha', name='bad-key', second=1,
        lineage=_lineage_meta(commit=_COMMIT['a'], project_key='../evil'),
    )
    httpd, thread = _serve(bad)
    try:
        payload = _body(httpd, '/api/runs/run-y/lineage')
    finally:
        _stop(httpd, thread)
    assert payload['error'] == 'invalid lineage project key'
    assert payload['files'] == []

    empty = tmp_path / 'empty-git'
    _seed_run(
        empty, 'run-q', project='alpha', name='empty', second=1,
        lineage=_lineage_meta(commit=_COMMIT['a'], parent_commit=_COMMIT['b'], project_key=key),
    )
    (empty / 'lineage' / f'{key}.git').mkdir(parents=True)

    def missing_git(*args, **kwargs):
        raise FileNotFoundError('git')

    monkeypatch.setattr(server.subprocess, 'Popen', missing_git)
    httpd, thread = _serve(empty)
    try:
        payload = _body(httpd, '/api/runs/run-q/lineage')
    finally:
        _stop(httpd, thread)
    assert payload['error'] == 'git is not available'
    assert payload['files'] == []

    class _TimeoutProc:
        def __init__(self, *args, **kwargs):
            self.stdout = None
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def timed_out(proc, timeout):
        raise subprocess.TimeoutExpired(cmd='git', timeout=timeout)

    monkeypatch.setattr(server.subprocess, 'Popen', lambda *args, **kwargs: _TimeoutProc())
    monkeypatch.setattr(server, '_iter_stdout', timed_out)
    httpd, thread = _serve(empty)
    try:
        payload = _body(httpd, '/api/runs/run-q/lineage')
    finally:
        _stop(httpd, thread)
    assert payload['error'] == 'git diff timed out'

    def failed_git(*args, **kwargs):
        assert kwargs.get('stderr') is subprocess.DEVNULL
        proc = _TimeoutProc()
        proc.returncode = 1
        proc.stderr_bytes = b'fatal: secret /tmp/private'
        return proc

    def empty_stdout(proc, timeout):
        if False:
            yield b''

    monkeypatch.setattr(server.subprocess, 'Popen', failed_git)
    monkeypatch.setattr(server, '_iter_stdout', empty_stdout)
    httpd, thread = _serve(empty)
    try:
        code, _, raw = _get(httpd, '/api/runs/run-q/lineage')
    finally:
        _stop(httpd, thread)
    assert code == 200
    assert b'secret' not in raw
    assert b'/tmp/private' not in raw
    assert json.loads(raw)['error'] == 'git diff failed'
    assert json.loads(raw)['files'] == []


def _git(repo, *args, input=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(
        GIT_AUTHOR_NAME='Tester', GIT_AUTHOR_EMAIL='t@example.com',
        GIT_COMMITTER_NAME='Tester', GIT_COMMITTER_EMAIL='t@example.com',
    )
    command = ['git', '--git-dir', str(repo), *args] if repo is not None else ['git', *args]
    completed = subprocess.run(command, input=input, capture_output=True, check=True, env=env, timeout=30)
    return completed.stdout


def test_lineage_diff_numstat_binary_and_truncated(tmp_path):
    if shutil.which('git') is None:
        pytest.skip('git is not available')
    root = tmp_path / 'store'
    key = 'demo-abcdef12'
    repo = root / 'lineage' / f'{key}.git'
    repo.parent.mkdir(parents=True)
    _git(None, 'init', '--bare', str(repo))
    hello = _git(repo, 'hash-object', '-w', '--stdin', input=b'hello\n').decode().strip()
    edited = _git(repo, 'hash-object', '-w', '--stdin', input=b'hello\nworld\n').decode().strip()
    added = _git(repo, 'hash-object', '-w', '--stdin', input=b'n\n').decode().strip()
    binary = _git(repo, 'hash-object', '-w', '--stdin', input=b'a\0b').decode().strip()
    tree_root = _git(
        repo, 'mktree',
        input=f'100644 blob {hello}\thello.txt\n100644 blob {binary}\tbin.dat\n'.encode(),
    ).decode().strip()
    tree_child = _git(
        repo, 'mktree',
        input=(
            f'100644 blob {edited}\thello.txt\n'
            f'100644 blob {binary}\tbin.dat\n'
            f'100644 blob {added}\tnew.txt\n'
        ).encode(),
    ).decode().strip()
    parent_commit = _git(repo, 'commit-tree', tree_root, '-m', 'root').decode().strip()
    child_commit = _git(repo, 'commit-tree', tree_child, '-p', parent_commit, '-m', 'child').decode().strip()
    blob = _git(repo, 'hash-object', '-w', '--stdin', input=b'x\n').decode().strip()
    wide = ''.join(f'100644 blob {blob}\tf{index:04d}.txt\n' for index in range(501))
    wide_tree = _git(repo, 'mktree', input=wide.encode()).decode().strip()
    wide_commit = _git(repo, 'commit-tree', wide_tree, '-m', 'wide').decode().strip()
    _git(repo, 'update-ref', 'refs/runs/diff-root', parent_commit)
    _git(repo, 'update-ref', 'refs/runs/diff-child', child_commit)
    _git(repo, 'update-ref', 'refs/runs/diff-wide', wide_commit)
    _seed_run(
        root, 'diff-root', project='demo', name='root', second=1,
        lineage=_lineage_meta(commit=parent_commit, tree=tree_root, parent_commit=None, project_key=key),
    )
    _seed_run(
        root, 'diff-child', project='demo', name='child', second=2, parent_run_id='diff-root',
        lineage=_lineage_meta(
            commit=child_commit, tree=tree_child, parent_commit=parent_commit,
            parent_run_id='diff-root', parent_inference='explicit', project_key=key,
        ),
    )
    _seed_run(
        root, 'diff-wide', project='demo', name='wide', second=3,
        lineage=_lineage_meta(commit=wide_commit, tree=wide_tree, parent_commit=None, project_key=key),
    )
    httpd, thread = _serve(root)
    try:
        root_diff = _body(httpd, '/api/runs/diff-root/lineage')
        child_diff = _body(httpd, '/api/runs/diff-child/lineage')
        wide_diff = _body(httpd, '/api/runs/diff-wide/lineage')
    finally:
        _stop(httpd, thread)
    assert root_diff['error'] is None and root_diff['truncated'] is False
    assert root_diff['parent_commit'] is None
    assert {row['path']: (row['added'], row['deleted']) for row in root_diff['files']} == {
        'hello.txt': (1, 0),
        'bin.dat': (None, None),
    }
    assert child_diff['error'] is None
    assert child_diff['parent_run_id'] == 'diff-root'
    assert child_diff['parent_commit'] == parent_commit
    assert {row['path']: (row['added'], row['deleted']) for row in child_diff['files']} == {
        'hello.txt': (1, 0),
        'new.txt': (1, 0),
    }
    assert wide_diff['error'] is None
    assert wide_diff['truncated'] is True
    assert len(wide_diff['files']) == 500
    assert {row['path'] for row in wide_diff['files']} <= {f'f{index:04d}.txt' for index in range(501)}
    assert all(row['added'] == 1 and row['deleted'] == 0 for row in wide_diff['files'])


class _GitProc:
    def __init__(self, returncode=0):
        self.stdout = None
        self.returncode = returncode
        self.terminated = False
        self.waited = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _patch_diff_git(monkeypatch, chunks, *, returncode=0):
    box = {'proc': None, 'pulled': 0, 'blobs': []}

    def popen(*args, **kwargs):
        assert kwargs.get('stderr') is subprocess.DEVNULL
        box['proc'] = _GitProc(returncode)
        return box['proc']

    def iterate(proc, timeout):
        for chunk in chunks:
            box['pulled'] += 1
            yield chunk

    real_parse = server._parse_numstat

    def counting(blob):
        box['blobs'].append(bytes(blob))
        return real_parse(blob)

    monkeypatch.setattr(server.subprocess, 'Popen', popen)
    monkeypatch.setattr(server, '_iter_stdout', iterate)
    monkeypatch.setattr(server, '_parse_numstat', counting)
    return box


def _seed_diff_target(root):
    key = 'demo-abcdef12'
    (root / 'lineage' / f'{key}.git').mkdir(parents=True)
    _seed_run(
        root, 'cap-run', project='demo', name='cap', second=1,
        lineage=_lineage_meta(commit=_COMMIT['a'], parent_commit=None, project_key=key),
    )


def test_lineage_diff_stream_caps_and_exact_paths(tmp_path, monkeypatch):
    assert server._LINEAGE_FILES == 500
    assert server._GIT_MAX_BYTES == 2 * 1024 * 1024
    root = tmp_path / 'stream'
    _seed_diff_target(root)
    record = b'1\t0\ta.txt\0'
    paths = _patch_diff_git(monkeypatch, [
        b'4\t1\tweird\r\nname\0',
        b'1\t0\tbad\xffname\0',
    ])
    httpd, thread = _serve(root)
    try:
        exact = _body(httpd, '/api/runs/cap-run/lineage')
        assert exact['error'] is None and exact['truncated'] is False
        assert [row['path'] for row in exact['files']] == ['weird\r\nname', 'bad\ufffdname']
        assert paths['pulled'] == 2
        assert len(paths['blobs']) == 1
        assert paths['proc'].waited and not paths['proc'].terminated

        monkeypatch.setattr(server, '_LINEAGE_FILES', 3)
        capped = _patch_diff_git(
            monkeypatch, [record] * 30, returncode=None,
        )
        overflow = _body(httpd, '/api/runs/cap-run/lineage')
        assert overflow['truncated'] is True
        assert overflow['error'] is None
        assert len(overflow['files']) <= 3
        assert capped['blobs'][-1].count(b'\0') <= 3
        assert capped['pulled'] < 30
        assert capped['proc'].terminated and capped['proc'].waited

        monkeypatch.setattr(server, '_LINEAGE_FILES', 50)
        monkeypatch.setattr(server, '_GIT_MAX_BYTES', 20)
        bounded = _patch_diff_git(
            monkeypatch, [record] * 10, returncode=None,
        )
        limited = _body(httpd, '/api/runs/cap-run/lineage')
        assert limited['truncated'] is True
        assert len(bounded['blobs'][-1]) <= 20
        assert bounded['blobs'][-1].count(b'\0') <= 50
        assert bounded['pulled'] < 10
        assert bounded['proc'].terminated and bounded['proc'].waited

        fitted = _patch_diff_git(monkeypatch, [record, record])
        complete = _body(httpd, '/api/runs/cap-run/lineage')
    finally:
        _stop(httpd, thread)
    assert complete['truncated'] is False
    assert len(complete['files']) == 2
    assert fitted['pulled'] == 2
    assert fitted['blobs'][-1].count(b'\0') == 2
    assert not fitted['proc'].terminated


def test_lineage_parent_precedence_ties_and_malformed_meta(tmp_path):
    root = tmp_path / 'parents'
    _seed_run(root, 'run-early', project='alpha', name='early', second=1, git={'repo_root': '/src/alpha'})
    _seed_run(
        root, 'run-top', project='alpha', name='top-wins', second=2, parent_run_id='run-early',
        lineage=_lineage_meta(
            parent_run_id='run-missing', parent_inference='repo_root', commit=_COMMIT['b'],
        ),
    )
    _seed_run(
        root, 'run-bad', project='alpha', name='bad-root', second=3, lineage=['not-a-dict'],
        git={'repo_root': '~__nonexistent_user__/repo'},
    )
    weird = root / 'runs' / 'run-weird'
    weird.mkdir(parents=True)
    (weird / 'meta.json').write_text(json.dumps({
        'format_version': 1,
        'run_id': 'run-weird',
        'project': 'alpha',
        'name': {'not': 'a string'},
        'status': 5,
        'created_at': {'bad': True},
        'parent_run_id': 12,
        'git': 'not-a-dict',
        'lineage': 'not-a-dict',
    }))
    _seed_run(
        root, 'run-agree', project='alpha', name='agree', second=4, parent_run_id='run-early',
        lineage=_lineage_meta(parent_run_id='run-early', parent_inference='repo_root'),
    )
    for run_id in ('m-2', 'm-1', 'm-3'):
        _seed_run(root, run_id, project='ties', name=run_id, second=8, git={'repo_root': '/src/ties'})
    _seed_run(root, 's-b', project='cross', name='bee', second=9, git={'repo_root': '/src/bee'})
    _seed_run(root, 's-a', project='cross', name='ay', second=9, git={'repo_root': '/src/ay'})
    httpd, thread = _serve(root)
    try:
        alpha = _body(httpd, '/api/lineage?project=alpha')
        ties = _body(httpd, '/api/lineage?project=ties')
        cross = _body(httpd, '/api/lineage?project=cross')
        diff = _body(httpd, '/api/runs/run-top/lineage')
    finally:
        _stop(httpd, thread)
    nodes = _index(alpha)
    assert set(nodes) == {'run-early', 'run-top', 'run-bad', 'run-weird', 'run-agree'}
    assert nodes['run-early']['parent_run_id'] is None
    assert nodes['run-top']['parent_run_id'] == 'run-early'
    assert nodes['run-top']['parent_inference'] == 'explicit'
    assert nodes['run-bad']['parent_run_id'] == 'run-top'
    assert nodes['run-bad']['parent_inference'] == 'project'
    assert nodes['run-weird']['parent_run_id'] is None
    assert nodes['run-weird']['name'] == ''
    assert nodes['run-weird']['status'] == ''
    assert nodes['run-agree']['parent_run_id'] == 'run-early'
    assert nodes['run-agree']['parent_inference'] == 'repo_root'
    edges = _edges(alpha)
    assert edges[('run-early', 'run-top')]['inferred'] is False
    assert edges[('run-top', 'run-bad')]['inferred'] is True
    assert ('run-missing', 'run-top') not in edges
    assert edges[('run-early', 'run-agree')]['inferred'] is True
    assert diff['parent_run_id'] == 'run-early'
    assert diff['error'] == 'lineage repository not found'
    tied = _index(ties)
    assert tied['m-1']['parent_run_id'] is None
    assert tied['m-2']['parent_run_id'] == 'm-1'
    assert tied['m-2']['parent_inference'] == 'repo_root'
    assert tied['m-3']['parent_run_id'] == 'm-2'
    assert tied['m-3']['parent_inference'] == 'repo_root'
    assert tied['m-1']['parent_run_id'] != 'm-1'
    crossed = _index(cross)
    assert crossed['s-a']['parent_run_id'] is None
    assert crossed['s-b']['parent_run_id'] == 's-a'
    assert crossed['s-b']['parent_inference'] == 'project'
    assert crossed['s-b']['parent_run_id'] != 's-b'


def test_lineage_inference_resolves_each_root_once(tmp_path, monkeypatch):
    root = tmp_path / 'once'
    ids = [f'n{index:02d}' for index in range(40)]
    for run_id in ids:
        _seed_run(root, run_id, project='bulk', name=run_id, second=1, git={'repo_root': '/src/bulk-root'})
    calls = {'n': 0}
    real_resolve = Path.resolve

    def counting(self, *args, **kwargs):
        if 'bulk-root' in str(self):
            calls['n'] += 1
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', counting)
    httpd, thread = _serve(root)
    try:
        calls['n'] = 0
        payload = _body(httpd, '/api/lineage?project=bulk')
    finally:
        _stop(httpd, thread)
    nodes = _index(payload)
    assert calls['n'] == 40
    assert nodes['n00']['parent_run_id'] is None
    for index in range(1, 40):
        assert nodes[ids[index]]['parent_run_id'] == ids[index - 1]
        assert nodes[ids[index]]['parent_inference'] == 'repo_root'
        assert nodes[ids[index]]['parent_run_id'] != ids[index]

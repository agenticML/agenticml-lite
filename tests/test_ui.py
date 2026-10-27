"""HTTP integration tests using the real reader and directly written v1 files."""
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import struct
import threading

import pytest

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
    assert set(runs[0]) == {'run_id','project','name','status','created_at','finished_at','tags','group','summary'}
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


@pytest.mark.parametrize('name',['index.html','app.js','app.css','uPlot.iife.min.js','uPlot.min.css','uPlot.LICENSE'])
def test_static(dashboard,name):
    path = '/' if name == 'index.html' else '/static/'+name
    status, headers, body = dashboard[0](path)
    assert status == 200 and body
    assert headers['ETag']
    assert dashboard[0](path,headers={'If-None-Match':headers['ETag']})[0] == 304


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

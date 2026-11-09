"""S3 objects use the same sync, authorization and quotas as disk objects."""
import hashlib
import json
import sys

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws
import pytest

from agenticml.server import admin, ingest
from agenticml.server.objectstore import FileObjects, S3Objects, open_backend
from agenticml.server.workspaces import delete_workspace, prepare_store, store_root
from agenticml.server.db.workspaces import WorkspaceRepo
from agenticml.store import RunWriter
from agenticml.ui import server as ui
from test_server_ingest import request
from test_server_signin import running
from test_server_web import call
from test_server_limits import configure
import test_server_isolation as isolation


@pytest.fixture
def aws(monkeypatch):
    for name, value in [('AWS_ACCESS_KEY_ID', 'testing'), ('AWS_SECRET_ACCESS_KEY', 'testing'),
                        ('AWS_DEFAULT_REGION', 'us-east-1')]:
        monkeypatch.setenv(name, value)
    with mock_aws():
        client = boto3.client('s3')
        client.create_bucket(Bucket='agml-test')
        yield client


@pytest.fixture
def server(tmp_path, aws):
    with running(tmp_path / 'data', objects='s3://agml-test/pfx') as server:
        _, server.test_key = server.keys.create('test', 'admin')
        server.test_cookie = isolation._key_cookie(server, server.test_key)
        yield server


def put(server, blob):
    sha = hashlib.sha256(blob).hexdigest()
    return sha, request(server, '/v2/objects/' + sha, 'PUT', blob)


def download(server, sha):
    return call(server, '/api/w/default/objects/' + sha,
                headers={'Cookie': server.test_cookie})


def test_sync_download_and_code_diff(server, tmp_path, aws):
    blobs = [b'print(1)\n', b'print(2)\n', b'preview', b'weights']
    for index, code in enumerate(blobs[:2]):
        writer = RunWriter.create(tmp_path / 'local', run_id='run-' + str(index), meta={'project': 'demo'})
        writer.add_code([], [('train.py', code)])
        writer.add_media('sample', 1, blobs[2], 'text/plain', 'caption')
        artifact = tmp_path / 'weights.bin'
        artifact.write_bytes(blobs[3])
        writer.add_artifact(artifact, 'weights', 'model', {}, copy=True)
        writer.finish('finished')
        writer.close()
        isolation._sync_run(server, server.test_key, writer)
    wid = server.default_workspace_id
    keys = {item['Key'] for item in aws.list_objects_v2(Bucket='agml-test')['Contents']}
    for blob in blobs:
        sha = hashlib.sha256(blob).hexdigest()
        key = f'pfx/{wid}/objects/{sha[:2]}/{sha}'
        assert key in keys
        assert download(server, sha)[::2] == (200, blob)
        assert aws.head_object(Bucket='agml-test', Key=key)['ServerSideEncryption'] == 'AES256'
    for workspace in WorkspaceRepo(server.db).list():
        assert not list((store_root(server.data_dir, workspace['id']) / 'objects').glob('*/*'))
    shas = [hashlib.sha256(blob).hexdigest() for blob in blobs[:2]]
    disk = FileObjects(tmp_path / 'disk')
    (tmp_path / 'disk').mkdir()
    prepare_store(tmp_path / 'disk', wid)
    for sha, blob in zip(shas, blobs):
        path = tmp_path / sha
        path.write_bytes(blob)
        disk.put(wid, sha, path, len(blob))
    expected = ui._code_pair_diff(lambda sha: disk.open(wid, sha), *shas, 10000, 100, ui._CodeDiffBudget())
    assert server.code_pair_diff(*shas, ui._CodeDiffBudget(), workspace_id=wid) == expected


@pytest.mark.parametrize('blob', [b'hello', b''])
def test_immutable_and_hash_validation(server, aws, blob):
    sha, first = put(server, blob)
    assert first[0:2] == (201, {'sha256': sha, 'created': True})
    key = server.objects._key(server.default_workspace_id, sha)
    etag = aws.head_object(Bucket='agml-test', Key=key)['ETag']
    assert put(server, blob)[1][0:2] == (200, {'sha256': sha, 'created': False})
    assert request(server, '/v2/objects/' + sha, 'PUT', b'wrong')[0] == 422
    assert aws.head_object(Bucket='agml-test', Key=key)['ETag'] == etag
    assert download(server, sha)[2] == blob


def test_kms(server, monkeypatch, tmp_path, aws):
    monkeypatch.setenv('AGENTICML_SERVER_S3_KMS_KEY_ID', 'alias/agml')
    server.objects = open_backend('s3://agml-test/pfx', server.data_dir)
    sha, result = put(server, b'kms')
    assert result[0] == 201
    head = aws.head_object(Bucket='agml-test', Key=server.objects._key(server.default_workspace_id, sha))
    assert head['ServerSideEncryption'] == 'aws:kms'
    assert head['SSEKMSKeyId'] == 'alias/agml'


def test_workspace_delete(server, tmp_path):
    server.users.add('owner', 'admin')
    other = WorkspaceRepo(server.db).create('other', 'Other', 'owner')
    prepare_store(server.data_dir, other['id'])
    sha, _ = put(server, b'isolated')
    path = tmp_path / 'blob'
    path.write_bytes(b'isolated')
    assert server.objects.missing(other['id'], [sha, sha]) == [sha]
    assert server.objects.put(other['id'], sha, path, 8)
    delete_workspace(server.db, server.data_dir, 'other', objects=server.objects)
    assert not server.objects.exists(other['id'], sha)
    assert server.objects.exists(server.default_workspace_id, sha)


def test_quota_and_orphan_recovery(server):
    usage = configure(server, storage_bytes=5)
    sha, result = put(server, b'12345')
    assert result[0] == 201
    assert usage.get()['stored_bytes'] == 5
    assert put(server, b'6')[1][0] == 403
    assert put(server, b'12345')[1][0] == 200
    assert usage.get()['stored_bytes'] == 5


@pytest.mark.parametrize('error', [ClientError({'Error': {'Code': '500', 'Message': 'AWS private text'}}, 'HeadObject'),
                                  EndpointConnectionError(endpoint_url='https://private-bucket')])
def test_failure_mapping(server, monkeypatch, error):
    def fail(**kwargs):
        raise error
    monkeypatch.setattr(server.objects.client, 'head_object', fail)
    status, payload, headers = request(server, '/v2/objects/missing', 'POST',
                                       json.dumps({'objects': ['a' * 64]}).encode())
    assert (status, payload, headers['Retry-After']) == (503, {'error': 'object store unavailable'}, '5')
    # Downloads use get_object, not head_object.
    monkeypatch.setattr(server.objects.client, 'get_object', fail)
    status, headers, body = download(server, 'a' * 64)
    assert status == 503 and headers['Retry-After'] == '5'
    assert json.loads(body) == {'error': 'object store unavailable'}


def test_record_failure_rolls_back(server, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('record failed')
    monkeypatch.setattr(ingest.IngestHandler, '_record_object', fail)
    sha, result = put(server, b'rollback')
    assert result[0] == 500
    assert not server.objects.exists(server.default_workspace_id, sha)
    assert not list((server.data_dir / 'server/tmp').iterdir())


def test_open_backend(tmp_path, aws, monkeypatch):
    for url in (None, '', 'file://', 'file://' + str(tmp_path)):
        assert isinstance(open_backend(url, tmp_path), FileObjects)
    for url in ('s3://agml-test', 's3://agml-test/a/b'):
        backend = open_backend(url, tmp_path)
        assert isinstance(backend, S3Objects) and backend.describe() == url
    for url in ('s3://b', 's3://BAD', 's3://127.0.0.1', 's3://agml-test/../x',
                's3://agml-test/a//b', 's3://agml-test/a/', 'http://x', 'file:///elsewhere'):
        with pytest.raises(ValueError):
            open_backend(url, tmp_path)
    monkeypatch.setitem(sys.modules, 'boto3', None)
    with pytest.raises(ValueError, match=r'\[s3\] extra'):
        open_backend('s3://agml-test', tmp_path)


def test_operator_reports_skip_local_objects(server, monkeypatch):
    original = admin._walk
    def walk(path, *args, **kwargs):
        assert 'objects' not in path.parts
        return original(path, *args, **kwargs)
    monkeypatch.setattr(admin, '_walk', walk)
    warning = 'objects are in S3; object checks come in package 23'
    assert warning in admin.format_status(admin.status(server.data_dir, objects='s3://agml-test/pfx'))
    monkeypatch.setenv('AGENTICML_SERVER_OBJECTS', 's3://agml-test/pfx')
    assert warning in admin.format_verify(admin.verify(server.data_dir))


@pytest.fixture
def iso(tmp_path_factory, aws, monkeypatch):
    monkeypatch.setenv('AGENTICML_SERVER_OBJECTS', 's3://agml-test/pfx')
    yield from isolation.iso.__wrapped__(tmp_path_factory)


# Reuse the existing authorization matrix with an S3-backed fixture.
test_sync_key_cannot_see_or_write_the_other_workspace = isolation.test_sync_key_cannot_see_or_write_the_other_workspace
test_key_session_url_matrix_and_revoke_is_scoped = isolation.test_key_session_url_matrix_and_revoke_is_scoped
test_workspace_routes_foreign_missing_and_suspended_are_identical = isolation.test_workspace_routes_foreign_missing_and_suspended_are_identical


def test_concurrent_upload_and_orphan_recovery(server, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    blob = b'concurrent'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: put(server, blob)[1][0], range(2)))
    assert sorted(results) == [200, 201]
    usage = configure(server, storage_bytes=100)
    orphan = b'orphan'
    sha = hashlib.sha256(orphan).hexdigest()
    path = tmp_path / 'orphan'
    path.write_bytes(orphan)
    assert server.objects.put(server.default_workspace_id, sha, path, len(orphan))
    before = usage.get()['stored_bytes']
    assert put(server, orphan)[1][0] == 200
    assert usage.get()['stored_bytes'] == before + len(orphan)


def test_conditional_put_checksum_and_precondition(server, tmp_path, monkeypatch):
    import base64
    sha = hashlib.sha256(b'conditional').hexdigest()
    path = tmp_path / 'conditional'
    path.write_bytes(b'conditional')
    def put_object(**kwargs):
        assert kwargs['IfNoneMatch'] == '*'
        assert kwargs['ChecksumSHA256'] == base64.b64encode(bytes.fromhex(sha)).decode()
        assert kwargs['ContentLength'] == len(b'conditional')
        assert kwargs['Body'].read() == b'conditional'
        raise ClientError({'Error': {'Code': 'PreconditionFailed'},
                           'ResponseMetadata': {'HTTPStatusCode': 412}}, 'PutObject')
    monkeypatch.setattr(server.objects.client, 'put_object', put_object)
    assert not server.objects.put(server.default_workspace_id, sha, path, len(b'conditional'))


def test_startup_bucket_unreachable(tmp_path, aws):
    from agenticml.server.app import make_server
    with pytest.raises(ValueError, match='bucket is unreachable'):
        make_server(tmp_path, port=0, objects='s3://absent-bucket/pfx')


@pytest.mark.parametrize('workspace,sha', [('../escape', 'a' * 64), ('invalid', 'a' * 64)])
def test_backend_rejects_invalid_workspace(server, workspace, sha):
    with pytest.raises(ValueError):
        server.objects.exists(workspace, sha)
    with pytest.raises(ValueError):
        server.objects.delete_workspace(workspace)


def test_fresh_hash_mismatch_writes_no_key(server, aws):
    sha = hashlib.sha256(b'expected bytes').hexdigest()
    prefix = server.objects._key(server.default_workspace_id, sha)
    assert request(server, '/v2/objects/' + sha, 'PUT', b'wrong')[0] == 422
    assert aws.list_objects_v2(Bucket='agml-test', Prefix=prefix)['KeyCount'] == 0


def test_deleted_bucket_is_unavailable(server, aws):
    aws.delete_bucket(Bucket='agml-test')
    sha = hashlib.sha256(b'new').hexdigest()
    for path, method, body in (
        ('/v2/objects/missing', 'POST', json.dumps({'objects': [sha]}).encode()),
        ('/v2/objects/' + sha, 'PUT', b'new'),
    ):
        status, payload, headers = request(server, path, method, body)
        assert (status, payload, headers['Retry-After']) == (503, {'error': 'object store unavailable'}, '5')
    status, headers, body = download(server, sha)
    assert (status, json.loads(body), headers['Retry-After']) == (503, {'error': 'object store unavailable'}, '5')


def test_same_hash_is_isolated_and_charged(iso):
    from agenticml.server.db.usage import UsageRepo
    server = iso['server']
    blob = b'new shared hash'
    sha = hashlib.sha256(blob).hexdigest()
    assert request(server, '/v2/objects/' + sha, 'PUT', blob, iso['sync_a'])[0] == 201
    cookie = iso['cookies']['password:owner-b']
    actual = isolation._ask(server, 'GET', '/api/w/beta/objects/' + sha, cookie=cookie)
    unknown = isolation._ask(server, 'GET', '/api/w/beta/objects/' + 'f' * 64, cookie=cookie)
    assert actual[0] == unknown[0] == 404
    assert actual[2] == unknown[2]
    assert request(server, '/v2/objects/missing', 'POST', json.dumps({'objects': [sha]}).encode(),
                   iso['sync_b'])[1] == {'missing': [sha]}
    usage = UsageRepo(server.db, iso['b']['id'])
    before = usage.get()['stored_bytes']
    assert request(server, '/v2/objects/' + sha, 'PUT', blob, iso['sync_b'])[:2] == (
        201, {'sha256': sha, 'created': True})
    assert usage.get()['stored_bytes'] == before + len(blob)


@pytest.mark.parametrize('count', [3, 1005])
def test_workspace_delete_all_pages(server, aws, count):
    server.users.add('owner', 'admin')
    other = WorkspaceRepo(server.db).create('other', 'Other', 'owner')
    prepare_store(server.data_dir, other['id'])
    sha, _ = put(server, b'keep me')
    prefix = f"pfx/{other['id']}/"
    for index in range(count):
        aws.put_object(Bucket='agml-test', Key=prefix + f'objects/{index:05d}', Body=b'x')
    delete_workspace(server.db, server.data_dir, 'other', objects=server.objects)
    assert aws.list_objects_v2(Bucket='agml-test', Prefix=prefix)['KeyCount'] == 0
    assert download(server, sha)[2] == b'keep me'
    assert aws.list_objects_v2(Bucket='agml-test')['KeyCount'] == 1


@pytest.mark.parametrize('n', [None, -1, -5, 0, 2, 20])
def test_stream_handles_short_reads(server, n):
    import io
    from agenticml.server.objectstore import _S3Stream
    class ShortBody(io.BytesIO):
        def read(self, amount=None):
            assert amount is None or amount >= 0
            return super().read(min(amount, 2) if amount is not None else 2)
    stream = _S3Stream(server.objects, {'Body': ShortBody(b'abcdef'), 'ContentLength': 6})
    expected = b'abcdef' if n is None or n < 0 else b'abcdef'[:n]
    assert stream.read(n) == expected
    stream.close()


def streaming_errors():
    from botocore.exceptions import IncompleteReadError, ResponseStreamingError, ConnectionClosedError
    from urllib3.exceptions import ProtocolError
    return [IncompleteReadError(actual_bytes=1, expected_bytes=10),
            ResponseStreamingError(error='broken'), ConnectionClosedError(endpoint_url='private'),
            ProtocolError('broken')]


@pytest.mark.parametrize('error', streaming_errors())
def test_stream_failure_mapping(server, error):
    from agenticml.server.objectstore import _S3Stream, ObjectStoreUnavailable
    class BrokenBody:
        def read(self, n):
            raise error
    stream = _S3Stream(server.objects, {'Body': BrokenBody(), 'ContentLength': 10})
    with pytest.raises(ObjectStoreUnavailable):
        stream.read(10)
    with pytest.raises(ObjectStoreUnavailable):
        server.objects._call(lambda: (_ for _ in ()).throw(error))


@pytest.mark.parametrize('path', ['/api/objects/', '/api/w/default/objects/'])
def test_midstream_failure_closes_without_second_response(server, monkeypatch, path):
    import socket
    from botocore.exceptions import ResponseStreamingError
    sha = 'a' * 64
    class BrokenBody:
        closed = False
        calls = 0
        def read(self, n):
            self.calls += 1
            if self.calls == 1:
                return b'x' * n
            raise ResponseStreamingError(error='private AWS text')
        def close(self):
            self.closed = True
    body = BrokenBody()
    if path == '/api/objects/':
        # This URL belongs to the local dashboard; server mode requires /api/w/<slug>/.
        monkeypatch.setattr(server, 'auth', None)
        monkeypatch.setattr(ui, '_ctx_open_object', lambda handler:
                            lambda sha: server.objects.open(server.default_workspace_id, sha))
    monkeypatch.setattr(server.objects.client, 'get_object', lambda **kw: {
        'Body': body, 'ContentLength': ui._CHUNK + 1})
    with socket.create_connection(server.server_address[:2], timeout=5) as sock:
        sock.sendall((f'GET {path}{sha} HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                      f'Cookie: {server.test_cookie}\r\nConnection: close\r\n\r\n').encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    wire = b''.join(chunks)
    head, payload = wire.split(b'\r\n\r\n', 1)
    assert head.startswith(b'HTTP/1.0 200')
    assert wire.count(b'HTTP/1.0 ') == 1
    assert payload == b'x' * ui._CHUNK
    assert body.closed


@pytest.mark.parametrize('command', ['status', 'verify'])
def test_cli_operator_objects_option(server, capsys, command):
    from agenticml import cli
    assert cli.main(['server', command, '--data', str(server.data_dir),
                     '--objects', 's3://agml-test/pfx']) == 0
    assert 'objects are in S3; object checks come in package 23' in capsys.readouterr().out


def test_cli_serve_objects_reaches_make_server(tmp_path, monkeypatch):
    from agenticml import cli
    from agenticml.server import app
    calls = []
    def make_server(*args, **kwargs):
        calls.append(kwargs)
        raise KeyboardInterrupt
    monkeypatch.setattr(app, 'make_server', make_server)
    assert cli.main(['server', 'serve', '--data', str(tmp_path),
                     '--objects', 's3://agml-test/pfx']) == 0
    assert calls[0]['objects'] == 's3://agml-test/pfx'


@pytest.mark.parametrize('failed', [False, True])
def test_cli_delete_objects_option_and_warning(server, aws, monkeypatch, capsys, failed):
    from agenticml import cli
    from agenticml.server.objectstore import ObjectStoreUnavailable
    server.users.add('delete-owner', 'admin')
    row = WorkspaceRepo(server.db).create('delete-me', 'Delete me', 'delete-owner')
    prepare_store(server.data_dir, row['id'])
    prefix = f"pfx/{row['id']}/"
    aws.put_object(Bucket='agml-test', Key=prefix + 'objects/example', Body=b'bytes')
    if failed:
        def fail(self, wid):
            assert wid == row['id']
            raise ObjectStoreUnavailable()
        monkeypatch.setattr(S3Objects, 'delete_workspace', fail)
    assert cli.main(['server', 'workspaces', 'delete', 'delete-me', '--data', str(server.data_dir),
                     '--yes-delete', 'delete-me', '--objects', 's3://agml-test/pfx']) == 0
    assert WorkspaceRepo(server.db).get('delete-me') is None
    assert aws.list_objects_v2(Bucket='agml-test', Prefix=prefix)['KeyCount'] == int(failed)
    err = capsys.readouterr().err
    if failed:
        assert 'Warning: object cleanup failed for workspace ' + row['id'] in err


def test_ingest_unavailable_after_headers_only_closes(server, monkeypatch):
    import socket
    from agenticml.store.errors import ObjectStoreUnavailable
    def fail_after_headers(self, body):
        self._begin(200, 'application/json', 100)
        self.wfile.write(b'{')
        raise ObjectStoreUnavailable()
    monkeypatch.setattr(ingest.IngestHandler, '_missing', fail_after_headers)
    with socket.create_connection(server.server_address[:2], timeout=5) as sock:
        sock.sendall((f'POST /v2/objects/missing HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                      f'Authorization: Bearer {server.test_key}\r\nContent-Length: 2\r\n\r\n{{}}').encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    head, body = b''.join(chunks).split(b'\r\n\r\n', 1)
    assert head.startswith(b'HTTP/1.0 200')
    assert body == b'{'


def test_up_explicitly_uses_files(tmp_path, monkeypatch):
    from agenticml.server import up
    calls = []
    monkeypatch.setenv('AGENTICML_SERVER_OBJECTS', 's3://agml-test/pfx')
    monkeypatch.setattr(up, 'check_tailscale', lambda: 'https://machine.ts.net')
    monkeypatch.setattr(up, 'bootstrap_admin', lambda *a, **kw: tmp_path)
    monkeypatch.setattr(up, 'serve', lambda *a, **kw: calls.append(kw))
    up.up(tmp_path)
    assert calls[0]['objects'] == 'file://'


def test_local_ui_requests_do_not_import_server(tmp_path):
    import subprocess
    code = '''
import http.client
import sys
import threading
from agenticml.ui.server import make_server
with make_server(sys.argv[1], port=0) as server:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
        conn.request('GET', '/api/runs')
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        conn.close()
        assert not any(k == 'agenticml.server' or k.startswith('agenticml.server.') for k in sys.modules)
    finally:
        server.shutdown()
        thread.join()
'''
    subprocess.run([sys.executable, '-c', code, str(tmp_path)], check=True, timeout=15)

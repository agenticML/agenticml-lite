"""30.2: workspace deletion never skips a step, and leftovers are found and retried."""
import json
import os

import pytest

from agenticml import cli
from agenticml.server import admin, app
from agenticml.server import workspaces as workspaces_module
from agenticml.server.db.repos import AuditRepo
from agenticml.server.db.workspaces import AccountRepo, WorkspaceRepo
from agenticml.server.keys import ClientError
from agenticml.server.objectstore import FileObjects, S3Objects, open_backend
from agenticml.server.workspaces import delete_workspace, discard_store, prepare_store
from agenticml.store.errors import ObjectStoreUnavailable
from test_server_ingest import running
from test_server_signin import running as running_with

S3_URL = 's3://agml-test/pfx'


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.delenv('AGENTICML_SERVER_OBJECTS', raising=False)
    with running(tmp_path / 'data') as httpd:
        httpd.users.add('root', 'admin')
        httpd.users.add('bob', 'admin')
        repo = WorkspaceRepo(httpd.db)
        httpd.beta = repo.create('beta', owner='bob')
        prepare_store(httpd.data_dir, httpd.beta['id'])
        yield httpd


@pytest.fixture
def aws(monkeypatch):
    boto3 = pytest.importorskip('boto3')
    moto = pytest.importorskip('moto')
    for name, value in [('AWS_ACCESS_KEY_ID', 'testing'), ('AWS_SECRET_ACCESS_KEY', 'testing'),
                        ('AWS_DEFAULT_REGION', 'us-east-1')]:
        monkeypatch.setenv(name, value)
    with moto.mock_aws():
        client = boto3.client('s3')
        client.create_bucket(Bucket='agml-test')
        yield client


def events(server, workspace=None):
    return AuditRepo(server.db, workspace).tail(1000)


def actions(server):
    return [e['action'] for e in events(server)]


def delete_cli(server, slug, *extra):
    return cli.main(['server', 'workspaces', 'delete', slug, '--yes-delete', slug,
                     '--data', str(server.data_dir), *extra])


def cli_run(server, *argv):
    return cli.main(['server', 'workspaces', *argv, '--data', str(server.data_dir)])


def workspace_dir(server, workspace):
    return server.data_dir / 'workspaces' / workspace['id']


class Recorder:
    def __init__(self, order, fail=False):
        self.order, self.fail, self.calls = order, fail, []

    def delete_workspace(self, wid):
        self.order.append('objects')
        self.calls.append(wid)
        if self.fail:
            raise ObjectStoreUnavailable()


# --- discard_store ----------------------------------------------------------

def test_discard_store_reports_leftovers(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    wid = 'a' * 32
    assert discard_store(data, wid) is True  # no workspaces directory at all
    (data / 'workspaces' / wid / 'store').mkdir(parents=True)
    (data / 'workspaces' / wid / 'store' / 'file').write_text('x')
    assert discard_store(data, wid) is True
    assert not (data / 'workspaces' / wid).exists()
    assert discard_store(data, wid) is True  # already missing
    monkeypatch.setattr(workspaces_module, '_rmtree_at', lambda parent_fd, name: None)
    (data / 'workspaces' / wid / 'store').mkdir(parents=True)
    assert discard_store(data, wid) is False
    assert (data / 'workspaces' / wid).is_dir()


def test_discard_store_false_when_safe_path_is_skipped(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    wid = 'b' * 32
    (data / 'workspaces' / wid).mkdir(parents=True)
    monkeypatch.setattr(workspaces_module.shutil.rmtree, 'avoids_symlink_attacks', False, raising=False)
    assert discard_store(data, wid) is False
    assert (data / 'workspaces' / wid).is_dir()


# --- delete_workspace --------------------------------------------------------

@pytest.mark.parametrize('mode', ['raises', 'false'])
def test_delete_workspace_files_failure_still_cleans_objects_and_audits(server, monkeypatch, mode):
    order = []

    def discard(data, wid):
        order.append('files')
        if mode == 'raises':
            raise OSError('EFS error')
        return False

    monkeypatch.setattr(workspaces_module, 'discard_store', discard)
    fake = Recorder(order)
    seen = []

    def on_deleted(row):
        order.append('on_deleted')
        seen.append(row['slug'])

    row = delete_workspace(server.db, server.data_dir, 'beta', objects=fake, on_deleted=on_deleted)
    assert WorkspaceRepo(server.db).get('beta') is None
    assert order == ['on_deleted', 'files', 'objects']
    assert seen == ['beta'] and fake.calls == [server.beta['id']]
    assert row['files_cleanup_failed'] is True and not row.get('object_cleanup_failed')


def test_delete_workspace_objects_failure_does_not_skip_files(server):
    order = []
    row = delete_workspace(server.db, server.data_dir, 'beta', objects=Recorder(order, fail=True),
                           on_deleted=lambda r: order.append('on_deleted'))
    assert row['object_cleanup_failed'] is True and row['files_cleanup_failed'] is False
    assert order == ['on_deleted', 'objects']
    assert not workspace_dir(server, server.beta).exists()
    assert WorkspaceRepo(server.db).get('beta') is None


def test_delete_workspace_callback_failure_never_blocks_cleanup(server):
    def boom(row):
        raise RuntimeError('audit down')

    row = delete_workspace(server.db, server.data_dir, 'beta', on_deleted=boom)
    assert row['files_cleanup_failed'] is False
    assert not workspace_dir(server, server.beta).exists()


def test_delete_workspace_missing_directory_is_fine(server):
    import shutil
    shutil.rmtree(workspace_dir(server, server.beta))
    row = delete_workspace(server.db, server.data_dir, 'beta')
    assert row['files_cleanup_failed'] is False
    assert not (server.data_dir / 'workspaces' / server.beta['id']).exists()


def test_delete_workspace_symlinked_store_is_unlinked_not_followed(server, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('keep')
    import shutil
    shutil.rmtree(workspace_dir(server, server.beta))
    workspace_dir(server, server.beta).symlink_to(outside, target_is_directory=True)
    row = delete_workspace(server.db, server.data_dir, 'beta')
    assert row['files_cleanup_failed'] is False
    assert WorkspaceRepo(server.db).get('beta') is None
    assert not os.path.lexists(workspace_dir(server, server.beta))
    assert (outside / 'secret').read_text() == 'keep'


def test_delete_default_workspace_is_refused_before_anything_happens(server):
    seen = []
    default = WorkspaceRepo(server.db).require('default')
    with pytest.raises(ClientError, match='cannot delete the default workspace'):
        delete_workspace(server.db, server.data_dir, 'default', on_deleted=seen.append)
    assert seen == [] and (server.data_dir / 'workspaces' / default['id']).is_dir()
    assert WorkspaceRepo(server.db).get('default') is not None


def test_cli_delete_files_failure_audits_and_exits_zero(server, monkeypatch, capsys):
    monkeypatch.setattr(workspaces_module, 'discard_store', lambda data, wid: False)
    assert delete_cli(server, 'beta') == 0
    err = capsys.readouterr().err
    wid = server.beta['id']
    assert f'Warning: file cleanup failed for workspace {wid}; retry with: agenticml server workspaces cleanup {wid}' in err
    assert 'object cleanup' not in err
    assert {'workspace.delete', 'workspace.cleanup_failed'} <= set(actions(server))
    failed = [e for e in events(server) if e['action'] == 'workspace.cleanup_failed'][0]
    assert failed['target'] == wid and failed['detail'] == {'files': True, 'objects': False}


def test_cli_delete_success_has_no_warning_and_one_event(server, capsys):
    assert delete_cli(server, 'beta') == 0
    assert capsys.readouterr().err == ''
    assert actions(server).count('workspace.delete') == 1
    assert 'workspace.cleanup_failed' not in actions(server)


# --- orphans and cleanup (file backend) --------------------------------------

def test_cleanup_files_orphan_dir(server, monkeypatch, capsys):
    real = workspaces_module.discard_store
    monkeypatch.setattr(workspaces_module, 'discard_store', lambda data, wid: False)
    assert delete_cli(server, 'beta') == 0
    capsys.readouterr()
    wid = server.beta['id']
    assert workspace_dir(server, server.beta).is_dir()
    assert cli_run(server, 'orphans', '--json') == 0
    assert json.loads(capsys.readouterr().out) == {'files': [wid], 'objects': []}
    assert cli_run(server, 'orphans') == 0
    assert wid in capsys.readouterr().out
    monkeypatch.setattr(workspaces_module, 'discard_store', real)
    assert cli_run(server, 'cleanup', wid) == 0
    out = capsys.readouterr().out
    assert out.strip() == f'cleaned {wid} (files)' and 'objects' not in out
    assert not workspace_dir(server, server.beta).exists()
    assert 'workspace.cleanup' in actions(server)
    assert cli_run(server, 'orphans', '--json') == 0
    assert json.loads(capsys.readouterr().out) == {'files': [], 'objects': []}


def test_cleanup_refusals_touch_nothing(server, capsys):
    live = server.beta['id']
    assert cli_run(server, 'cleanup', live) == 1
    assert 'workspace still exists' in capsys.readouterr().err
    assert workspace_dir(server, server.beta).is_dir()
    for bad in ('../x', 'abc', 'A' * 32, 'g' * 32):
        assert cli_run(server, 'cleanup', bad) == 1
        assert capsys.readouterr().err.startswith('agenticml server: ')
    assert cli_run(server, 'cleanup', '--all') == 1
    assert '--yes-delete all' in capsys.readouterr().err
    assert cli_run(server, 'cleanup', '--all', '--yes-delete', 'nope') == 1
    capsys.readouterr()
    assert cli_run(server, 'cleanup') == 1
    assert cli_run(server, 'cleanup', 'a' * 32, '--all', '--yes-delete', 'all') == 1
    capsys.readouterr()
    assert workspace_dir(server, server.beta).is_dir()
    assert WorkspaceRepo(server.db).get('beta') is not None


def test_cleanup_all_removes_only_orphans(server, capsys):
    stray = server.data_dir / 'workspaces' / ('d' * 32) / 'store'
    stray.mkdir(parents=True)
    (server.data_dir / 'workspaces' / 'not-an-id').mkdir()
    (server.data_dir / 'workspaces' / ('e' * 32)).write_text('a file, not a workspace directory')
    assert cli_run(server, 'cleanup', '--all', '--yes-delete', 'all') == 0
    capsys.readouterr()
    assert not stray.parent.exists()
    assert (server.data_dir / 'workspaces' / 'not-an-id').is_dir()
    assert (server.data_dir / 'workspaces' / ('e' * 32)).is_file()
    assert workspace_dir(server, server.beta).is_dir()
    default = WorkspaceRepo(server.db).require('default')
    assert (server.data_dir / 'workspaces' / default['id']).is_dir()


def test_cleanup_all_rechecks_the_database_before_each_id(server, monkeypatch, capsys):
    stray = server.data_dir / 'workspaces' / ('d' * 32)
    (stray / 'store').mkdir(parents=True)
    real = admin.orphan_workspaces

    def listed_then_created(data, db, backend=None):
        found = real(data, db, backend)
        # The id becomes a real workspace after the scan but before deletion.
        with db.transaction(write=True) as tx:
            tx.cross_workspace('test')
            tx.execute('INSERT INTO workspaces (id, slug, name, status, created_at, plan, personal) '
                       "VALUES (?, 'late', 'late', 'active', '2026-01-01T00:00:00.000000Z', 'free', 0)",
                       ('d' * 32,))
        return found

    monkeypatch.setattr(admin, 'orphan_workspaces', listed_then_created)
    assert cli_run(server, 'cleanup', '--all', '--yes-delete', 'all') == 0
    assert 'skipped' in capsys.readouterr().err
    assert (stray / 'store').is_dir()


def test_cleanup_reports_failure_with_exit_one(server, monkeypatch, capsys):
    stray = server.data_dir / 'workspaces' / ('d' * 32)
    stray.mkdir(parents=True)
    monkeypatch.setattr(workspaces_module, 'discard_store', lambda data, wid: False)
    assert cli_run(server, 'cleanup', 'd' * 32) == 1
    assert 'retry with: agenticml server workspaces cleanup ' + 'd' * 32 in capsys.readouterr().err
    assert stray.is_dir()


# --- verify and status warnings ----------------------------------------------

def bare_data(tmp_path):
    data = tmp_path / 'data'
    app.prepare_data_dir(data)
    db = admin.for_server_dir(data / 'server')
    prepare_store(data, WorkspaceRepo(db).require('default')['id'])
    return data, db


def test_verify_and_status_report_workspace_orphans_as_warnings(tmp_path):
    data, _ = bare_data(tmp_path)
    stray = 'c' * 32
    (data / 'workspaces' / stray / 'store').mkdir(parents=True)
    (data / 'workspaces' / 'not-hex').mkdir()
    report = admin.verify(data)
    assert report['ok'] is True and report['problems'] == []
    [warning] = [w for w in report['warnings'] if w['kind'] == 'workspace_orphan_files']
    assert warning['path'] == f'workspaces/{stray}'
    assert 'workspaces cleanup ' + stray in warning['detail']
    assert not any('not-hex' in json.dumps(w) for w in report['warnings'])
    status = admin.status(data)
    assert any(stray in w and 'workspaces cleanup' in w for w in status['warnings'])
    assert not any('not-hex' in w for w in status['warnings'])
    # A single-workspace report does not scan for orphans.
    assert not [w for w in admin.verify(data, workspace='default')['warnings']
                if w['kind'].startswith('workspace_orphan')]


def test_orphan_scan_lists_before_it_reads_the_database(tmp_path, monkeypatch):
    """A workspace created right after the snapshot must not look like an orphan."""
    data, db = bare_data(tmp_path)
    AccountRepo(db).create('someone')
    real = WorkspaceRepo.list
    created = {}

    def list_then_create(self):
        rows = real(self)
        if not created:
            row = WorkspaceRepo(self.db).create('fresh', owner='someone')
            prepare_store(data, row['id'])
            created.update(row)
        return rows

    monkeypatch.setattr(WorkspaceRepo, 'list', list_then_create)
    found = admin.orphan_workspaces(data, db)
    assert created and found == {'files': [], 'objects': []}
    monkeypatch.undo()
    assert admin.orphan_workspaces(data, db) == {'files': [], 'objects': []}


# --- S3 ----------------------------------------------------------------------

def put_object(aws, wid, name='objects/aa/blob', prefix='pfx'):
    aws.put_object(Bucket='agml-test', Key=f'{prefix}/{wid}/{name}' if prefix else f'{wid}/{name}', Body=b'bytes')


def keys_under(aws, prefix):
    page = aws.list_objects_v2(Bucket='agml-test', Prefix=prefix)
    return [item['Key'] for item in page.get('Contents', [])]


def test_workspace_ids_pages_and_ignores_foreign_names(aws, tmp_path):
    ids = [f'{n:032x}' for n in range(1, 6)]
    for wid in ids:
        put_object(aws, wid)
    aws.put_object(Bucket='agml-test', Key='pfx/not-an-id/x', Body=b'x')
    aws.put_object(Bucket='agml-test', Key='pfx/' + 'A' * 32 + '/x', Body=b'x')
    aws.put_object(Bucket='agml-test', Key='pfx/' + 'a' * 31 + '/x', Body=b'x')
    aws.put_object(Bucket='agml-test', Key='pfx/' + 'b' * 32, Body=b'loose key, not a prefix')
    aws.put_object(Bucket='agml-test', Key='other/' + 'c' * 32 + '/x', Body=b'x')
    aws.put_object(Bucket='agml-test', Key='top-level', Body=b'x')
    backend = open_backend(S3_URL, tmp_path)
    original = backend.client.list_objects_v2
    pages = []

    def small_pages(**kwargs):
        pages.append(kwargs.get('ContinuationToken'))
        return original(MaxKeys=2, **kwargs)

    backend.client.list_objects_v2 = small_pages
    assert backend.workspace_ids() == ids
    assert len(pages) > 1
    assert FileObjects(tmp_path).workspace_ids() == []


def test_workspace_ids_with_empty_base_prefix(aws, tmp_path):
    put_object(aws, 'a' * 32, prefix='')
    aws.put_object(Bucket='agml-test', Key='loose', Body=b'x')
    assert open_backend('s3://agml-test', tmp_path).workspace_ids() == ['a' * 32]


@pytest.fixture
def s3_server(tmp_path, aws, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_KEY', raising=False)
    monkeypatch.delenv('AGENTICML_SERVER_OBJECTS', raising=False)
    with running_with(tmp_path / 'data', objects=S3_URL) as httpd:
        httpd.users.add('root', 'admin')
        httpd.users.add('own', 'admin')
        repo = WorkspaceRepo(httpd.db)
        httpd.a = repo.create('aaa', owner='own')
        AccountRepo(httpd.db).create('own2')
        httpd.b = repo.create('bbb', owner='own2')
        for row in (httpd.a, httpd.b):
            prepare_store(httpd.data_dir, row['id'])
            put_object(aws, row['id'])
        httpd.default_id = repo.require('default')['id']
        put_object(aws, httpd.default_id)
        yield httpd


def test_failed_cleanup_can_be_retried_and_removes_only_that_prefix(s3_server, aws, monkeypatch, capsys):
    server = s3_server
    real = S3Objects.delete_workspace

    def fail(self, wid):
        raise ObjectStoreUnavailable()

    monkeypatch.setattr(S3Objects, 'delete_workspace', fail)
    assert delete_cli(server, 'aaa', '--objects', S3_URL) == 0
    err = capsys.readouterr().err
    wid = server.a['id']
    assert f'Warning: object cleanup failed for workspace {wid}; retry with: agenticml server workspaces cleanup {wid}' in err
    assert 'file cleanup' not in err
    assert {'workspace.delete', 'workspace.cleanup_failed'} <= set(actions(server))
    assert keys_under(aws, f'pfx/{wid}/') and keys_under(aws, f'pfx/{server.b["id"]}/')
    assert not workspace_dir(server, server.a).exists()
    assert cli_run(server, 'orphans', '--json', '--objects', S3_URL) == 0
    assert json.loads(capsys.readouterr().out) == {'files': [], 'objects': [wid]}
    monkeypatch.setattr(S3Objects, 'delete_workspace', real)
    assert cli_run(server, 'cleanup', wid, '--objects', S3_URL) == 0
    assert capsys.readouterr().out.strip() == f'cleaned {wid} (files and objects)'
    assert keys_under(aws, f'pfx/{wid}/') == []
    assert keys_under(aws, f'pfx/{server.b["id"]}/') and keys_under(aws, f'pfx/{server.default_id}/')
    assert 'workspace.cleanup' in actions(server)
    assert cli_run(server, 'orphans', '--json', '--objects', S3_URL) == 0
    assert json.loads(capsys.readouterr().out) == {'files': [], 'objects': []}


def test_cleanup_refuses_live_workspace_and_all_needs_confirmation_s3(s3_server, aws, capsys):
    server = s3_server
    live = server.b['id']
    assert cli_run(server, 'cleanup', live, '--objects', S3_URL) == 1
    assert 'workspace still exists' in capsys.readouterr().err
    assert keys_under(aws, f'pfx/{live}/')
    assert cli_run(server, 'cleanup', '--all', '--objects', S3_URL) == 1
    capsys.readouterr()
    orphan = 'f' * 32
    put_object(aws, orphan)
    (server.data_dir / 'workspaces' / orphan).mkdir()
    assert cli_run(server, 'cleanup', '--all', '--yes-delete', 'all', '--objects', S3_URL) == 0
    capsys.readouterr()
    assert keys_under(aws, f'pfx/{orphan}/') == [] and not (server.data_dir / 'workspaces' / orphan).exists()
    for wid in (server.a['id'], server.b['id'], server.default_id):
        assert keys_under(aws, f'pfx/{wid}/')


def test_cleanup_reports_object_failure_and_exits_one(s3_server, aws, monkeypatch, capsys):
    orphan = 'f' * 32
    put_object(aws, orphan)

    def fail(self, wid):
        raise ObjectStoreUnavailable()

    monkeypatch.setattr(S3Objects, 'delete_workspace', fail)
    assert cli_run(s3_server, 'cleanup', orphan, '--objects', S3_URL) == 1
    assert 'objects failed' in capsys.readouterr().err
    assert keys_under(aws, f'pfx/{orphan}/')


def test_verify_and_status_warn_about_orphan_prefixes_and_survive_listing_failures(s3_server, aws, monkeypatch):
    data = s3_server.data_dir
    stray = 'f' * 32
    put_object(aws, stray)
    (data / 'workspaces' / stray).mkdir()
    report = admin.verify(data, objects=S3_URL)
    assert report['ok'] is True
    kinds = {(w['kind'], w['path']) for w in report['warnings'] if w['kind'].startswith('workspace_orphan')}
    assert kinds == {('workspace_orphan_files', f'workspaces/{stray}'),
                     ('workspace_orphan_objects', f'workspaces/{stray}')}
    status = admin.status(data, objects=S3_URL)
    assert sum(stray in w for w in status['warnings']) == 2

    def fail(self):
        raise ObjectStoreUnavailable()

    monkeypatch.setattr(S3Objects, 'workspace_ids', fail)
    report = admin.verify(data, objects=S3_URL)
    assert report['ok'] is True
    assert any(w['kind'] == 'workspace_orphan_scan' for w in report['warnings'])
    assert any(w['kind'] == 'workspace_orphan_files' for w in report['warnings'])
    status = admin.status(data, objects=S3_URL)
    assert any('could not list object store prefixes' in w for w in status['warnings'])
    with pytest.raises(ObjectStoreUnavailable):
        admin.orphan_workspaces(data, s3_server.db, open_backend(S3_URL, data))
    assert cli_run(s3_server, 'orphans', '--objects', S3_URL) == 1


def test_delete_s3_failure_does_not_skip_files(s3_server, monkeypatch):
    def fail(self, wid):
        raise ObjectStoreUnavailable()

    monkeypatch.setattr(S3Objects, 'delete_workspace', fail)
    row = delete_workspace(s3_server.db, s3_server.data_dir, 'aaa', objects=s3_server.objects)
    assert row['object_cleanup_failed'] is True
    assert not workspace_dir(s3_server, s3_server.a).exists()
    assert WorkspaceRepo(s3_server.db).get('aaa') is None


def test_orphans_and_cleanup_never_create_a_database(tmp_path, aws, capsys):
    """A mistyped --data must not turn into an empty database that makes live prefixes orphans."""
    live = 'a' * 32
    put_object(aws, live)
    empty = tmp_path / 'typo'
    for argv in (['orphans', '--objects', S3_URL], ['cleanup', live, '--objects', S3_URL],
                 ['cleanup', '--all', '--yes-delete', 'all', '--objects', S3_URL]):
        assert cli.main(['server', 'workspaces', *argv, '--data', str(empty)]) == 1
        assert 'no server database found' in capsys.readouterr().err
    assert not empty.exists()
    assert keys_under(aws, f'pfx/{live}/')
    missing = tmp_path / 'data2'
    (missing / 'server').mkdir(parents=True)
    assert cli.main(['server', 'workspaces', 'orphans', '--data', str(missing),
                     '--database', 'sqlite:///' + str(missing / 'server' / 'nope.db')]) == 1
    assert not (missing / 'server' / 'nope.db').exists()


def test_cleanup_refuses_a_database_without_workspaces(tmp_path, aws, capsys):
    data = tmp_path / 'data'
    app.prepare_data_dir(data)
    db = admin.for_server_dir(data / 'server')
    with db.transaction(write=True) as tx:
        tx.cross_workspace('test')
        tx.execute('DELETE FROM workspaces')
    live = 'b' * 32
    put_object(aws, live)
    (data / 'workspaces' / live).mkdir(parents=True)
    assert cli.main(['server', 'workspaces', 'cleanup', '--all', '--yes-delete', 'all',
                     '--objects', S3_URL, '--data', str(data)]) == 1
    assert 'no workspaces' in capsys.readouterr().err
    assert cli.main(['server', 'workspaces', 'cleanup', live, '--objects', S3_URL, '--data', str(data)]) == 1
    capsys.readouterr()
    assert keys_under(aws, f'pfx/{live}/') and (data / 'workspaces' / live).is_dir()

"""server backup: a copy taken while a client syncs restores to a working server."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading

import pytest

from agenticml import cli, lineage, sync
from agenticml.client import Client
from agenticml.server import admin, backup
from agenticml.server.db.repos import RunRepo
from agenticml.store import RunReader
from server_db_helpers import default_workspace_id, on_postgres
from test_server_objects_s3 import aws, server as s3_server  # noqa: F401  (fixtures)
from test_server_signin import running
from test_sync import allowed, remote, writer_at  # noqa: F401  (remote is a fixture)


def files_only():
    """Postgres backups need --files-only (the dump is taken separately)."""
    return {'files_only': True} if on_postgres() else {}


def cli_flags():
    return ['--files-only'] if on_postgres() else []


def push_all(url, key, writers, *, complete=True):
    with Client(url, key, timeout=5, max_retries=0) as client:
        for writer in writers:
            result = sync.SyncResult(writer.run_id)
            sync.sync_run(RunReader(writer.root, writer.run_id), url, client, result)
            assert not result.failed and (result.complete or not complete), (writer.run_id, result)


def modes(root):
    for path in root.rglob('*'):
        if not path.is_symlink():
            mode = stat.S_IMODE(path.lstat().st_mode)
            yield path, mode


@pytest.mark.sqlite_only
def test_backup_during_sync_restores_to_a_working_server(remote, tmp_path, monkeypatch):
    server, client = remote
    url = 'http://127.0.0.1:%d' % server.server_address[1]
    writers = [writer_at(tmp_path, run_id=f'run-{i}', status=None, count=40) for i in range(3)]
    push_all(url, client.api_key, writers, complete=False)
    started, errors = threading.Event(), []

    def appender():
        try:
            with Client(url, client.api_key, timeout=10, max_retries=0) as live:
                for round_ in range(1, 25):
                    for writer in writers:
                        writer.write_scalars([(1000 * round_ + i, {'loss': float(i)}) for i in range(40)])
                    if round_ == 3:
                        artifact = tmp_path / 'weights.bin'
                        artifact.write_bytes(b'late weights')
                        writers[0].add_artifact(artifact, 'weights', 'model', {}, copy=True)
                    for writer in writers:
                        result = sync.SyncResult(writer.run_id)
                        sync.sync_run(RunReader(writer.root, writer.run_id), url, live, result)
                        assert not result.failed, result
                    started.set()
        except BaseException as exc:  # reported after the join
            errors.append(exc)
            started.set()

    thread = threading.Thread(target=appender)
    thread.start()
    assert started.wait(30)
    target = tmp_path / 'backup'
    report = backup.backup(target, data=server.data_dir, **files_only())
    thread.join(60)
    assert not thread.is_alive() and not errors, errors
    assert report['database'] == 'sqlite' and report['counts']['runs'] == 3
    assert json.loads((target / 'BACKUP.json').read_text()) == report
    assert 'postgres' not in json.dumps(report) and str(server.data_dir) not in json.dumps(report)
    assert all(mode == (0o700 if path.is_dir() else 0o600) for path, mode in modes(target))
    # The source keeps its sync.json for the old URL; finish and send everything again later.
    for writer in writers:
        writer.finish('finished')
    monkeypatch.setenv('AGENTICML_CONFIG_DIR', str(tmp_path / 'config'))
    with running(target) as restored:
        restored_url = 'http://127.0.0.1:%d' % restored.server_address[1]
        wid = default_workspace_id(restored.db)
        assert {row['run_id'] for row in RunRepo(restored.db, wid).all_rows()} == {'run-0', 'run-1', 'run-2'}
        check = admin.verify(target)
        assert check['ok'] and not check['problems'], check
        push_all(restored_url, client.api_key, writers)
        for writer in writers:
            restored_run = restored.root / 'runs' / writer.run_id
            assert allowed(restored_run) == allowed(writer.run_dir)
        assert admin.verify(target)['ok']
    # The source server is unaffected.
    assert admin.verify(server.data_dir)['ok']


def test_backup_refusals_write_nothing(remote, tmp_path):
    server, _ = remote
    data = server.data_dir
    full = tmp_path / 'full'
    full.mkdir()
    (full / 'keep').write_text('x')
    with pytest.raises(backup.BackupError, match='not empty'):
        backup.backup(full, data=data, **files_only())
    assert [p.name for p in full.iterdir()] == ['keep']
    with pytest.raises(backup.BackupError, match='inside the data directory'):
        backup.backup(data / 'inside', data=data, **files_only())
    assert not (data / 'inside').exists()
    with pytest.raises(backup.BackupError, match='inside the data directory'):
        backup.backup(data / 'workspaces' / 'deeper' / 'still', data=data, **files_only())
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real)
    with pytest.raises(backup.BackupError, match='symlink'):
        backup.backup(link, data=data, **files_only())
    assert list(real.iterdir()) == []
    afile = tmp_path / 'file'
    afile.write_text('x')
    with pytest.raises(backup.BackupError, match='not a directory'):
        backup.backup(afile, data=data, **files_only())
    with pytest.raises(backup.BackupError, match='contain the data'):
        backup.backup(tmp_path, data=data, **files_only())


def test_postgres_without_files_only_is_refused_with_the_dump_command(tmp_path, capsys, monkeypatch):
    url = 'postgresql://backup_user:s3cret-pw@127.0.0.1:1/db'
    monkeypatch.setenv('AGENTICML_SERVER_DATABASE_URL', url)
    data = tmp_path / 'data'
    target = tmp_path / 'bk'
    code = cli.main(['server', 'backup', '--data', str(data), '--to', str(target)])
    err = capsys.readouterr().err
    assert code == 1
    assert 'pg_dump --format=custom "$DATABASE_URL" > ' + str(target / 'server.dump') in err
    assert '--files-only' in err and 'RDS automated snapshots' in err
    assert 's3cret-pw' not in err and url not in err
    assert not target.exists() and not data.exists()


@pytest.mark.skipif(not on_postgres(), reason='needs AGENTICML_TEST_DATABASE_URL')
def test_postgres_files_only_copies_files_and_prints_the_note(remote, tmp_path, capsys, monkeypatch):
    server, client = remote
    writer = writer_at(tmp_path, run_id='pg-run', status='finished', rich=True)
    push_all('http://127.0.0.1:%d' % server.server_address[1], client.api_key, [writer])
    target = tmp_path / 'bk'
    code = cli.main(['server', 'backup', '--data', str(server.data_dir), '--to', str(target), '--files-only'])
    out = capsys.readouterr()
    password = 'agenticml-test-password'
    assert code == 0 and 'pg_dump --format=custom "$DATABASE_URL"' in out.out
    assert password not in out.out + out.err and os.environ['AGENTICML_SERVER_DATABASE_URL'] not in out.out
    report = json.loads((target / 'BACKUP.json').read_text())
    assert report['database'] == 'postgres' and report['files_only'] and report['counts']['runs'] == 1
    assert not (target / 'server' / 'agenticml.db').exists()
    restored = [p for p in (target / 'workspaces').glob('*/store/runs/pg-run')]
    assert len(restored) == 1 and allowed(restored[0]) == allowed(writer.run_dir)


def test_s3_objects_are_skipped_with_a_note(s3_server, tmp_path, capsys):
    server = s3_server
    from test_server_objects_s3 import put
    sha, response = put(server, b'blob in s3')
    assert response[0] == 200 or response[0] == 201 or response
    writer = writer_at(tmp_path, run_id='s3-run', status='finished', rich=True)
    url = 'http://127.0.0.1:%d' % server.server_address[1]
    push_all(url, server.test_key, [writer])
    target = tmp_path / 'bk'
    code = cli.main(['server', 'backup', '--data', str(server.data_dir), '--to', str(target),
                     '--objects', 's3://agml-test/pfx', *cli_flags()])
    out = capsys.readouterr().out
    assert code == 0 and 'objects are in S3 and were not copied' in out
    report = json.loads((target / 'BACKUP.json').read_text())
    assert report['counts']['objects'] == 0 and report['counts']['runs'] == 1
    assert not list((target / 'workspaces').glob('*/store/objects'))
    assert list((target / 'workspaces').glob('*/store/runs/s3-run/meta.json'))


def commit_in_bare_repo(repo, key):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    subprocess.run(['git', 'init', '--bare', '-q', str(repo)], check=True, env=env)
    tree = subprocess.run(['git', '--git-dir', str(repo), 'mktree'], input=b'', check=True, env=env,
                          capture_output=True).stdout.decode().strip()
    commit = subprocess.run(['git', '--git-dir', str(repo), 'commit-tree', tree, '-m', 'x'], check=True,
                            env=env, capture_output=True).stdout.decode().strip()
    subprocess.run(['git', '--git-dir', str(repo), 'update-ref', 'refs/runs/run-lin', commit], check=True, env=env)
    return commit


@pytest.mark.skipif(shutil.which('git') is None, reason='needs git')
def test_lineage_repository_is_copied_with_server_modes(remote, tmp_path):
    server, _ = remote
    repo = server.root / 'lineage' / (lineage.project_key('demo') + '.git')
    repo.parent.mkdir(exist_ok=True)
    commit = commit_in_bare_repo(repo, 'demo')
    target = tmp_path / 'bk'
    report = backup.backup(target, data=server.data_dir, **files_only())
    assert report['counts']['lineage'] == 1
    copy = next((target / 'workspaces').glob('*/store/lineage/*.git'))
    refs = subprocess.run(['git', '--git-dir', str(copy), 'for-each-ref', '--format=%(objectname) %(refname)'],
                          check=True, capture_output=True).stdout.decode()
    assert refs.strip() == f'{commit} refs/runs/run-lin'
    assert all(mode == (0o700 if path.is_dir() else 0o600) for path, mode in modes(copy))
    assert not (copy / 'hooks').exists() or not any((copy / 'hooks').iterdir())


def test_missing_run_and_short_file_become_warnings(remote, tmp_path, monkeypatch):
    server, client = remote
    url = 'http://127.0.0.1:%d' % server.server_address[1]
    writers = [writer_at(tmp_path, run_id=name, status='finished') for name in ('keep', 'gone', 'short')]
    push_all(url, client.api_key, writers)
    shutil.rmtree(server.root / 'runs' / 'gone')
    (server.root / 'runs' / 'short' / 'events.jsonl').write_bytes(b'')
    report = backup.backup(tmp_path / 'bk', data=server.data_dir, **files_only())
    assert any('run gone was deleted since the snapshot' in w for w in report['warnings'])
    assert any('run short: events.jsonl is shorter than its committed size' in w for w in report['warnings'])
    assert report['counts']['runs'] == 2


def test_disk_full_stops_with_where_and_removes_nothing(remote, tmp_path, monkeypatch, capsys):
    server, client = remote
    push_all('http://127.0.0.1:%d' % server.server_address[1], client.api_key,
             [writer_at(tmp_path, run_id='r1', status='finished', rich=True)])
    original = backup._Writer.put
    seen = []

    def full(self, path, chunks):
        if path.name == 'meta.json':
            raise OSError(28, 'No space left on device')
        seen.append(path)
        return original(self, path, chunks)

    monkeypatch.setattr(backup._Writer, 'put', full)
    target = tmp_path / 'bk'
    code = cli.main(['server', 'backup', '--data', str(server.data_dir), '--to', str(target), *cli_flags()])
    err = capsys.readouterr().err
    assert code == 1 and 'backup stopped at' in err and 'disk full' in err
    assert str(target) in err and 'nothing already written was removed' in err
    assert seen and all(path.exists() for path in seen)
    assert on_postgres() or (target / 'server' / 'agenticml.db').exists()
    assert not (target / 'BACKUP.json').exists()

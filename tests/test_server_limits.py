"""Usage accounting on both database backends; enforcement lands in 18c.1b."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from agenticml import cli
from agenticml.server.db import usage as usage_module
from agenticml.server.db import connect, for_server_dir
from agenticml.server.db import migrate
from agenticml.server.db.backfill import backfill_objects
from agenticml.server.db.repos import AuditRepo, RunRepo, SettingsRepo
from agenticml.server.db.usage import UsageRepo
from agenticml.server.db.workspaces import AccountRepo, WorkspaceRepo
from agenticml.server.workspaces import store_root
from agenticml.store import layout
from server_db_helpers import database_url, default_workspace_id
from test_server_ingest import server, running, request, append, put_meta  # noqa: F401 (fixture)
from test_server_lineage import snapshots, bundle  # noqa: F401 (fixture)


def test_migration_seeds_existing_files_and_default(tmp_path, monkeypatch):
    original = migrate.MIGRATIONS
    prior = tmp_path / 'migrations'
    prior.mkdir()
    for path in original.iterdir():
        if path.suffix in ('.py', '.sql') and path.name[:4].isdigit() and int(path.name[:4]) < 5:
            shutil.copy(path, prior / path.name)
    monkeypatch.setattr(migrate, 'MIGRATIONS', prior)
    db = connect(database_url(tmp_path / 'state.db'))
    wid = default_workspace_id(db)
    with db.transaction(write=True) as tx:
        tx.execute('INSERT INTO runs (workspace_id, run_id, list_row, updated_at) VALUES (?, ?, ?, ?)',
                   (wid, 'old', '{}', layout.format_timestamp()))
        for name, size in [('meta.json', 13), ('scalars/0.bin', 32)]:
            tx.execute('INSERT INTO run_files (workspace_id, run_id, path, size) VALUES (?, ?, ?, ?)',
                       (wid, 'old', name, size))
        tx.execute('INSERT INTO workspaces (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)',
                   ('b' * 32, 'empty', 'Empty', 'active', layout.format_timestamp()))
    monkeypatch.setattr(migrate, 'MIGRATIONS', original)
    migrate.migrate(db)
    assert UsageRepo(db, wid).get()['stored_bytes'] == 45
    assert UsageRepo(db, 'b' * 32).get()['stored_bytes'] == 0
    assert WorkspaceRepo(db).get('default')['plan'] == 'free'
    assert WorkspaceRepo(db).get('default')['personal'] == 0
    db.close()


def test_workspace_creation_and_cascade(server):
    account = AccountRepo(server.db).create('owner')
    row = WorkspaceRepo(server.db).create('second', owner=account['login'])
    usage = UsageRepo(server.db, row['id'])
    assert usage.get()['stored_bytes'] == 0
    with server.db.transaction(write=True) as tx:
        usage.record_object('a' * 64, 9, tx)
    WorkspaceRepo(server.db).delete('second')
    with server.db.transaction() as tx:
        assert tx.fetchone('SELECT 1 FROM workspace_usage WHERE workspace_id=?', (row['id'],)) is None
        assert tx.fetchone('SELECT 1 FROM objects WHERE workspace_id=?', (row['id'],)) is None


def test_backfill_once_and_resumable(server, tmp_path):
    db = server.db
    wid = default_workspace_id(db)
    usage = UsageRepo(db, wid)
    server.run_index.record_file('old', 'meta.json', 11)
    sha = hashlib.sha256(b'object').hexdigest()
    path = layout.object_path(server.root, sha)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'object')
    (path.parent / 'bad-name').write_bytes(b'bad')
    (path.parent / (sha[:2] + '0' * 62)).symlink_to(path)
    (path.parent / (sha[:2] + '1' * 62)).mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / ('ff' + '0' * 62)).write_bytes(b'outside')
    (server.root / 'objects' / 'ff').symlink_to(outside, target_is_directory=True)
    SettingsRepo(db).delete('objects_backfilled')
    backfill_objects(db, server.data_dir)
    assert usage.get()['stored_bytes'] == 17
    assert usage.recount() == 17
    backfill_objects(db, server.data_dir)
    assert usage.get()['stored_bytes'] == 17
    # Simulate a crash after a workspace commits, before the global flag.
    SettingsRepo(db).delete('objects_backfilled')
    backfill_objects(db, server.data_dir)
    assert usage.get()['stored_bytes'] == 17
    assert usage.get()['upload_bytes'] == 0


def test_backfill_runs_when_opening_existing_store(tmp_path):
    data = tmp_path / 'data'
    with running(data) as httpd:
        db = httpd.db
        wid = default_workspace_id(db)
        path = layout.object_path(store_root(data, wid), 'a' * 64)
        path.parent.mkdir(parents=True)
        path.write_bytes(b'old-object')
        SettingsRepo(db).delete('objects_backfilled')
    db.close()
    reopened = for_server_dir(data / 'server', create=True)
    assert UsageRepo(reopened, wid).get()['stored_bytes'] == 10
    assert SettingsRepo(reopened).get('objects_backfilled')


def test_readonly_does_not_create_database(tmp_path, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_DATABASE_URL', raising=False)
    assert for_server_dir(tmp_path / 'absent' / 'server', create=False) is None
    assert not (tmp_path / 'absent').exists()


def test_mixed_ingest_exact_storage_and_daily_uploads(server, monkeypatch):
    monkeypatch.setattr(usage_module, 'utc_day', lambda: '2026-11-08')
    usage = UsageRepo(server.db, default_workspace_id(server.db))
    uploaded = 0

    def check():
        before = usage.get()['stored_bytes']
        assert before == usage.recount()
        assert usage.get()['upload_bytes'] == uploaded

    meta = json.dumps({'run_id': 'run-1', 'status': 'running'}).encode()
    assert put_meta(server)[0] == 200
    uploaded += len(meta)
    check()
    for data, offset, query in [(b'a' * 32, 0, ''), (b'a' * 32, 0, ''),
                                 (b'b' * 16, 0, '&truncate=1')]:
        assert append(server, data, offset=offset, query=query)[0] == 200
        uploaded += len(data)
        check()
    for data in [b'{"long": "value"}', b'{}']:
        assert request(server, '/v2/runs/run-1/files/scalars/index.json', 'PUT', data)[0] == 200
        uploaded += len(data)
        check()
    data = b'new-object'
    sha = hashlib.sha256(data).hexdigest()
    for expected in [201, 200]:
        assert request(server, '/v2/objects/' + sha, 'PUT', data)[0] == expected
        if expected == 201:
            uploaded += len(data)
        check()
    # An existing disk object absent from the inventory repairs storage only.
    data = b'pre-backfill'
    sha = hashlib.sha256(data).hexdigest()
    path = layout.object_path(server.root, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    assert request(server, '/v2/objects/' + sha, 'PUT', data)[1]['created'] is False
    check()
    # Rejected uploads never increment either counter.
    assert append(server, b'c' * 16, offset=128)[0] == 409
    check()
    server.run_index.delete('run-1')
    assert usage.get()['stored_bytes'] == len(b'new-objectpre-backfill')
    check()
    monkeypatch.setattr(usage_module, 'utc_day', lambda: '2026-11-09')
    assert put_meta(server)[0] == 200
    assert usage.get()['upload_day'] == '2026-11-09'
    assert usage.get()['upload_bytes'] == len(meta)


def test_lineage_upload_counts_only_uploads(server, snapshots, tmp_path, monkeypatch):
    if not server.lineage_enabled:
        pytest.skip('git unavailable')
    from agenticml.store import RunReader
    monkeypatch.setattr(usage_module, 'utc_day', lambda: '2026-11-08')
    writer = snapshots[0]
    body = json.dumps(RunReader(writer.root, writer.run_id).meta()).encode()
    assert request(server, f'/v2/runs/{writer.run_id}/files/meta.json', 'PUT', body)[0] == 200
    usage = UsageRepo(server.db, default_workspace_id(server.db))
    data = bundle(writer, tmp_path)
    for expected in (201, 200):
        assert request(server, f'/v2/runs/{writer.run_id}/lineage', 'PUT', data)[0] == expected
    assert usage.get()['stored_bytes'] == len(body)
    assert usage.get()['upload_bytes'] == len(body) + 2 * len(data)


def test_replacements_adoption_recreation_and_rollback(server):
    runs = server.run_index
    usage = UsageRepo(server.db, default_workspace_id(server.db))
    runs.record_file('one', 'meta.json', 20)
    runs.replace_files('one', [('meta.json', 7, None)])
    assert usage.get()['stored_bytes'] == 7
    row = runs.get('one')
    row['run_id'] = 'two'
    assert runs.insert_adopted(row, [('meta.json', 5, None)])
    assert not runs.insert_adopted(row, [('meta.json', 500, None)])
    runs.upsert_adopted(row, [('meta.json', 9, None)])
    assert usage.get()['stored_bytes'] == 16
    runs.claim_meta('two', owner_user=None, owner_key=None, list_row={}, project=None,
                    created_at=None, launch_note=None, claim=False, recreate=True)
    assert usage.get()['stored_bytes'] == 7
    with pytest.raises(RuntimeError):
        with server.db.transaction(write=True) as tx:
            runs.replace_files('one', [('meta.json', 99, None)], tx)
            usage.record_object('a' * 64, 100, tx)
            raise RuntimeError('rollback')
    assert usage.get()['stored_bytes'] == usage.recount() == 7


def test_concurrent_object_dedupe_and_recount(server):
    usage = UsageRepo(server.db, default_workspace_id(server.db))

    def write(i):
        with server.db.transaction(write=True) as tx:
            return usage.record_object('a' * 64, 13, tx)

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(write, range(8))) == 1
    assert usage.get()['stored_bytes'] == usage.recount() == 13


@pytest.mark.parametrize('target', ['default', '--all'])
def test_recount_cli(server, capsys, target):
    usage = UsageRepo(server.db, default_workspace_id(server.db))
    server.run_index.record_file('one', 'meta.json', 21)
    with server.db.transaction(write=True) as tx:
        usage.add_stored(100, tx)
    assert cli.main(['server', 'workspaces', 'recount', target,
                     '--data', str(server.data_dir)]) == 0
    assert 'default: 121 -> 21 stored bytes' in capsys.readouterr().out
    event = next(e for e in AuditRepo(server.db, None).tail(100) if e['action'] == 'workspace.recount')
    assert event['target'] == default_workspace_id(server.db)
    assert event['detail']['old_stored_bytes'] == 121
    assert event['detail']['new_stored_bytes'] == 21
    assert usage.get()['stored_bytes'] == 21


def test_readonly_open_does_not_backfill(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    with running(data) as httpd:
        db = httpd.db
        wid = default_workspace_id(db)
        path = layout.object_path(store_root(data, wid), 'a' * 64)
        path.parent.mkdir(parents=True)
        path.write_bytes(b'old-object')
        SettingsRepo(db).delete('objects_backfilled')
    db.close()
    def unexpected_scan(root):
        pytest.fail('read-only open scanned objects')
    from agenticml.server.db import backfill
    original_scan = backfill._scan
    monkeypatch.setattr(backfill, '_scan', unexpected_scan)
    reopened = for_server_dir(data / 'server', create=False)
    assert UsageRepo(reopened, wid).get()['stored_bytes'] == 0
    assert SettingsRepo(reopened).get('objects_backfilled') is None
    monkeypatch.setattr(backfill, '_scan', original_scan)
    # A writing open must backfill even when the read-only open cached the DB.
    assert for_server_dir(data / 'server', create=True) is reopened
    assert UsageRepo(reopened, wid).get()['stored_bytes'] == 10


@pytest.mark.parametrize('component', ['objects', 'store', 'workspace', 'workspaces', 'missing'])
def test_backfill_skips_unsafe_store(server, tmp_path, caplog, component):
    root = server.root
    path = layout.object_path(root, 'a' * 64)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'outside')
    caplog.clear()
    target = {'objects': root / 'objects', 'store': root, 'workspace': root.parent,
              'workspaces': root.parent.parent, 'missing': root / 'objects'}[component]
    outside = tmp_path / 'moved'
    target.rename(outside)
    if component != 'missing':
        target.symlink_to(outside, target_is_directory=True)
    try:
        SettingsRepo(server.db).delete('objects_backfilled')
        backfill_objects(server.db, server.data_dir)
        assert UsageRepo(server.db, default_workspace_id(server.db)).get()['stored_bytes'] == 0
        assert 'Skipping unsafe or missing workspace store' in caplog.text
    finally:
        if target.is_symlink():
            target.unlink()
        outside.rename(target)


def test_backfill_unreadable_prefix(server, caplog):
    if os.geteuid() == 0:
        pytest.skip('root can read chmod 000 directories')
    blocked = layout.object_path(server.root, 'a' * 64)
    good = layout.object_path(server.root, 'b' * 64)
    for path in (blocked, good):
        path.parent.mkdir(parents=True)
        path.write_bytes(b'object')
    blocked.parent.chmod(0)
    try:
        SettingsRepo(server.db).delete('objects_backfilled')
        backfill_objects(server.db, server.data_dir)
        assert UsageRepo(server.db, default_workspace_id(server.db)).get()['stored_bytes'] == 6
        assert str(blocked.parent) in caplog.text
        assert any(record.levelname == 'WARNING' for record in caplog.records)
    finally:
        blocked.parent.chmod(0o700)


@pytest.mark.parametrize('failure', ['prefix', 'object'])
def test_backfill_vanished_entry(server, monkeypatch, caplog, failure):
    path = layout.object_path(server.root, 'a' * 64)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'object')
    original = Path.lstat
    vanished = path.parent if failure == 'prefix' else path
    def lstat(self, *args, **kwargs):
        if self == vanished:
            raise OSError('vanished entry')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    SettingsRepo(server.db).delete('objects_backfilled')
    backfill_objects(server.db, server.data_dir)
    assert UsageRepo(server.db, default_workspace_id(server.db)).get()['stored_bytes'] == 0
    assert 'vanished entry' in caplog.text


def test_upload_clock_is_read_after_lock_and_never_regresses(server, monkeypatch):
    usage = UsageRepo(server.db, default_workspace_id(server.db))
    original = usage.lock
    day = '2026-11-08'
    def lock(tx):
        nonlocal day
        original(tx)
        day = '2026-11-09'
    monkeypatch.setattr(usage, 'lock', lock)
    usage.clock = lambda: day
    with server.db.transaction(write=True) as tx:
        usage.add_upload(10, tx)
    assert usage.get()['upload_day'] == '2026-11-09'
    monkeypatch.setattr(usage, 'lock', original)
    day = '2026-11-08'
    with server.db.transaction(write=True) as tx:
        usage.add_upload(5, tx)
    assert usage.get()['upload_day'] == '2026-11-09'
    assert usage.get()['upload_bytes'] == 15
    day = '2026-11-10'
    with server.db.transaction(write=True) as tx:
        usage.add_upload(3, tx)
    assert usage.get()['upload_bytes'] == 3
    assert usage.get()['upload_day'] == day


def test_upload_requires_day_and_rolls_back_file(server, monkeypatch):
    monkeypatch.setattr(usage_module, 'utc_day', lambda: None)
    with pytest.raises(ValueError, match='require a UTC day'):
        server.run_index.record_file('one', 'meta.json', 20, upload_bytes=20)
    assert server.run_index.get('one') is None


@pytest.mark.parametrize('operation', ['stored', 'upload'])
def test_missing_usage_row_self_heals(server, operation):
    usage = UsageRepo(server.db, default_workspace_id(server.db), clock=lambda: '2026-11-09')
    with server.db.transaction(write=True) as tx:
        tx.execute('DELETE FROM workspace_usage WHERE workspace_id=?', (usage.workspace_id,))
    assert usage.get() == {'workspace_id': usage.workspace_id, 'stored_bytes': 0,
                           'upload_day': None, 'upload_bytes': 0,
                           'request_minute': None, 'request_count': 0}
    with server.db.transaction(write=True) as tx:
        if operation == 'stored':
            usage.add_stored(7, tx)
        else:
            usage.add_upload(7, tx)
    assert usage.get()[operation + '_bytes'] == 7


def test_concurrent_file_paths_and_recount(server):
    usage = UsageRepo(server.db, default_workspace_id(server.db))
    def write(i):
        # Different runs avoid serializing every writer on one runs row;
        # the shared usage lock must protect this workspace across writers.
        server.run_index.record_file(f'run-{i}', f'scalars/{i}.bin', i + 1)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(write, range(32)))
    assert usage.get()['stored_bytes'] == usage.recount() == sum(range(1, 33))


def test_recount_all_two_workspaces(server, capsys):
    account = AccountRepo(server.db).create('owner')
    second = WorkspaceRepo(server.db).create('second', owner=account['login'])
    server.run_index.record_file('one', 'meta.json', 21)
    RunRepo(server.db, second['id']).record_file('two', 'meta.json', 34)
    usages = [UsageRepo(server.db, wid) for wid in (default_workspace_id(server.db), second['id'])]
    with server.db.transaction(write=True) as tx:
        for usage in usages:
            usage.add_stored(100, tx)
    assert cli.main(['server', 'workspaces', 'recount', '--all',
                     '--data', str(server.data_dir)]) == 0
    output = capsys.readouterr().out
    assert 'default: 121 -> 21 stored bytes' in output
    assert 'second: 134 -> 34 stored bytes' in output
    assert [usage.get()['stored_bytes'] for usage in usages] == [21, 34]


def test_object_upload_counts_only_inserted_inventory(server):
    data = b'previously-inventoried'
    sha = hashlib.sha256(data).hexdigest()
    usage = UsageRepo(server.db, default_workspace_id(server.db))
    with server.db.transaction(write=True) as tx:
        assert usage.record_object(sha, len(data), tx)
    # The disk file is missing, but replacing it must not count an existing row.
    assert request(server, '/v2/objects/' + sha, 'PUT', data)[0] == 201
    assert usage.get()['upload_bytes'] == 0
    assert usage.get()['stored_bytes'] == len(data)

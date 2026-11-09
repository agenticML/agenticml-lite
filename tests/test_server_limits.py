"""Usage accounting and limits enforcement on both database backends."""
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
        tx.cross_workspace('test cascade delete')
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
    from datetime import datetime, timezone
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    configure(server, clock=lambda: now)
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
        uploaded += len(data)
        check()
    # An existing disk object absent from the inventory repairs storage.
    data = b'pre-backfill'
    sha = hashlib.sha256(data).hexdigest()
    path = layout.object_path(server.root, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    assert request(server, '/v2/objects/' + sha, 'PUT', data)[1]['created'] is False
    uploaded += len(data)
    check()
    # Rejected uploads never increment either counter.
    assert append(server, b'c' * 16, offset=128)[0] == 409
    check()
    server.run_index.delete('run-1')
    assert usage.get()['stored_bytes'] == len(b'new-objectpre-backfill')
    check()
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    assert put_meta(server)[0] == 200
    assert usage.get()['upload_day'] == '2026-11-09'
    assert usage.get()['upload_bytes'] == len(meta)


def test_lineage_upload_counts_only_uploads(server, snapshots, tmp_path, monkeypatch):
    if not server.lineage_enabled:
        pytest.skip('git unavailable')
    from agenticml.store import RunReader
    from datetime import datetime, timezone
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    configure(server, clock=lambda: now)
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
    capsys.readouterr()
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
    caplog.set_level('DEBUG', logger='agenticml.server.db.backfill')
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
        assert any(record.levelname == ('DEBUG' if component == 'missing' else 'WARNING')
                   for record in caplog.records)
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
    with pytest.raises(ValueError, match='require a UTC day'):
        server.run_index.record_file('one', 'meta.json', 20, upload_bytes=20,
                                     upload_clock=lambda: None)
    assert server.run_index.get('one') is None


@pytest.mark.parametrize('operation', ['stored', 'upload'])
def test_missing_usage_row_self_heals(server, operation):
    usage = UsageRepo(server.db, default_workspace_id(server.db), clock=lambda: '2026-11-09')
    with server.db.transaction(write=True) as tx:
        tx.bind_workspace(usage.workspace_id)
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


def configure(server, *, clock=None, **limits):
    from agenticml.server.limits import Plans, UNLIMITED
    server.plans = Plans({'default_plan': 'free', 'free_workspaces_per_account': 1,
                          'plans': {'free': {**UNLIMITED, **limits}}}, clock=clock)
    return UsageRepo(server.db, server.default_workspace_id)


@pytest.mark.parametrize('route', ['atomic', 'append', 'object'])
@pytest.mark.parametrize('headroom', [-1, 0, 1])
def test_storage_boundaries_write_nothing(server, route, headroom):
    assert put_meta(server)[0] == 200
    usage = UsageRepo(server.db, server.default_workspace_id)
    before = usage.get()['stored_bytes']
    before_files = server.run_index.files('run-1')
    if route == 'atomic':
        data = b'{"value": 1}'
        path = '/v2/runs/run-1/files/scalars/index.json'
        method = 'PUT'
        disk = server.root / 'runs/run-1/scalars/index.json'
    elif route == 'append':
        data = b'a' * 16
        path = '/v2/runs/run-1/append/scalars/0.bin?offset=0'
        method = 'POST'
        disk = server.root / 'runs/run-1/scalars/0.bin'
    else:
        data = b'object-data'
        sha = hashlib.sha256(data).hexdigest()
        path = '/v2/objects/' + sha
        method = 'PUT'
        disk = layout.object_path(server.root, sha)
    configure(server, storage_bytes=before + len(data) + headroom)
    status, body, _ = request(server, path, method, data)
    if headroom == -1:
        assert status == 403
        assert body == {'error': f'storage limit reached: {before / 1e9:.1f} GB of {(before + len(data) - 1) / 1e9:.1f} GB used; delete runs or ask for more'}
        assert not disk.exists()
        assert server.run_index.files('run-1') == before_files
        assert usage.get()['stored_bytes'] == before
        assert not list(server.tmp_dir.iterdir())
    else:
        assert status == (201 if route == 'object' else 200)
        assert disk.read_bytes() == data
        assert usage.get()['stored_bytes'] == before + len(data)


@pytest.mark.parametrize("overage", [0, 1, 100])
def test_storage_full_overlap_replace_truncate_and_dedupe(server, overage):
    assert put_meta(server)[0] == 200
    assert append(server, b'a' * 32)[0] == 200
    assert request(server, '/v2/runs/run-1/files/scalars/index.json', 'PUT', b'{"long": 123}')[0] == 200
    data = b'held-object'
    sha = hashlib.sha256(data).hexdigest()
    assert request(server, '/v2/objects/' + sha, 'PUT', data)[0] == 201
    used = UsageRepo(server.db, server.default_workspace_id).get()['stored_bytes']
    configure(server, storage_bytes=max(0, used - overage))
    assert append(server, b'a' * 32)[0] == 200
    assert append(server, b'a' * 16)[0] == 200
    assert request(server, '/v2/objects/' + sha, 'PUT', data)[0:2] == (200, {'sha256': sha, 'created': False})
    assert request(server, '/v2/runs/run-1/files/scalars/index.json', 'PUT', b'{}')[0] == 200
    assert append(server, b'b' * 16, query='&truncate=1')[0] == 200
    assert UsageRepo(server.db, server.default_workspace_id).get()['stored_bytes'] == used - 16 - (len(b'{"long": 123}') - 2)


def test_storage_rejected_append_preserves_existing_file(server):
    assert put_meta(server)[0] == 200
    assert append(server, b'a' * 16)[0] == 200
    usage = UsageRepo(server.db, server.default_workspace_id)
    configure(server, storage_bytes=usage.get()['stored_bytes'] + 15)
    files = server.run_index.files('run-1')
    assert append(server, b'b' * 16, offset=16)[0] == 403
    assert (server.root / 'runs/run-1/scalars/0.bin').read_bytes() == b'a' * 16
    assert server.run_index.files('run-1') == files


@pytest.mark.parametrize('route', ['atomic', 'append', 'object'])
@pytest.mark.parametrize('headroom', [-1, 0, 1])
def test_daily_upload_boundaries(server, route, headroom):
    from datetime import datetime, timezone
    assert put_meta(server)[0] == 200
    now = datetime(2026, 11, 10, 20, 48, tzinfo=timezone.utc)
    usage = configure(server, clock=lambda: now, upload_bytes_per_day=16 + headroom)
    if route == 'atomic':
        data = b'{"a": "1234567"}'
        assert len(data) == 16
        path, method = '/v2/runs/run-1/files/scalars/index.json', 'PUT'
    elif route == 'append':
        data = b'a' * 16
        path, method = '/v2/runs/run-1/append/scalars/0.bin?offset=0', 'POST'
    else:
        data = b'a' * 16
        path, method = '/v2/objects/' + hashlib.sha256(data).hexdigest(), 'PUT'
    status, body, _ = request(server, path, method, data)
    if headroom == -1:
        assert status == 403
        assert body['error'] == 'daily upload limit reached: 0.0 GB of 0.0 GB today; resets at 00:00 UTC (in 3 h 12 min)'
        assert usage.get()['upload_day'] != '2026-11-10'
    else:
        assert status in (200, 201)
        assert usage.get()['upload_day'] == '2026-11-10'
        assert usage.get()['upload_bytes'] == 16


def test_daily_rollover_and_read_routes_remain_available(server):
    from datetime import datetime, timezone
    from test_server_ingest import commit
    assert put_meta(server)[0] == 200
    now = datetime(2026, 11, 10, 23, 59, 59, tzinfo=timezone.utc)
    usage = configure(server, clock=lambda: now, storage_bytes=10**6, upload_bytes_per_day=16)
    assert append(server, b'a' * 16)[0] == 200
    assert append(server, b'b' * 16, offset=16)[0] == 403
    configure(server, clock=lambda: now, storage_bytes=0, upload_bytes_per_day=0)
    assert request(server, '/v2/whoami')[0] == 200
    assert request(server, '/v2/runs/run-1/state')[0] == 200
    assert request(server, '/v2/objects/missing', 'POST', b'{"objects": []}')[0] == 200
    assert commit(server, sizes={'scalars/0.bin': 16})[0] == 200
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    configure(server, clock=lambda: now, upload_bytes_per_day=16)
    assert append(server, b'b' * 16, offset=16)[0] == 200
    assert usage.get()['upload_bytes'] == 16
    assert usage.get()['upload_day'] == '2026-11-11'


def test_request_rate_rollover_health_and_retry_after(server):
    from datetime import datetime, timezone
    from test_server_ingest import raw
    now = datetime(2026, 11, 10, 12, 0, 42, tzinfo=timezone.utc)
    usage = configure(server, clock=lambda: now, requests_per_minute=2)
    assert request(server, '/v2/whoami')[0] == 200  # just under
    assert request(server, '/v2/whoami')[0] == 200  # at
    status, body, headers = request(server, '/v2/whoami')
    assert (status, body, headers['Retry-After']) == (429, {'error': 'too many requests'}, '18')
    assert raw(server, 'GET', '/healthz')[0] == 200
    assert request(server, '/v2/whoami', key='invalid')[0] == 401
    assert usage.get()['request_count'] == 3
    now = datetime(2026, 11, 10, 12, 1, tzinfo=timezone.utc)
    assert request(server, '/v2/whoami')[0] == 200
    assert usage.get()['request_count'] == 1


def test_request_counter_atomic_concurrency_and_sqlite_fallback(server, monkeypatch):
    import sqlite3
    usage = UsageRepo(server.db, server.default_workspace_id)
    with ThreadPoolExecutor(8) as pool:
        counts = list(pool.map(lambda _: usage.count_request(100)[0], range(24)))
    assert sorted(counts) == list(range(1, 25))
    monkeypatch.setattr(sqlite3, 'sqlite_version_info', (3, 34, 0))
    assert usage.count_request(100)[0] == 25
    assert usage.count_request(101)[0] == 1


def test_no_limits_request_never_creates_usage_row(server):
    with server.db.transaction(write=True) as tx:
        tx.bind_workspace(server.default_workspace_id)
        tx.execute('DELETE FROM workspace_usage WHERE workspace_id=?', (server.default_workspace_id,))
    for _ in range(3):
        assert request(server, '/v2/whoami')[0] == 200
    with server.db.transaction() as tx:
        tx.bind_workspace(server.default_workspace_id)
        assert tx.fetchone('SELECT 1 FROM workspace_usage WHERE workspace_id=?', (server.default_workspace_id,)) is None


def test_custom_nulls_are_unlimited(server):
    from agenticml.server.limits import UNLIMITED
    configure(server, storage_bytes=0, upload_bytes_per_day=0, requests_per_minute=0, people=0)
    WorkspaceRepo(server.db).set_plan('default', 'custom', dict(UNLIMITED))
    for _ in range(3):
        assert put_meta(server)[0] == 200
    assert server.plans.limits_for(WorkspaceRepo(server.db).get('default')) == UNLIMITED


@pytest.mark.parametrize('used', [0, 1, 2])
def test_people_boundaries_pending_and_existing_accounts(server, used):
    from agenticml.server.db.workspaces import InviteRepo
    from agenticml.server.limits import LimitReached
    wid = server.default_workspace_id
    usage = configure(server, people=3)
    invites = InviteRepo(server.db, wid)
    for i in range(used):
        invites.create(f'pending-{i}', 'member', 'cli')
    assert usage.people() == used + 1
    if used < 2:
        invites.create('new-person', 'member', 'admin', plans=server.plans)
        assert usage.people() == used + 2
    else:
        with pytest.raises(LimitReached, match='people limit reached \\(3 of 3\\)'):
            invites.create('new-person', 'member', 'admin', plans=server.plans)
        AccountRepo(server.db).create('existing')
        with pytest.raises(LimitReached):
            invites.create('existing', 'member', 'admin', plans=server.plans)
        with pytest.raises(LimitReached):
            server.users.add('member', plans=server.plans)
        # Explicitly administrative paths bypass quotas.
        server.users.add('operator-added')
        assert usage.people() == 4


def test_people_expired_invites_do_not_count(server):
    from agenticml.server.db.workspaces import InviteRepo
    from datetime import datetime, timezone
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    usage = configure(server, people=2, clock=lambda: now)
    repo = InviteRepo(server.db, server.default_workspace_id)
    repo.create('expired', 'viewer', 'cli')
    with server.db.transaction(write=True) as tx:
        tx.bind_workspace(server.default_workspace_id)
        tx.execute('UPDATE invites SET expires_at=? WHERE workspace_id=?',
                   ('2026-11-09T00:00:00Z', server.default_workspace_id))
    assert usage.people(now=now) == 1
    repo.create('fresh', 'viewer', 'admin', plans=server.plans)


def test_dashboard_rate_and_people_errors(server):
    from test_server_web import call, csrf, login
    # The ingest fixture's key belongs to its initial admin account.
    _, admin_key = server.keys.create("dashboard-admin", scope="admin")
    status, _, hdrs = login(server, admin_key)
    assert status == 200
    cookie = hdrs["Set-Cookie"].split(";", 1)[0]
    headers = {'Cookie': cookie, **csrf(server)}
    used = UsageRepo(server.db, server.default_workspace_id).people()
    configure(server, people=used)
    for path, payload in [('/api/w/default/invites', {'login': 'pending'}),
                          ('/api/w/default/members', {'name': 'new-user'})]:
        status, _, body = call(server, path, 'POST', json.dumps(payload), headers)
        assert status == 403, body
        assert json.loads(body)['error'] == f'people limit reached ({used} of {used})'
    status, _, body = call(server, '/api/w/default/invites', 'POST',
                           json.dumps({'login': server.users.list()[0].name}), headers)
    assert status == 400, body
    assert json.loads(body)['error'] == 'membership already exists'
    configure(server, people=used + 1)
    for _ in range(2):
        status, _, body = call(server, '/api/w/default/invites', 'POST',
                               json.dumps({'login': 'repeat-invite'}), headers)
        assert status == 201, body
    configure(server, requests_per_minute=0)
    for path in ['/api/w/default/members', '/w/default/']:
        status, hdrs, body = call(server, path, headers={'Cookie': cookie})
        assert status == 429, body
        assert 1 <= int(hdrs['Retry-After']) <= 60
        assert json.loads(body) == {'error': 'too many requests'}


def test_set_plan_usage_cli_and_audit(server, tmp_path, capsys):
    from agenticml.server.limits import Plans
    data = str(server.data_dir)
    assert cli.main(['server', 'workspaces', 'set-plan', 'default', 'custom',
                     '--storage', '1.5GB', '--uploads-per-day', '2MB',
                     '--requests-per-minute', 'unlimited', '--people', '0', '--data', data]) == 0
    row = WorkspaceRepo(server.db).get('default')
    assert json.loads(row['custom_limits']) == {'storage_bytes': 1500000000, 'upload_bytes_per_day': 2000000,
                                               'requests_per_minute': None, 'people': 0}
    capsys.readouterr()
    event = next(e for e in AuditRepo(server.db, None).tail(100) if e['action'] == 'workspace.plan')
    assert event['detail']['old'] == {'plan': 'free', 'limits': None}
    assert event['detail']['new']['limits'] == json.loads(row['custom_limits'])
    server.run_index.record_file('one', 'meta.json', 100)
    account = AccountRepo(server.db).create('owner')
    second = WorkspaceRepo(server.db).create('second', owner=account['login'])
    RunRepo(server.db, second['id']).record_file('two', 'meta.json', 200)
    path = tmp_path / 'limits.json'
    path.write_text(Path('docs/examples/limits.json').read_text())
    Plans.load(path)
    assert cli.main(['server', 'workspaces', 'usage', '--json', '--data', data, '--limits', str(path)]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r['slug'] for r in rows] == ['second', 'default']
    assert rows[0]['limits']['storage_bytes'] == 5000000000
    assert rows[1]['limits']['storage_bytes'] == 1500000000
    assert rows[0]['people'] == 1
    assert cli.main(['server', 'workspaces', 'usage', '--data', data]) == 0
    assert 'no limits file' in capsys.readouterr().out
    assert cli.main(['server', 'workspaces', 'set-plan', 'default', 'free', '--data', data]) == 0
    assert WorkspaceRepo(server.db).get('default')['custom_limits'] is None
    assert cli.main(['server', 'workspaces', 'set-plan', 'default', 'free', '--people', '1', '--data', data]) == 1


@pytest.mark.parametrize('mutate', [
    lambda c: c.update(unknown=1),
    lambda c: c['plans'].update(custom={}),
    lambda c: c['plans']['free'].update(bogus=1),
    lambda c: c['plans']['free'].update(people=-1),
    lambda c: c['plans']['free'].update(people=True),
    lambda c: c['plans']['free'].update(people=1.5),
    lambda c: c.update(free_workspaces_per_account=-1),
    lambda c: c.update(default_plan='custom'),
    lambda c: c['plans']['free'].pop('people'),
])
def test_invalid_limits_refused_before_server_start(tmp_path, mutate, capsys, monkeypatch):
    from agenticml.server.app import make_server
    config = json.loads(Path('docs/examples/limits.json').read_text())
    mutate(config)
    path = tmp_path / 'limits.json'
    path.write_text(json.dumps(config))
    data = tmp_path / 'data'
    assert cli.main(['server', 'serve', '--data', str(data), '--limits', str(path)]) == 1
    assert 'invalid limits file' in capsys.readouterr().err
    assert not data.exists()
    with pytest.raises(ValueError, match='invalid limits file'):
        make_server(data, port=0, plans=path)
    monkeypatch.setenv('AGENTICML_SERVER_LIMITS', str(path))
    assert cli.main(['server', 'serve', '--data', str(data)]) == 1


def test_limits_flag_and_environment_reach_server(tmp_path, monkeypatch):
    from agenticml.server.limits import Plans
    import agenticml.server
    path = Path('docs/examples/limits.json')
    received = []
    monkeypatch.setattr(agenticml.server, 'serve', lambda *a, **kw: received.append(kw['plans']))
    monkeypatch.setenv('AGENTICML_SERVER_LIMITS', str(path))
    assert cli.main(['server', 'serve', '--data', str(tmp_path)]) == 0
    assert isinstance(received[0], Plans)
    assert received[0].free['people'] == 5


def test_oversized_body_refused_before_read(server):
    from test_server_ingest import raw, auth
    assert put_meta(server)[0] == 200
    usage = UsageRepo(server.db, server.default_workspace_id)
    configure(server, storage_bytes=usage.get()['stored_bytes'])
    # No body is sent. Reading it would block rather than return the quota error.
    headers = auth(server) + [('Content-Length', '16')]
    assert raw(server, 'POST', '/v2/runs/run-1/append/scalars/0.bin?offset=0', headers)[0] == 403
    configure(server, upload_bytes_per_day=0)
    assert raw(server, 'PUT', '/v2/objects/' + 'a' * 64, headers)[0] == 403
    assert not list(server.tmp_dir.iterdir())


def test_lineage_daily_limit_and_storage_exemption(server, snapshots, tmp_path):
    from datetime import datetime, timezone
    from agenticml.store import RunReader
    if not server.lineage_enabled:
        pytest.skip('git unavailable')
    writer = snapshots[0]
    body = json.dumps(RunReader(writer.root, writer.run_id).meta()).encode()
    assert request(server, f'/v2/runs/{writer.run_id}/files/meta.json', 'PUT', body)[0] == 200
    data = bundle(writer, tmp_path)
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    usage = configure(server, clock=lambda: now, storage_bytes=0, upload_bytes_per_day=len(data) - 1)
    path = f'/v2/runs/{writer.run_id}/lineage'
    assert request(server, path, 'PUT', data)[0] == 403
    configure(server, clock=lambda: now, storage_bytes=0, upload_bytes_per_day=len(data))
    assert request(server, path, 'PUT', data)[0] == 201
    assert usage.get()['stored_bytes'] == len(body)
    assert usage.get()['upload_bytes'] == len(data)
    configure(server, clock=lambda: now, storage_bytes=0, upload_bytes_per_day=2 * len(data) + 1)
    assert request(server, path, 'PUT', data)[0] == 200


def test_held_inventory_missing_disk_and_dedupe_daily_bytes(server):
    from datetime import datetime, timezone
    data = b'previously-held'
    sha = hashlib.sha256(data).hexdigest()
    usage = UsageRepo(server.db, server.default_workspace_id)
    with server.db.transaction(write=True) as tx:
        usage.record_object(sha, len(data), tx)
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    configure(server, clock=lambda: now, storage_bytes=len(data), upload_bytes_per_day=2 * len(data))
    assert request(server, '/v2/objects/' + sha, 'PUT', data)[0] == 201
    assert request(server, '/v2/objects/' + sha, 'PUT', data)[0] == 200
    assert usage.get()['stored_bytes'] == len(data)
    assert usage.get()['upload_bytes'] == 2 * len(data)
    assert request(server, '/v2/objects/' + sha, 'PUT', data)[0] == 403


def test_people_admission_is_atomic(server):
    from agenticml.server.db.workspaces import InviteRepo
    from agenticml.server.limits import LimitReached
    usage = configure(server, people=2)
    def invite(i):
        try:
            InviteRepo(server.db, server.default_workspace_id).create(
                f'person-{i}', 'viewer', 'admin', plans=server.plans)
            return True
        except LimitReached:
            return False
    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(invite, range(8))) == 1
    assert usage.people() == 2


@pytest.mark.parametrize('value, expected', [('1KB', 1000), ('2MB', 2000000), ('1.5GB', 1500000000),
                                            ('1TB', 1000000000000), ('0', 0), ('unlimited', None)])
def test_cli_size_units(value, expected):
    from agenticml.server.limits import parse_size
    assert parse_size(value) == expected


@pytest.mark.parametrize('value', ['-1GB', '1GiB', '0.0001KB', 'NaN', '1.5'])
def test_cli_bad_sizes(value):
    from agenticml.server.limits import parse_size
    with pytest.raises(ValueError):
        parse_size(value)


def test_usage_env_and_no_limits_json(server, monkeypatch, capsys):
    args = ['server', 'workspaces', 'usage', '--json', '--data', str(server.data_dir)]
    monkeypatch.delenv('AGENTICML_SERVER_LIMITS', raising=False)
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)[0]['limits'] is None
    monkeypatch.setenv('AGENTICML_SERVER_LIMITS', 'docs/examples/limits.json')
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)[0]['limits']['people'] == 5


@pytest.mark.parametrize('content', ['{', '[]', 'null'])
def test_bad_limits_json_refused(tmp_path, capsys, content):
    path = tmp_path / 'bad.json'
    path.write_text(content)
    assert cli.main(['server', 'serve', '--limits', str(path)]) == 1
    assert 'invalid limits file' in capsys.readouterr().err


def test_new_limits_apply_on_restart(tmp_path):
    from agenticml.server.app import make_server
    path = tmp_path / 'limits.json'
    config = json.loads(Path('docs/examples/limits.json').read_text())
    path.write_text(json.dumps(config))
    httpd = make_server(tmp_path / 'data', port=0, plans=path)
    assert httpd.plans.free['people'] == 5
    httpd.server_close()
    config['plans']['free']['people'] = 8
    path.write_text(json.dumps(config))
    httpd = make_server(tmp_path / 'data', port=0, plans=path)
    try:
        assert httpd.plans.free['people'] == 8
    finally:
        httpd.server_close()


def test_limits_and_request_counts_are_workspace_bound(server):
    from agenticml.server.keys import KeyStore
    from agenticml.server.limits import UNLIMITED
    from agenticml.server.workspaces import prepare_store
    owner = AccountRepo(server.db).create('second-owner')
    second = WorkspaceRepo(server.db).create('second', owner=owner['login'])
    prepare_store(server.data_dir, second['id'])
    _, key = KeyStore(server.data_dir / 'server', db=server.db, workspace_id=second['id']).create(
        'second-key', user=owner['login'])
    WorkspaceRepo(server.db).set_plan('second', 'custom', dict(UNLIMITED))
    usage = configure(server, requests_per_minute=0, storage_bytes=0)
    assert request(server, '/v2/whoami')[0] == 429
    assert request(server, '/v2/whoami', key=key)[0] == 200
    assert put_meta(server, key=key)[0] == 200
    assert usage.get()['request_count'] == 1
    assert usage.get()['stored_bytes'] == 0
    assert UsageRepo(server.db, second['id']).get()['request_count'] == 2
    assert UsageRepo(server.db, second['id']).get()['stored_bytes'] > 0


def test_missing_limits_file_refused(tmp_path, capsys):
    assert cli.main(['server', 'serve', '--limits', str(tmp_path / 'absent.json')]) == 1
    assert 'invalid limits file' in capsys.readouterr().err


@pytest.mark.parametrize('offset', [32, 10**100])
def test_append_future_offset_resync_at_storage_limit(server, offset):
    assert put_meta(server)[0] == 200
    assert append(server, b'a' * 16)[0] == 200
    used = UsageRepo(server.db, server.default_workspace_id).get()['stored_bytes']
    configure(server, storage_bytes=used)
    assert append(server, b'b' * 16, offset=offset)[:2] == (409, {'size': 16})
    assert UsageRepo(server.db, server.default_workspace_id).get()['stored_bytes'] == used
    assert (server.root / 'runs/run-1/scalars/0.bin').read_bytes() == b'a' * 16


@pytest.mark.parametrize('route', ['atomic', 'append', 'object'])
@pytest.mark.parametrize('quota', ['storage_bytes', 'upload_bytes_per_day'])
@pytest.mark.parametrize('over_by', [0, 1])
def test_slow_body_does_not_lock_and_quota_rechecked(server, monkeypatch, route, quota, over_by):
    import http.client
    import threading
    from agenticml.server.ingest import IngestHandler
    assert put_meta(server)[0] == 200
    assert put_meta(server, run_id='run-2')[0] == 200
    body = b'{"a": "1234567"}' if route == 'atomic' else b'a' * 16
    sha = hashlib.sha256(body).hexdigest()
    path, method = {
        'atomic': ('/v2/runs/run-1/files/scalars/index.json', 'PUT'),
        'append': ('/v2/runs/run-1/append/scalars/0.bin?offset=0', 'POST'),
        'object': ('/v2/objects/' + sha, 'PUT'),
    }[route]
    usage = UsageRepo(server.db, server.default_workspace_id)
    counter = 'stored_bytes' if quota == 'storage_bytes' else 'upload_bytes'
    configure(server, **{quota: usage.get()[counter] + 16 + (16 if route == 'object' else 0) + len(body) - over_by})
    receiving = threading.Event()
    original = IngestHandler._read_body
    def read(handler, *args, **kwargs):
        if handler.path == path:
            receiving.set()
        return original(handler, *args, **kwargs)
    monkeypatch.setattr(IngestHandler, '_read_body', read)
    conn = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    try:
        conn.putrequest(method, path)
        conn.putheader('Authorization', 'Bearer ' + server.test_key)
        conn.putheader('Content-Length', str(len(body)))
        conn.endheaders()
        conn.send(body[:1])
        assert receiving.wait(2)
        # The body remains incomplete while other requests use the locks.
        assert request(server, '/v2/runs/run-1/state')[0] == 200
        assert request(server, '/v2/runs/run-2/files/scalars/index.json', 'PUT', b'{"a": "1234567"}')[0] == 200
        if route == 'object':
            for i in range(10000):
                other = str(i).encode().ljust(16, b'x')
                other_sha = hashlib.sha256(other).hexdigest()
                if other_sha.startswith(sha[:2]) and other_sha != sha:
                    break
            else:
                pytest.fail('no matching SHA prefix')
            assert request(server, '/v2/objects/' + other_sha, 'PUT', other)[0] == 201
        before = usage.get()
        files = server.run_index.files('run-1')
        conn.send(body[1:])
        response = conn.getresponse()
        if over_by == 0:
            assert response.status == (201 if route == 'object' else 200)
            response.read()
            assert usage.get()[counter] == before[counter] + len(body)
            return
        assert response.status == 403
        assert json.loads(response.read())['error'].startswith(
            'storage limit reached:' if quota == 'storage_bytes' else 'daily upload limit reached:')
        assert usage.get() == before
        assert server.run_index.files('run-1') == files
        assert not (server.root / 'runs/run-1/scalars/index.json').exists()
        assert not (server.root / 'runs/run-1/scalars/0.bin').exists()
        assert not layout.object_path(server.root, sha).exists()
    finally:
        conn.close()


def test_invite_resend_at_people_limit_and_member_error(server):
    from agenticml.server.db.workspaces import InviteRepo
    from agenticml.server.keys import ClientError
    from agenticml.server.limits import LimitReached
    usage = configure(server, people=2)
    invites = InviteRepo(server.db, server.default_workspace_id)
    first = invites.create('pending', 'viewer', 'admin', plans=server.plans)
    assert usage.people() == 2
    second = invites.create('pending', 'member', 'admin', plans=server.plans)
    assert first != second
    assert invites.find_pending(server.db, first) is None
    assert invites.find_pending(server.db, second)['role'] == 'member'
    assert usage.people() == 2
    member = server.users.list()[0].name
    with pytest.raises(ClientError, match='membership already exists') as exc:
        invites.create(member, 'viewer', 'admin', plans=server.plans)
    assert not isinstance(exc.value, LimitReached)
    with server.db.transaction(write=True) as tx:
        tx.bind_workspace(server.default_workspace_id)
        tx.execute('UPDATE invites SET expires_at=? WHERE workspace_id=?',
                   ('2000-01-01T00:00:00Z', server.default_workspace_id))
    configure(server, people=1)
    with pytest.raises(LimitReached):
        invites.create('pending', 'viewer', 'admin', plans=server.plans)


def test_custom_plan_partial_updates_and_output(server, capsys, monkeypatch):
    monkeypatch.delenv('AGENTICML_SERVER_LIMITS', raising=False)
    args = ['server', 'workspaces', 'set-plan', 'default', 'custom', '--data', str(server.data_dir)]
    assert cli.main(args) == 1
    assert 'at least one limit flag' in capsys.readouterr().err
    assert cli.main(args + ['--storage', '1GB']) == 0
    initial = json.loads(capsys.readouterr().out)['limits']
    assert initial['storage_bytes'] == 10**9
    assert cli.main(args + ['--people', '3']) == 0
    updated = json.loads(capsys.readouterr().out)['limits']
    assert updated == dict(initial, people=3)
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)['limits'] == updated
    free = ['server', 'workspaces', 'set-plan', 'default', 'free', '--data', str(server.data_dir)]
    assert cli.main(free) == 0
    capsys.readouterr()
    assert cli.main(args + ['--limits', 'docs/examples/limits.json', '--people', '7']) == 0
    from agenticml.server.limits import Plans
    assert json.loads(capsys.readouterr().out)['limits'] == dict(Plans.load('docs/examples/limits.json').free, people=7)
    assert cli.main(free) == 0
    capsys.readouterr()
    monkeypatch.setenv('AGENTICML_SERVER_LIMITS', 'docs/examples/limits.json')
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)['limits'] == Plans.load('docs/examples/limits.json').free


def test_deleted_workspace_during_request_returns_404(server, monkeypatch):
    from test_server_web import call, login
    _, key = server.keys.create('dashboard-admin', scope='admin')
    status, _, headers = login(server, key)
    assert status == 200
    cookie = headers['Set-Cookie'].split(';', 1)[0]
    usage = configure(server)
    original = WorkspaceRepo.by_id
    def deleted(repo, wid, tx=None):
        if tx is None:
            return None
        return original(repo, wid, tx)
    # Binding completes, then the quota lookup sees the concurrent deletion.
    original_request = server.plans.request
    def deleted_request(db, wid):
        with monkeypatch.context() as patch:
            patch.setattr(WorkspaceRepo, 'by_id', deleted)
            return original_request(db, wid)
    monkeypatch.setattr(server.plans, 'request', deleted_request)
    assert request(server, '/v2/whoami')[:2] == (404, {'error': 'not found'})
    assert call(server, '/api/w/default/members', headers={'Cookie': cookie})[0] == 404
    assert usage.get()['request_count'] == 0


def test_usage_limits_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(['server', 'workspaces', 'usage', '--help'])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert '--limits FILE' in help_text
    assert 'AGENTICML_SERVER_LIMITS' in help_text


@pytest.mark.parametrize('route', ['atomic', 'append', 'object'])
@pytest.mark.parametrize('quota', ['storage_bytes', 'upload_bytes_per_day', 'both'])
def test_workspace_quota_serializes_parallel_publish(server, monkeypatch, route, quota):
    import threading
    from agenticml.server.ingest import IngestHandler
    for i in range(8):
        assert put_meta(server, run_id=f'run-{i}')[0] == 200
    usage = UsageRepo(server.db, server.default_workspace_id)
    initial = usage.get()['stored_bytes']
    limits = {}
    if quota in ('storage_bytes', 'both'):
        limits['storage_bytes'] = initial + 32
    if quota in ('upload_bytes_per_day', 'both'):
        limits['upload_bytes_per_day'] = 32
    configure(server, **limits)
    # The login throttle separately caps simultaneous authentications per IP.
    # Complete authentication in turn; all eight bodies still receive together.
    authenticating = threading.Lock()
    authenticate = IngestHandler._authenticate
    def auth(handler):
        with authenticating:
            return authenticate(handler)
    monkeypatch.setattr(IngestHandler, '_authenticate', auth)
    barrier = threading.Barrier(8)
    original = IngestHandler._read_body
    def read(handler, *args, **kwargs):
        result = original(handler, *args, **kwargs)
        barrier.wait(timeout=30)
        return result
    monkeypatch.setattr(IngestHandler, '_read_body', read)
    def upload(i):
        data = ('{"v": "%07d"}' % i).encode() if route == 'atomic' else bytes([i]) * 16
        assert len(data) == 16
        path, method = {
            'atomic': (f'/v2/runs/run-{i}/files/scalars/index.json', 'PUT'),
            'append': (f'/v2/runs/run-{i}/append/scalars/0.bin?offset=0', 'POST'),
            'object': ('/v2/objects/' + hashlib.sha256(data).hexdigest(), 'PUT'),
        }[route]
        import http.client
        conn = http.client.HTTPConnection(*server.server_address[:2], timeout=40)
        try:
            conn.request(method, path, data, {'Authorization': 'Bearer ' + server.test_key})
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()
    with ThreadPoolExecutor(8) as pool:
        statuses = list(pool.map(upload, range(8)))
    assert statuses.count(201 if route == 'object' else 200) == 2
    assert statuses.count(403) == 6
    assert usage.get()['stored_bytes'] == usage.recount() == initial + 32
    assert usage.get()['upload_bytes'] == 32


def test_object_record_failure_removes_publish_and_retry_counts_once(server, monkeypatch):
    data = b'failed-publish'
    sha = hashlib.sha256(data).hexdigest()
    usage = configure(server, storage_bytes=len(data))
    original = UsageRepo.record_object
    def fail(repo, *args, **kwargs):
        original(repo, *args, **kwargs)
        raise RuntimeError('injected recording failure')
    with monkeypatch.context() as patch:
        patch.setattr(UsageRepo, 'record_object', fail)
        assert request(server, '/v2/objects/' + sha, 'PUT', data)[0] == 500
    assert not layout.object_path(server.root, sha).exists()
    assert usage.get()['stored_bytes'] == 0
    for expected in (201, 200):
        assert request(server, '/v2/objects/' + sha, 'PUT', data)[0] == expected
    assert usage.get()['stored_bytes'] == usage.recount() == len(data)
    assert usage.get()['upload_bytes'] == 2 * len(data)


def test_object_disk_inventory_repair_at_full_storage(server):
    data = b'uninventoried'
    sha = hashlib.sha256(data).hexdigest()
    dest = layout.object_path(server.root, sha)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    usage = configure(server, storage_bytes=0)
    for _ in range(2):
        assert request(server, '/v2/objects/' + sha, 'PUT', data)[:2] == (
            200, {'sha256': sha, 'created': False})
    assert usage.get()['stored_bytes'] == usage.recount() == len(data)


def test_dashboard_member_and_reset_invite_count_once(server):
    from test_server_web import call, csrf, login
    _, key = server.keys.create('dashboard-admin', scope='admin')
    status, _, headers = login(server, key)
    assert status == 200
    headers = {'Cookie': headers['Set-Cookie'].split(';', 1)[0], **csrf(server)}
    usage = configure(server, people=3)
    for name in ('first', 'second'):
        assert call(server, '/api/w/default/members', 'POST', json.dumps({'name': name}), headers)[0] == 201
    assert usage.people() == 3
    assert call(server, '/api/w/default/members', 'POST', json.dumps({'name': 'third'}), headers)[0] == 403


def test_existing_account_pending_invite_checks_people(server):
    from agenticml.server.db.workspaces import InviteRepo, MembershipRepo
    from agenticml.server.limits import LimitReached
    invites = InviteRepo(server.db, server.default_workspace_id)
    invites.create('pending-account', 'viewer', 'admin')
    account = AccountRepo(server.db).create('pending-account')
    usage = configure(server, people=2)
    assert usage.people() == 2
    with pytest.raises(LimitReached):
        invites.create('pending-account', 'member', 'admin', plans=server.plans)
    assert MembershipRepo(server.db, server.default_workspace_id).get(account['id']) is None


def test_no_limits_uploads_maintain_only_storage(server, snapshots, tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('quota lock or upload accounting without limits')
    monkeypatch.setattr(server, 'quota_lock', unexpected)
    monkeypatch.setattr(UsageRepo, 'add_upload', unexpected)
    assert put_meta(server)[0] == 200
    assert append(server, b'a' * 16)[0] == 200
    data = b'object'
    assert request(server, '/v2/objects/' + hashlib.sha256(data).hexdigest(), 'PUT', data)[0] == 201
    if server.lineage_enabled:
        from agenticml.store import RunReader
        writer = snapshots[0]
        body = json.dumps(RunReader(writer.root, writer.run_id).meta()).encode()
        assert request(server, f'/v2/runs/{writer.run_id}/files/meta.json', 'PUT', body)[0] == 200
        assert request(server, f'/v2/runs/{writer.run_id}/lineage', 'PUT', bundle(writer, tmp_path))[0] == 201
    usage = UsageRepo(server.db, server.default_workspace_id)
    row = usage.get()
    assert row['stored_bytes'] == usage.recount() > 0
    assert row['upload_bytes'] == row['request_count'] == 0
    assert row['upload_day'] is row['request_minute'] is None


@pytest.mark.parametrize('limits', [False, True])
def test_future_append_offset_rejected_without_body(server, limits):
    from test_server_ingest import raw, auth
    assert put_meta(server)[0] == 200
    assert append(server, b'a' * 16)[0] == 200
    if limits:
        configure(server)
    assert raw(server, 'POST', '/v2/runs/run-1/append/scalars/0.bin?offset=32',
               auth(server) + [('Content-Length', '16')])[:2] == (409, {'size': 16})


def test_set_free_ignores_unreadable_limits(server, monkeypatch, tmp_path):
    monkeypatch.setenv('AGENTICML_SERVER_LIMITS', str(tmp_path / 'missing.json'))
    assert cli.main(['server', 'workspaces', 'set-plan', 'default', 'free',
                     '--data', str(server.data_dir)]) == 0


def _dash_cookie(server):
    from test_server_web import login
    _, key = server.keys.create('dash-admin', scope='admin')
    status, _, headers = login(server, key)
    assert status == 200
    return headers['Set-Cookie']


def test_usage_api_without_limits_and_non_member_404(server):
    from test_server_signin import PASSWORD, password_login
    from test_server_web import authed
    from agenticml.server.users import UserStore
    from agenticml.server.workspaces import prepare_store

    cookie = _dash_cookie(server)
    usage = UsageRepo(server.db, server.default_workspace_id)
    before = usage.get()
    status, _, body, _ = authed(server, '/api/w/default/usage', cookie=cookie)
    assert status == 200
    assert json.loads(body) == {'limits': None}
    after = usage.get()
    assert after['stored_bytes'] == before['stored_bytes']
    assert after['upload_bytes'] == before['upload_bytes']
    assert after['request_count'] == before['request_count']

    other = WorkspaceRepo(server.db).create('other', owner='admin')
    prepare_store(server.data_dir, other['id'])
    UserStore(server.data_dir / 'server', db=server.db, workspace_id=other['id']).add(
        'viewer-b', 'member')
    UserStore(server.data_dir / 'server', db=server.db, workspace_id=other['id']).set_password(
        'viewer-b', PASSWORD)
    status, _, hdrs = password_login(server, 'viewer-b', PASSWORD)
    assert status == 200
    outsider = hdrs['Set-Cookie']
    denied = authed(server, '/api/w/default/usage', cookie=outsider)
    missing = authed(server, '/api/w/default/nope', cookie=outsider)
    assert denied[0] == 404 and json.loads(denied[2]) == {'error': 'not found'}
    assert (denied[0], json.loads(denied[2])) == (missing[0], json.loads(missing[2]))
    status, _, body, _ = authed(server, '/api/w/other/usage', cookie=outsider)
    assert status == 200
    assert json.loads(body) == {'limits': None}


def test_usage_api_with_limits(server):
    from datetime import datetime, timezone
    from test_server_web import authed

    now = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    usage = configure(server, clock=lambda: now, storage_bytes=5_000_000_000,
                      upload_bytes_per_day=2_000_000_000, requests_per_minute=600, people=5)
    with server.db.transaction(write=True) as tx:
        usage.add_stored(1_500_000_000, tx)
        usage.add_upload(400_000_000, tx)
    cookie = _dash_cookie(server)
    before = usage.get()
    status, _, body, _ = authed(server, '/api/w/default/usage', cookie=cookie)
    assert status == 200
    payload = json.loads(body)
    assert payload['plan'] == 'free'
    assert payload['storage'] == {'used': 1_500_000_000, 'limit': 5_000_000_000}
    assert payload['uploads_today'] == {
        'used': 400_000_000, 'limit': 2_000_000_000, 'resets_at': '2026-11-10T00:00:00Z'}
    assert payload['requests_per_minute'] == {'limit': 600}
    assert payload['people']['limit'] == 5
    assert isinstance(payload['people']['used'], int) and payload['people']['used'] >= 1
    after = usage.get()
    assert after['stored_bytes'] == before['stored_bytes']
    assert after['upload_bytes'] == before['upload_bytes']
    assert after['request_count'] >= before['request_count']


def test_admin_patch_plan_limits_audit_and_non_admin_404(server):
    from test_server_signin import PASSWORD, password_login
    from test_server_web import authed, csrf
    from agenticml.server.limits import UNLIMITED

    cookie = _dash_cookie(server)
    custom = {**UNLIMITED, 'storage_bytes': 1000, 'people': 2}
    missing_csrf = csrf(server)
    del missing_csrf['X-AgenticML-Request']
    status, _, body, _ = authed(
        server, '/api/admin/workspaces/default', 'PATCH',
        json.dumps({'plan': 'custom', 'limits': custom}).encode(),
        extra=missing_csrf, cookie=cookie)
    assert status == 403
    assert json.loads(body)['error'] == 'cross-site request refused'
    assert WorkspaceRepo(server.db).get('default')['plan'] == 'free'

    status, _, body, _ = authed(
        server, '/api/admin/workspaces/default', 'PATCH',
        json.dumps({'plan': 'custom', 'limits': custom, 'name': 'Nope'}).encode(),
        extra=csrf(server), cookie=cookie)
    assert status == 400
    assert json.loads(body)['error'] == (
        'cannot change plan or limits together with name, slug, or status')
    stored = WorkspaceRepo(server.db).get('default')
    assert stored['plan'] == 'free' and stored['name'] != 'Nope'

    status, _, body, _ = authed(
        server, '/api/admin/workspaces/default', 'PATCH',
        json.dumps({'plan': 'custom', 'limits': custom}).encode(),
        extra=csrf(server), cookie=cookie)
    assert status == 200, body
    row = json.loads(body)['workspace']
    assert row['plan'] == 'custom'
    assert row['limits'] == custom
    assert row['stored_bytes'] >= 0 and row['uploads_today'] >= 0 and row['people'] >= 1
    stored = WorkspaceRepo(server.db).get('default')
    assert json.loads(stored['custom_limits']) == custom
    event = next(e for e in AuditRepo(server.db, None).tail(100) if e['action'] == 'workspace.plan')
    assert event['detail']['old'] == {'plan': 'free', 'limits': None}
    assert event['detail']['new'] == {'plan': 'custom', 'limits': custom}

    listed = authed(server, '/api/admin/workspaces', cookie=cookie)
    assert listed[0] == 200
    default = next(r for r in json.loads(listed[2])['workspaces'] if r['slug'] == 'default')
    assert default['plan'] == 'custom' and default['limits'] == custom

    status, _, body, _ = authed(
        server, '/api/admin/workspaces/default', 'PATCH',
        json.dumps({'plan': 'custom', 'limits': {'storage_bytes': 1}}).encode(),
        extra=csrf(server), cookie=cookie)
    assert status == 200, body
    merged = dict(custom, storage_bytes=1)
    assert json.loads(body)['workspace']['limits'] == merged
    assert json.loads(WorkspaceRepo(server.db).get('default')['custom_limits']) == merged

    status, _, body, _ = authed(
        server, '/api/admin/workspaces/default', 'PATCH',
        json.dumps({'plan': 'free', 'limits': custom}).encode(),
        extra=csrf(server), cookie=cookie)
    assert status == 400
    assert json.loads(body)['error'] == 'free plan cannot have custom limits'

    status, _, body, _ = authed(
        server, '/api/admin/workspaces/default', 'PATCH',
        json.dumps({'plan': 'free'}).encode(), extra=csrf(server), cookie=cookie)
    assert status == 200
    assert json.loads(body)['workspace']['plan'] == 'free'
    assert json.loads(body)['workspace']['limits'] is None

    listed = authed(server, '/api/admin/workspaces', cookie=cookie)
    assert listed[0] == 200
    default = next(r for r in json.loads(listed[2])['workspaces'] if r['slug'] == 'default')
    assert default['plan'] == 'free' and default['limits'] is None
    assert 'stored_bytes' in default and 'uploads_today' in default and 'people' in default

    server.users.add('member', 'member')
    server.users.set_password('member', PASSWORD)
    status, _, hdrs = password_login(server, 'member', PASSWORD)
    outsider = hdrs['Set-Cookie']
    denied = authed(
        server, '/api/admin/workspaces/default', 'PATCH',
        json.dumps({'plan': 'custom', 'limits': custom}).encode(),
        extra=csrf(server), cookie=outsider)
    missing = authed(server, '/api/no-such', 'PATCH', b'{}', extra=csrf(server), cookie=outsider)
    assert denied[0] == 404 and json.loads(denied[2]) == {'error': 'not found'}
    assert denied[0] == missing[0]

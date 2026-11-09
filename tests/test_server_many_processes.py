"""Package 20.1: real Postgres locks and two server instances (20.2 adds processes)."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
import threading
import time

import pytest

from agenticml.server import app, ingest
from agenticml.server.db import DatabaseBusy
from agenticml.server.db.postgres import PostgresDatabase
from agenticml.server.db.usage import UsageRepo
from test_server_ingest import request, put_meta, append, run_path

pytestmark = pytest.mark.skipif(not os.environ.get('AGENTICML_TEST_DATABASE_URL'),
                                reason='AGENTICML_TEST_DATABASE_URL is not set')


@pytest.fixture
def databases(server_database):
    a, b = PostgresDatabase(server_database), PostgresDatabase(server_database)
    try:
        yield a, b
    finally:
        a.close()
        b.close()


@pytest.fixture
def servers(tmp_path, server_database, monkeypatch):
    a = app.make_server(tmp_path / 'data', port=0)
    # B has its own pool, just as a second process would.
    b_db = PostgresDatabase(server_database)
    monkeypatch.setattr(app, 'for_server_dir', lambda *args: b_db)
    b = app.make_server(tmp_path / 'data', port=0)
    _, a.test_key = a.keys.create('one')
    b.test_key = a.test_key
    threads = []
    for server in (a, b):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        threads.append(thread)
    try:
        assert a._server_lock is not None and b._server_lock is not None
        yield a, b
    finally:
        for server, thread in zip((a, b), threads):
            server.shutdown()
            thread.join(5)
            server.server_close()


def test_session_exclusion_timeout_and_release(databases):
    a, b = databases
    with a.session_lock('run:workspace:one'):
        with pytest.raises(DatabaseBusy):
            with b.session_lock('run:workspace:one', timeout=.05):
                pytest.fail('entered a held lock')
        # A different key can proceed independently.
        with b.session_lock('run:workspace:two', timeout=.2):
            pass
    with b.session_lock('run:workspace:one', timeout=.2):
        pass


def test_terminated_holder_releases_lock(databases, postgres_admin, monkeypatch):
    a, b = databases
    connections = []
    original = a._new_connection

    def connect(**kwargs):
        connection = original(**kwargs)
        if kwargs.get('lock'):
            connections.append(connection)
        return connection

    monkeypatch.setattr(a, '_new_connection', connect)
    with a.session_lock('killed'):
        holder = connections[-1]
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(holder.info.dsn)
        assert params['keepalives'] == '1' and params['keepalives_idle'] == '30'
        assert holder.execute('SHOW tcp_keepalives_idle').fetchone()[0] == '30'
        assert holder.execute('SHOW tcp_keepalives_interval').fetchone()[0] == '10'
        assert holder.execute('SHOW tcp_keepalives_count').fetchone()[0] == '3'
        assert holder.execute('SHOW tcp_user_timeout').fetchone()[0] == '30000'
        assert holder.execute('SHOW idle_session_timeout').fetchone()[0] == '0'
        pid = holder.execute('SELECT pg_backend_pid()').fetchone()[0]
        assert postgres_admin.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone()[0]
        with b.session_lock('killed', timeout=1):
            pass
    assert holder.closed


def test_session_admission_bounded_and_recovered(databases, monkeypatch):
    from agenticml.server.db import postgres
    assert postgres.SESSION_LOCK_LIMIT == 64
    monkeypatch.setattr(postgres, '_inner_slots', threading.BoundedSemaphore(1))
    a, b = databases
    with a.session_lock('one'):
        with pytest.raises(DatabaseBusy):
            with b.session_lock('two', timeout=.05):
                pass
    with b.session_lock('two', timeout=.2):
        pass


@pytest.mark.parametrize('kind', ['run', 'object', 'quota', 'lineage'])
def test_server_locks_exclude(servers, kind):
    a, b = servers
    wid = a.default_workspace_id

    def lock(server):
        if kind == 'quota':
            return server.quota_lock(wid)
        if kind == 'lineage':
            return server.lineage_lock(wid, 'project')
        return getattr(server, kind + '_lock')('same', workspace_id=wid)

    entered = threading.Event()
    attempting = threading.Event()

    def contender():
        attempting.set()
        with lock(b):
            entered.set()

    with ThreadPoolExecutor(1) as pool:
        with lock(a):
            future = pool.submit(contender)
            assert attempting.wait(2)
            assert not entered.wait(.1)
        future.result(timeout=3)
    assert entered.is_set()


def test_process_lock_acquire_release(servers):
    a, b = servers
    wid = a.default_workspace_id
    held = a.run_lock('run-1', workspace_id=wid)
    other = b.run_lock('run-1', workspace_id=wid)
    assert held.acquire(blocking=False)
    assert not other.acquire(blocking=False)
    held.release()
    assert other.acquire(blocking=True, timeout=1)
    other.release()
    free = a.run_lock('run-1', workspace_id=wid)
    assert free.acquire(blocking=False)
    free.release()


def test_record_file_cas_refuses_changed_size(servers):
    a, _ = servers
    assert put_meta(a)[0] == 200
    a.run_index.record_file('run-1', 'events.jsonl', 8)
    with pytest.raises(DatabaseBusy):
        a.run_index.record_file('run-1', 'events.jsonl', 16, expected_size=0)
    assert a.run_index.committed_size('run-1', 'events.jsonl') == 8
    a.run_index.record_file('run-1', 'events.jsonl', 16, expected_size=8)
    assert a.run_index.committed_size('run-1', 'events.jsonl') == 16


def test_record_file_cas_refuses_changed_sha256(servers):
    a, _ = servers
    assert put_meta(a)[0] == 200
    first = b'{"a":1}'
    second = b'{"b":1}'
    assert len(first) == len(second)
    sha_first = hashlib.sha256(first).hexdigest()
    sha_second = hashlib.sha256(second).hexdigest()
    a.run_index.record_file('run-1', 'scalars/index.json', len(first), sha256=sha_first)
    a.run_index.record_file('run-1', 'scalars/index.json', len(second), sha256=sha_second)
    with pytest.raises(DatabaseBusy):
        a.run_index.record_file(
            'run-1', 'scalars/index.json', len(first), sha256=sha_first,
            expected_size=len(first), expected_sha256=sha_first)
    recorded = a.run_index.files('run-1')['scalars/index.json']
    assert recorded['size'] == len(second) and recorded['sha256'] == sha_second


def _put_index(server, body):
    return request(server, '/v2/runs/run-1/files/scalars/index.json', 'PUT', body)


def test_terminated_lock_refuses_stale_atomic(servers, postgres_admin, monkeypatch):
    a, b = servers
    assert put_meta(a)[0] == 200
    first = b'{"a":1}'
    second = b'{"b":2}'
    stale = b'{"c":3}'
    assert _put_index(a, first)[0] == 200
    connections = _lock_connections(a.db, monkeypatch)
    original = a.db.confirm_session_locks

    def confirm():
        if connections:
            _kill_backend(connections[-1], postgres_admin)
            status, payload, _ = _put_index(b, second)
            assert status == 200 and payload['size'] == len(second)
        original()

    monkeypatch.setattr(a.db, 'confirm_session_locks', confirm)
    status, _, headers = _put_index(a, stale)
    assert status == 503
    assert headers['Retry-After'] == '5'
    assert run_path(a, 'scalars/index.json').read_bytes() == second
    recorded = a.run_index.files('run-1')['scalars/index.json']
    assert recorded['sha256'] == hashlib.sha256(second).hexdigest()


def test_stale_atomic_same_size_refused_via_sha256(servers, postgres_admin, monkeypatch):
    a, b = servers
    assert put_meta(a)[0] == 200
    first = b'{"x":"aa"}'
    other = b'{"x":"bb"}'
    stale = b'{"x":"cc"}'
    assert len(first) == len(other) == len(stale)
    assert first != other != stale
    assert _put_index(a, first)[0] == 200
    connections = _lock_connections(a.db, monkeypatch)
    swapped = []

    def confirm():
        if not swapped:
            swapped.append(True)
            _kill_backend(connections[-1], postgres_admin)
            status, _, _ = _put_index(b, other)
            assert status == 200
            return
        return

    monkeypatch.setattr(a.db, 'confirm_session_locks', confirm)
    status, _, headers = _put_index(a, stale)
    assert status == 503
    assert headers['Retry-After'] == '5'
    assert run_path(a, 'scalars/index.json').read_bytes() == other
    recorded = a.run_index.files('run-1')['scalars/index.json']
    assert recorded['size'] == len(other)
    assert recorded['sha256'] == hashlib.sha256(other).hexdigest()


def test_atomic_commit_failure_restores_old_content(servers, monkeypatch):
    a, _ = servers
    assert put_meta(a)[0] == 200
    first = b'{"k":"old"}'
    second = b'{"k":"new"}'
    assert _put_index(a, first)[0] == 200
    original = a.db.transaction
    published = []

    def after(run_id, name):
        published.append(run_path(a, name).read_bytes())

    monkeypatch.setattr(a, '_after_atomic_write', after)

    @contextmanager
    def wrapping(write=False):
        with original(write=write) as tx:
            yield tx
            if write and published:
                raise RuntimeError('injected commit failure')

    monkeypatch.setattr(a.db, 'transaction', wrapping)
    status, _, _ = _put_index(a, second)
    assert status == 500
    assert published == [second]
    assert run_path(a, 'scalars/index.json').read_bytes() == first
    recorded = a.run_index.files('run-1')['scalars/index.json']
    assert recorded['sha256'] == hashlib.sha256(first).hexdigest()


def test_terminated_lock_refuses_stale_record(servers, postgres_admin, monkeypatch):
    a, b = servers
    assert put_meta(a)[0] == 200
    first = b'{"a":1}\n'
    second = b'{"b":1}\n'
    stale = b'{"a":2}\n'
    assert append(a, first, name='events.jsonl')[0] == 200
    connections = []
    original = a.db._new_connection

    def connect(**kwargs):
        connection = original(**kwargs)
        if kwargs.get('lock'):
            connections.append(connection)
        return connection

    monkeypatch.setattr(a.db, '_new_connection', connect)

    def after_append(run_id, name):
        holder = connections[-1]
        pid = holder.execute('SELECT pg_backend_pid()').fetchone()[0]
        assert postgres_admin.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone()[0]
        status, payload, _ = append(b, second, offset=len(first), name='events.jsonl')
        assert status == 200 and payload['size'] == len(first) + len(second)

    monkeypatch.setattr(a, '_after_append_write', after_append)
    status, _, headers = append(a, stale, offset=len(first), name='events.jsonl')
    assert status == 503
    assert headers['Retry-After'] == '5'
    assert a.run_index.committed_size('run-1', 'events.jsonl') == len(first) + len(second)
    assert run_path(a, 'events.jsonl').read_bytes() == first + second


def test_timeout_real_request_returns_retry_after(servers, monkeypatch):
    a, b = servers
    original = b.db.session_lock
    monkeypatch.setattr(b.db, 'session_lock', lambda key, timeout=30: original(key, timeout=.05))
    with a.run_lock('run-1', workspace_id=a.default_workspace_id):
        status, _, headers = put_meta(b)
    assert status == 503
    assert headers['Retry-After'] == '5'
    assert put_meta(b)[0] == 200  # Thread lock and connection slot were released.


def test_tmp_cleanup(tmp_path, caplog):
    retention = app.tmp_retention_seconds()
    assert retention == pytest.approx(
        app.BODY_DEADLINE + app.OBJECT_BODY_LIMIT / app.BODY_BYTES_PER_SECOND + 3600)
    fresh = tmp_path / 'fresh.body'
    slow = tmp_path / 'slow.body'
    old = tmp_path / 'old.body'
    fresh.write_bytes(b'active')
    slow.write_bytes(b'inflight')
    old.write_bytes(b'abandoned')
    fresh_dir = tmp_path / 'lineage-fresh'
    old_dir = tmp_path / 'lineage-old'
    fresh_dir.mkdir()
    old_dir.mkdir()
    other_dir = tmp_path / 'other'
    other_dir.mkdir()
    os.utime(slow, (time.time() - 7200,) * 2)
    for entry in (old, old_dir, other_dir):
        os.utime(entry, (time.time() - retention - 60,) * 2)
    target = tmp_path.parent / (tmp_path.name + '-target')
    target.mkdir()
    (target / 'keep').write_bytes(b'safe')
    link = tmp_path / 'lineage-symlink'
    link.symlink_to(target, target_is_directory=True)
    os.utime(link, (time.time() - retention - 60,) * 2, follow_symlinks=False)
    app._clean_tmp(tmp_path, postgres=True)
    assert not link.is_symlink()
    assert (target / 'keep').read_bytes() == b'safe'
    assert 'ignoring subdirectory in server/tmp' in caplog.text
    assert fresh.exists() and slow.exists() and fresh_dir.exists() and other_dir.exists()
    assert not old.exists() and not old_dir.exists()


def test_tmp_cleanup_cutoff_keeps_legal_slow_body(tmp_path):
    retention = app.tmp_retention_seconds()
    keep = tmp_path / 'keep.body'
    drop = tmp_path / 'drop.body'
    keep.write_bytes(b'k')
    drop.write_bytes(b'd')
    os.utime(keep, (time.time() - retention + 60,) * 2)
    os.utime(drop, (time.time() - retention - 60,) * 2)
    app._clean_tmp(tmp_path, postgres=True)
    assert keep.exists() and not drop.exists()


def test_requests_exact_across_servers(servers):
    a, b = servers
    # An injected minute avoids a real minute boundary making this test flaky.
    minute = 123456
    wid = a.default_workspace_id
    with ThreadPoolExecutor(16) as pool:
        counts = list(pool.map(lambda n: UsageRepo((a, b)[n % 2].db, wid).count_request(minute)[0],
                               range(160)))
    assert sorted(counts) == list(range(1, 161))
    assert UsageRepo(a.db, wid).get()['request_count'] == 160


def test_revoked_key_and_suspension_seen_on_next_request(servers):
    a, b = servers
    assert request(a, '/v2/whoami')[0] == 200
    b.keys.revoke('one')
    assert request(a, '/v2/whoami')[0] == 401
    _, a.test_key = b.keys.create('two')
    assert request(a, '/v2/whoami')[0] == 200
    with b.db.transaction(write=True) as tx:
        tx.execute("UPDATE workspaces SET status='suspended' WHERE id=?", (a.default_workspace_id,))
    assert request(a, '/v2/whoami')[0] == 403


def test_period_keys_use_database_clock(servers, monkeypatch):
    from agenticml.server import limits
    from agenticml.server.db import usage as usage_module
    a, _ = servers
    wid = a.default_workspace_id
    fake = datetime(2001, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(limits, 'utc_now', lambda: fake)
    monkeypatch.setattr(usage_module, 'utc_day', lambda: '2001-01-01')
    plans = limits.Plans({'default_plan': 'free', 'free_workspaces_per_account': 1,
                         'plans': {'free': limits.UNLIMITED}})
    with a.db.transaction() as tx:
        before = tx.fetchone('SELECT CURRENT_TIMESTAMP')[0]
    assert plans.request(a.db, wid) is None
    usage = UsageRepo(a.db, wid)
    with a.db.transaction(write=True) as tx:
        stamp = tx.fetchone('SELECT CURRENT_TIMESTAMP')[0]
        usage.add_upload(9, tx)
        assert usage.get(tx)['upload_day'] == stamp.date().isoformat()
    with a.db.transaction() as tx:
        after = tx.fetchone('SELECT CURRENT_TIMESTAMP')[0]
    row = usage.get()
    assert row['request_minute'] in range(int(before.timestamp() // 60), int(after.timestamp() // 60) + 1)
    assert row['upload_day'] != '2001-01-01'
    # Explicit test clocks remain usable.
    with a.db.transaction(write=True) as tx:
        UsageRepo(a.db, wid, clock=lambda: '2099-01-01').add_upload(1, tx)
    assert usage.get()['upload_day'] == '2099-01-01'


def test_cached_reader_observes_committed_sizes(servers):
    a, b = servers
    assert put_meta(b)[0] == 200
    assert append(b, b'{"step":1}\n', name='events.jsonl')[0] == 200
    with a.run_reader('run-1', workspace_id=a.default_workspace_id) as reader:
        assert reader.events() == [{'step': 1}]
    # A's reader is cached, while B appends and records a new committed size.
    assert append(b, b'{"step":2}\n', offset=11, name='events.jsonl')[0] == 200
    path = reader.run_dir / 'events.jsonl'
    with path.open('ab') as handle:
        handle.write(b'{"step":999}\n')  # Crash tail, never committed.
    with a.run_reader('run-1', workspace_id=a.default_workspace_id) as cached:
        assert cached is reader
        assert cached.events() == [{'step': 1}, {'step': 2}]


def test_parallel_legacy_import(databases, tmp_path, monkeypatch):
    import json
    from agenticml.server.db.import2x import import_state
    from agenticml.server.db.repos import SettingsRepo
    a, b = databases
    directory = tmp_path / 'legacy' / 'server'
    directory.mkdir(parents=True)
    (directory / 'users.json').write_text(json.dumps({'version': 1, 'users': []}))
    (directory / 'setup-closed').touch()
    barrier = threading.Barrier(2)
    timeouts = []
    for database in (a, b):
        original = database.session_lock

        def lock(key, timeout=30, original=original):
            timeouts.append((key, timeout))
            return original(key, timeout=timeout)

        monkeypatch.setattr(database, 'session_lock', lock)

    def start(db):
        barrier.wait(timeout=3)
        import_state(db, directory)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(start, (a, b)))
    assert timeouts == [('import:2x', 600)] * 2
    assert SettingsRepo(a).get('imported_2x')
    assert SettingsRepo(b).get('imported_runs_2x')
    assert (directory / 'users.json.2x').exists()


def test_registry_available_during_connect(server_database, tmp_path, monkeypatch):
    from agenticml.server import db as registry
    entered, release = threading.Event(), threading.Event()
    original = registry.connect

    def connect(url):
        entered.set()
        assert release.wait(3)
        return original(url)

    monkeypatch.setattr(registry, 'connect', connect)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(registry.for_server_dir, tmp_path / 'server', server_database)
        try:
            assert entered.wait(3)
            assert registry._registry_lock.acquire(timeout=.2)
            registry._registry_lock.release()
        finally:
            release.set()
        db = future.result(timeout=5)
    db.close()


def test_lineage_git_outside_quota_and_accounting_inside(servers, monkeypatch):
    from agenticml.server import lineage
    from agenticml.server.limits import Plans, UNLIMITED
    a, b = servers
    wid = a.default_workspace_id
    a.plans = Plans({'default_plan': 'free', 'free_workspaces_per_account': 1,
                     'plans': {'free': UNLIMITED}})
    assert put_meta(a)[0] == 200
    monkeypatch.setattr(lineage, '_info', lambda meta: {'project_key': 'project'})
    original_lock = b.db.session_lock
    monkeypatch.setattr(b.db, 'session_lock', lambda key, timeout=30: original_lock(key, timeout=.05))

    def receive(*args):
        # Git work holds the lineage lock, while another run may account usage.
        with b.quota_lock(wid):
            pass
        with pytest.raises(DatabaseBusy):
            with b.lineage_lock(wid, 'project'):
                pass
        return {'created': True}, 200

    original_add = UsageRepo.add_upload
    seen = []

    def add_upload(self, length, tx):
        with pytest.raises(DatabaseBusy):
            with b.quota_lock(wid):
                pass
        seen.append(length)
        return original_add(self, length, tx)

    monkeypatch.setattr(lineage, 'receive', receive)
    monkeypatch.setattr(UsageRepo, 'add_upload', add_upload)
    assert request(a, '/v2/runs/run-1/lineage', 'PUT', b'bundle')[0] == 200
    assert seen == [6]


def test_cached_scalar_reader_excludes_uncommitted_tail(servers):
    import json
    from agenticml.store import layout
    a, b = servers
    assert put_meta(b)[0] == 200
    index = json.dumps({'version': 1, 'series': {'loss': {'id': 0, 'monotonic': True}}}).encode()
    assert request(b, '/v2/runs/run-1/files/scalars/index.json', 'PUT', index)[0] == 200
    record = layout.RECORD_STRUCT
    assert append(b, record.pack(1, 1.0))[0] == 200
    with a.run_reader('run-1', workspace_id=a.default_workspace_id) as reader:
        assert reader.series('loss')['total'] == 1
    assert append(b, record.pack(2, 2.0), offset=record.size)[0] == 200
    with (reader.run_dir / 'scalars' / '0.bin').open('ab') as handle:
        handle.write(record.pack(999, 999.0))
    with a.run_reader('run-1', workspace_id=a.default_workspace_id) as cached:
        assert cached.series('loss')['total'] == 2
        assert cached.tail('loss')['last_step'] == 2
        assert len(cached.read_records('loss')[0]) == 2


def test_concurrent_tmp_cleanup(tmp_path, monkeypatch):
    from pathlib import Path
    old = tmp_path / 'old.body'
    old.touch()
    os.utime(old, (time.time() - app.tmp_retention_seconds() - 60,) * 2)
    barrier = threading.Barrier(2)
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == old:
            barrier.wait(timeout=3)
        return result

    monkeypatch.setattr(Path, 'lstat', lstat)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: app._clean_tmp(tmp_path, postgres=True), range(2)))
    assert not old.exists()


def test_outer_capacity_leaves_room_for_inner(databases):
    from contextlib import ExitStack
    a, b = databases
    with ExitStack() as held:
        for n in range(48):
            held.enter_context(a.session_lock(f'run:capacity:{n}'))
        with pytest.raises(DatabaseBusy):
            with b.session_lock('object:capacity', timeout=.05):
                pass
        with b.session_lock('quota:capacity', timeout=1):
            pass


def test_reindex_refuses_until_every_server_closed(servers, capsys, tmp_path):
    from argparse import Namespace
    from agenticml.cli import cmd_server_reindex
    a, b = servers
    args = Namespace(data=str(tmp_path / 'data'), database=None, workspace=None)
    assert cmd_server_reindex(args) == 2
    assert 'stop every agenticml server using this database first' in capsys.readouterr().err
    a.shutdown()
    a.server_close()
    assert cmd_server_reindex(args) == 2
    assert 'stop every agenticml server using this database first' in capsys.readouterr().err
    b.shutdown()
    b.server_close()
    assert cmd_server_reindex(args) == 0


def test_usage_clock_advances_within_transaction(servers):
    a, _ = servers
    usage = UsageRepo(a.db, a.default_workspace_id)
    with a.db.transaction() as tx:
        before = usage.now(tx)
        tx.execute('SELECT pg_sleep(0.03)')
        assert (usage.now(tx) - before).total_seconds() >= .03


def test_request_clock_and_count_share_transaction(servers, monkeypatch):
    a, _ = servers
    original = UsageRepo.now
    seen = []

    def now(self, tx=None):
        assert tx is not None
        stamp = original(self, tx)
        seen.append(stamp)
        return stamp

    monkeypatch.setattr(UsageRepo, 'now', now)
    count, stamp = UsageRepo(a.db, a.default_workspace_id).count_request()
    assert count == 1 and seen == [stamp]

    from agenticml.server.limits import Plans, UNLIMITED
    plans = Plans({'default_plan': 'free', 'free_workspaces_per_account': 1,
                   'plans': {'free': UNLIMITED}})
    assert plans.request(a.db, a.default_workspace_id) is None
    assert len(seen) == 2


def test_import_lock_statement_timeout_allows_long_wait(databases, monkeypatch):
    a, _ = databases
    original = a._new_connection
    connections = []

    def connect(**kwargs):
        connection = original(**kwargs)
        if kwargs.get('lock'):
            connections.append(connection)
        return connection

    monkeypatch.setattr(a, '_new_connection', connect)
    with a.session_lock('import:timeout-test', timeout=600):
        connection = connections[-1]
        # GUC values are reported in milliseconds by pg_settings.
        settings = connection.execute(
            "SELECT name, setting::int FROM pg_settings "
            "WHERE name IN ('statement_timeout', 'lock_timeout')").fetchall()
        assert len(settings) == 2
        values = {row[0]: row[1] for row in settings}
        assert 590000 < values['lock_timeout'] <= 600000
        assert values['lock_timeout'] < values['statement_timeout'] <= 601000


def _lock_connections(database, monkeypatch):
    connections = []
    original = database._new_connection

    def connect(**kwargs):
        connection = original(**kwargs)
        if kwargs.get('lock'):
            connections.append(connection)
        return connection

    monkeypatch.setattr(database, '_new_connection', connect)
    return connections


def _kill_backend(connection, postgres_admin):
    pid = connection.execute('SELECT pg_backend_pid()').fetchone()[0]
    assert postgres_admin.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone()[0]


def test_lock_loss_before_write_writes_nothing(servers, postgres_admin, monkeypatch):
    a, _ = servers
    assert put_meta(a)[0] == 200
    first = b'{"a":1}\n'
    assert append(a, first, name='events.jsonl')[0] == 200
    connections = _lock_connections(a.db, monkeypatch)
    original = a.db.confirm_session_locks
    killed = []

    def confirm():
        if not killed:
            _kill_backend(connections[-1], postgres_admin)
            killed.append(True)
        original()

    monkeypatch.setattr(a.db, 'confirm_session_locks', confirm)
    status, _, headers = append(a, b'{"b":1}\n', offset=len(first), name='events.jsonl')
    assert status == 503
    assert headers['Retry-After'] == '5'
    assert a.run_index.committed_size('run-1', 'events.jsonl') == len(first)
    assert run_path(a, 'events.jsonl').read_bytes() == first


def test_lock_loss_mid_append_truncates_to_committed(servers, postgres_admin, monkeypatch):
    a, _ = servers
    assert put_meta(a)[0] == 200
    first = b'{"a":1}\n'
    assert append(a, first, name='events.jsonl')[0] == 200
    connections = _lock_connections(a.db, monkeypatch)
    monkeypatch.setattr(ingest, '_LOCK_CONFIRM_BYTES', 8)
    original = a.db.confirm_session_locks
    calls = []

    def confirm():
        calls.append(1)
        if len(calls) == 2:
            _kill_backend(connections[-1], postgres_admin)
        original()

    monkeypatch.setattr(a.db, 'confirm_session_locks', confirm)
    payload = b'{"b":2}\n' * 100
    status, _, headers = append(a, payload, offset=len(first), name='events.jsonl')
    assert status == 503
    assert headers['Retry-After'] == '5'
    assert len(calls) >= 2
    assert a.run_index.committed_size('run-1', 'events.jsonl') == len(first)
    assert run_path(a, 'events.jsonl').read_bytes() == first


def test_verify_after_write_refuses_foreign_bytes(servers, monkeypatch, caplog):
    a, _ = servers
    assert put_meta(a)[0] == 200
    first = b'{"a":1}\n'
    assert append(a, first, name='events.jsonl')[0] == 200
    committed = len(first)
    original = a.db.confirm_session_locks
    injected = []

    def confirm():
        original()
        path = run_path(a, 'events.jsonl')
        size = path.stat().st_size
        if size > committed and not injected:
            with open(path, 'r+b') as handle:
                handle.seek(committed)
                handle.write(b'CORRUPT!')
            injected.append(True)

    monkeypatch.setattr(a.db, 'confirm_session_locks', confirm)
    monkeypatch.setattr(ingest, '_LOCK_CONFIRM_BYTES', 1)
    payload = b'{"b":2}\n' * 20
    status, _, headers = append(a, payload, offset=committed, name='events.jsonl')
    assert injected and status == 503
    assert headers['Retry-After'] == '5'
    assert 'concurrent writer detected' in caplog.text
    on_disk = run_path(a, 'events.jsonl').read_bytes()
    assert a.run_index.committed_size('run-1', 'events.jsonl') == committed
    assert on_disk.startswith(first)
    assert len(on_disk) > committed


def test_out_of_order_session_lock_release_keeps_stack(databases):
    from agenticml.server.db import postgres
    a, _ = databases
    outer_cm = a.session_lock('outer-stack')
    inner_cm = a.session_lock('inner-stack')
    outer = inner = None
    try:
        outer = outer_cm.__enter__()
        inner = inner_cm.__enter__()
        assert postgres._held_lock_connections.stack == [outer, inner]
        outer_cm.__exit__(None, None, None)
        outer = None
        assert postgres._held_lock_connections.stack == [inner]
        a.confirm_session_locks()
        assert not inner.closed
    finally:
        if inner is not None:
            inner_cm.__exit__(None, None, None)
        if outer is not None:
            outer_cm.__exit__(None, None, None)
    assert postgres._held_lock_connections.stack == []

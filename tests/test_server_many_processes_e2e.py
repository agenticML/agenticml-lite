"""Package 20.2: real CLI processes, crash recovery, and reference parity.

Run with AGENTICML_TEST_DATABASE_URL; every child uses the harness's isolated
schema, never the administrator URL. Logs survive failures under tmp_path.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import requests

from requests.adapters import HTTPAdapter

from agenticml import sync
from agenticml.client import Client
from agenticml.server.db.postgres import PostgresDatabase
from agenticml.server.db.workspaces import WorkspaceRepo
from agenticml.server.db.usage import UsageRepo
from agenticml.server.workspaces import store_root
from agenticml.store import RunReader, RunWriter
from agenticml.store.layout import object_path
from agenticml.store.reader import run_references

pytestmark = pytest.mark.skipif(
    not os.environ.get('AGENTICML_TEST_DATABASE_URL'),
    reason='AGENTICML_TEST_DATABASE_URL is not set')
ROOT = Path(__file__).resolve().parents[1]
ENTRY = 'from agenticml.cli import main; raise SystemExit(main())'


def child_env():
    env = os.environ.copy()
    # Keep the per-test schema URL, but discard unrelated operator settings.
    for name in list(env):
        if name.startswith('AGENTICML_') and name != 'AGENTICML_SERVER_DATABASE_URL':
            env.pop(name)
    env['PYTHONPATH'] = str(ROOT / 'src')
    env['PYTHONUNBUFFERED'] = '1'
    return env


def cli(data, env, *args):
    result = subprocess.run([sys.executable, '-c', ENTRY, 'server', *args,
                             '--data', str(data)], env=env, cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, data, env, log, port=None):
        self.port = free_port() if port is None else port
        self.url = f'http://127.0.0.1:{self.port}'
        self.log = log
        self.output = log.open('w')
        self.process = subprocess.Popen(
            [sys.executable, '-c', ENTRY, 'server', 'serve', '--data', str(data),
             '--port', str(self.port)], env=env, cwd=ROOT,
            stdout=self.output, stderr=subprocess.STDOUT)

    def ready(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert self.process.poll() is None, self.log.read_text()
            try:
                if requests.get(self.url + '/healthz', timeout=.25).status_code == 200:
                    return self
            except requests.ConnectionError:
                pass
            time.sleep(.02)
        pytest.fail('server did not become ready: ' + self.log.read_text())

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.output.close()


def allowed(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes()
            for p in directory.rglob('*') if p.is_file() and
            (sync._ATOMIC.fullmatch(p.relative_to(directory).as_posix()) or
             sync._APPEND.fullmatch(p.relative_to(directory).as_posix()))}


def normalized(value):
    """Drop only server timestamps, key names and workspace identity."""
    if isinstance(value, str):
        try:
            return normalized(json.loads(value))
        except (ValueError, TypeError):
            return value
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k not in {
            'workspace_id', 'owner_key', 'last_sync_key', 'last_sync_at',
            'updated_at', 'created_at', 'synced_at', 'last_synced_at'}}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def rows(db, table, wid, order):
    with db.transaction() as tx:
        tx.bind_workspace(wid)
        return [normalized(dict(row)) for row in tx.fetchall(
            f'SELECT * FROM {table} WHERE workspace_id=? ORDER BY {order}', (wid,))]


def test_two_process_sync_kill_restart_matches_single_process(tmp_path, server_database):
    assert server_database == os.environ['AGENTICML_SERVER_DATABASE_URL']
    env = child_env()
    data = tmp_path / 'data'
    keys = {}
    cli(data, env, 'accounts', 'add', 'owner', '--server-admin')
    for workspace in ('parallel', 'reference'):
        cli(data, env, 'workspaces', 'create', workspace)
        cli(data, env, 'users', 'add', 'trainer', '--workspace', workspace, '--no-invite')
        keys[workspace] = cli(data, env, 'keys', 'create', '--workspace', workspace,
                              '--name', workspace + '-key', '--user', 'trainer')

    source = tmp_path / 'source'
    run_ids = [f'run-{i:02}' for i in range(12)]
    artifact = tmp_path / 'weights.bin'
    artifact.write_bytes(b'shared weights\x00' * 128)
    for i, run_id in enumerate(run_ids):
        writer = RunWriter.create(source, run_id=run_id, meta={'project': 'many', 'name': run_id})
        try:
            writer.write_scalars([(n, {'loss': 1 / (n + 1), 'acc': n / 64}) for n in range(64)])
            writer.add_media('preview', i, b'shared preview', 'text/plain', 'sample')
            writer.add_artifact(artifact, 'weights', 'model', {}, copy=True)
            writer.finish('finished')
        finally:
            writer.close()
    # Different local roots are essential: sync_run locks local runs too.
    copies = []
    for name in ('client-a', 'client-b', 'reference-client'):
        target = tmp_path / name
        shutil.copytree(source, target)
        copies.append(target)

    acks, ack_lock = [], threading.Lock()
    duplicate_barrier = threading.Barrier(2)
    both_appending = threading.Event()
    racing_clients = []

    class RaceAdapter(HTTPAdapter):
        def __init__(self, client):
            super().__init__()
            self._client = client

        def send(self, request, **kwargs):
            if '/append/' in request.url and self._client.race:
                self._client.race = False
                duplicate_barrier.wait(timeout=10)
                with ack_lock:
                    racing_clients.append(self._client.base_url)
                    if len(set(racing_clients)) == 2:
                        both_appending.set()
            return super().send(request, **kwargs)

    class RecordingClient(Client):
        def __init__(self, *args, race=False, **kwargs):
            super().__init__(*args, **kwargs)
            self.race = race
            if race:
                self.session.mount('http://', RaceAdapter(self))

        def _request(self, method, url, **kwargs):
            response = super()._request(method, url, **kwargs)
            if '/append/' in url and response.status_code == 200:
                parsed = urlsplit(url)
                run_id, name = unquote(parsed.path).split('/v2/runs/')[1].split('/append/')
                offset = int(parse_qs(parsed.query)['offset'][0])
                with ack_lock:
                    acks.append((run_id, name, offset, bytes(kwargs['data'])))
            return response

    def push(root, run_id, server, key, race=False):
        with RecordingClient(server.url, key, timeout=10, max_retries=0, race=race) as client:
            result = sync.SyncResult(run_id)
            sync.sync_run(RunReader(root, run_id), server.url, client, result)
            return result

    with ExitStack() as stack:
        db = PostgresDatabase(server_database)
        stack.callback(db.close)
        wid = WorkspaceRepo(db).require('parallel')['id']
        ref_wid = WorkspaceRepo(db).require('reference')['id']
        parallel_store = store_root(data, wid)
        limits = tmp_path / 'limits.json'
        limits.write_text(json.dumps({
            'default_plan': 'free', 'free_workspaces_per_account': 100,
            'plans': {'free': {
                'storage_bytes': 10 ** 12, 'upload_bytes_per_day': 10 ** 12,
                'requests_per_minute': 10 ** 6, 'people': 1000}}}))
        limits_env = dict(env, AGENTICML_SERVER_LIMITS=str(limits))
        slow_env = dict(limits_env, AGENTICML_TEST_HOOKS='1', AGENTICML_TEST_APPEND_DELAY_MS='1500')

        def start(name, environment, port=None):
            server = Server(data, environment, tmp_path / (name + '.log'), port)
            stack.callback(server.close)
            return server.ready()

        a, b = start('a', slow_env), start('b', slow_env)
        assert a.port != b.port
        # Even an external flock contender must succeed in Postgres mode.
        with (data / 'server' / 'server.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)

        headers = {'Authorization': 'Bearer ' + keys['parallel']}
        def ping(server):
            assert requests.get(server.url + '/v2/whoami', headers=headers, timeout=5).status_code == 200
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda i: ping((a, b)[i % 2]), range(24)))
        assert UsageRepo(db, wid).get()['request_count'] == 24

        with ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(push, copies[i % 2], rid, (a, b)[i % 2], keys['parallel'])
                       for i, rid in enumerate(run_ids[:9])]
            for future in futures:
                result = future.result(timeout=30)
                assert result.complete, result
            assert acks, 'must acknowledge appends before killing A'
            # Duplicate run-09 through A and B first: the workspace quota lock
            # is held across the append delay, so starting run-10 at the same
            # time can commit its first append before both_appending is set.
            race_b = pool.submit(push, copies[1], run_ids[9], b, keys['parallel'], True)
            later = pool.submit(push, copies[1], run_ids[11], b, keys['parallel'])
            race_a = pool.submit(push, copies[0], run_ids[9], a, keys['parallel'], True)
            race_futures = (race_b, race_a)
            assert both_appending.wait(15), 'both duplicate appends must be in flight'
            assert set(racing_clients) == {a.url, b.url}
            slow = pool.submit(push, copies[0], run_ids[10], a, keys['parallel'])
            futures = [race_b, slow, later, race_a]
            deadline = time.monotonic() + 15
            marker = 'AGENTICML_TEST_APPEND_WRITE ' + run_ids[10] + ' '
            gap = None
            while gap is None:
                assert time.monotonic() < deadline, a.log.read_text()
                assert not any(future.done() for future in race_futures)
                for line in reversed(a.log.read_text().splitlines()):
                    if not line.startswith(marker):
                        continue
                    name = line.split(' ', 2)[2]
                    path = parallel_store / 'runs' / run_ids[10] / name
                    try:
                        size = path.stat().st_size
                    except FileNotFoundError:
                        break
                    with db.transaction() as tx:
                        tx.bind_workspace(wid)
                        row = tx.fetchone(
                            'SELECT size FROM run_files WHERE workspace_id=? AND run_id=? AND path=?',
                            (wid, run_ids[10], name))
                    committed = row['size'] if row else 0
                    if size > committed:
                        gap = path, committed
                    break
                time.sleep(.01)
            path, committed = gap
            assert path.stat().st_size > committed
            a.process.send_signal(signal.SIGKILL)
            a.process.wait(timeout=5)
            assert a.process.returncode == -signal.SIGKILL
            # Finish client response handling before taking the acknowledgment snapshot.
            interrupted = 0
            for future, target in zip(futures, (b, a, b, a)):
                try:
                    result = future.result(timeout=20)
                    assert not result.permanent, result
                except requests.ConnectionError:
                    assert target is a, 'only the killed server may disconnect a client'
                    interrupted += 1
            assert interrupted, 'the kill must interrupt a real sync client'
            with ack_lock:
                before_repair = list(acks)
            # Verify against disk + DB before restarting A (no reconcile yet).
            for rid, name, offset, body in before_repair:
                path = parallel_store / 'runs' / rid / name
                assert path.read_bytes()[offset:offset + len(body)] == body
                with db.transaction() as tx:
                    tx.bind_workspace(wid)
                    row = tx.fetchone('SELECT size FROM run_files WHERE workspace_id=? AND run_id=? AND path=?',
                                      (wid, rid, name))
                assert row is not None and row['size'] >= offset + len(body)
            a = start('a-restarted', limits_env, a.port)

            # Bounded recovery passes, every run through each server, independent cursors.
            for server, root in ((a, copies[0]), (b, copies[1])):
                pending = list(run_ids)
                for _ in range(3):
                    results = list(pool.map(lambda rid: push(root, rid, server, keys['parallel']), pending))
                    assert not any(r.permanent for r in results), results
                    pending = [rid for rid, result in zip(pending, results) if not result.complete]
                    if not pending:
                        break
                assert not pending, pending

        # A alone writes the reference into a fresh workspace, with a fresh key.
        b.close()
        for rid in run_ids:
            result = push(copies[2], rid, a, keys['reference'])
            assert result.complete, result
        reference_store = store_root(data, ref_wid)
        for rid in run_ids:
            expected = allowed(source / 'runs' / rid)
            assert {'meta.json', 'events.jsonl', 'media/index.json', 'scalars/index.json'} <= expected.keys()
            assert allowed(parallel_store / 'runs' / rid) == expected
            assert allowed(reference_store / 'runs' / rid) == expected
            for sha in set().union(*run_references(source, rid).values()):
                expected_bytes = object_path(source, sha).read_bytes()
                assert object_path(parallel_store, sha).read_bytes() == expected_bytes
                assert object_path(reference_store, sha).read_bytes() == expected_bytes
        for table, order in (('runs', 'run_id'), ('run_files', 'run_id, path'), ('objects', 'sha256')):
            actual, expected = rows(db, table, wid, order), rows(db, table, ref_wid, order)
            assert actual and actual == expected, (table, actual, expected)
        parallel_usage, reference_usage = UsageRepo(db, wid).get(), UsageRepo(db, ref_wid).get()
        stored = parallel_usage['stored_bytes']
        assert stored > 0 and stored == reference_usage['stored_bytes']
        # Accepted bodies count even on overlap replay (see
        # test_mixed_ingest_exact_storage_and_daily_uploads), so the
        # duplicate sync and crash retries exceed a clean single-process path.
        uploaded, reference_uploaded = parallel_usage['upload_bytes'], reference_usage['upload_bytes']
        assert uploaded >= reference_uploaded > 0
        assert uploaded < 2 * reference_uploaded
        with db.transaction() as tx:
            tx.bind_workspace(wid)
            total = tx.fetchone('SELECT (SELECT SUM(size) FROM run_files WHERE workspace_id=?) + '
                                '(SELECT SUM(size) FROM objects WHERE workspace_id=?) AS n', (wid, wid))['n']
        assert stored == total


def test_sqlite_second_cli_process_still_refuses(tmp_path):
    env = child_env()
    env.pop('AGENTICML_SERVER_DATABASE_URL', None)
    with ExitStack() as stack:
        first = Server(tmp_path / 'sqlite', env, tmp_path / 'sqlite-first.log')
        stack.callback(first.close)
        first.ready()
        second = Server(tmp_path / 'sqlite', env, tmp_path / 'sqlite-second.log')
        stack.callback(second.close)
        assert second.process.wait(timeout=10) != 0
        assert 'another agenticml server is using' in second.log.read_text()
        assert first.process.poll() is None


@pytest.mark.parametrize('enabled,delay,pytest_test,expected', [
    (None, '1500', 't', []),
    ('0', '1500', 't', []),
    ('1', None, 't', []),
    ('1', '1500', None, []),
    ('1', '1500', 't', [1.5]),
])
def test_append_delay_requires_hooks_and_pytest(monkeypatch, capsys, enabled, delay, pytest_test, expected):
    from agenticml.server import app
    for key, value in (('AGENTICML_TEST_HOOKS', enabled), ('AGENTICML_TEST_APPEND_DELAY_MS', delay),
                       ('PYTEST_CURRENT_TEST', pytest_test)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    sleeps = []
    monkeypatch.setattr(app.time, 'sleep', sleeps.append)
    app.IngestServer._after_append_write(None, 'run', 'events.jsonl')
    assert sleeps == expected
    assert bool(capsys.readouterr().out) == bool(expected)

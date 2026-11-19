"""Package 31: server totals, daily history, last activity, refresher, CLI and admin API."""
from datetime import datetime, timedelta, timezone
import json
import contextlib
import logging
import re
import secrets
import shutil
import threading
import time
from types import SimpleNamespace

import pytest
from server_db_helpers import on_postgres

from agenticml import cli
from agenticml.server import app, stats as stats_module
from agenticml.server.app import prepare_data_dir
from agenticml.server.db import DatabaseError, connect, for_server_dir, migrate
from agenticml.server.db.repos import KeyRepo, SessionRepo
from agenticml.server.db.stats import ACTIVE_RESOLUTION, COLUMNS, StatsRepo, mark_account_active
from agenticml.server.db.workspaces import AccountRepo, WorkspaceRepo
from agenticml.server.keys import KeyStore
from agenticml.server.stats import STATS_REFRESH, StatsRefresher
from agenticml.store import layout
from test_server_db import url
from test_server_web import call
from test_server_workspace_routes import _authed, _password_cookie, _snapshot, two  # noqa: F401

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
STAMP = layout.format_timestamp


def ago(now, **delta):
    return STAMP(now - timedelta(**delta))


def seed(db, now):
    """4 accounts (created 1h, 3d, 20d, 40d ago), 3 workspaces, usage and 3 runs."""
    accounts = AccountRepo(db)
    ids = [accounts.create(login)['id'] for login in ('ada', 'bob', 'cy', 'dee')]
    workspaces = WorkspaceRepo(db)
    wids = {slug: workspaces.create(slug, owner=owner)['id']
            for slug, owner in (('alpha', 'ada'), ('beta', 'bob'), ('gamma', 'cy'))}
    day, yesterday = now.date().isoformat(), (now - timedelta(days=1)).date().isoformat()
    with db.transaction(write=True) as tx:
        tx.cross_workspace('test seed')
        for aid, created, active in zip(ids, (ago(now, hours=1), ago(now, days=3), ago(now, days=20),
                                              ago(now, days=40)),
                                        (ago(now, hours=2), ago(now, days=3), ago(now, days=20), None)):
            tx.execute('UPDATE accounts SET created_at=?, last_active_at=? WHERE id=?',
                       (created, active, aid))
        tx.execute("UPDATE workspaces SET status='suspended', plan='custom' WHERE id=?", (wids['beta'],))
        tx.execute("UPDATE workspaces SET status='suspended', plan='custom', archived_at=? WHERE id=?",
                   (ago(now, days=2), wids['gamma']))
        for slug, stored, upload_day, uploaded in (('alpha', 1000, day, 500), ('beta', 2000, yesterday, 700),
                                                   ('gamma', 4000, day, 250)):
            tx.execute(db.dialect.upsert(
                'workspace_usage', ('workspace_id', 'stored_bytes', 'upload_day', 'upload_bytes'),
                ('workspace_id',), ('stored_bytes', 'upload_day', 'upload_bytes')),
                (wids[slug], stored, upload_day, uploaded))
        for slug, run_id in (('alpha', 'r1'), ('alpha', 'r2'), ('beta', 'r3')):
            tx.execute('INSERT INTO runs (workspace_id, run_id, list_row, updated_at) VALUES (?, ?, ?, ?)',
                       (wids[slug], run_id, '{}', STAMP(now)))
    return ids, wids


@pytest.fixture
def db(tmp_path):
    database = connect(url(tmp_path / 'state.db'))
    yield database
    database.close()


def test_totals_on_an_empty_database(db):
    # a fresh database holds only the default workspace, with no accounts or data
    totals = StatsRepo(db).totals(now=NOW)
    assert totals == {
        'as_of': STAMP(NOW), 'accounts': 0, 'new_accounts_1d': 0, 'new_accounts_7d': 0,
        'active_1d': 0, 'active_7d': 0, 'active_30d': 0, 'workspaces_active': 1,
        'workspaces_suspended': 0, 'workspaces_archived': 0, 'workspaces_custom': 0,
        'stored_bytes': 0, 'upload_bytes_today': 0, 'runs': 0}
    assert StatsRepo(db).top_workspaces() == [
        {'slug': 'default', 'name': 'Default', 'stored_bytes': 0, 'archived': False}]
    assert StatsRepo(db).history(90, now=NOW) == []


def test_totals_are_exact_across_workspaces(db):
    seed(db, NOW)
    totals = StatsRepo(db).totals(now=NOW)
    assert totals == {
        'as_of': STAMP(NOW), 'accounts': 4, 'new_accounts_1d': 1, 'new_accounts_7d': 2,
        'active_1d': 1, 'active_7d': 2, 'active_30d': 3,
        'workspaces_active': 2, 'workspaces_suspended': 1, 'workspaces_archived': 1,
        'workspaces_custom': 1, 'stored_bytes': 7000, 'upload_bytes_today': 750, 'runs': 3}
    assert all(isinstance(value, int) for key, value in totals.items() if key != 'as_of')
    assert StatsRepo(db).top_workspaces(2) == [
        {'slug': 'gamma', 'name': 'gamma', 'stored_bytes': 4000, 'archived': True},
        {'slug': 'beta', 'name': 'beta', 'stored_bytes': 2000, 'archived': False}]
    assert [row['slug'] for row in StatsRepo(db).top_workspaces()] == ['gamma', 'beta', 'alpha', 'default']


def test_mark_account_active_sets_once_per_hour_and_ignores_none(db):
    account = AccountRepo(db).create('ada')['id']
    start = 1_800_000_000.0

    def mark(at, who=account):
        with db.transaction(write=True) as tx:
            mark_account_active(tx, who, at)
        return AccountRepo(db).get('ada')['last_active_at']

    assert AccountRepo(db).get('ada')['last_active_at'] is None
    first = mark(start)
    assert first == STAMP(datetime.fromtimestamp(start, timezone.utc))
    assert mark(start + ACTIVE_RESOLUTION // 2) == first
    assert mark(start + ACTIVE_RESOLUTION) == first
    assert mark(start + ACTIVE_RESOLUTION + 1) == STAMP(
        datetime.fromtimestamp(start + ACTIVE_RESOLUTION + 1, timezone.utc))
    with db.transaction(write=True) as tx:
        mark_account_active(tx, None, start)
        assert tx.fetchone('SELECT COUNT(*) FROM accounts WHERE last_active_at IS NULL')[0] == 0


def _session(account_id, seen):
    return {'id': account_id, 'workspace_id': None, 'generation': 0, 'via': 'password', 'key': None,
            'key_sha256': None, 'created': seen, 'last_seen': seen}


def test_session_create_and_touch_mark_the_right_account(db):
    accounts = AccountRepo(db)
    ada = accounts.create('ada')['id']
    accounts.create('bob')
    sessions = SessionRepo(db)
    start = 1_800_000_000.0
    sessions.create('d' * 64, _session(ada, start), 100)
    assert accounts.get('ada')['last_active_at'] == STAMP(datetime.fromtimestamp(start, timezone.utc))
    assert accounts.get('bob')['last_active_at'] is None
    sessions.create('e' * 64, _session(None, start), 100)
    assert accounts.get('bob')['last_active_at'] is None
    with db.transaction(write=True) as tx:
        tx.execute('UPDATE accounts SET last_active_at=NULL')
    sessions.touch('d' * 64, start + 30)
    assert accounts.get('ada')['last_active_at'] is None
    sessions.touch('d' * 64, start + 120)
    assert accounts.get('ada')['last_active_at'] == STAMP(datetime.fromtimestamp(start + 120, timezone.utc))
    assert accounts.get('bob')['last_active_at'] is None
    sessions.touch('e' * 64, start + 120)
    assert accounts.get('bob')['last_active_at'] is None


def test_key_touch_marks_its_account_and_env_key_marks_nothing(db, tmp_path):
    accounts = AccountRepo(db)
    accounts.create('bob')
    accounts.create('ada')
    workspace = WorkspaceRepo(db).create('lab', owner='ada')
    secret = secrets.token_urlsafe(32)
    keys = KeyStore(tmp_path / 'server', db=db, workspace_id=workspace['id'])
    _, key = keys.create('laptop', user='ada')
    record = keys.verify(key)
    assert record.name == 'laptop' and accounts.get('ada')['last_active_at'] is None
    keys.touch(record)
    stamp = accounts.get('ada')['last_active_at']
    assert stamp is not None
    assert abs((datetime.now(timezone.utc) - layout.parse_timestamp(stamp)).total_seconds()) < 60
    assert accounts.get('bob')['last_active_at'] is None
    with db.transaction(write=True) as tx:
        tx.execute('UPDATE accounts SET last_active_at=NULL')
    default = WorkspaceRepo(db).get('default')['id']
    env = KeyStore(tmp_path / 'server', env_key=secret, db=db, workspace_id=default).verify(secret)
    assert env.name == 'env'
    KeyStore(tmp_path / 'server', env_key=secret, db=db, workspace_id=default).touch(env)
    assert accounts.get('ada')['last_active_at'] is None


def _database_before_0010(tmp_path):
    prepare_data_dir(tmp_path)
    old = tmp_path / 'old-migrations'
    old.mkdir()
    for path in migrate.MIGRATIONS.glob('[0-9][0-9][0-9][0-9]_*'):
        if int(path.name[:4]) <= 9 and path.suffix in ('.sql', '.py'):
            shutil.copyfile(path, old / path.name)
    target = url(tmp_path / 'server' / 'agenticml.db')
    real = migrate.MIGRATIONS
    migrate.MIGRATIONS = old
    try:
        return connect(target), target
    finally:
        migrate.MIGRATIONS = real


_postgres = pytest.mark.skipif(not on_postgres(), reason='AGENTICML_TEST_DATABASE_URL is not set')


@contextlib.contextmanager
def _hold_account_row(db, account_id):
    """Another transaction holds the account row lock (like an UPDATE of a non-key column) until the block ends.

    FOR UPDATE would also block the foreign key check of a session insert, which is not what this guards.
    """
    held, release, errors = threading.Event(), threading.Event(), []

    def hold():
        try:
            with db.transaction(write=True) as tx:
                tx.cross_workspace('test hold')
                tx.fetchone('SELECT id FROM accounts WHERE id=? FOR NO KEY UPDATE', (account_id,))
                held.set()
                release.wait(30)
        except BaseException as exc:  # reported by the test
            errors.append(exc)
            held.set()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(10) and not errors
        yield
    finally:
        release.set()
        thread.join(30)
    assert not errors


def _timed(function, *args):
    started = time.monotonic()
    function(*args)
    return time.monotonic() - started


@_postgres
def test_activity_marking_never_waits_on_a_locked_account_row(db, tmp_path):
    accounts = AccountRepo(db)
    ada = accounts.create('ada')['id']
    workspace = WorkspaceRepo(db).create('lab', owner='ada')
    keys = KeyStore(tmp_path / 'server', db=db, workspace_id=workspace['id'])
    _, secret = keys.create('laptop', user='ada')
    record = keys.verify(secret)
    assert record.account_id == ada
    sessions = SessionRepo(db)
    start = time.time()
    sessions.create('a' * 64, _session(ada, start - 500), 100)
    with db.transaction(write=True) as tx:
        tx.execute('UPDATE accounts SET last_active_at=NULL')
    assert accounts.get('ada')['last_active_at'] is None

    def key_used():
        with db.transaction() as tx:
            tx.bind_workspace(workspace['id'])
            return tx.fetchone('SELECT last_used_at FROM keys WHERE name=?', ('laptop',))[0]

    def seen(digest):
        with db.transaction() as tx:
            tx.cross_workspace('test read')
            return tx.fetchone('SELECT last_seen FROM sessions WHERE token_sha256=?', (digest,))[0]

    with _hold_account_row(db, ada):
        assert _timed(keys.touch, record) < 2
        assert _timed(sessions.touch, 'a' * 64, start) < 2
        assert _timed(sessions.create, 'b' * 64, _session(ada, start), 100) < 2
        assert key_used() is not None
        assert seen('a' * 64) == start and seen('b' * 64) == start
    assert accounts.get('ada')['last_active_at'] is None  # skipped while the row was held
    sessions.touch('a' * 64, start + ACTIVE_RESOLUTION)
    assert accounts.get('ada')['last_active_at'] == STAMP(
        datetime.fromtimestamp(start + ACTIVE_RESOLUTION, timezone.utc))


@_postgres
def test_key_touch_does_not_deadlock_with_deleting_the_creator(db, tmp_path):
    """touch locks keys then accounts; delete locks accounts then keys. Marking must not wait."""
    accounts = AccountRepo(db)
    creator = accounts.create('boss')['id']
    member = accounts.create('ada', created_by='boss')['id']
    workspace = WorkspaceRepo(db).create('lab', owner='ada')
    keys = KeyStore(tmp_path / 'server', db=db, workspace_id=workspace['id'])
    keys.create('laptop', user='ada', created_by='boss')
    holding, go, outcome = threading.Event(), threading.Event(), {}

    def touch_like():
        try:
            with db.transaction(write=True) as tx:
                tx.bind_workspace(workspace['id'])
                tx.execute('UPDATE keys SET last_used_at=? WHERE name=?', (STAMP(NOW), 'laptop'))
                holding.set()  # the keys row is locked, as in KeyRepo.touch
                go.wait(30)
                started = time.monotonic()
                mark_account_active(tx, member, time.time())
                outcome['mark'] = time.monotonic() - started
        except BaseException as exc:
            outcome['error'] = exc
            holding.set()

    def delete():
        try:
            AccountRepo(db).delete('boss')
        except BaseException as exc:
            outcome['delete_error'] = exc

    toucher = threading.Thread(target=touch_like)
    toucher.start()
    assert holding.wait(10) and 'error' not in outcome
    deleter = threading.Thread(target=delete)
    deleter.start()
    deleter.join(1)
    assert deleter.is_alive()  # holds the member's account row, waits for the keys row
    go.set()
    toucher.join(20)
    deleter.join(20)
    assert not toucher.is_alive() and not deleter.is_alive()
    assert 'error' not in outcome and 'delete_error' not in outcome, outcome
    assert outcome['mark'] < 2
    assert accounts.get('boss') is None and accounts.get('ada')['created_by'] != 'boss'
    assert creator and accounts.get('ada')['id'] == member


def test_migration_0010_backfills_last_active_at(tmp_path):
    db, target = _database_before_0010(tmp_path)
    accounts = AccountRepo(db)
    ids = {login: accounts.create(login)['id'] for login in ('one', 'two', 'three')}
    wids = {login: WorkspaceRepo(db).create('lab-' + login, owner=login)['id'] for login in ('one', 'two')}

    def epoch(day):
        return datetime(2026, 11, day, tzinfo=timezone.utc).timestamp()

    with db.transaction(write=True) as tx:
        assert tx.fetchone('SELECT MAX(version) FROM schema_migrations')[0] == 9
        tx.cross_workspace('test seed')
        for login, used in (('one', '2026-11-10T00:00:00.000000Z'), ('two', '2026-11-15T00:00:00.000000Z')):
            tx.execute('INSERT INTO keys (workspace_id, name, account_id, scope, sha256, prefix, created_at, '
                       'created_by, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (wids[login], 'k', ids[login], 'sync', login * 32, 'aml_', used, 'cli', used))
        for n, (login, seen) in enumerate((('one', epoch(12)), ('two', epoch(11)))):
            tx.execute('INSERT INTO sessions (token_sha256, account_id, generation, via, created, last_seen, '
                       'created_order) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (str(n) * 64, ids[login], 0, 'password', seen, seen, n + 1))
    db.close()
    db = connect(target)
    try:
        with db.transaction() as tx:
            assert tx.fetchone('SELECT MAX(version) FROM schema_migrations')[0] >= 10
            assert tx.fetchone('SELECT COUNT(*) FROM stats_daily')[0] == 0
        accounts = AccountRepo(db)
        assert accounts.get('one')['last_active_at'] == '2026-11-12T00:00:00.000000Z'
        assert accounts.get('two')['last_active_at'] == '2026-11-15T00:00:00.000000Z'
        assert accounts.get('three')['last_active_at'] is None
    finally:
        db.close()


def test_migration_0010_backfill_spans_chunks_and_skips_keys_without_an_account(tmp_path):
    db, target = _database_before_0010(tmp_path)
    wid = WorkspaceRepo(db).get('default')['id']
    count = 1100  # more than two chunks of 500
    with db.transaction(write=True) as tx:
        tx.cross_workspace('test seed')
        for n in range(count):
            tx.execute('INSERT INTO accounts (id, login, display_name, created_at, created_by) '
                       'VALUES (?, ?, ?, ?, ?)', (f'id{n}', f'user{n}', f'user{n}', '2026-11-01T00:00:00.000000Z', 'cli'))
            tx.execute("INSERT INTO memberships (workspace_id, account_id, role) VALUES (?, ?, 'member')",
                       (wid, f'id{n}'))
            used = f'2026-11-{1 + n % 28:02d}T00:00:00.000000Z'
            tx.execute('INSERT INTO keys (workspace_id, name, account_id, scope, sha256, prefix, created_at, '
                       'created_by, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (wid, f'k{n}', f'id{n}', 'sync', f'{n:064d}', 'aml_', used, 'cli', used))
        tx.execute('INSERT INTO accounts (id, login, display_name, created_at, created_by) '
                   'VALUES (?, ?, ?, ?, ?)', ('idnone', 'unused', 'unused', '2026-11-01T00:00:00.000000Z', 'cli'))
    db.close()
    db = connect(target)
    try:
        with db.transaction() as tx:
            tx.cross_workspace('test read')
            rows = dict(tx.fetchall('SELECT id, last_active_at FROM accounts'))
        assert rows['idnone'] is None
        for n in range(count):
            assert rows[f'id{n}'] == f'2026-11-{1 + n % 28:02d}T00:00:00.000000Z'
    finally:
        db.close()


def test_snapshot_upserts_today_and_history_windows(db):
    seed(db, NOW)
    repo = StatsRepo(db)
    expected = {
        'day': '2026-11-17', 'taken_at': STAMP(NOW), 'accounts': 4, 'active_1d': 1, 'active_7d': 2,
        'active_30d': 3, 'workspaces_active': 2, 'workspaces_suspended': 1, 'workspaces_archived': 1,
        'workspaces_custom': 1, 'stored_bytes': 7000, 'upload_bytes': 750, 'runs': 3}
    assert repo.snapshot(now=NOW) == expected
    assert list(expected) == list(COLUMNS)
    (row,) = repo.history(90, now=NOW)
    assert row == expected
    later = NOW + timedelta(seconds=10)
    assert repo.snapshot(now=later, min_age=3600) is None
    assert repo.history(90, now=later)[0]['taken_at'] == STAMP(NOW)
    assert repo.snapshot(now=later)['taken_at'] == STAMP(later)
    assert repo.history(90, now=later)[0]['taken_at'] == STAMP(later)
    assert repo.snapshot(now=NOW + timedelta(hours=2), min_age=3600)
    assert len(repo.history(90, now=NOW)) == 1
    assert repo.snapshot(now=NOW - timedelta(days=7))
    assert repo.snapshot(now=NOW - timedelta(days=2))
    assert [r['day'] for r in repo.history(90, now=NOW)] == ['2026-11-10', '2026-11-15', '2026-11-17']
    assert [r['day'] for r in repo.history(3, now=NOW)] == ['2026-11-15', '2026-11-17']
    assert [r['day'] for r in repo.history(2, now=NOW)] == ['2026-11-17']
    assert [r['day'] for r in repo.history(1, now=NOW)] == ['2026-11-17']
    assert [r['day'] for r in repo.history(8, now=NOW)] == ['2026-11-10', '2026-11-15', '2026-11-17']
    assert [r['day'] for r in repo.history(7, now=NOW)] == ['2026-11-15', '2026-11-17']
    for days in (0, -1):
        with pytest.raises(ValueError):
            repo.history(days, now=NOW)


def _wait(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_refresher_takes_snapshots_and_stops(db):
    refresher = StatsRefresher(db, interval=0.05)
    refresher.start()
    try:
        assert _wait(lambda: StatsRepo(db).history(1))
    finally:
        assert refresher.stop() is True
    assert not refresher._thread.is_alive()
    assert STATS_REFRESH == 3600


def test_refresher_stop_is_prompt_and_reports_the_thread_ended(db):
    refresher = StatsRefresher(db, interval=3600)
    assert refresher.stop() is True  # never started
    refresher = StatsRefresher(db, interval=3600)
    refresher.start()
    assert _wait(lambda: StatsRepo(db).history(1))
    started = time.monotonic()
    assert refresher.stop() is True
    assert time.monotonic() - started < 1
    assert not refresher._thread.is_alive()


def test_refresher_logs_errors_and_keeps_running(db, monkeypatch, caplog):
    calls = []

    def snapshot(self, *, now=None, min_age=None):
        calls.append(min_age)
        if len(calls) == 1:
            raise RuntimeError('database went away')
        return None

    monkeypatch.setattr(stats_module.StatsRepo, 'snapshot', snapshot)
    caplog.set_level(logging.WARNING, logger='agenticml.server.stats')
    refresher = StatsRefresher(db, interval=0.05)
    refresher.start()
    try:
        assert _wait(lambda: len(calls) >= 3)
    finally:
        assert refresher.stop() is True
    ours = [r for r in caplog.records if r.name == 'agenticml.server.stats']
    assert ours and all(r.levelno == logging.WARNING and r.exc_info is None for r in ours)
    assert any('database went away' in r.getMessage() for r in ours)
    assert not refresher._thread.is_alive()
    assert calls[0] == 0.025


@pytest.mark.parametrize('interval, expected', [(STATS_REFRESH, ['start', 'stop', 'close']),
                                                 (0, ['close']), (None, ['close'])])
def test_serve_runs_the_refresher_around_the_server(monkeypatch, interval, expected):
    events = []

    class Recorder:
        def __init__(self, db, every):
            assert db == 'the-db' and every == interval

        def start(self):
            events.append('start')

        def stop(self):
            events.append('stop')
            return True

    server = SimpleNamespace(
        db='the-db', keys=SimpleNamespace(list=lambda: ['admin']), server_address=('127.0.0.1', 1),
        data_dir='data', public_url=None, serve_forever=lambda: None,
        server_close=lambda **kwargs: (kwargs['before_db_close']() if 'before_db_close' in kwargs else None,
                                       events.append('close')))
    monkeypatch.setattr(app, 'make_server', lambda *args: server)
    monkeypatch.setattr(app, 'StatsRefresher', Recorder)
    app.serve(stats_interval=interval)
    assert events == expected


def _server_dir(tmp_path):
    data = tmp_path / 'data'
    prepare_data_dir(data)
    database = for_server_dir(data / 'server')
    seed(database, datetime.now(timezone.utc))
    return data, database


def test_cli_stats_show_json_snapshot_and_days(tmp_path, capsys):
    data, database = _server_dir(tmp_path)
    assert cli.main(['server', 'stats', 'snapshot', '--data', str(data)]) == 0
    today = datetime.now(timezone.utc).date().isoformat()
    assert capsys.readouterr().out.strip() == f'snapshot saved for {today}'
    assert cli.main(['server', 'stats', 'snapshot', '--data', str(data), '--json']) == 0
    row = json.loads(capsys.readouterr().out)
    assert row['accounts'] == 4 and row['stored_bytes'] == 7000 and row['runs'] == 3

    assert cli.main(['server', 'stats', '--data', str(data)]) == 0
    text = capsys.readouterr().out
    assert 'Totals' in text and 'Largest workspaces' in text and 'History, last 30 days' in text
    assert 'gamma' in text and '(archived)' in text and today in text
    for pattern in (r'^  accounts\s+4$', r'^  new accounts, 1 day\s+1$', r'^  active accounts, 30 days\s+3$',
                    r'^  workspaces archived\s+1$', r'^  stored\s+6\.8 KB$', r'^  uploaded today\s+750 B$',
                    r'^  runs\s+3$', r'^  gamma\s+gamma\s+3\.9 KB \(archived\)$',
                    r'^  beta\s+beta\s+2\.0 KB$', rf'^  {today}\s+4\s+2\s+2\s+6\.8 KB\s+750 B\s+3$'):
        assert re.search(pattern, text, re.M), pattern
    assert 'none' not in text and 'no daily snapshots yet' not in text

    old_day = (datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat()
    with database.transaction(write=True) as tx:
        tx.execute('INSERT INTO stats_daily (day, taken_at, accounts, active_1d, active_7d, active_30d, '
                   'workspaces_active, workspaces_suspended, workspaces_archived, workspaces_custom, '
                   'stored_bytes, upload_bytes, runs) VALUES (?, ?, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)',
                   (old_day, old_day + 'T00:00:00.000000Z'))
    assert cli.main(['server', 'stats', 'show', '--data', str(data), '--json']) == 0
    shown = json.loads(capsys.readouterr().out)
    assert set(shown) == {'totals', 'top_workspaces', 'history'}
    assert shown['totals']['accounts'] == 4 and shown['totals']['runs'] == 3
    assert [r['day'] for r in shown['history']] == [today]
    assert shown['top_workspaces'][0]['slug'] == 'gamma'
    assert cli.main(['server', 'stats', '--data', str(data), '--json', '--days', '60']) == 0
    assert [r['day'] for r in json.loads(capsys.readouterr().out)['history']] == [old_day, today]
    capsys.readouterr()
    for days in ('0', '-5', '3651', '100000'):
        assert cli.main(['server', 'stats', '--data', str(data), '--days', days]) == 2
        err = capsys.readouterr().err
        assert err == 'agenticml server: --days must be between 1 and 3650\n'
    assert cli.main(['server', 'stats', '--data', str(data), '--days', '3650', '--json']) == 0
    capsys.readouterr()


def test_cli_stats_table_without_snapshots_or_workspace_data(tmp_path, capsys):
    data = tmp_path / 'data'
    prepare_data_dir(data)
    for_server_dir(data / 'server')
    assert cli.main(['server', 'stats', '--data', str(data)]) == 0
    out = capsys.readouterr().out
    assert re.search(r'^  accounts\s+0$', out, re.M)
    assert re.search(r'^  workspaces active\s+1$', out, re.M)
    assert re.search(r'^  default\s+Default\s+0 B$', out, re.M)
    assert re.search(r'^  no daily snapshots yet$', out, re.M)
    assert 'none' not in out


def test_cli_stats_table_largest_workspaces_none(tmp_path, capsys, monkeypatch):
    data = tmp_path / 'data'
    prepare_data_dir(data)
    for_server_dir(data / 'server')
    monkeypatch.setattr(StatsRepo, 'top_workspaces', lambda self, limit=5, *, tx=None: [])
    assert cli.main(['server', 'stats', '--data', str(data)]) == 0
    assert re.search(r'^Largest workspaces\n  none$', capsys.readouterr().out, re.M)


def test_cli_stats_database_error_exits_1(tmp_path, capsys, monkeypatch):
    data = tmp_path / 'data'
    prepare_data_dir(data)
    for_server_dir(data / 'server')

    def broken(self, *, now=None, tx=None):
        raise DatabaseError('could not read or write database')

    monkeypatch.setattr(StatsRepo, 'totals', broken)
    assert cli.main(['server', 'stats', '--data', str(data)]) == 1
    assert capsys.readouterr().err == 'agenticml server: could not read or write database\n'


def test_cli_stats_missing_database_exits_2(tmp_path, capsys):
    data = tmp_path / 'empty'
    data.mkdir()
    assert cli.main(['server', 'stats', '--data', str(data)]) == 2
    err = capsys.readouterr().err
    assert err.startswith('agenticml server: no server database found for ')
    assert err.strip().endswith('check --data and --database')
    assert cli.main(['server', 'stats', 'snapshot', '--data', str(data)]) == 2
    assert 'no server database found' in capsys.readouterr().err


@pytest.mark.sqlite_only
def test_cli_stats_missing_sqlite_file_exits_2(tmp_path, capsys):
    missing = tmp_path / 'nowhere' / 'agenticml.db'
    assert cli.main(['server', 'stats', '--data', str(tmp_path), '--database', f'sqlite:///{missing}']) == 2
    err = capsys.readouterr().err
    assert err.startswith('agenticml server: ') and 'no server database found' in err
    assert not missing.exists()


def test_admin_stats_api_reports_database_errors_as_503(two, monkeypatch):  # noqa: F811
    server, _, _, _, _, _, _ = two
    alice = _password_cookie(server, 'alice')

    def broken(self, *, now=None, tx=None):
        raise DatabaseError('could not read or write database')

    monkeypatch.setattr(StatsRepo, 'totals', broken)
    status, headers, body, _ = _authed(server, '/api/admin/stats', cookie=alice)
    assert status == 503 and headers['Cache-Control'] == 'no-store'
    assert json.loads(body) == {'error': 'usage statistics unavailable'}


def test_admin_stats_api(two):  # noqa: F811
    server, _, _, _, _, _, _ = two
    alice, bob = _password_cookie(server, 'alice'), _password_cookie(server, 'bob')
    StatsRepo(server.db).snapshot()
    status, headers, body, _ = _authed(server, '/api/admin/stats', cookie=alice)
    assert status == 200 and headers['Cache-Control'] == 'no-store'
    payload = json.loads(body)
    assert set(payload) == {'totals', 'top_workspaces', 'history', 'refresh_seconds'}
    assert payload['refresh_seconds'] == STATS_REFRESH
    expected = StatsRepo(server.db).totals()
    assert payload['totals'].pop('as_of') <= expected.pop('as_of')
    assert payload['totals'] == expected == {
        'accounts': 2, 'new_accounts_1d': 2, 'new_accounts_7d': 2, 'active_1d': 2, 'active_7d': 2,
        'active_30d': 2, 'workspaces_active': 3, 'workspaces_suspended': 0, 'workspaces_archived': 0,
        'workspaces_custom': 0, 'stored_bytes': 0, 'upload_bytes_today': 0, 'runs': 0}
    assert {row['slug'] for row in payload['top_workspaces']} == {'default', 'alpha', 'beta'}
    assert set(payload['top_workspaces'][0]) == {'slug', 'name', 'stored_bytes', 'archived'}
    (row,) = payload['history']
    assert row['accounts'] == payload['totals']['accounts']
    assert '@' not in body.decode() and 'alice' not in body.decode()

    unknown = _authed(server, '/api/no-such', cookie=bob)
    assert unknown[0] == 404
    assert _snapshot(*_authed(server, '/api/admin/stats', cookie=bob)[:3]) == _snapshot(*unknown[:3])
    anon = call(server, '/api/admin/stats')
    assert _snapshot(*anon) == _snapshot(*call(server, '/api/no-such'))
    assert json.loads(anon[2]) == {'error': 'login required'}

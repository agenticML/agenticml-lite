"""A rotated database password is re-read from the secret, and connect failures are logged."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import logging
import os
import threading

import pytest

from agenticml.server import ratelog
from agenticml.server.db import DatabaseError
from agenticml.server.db import postgres
from agenticml.server.db.postgres import PASSWORD_SECRET_ENV, PostgresDatabase

PASSWORD_FAILURE = ('connection failed: connection to server at "10.0.0.5", port 5432 failed: '
                    'FATAL:  password authentication failed for user "agenticml"')


class FakeError(Exception):
    def __init__(self, message, sqlstate=None, primary=None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(message_primary=primary)


class FakeConnection:
    closed = False
    broken = False

    def __init__(self):
        self.statements = []

    def execute(self, statement, *args):
        self.statements.append(statement)

    def close(self):
        self.closed = True


class FakePsycopg:
    """connect() accepts only the password in ``accepted``; no password means libpq's own source."""
    Error = FakeError
    errors = SimpleNamespace(UndefinedObject=type('UndefinedObject', (FakeError,), {}),
                             FeatureNotSupported=type('FeatureNotSupported', (FakeError,), {}))

    def __init__(self, accepted, ambient='stale', error=None):
        self.accepted = accepted
        self.ambient = ambient
        self.error = error or FakeError(PASSWORD_FAILURE)
        self.calls = []

    def connect(self, conninfo, **kwargs):
        self.calls.append(kwargs.get('password'))
        if (kwargs.get('password') or self.ambient) != self.accepted:
            raise self.error
        return FakeConnection()


@pytest.fixture
def make_db(monkeypatch):
    monkeypatch.setattr(postgres, '_conninfo_with_timeout', lambda url: url)
    monkeypatch.delenv('PGPASSWORD', raising=False)
    monkeypatch.setattr(ratelog, '_unavailable', {})

    def make(fake, secret='arn:aws:secretsmanager:eu-west-1:1:secret:db', fetch=None, clock=None):
        if secret:
            monkeypatch.setenv(PASSWORD_SECRET_ENV, secret)
        else:
            monkeypatch.delenv(PASSWORD_SECRET_ENV, raising=False)
        db = PostgresDatabase.__new__(PostgresDatabase)
        db._psycopg = fake
        db._url = 'postgresql://agenticml@db.internal:5432/agenticml'
        db._lock = threading.Lock()
        db._idle = []
        db._connections = set()
        db._init_password_refresh()
        db.fetches = []

        def fetch_secret():
            db.fetches.append(1)
            result = fetch() if callable(fetch) else fetch
            if isinstance(result, Exception):
                raise result
            return result

        db._fetch_secret_password = fetch_secret
        if clock is not None:
            monkeypatch.setattr(postgres.time, 'monotonic', clock)
        return db

    return make


def test_rotated_password_is_fetched_and_the_connect_retried_once(make_db, caplog):
    fake = FakePsycopg(accepted='rotated')
    db = make_db(fake, fetch='rotated')
    with caplog.at_level(logging.INFO, logger=postgres.logger.name):
        connection = db._new_connection()
    assert isinstance(connection, FakeConnection)
    assert fake.calls == [None, 'rotated']
    assert len(db.fetches) == 1
    assert 'database password refreshed from the secret' in caplog.text
    assert 'rotated' not in caplog.text.replace('database password refreshed', '')
    # The next connection uses the stored password straight away.
    db._new_connection()
    assert fake.calls == [None, 'rotated', 'rotated']
    assert len(db.fetches) == 1


def test_sqlstate_28p01_alone_is_a_password_failure(make_db):
    fake = FakePsycopg(accepted='rotated', error=FakeError('connection failed', sqlstate='28P01'))
    db = make_db(fake, fetch='rotated')
    assert isinstance(db._new_connection(), FakeConnection)
    assert fake.calls == [None, 'rotated']


def test_other_connect_failures_do_not_fetch(make_db):
    fake = FakePsycopg(accepted='rotated', error=FakeError('connection refused', sqlstate='08006'))
    db = make_db(fake, fetch='rotated')
    with pytest.raises(DatabaseError, match='could not connect to Postgres'):
        db._new_connection()
    assert db.fetches == [] and fake.calls == [None]


def test_unset_variable_never_fetches(make_db):
    fake = FakePsycopg(accepted='rotated')
    db = make_db(fake, secret='', fetch='rotated')
    with pytest.raises(DatabaseError, match='could not connect to Postgres'):
        db._new_connection()
    assert db.fetches == [] and fake.calls == [None]


@pytest.mark.parametrize('failure', [RuntimeError('AccessDenied for arn:secret:db'), KeyError('password'),
                                     ImportError('no boto3')])
def test_fetch_failure_raises_the_connect_error_and_logs_a_class_only_warning(make_db, caplog, failure):
    fake = FakePsycopg(accepted='rotated')
    db = make_db(fake, fetch=failure)
    with caplog.at_level(logging.WARNING, logger=postgres.logger.name):
        with pytest.raises(DatabaseError, match='could not connect to Postgres') as raised:
            db._new_connection()
    assert fake.calls == [None]
    assert type(failure).__name__ in caplog.text
    assert 'arn:' not in caplog.text and 'AccessDenied' not in caplog.text
    # Callers never see driver text.
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_same_password_is_not_retried(make_db, monkeypatch):
    fake = FakePsycopg(accepted='rotated')
    db = make_db(fake, fetch='stale')  # the ambient (PGPASSWORD) one that just failed
    monkeypatch.setenv('PGPASSWORD', 'stale')
    with pytest.raises(DatabaseError):
        db._new_connection()
    assert fake.calls == [None] and len(db.fetches) == 1


def test_refetched_password_that_still_fails_is_tried_once(make_db):
    fake = FakePsycopg(accepted='something else')
    db = make_db(fake, fetch='rotated')
    with pytest.raises(DatabaseError):
        db._new_connection()
    assert fake.calls == [None, 'rotated']


def test_secret_is_refetched_at_most_every_30_seconds(make_db):
    now = [1000.0]
    fake = FakePsycopg(accepted='never')
    db = make_db(fake, fetch='wrong', clock=lambda: now[0])
    for _ in range(3):
        with pytest.raises(DatabaseError):
            db._new_connection()
        now[0] += 5
    assert len(db.fetches) == 1
    now[0] += 30
    with pytest.raises(DatabaseError):
        db._new_connection()
    assert len(db.fetches) == 2


def test_threads_that_fail_together_share_one_fetch(make_db):
    fake = FakePsycopg(accepted='rotated')
    release = threading.Event()
    entered = threading.Barrier(4)

    def fetch():
        release.wait(5)
        return 'rotated'

    db = make_db(fake, fetch=fetch)

    def connect(_):
        entered.wait(5)
        return db._new_connection()

    with ThreadPoolExecutor(4) as pool:
        futures = [pool.submit(connect, n) for n in range(4)]
        # All four have failed once the fetching thread holds the lock; let it go.
        while fake.calls.count(None) < 4:
            threading.Event().wait(0.01)
        release.set()
        connections = [f.result(10) for f in futures]
    assert all(isinstance(c, FakeConnection) for c in connections)
    assert len(db.fetches) == 1


def test_connect_failure_is_logged_without_url_or_password(make_db, caplog):
    fake = FakePsycopg(accepted='never')
    db = make_db(fake, fetch='s3cret-new')
    with caplog.at_level(logging.WARNING, logger=postgres.logger.name):
        with pytest.raises(DatabaseError):
            db._new_connection()
    (record,) = [r for r in caplog.records if r.getMessage().startswith('could not connect')]
    assert record.levelno == logging.WARNING
    assert 'password authentication failed for user "agenticml"' in record.getMessage()
    for secret in ('s3cret-new', 'db.internal', 'postgresql://', '10.0.0.5'):
        assert secret not in caplog.text


def test_connect_failure_uses_sqlstate_and_primary_message_when_present(make_db, caplog):
    error = FakeError('long text with host', sqlstate='57P03', primary='the database system is starting up')
    db = make_db(FakePsycopg(accepted='never', error=error), secret='')
    with caplog.at_level(logging.WARNING, logger=postgres.logger.name):
        with pytest.raises(DatabaseError):
            db._new_connection()
    assert 'could not connect to Postgres: 57P03: the database system is starting up' in caplog.text
    assert 'long text' not in caplog.text


def test_connect_failure_log_is_rate_limited_and_counts_suppressed(make_db, caplog):
    now = [50.0]
    db = make_db(FakePsycopg(accepted='never'), secret='', clock=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger=postgres.logger.name):
        for _ in range(4):
            with pytest.raises(DatabaseError):
                db._new_connection()
        assert len(caplog.records) == 1
        now[0] += 11
        with pytest.raises(DatabaseError):
            db._new_connection()
    assert len(caplog.records) == 2
    assert '(3 more since last log)' in caplog.records[1].getMessage()


def test_secret_client_has_short_timeouts_and_returns_the_json_password(make_db, monkeypatch):
    boto3 = pytest.importorskip('boto3')
    pytest.importorskip('botocore')
    seen = {}

    class Client:
        def get_secret_value(self, SecretId):
            seen['secret'] = SecretId
            return {'SecretString': '{"username": "agenticml", "password": "rotated-pw"}'}

    def client(service, **kwargs):
        seen['service'] = service
        seen.update(kwargs)
        return Client()

    monkeypatch.setattr(boto3, 'client', client)
    monkeypatch.setenv('AWS_REGION', 'eu-west-1')
    db = make_db(FakePsycopg(accepted='x'), secret='the-secret-arn')
    assert PostgresDatabase._fetch_secret_password(db) == 'rotated-pw'
    assert seen['service'] == 'secretsmanager' and seen['secret'] == 'the-secret-arn'
    assert seen['region_name'] == 'eu-west-1'
    config = seen['config']
    assert config.connect_timeout == 3 and config.read_timeout == 5
    assert config.retries == {'max_attempts': 2}


def test_log_unavailable_names_class_method_and_path_only(caplog):
    ratelog._unavailable.clear()
    with caplog.at_level(logging.WARNING, logger='t'):
        ratelog.log_unavailable(logging.getLogger('t'), 'dashboard request failed: database unavailable',
                                DatabaseError('private'), 'GET', '/api/w/x/runs?key=secret\nfake line')
    assert caplog.messages == ['dashboard request failed: database unavailable (DatabaseError) GET /api/w/x/runs']
    ratelog._unavailable.clear()


# ----------------------------------------------------------------- real Postgres

@pytest.mark.skipif(not os.environ.get('AGENTICML_TEST_DATABASE_URL'),
                    reason='AGENTICML_TEST_DATABASE_URL is not set')
def test_real_postgres_rotation_is_picked_up_without_a_restart(server_database, postgres_admin, monkeypatch, caplog):
    import secrets
    from urllib.parse import urlsplit, urlunsplit
    from psycopg import sql
    role = 'rot_' + secrets.token_hex(4)
    old, new = 'old-' + secrets.token_hex(6), 'new-' + secrets.token_hex(6)
    schema = urlsplit(server_database)
    database = postgres_admin.execute('SELECT current_database()').fetchone()[0]
    postgres_admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}').format(
        sql.Identifier(role), sql.Literal(old)))
    postgres_admin.execute(sql.SQL('GRANT CREATE, CONNECT ON DATABASE {} TO {}').format(
        sql.Identifier(database), sql.Identifier(role)))
    schema_name = 'rot_' + secrets.token_hex(4)
    postgres_admin.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(
        sql.Identifier(schema_name), sql.Identifier(role)))
    netloc = f'{role}@{schema.hostname}' + (f':{schema.port}' if schema.port else '')
    url = urlunsplit((schema.scheme, netloc, schema.path, f'options=-csearch_path%3D{schema_name}', ''))
    monkeypatch.setenv('PGPASSWORD', old)
    monkeypatch.setenv(PASSWORD_SECRET_ENV, 'fake-secret')
    db = None
    try:
        db = postgres.PostgresDatabase(url)
        fetched = []
        db._fetch_secret_password = lambda: fetched.append(1) or new
        # Rotate: open connections survive, so hold them busy and open new ones.
        postgres_admin.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(role), sql.Literal(new)))
        barrier = threading.Barrier(3)

        def work(_):
            with db.transaction() as tx:
                barrier.wait(10)
                return tx.fetchone('SELECT 1')[0]

        with caplog.at_level(logging.INFO, logger=postgres.logger.name):
            with ThreadPoolExecutor(3) as pool:
                assert list(pool.map(work, range(3))) == [1, 1, 1]
        assert len(fetched) == 1
        assert 'database password refreshed from the secret' in caplog.text
        assert new not in caplog.text
        # Lock connections go through the same path with the stored password.
        with db.session_lock('probe', timeout=5):
            pass
        assert len(fetched) == 1
    finally:
        if db is not None:
            db.close()
        postgres_admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema_name)))
        postgres_admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM {}').format(
            sql.Identifier(database), sql.Identifier(role)))
        postgres_admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@pytest.mark.skipif(not os.environ.get('AGENTICML_TEST_DATABASE_URL'),
                    reason='AGENTICML_TEST_DATABASE_URL is not set')
def test_real_postgres_password_failure_shape(server_database, monkeypatch):
    """What psycopg reports for a refused password: pins the detection the refresh relies on."""
    import psycopg
    from urllib.parse import urlsplit
    parts = urlsplit(server_database)
    with pytest.raises(psycopg.OperationalError) as raised:
        psycopg.connect(host=parts.hostname, port=parts.port, dbname=parts.path.lstrip('/'),
                        user=parts.username, password='definitely-wrong', connect_timeout=5)
    assert PostgresDatabase._password_failure(raised.value)
    assert raised.value.sqlstate is None  # psycopg has no SQLSTATE for a failed connect: the message decides

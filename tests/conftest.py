import pytest


@pytest.fixture(autouse=True)
def local_run_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path / "agenticml"))


def pytest_configure(config):
    config.addinivalue_line('markers', 'sqlite_only: exercises SQLite storage mechanics')


@pytest.fixture(scope='session')
def postgres_admin():
    import os
    if not os.environ.get('AGENTICML_TEST_DATABASE_URL'):
        yield None
        return
    import psycopg
    from psycopg import sql
    with psycopg.connect(os.environ['AGENTICML_TEST_DATABASE_URL'], autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname='agenticml_test'").fetchone():
            admin.execute("CREATE ROLE agenticml_test LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'agenticml-test-password'")
        admin.execute("ALTER ROLE agenticml_test LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'agenticml-test-password'")
        database = admin.execute('SELECT current_database()').fetchone()[0]
        admin.execute(sql.SQL('GRANT CREATE, CONNECT ON DATABASE {} TO agenticml_test').format(sql.Identifier(database)))
        yield admin


@pytest.fixture(autouse=True)
def server_database(request, monkeypatch, postgres_admin):
    import os
    import secrets
    from agenticml.server import db
    from server_db_helpers import postgres_url
    if postgres_admin is None:
        monkeypatch.delenv('AGENTICML_SERVER_DATABASE_URL', raising=False)
        yield None
        return
    if request.node.get_closest_marker('sqlite_only'):
        pytest.skip('exercises SQLite storage mechanics')
    from psycopg import sql
    import server_db_helpers
    schemas = {}

    def schema_url(key):
        if key not in schemas:
            schema = 't_' + secrets.token_hex(8)
            postgres_admin.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION agenticml_test').format(sql.Identifier(schema)))
            schemas[key] = (schema, postgres_url(os.environ['AGENTICML_TEST_DATABASE_URL'], schema))
        return schemas[key][1]

    monkeypatch.setattr(server_db_helpers, '_schema_url', schema_url)
    url = schema_url('default')
    monkeypatch.setenv('AGENTICML_SERVER_DATABASE_URL', url)
    try:
        yield url
    finally:
        with db._registry_lock:
            for database in list(db._registry.values()):
                database.close()
            db._registry.clear()
        for schema, _ in schemas.values():
            postgres_admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

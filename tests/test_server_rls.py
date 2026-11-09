"""Row-level security: unbound queries see nothing; policies cover every workspace table."""
import os
import threading

import pytest
from server_db_helpers import insert_run

from agenticml.server.db import DatabaseBusy, DatabaseError, connect
from agenticml.server.db.repos import AuditRepo, KeyRepo, NoteRepo, RunRepo, SessionRepo
from agenticml.server.db.usage import UsageRepo
from agenticml.server.db.workspaces import AccountRepo, InviteRepo, WorkspaceRepo
from agenticml.server.workspaces import default_workspace_id
from agenticml.store import layout

_postgres = pytest.mark.skipif(not os.environ.get('AGENTICML_TEST_DATABASE_URL'),
                               reason='AGENTICML_TEST_DATABASE_URL is not set')


def _rls_tables(tx):
    return [row['table_name'] for row in tx.fetchall(
        "SELECT c.table_name FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "ON t.table_schema=c.table_schema AND t.table_name=c.table_name "
        "WHERE c.table_schema=current_schema() AND c.column_name='workspace_id' "
        "AND t.table_type='BASE TABLE' ORDER BY c.table_name")]


def tables_missing_workspace_rls(tx):
    """Workspace-id tables that lack FORCE RLS plus ``<table>_workspace``."""
    tables = _rls_tables(tx)
    flags = {row['relname']: (row['relrowsecurity'], row['relforcerowsecurity'])
             for row in tx.fetchall(
                 "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                 "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                 "WHERE n.nspname=current_schema() AND c.relkind='r'")}
    policies = {row['tablename'] for row in tx.fetchall(
        "SELECT tablename FROM pg_policies WHERE schemaname=current_schema() "
        "AND policyname=tablename || '_workspace'")}
    return [name for name in tables
            if flags.get(name) != (True, True) or name not in policies]


def _seed(db, wid):
    KeyRepo(db, wid).create('rls-key', 'sync', 'cli', user=None, only_if_no_admin=False, env=False)
    InviteRepo(db, wid).create('pending-rls', 'viewer', 'cli')
    RunRepo(db, wid).insert({
        'run_id': 'rls-run', 'project': None, 'created_at': None, 'owner_user': None,
        'owner_key': None, 'status': None, 'fingerprint': None, 'last_sync_at': None,
        'last_sync_key': None, 'list_row': '{"run_id":"rls-run"}', 'last_step': None,
        'launch_note': None, 'commit_sizes': None, 'updated_at': layout.format_timestamp(),
    })
    RunRepo(db, wid).replace_files('rls-run', [('meta.json', 4, None)])
    NoteRepo(db, wid).upsert('rls-run', 'note', layout.format_timestamp())
    AuditRepo(db, wid).insert({'ts': layout.format_timestamp(), 'actor': 'rls', 'action': 'rls.seed'})
    SessionRepo(db).create('a' * 64, {
        'user': 'rls', 'id': None, 'workspace_id': wid, 'generation': 0, 'via': 'key',
        'key': 'rls', 'key_sha256': 'b' * 64, 'created': 1.0, 'last_seen': 1.0,
    }, 1000)
    with db.transaction(write=True) as tx:
        UsageRepo(db, wid).record_object('c' * 64, 8, tx)


@_postgres
def test_unbound_selects_are_empty_and_insert_fails_check(server_database):
    db = connect(server_database)
    try:
        wid = default_workspace_id(db)
        _seed(db, wid)
        with db.transaction() as tx:
            tables = _rls_tables(tx)
            tx.cross_workspace('rls seed check')
            present = {name: tx.fetchone('SELECT COUNT(*) FROM ' + name)[0] for name in tables}
        assert tables and all(count > 0 for count in present.values()), present
        with db.transaction() as tx:
            empty = {name: tx.fetchone('SELECT COUNT(*) FROM ' + name)[0] for name in tables}
        assert empty == dict.fromkeys(tables, 0)
        with pytest.raises(DatabaseError):
            with db.transaction(write=True) as tx:
                tx.execute(
                    'INSERT INTO runs (workspace_id, run_id, list_row, updated_at) VALUES (?, ?, ?, ?)',
                    (wid, 'unbound', '{}', layout.format_timestamp()))
    finally:
        db.close()


@_postgres
def test_bound_workspace_cannot_read_another_runs_row_by_id(server_database):
    db = connect(server_database)
    try:
        wid_a = default_workspace_id(db)
        owner = AccountRepo(db).create('rls-owner')
        wid_b = WorkspaceRepo(db).create('rls-b', owner=owner['login'])['id']
        insert_run(RunRepo(db, wid_a), 'run-a')
        insert_run(RunRepo(db, wid_b), 'run-b')
        with db.transaction() as tx:
            tx.bind_workspace(wid_a)
            assert tx.fetchone('SELECT run_id FROM runs WHERE run_id=?', ('run-a',))['run_id'] == 'run-a'
            assert tx.fetchone('SELECT run_id FROM runs WHERE run_id=?', ('run-b',)) is None
    finally:
        db.close()


@_postgres
def test_every_workspace_id_table_has_forced_rls_and_policy(server_database):
    db = connect(server_database)
    try:
        with db.transaction() as tx:
            tables = _rls_tables(tx)
            missing = tables_missing_workspace_rls(tx)
        assert tables
        assert missing == []
    finally:
        db.close()


@_postgres
def test_catalog_helper_flags_new_workspace_table(server_database):
    db = connect(server_database)
    try:
        with db.transaction(write=True) as tx:
            tx.execute('CREATE TABLE rls_probe_missing (workspace_id TEXT)')
            assert 'rls_probe_missing' in tables_missing_workspace_rls(tx)
            db.dialect.enable_workspace_rls(tx, 'rls_probe_missing')
            assert 'rls_probe_missing' not in tables_missing_workspace_rls(tx)
            tx.execute('DROP TABLE rls_probe_missing')
    finally:
        db.close()


@_postgres
def test_repo_rebind_restores_caller_binding(server_database):
    db = connect(server_database)
    try:
        wid_a = default_workspace_id(db)
        owner = AccountRepo(db).create('rls-owner')
        wid_b = WorkspaceRepo(db).create('rls-b', owner=owner['login'])['id']
        insert_run(RunRepo(db, wid_a), 'run-a')
        insert_run(RunRepo(db, wid_b), 'run-b')
        with db.transaction() as tx:
            tx.bind_workspace(wid_a)
            assert RunRepo(db, wid_b).ids(tx) == {'run-b'}
            assert tx.bound == wid_a
            assert tx.fetchone('SELECT run_id FROM runs WHERE run_id=?', ('run-a',))['run_id'] == 'run-a'
            assert tx.fetchone('SELECT run_id FROM runs WHERE run_id=?', ('run-b',)) is None
            slugs = AccountRepo(db).workspace_slugs(owner['login'], tx)
            assert 'rls-b' in slugs
            assert tx.bound == wid_a
            assert tx.fetchone('SELECT run_id FROM runs WHERE run_id=?', ('run-a',))['run_id'] == 'run-a'
    finally:
        db.close()


@_postgres
def test_null_workspace_audit_and_sessions_visible_only_under_star(server_database):
    db = connect(server_database)
    try:
        wid = default_workspace_id(db)
        _seed(db, wid)
        with db.transaction(write=True) as tx:
            tx.cross_workspace('rls null workspace seed')
            tx.execute(
                'INSERT INTO audit_events (workspace_id, ts, actor, action) VALUES (?, ?, ?, ?)',
                (None, layout.format_timestamp(), 'rls', 'rls.server'))
            tx.execute(
                'INSERT INTO sessions (token_sha256, account_id, workspace_id, generation, via, '
                'key_name, key_sha256, created, last_seen, created_order) VALUES '
                '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ('d' * 64, None, None, 0, 'password', None, None, 1.0, 1.0, 99))
        with db.transaction() as tx:
            assert tx.fetchone(
                'SELECT COUNT(*) FROM audit_events WHERE workspace_id IS NULL')[0] == 0
            assert tx.fetchone(
                'SELECT COUNT(*) FROM sessions WHERE workspace_id IS NULL')[0] == 0
        with db.transaction() as tx:
            tx.bind_workspace(wid)
            assert tx.fetchone(
                'SELECT COUNT(*) FROM audit_events WHERE workspace_id IS NULL')[0] == 0
            assert tx.fetchone(
                'SELECT COUNT(*) FROM sessions WHERE workspace_id IS NULL')[0] == 0
        with db.transaction() as tx:
            tx.cross_workspace('rls null workspace read')
            assert tx.fetchone(
                'SELECT COUNT(*) FROM audit_events WHERE workspace_id IS NULL')[0] == 1
            assert tx.fetchone(
                'SELECT COUNT(*) FROM sessions WHERE workspace_id IS NULL')[0] == 1
    finally:
        db.close()


@_postgres
def test_unbound_update_and_delete_affect_nothing(server_database):
    db = connect(server_database)
    try:
        wid = default_workspace_id(db)
        _seed(db, wid)
        with db.transaction(write=True) as tx:
            updated = tx.execute('UPDATE runs SET status=? WHERE run_id=?',
                                 ('crashed', 'rls-run'))
            deleted = tx.execute('DELETE FROM runs WHERE run_id=?', ('rls-run',))
            assert updated.rowcount == 0
            assert deleted.rowcount == 0
        with db.transaction() as tx:
            tx.bind_workspace(wid)
            assert tx.fetchone('SELECT run_id FROM runs WHERE run_id=?', ('rls-run',))['run_id'] == 'rls-run'
    finally:
        db.close()


@_postgres
def test_binding_does_not_leak_to_next_pooled_transaction(server_database):
    db = connect(server_database)
    try:
        wid = default_workspace_id(db)
        _seed(db, wid)
        with db.transaction() as tx:
            tx.bind_workspace(wid)
            assert tx.fetchone('SELECT COUNT(*) FROM runs')[0] > 0
        with db.transaction() as tx:
            assert tx.bound is None
            assert tx.fetchone('SELECT COUNT(*) FROM runs')[0] == 0
            setting = tx.fetchone("SELECT current_setting('app.workspace_id', true) AS v")['v']
            assert setting in (None, '')
    finally:
        db.close()


def test_bind_and_cross_are_nops_on_sqlite(tmp_path):
    db = connect('sqlite:///' + str(tmp_path / 'rls.db'))
    try:
        assert db.dialect.name == 'sqlite'
        wid_a = default_workspace_id(db)
        owner = AccountRepo(db).create('rls-owner')
        wid_b = WorkspaceRepo(db).create('rls-b', owner=owner['login'])['id']
        insert_run(RunRepo(db, wid_a), 'run-a')
        insert_run(RunRepo(db, wid_b), 'run-b')
        with db.transaction() as tx:
            tx.bind_workspace(wid_a)
            assert tx.fetchone('SELECT COUNT(*) FROM runs')[0] == 2
            with tx.cross_scope('sqlite nop scope'):
                assert tx.bound == '*'
                assert tx.fetchone('SELECT COUNT(*) FROM runs')[0] == 2
            assert tx.bound == wid_a
            tx.cross_workspace('sqlite nop')
            assert tx.fetchone('SELECT COUNT(*) FROM runs')[0] == 2
    finally:
        db.close()


@_postgres
def test_bind_restore_preserves_lock_timeout_as_busy(server_database):
    db = connect(server_database)
    held, done = threading.Event(), threading.Event()
    try:
        wid = default_workspace_id(db)

        def hold():
            with db.transaction(write=True) as tx:
                tx.advisory_lock('x')
                held.set()
                done.wait(10)

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            assert held.wait(5)
            repo = KeyRepo(db, wid)

            def wait_for_lock(tx):
                tx.execute("SET LOCAL lock_timeout = '50ms'")
                tx.advisory_lock('x')

            with pytest.raises(DatabaseBusy):
                with repo._tx(write=True) as tx:
                    wait_for_lock(tx)
            with pytest.raises(DatabaseBusy):
                with db.transaction(write=True) as tx:
                    with tx.cross_scope('rls lock timeout'):
                        wait_for_lock(tx)
            with pytest.raises(DatabaseBusy):
                with db.transaction(write=True) as tx:
                    with AuditRepo(db, wid)._bind(tx):
                        wait_for_lock(tx)
        finally:
            done.set()
            thread.join(10)
    finally:
        db.close()

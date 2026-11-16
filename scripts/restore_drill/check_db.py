"""Read-only SQL counts for the restore drill. Runs in a task as `python -I -c <this file>`.

Reads DRILL_DB_URL (the connection URL without the password) and PGPASSWORD (injected by ECS from
the production secret, as for the live service). Every statement runs in a read-only session, so the
same script is safe against the restored database or, with mk_td.py --prod, against production.

Each query is independent (autocommit, one try per query), so one failing statement cannot stop
the rest. Required queries (who, in_recovery, ssl, migrations, table counts) make the script exit
1 and skip DRILL_DB_OK when they fail or a required table is absent. Optional ones (server
version, rds.force_ssl, latest_run_update) are printed, never fatal: the app role may not see
rds.* parameters.
"""
import os
import sys

COUNT_TABLES = ('accounts', 'workspaces', 'memberships', 'keys', 'runs', 'run_files', 'objects')


def run(c, out=print):
    """Run every check on connection c. Returns True when all required checks passed."""
    failed = []

    def q(label, sql, required=True, params=None):
        try:
            rows = c.execute(sql, params).fetchall() if params is not None else c.execute(sql).fetchall()
        except Exception as exc:  # one failing statement must not stop the rest
            out(label, 'ERROR', type(exc).__name__, str(exc).splitlines()[0] if str(exc) else '')
            if required:
                failed.append(label)
            return None
        return rows

    def show(label, sql, required=True):
        rows = q(label, sql, required)
        if rows is not None:
            out(label, rows)
        return rows

    show('who', "select current_user, current_database(), inet_server_addr()::text")
    show('in_recovery', 'select pg_is_in_recovery()')
    show('ssl', 'select ssl, version, cipher from pg_stat_ssl where pid = pg_backend_pid()')
    show('server_version', 'show server_version', required=False)
    # rds.* parameters are not visible to every role: missing_ok returns NULL instead of an error.
    rows = q('rds.force_ssl', "select current_setting('rds.force_ssl', true)", required=False)
    value = rows[0][0] if rows else None
    out('rds.force_ssl', value if value is not None else 'not visible to this role')
    q('workspace_scope', "select set_config('app.workspace_id', '*', false)", required=False)  # cross-workspace read under FORCE RLS

    present = {}
    for table in ('schema_migrations', 'runs') + COUNT_TABLES:
        if table in present:
            continue
        rows = q('table ' + table, "select to_regclass(%s) is not null", params=(table,))
        present[table] = bool(rows and rows[0][0])
        if rows is not None and not present[table]:
            out('table', table, 'ABSENT')
            failed.append('table ' + table)
    if present.get('schema_migrations'):
        show('migrations', 'select count(*), max(version) from schema_migrations')
    for table in COUNT_TABLES:
        if present.get(table):
            show('count ' + table, 'select count(*) from ' + table)
    if present.get('runs'):
        show('latest_run_update', 'select max(updated_at) from runs', required=False)
    if failed:
        out('DRILL_DB_FAILED', ', '.join(failed))
    return not failed


def main():
    import psycopg

    url = os.environ['DRILL_DB_URL']
    with psycopg.connect(url, connect_timeout=15, autocommit=True,
                         options='-c default_transaction_read_only=on') as c:
        ok = run(c)
    if ok:
        print('DRILL_DB_OK')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

"""Repository-based inspection and legacy fixtures for server storage tests."""
from dataclasses import asdict
import json

from agenticml.server.db import for_server_dir
from agenticml.server.db.repos import AuditRepo, RunRepo
from agenticml.server.db.workspaces import WorkspaceRepo
from agenticml.server.workspaces import default_workspace_id as _default_workspace_id, store_root
from agenticml.store import layout


def migration_versions():
    """Versions in the active migration directory, including Python migrations."""
    from agenticml.server.db import migrate

    return sorted(int(path.name[:4])
                  for path in migrate.MIGRATIONS.glob('[0-9][0-9][0-9][0-9]_*')
                  if path.suffix in ('.sql', '.py'))


def key_object(store):
    return {'version': 2, 'keys': [{k:v for k,v in asdict(row).items() if k not in ('workspace_id','account_id')} for row in store.repo.list()]}


def user_object(store):
    return {'version': 1, 'users': [{**asdict(row), 'role': 'admin' if row.role == 'owner' else row.role} for row in store.repo.list()]}


def audit_rows(directory):
    if not directory.is_dir():
        return []
    db = for_server_dir(directory, create=False)
    if db is None:
        return []
    workspace = list(reversed(AuditRepo(db, default_workspace_id(db)).tail(1000)))
    server = list(reversed(AuditRepo(db, None).tail(1000)))
    rows = workspace + server
    rows.sort(key=lambda row: row["ts"] or "")
    return rows


def audit_text(directory):
    return json.dumps(audit_rows(directory))


def as_remote(httpd, run_id):
    """Committed remote dict from the server's run index."""
    return httpd.run_index.as_remote(run_id)


def imported_remote(directory):
    """2.0-rewritten remote.json, possibly renamed to .2x by import."""
    for name in ('remote.json', 'remote.json.2x'):
        path = directory / name
        if path.is_file() and not path.is_symlink():
            return json.loads(path.read_text()), path
    raise FileNotFoundError(directory / 'remote.json')


def insert_run(index, run_id, *, owner_user=None, owner_key=None, status=None,
               last_sync_at=None, last_sync_key=None, fingerprint=None, project=None,
               created_at=None, list_row=None, last_step=None, launch_note=None, tx=None):
    index.insert({
        'run_id': run_id,
        'project': project,
        'created_at': created_at,
        'owner_user': owner_user,
        'owner_key': owner_key,
        'status': status,
        'fingerprint': fingerprint,
        'last_sync_at': last_sync_at,
        'last_sync_key': last_sync_key,
        'list_row': json.dumps(list_row if list_row is not None else {'run_id': run_id}),
        'last_step': last_step,
        'launch_note': launch_note,
        'updated_at': layout.format_timestamp(),
    }, tx)


def adopt_run(root, directory, index, *, owner=None, owner_key=None, status=None,
              last_sync_at=None, last_sync_key=None, fingerprint=None, note_body=None):
    from agenticml.server.runs import adopt_directory

    remote = {
        'owner': owner,
        'owner_key': owner_key,
        'status': status,
        'last_sync_at': last_sync_at,
        'last_sync_key': last_sync_key,
        'fingerprint': fingerprint,
    }
    return adopt_directory(root, directory, index, remote=remote, note_body=note_body,
                           overwrite=True)


def index_if_open(root, directory, remote=None, note_body=None):
    """Adopt ``directory`` when a server database is already open."""
    from agenticml.server.runs import adopt_directory

    server_dir = (root.parents[2] / 'server' if root.parent.parent.name == 'workspaces' else root.parent / 'server') if root.name == 'store' else None
    if server_dir is None or not server_dir.is_dir():
        return None
    db = for_server_dir(server_dir, create=False)
    if db is None:
        return None
    return adopt_directory(root, directory, RunRepo(db, default_workspace_id(db)), remote=remote, note_body=note_body,
                           overwrite=True)


def default_workspace_id(db):
    if not hasattr(db, 'transaction'):
        db = for_server_dir(db)
    return _default_workspace_id(db)


def default_store(data):
    """Legacy root before import, default workspace root after import."""
    db = for_server_dir(data / 'server', create=False)
    if db is None:
        return data / 'store'
    return store_root(data, default_workspace_id(db))


def transfer_default_owner(users, login):
    WorkspaceRepo(users.db).set_owner('default', login)


def on_postgres():
    import os
    return bool(os.environ.get('AGENTICML_TEST_DATABASE_URL'))


def postgres_url(base, schema, *, role='agenticml_test', password='agenticml-test-password'):
    """Preserve libpq query settings while selecting a test role and schema."""
    from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
    parts = urlsplit(base)
    host = parts.netloc.rsplit('@', 1)[-1]
    query = dict(parse_qsl(parts.query))
    query['options'] = (query.get('options', '') + ' -c search_path=' + schema).strip()
    return urlunsplit(parts._replace(
        netloc=quote(role, safe='') + ':' + quote(password, safe='') + '@' + host,
        query=urlencode(query, quote_via=quote)))


_schema_url = None


def isolate_server_dir(directory, monkeypatch):
    """Give an independent server directory its own schema in this test.

    Select before opening its repositories or starting a subprocess server.
    SQLite already isolates directories and needs no override. Returns the
    URL now selected on Postgres; None on SQLite. Uses monkeypatch so the
    env change is undone when the test ends.
    """
    if on_postgres():
        url = _schema_url(str(directory.resolve()))
        monkeypatch.setenv('AGENTICML_SERVER_DATABASE_URL', url)
        return url
    return None


def database_url(path):
    """Backend-aware URL for tests opening a database without importing files."""
    import os
    return os.environ['AGENTICML_SERVER_DATABASE_URL'] if on_postgres() else 'sqlite:///' + str(path)


def record_object(data, sha, size, *, slug='default'):
    """Record a hand-placed object in the database as the server does on upload."""
    from agenticml.server.db.usage import UsageRepo
    db = for_server_dir(data / 'server', create=False)
    wid = WorkspaceRepo(db).require(slug)['id']
    with db.transaction(write=True) as tx:
        UsageRepo(db, wid).record_object(sha, size, tx)


def write_22_data_dir(data, local, *, runs=('old-1', 'old-2')):
    """Build a 2.2-shaped server data directory by writing the 2.2 files directly.

    users.json (admin x2, member, viewer), keys.json (version 2, bound to
    users), audit.jsonl, setup-closed, and ``store/`` with one directory per
    run (copied from a real local writer, plus 2.2 remote.json and notes.md),
    ``store/objects`` and a bare lineage repository. Returns the secrets and
    the run writers so tests can keep syncing the same runs.
    """
    import hashlib
    import shutil
    import subprocess
    from agenticml.lineage import project_key
    from agenticml.server.app import prepare_data_dir
    from agenticml.store import RunWriter

    prepare_data_dir(data)
    server = data / 'server'

    def user(name, role, created):
        return {'name': name, 'id': hashlib.sha256(name.encode()).hexdigest()[:32], 'role': role,
                'identity': None, 'password': None, 'invite': None, 'generation': 0,
                'created_at': created, 'created_by': 'cli'}

    (server / 'users.json').write_text(json.dumps({'version': 1, 'users': [
        user('alice', 'admin', '2025-01-01T00:00:00Z'), user('dave', 'admin', '2025-03-01T00:00:00Z'),
        user('bob', 'member', '2025-02-01T00:00:00Z'), user('carol', 'viewer', '2025-02-02T00:00:00Z')]}))
    secrets = {'alice-admin': 'salice' + 'x' * 32, 'bob-sync': 'sbob' + 'y' * 32,
               'carol-sync': 'scarol' + 'z' * 32}
    owners = {'alice-admin': ('alice', 'admin'), 'bob-sync': ('bob', 'sync'), 'carol-sync': ('carol', 'sync')}
    (server / 'keys.json').write_text(json.dumps({'version': 2, 'keys': [
        {'name': name, 'scope': owners[name][1], 'sha256': hashlib.sha256(secret.encode()).hexdigest(),
         'prefix': secret[:8], 'created_at': '2025-02-03T00:00:00Z', 'created_by': 'cli',
         'last_used_at': None, 'user': owners[name][0]} for name, secret in secrets.items()]}))
    (server / 'audit.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in (
        {'ts': '2025-02-04T00:00:00Z', 'actor': 'alice', 'via': 'cli', 'action': 'key.create',
         'target': 'bob-sync', 'client': None},
        {'ts': '2025-02-05T00:00:00Z', 'actor': 'bob', 'via': 'web', 'action': 'login.ok',
         'target': None, 'client': None})))
    (server / 'setup-closed').write_text('')
    store = data / 'store'
    writers = {}
    for index, run_id in enumerate(runs):
        writer = RunWriter.create(local, run_id=run_id, meta={'project': 'demo', 'name': run_id})
        writer.write_scalars([(i, {'loss': 1 / (i + 1)}) for i in range(8)])
        writer.add_code([], [('train.py', ('print(%d)\n' % index).encode())])
        writers[run_id] = writer
        target = store / 'runs' / run_id
        shutil.copytree(writer.run_dir, target, ignore=shutil.ignore_patterns('sync.json'))
        sizes = {path.relative_to(target).as_posix(): path.stat().st_size
                 for path in target.rglob('*') if path.is_file()}
        (target / 'remote.json').write_text(json.dumps({
            'owner': 'bob', 'owner_key': 'bob-sync', 'status': 'running',
            'last_sync_at': '2025-02-06T00:00:00Z', 'last_sync_key': 'bob-sync',
            'fingerprint': 'f' * 8, 'sizes': {k: v for k, v in sizes.items() if k != 'remote.json'}}))
        if index == 0:
            (target / 'notes.md').write_text('first imported note\n')
        for sha_dir in (writer.root / 'objects').glob('*') if (writer.root / 'objects').is_dir() else ():
            shutil.copytree(sha_dir, store / 'objects' / sha_dir.name, dirs_exist_ok=True)
    git = shutil.which('git')
    if git:
        repo = store / 'lineage' / (project_key('demo') + '.git')
        subprocess.run([git, 'init', '--bare', '-q', str(repo)], check=True)
    return secrets, writers

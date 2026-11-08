"""Repository-based inspection and legacy fixtures for server storage tests."""
from dataclasses import asdict
import json

from agenticml.server.db import for_server_dir
from agenticml.server.db.repos import AuditRepo, RunRepo
from agenticml.server.db.workspaces import WorkspaceRepo
from agenticml.server.workspaces import default_workspace_id as _default_workspace_id, store_root
from agenticml.store import layout


def key_object(store):
    return {'version': 2, 'keys': [{k:v for k,v in asdict(row).items() if k not in ('workspace_id','account_id')} for row in store.repo.list()]}


def user_object(store):
    return {'version': 1, 'users': [{**asdict(row), 'role': 'admin' if row.role == 'owner' else row.role} for row in store.repo.list()]}


def audit_rows(directory):
    db = for_server_dir(directory, create=False)
    return list(reversed(AuditRepo(db, default_workspace_id(db)).tail(1000))) if db is not None else []


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

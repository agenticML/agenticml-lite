"""Workspace store layout and crash-resumable single-team conversion."""
import errno
import os
from pathlib import Path
import shutil
import stat

from agenticml.store import layout
from .db.errors import DatabaseError
from .db.repos import SettingsRepo
from .db.workspaces import WorkspaceRepo, require_workspace_id
from .runs import store_paths_safe


def default_workspace_id(db):
    """Id of the self-hosted ``default`` workspace.

    Callers that still talk to one workspace (the 2.2 dashboard until 18.1b,
    and CLI ``--workspace`` default) resolve it here, not inside stores.
    """
    return WorkspaceRepo(db).require('default')['id']


def store_root(data, workspace_id):
    return Path(data) / 'workspaces' / require_workspace_id(workspace_id) / 'store'


def _directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise DatabaseError('workspace store path is a symlink or not a directory; fix or move it away')


def prepare_store(data, workspace_id):
    data = Path(data)
    root = store_root(data, workspace_id)
    for path in (data / 'workspaces', root.parent, root):
        _directory(path)
    if not store_paths_safe(root):
        raise DatabaseError('workspace store or runs is a symlink or not a directory; fix or move it away')
    tmp = data / 'server' / 'tmp'
    if tmp.exists() and tmp.stat().st_dev != root.stat().st_dev:
        raise DatabaseError('server/tmp and workspaces must be on the same filesystem')
    return root


def discard_store(data, workspace_id):
    """Remove workspaces/<id>/ without following symlinks. Missing is OK."""
    wid = require_workspace_id(workspace_id)
    parent = Path(data) / 'workspaces'
    try:
        parent_st = parent.lstat()
    except OSError:
        return
    if stat.S_ISLNK(parent_st.st_mode) or not stat.S_ISDIR(parent_st.st_mode):
        return
    target = parent / wid
    try:
        st = target.lstat()
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        try:
            os.unlink(target)
        except OSError:
            pass
        return
    for root, dirs, files in os.walk(target, topdown=False, followlinks=False):
        for name in files:
            try:
                os.unlink(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            path = os.path.join(root, name)
            try:
                dst = os.lstat(path)
                if stat.S_ISLNK(dst.st_mode):
                    os.unlink(path)
                else:
                    os.rmdir(path)
            except OSError:
                pass
    try:
        os.rmdir(target)
    except OSError:
        pass


def convert_store(db, data):
    """Rename first, then mark. A crash after rename is safe to resume."""
    data = Path(data)
    settings = SettingsRepo(db)
    default = WorkspaceRepo(db).get('default')
    old = data / 'store'
    if not store_paths_safe(old):
        raise DatabaseError('store or store/runs is a symlink or not a directory; fix or move it away')
    _directory(data / 'workspaces')
    tmp = data / 'server' / 'tmp'
    if tmp.exists() and tmp.stat().st_dev != (data / 'workspaces').stat().st_dev:
        raise DatabaseError('server/tmp and workspaces must be on the same filesystem')
    if settings.get('workspace_store_converted') is None:
        if default is None:
            for workspace in WorkspaceRepo(db).list():
                prepare_store(data, workspace['id'])
            return
        root = store_root(data, default['id'])
        _directory(root.parent)
        if not os.path.lexists(root) and os.path.lexists(old):
            workspaces = data / 'workspaces'
            try:
                if old.stat().st_dev != workspaces.stat().st_dev:
                    raise DatabaseError('server/tmp and workspaces must be on the same filesystem')
                os.rename(old, root)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise DatabaseError('server/tmp and workspaces must be on the same filesystem') from exc
                raise
            layout.fsync_dir(root.parent)
            layout.fsync_dir(root.parent.parent)
            layout.fsync_dir(data)
        elif os.path.lexists(old) and os.path.lexists(root):
            raise DatabaseError('both legacy and default workspace stores exist; fix or move the legacy store away')
        settings.set('workspace_store_converted', layout.format_timestamp())
    for workspace in WorkspaceRepo(db).list():
        prepare_store(data, workspace['id'])


def delete_workspace(db, data, slug):
    row = WorkspaceRepo(db).require(slug)
    root = prepare_store(data, row['id'])
    # No SQLite transaction is held across filesystem operations.
    WorkspaceRepo(db).delete(slug)
    shutil.rmtree(root.parent)
    layout.fsync_dir(root.parent.parent)
    return row

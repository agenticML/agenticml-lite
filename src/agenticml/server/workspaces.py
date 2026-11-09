"""Workspace store layout and crash-resumable single-team conversion."""
import errno
import logging
import os
from pathlib import Path
import shutil
import stat

from agenticml.store import layout
from .db.errors import DatabaseError
from .db.repos import SettingsRepo
from .db.workspaces import WorkspaceRepo, require_workspace_id
from .runs import store_paths_safe

logger = logging.getLogger('agenticml.server.workspaces')


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


def _rmtree_at(parent_fd, name):
    """Delete ``name`` relative to ``parent_fd`` without following symlinks."""
    try:
        shutil.rmtree(name, ignore_errors=True, dir_fd=parent_fd)
        return
    except TypeError:
        pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError:
            pass
        return
    try:
        try:
            entries = [entry.name for entry in os.scandir(fd)]
        except OSError:
            entries = []
        for entry in entries:
            try:
                st = os.lstat(entry, dir_fd=fd)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                _rmtree_at(fd, entry)
            else:
                try:
                    os.unlink(entry, dir_fd=fd)
                except OSError:
                    pass
    finally:
        os.close(fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        pass


def discard_store(data, workspace_id):
    """Remove workspaces/<id>/ without following symlinks. Missing is OK."""
    wid = require_workspace_id(workspace_id)
    parent = Path(data) / 'workspaces'
    if not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW'):
        logger.warning('discard_store skipped: descriptor-relative deletion is unavailable')
        return
    if not getattr(shutil.rmtree, 'avoids_symlink_attacks', False):
        logger.warning('discard_store skipped: shutil.rmtree cannot avoid symlink attacks')
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        parent_fd = os.open(parent, flags)
    except OSError:
        return
    try:
        st = os.fstat(parent_fd)
        if not stat.S_ISDIR(st.st_mode):
            return
        if st.st_uid != os.geteuid():
            logger.warning('discard_store skipped: workspaces directory is not owned by this process')
            return
        try:
            child = os.lstat(wid, dir_fd=parent_fd)
        except OSError:
            return
        if stat.S_ISLNK(child.st_mode) or not stat.S_ISDIR(child.st_mode):
            try:
                os.unlink(wid, dir_fd=parent_fd)
            except OSError:
                pass
            return
        _rmtree_at(parent_fd, wid)
    finally:
        os.close(parent_fd)


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

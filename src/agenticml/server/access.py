"""Explicit workspace binding shared by sync and future dashboard dispatch."""
from dataclasses import dataclass, field
from pathlib import Path

from .db.repos import RunRepo, NoteRepo
from .db.workspaces import WorkspaceRepo, MembershipRepo, require_workspace_id
from .users import Principal, allowed
from .workspaces import store_root


class WorkspaceNotFound(ValueError):
    def __init__(self):
        super().__init__('not found')


class WorkspaceSuspended(ValueError):
    def __init__(self):
        super().__init__('workspace suspended')


def workspace_membership(db, workspace_id, account_id):
    return MembershipRepo(db, workspace_id).get(account_id)


def require_workspace_member(db, workspace_id, account_id, *, key_workspace_id=None):
    workspace = WorkspaceRepo(db).by_id(workspace_id)
    if (workspace is None or workspace['status'] != 'active' or
            key_workspace_id is not None and key_workspace_id != workspace_id):
        raise WorkspaceNotFound()
    member = workspace_membership(db, workspace_id, account_id)
    if member is None:
        raise WorkspaceNotFound()
    return member


def require_workspace_manager(db, workspace_id, account_id, **kwargs):
    member = require_workspace_member(db, workspace_id, account_id, **kwargs)
    if member['role'] not in ('owner', 'admin'):
        raise PermissionError('forbidden')
    return member


class WorkspaceStores:
    """UserStore, KeyStore and AuditLog for one workspace, built on first use."""

    def __init__(self, server, workspace_id):
        self._server = server
        self._workspace_id = workspace_id
        self._users = self._keys = self._audit = None

    @property
    def users(self):
        if self._users is None:
            from .users import UserStore
            self._users = UserStore(self._server.data_dir / 'server', db=self._server.db,
                                    workspace_id=self._workspace_id)
        return self._users

    @property
    def keys(self):
        if self._keys is None:
            from .keys import KeyStore
            self._keys = KeyStore(self._server.data_dir / 'server', db=self._server.db,
                                  workspace_id=self._workspace_id)
        return self._keys

    @property
    def audit(self):
        if self._audit is None:
            from .audit import AuditLog
            self._audit = AuditLog(self._server.data_dir / 'server', db=self._server.db,
                                   workspace_id=self._workspace_id)
        return self._audit


@dataclass(frozen=True)
class WorkspaceContext:
    id: str
    slug: str
    root: Path
    principal: Principal
    run_index: RunRepo
    note_index: NoteRepo
    server: object
    _stores: object = field(repr=False, compare=False)

    @property
    def users(self):
        return self._stores.users

    @property
    def keys(self):
        return self._stores.keys

    @property
    def audit(self):
        return self._stores.audit

    def run_reader(self, run_id):
        return self.server.run_reader(run_id, workspace_id=self.id)

    def cached_reader(self, run_id):
        return self.server.cached_reader(run_id, workspace_id=self.id)

    def code_pair_diff(self, base_sha, target_sha, budget):
        return self.server.code_pair_diff(base_sha, target_sha, budget, workspace_id=self.id)

    def run_lock(self, run_id):
        return self.server.run_lock(run_id, workspace_id=self.id)

    def object_lock(self, sha):
        return self.server.object_lock(sha, workspace_id=self.id)


def bind_sync(server, record):
    from .users import resolve

    wid = require_workspace_id(record.workspace_id)
    workspace = WorkspaceRepo(server.db).by_id(wid)
    if workspace is None:
        server.drop_workspace_stores(wid)
        raise WorkspaceNotFound()
    if workspace['status'] != 'active':
        raise WorkspaceSuspended()
    import os
    import stat
    root = store_root(server.data_dir, wid)
    try:
        paths = (root.parent.parent, root.parent) + ((root,) if os.path.lexists(root) else ())
        safe = all(stat.S_ISDIR(p.lstat().st_mode) for p in paths)
    except OSError:
        safe = False
    if not safe:
        raise WorkspaceNotFound()
    stores = server.workspace_stores(wid, workspace['status'])
    principal = resolve(None, stores.users, record)
    if principal is None:
        raise WorkspaceNotFound()
    return WorkspaceContext(wid, workspace['slug'], root, principal,
                            RunRepo(server.db, wid), NoteRepo(server.db, wid), server, stores)

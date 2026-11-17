"""Read-only inventories of committed files and objects for operator commands."""
from .workspaces import WorkspaceBound, require_workspace_id


class InventoryRepo(WorkspaceBound):
    def __init__(self, db, workspace_id):
        self.db = db
        self.workspace_id = require_workspace_id(workspace_id)

    def objects(self, tx=None):
        """sha256 -> size for every recorded object of the workspace."""
        if tx is None:
            with self._tx() as tx:
                return self.objects(tx)
        with self._bind(tx):
            return {row['sha256']: row['size'] for row in tx.fetchall(
                'SELECT sha256, size FROM objects WHERE workspace_id=?', (self.workspace_id,))}

    def run_files(self, tx=None):
        """run_id -> path -> {size, sha256} for every committed run file."""
        if tx is None:
            with self._tx() as tx:
                return self.run_files(tx)
        with self._bind(tx):
            files = {}
            for row in tx.fetchall(
                    'SELECT run_id, path, size, sha256 FROM run_files WHERE workspace_id=?',
                    (self.workspace_id,)):
                files.setdefault(row['run_id'], {})[row['path']] = {
                    'size': row['size'], 'sha256': row['sha256']}
            return files

    def run_ids(self, tx=None):
        if tx is None:
            with self._tx() as tx:
                return self.run_ids(tx)
        with self._bind(tx):
            return {row['run_id'] for row in tx.fetchall(
                'SELECT run_id FROM runs WHERE workspace_id=?', (self.workspace_id,))}

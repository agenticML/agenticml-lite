"""One-time object inventory after the legacy store has been converted."""
import logging
import stat

from agenticml.store import layout
from .repos import SettingsRepo
from .usage import UsageRepo
from .workspaces import WorkspaceRepo

logger = logging.getLogger(__name__)


def _entries(directory):
    try:
        yield from directory.iterdir()
    except OSError as exc:
        logger.warning('Skipping unreadable object directory %s: %s', directory, exc)


def _scan(root):
    objects = root / layout.OBJECTS_DIRNAME
    try:
        safe = all(stat.S_ISDIR(p.lstat().st_mode)
                   for p in (root.parent.parent, root.parent, root, objects))
    except FileNotFoundError as exc:
        log = logger.debug if exc.filename == str(objects) else logger.warning
        log('Skipping unsafe or missing workspace store %s: %s', root, exc)
        return []
    except OSError as exc:
        logger.warning('Skipping unsafe or missing workspace store %s: %s', root, exc)
        return []
    if not safe:
        logger.warning('Skipping unsafe or missing workspace store %s', root)
        return []
    rows = []
    for prefix in _entries(objects):
        try:
            if not stat.S_ISDIR(prefix.lstat().st_mode):
                continue
            for path in _entries(prefix):
                if not layout.valid_sha(path.name) or path.name[:2] != prefix.name:
                    continue
                try:
                    info = path.lstat()
                except OSError as exc:
                    logger.warning('Skipping unreadable object %s: %s', path, exc)
                    continue
                if stat.S_ISREG(info.st_mode):
                    rows.append((path.name, info.st_size))
        except OSError as exc:
            logger.warning('Skipping unreadable object prefix %s: %s', prefix, exc)
    return rows


def backfill_objects(db, data):
    from ..workspaces import store_root

    settings = SettingsRepo(db)
    if settings.get('objects_backfilled') is not None:
        return
    for workspace in WorkspaceRepo(db).list():
        wid = workspace['id']
        # All filesystem operations happen before taking database locks.
        rows = _scan(store_root(data, wid))
        with db.transaction(write=True) as tx:
            tx.advisory_lock('objects_backfill')
            if settings.get('objects_backfilled', tx) is not None:
                return
            if WorkspaceRepo(db).by_id(wid, tx) is None:
                continue
            usage = UsageRepo(db, wid)
            usage.lock(tx)
            tx.bind_workspace(wid)
            inserted_size = 0
            for sha, size in rows:
                result = tx.execute(
                    'INSERT INTO objects (workspace_id, sha256, size, created_at) VALUES (?, ?, ?, ?) '
                    'ON CONFLICT (workspace_id, sha256) DO NOTHING',
                    (wid, sha, size, layout.format_timestamp()))
                if result.rowcount:
                    inserted_size += size
            usage.add_stored(inserted_size, tx)
    with db.transaction(write=True) as tx:
        tx.advisory_lock('objects_backfill')
        settings.set('objects_backfilled', layout.format_timestamp(), tx)

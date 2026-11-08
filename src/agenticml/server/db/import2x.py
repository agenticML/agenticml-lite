"""Import legacy state exactly once, then retire the old files."""
import logging
import os
from pathlib import Path
import stat

from agenticml.store import layout
from .errors import DatabaseError
from .repos import AuditRepo, KeyRepo, NoteRepo, RunRepo, SettingsRepo, UserRepo

logger = logging.getLogger('agenticml.server.db.import2x')


def _exists(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DatabaseError(f'could not import {path.name}: unreadable file; fix or move it away') from exc


def import_state(db, server_dir, *, actor='server'):
    directory = Path(server_dir)
    settings = SettingsRepo(db)
    if settings.get('imported_2x') is None:
        _import_users_keys(db, directory, actor=actor)
    _import_runs(db, directory)


def _import_users_keys(db, directory, *, actor='server'):
    from ..keys import _parse_keys_object
    from ..users import UserStore
    from .legacy2x import read_json
    import json

    settings = SettingsRepo(db)
    # Serialize legacy conversion and import across openers as well as threads.
    # The conversion uses keys.lock itself; import is guarded by BEGIN IMMEDIATE.
    imported = []
    skipped = 0
    skipped_keys = 0
    with db.transaction(write=True) as tx:
        tx.advisory_lock('import')
        if settings.get('imported_2x', tx) is not None:
            return
        for name in ('users.json', 'keys.json'):
            path = directory / name
            try:
                if _exists(path):
                    read_json(path)
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                raise DatabaseError(f'could not import {name}: invalid or unreadable file; '
                                    'fix or move it away') from exc
        if not _exists(directory / 'users.json') and _exists(directory / 'keys.json'):
            # Migration audit uses this transaction too: no nested transaction.
            from . import legacy2x
            if not legacy2x.migrate(directory.parent, actor=actor, db=_ImportAuditDb(db, tx)):
                raise DatabaseError('could not import keys.json: legacy migration failed; fix or move it away')
        users = UserRepo(db, directory)
        keys = KeyRepo(db, directory)
        for name in ('users.json', 'keys.json'):
            path = directory / name
            if not _exists(path):
                continue
            try:
                obj = read_json(path)
                if name == 'users.json':
                    if (not isinstance(obj, dict) or type(obj.get('version')) is not int
                            or obj['version'] != 1 or not isinstance(obj.get('users'), list)):
                        raise ValueError('invalid users.json')
                    names, identities, ids = set(), set(), set()
                    for row in obj['users']:
                        users.save(tx, UserStore._parse_row(row, names, identities, ids))
                else:
                    known = {u.name for u in users.list(tx)}
                    orphaned = 0
                    for record in _parse_keys_object(obj, (2,)):
                        if record.user not in known:
                            orphaned += 1
                            continue
                        keys.insert(tx, record)
                    skipped_keys = orphaned
            except Exception as exc:
                raise DatabaseError(f'could not import {name}: invalid or unreadable records; '
                                    'fix or move it away') from exc
            imported.append(path)
        marker = directory / 'setup-closed'
        if _exists(marker):
            settings.set('setup_closed', layout.format_timestamp(), tx)
            imported.append(marker)
        path = directory / 'audit.jsonl'
        if _exists(path):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                fd = os.open(path, flags)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise ValueError('not a regular file')
                    file = os.fdopen(fd, 'rb')
                except BaseException:
                    os.close(fd)
                    raise
                # Read forward with a bounded buffer, retaining file order for
                # events whose timestamps tie. Drain oversized lines once.
                with file:
                    while True:
                        line = file.readline(1024 * 1024 + 2)
                        if not line:
                            break
                        if not line.endswith(b'\n') or len(line) > 1024 * 1024 + 1:
                            while line and not line.endswith(b'\n'):
                                line = file.readline(64 * 1024)
                            skipped += 1
                            continue
                        try:
                            event = json.loads(line.decode('utf-8'))
                            if (not isinstance(event, dict) or not isinstance(event.get('ts'), str)
                                    or any(event.get(k) is not None and not isinstance(event[k], str)
                                           for k in ('actor', 'via', 'action', 'target', 'client'))):
                                raise ValueError('invalid audit event')
                            AuditRepo(db).insert(event, tx)
                        except (ValueError, RecursionError):
                            skipped += 1
            except (OSError, ValueError):
                skipped += 1
            imported.append(path)
        settings.set('imported_2x', layout.format_timestamp(), tx)
    if skipped:
        logger.warning('skipped %d malformed or unreadable legacy audit line(s)', skipped)
    if skipped_keys:
        logger.warning('skipped %d key(s) whose user is not among the imported users', skipped_keys)
    for path in imported:
        dest = path.with_name(path.name + '.2x')
        try:
            dest.lstat()
        except FileNotFoundError:
            try:
                path.rename(dest)
            except OSError:
                logger.warning('could not rename imported %s', path.name)
        except OSError:
            logger.warning('could not rename imported %s', path.name)
        else:
            logger.warning('skipping rename of imported %s; %s already exists', path.name, dest.name)
    try:
        (directory / 'keys.lock').unlink(missing_ok=True)
    except OSError:
        logger.warning('could not remove legacy keys.lock')


def _import_runs(db, directory):
    from ..runs import (
        _last_step, _meta_fields, _on_disk_files, _read_meta, adopt_directory,
        read_note_file, remote_from_file, store_paths_safe,
    )

    settings = SettingsRepo(db)
    if settings.get('imported_runs_2x') is not None:
        return
    root = directory.parent / 'store'
    if not store_paths_safe(root):
        raise DatabaseError('could not import runs: store or store/runs is a symlink or not a '
                            'directory; fix or move it away')
    runs_root = root / 'runs'
    run_index = RunRepo(db)
    try:
        entries = list(os.scandir(runs_root))
    except FileNotFoundError:
        entries = []
    except OSError as exc:
        raise DatabaseError('could not import runs: unreadable runs directory; '
                            'fix or move it away') from exc
    prepared = []
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        run_dir = Path(entry.path)
        remote_path = run_dir / 'remote.json'
        notes_path = run_dir / 'notes.md'
        remote = None
        try:
            if _exists(remote_path):
                remote = remote_from_file(run_dir)
                if remote is None:
                    logger.warning('import skipped invalid or symlinked remote.json in %s',
                                   entry.name)
        except (OSError, ValueError):
            logger.warning('import skipped invalid or symlinked remote.json in %s', entry.name)
            remote = None
        note_body = None
        try:
            if _exists(notes_path):
                note_body = read_note_file(run_dir)
        except (OSError, ValueError):
            note_body = None
        meta = _read_meta(run_dir)
        if meta is None:
            continue
        if meta.get('run_id') != entry.name:
            meta = {**meta, 'run_id': entry.name}
        fields = _meta_fields(meta)
        files = _on_disk_files(root, run_dir)
        last_step = _last_step(run_dir, files)
        rename_remote = remote_path if _exists(remote_path) else None
        rename_notes = notes_path if note_body is not None and _exists(notes_path) else None
        prepared.append((run_dir, remote, note_body, files, fields, last_step,
                         rename_remote, rename_notes))
    renamed = []
    with db.transaction(write=True) as tx:
        tx.advisory_lock('import_runs')
        if settings.get('imported_runs_2x', tx) is not None:
            return
        for (run_dir, remote, note_body, files, fields, last_step,
             rename_remote, rename_notes) in prepared:
            adopted = adopt_directory(
                root, run_dir, run_index, remote=remote, note_body=note_body,
                files=files, fields=fields, last_step=last_step, tx=tx, overwrite=True)
            if adopted is None:
                continue
            if rename_remote is not None:
                renamed.append(rename_remote)
            if rename_notes is not None:
                renamed.append(rename_notes)
        settings.set('imported_runs_2x', layout.format_timestamp(), tx)
    for path in renamed:
        dest = path.with_name(path.name + '.2x')
        try:
            dest.lstat()
        except FileNotFoundError:
            try:
                path.rename(dest)
            except OSError:
                logger.warning('could not rename imported %s', path.name)
        except OSError:
            logger.warning('could not rename imported %s', path.name)
        else:
            logger.warning('skipping rename of imported %s; %s already exists', path.name, dest.name)


class _ImportAuditDb:
    """Let the legacy migration's best-effort audit join the import transaction."""
    def __init__(self, db, tx):
        self.db = db
        self.tx = tx

    def transaction(self, write=False):
        from contextlib import nullcontext
        return nullcontext(self.tx)

"""Import legacy state exactly once, then retire the old files."""
from contextlib import nullcontext
import logging
import os
from pathlib import Path
import stat

from agenticml.store import layout
from .errors import ConvertedTwice, DatabaseError
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


def _after_commit(stage):
    """Test hook between a conversion commit and its file renames."""


def _retire_file(path):
    """Rename an imported 2.x file to ``<name>.2x``, never replacing an archive."""
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


def _converted_on(settings, name):
    stamp = settings.get(name)
    return stamp[:10] if isinstance(stamp, str) else 'an earlier start'


def _guard_converted(db, directory):
    """After conversion: refuse reappeared 2.x state; finish an interrupted retirement.

    The renames come after the database commit, so a file with no ``.2x``
    beside it is a conversion that stopped in between, not a second 2.x
    copy. Nothing is imported again either way.
    """
    settings = SettingsRepo(db)
    imported = settings.get('imported_2x')
    if imported is not None:
        for name in ('users.json', 'keys.json', 'audit.jsonl', 'setup-closed'):
            path = directory / name
            if not _exists(path):
                continue
            if not _exists(path.with_name(name + '.2x')):
                _retire_file(path)
            elif name != 'setup-closed':
                raise ConvertedTwice(
                    f'found 2.x {path} after this server was converted on '
                    f'{_converted_on(settings, "imported_2x")}; move it away '
                    f'(the converted copy is {path}.2x)')
    converted = settings.get('workspace_store_converted')
    if converted is not None:
        data = directory.parent
        from ..workspaces import store_root
        from .workspaces import WorkspaceRepo
        default = WorkspaceRepo(db).get('default')
        copy = store_root(data, default['id']) if default else data / 'workspaces'
        for runs in (data / 'store' / 'runs', data / 'runs'):
            if _has_run_directories(runs):
                raise ConvertedTwice(
                    f'found 2.x {runs} after this server was converted on '
                    f'{_converted_on(settings, "workspace_store_converted")}; move it away '
                    f'(the converted copy is {copy})')


def _has_run_directories(path):
    try:
        with os.scandir(path) as entries:
            return any(entry.is_dir(follow_symlinks=False) for entry in entries)
    except FileNotFoundError:
        return False
    except NotADirectoryError:
        return False
    except OSError as exc:
        raise DatabaseError(f'could not inspect {path}: unreadable; fix or move it away') from exc


def import_state(db, server_dir, *, actor='server'):
    # The transaction import lock protects rows, but legacy file preparation
    # and retirement also need serialization when multiple processes start.
    with db.session_lock('import:2x', timeout=600) if db.dialect.name == 'postgres' else nullcontext():
        directory = Path(server_dir)
        settings = SettingsRepo(db)
        _guard_converted(db, directory)
        if settings.get('imported_2x') is None:
            _import_users_keys(db, directory, actor=actor)
        from ..workspaces import convert_store
        convert_store(db, directory.parent)
        _import_runs(db, directory)


def _import_users_keys(db, directory, *, actor='server'):
    from ..keys import _parse_keys_object
    from ..users import UserStore
    from .legacy2x import read_json, migrate
    from .workspaces import WorkspaceRepo
    import json

    settings = SettingsRepo(db)
    imported = []
    skipped = 0
    skipped_keys = 0
    # Prepare all files and legacy rewrites before opening a SQLite writer.
    for name in ('users.json', 'keys.json'):
        path = directory / name
        try:
            if _exists(path):
                read_json(path)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise DatabaseError(f'could not import {name}: invalid or unreadable file; fix or move it away') from exc
    if not _exists(directory / 'users.json') and _exists(directory / 'keys.json'):
        if not migrate(directory.parent, actor=actor, db=db):
            raise DatabaseError('could not import keys.json: legacy migration failed; fix or move it away')
    prepared_users, prepared_keys, events = [], [], []
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
                prepared_users = [UserStore._parse_row(row, names, identities, ids) for row in obj['users']]
            else:
                prepared_keys = _parse_keys_object(obj, (2,))
        except Exception as exc:
            raise DatabaseError(f'could not import {name}: invalid or unreadable records; fix or move it away') from exc
        imported.append(path)
    marker = directory / 'setup-closed'
    closed = _exists(marker)
    if closed:
        imported.append(marker)
    path = directory / 'audit.jsonl'
    if _exists(path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError('not a regular file')
                file = os.fdopen(fd, 'rb')
            except BaseException:
                os.close(fd)
                raise
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
                        events.append(event)
                    except (ValueError, RecursionError):
                        skipped += 1
        except (OSError, ValueError):
            skipped += 1
        imported.append(path)
    with db.transaction(write=True) as tx:
        tx.advisory_lock('import')
        tx.advisory_lock('users')
        if settings.get('imported_2x', tx) is not None:
            return
        wid = WorkspaceRepo(db).get('default', tx)['id']
        users = UserRepo(db, wid)
        keys = KeyRepo(db, wid)
        for record in sorted(prepared_users, key=lambda u: (u.created_at, u.name)):
            users.save(tx, record)
        known = {u.name for u in users.list(tx)}
        for record in prepared_keys:
            if record.user not in known:
                skipped_keys += 1
                continue
            keys.insert(tx, record)
        if closed:
            settings.set('setup_closed', layout.format_timestamp(), tx)
        for event in events:
            AuditRepo(db, wid).insert(event, tx)
        settings.set('imported_2x', layout.format_timestamp(), tx)
    if skipped:
        logger.warning('skipped %d malformed or unreadable legacy audit line(s)', skipped)
    if skipped_keys:
        logger.warning('skipped %d key(s) whose user is not among the imported users', skipped_keys)
    _after_commit('users')
    for path in imported:
        _retire_file(path)
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
        if settings.get('runs_2x_retired') is None:
            _finish_run_retirement(db, directory)
        return
    from .workspaces import WorkspaceRepo
    from ..workspaces import store_root
    default = WorkspaceRepo(db).get('default')
    if default is None:
        return
    wid = default['id']
    root = store_root(directory.parent, wid)
    if not store_paths_safe(root):
        raise DatabaseError('could not import runs: store or store/runs is a symlink or not a '
                            'directory; fix or move it away')
    runs_root = root / 'runs'
    run_index = RunRepo(db, wid)
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
    imported = []
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
            imported.append(adopted)
            if rename_remote is not None:
                renamed.append(rename_remote)
            if rename_notes is not None:
                renamed.append(rename_notes)
        settings.set('imported_runs_2x', layout.format_timestamp(), tx)
    _after_commit('runs')
    for run_id in imported:
        run_index.rebuild_metrics(run_id, root)
    for path in renamed:
        _retire_file(path)
    settings.set('runs_2x_retired', layout.format_timestamp())


def _finish_run_retirement(db, directory):
    """Rename remote.json/notes.md left behind by a conversion that stopped before the renames."""
    from .workspaces import WorkspaceRepo
    from ..workspaces import store_root
    default = WorkspaceRepo(db).get('default')
    if default is None:
        return
    root = store_root(directory.parent, default['id'])
    run_index = RunRepo(db, default['id'])
    for row in run_index.all_rows():
        # the interrupted conversion may have stopped before the post-commit rebuild
        try:
            run_index.rebuild_metrics(row['run_id'], root)
        except OSError:
            logger.warning('could not rebuild metric summaries for run %s', row['run_id'])
    try:
        entries = list(os.scandir(root / 'runs'))
    except OSError:
        return
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        for name in ('remote.json', 'notes.md'):
            path = Path(entry.path) / name
            if _exists(path):
                _retire_file(path)
    SettingsRepo(db).set('runs_2x_retired', layout.format_timestamp())

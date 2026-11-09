"""Consistent backup of a running server: database first, then the files it commits.

Every append truncates the run file to its committed size first, and the
database records committed sizes. A copy of the database taken *before* the
files is therefore consistent with any later copy of the files whose run
files are at least as long as the committed sizes; this module copies each
file only up to the size the snapshot recorded.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import stat
import subprocess

from agenticml import lineage
from agenticml.store import layout
from .admin import _data_root
from .app import resolve_data_dir
from .db import _is_postgres, _path, for_server_dir, resolve_url
from .db.inventory import InventoryRepo
from .db.sqlite import SqliteDatabase
from .db.workspaces import WorkspaceRepo
from .lineage import _CONFIG
from .runs import is_atomic_file
from .workspaces import store_root

VERSION = 1
_CHUNK = 1024 * 1024
_S3_NOTE = ('objects are in S3 and were not copied; bucket versioning and AWS Backup '
            'cover them (the hosted deployment enables both)')


class BackupError(ValueError):
    """A refusal or a failure that stops the backup; the message is safe to print."""


def pg_dump_note(to):
    return ('Postgres backups need a database dump taken first. Run:\n'
            f'  pg_dump --format=custom "$DATABASE_URL" > {shlex.quote(str(Path(to) / "server.dump"))}\n'
            'then run this command again with --files-only. '
            'In the hosted service, RDS automated snapshots cover the database.')


class _Writer:
    """Creates files 0600 and directories 0700, remembering directories to fsync."""

    def __init__(self, base):
        self.base = base
        self.directories = {base}
        self.files = 0
        self.bytes = 0

    def directory(self, path):
        missing = []
        while path != self.base and path not in self.directories:
            missing.append(path)
            path = path.parent
        for item in reversed(missing):
            try:
                os.mkdir(item, 0o700)
            except FileExistsError:
                if not stat.S_ISDIR(item.lstat().st_mode):
                    raise
            self.directories.add(item)
            self.directories.add(item.parent)

    def create(self, path):
        self.directory(path.parent)
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)

    def put(self, path, chunks):
        """Write chunks to a new file; fsynced before returning."""
        fd = self.create(path)
        written = 0
        try:
            with os.fdopen(fd, 'wb') as handle:
                fd = None
                for chunk in chunks:
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd is not None:
                os.close(fd)
        self.files += 1
        self.bytes += written
        return written

    def sync(self):
        for directory in sorted(self.directories, key=lambda item: -len(item.parts)):
            layout.fsync_dir(directory)


def _open_source(root, path):
    """Open a regular file inside ``root`` without following symlinks."""
    try:
        resolved = layout.contained(root, path)
    except (ValueError, RuntimeError) as exc:
        raise ValueError('symlinked path') from exc
    if resolved != path:
        raise ValueError('symlinked path')
    handle = layout.open_nofollow(path)
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise ValueError('not a regular file')
    return handle


def _read_limited(handle, limit):
    remaining = limit
    while remaining is None or remaining > 0:
        chunk = handle.read(_CHUNK if remaining is None else min(_CHUNK, remaining))
        if not chunk:
            return
        if remaining is not None:
            remaining -= len(chunk)
        yield chunk


def _check_destination(to, data):
    if to.is_symlink():
        raise BackupError('refusing a symlink as the backup directory')
    parent = to.parent.resolve()
    target = parent / to.name
    resolved_data = data.resolve()
    if target == resolved_data or resolved_data in target.parents:
        raise BackupError('the backup directory must not be inside the data directory')
    if target in resolved_data.parents:
        raise BackupError('the backup directory must not contain the data directory')
    try:
        info = to.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise BackupError('the backup directory exists and is not a directory')
    with os.scandir(to) as entries:
        if any(True for _ in entries):
            raise BackupError('the backup directory is not empty')


def _snapshot_sqlite(data, url, to, writer):
    source = _path(url)
    try:
        info = source.lstat()
    except FileNotFoundError:
        raise BackupError('no server database to back up') from None
    if not stat.S_ISREG(info.st_mode):
        raise BackupError('the server database is a symlink or not a regular file')
    destination = to / 'server' / 'agenticml.db'
    os.close(writer.create(destination))
    connection = sqlite3.connect(source, timeout=30)
    snapshot = sqlite3.connect(destination)
    try:
        # One consistent snapshot, even while the server keeps writing (WAL).
        connection.backup(snapshot)
    finally:
        snapshot.close()
        connection.close()
    os.chmod(destination, 0o600)
    with open(destination, 'rb') as handle:
        os.fsync(handle.fileno())
    return SqliteDatabase(destination)


class _Run:
    def __init__(self, source, writer, to, warnings):
        self.source, self.writer, self.to, self.warnings = source, writer, to, warnings

    def warn(self, message):
        self.warnings.append(message)

    def copy_file(self, run_id, path, info):
        source = self.source / 'runs' / run_id / path
        target = self.to / 'runs' / run_id / path
        committed = info['size']
        atomic = is_atomic_file(path)
        for attempt in (1, 2):
            try:
                handle = _open_source(self.source, source)
            except FileNotFoundError:
                self.warn(f'run {run_id}: {path} is missing; skipped')
                return
            except (OSError, ValueError) as exc:
                self.warn(f'run {run_id}: {path} skipped ({exc})')
                return
            with handle:
                if atomic:
                    body = b''.join(_read_limited(handle, None))
                    digest = hashlib.sha256(body).hexdigest()
                    if info.get('sha256') and digest != info['sha256'] and attempt == 1:
                        continue
                    if info.get('sha256') and digest != info['sha256']:
                        self.warn(f'run {run_id}: {path} changed since the snapshot; kept the newer file '
                                  '(clients repair it on the next sync)')
                    chunks, length = [body], len(body)
                else:
                    chunks = _read_limited(handle, committed)
                    length = None
                written = self.writer.put(target, chunks)
            length = written if length is None else length
            if length < committed:
                self.warn(f'run {run_id}: {path} is shorter than its committed size '
                          f'({length} < {committed}); copied as found')
            return

    def copy_object(self, sha, size):
        source = layout.object_path(self.source, sha)
        target = layout.object_path(self.to, sha)
        try:
            handle = _open_source(self.source, source)
        except FileNotFoundError:
            self.warn(f'object {sha[:12]} is recorded but missing; skipped')
            return False
        except (OSError, ValueError) as exc:
            self.warn(f'object {sha[:12]} skipped ({exc})')
            return False
        digest = hashlib.sha256()

        def chunks():
            with handle:
                for chunk in _read_limited(handle, None):
                    digest.update(chunk)
                    yield chunk

        written = self.writer.put(target, chunks())
        if digest.hexdigest() != sha or written != size:
            self.warn(f'object {sha[:12]} does not match its recorded hash or size')
        return True


def _copy_lineage(source, to, writer, warnings):
    repos = source / 'lineage'
    try:
        names = sorted(entry.name for entry in os.scandir(repos)
                       if entry.name.endswith('.git') and lineage._KEY_RE.fullmatch(entry.name[:-4]))
    except FileNotFoundError:
        return 0
    copied = 0
    for name in names:
        repo = repos / name
        try:
            info = repo.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('not a directory')
            layout.contained(source, repo)
        except (OSError, ValueError, RuntimeError):
            warnings.append(f'lineage repository {name} is a symlink or unreadable; skipped')
            continue
        destination = to / 'lineage' / name
        writer.directory(destination.parent)
        try:
            # Hardened clone (hooks off, fixed config): git reads refs and
            # objects consistently while the server keeps adding refs.
            result = lineage._git([*_CONFIG, 'clone', '--mirror', '--no-hardlinks', '--template=', '-q', '--',
                                   str(repo), str(destination)], timeout=600, check=False)
            if result.returncode:
                raise OSError('git clone failed')
        except (OSError, subprocess.SubprocessError):
            shutil.rmtree(destination, ignore_errors=True)
            warnings.append(f'lineage repository {name} could not be copied; run the backup again')
            continue
        for directory, directories, files in os.walk(destination):
            os.chmod(directory, 0o700)
            for item in files:
                path = Path(directory) / item
                if not path.is_symlink():
                    os.chmod(path, 0o600)
            writer.directories.add(Path(directory))
        copied += 1
    return copied


def backup(to, *, data=None, database=None, objects=None, files_only=False):
    """Copy the server's committed state to ``to``; returns the report written to BACKUP.json."""
    data = resolve_data_dir(data)
    to = Path(to).expanduser()
    url = resolve_url(data, database)
    postgres = _is_postgres(url)
    # Refuse before opening the database or writing anything.
    if postgres and not files_only:
        raise BackupError(pg_dump_note(to))
    _check_destination(to, data)
    data, _ = _data_root(data, database)
    object_url = objects if objects is not None else os.environ.get('AGENTICML_SERVER_OBJECTS', '')
    s3 = object_url.startswith('s3://')
    if not s3:
        from .objectstore import open_backend
        open_backend(object_url, data)
    to.mkdir(mode=0o700, parents=True, exist_ok=True)
    to = to.resolve()
    writer = _Writer(to)
    warnings, notes = [], []
    counts = dict(workspaces=0, runs=0, files=0, bytes=0, objects=0, lineage=0)
    stage = str(to)
    try:
        if postgres:
            snapshot = for_server_dir(data / 'server', url, create=False)
            if snapshot is None:
                raise BackupError('no server database to back up')
            notes.append('files only: the database was not copied; take the dump before this command with '
                         f'pg_dump --format=custom "$DATABASE_URL" > {shlex.quote(str(to / "server.dump"))}')
            owned = False
        else:
            stage = str(to / 'server' / 'agenticml.db')
            snapshot = _snapshot_sqlite(data, url, to, writer)
            owned = True
        try:
            for workspace in WorkspaceRepo(snapshot).list():
                wid = workspace['id']
                source = store_root(data.resolve(), wid)
                destination = store_root(to, wid)
                inventory = InventoryRepo(snapshot, wid)
                run_files = inventory.run_files()
                run_ids = sorted(inventory.run_ids() | set(run_files))
                recorded = inventory.objects()
                stage = str(destination)
                try:
                    if not stat.S_ISDIR(source.lstat().st_mode):
                        raise NotADirectoryError(str(source))
                except OSError:
                    warnings.append(f'workspace {workspace["slug"]} was removed since the snapshot; skipped')
                    continue
                counts['workspaces'] += 1
                writer.directory(destination)
                copier = _Run(source, writer, destination, warnings)
                for run_id in run_ids:
                    try:
                        if not stat.S_ISDIR((source / 'runs' / run_id).lstat().st_mode):
                            raise NotADirectoryError(run_id)
                    except OSError:
                        warnings.append(f'run {run_id} was deleted since the snapshot; skipped')
                        continue
                    counts['runs'] += 1
                    stage = str(destination / 'runs' / run_id)
                    writer.directory(destination / 'runs' / run_id)
                    for path, info in sorted(run_files.get(run_id, {}).items()):
                        copier.copy_file(run_id, path, info)
                if s3:
                    if _S3_NOTE not in notes:
                        notes.append(_S3_NOTE)
                else:
                    for sha, size in sorted(recorded.items()):
                        stage = str(destination / 'objects' / sha[:2] / sha)
                        counts['objects'] += copier.copy_object(sha, size)
                stage = str(destination / 'lineage')
                if shutil.which('git') is None:
                    warnings.append('git is not installed; lineage repositories were not copied')
                else:
                    counts['lineage'] += _copy_lineage(source, destination, writer, warnings)
        finally:
            if owned:
                snapshot.close()
        counts['files'], counts['bytes'] = writer.files, writer.bytes
        report = {'version': VERSION, 'created_at': layout.format_timestamp(),
                  'database': 'postgres' if postgres else 'sqlite', 'files_only': bool(postgres),
                  'counts': counts, 'notes': notes, 'warnings': warnings}
        stage = str(to / 'BACKUP.json')
        writer.put(to / 'BACKUP.json', [json.dumps(report, indent=2, sort_keys=True).encode() + b'\n'])
        writer.sync()
    except OSError as exc:
        reason = 'disk full' if exc.errno in (errno.ENOSPC, errno.EDQUOT) else (exc.strerror or 'I/O error')
        raise BackupError(f'backup stopped at {stage}: {reason}; nothing already written was removed') from exc
    return report


def format_report(report, to):
    counts = report['counts']
    lines = [f'backup: {counts["workspaces"]} workspaces, {counts["runs"]} runs, '
             f'{counts["files"]} files, {counts["objects"]} objects, '
             f'{counts["lineage"]} lineage repositories -> {to}; {len(report["warnings"])} warnings']
    lines.extend(f'note: {note}' for note in report['notes'])
    lines.extend(f'warning: {warning}' for warning in report['warnings'])
    return '\n'.join(lines)

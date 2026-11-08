"""Server run index: list rows, reconcile, and reindex."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat

from agenticml.store import layout, notes as note_store
from agenticml.store.reader import last_step_from_size, read_remote
from agenticml.ui.server import run_list_row

logger = logging.getLogger("agenticml.server.runs")

_UNSET = object()

ATOMIC = re.compile(
    r"(?:meta\.json|artifacts\.json|scalars/index\.json|media/index\.json|"
    r"scalars/(?:0|[1-9][0-9]{0,8})\.ovw)")
APPEND = re.compile(
    r"(?:events\.jsonl|scalars/_time\.bin|scalars/(?:0|[1-9][0-9]{0,8})\.bin)")
_TIME_PATH = "scalars/_time.bin"


def is_run_file(name):
    return bool(ATOMIC.fullmatch(name) or APPEND.fullmatch(name))


def is_atomic_file(name):
    return bool(ATOMIC.fullmatch(name))


def _regular(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def store_paths_safe(store):
    """lstat store and store/runs without following. Missing is OK; symlink or non-dir is not."""
    store = Path(store)
    for path in (store, store / "runs"):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return False
    return True


def _sha256_file(path):
    digest = hashlib.sha256()
    with layout.open_nofollow(path) as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _meta_fields(meta):
    row = run_list_row(meta)
    project = row.get("project") or None
    if project == "":
        project = None
    created_at = meta.get("created_at")
    if not isinstance(created_at, str):
        created_at = None
    launch = note_store._launch_text(meta.get("notes"))
    return row, project, created_at, launch


def list_row_from_bytes(body, run_id):
    try:
        meta = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
    except (ValueError, RecursionError, UnicodeDecodeError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta = {**meta, "run_id": run_id}
    return _meta_fields(meta)


def _read_meta(directory):
    path = directory / layout.META_NAME
    try:
        raw = layout.read_file_nofollow(path)
        meta = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError, UnicodeDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def _on_disk_files(root, directory):
    found = []
    names = [
        "meta.json", "artifacts.json", "events.jsonl",
        "scalars/index.json", "media/index.json", "scalars/_time.bin",
    ]
    scalars = directory / "scalars"
    try:
        if scalars.is_dir() and not scalars.is_symlink():
            names.extend("scalars/" + entry.name for entry in scalars.iterdir())
    except OSError:
        pass
    seen = set()
    for name in names:
        if name in seen or not is_run_file(name):
            continue
        seen.add(name)
        path = directory / name
        try:
            layout.contained(root, path)
        except (ValueError, OSError):
            continue
        if not _regular(path):
            continue
        try:
            size = path.lstat().st_size
        except OSError:
            continue
        sha = None
        if is_atomic_file(name):
            try:
                sha = _sha256_file(path)
            except (OSError, ValueError):
                sha = None
        found.append((name, size, sha))
    return found


def _last_step(directory, files):
    size = next((item[1] for item in files if item[0] == _TIME_PATH), None)
    if not size:
        return None
    path = directory / _TIME_PATH
    try:
        return last_step_from_size(path, size)
    except (OSError, ValueError):
        return None


def adopt_directory(root, directory, run_index, *, remote=None, note_body=None, tx=None,
                    overwrite=False, files=None, fields=None, last_step=_UNSET):
    """Create a runs row from an on-disk directory. Returns the run id or None.

    Reconcile inserts only when the row is absent. Import passes ``overwrite=True``.
    When ``fields`` / ``last_step`` / ``files`` are provided, this does not read the
    run directory (so import can insert inside a write transaction with no file I/O).
    """
    run_id = directory.name
    if not layout.valid_run_id(run_id):
        return None
    if fields is None:
        meta = _read_meta(directory)
        if meta is None:
            return None
        if meta.get("run_id") != run_id:
            meta = {**meta, "run_id": run_id}
        fields = _meta_fields(meta)
    list_row, project, created_at, launch = fields
    if files is None:
        files = _on_disk_files(root, directory)
    if last_step is _UNSET:
        last_step = _last_step(directory, files)
    owner_user = owner_key = status = fingerprint = last_sync_at = last_sync_key = None
    commit_sizes = None
    if remote:
        owner_user = remote.get("owner") if isinstance(remote.get("owner"), str) else None
        owner_key = remote.get("owner_key") if isinstance(remote.get("owner_key"), str) else None
        status = remote.get("status")
        if status not in ("running", "finished", "crashed"):
            status = None
        fingerprint = remote.get("fingerprint") if isinstance(remote.get("fingerprint"), str) else None
        last_sync_at = remote.get("last_sync_at") if isinstance(remote.get("last_sync_at"), str) else None
        last_sync_key = remote.get("last_sync_key") if isinstance(remote.get("last_sync_key"), str) else None
        sizes = remote.get("sizes")
        if isinstance(sizes, dict):
            commit_sizes = json.dumps(sizes)
    row = {
        "run_id": run_id,
        "project": project,
        "created_at": created_at,
        "owner_user": owner_user,
        "owner_key": owner_key,
        "status": status,
        "fingerprint": fingerprint,
        "last_sync_at": last_sync_at,
        "last_sync_key": last_sync_key,
        "list_row": json.dumps(list_row),
        "last_step": last_step,
        "launch_note": launch,
        "commit_sizes": commit_sizes,
        "updated_at": layout.format_timestamp(),
    }
    if overwrite:
        run_index.upsert_adopted(row, files, tx)
    else:
        run_index.insert_adopted(row, files, tx)
    if note_body is not None:
        from .db.repos import NoteRepo
        NoteRepo(run_index.db).upsert(run_id, note_body, layout.format_timestamp(), tx)
    return run_id


def _valid_run_dirs(root):
    if not store_paths_safe(root):
        return None
    runs_root = root / "runs"
    found = {}
    try:
        with os.scandir(runs_root) as entries:
            for entry in entries:
                if not layout.valid_run_id(entry.name):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                found[entry.name] = type(runs_root)(entry.path)
    except OSError:
        return None
    return found


def reconcile(root, run_index):
    """Adopt on-disk runs missing from the index; drop rows whose directory is gone."""
    store = Path(root)
    known = run_index.ids()
    if not store_paths_safe(store):
        logger.warning("store or runs is a symlink or not a directory; skipping reconcile")
        return {"adopted": 0, "removed": 0, "rebuilt": 0}
    root = layout.resolve_root(root)
    on_disk = _valid_run_dirs(root)
    if on_disk is None:
        if known:
            logger.warning("runs directory missing or unreadable; skipping reconcile deletion")
        return {"adopted": 0, "removed": 0, "rebuilt": 0}
    adopted = 0
    removed = 0
    for run_id, directory in on_disk.items():
        if run_id in known:
            continue
        if adopt_directory(root, directory, run_index) is not None:
            adopted += 1
    for run_id in known - set(on_disk):
        path = root / "runs" / run_id
        try:
            path.lstat()
        except FileNotFoundError:
            run_index.delete(run_id)
            removed += 1
        except OSError:
            logger.warning("could not stat runs/%s; skipping reconcile deletion", run_id)
    return {"adopted": adopted, "removed": removed, "rebuilt": 0}


def reindex(root, run_index):
    """Reconcile, then rebuild list_row, last_step, and launch_note from files."""
    counts = reconcile(root, run_index)
    if not store_paths_safe(root):
        return counts
    root = layout.resolve_root(root)
    rebuilt = 0
    for row in run_index.all_rows():
        directory = layout.run_dir(root, row["run_id"])
        meta = _read_meta(directory)
        if meta is None:
            continue
        if meta.get("run_id") != row["run_id"]:
            meta = {**meta, "run_id": row["run_id"]}
        list_row, project, created_at, launch = _meta_fields(meta)
        files = _on_disk_files(root, directory)
        last_step = _last_step(directory, files)
        run_index.rebuild_index(row["run_id"], list_row, project, created_at, launch, last_step)
        rebuilt += 1
    counts["rebuilt"] = rebuilt
    return counts


def remote_from_file(directory):
    """Normalised remote.json plus fingerprint for import, or None if missing/invalid/symlinked."""
    normalized = read_remote(directory)
    if normalized is None:
        return None
    try:
        raw = layout.read_file_nofollow(directory / layout.REMOTE_NAME)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError, UnicodeDecodeError):
        return normalized
    if not isinstance(data, dict):
        return normalized
    extra = {}
    fingerprint = data.get("fingerprint")
    if isinstance(fingerprint, str):
        extra["fingerprint"] = fingerprint
    sizes = data.get("sizes")
    if isinstance(sizes, dict):
        extra["sizes"] = sizes
    return {**normalized, **extra} if extra else normalized


def read_note_file(directory):
    """Stored notes.md body for import, or None if missing/special/unreadable."""
    from agenticml.store.notes import NOTE_NAME, _pinned_run, _read_stored

    try:
        with _pinned_run(directory) as dir_fd:
            kind, note = _read_stored(directory, dir_fd)
    except (OSError, ValueError):
        return None
    if kind != "ok":
        return None
    return note["text"]

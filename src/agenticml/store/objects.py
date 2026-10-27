"""Content-addressed blobs shared by every run under one root.

Code files, media, and copied artifacts are stored once per digest. Runs
hold the digest, not a second copy of the bytes. Writes go to a temp file
in ``objects/`` (same filesystem as the final shard) and are published with
``os.replace`` only after the bytes are fsynced, so a named object is never
a torn copy. A crash leaves a ``.tmp.*`` dotfile, which readers ignore.

If the digest is already present the temp file is discarded and the existing
inode has its mtime refreshed so GC protects newly reused bytes.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .layout import contained, object_path, unique_tmp

# One pass over a multi-GB checkpoint. Big enough that syscall overhead is
# noise, small enough that a copy does not pin the file in memory.
_CHUNK = 1024 * 1024


def _is_regular_file(path: Path) -> bool:
    try:
        st = os.lstat(path)
        return stat.S_ISREG(st.st_mode)
    except OSError:
        return False


def _refresh_existing_object(path: Path) -> bool:
    if not _is_regular_file(path):
        return False
    try:
        os.utime(path, None, follow_symlinks=False)
    except FileNotFoundError:
        # GC moved the old inode to trash after our existence check.
        return False
    return True


def put_bytes(root, data: bytes) -> str:
    """Store ``data`` and return its SHA-256 hex digest.

    Identical bytes are not written twice. ``data`` must already be bytes;
    hashing a str would depend on an encoding the caller did not choose.
    """
    if isinstance(data, str) or not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes")
    blob = bytes(data)
    sha = hashlib.sha256(blob).hexdigest()
    dest = object_path(root, sha)
    if _refresh_existing_object(dest):
        return sha
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_tmp(dest.parent, f".{sha}")
    try:
        with tmp.open("wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        if not _refresh_existing_object(dest):
            os.replace(tmp, dest)
        return sha
    finally:
        tmp.unlink(missing_ok=True)


def put_file(root, path) -> tuple[str, int]:
    """Stream ``path`` into the object store. Returns ``(sha256, size)``.

    The digest is computed while the bytes are copied so a checkpoint is
    read once. The temp file lives in ``objects/`` until the digest is known,
    then moves into ``objects/<sha[:2]>/``.
    """
    src = Path(path)
    root_path = Path(root).expanduser().resolve()
    objects = contained(root_path, root_path / "objects")
    objects.mkdir(parents=True, exist_ok=True)
    tmp = unique_tmp(objects, ".tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with src.open("rb") as src_handle, tmp.open("wb") as dst_handle:
            while True:
                chunk = src_handle.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                dst_handle.write(chunk)
                size += len(chunk)
            dst_handle.flush()
            os.fsync(dst_handle.fileno())
        sha = digest.hexdigest()
        dest = object_path(root_path, sha)
        if not _refresh_existing_object(dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dest)
        return sha, size
    finally:
        tmp.unlink(missing_ok=True)


def has(root, sha: str) -> bool:
    """True when the object for ``sha`` is already stored."""
    return _is_regular_file(object_path(root, sha))


def path_for(root, sha: str) -> Path:
    """Path the object would occupy. Does not create it."""
    return object_path(root, sha)

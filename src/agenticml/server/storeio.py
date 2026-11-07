"""Lock and atomic JSON writes shared by keys.json and users.json."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat

from agenticml.store import layout


@contextmanager
def exclusive_lock(server_dir):
    """Hold ``keys.lock`` exclusively. Serializes keys.json and users.json mutations."""
    try:
        import fcntl
    except ImportError as exc:
        raise ValueError("the server needs POSIX fcntl") from exc
    directory = Path(server_dir)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(directory / "keys.lock", flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("keys.lock is not a regular file")
        handle = os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def write_json(directory, path, obj, tmp_prefix):
    """Atomically replace ``path`` with JSON, no-follow, mode 0600, fsync."""
    directory = Path(directory)
    path = Path(path)
    tmp = layout.unique_tmp(directory, tmp_prefix)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(obj, indent=2).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        layout.fsync_dir(directory)
    finally:
        tmp.unlink(missing_ok=True)

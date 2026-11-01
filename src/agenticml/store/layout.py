"""On-disk locations and names for the v1 run store.

Training writes only under a local root. The UI and ``agenticml sync``
read those bytes back, so every path and id is checked before it is joined
onto the root: a run id or content hash that came from a request must not be
able to escape ``runs/`` or ``objects/``.

``resolve_root`` picks the store root: explicit directory, else
``AGENTICML_DIR``, else ``~/.agenticml``. This module does not import the client.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import stat
import struct
import threading
import weakref
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("agenticml.store.layout")

_run_lock_handles = weakref.WeakSet()
_lock_warning_emitted = False
_probe_warned: set[str] = set()
_LOCK_UNSUPPORTED = {errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP}


def _close_inherited_run_locks() -> None:
    # flock belongs to the shared open file description. LOCK_UN here would
    # unlock the parent's writer; keeping the fd would outlive a dead parent.
    for handle in list(_run_lock_handles):
        try:
            handle.close()
        except OSError:
            pass
    _run_lock_handles.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_close_inherited_run_locks)

# Default store root. Kept here so the store stays stdlib-only.
DEFAULT_DIR = Path("~/.agenticml")
FORMAT_VERSION = 1

# Scalar point: little-endian uint32 step, float32 value. 8 bytes, no padding.
RECORD_STRUCT = struct.Struct("<If")
RECORD_SIZE = RECORD_STRUCT.size

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

META_NAME = "meta.json"
EVENTS_NAME = "events.jsonl"
INDEX_NAME = "index.json"
TIME_BIN_NAME = "_time.bin"
SCALARS_DIRNAME = "scalars"
MEDIA_DIRNAME = "media"
OBJECTS_DIRNAME = "objects"
RUNS_DIRNAME = "runs"


def resolve_root(dir=None) -> Path:
    """Local asset root.

    Precedence is ``dir`` > ``AGENTICML_DIR`` > ``~/.agenticml``. The result is
    expanduser'd and resolved.
    """
    raw = dir if dir is not None else os.environ.get("AGENTICML_DIR", str(DEFAULT_DIR))
    return Path(raw).expanduser().resolve()


def new_run_id() -> str:
    """UTC ``YYYYmmddTHHMMSS`` plus 8 hex chars.

    Second resolution would collide if two runs start together; the suffix
    is 32 bits from the OS CSPRNG so a retry is not required in practice.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{secrets.token_hex(4)}"


def valid_run_id(run_id: str) -> bool:
    """True when ``run_id`` is a single safe path component.

    The first character is alphanumeric so ``.`` / ``..`` / ``.hidden`` cannot
    pass. Slashes, backslashes, and spaces cannot pass either.
    """
    return isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id) is not None


def valid_sha(sha: str) -> bool:
    """True for a lowercase hex SHA-256 digest."""
    return isinstance(sha, str) and _SHA_RE.fullmatch(sha) is not None


def contained(root, path) -> Path:
    """Resolved ``path`` if it stays inside ``root``.

    ``resolve(strict=False)`` follows symlinks that already exist and keeps a
    missing final component. A symlink such as ``runs/<id>``, ``runs/<id>/scalars``,
    or ``objects/<hh>`` that points outside the store root raises ``ValueError``.
    """
    root_path = Path(root).expanduser().resolve()
    resolved = Path(path).resolve(strict=False)
    if not resolved.is_relative_to(root_path):
        raise ValueError(f"path escapes store root: {path}")
    return resolved


def run_dir(root, run_id: str) -> Path:
    """``<root>/runs/<run_id>``. Raises ``ValueError`` on a bad id or an escaping symlink."""
    if not valid_run_id(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")
    root_path = Path(root).expanduser().resolve()
    path = root_path / RUNS_DIRNAME / run_id
    contained(root_path, path)
    return path


def object_path(root, sha: str) -> Path:
    """``<root>/objects/<sha[:2]>/<sha>``. Raises ``ValueError`` on a bad digest.

    ``objects/<hh>`` must stay inside the root. A symlink at the object file
    itself is returned unchanged so a caller can reject it (the UI answers
    404) without treating that as a bad digest.
    """
    if not valid_sha(sha):
        raise ValueError(f"invalid sha256: {sha!r}")
    root_path = Path(root).expanduser().resolve()
    path = root_path / OBJECTS_DIRNAME / sha[:2] / sha
    contained(root_path, path.parent)
    if not path.is_symlink():
        contained(root_path, path)
    return path


def format_timestamp(when: datetime | None = None) -> str:
    """UTC timestamp with microsecond precision, ``Z`` suffix, fixed width.

    Fixed width keeps ``created_at`` lexicographically sortable.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_timestamp(value: str) -> datetime:
    """Parse a timestamp written by :func:`format_timestamp`.

    ``Z`` is rewritten because :meth:`datetime.fromisoformat` on Python 3.9
    does not accept it. Naive strings are treated as UTC.
    """
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def unique_tmp(directory: Path, prefix: str) -> Path:
    """``{prefix}.{pid}.{thread id}.{8 hex}.tmp`` in ``directory``.

    ``prefix`` includes the leading dot. The pid, thread id, and random
    suffix together are unique across processes and threads in one directory.
    """
    name = f"{prefix}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    return Path(directory) / name


def atomic_write(path: Path, data: bytes, *, fsync: bool = False) -> None:
    """Replace ``path`` with ``data`` via a temp file in the same directory.

    ``os.replace`` is atomic on one filesystem, so a reader opens either the
    old inode or the new one, never a half-written JSON file. The temp name
    is a dotfile (``.<name>.<pid>.<thread>.<hex>.tmp``); readers never open
    it. A crash before replace leaves the previous file in place.

    ``fsync=True`` pushes the new bytes to stable storage before the name is
    published. The scalar hot path does not need that; ``finish`` does.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_tmp(path.parent, f".{path.name}")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def fsync_dir(path: Path) -> None:
    """Fsync a directory so a just-published name survives power loss."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class RunLockedError(ValueError):
    """Raised when a run's lifetime lock is held by another process."""


def try_lock_run(run_dir: Path):
    """Take the run's non-blocking exclusive lifetime lock, or return None."""
    handle = open_nofollow(Path(run_dir) / "run.lock", "ab", buffering=0)
    handle.owner_pid = os.getpid()
    handle.locking_supported = os.name != "nt"
    _run_lock_handles.add(handle)
    if os.name == "nt":
        # Windows: no-op locks; fcntl.flock is unavailable.
        return handle
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _LOCK_UNSUPPORTED:
            handle.locking_supported = False
            _warn_lock_unsupported()
        else:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return None
            raise
    return handle


def _warn_lock_unsupported() -> None:
    global _lock_warning_emitted
    if not _lock_warning_emitted:
        _lock_warning_emitted = True
        logger.warning("run locking is unsupported on this filesystem; continuing unlocked")


def is_run_locked(run_dir: Path) -> bool | None:
    """Probe without creating files; None if unsupported or unreadable."""
    if os.name == "nt":
        return None
    import fcntl

    try:
        with open_nofollow(Path(run_dir) / "run.lock") as handle:
            try:
                # A shared probe conflicts with the writer's exclusive lock
                # and works with a read-only descriptor on NFS.
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return True
                if exc.errno in _LOCK_UNSUPPORTED:
                    _warn_lock_unsupported()
                    return None
                raise
            return False  # Closing releases this probe's lock.
    except FileNotFoundError:
        return False
    except OSError as exc:
        path = str(run_dir)
        if path not in _probe_warned:
            _probe_warned.add(path)
            logger.warning("could not probe run.lock in %s: %s", path, exc)
        return None


def unlock_run(handle) -> None:
    """Release a handle returned by try_lock_run (also safe for None)."""
    if handle is not None:
        try:
            if not handle.closed and handle.locking_supported and os.getpid() == handle.owner_pid:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            _run_lock_handles.discard(handle)


def is_symlink_error(exc: OSError) -> bool:
    """True if OSError was caused by opening a symbolic link."""
    return exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", None))


def open_nofollow(path: Path | str, mode: str = "rb", buffering: int = -1):
    """Open a file without following symlinks.

    Uses os.open with O_NOFOLLOW where available, else lstat + S_ISREG check.
    Raises OSError (errno.ELOOP) or FileNotFoundError if the path is a symlink.
    """
    if "a" in mode:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    elif "+" in mode:
        flags = os.O_RDWR
    elif "w" in mode:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    else:
        flags = os.O_RDONLY

    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    else:
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                raise OSError(errno.ELOOP, f"refusing symlink: {path}")
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, f"not a regular file: {path}")
        except FileNotFoundError:
            if not (flags & os.O_CREAT):
                raise
    fd = os.open(path, flags, 0o666)
    try:
        st_fd = os.fstat(fd)
        if not stat.S_ISREG(st_fd.st_mode):
            raise OSError(errno.EINVAL, f"not a regular file: {path}")
        return open(fd, mode, buffering=buffering)
    except Exception:
        os.close(fd)
        raise


def read_file_nofollow(path: Path | str) -> bytes:
    """Read full file contents without following symlinks."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    else:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            raise OSError(errno.ELOOP, f"refusing symlink: {path}")
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, f"not a regular file: {path}")
    fd = os.open(path, flags)
    try:
        st_fd = os.fstat(fd)
        if not stat.S_ISREG(st_fd.st_mode):
            raise OSError(errno.EINVAL, f"not a regular file: {path}")
        size = st_fd.st_size
        chunks = []
        remaining = size
        while True:
            chunk = os.read(fd, max(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def open_object(root, sha: str):
    """Open an object file safely for reading.

    Returns a binary file object opened in 'rb' mode.
    Refuses symlinks and files with st_nlink > 1 (a hard link indicates
    a planted file), raising FileNotFoundError otherwise.
    """
    if not valid_sha(sha):
        raise FileNotFoundError(f"invalid sha256: {sha!r}")
    root_path = Path(root).expanduser().resolve()
    path = root_path / OBJECTS_DIRNAME / sha[:2] / sha
    try:
        contained(root_path, path.parent)
    except ValueError as exc:
        raise FileNotFoundError(str(exc)) from exc

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    else:
        try:
            st_link = os.lstat(path)
            if stat.S_ISLNK(st_link.st_mode):
                raise FileNotFoundError(f"refusing symlinked object: {path}")
        except OSError as exc:
            raise FileNotFoundError(f"object not found: {sha}") from exc

    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if is_symlink_error(exc):
            raise FileNotFoundError(f"refusing symlinked object: {path}") from exc
        raise FileNotFoundError(f"object not found or inaccessible: {sha}") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FileNotFoundError(f"not a regular file: {path}")
        if st.st_nlink > 1:
            raise FileNotFoundError(f"refusing hard-linked object (st_nlink={st.st_nlink}): {path}")
        return open(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def load_events(path: Path) -> list[dict]:
    """Read ``events.jsonl``.

    A crash can tear the last line. If that line is not valid JSON it is
    ignored with no warning — the writer will discard it before appending.
    An earlier line that does not parse is skipped and logged: append-only
    writers do not rewrite the middle of the file, so that line is corrupt,
    not torn. Symlinked files are refused with a warning.
    """
    try:
        raw = read_file_nofollow(path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        if is_symlink_error(exc) or path.is_symlink():
            logger.warning("refusing symlinked events file: %s", path)
            return []
        logger.warning("could not read events file %s: %s", path, exc)
        return []
    if not raw:
        return []
    # LF only. str.splitlines() also splits on U+2028 and U+0085, which are
    # legal inside a JSON string written with ensure_ascii=False.
    parts = raw.split(b"\n")
    torn = not raw.endswith(b"\n")
    if not torn:
        parts = parts[:-1]
    events: list[dict] = []
    last = len(parts) - 1
    for index, raw_line in enumerate(parts):
        line = raw_line.decode("utf-8", errors="replace")
        if not line.strip():
            continue
        is_torn_tail = torn and index == last
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            if not is_torn_tail:
                logger.warning("skipping unparseable line %d in %s", index + 1, path)
            continue
        if isinstance(obj, dict):
            events.append(obj)
        elif not is_torn_tail:
            logger.warning("skipping non-object line %d in %s", index + 1, path)
    return events

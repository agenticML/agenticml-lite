"""One Markdown note per run, at ``runs/<run_id>/notes.md``.

Notes stay on this machine: sync does not read the run directory, and
removing a run removes the note with it. Writes compare-and-swap on the
SHA-256 of the stored bytes.

A FIFO or other special ``notes.md`` is not a note. Opening it goes through
``O_NONBLOCK`` (where the flag exists) and then ``fstat`` on that descriptor,
so a planted FIFO cannot block ``open`` — including while the process lock
is held. Reads keep at most ``MAX_NOTE_BYTES + 1`` bytes. On POSIX, after
:func:`layout.run_dir` validates the path, the directory is pinned with
``O_DIRECTORY|O_NOFOLLOW`` and the note is created, replaced, and unlinked
through that descriptor. A symlink swapped in for ``runs/<id>`` is not
followed. Platforms without directory-fd operations keep the path-based
fallback.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import errno
import hashlib
import logging
import os
import stat
import threading

from . import layout, reader

logger = logging.getLogger("agenticml.store.notes")

NOTE_NAME = "notes.md"
MAX_NOTE_BYTES = 256 * 1024
# One extra byte distinguishes "exactly at the cap" from "larger than the cap"
# without reading the rest of a sparse or multi-gigabyte file.
_PROBE_BYTES = MAX_NOTE_BYTES + 1
# Mixed into an oversized revision so it cannot match SHA-256(prefix) for a
# note whose stored bytes are exactly that prefix. NUL cannot occur in a
# note written through write_note.
_TOO_LARGE_MARK = b"\0too_large"

# One lock for every run. The check and the replace must not interleave.
# Serializes within one process only (two dashboard processes on one store can lose an update).
_LOCK = threading.Lock()

# Linux reports ENOTDIR (not ELOOP) for O_DIRECTORY|O_NOFOLLOW on a symlink.
# A regular file at the same path fails with ENOTDIR too, so the errno alone
# is not a refusal: the caller lstats and refuses only a symlink.
_PIN_REFUSED = {errno.ELOOP, errno.ENOTDIR}
if hasattr(errno, "EMLINK"):
    _PIN_REFUSED.add(errno.EMLINK)


class NoteConflict(Exception):
    """``base_revision`` does not match the note stored for this run."""

    def __init__(self, note: dict):
        super().__init__("note conflict")
        self.note = note


class NoteTooLarge(ValueError):
    """Encoded note text is larger than ``MAX_NOTE_BYTES``."""


class _SpecialNote(Exception):
    """``notes.md`` is a symlink or some other non-regular file."""


def _empty() -> dict:
    return {"text": "", "updated_at": None, "revision": None}


def _text(value):
    return value if isinstance(value, str) else ""


def _pin_run_dirs() -> bool:
    """True when note IO can stay relative to one directory descriptor.

    ``os.replace`` takes ``src_dir_fd`` on POSIX, but CPython does not put
    ``os.replace`` in ``os.supports_dir_fd`` (``os.rename``, the renameat
    wrapper, is the registered one). Requiring ``os.replace in
    os.supports_dir_fd`` would skip the pin on Linux. Windows has neither.
    """
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        return False
    replace_ok = os.replace in os.supports_dir_fd or os.rename in os.supports_dir_fd
    return bool(
        replace_ok
        and os.open in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
    )


def _is_symlink(path: Path) -> bool:
    """True when the final component is a symlink. ``lstat`` does not follow it."""
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


def _open_run_dir(directory: Path):
    """Pin ``directory``, or return None when directory fds are unavailable.

    ``O_NOFOLLOW`` applies to the final component. A symlink swapped in for
    the run directory raises ``ValueError`` instead of being followed. A
    non-symlink that fails ``O_DIRECTORY`` (a regular file) is a missing run.
    """
    if not _pin_run_dirs():
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(directory, flags)
    except OSError as exc:
        if exc.errno in _PIN_REFUSED or layout.is_symlink_error(exc):
            # ENOTDIR covers both a symlink (Linux, O_DIRECTORY|O_NOFOLLOW)
            # and a regular file. Only the symlink is a refusal.
            if not _is_symlink(directory):
                raise FileNotFoundError(directory.name) from exc
            raise ValueError("refusing to follow a symlinked run directory") from exc
        raise


@contextmanager
def _pinned_run(directory: Path):
    fd = _open_run_dir(directory)
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _stat_entry(directory: Path, dir_fd, name: str):
    if dir_fd is not None:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    return os.lstat(directory / name)


def _require_run(directory: Path, dir_fd=None) -> None:
    """``meta.json`` must be a regular file, otherwise the run is absent.

    A regular file at ``runs/<id>`` makes the ``meta.json`` stat fail with
    ``ENOTDIR`` on the path fallback (no directory fd). That is a missing
    run, same as the pinned-open path.
    """
    try:
        info = _stat_entry(directory, dir_fd, layout.META_NAME)
    except (FileNotFoundError, NotADirectoryError):
        raise FileNotFoundError(directory.name) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise FileNotFoundError(directory.name)


def _ensure_attached(directory: Path, dir_fd) -> None:
    """On the path fallback, the run directory must still be a real directory.

    GC can remove it after the existence check. Creating ``notes.md`` by
    path would resurrect ``runs/<id>/``. A pinned directory needs no
    ``st_nlink`` check: creating in a removed directory fails with
    ``ENOENT``, and some FUSE filesystems report ``st_nlink == 0`` for a
    directory that is still there.
    """
    if dir_fd is not None:
        return
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        raise FileNotFoundError(directory.name) from None
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise FileNotFoundError(directory.name)


def _entry_is_special(directory: Path, dir_fd, name: str) -> bool:
    try:
        info = _stat_entry(directory, dir_fd, name)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)


def _open_note(directory: Path, dir_fd) -> int:
    """Open ``notes.md`` without blocking on a FIFO and without following a symlink.

    ``lstat`` then ``open`` still races: the inode can become a FIFO in
    between, and ``open`` waits forever. ``O_NONBLOCK`` makes that ``open``
    return so ``fstat`` can reject the descriptor we actually hold.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    elif _entry_is_special(directory, dir_fd, NOTE_NAME):
        raise _SpecialNote()
    if dir_fd is not None:
        return os.open(NOTE_NAME, flags, dir_fd=dir_fd)
    return os.open(directory / NOTE_NAME, flags)


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _revision(data: bytes, too_large: bool) -> str:
    """SHA-256 hex of the note bytes.

    An oversized file hashes the retained prefix plus ``b"\\0too_large"``.
    A note that fits hashes its stored bytes alone, so an exactly
    ``MAX_NOTE_BYTES`` file cannot share a revision with the oversized
    file whose prefix it is.
    """
    payload = data + _TOO_LARGE_MARK if too_large else data
    return hashlib.sha256(payload).hexdigest()


def _note_from_fd(fd: int) -> dict:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise _SpecialNote()
    if hasattr(os, "set_blocking"):
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass
    raw = _read_bounded(fd, _PROBE_BYTES)
    # st_size catches a sparse file whose logical size is past the cap.
    # The probe byte catches a file that grew after that fstat.
    too_large = info.st_size > MAX_NOTE_BYTES or len(raw) > MAX_NOTE_BYTES
    data = raw[:MAX_NOTE_BYTES]
    when = datetime.fromtimestamp(info.st_mtime, timezone.utc)
    note = {
        "text": data.decode("utf-8", errors="replace"),
        "updated_at": layout.format_timestamp(when),
        "revision": _revision(data, too_large),
    }
    if too_large:
        note["too_large"] = True
    return note


def _read_stored(directory: Path, dir_fd):
    """``("missing"|"special"|"ok", note)`` for the pinned run directory."""
    try:
        fd = _open_note(directory, dir_fd)
    except _SpecialNote:
        return "special", _empty()
    except FileNotFoundError:
        return "missing", _empty()
    except OSError as exc:
        if layout.is_symlink_error(exc) or _entry_is_special(directory, dir_fd, NOTE_NAME):
            return "special", _empty()
        raise
    try:
        try:
            return "ok", _note_from_fd(fd)
        except _SpecialNote:
            return "special", _empty()
    finally:
        os.close(fd)


def _load(directory: Path, dir_fd=None) -> dict:
    _kind, note = _read_stored(directory, dir_fd)
    if _kind != "ok":
        return _empty()
    return note


def _unlink_note(directory: Path, dir_fd) -> None:
    try:
        if dir_fd is not None:
            os.unlink(NOTE_NAME, dir_fd=dir_fd)
        else:
            (directory / NOTE_NAME).unlink()
    except FileNotFoundError:
        pass


def _discard_temp(name: str, directory: Path, dir_fd) -> None:
    try:
        if dir_fd is not None:
            os.unlink(name, dir_fd=dir_fd)
        else:
            (directory / name).unlink()
    except OSError:
        pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write on notes.md")
        view = view[written:]


def _publish(directory: Path, dir_fd, data: bytes) -> None:
    """Replace ``notes.md`` without creating missing parent directories.

    ``layout.atomic_write`` would mkdir the run directory. If GC removed the
    run after the existence check, that would leave an orphan ``runs/<id>/``
    containing only ``notes.md``. A missing directory raises ``FileNotFoundError``.
    """
    tmp_name = layout.unique_tmp(directory, f".{NOTE_NAME}").name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        if dir_fd is not None:
            fd = os.open(tmp_name, flags, 0o666, dir_fd=dir_fd)
        else:
            fd = os.open(directory / tmp_name, flags, 0o666)
        created = True
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if dir_fd is not None:
            os.replace(tmp_name, NOTE_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            os.replace(directory / tmp_name, directory / NOTE_NAME)
    except Exception:
        if created:
            _discard_temp(tmp_name, directory, dir_fd)
        raise


def _note_stat(directory: Path, dir_fd=None):
    """``(mtime, size)`` when ``notes.md`` is a regular file, else None.

    A missing file, symlink, or other non-regular file is not a note.
    ``lstat`` does not open the inode, so a FIFO does not block the list.
    Other ``OSError``s propagate so the caller can skip that run.
    """
    try:
        info = _stat_entry(directory, dir_fd, NOTE_NAME)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return info.st_mtime, info.st_size


def read_note(root, run_id: str) -> dict:
    """The run's note.

    A missing ``notes.md`` is ``{"text": "", "updated_at": None, "revision": None}``.
    A symlink, FIFO, directory, or other non-regular ``notes.md`` is treated
    the same way and is not followed. ``meta.json`` must be a regular file;
    otherwise ``FileNotFoundError``. A bad ``run_id`` raises ``ValueError``.

    At most ``MAX_NOTE_BYTES + 1`` bytes are read. When the file is larger
    than ``MAX_NOTE_BYTES`` (by ``st_size`` or by that probe), the result
    adds ``too_large: True``. ``text`` is the first ``MAX_NOTE_BYTES`` of the
    bytes read, decoded with ``errors="replace"``. ``revision`` is the
    SHA-256 of the stored bytes when the note fits. When it does not,
    ``revision`` is the SHA-256 of the retained prefix plus
    ``b"\\0too_large"``, so it cannot match a note whose bytes are exactly
    that prefix. The probe byte and the unread tail are not hashed. A note
    that fits omits ``too_large``. ``updated_at`` is the file mtime.
    """
    directory = layout.run_dir(root, run_id)
    with _pinned_run(directory) as dir_fd:
        _require_run(directory, dir_fd)
        return _load(directory, dir_fd)


def write_note(root, run_id: str, text, base_revision) -> dict:
    """Replace the note when ``base_revision`` is still current.

    ``None`` means the note must be absent. Line endings are normalized to
    ``\\n``. NUL is rejected. Empty or whitespace-only text deletes the file.
    ``NoteConflict`` carries the note currently on disk. ``NoteTooLarge`` is a
    ``ValueError``. A symlink, FIFO, or other non-regular ``notes.md`` raises
    ``ValueError`` and is left in place. That check happens after the
    revision compare, so a stale ``base_revision`` is still ``NoteConflict``.

    An oversized file uses the revision described on :func:`read_note`.
    ``write_note`` leaves it unchanged unless ``base_revision`` equals that
    revision. A match replaces or deletes the whole file, including the
    unread tail. A mismatch raises ``NoteConflict`` with ``too_large`` set.
    """
    if not isinstance(text, str):
        raise ValueError("note text must be a string")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise ValueError("note text contains NUL")
    data = text.encode("utf-8")
    if len(data) > MAX_NOTE_BYTES:
        raise NoteTooLarge(f"note exceeds {MAX_NOTE_BYTES} bytes")
    with _LOCK:
        directory = layout.run_dir(root, run_id)
        with _pinned_run(directory) as dir_fd:
            _require_run(directory, dir_fd)
            _ensure_attached(directory, dir_fd)
            kind, current = _read_stored(directory, dir_fd)
            if base_revision != current["revision"]:
                raise NoteConflict(current)
            if kind == "special":
                raise ValueError("refusing to replace a non-regular notes.md")
            _ensure_attached(directory, dir_fd)
            if not text.strip():
                _unlink_note(directory, dir_fd)
                return _empty()
            _publish(directory, dir_fd, data)
            return _load(directory, dir_fd)


def list_notes(root, project=None, limit_bytes=4 * 1024 * 1024) -> dict:
    """Non-empty notes, newest ``notes.md`` mtime first.

    ``project`` keeps rows whose project string matches exactly. Each
    candidate is stat'd before its body is read. Whitespace-only text is
    skipped. A file larger than ``MAX_NOTE_BYTES`` is omitted without
    being read (``truncated``). A file that grows past the cap between
    that stat and the read is still bounded to ``MAX_NOTE_BYTES + 1``
    bytes, omitted, and counted as ``truncated``. Once the next file
    would pass ``limit_bytes``, the scan stops and older bodies are not
    read. One unreadable run is skipped. Rows do not include ``too_large``.
    """
    candidates = []
    for meta in reader.list_runs(root):
        if not isinstance(meta, dict):
            continue
        run_id = meta.get("run_id")
        if not isinstance(run_id, str):
            continue
        row_project = _text(meta.get("project"))
        if project is not None and row_project != project:
            continue
        try:
            directory = layout.run_dir(root, run_id)
            with _pinned_run(directory) as dir_fd:
                found = _note_stat(directory, dir_fd)
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable note for run %s: %s", run_id, exc)
            continue
        if found is None:
            continue
        mtime, size = found
        candidates.append((mtime, size, directory, meta, row_project))
    # st_mtime, not the formatted timestamp: the body is read only after this order is known.
    candidates.sort(key=lambda item: item[0], reverse=True)
    kept = []
    used = 0
    truncated = False
    for _mtime, size, directory, meta, row_project in candidates:
        if size == 0:
            continue
        # A file that does not fit ends the scan so older bodies stay unread.
        # Over the per-note cap but still inside the budget: omit it and keep going.
        if used + size > limit_bytes:
            truncated = True
            break
        if size > MAX_NOTE_BYTES:
            truncated = True
            continue
        run_id = meta.get("run_id")
        try:
            with _pinned_run(directory) as dir_fd:
                note = _load(directory, dir_fd)
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable note for run %s: %s", run_id, exc)
            continue
        if note.get("too_large"):
            truncated = True
            continue
        if not note["text"].strip():
            continue
        text_size = len(note["text"].encode("utf-8"))
        if used + text_size > limit_bytes:
            truncated = True
            break
        if "effective_status" in meta:
            status = meta.get("effective_status")
        else:
            status = meta.get("status")
        kept.append({
            "run_id": run_id,
            "name": _text(meta.get("name")),
            "project": row_project,
            "status": status,
            "created_at": meta.get("created_at"),
            "text": note["text"],
            "updated_at": note["updated_at"],
            "revision": note["revision"],
        })
        used += text_size
    return {"notes": kept, "truncated": truncated}

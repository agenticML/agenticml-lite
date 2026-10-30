"""One Markdown note per run, at ``runs/<run_id>/notes.md``.

Notes stay on this machine: sync does not read the run directory, and
removing a run removes the note with it. Writes compare-and-swap on the
SHA-256 of the stored bytes. When ``notes.md`` is missing, a non-empty
``notes`` string in ``meta.json`` is that run's launch note. An empty
``notes.md`` stays empty, so clearing the notepad does not bring the launch
note back. Every note dict includes ``source``: ``file``, ``launch``, or
``none``.

A FIFO or other special ``notes.md`` is not a note, and it does not fall
back to the launch note. Opening it goes through
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
import json
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
# meta.json holds config and env as well as the launch note. Past this cap
# it is not parsed, and the run has no launch note.
_META_CAP = 16 * 1024 * 1024
# Mixed into an oversized revision so it cannot match SHA-256(prefix) for a
# note whose stored bytes are exactly that prefix. NUL cannot occur in a
# note written through write_note.
_TOO_LARGE_MARK = b"\0too_large"

# One lock for every run. The check and the replace must not interleave.
# Serializes within one process only (two dashboard processes on one store can lose an update).
_LOCK = threading.Lock()

# ``_note_stat`` returns this when ``notes.md`` exists but is not a regular
# file. ``None`` means the file is absent, which is the only stat result that
# may become a launch row.
_NOTE_SPECIAL = object()

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
    return {"text": "", "updated_at": None, "revision": None, "source": "none"}


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


def _open_entry(directory: Path, dir_fd, name: str) -> int:
    """Open ``name`` without blocking on a FIFO and without following a symlink.

    ``lstat`` then ``open`` still races: the inode can become a FIFO in
    between, and ``open`` waits forever. ``O_NONBLOCK`` makes that ``open``
    return so ``fstat`` can reject the descriptor we actually hold.
    ``notes.md`` and ``meta.json`` both use this open.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    elif _entry_is_special(directory, dir_fd, name):
        raise _SpecialNote()
    if dir_fd is not None:
        return os.open(name, flags, dir_fd=dir_fd)
    return os.open(directory / name, flags)


def _open_note(directory: Path, dir_fd) -> int:
    """Open ``notes.md`` through :func:`_open_entry`."""
    return _open_entry(directory, dir_fd, NOTE_NAME)


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


def _regular_stat(fd: int):
    """``fstat`` of a regular file, ready for a blocking read.

    ``O_NONBLOCK`` was only there so ``open`` could not wait on a FIFO.
    A regular file is switched back to blocking before its bytes are read.
    """
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise _SpecialNote()
    if hasattr(os, "set_blocking"):
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass
    return info


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _clip_utf8(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` UTF-8 bytes on a character boundary."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    clipped = raw[:limit]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def _launch_text(value):
    """Normalized launch note, or None when ``value`` is not usable text.

    A lone surrogate is not UTF-8. JSON round-trips ``\\udcff``, and
    ``--notes`` read with ``surrogateescape`` can store one. ``None`` keeps
    that run out of the notebook instead of failing ``list_notes`` for every
    run.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        text = _clip_utf8(_normalize_newlines(value), MAX_NOTE_BYTES)
    except UnicodeEncodeError:
        return None
    if not text.strip():
        return None
    return text


def _read_meta_bytes(directory: Path, dir_fd):
    """``meta.json`` bytes, or None when it cannot supply a launch note.

    A missing ``meta.json`` is a missing run (``FileNotFoundError``).
    :func:`_require_run` is what turns that into a 404. A symlink, FIFO, or
    other non-regular file is not followed. Any other error opening the file,
    including permission denied, is not a launch note. A file larger than
    ``_META_CAP`` is not parsed.
    """
    try:
        fd = _open_entry(directory, dir_fd, layout.META_NAME)
    except _SpecialNote:
        return None
    except FileNotFoundError:
        raise FileNotFoundError(directory.name) from None
    except OSError:
        return None
    try:
        try:
            info = _regular_stat(fd)
        except _SpecialNote:
            return None
        # st_size rejects a sparse file before its hole is read.
        if info.st_size > _META_CAP:
            return None
        raw = _read_bounded(fd, _META_CAP + 1)
    finally:
        os.close(fd)
    if len(raw) > _META_CAP:
        return None
    return raw


def _launch_note(directory: Path, dir_fd):
    """Launch note dict, or None when this run has no usable launch note."""
    raw = _read_meta_bytes(directory, dir_fd)
    if raw is None:
        return None
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(meta, dict):
        return None
    text = _launch_text(meta.get("notes"))
    if text is None:
        return None
    return {
        "text": text,
        "updated_at": None,
        "revision": None,
        "source": "launch",
    }


def _created_sort_time(value) -> float:
    """POSIX time for ``created_at``, or the oldest key when it will not parse."""
    try:
        return layout.parse_timestamp(value).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


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
    info = _regular_stat(fd)
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
        "source": "file",
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
    """``(mtime, size)`` for a regular ``notes.md``.

    ``None`` means the file is missing. ``_NOTE_SPECIAL`` means a
    symlink, FIFO, directory, or other non-regular file: not a note, and not
    a launch row. ``lstat`` does not open the inode, so a FIFO does not block
    the list. Other ``OSError``s propagate so the caller can skip that run.
    """
    try:
        info = _stat_entry(directory, dir_fd, NOTE_NAME)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return _NOTE_SPECIAL
    return info.st_mtime, info.st_size


def read_note(root, run_id: str) -> dict:
    """The run's note.

    A regular ``notes.md``, including an empty or whitespace-only file, is
    ``source`` ``file``. A missing ``notes.md`` whose ``meta.json`` ``notes``
    string has non-whitespace UTF-8 text is ``source`` ``launch``: line endings are
    normalized to ``\\n``, the text is cut to ``MAX_NOTE_BYTES`` of UTF-8 on
    a character boundary, and ``revision`` and ``updated_at`` are None.
    Otherwise the result is empty with ``source`` ``none``. A symlink, FIFO,
    directory, or other non-regular ``notes.md`` is not followed and is not a
    launch note. ``meta.json`` must be a regular file; otherwise
    ``FileNotFoundError``. A bad ``run_id`` raises ``ValueError``. The launch
    note is read through the same pinned directory and non-blocking
    regular-file open as ``notes.md``. A ``meta.json`` over ``_META_CAP``, one
    that is not JSON, one whose ``notes`` text is not UTF-8, or one that
    cannot be opened (for example permission denied) is not a launch note.

    At most ``MAX_NOTE_BYTES + 1`` bytes of ``notes.md`` are read. When the
    file is larger than ``MAX_NOTE_BYTES`` (by ``st_size`` or by that probe),
    the result adds ``too_large: True``. ``text`` is the first
    ``MAX_NOTE_BYTES`` of the bytes read, decoded with ``errors="replace"``.
    ``revision`` is the SHA-256 of the stored bytes when the note fits. When
    it does not, ``revision`` is the SHA-256 of the retained prefix plus
    ``b"\\0too_large"``, so it cannot match a note whose bytes are exactly
    that prefix. The probe byte and the unread tail are not hashed. A note
    that fits omits ``too_large``. ``too_large`` applies only to file notes.
    ``updated_at`` is the file mtime.
    """
    directory = layout.run_dir(root, run_id)
    with _pinned_run(directory) as dir_fd:
        _require_run(directory, dir_fd)
        kind, note = _read_stored(directory, dir_fd)
        if kind == "ok":
            return note
        if kind == "missing":
            launch = _launch_note(directory, dir_fd)
            if launch is not None:
                return launch
        return note


def write_note(root, run_id: str, text, base_revision) -> dict:
    """Replace the note when ``base_revision`` is still current.

    ``None`` means the note must be absent (a launch note's revision is
    None, so the first save creates ``notes.md``). Line endings are
    normalized to ``\\n``. NUL is rejected. Empty or whitespace-only text
    deletes the file, unless the run has a launch note: then an empty
    ``notes.md`` is written so the launch note does not come back.
    ``NoteConflict`` carries the note currently visible, including ``source``.
    ``NoteTooLarge`` is a ``ValueError``. A symlink, FIFO, or other
    non-regular ``notes.md`` raises ``ValueError`` and is left in place. That
    check happens after the revision compare, so a stale ``base_revision``
    is still ``NoteConflict``.

    An oversized file uses the revision described on :func:`read_note`.
    ``write_note`` leaves it unchanged unless ``base_revision`` equals that
    revision. A match replaces or deletes the whole file, including the
    unread tail. A mismatch raises ``NoteConflict`` with ``too_large`` set.
    """
    if not isinstance(text, str):
        raise ValueError("note text must be a string")
    text = _normalize_newlines(text)
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
            if kind == "missing":
                launch = _launch_note(directory, dir_fd)
                if launch is not None:
                    current = launch
            if base_revision != current["revision"]:
                raise NoteConflict(current)
            if kind == "special":
                raise ValueError("refusing to replace a non-regular notes.md")
            _ensure_attached(directory, dir_fd)
            if not text.strip():
                if kind == "missing":
                    has_launch = current.get("source") == "launch"
                else:
                    has_launch = _launch_note(directory, dir_fd) is not None
                if has_launch:
                    _publish(directory, dir_fd, b"")
                    return _load(directory, dir_fd)
                _unlink_note(directory, dir_fd)
                return _empty()
            _publish(directory, dir_fd, data)
            return _load(directory, dir_fd)


def list_notes(root, project=None, limit_bytes=4 * 1024 * 1024) -> dict:
    """Non-empty notes, newest first.

    File rows sort by ``notes.md`` mtime. A missing ``notes.md`` whose
    ``meta["notes"]`` (already loaded by :func:`reader.list_runs`) is
    non-whitespace UTF-8 is a launch row, sorted by ``created_at``. Text that
    cannot be encoded as UTF-8 is skipped, and that run does not drop the
    rest of the list. A symlink, FIFO, directory, or other non-regular
    ``notes.md`` is not a launch row. An unparseable ``created_at`` is the
    oldest. ``project`` keeps rows whose project string matches exactly. Each
    file is stat'd before its body is read. Whitespace-only files are skipped,
    including a cleared note that left an empty ``notes.md``.
    A file larger than ``MAX_NOTE_BYTES`` is omitted without being read
    (``truncated``). A file that grows past the cap between that stat and the
    read is still bounded to ``MAX_NOTE_BYTES + 1`` bytes, omitted, and
    counted as ``truncated``. Launch text is normalized and capped the same
    way as :func:`read_note`, and those bytes count toward ``limit_bytes``.
    ``too_large`` applies only to file notes and is not a row field. Once the
    next note would pass ``limit_bytes``, the scan stops and older bodies are
    not read. One unreadable run is skipped.
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
        if found is _NOTE_SPECIAL:
            continue
        if found is None:
            # list_runs already parsed meta.json. Do not open it again.
            # Only a truly missing file can fall back to the launch note.
            launch_text = _launch_text(meta.get("notes"))
            if launch_text is None:
                continue
            when = _created_sort_time(meta.get("created_at"))
            size = len(launch_text.encode("utf-8"))
            candidates.append((when, size, None, meta, row_project, launch_text))
            continue
        mtime, size = found
        candidates.append((mtime, size, directory, meta, row_project, None))
    # The sort key is a POSIX timestamp. The body is read only after this order is known.
    candidates.sort(key=lambda item: item[0], reverse=True)
    kept = []
    used = 0
    truncated = False
    for _when, size, directory, meta, row_project, launch_text in candidates:
        if launch_text is None and size == 0:
            continue
        # A note that does not fit ends the scan so older bodies stay unread.
        # Over the per-note cap but still inside the budget: omit the file and keep going.
        if used + size > limit_bytes:
            truncated = True
            break
        if launch_text is None and size > MAX_NOTE_BYTES:
            truncated = True
            continue
        run_id = meta.get("run_id")
        if launch_text is None:
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
            text = note["text"]
            updated_at = note["updated_at"]
            revision = note["revision"]
            source = "file"
        else:
            text = launch_text
            updated_at = None
            revision = None
            source = "launch"
        text_size = len(text.encode("utf-8"))
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
            "text": text,
            "updated_at": updated_at,
            "revision": revision,
            "source": source,
        })
        used += text_size
    return {"notes": kept, "truncated": truncated}

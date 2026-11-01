"""Append-only writer used by the training process.

One process owns a run. Threads inside that process share one lock: the
flush thread writes scalars while other threads log media, artifacts, code,
or config. Scalar appends are not fsynced. A crash can tear the last record
or the last event line; readers ignore that tail, and the next append chops
it off so later records stay aligned.

``finish`` is the durability point: overviews, data files, and ``meta.json``
are fsynced, then the status flips to ``finished`` or ``crashed``.
"""

from __future__ import annotations

import copy as _copy
import json
import logging
import math
import operator
import os
import socket
import stat
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .layout import (
    EVENTS_NAME,
    FORMAT_VERSION,
    INDEX_NAME,
    MEDIA_DIRNAME,
    META_NAME,
    RECORD_SIZE,
    RECORD_STRUCT,
    SCALARS_DIRNAME,
    TIME_BIN_NAME,
    atomic_write,
    contained,
    format_timestamp,
    fsync_dir,
    load_events,
    new_run_id,
    open_nofollow,
    parse_timestamp,
    read_file_nofollow,
    RunLockedError,
    run_dir,
    try_lock_run,
    unlock_run,
    valid_run_id,
)
from .objects import put_bytes, put_file
from .overview import OVERVIEW_MIN_POINTS, Overview

logger = logging.getLogger("agenticml.store.writer")

# Past this many series the rest are opened, appended, and closed per call.
# The first series that receive points keep their handles; _time.bin has its
# own handle and does not count toward the cap.
MAX_OPEN_HANDLES = 256
_UINT32_MAX = 0xFFFFFFFF
_FINISHED = frozenset({"finished", "crashed"})

MEDIA_MAX_PER_KEY = 64
MEDIA_MAX_PX = 1024


def _media_limit(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
        return value if value > 0 else default
    except (ValueError, TypeError):
        return default


def _downscale_image(data: bytes, content_type: str) -> tuple[bytes, str]:
    if not (content_type and content_type.startswith("image/")):
        return data, content_type
    try:
        from PIL import Image
    except ImportError:
        return data, content_type

    max_px = _media_limit("AGENTICML_MEDIA_MAX_PX", MEDIA_MAX_PX)
    try:
        import io

        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if max(w, h) <= max_px:
            return data, content_type

        scale = max_px / max(w, h)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
        resized = img.resize((new_w, new_h), resample=resample)

        buf_png = io.BytesIO()
        resized.save(buf_png, format="PNG")
        return buf_png.getvalue(), "image/png"
    except Exception as exc:
        logger.warning("could not downscale image: %s", exc)
        return data, content_type


class RunFinishedError(RuntimeError):
    """Raised when an operation is attempted on a finished or crashed run."""


@dataclass
class _Series:
    id: int
    monotonic: bool
    last_step: int | None
    max_step: int | None


def _hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:
        return None


def _meta_bytes(meta: dict) -> bytes:
    text = json.dumps(meta, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _initial_meta(user: dict, run_id: str, created_at: str, pid: int, hostname: str | None) -> dict:
    meta = {
        "format_version": FORMAT_VERSION,
        "run_id": run_id,
        "project": "",
        "name": "",
        "config": {},
        "tags": [],
        "notes": "",
        "group": None,
        "job_type": None,
        "parent_run_id": None,
        "env": {},
        "git": {},
        "status": "running",
        "summary": {},
        "created_at": created_at,
        "finished_at": None,
        "pid": pid,
        "hostname": hostname,
    }
    meta.update(user)
    # The directory and the clock belong to this process, not the caller.
    meta["format_version"] = FORMAT_VERSION
    meta["run_id"] = run_id
    meta["status"] = "running"
    meta["created_at"] = created_at
    meta["finished_at"] = None
    meta["pid"] = pid
    meta["hostname"] = hostname
    return meta


def coerce_step(step) -> int | None:
    """Return a uint32 step, or None when the point should be dropped.

    Bools are rejected (they are ints, but not steps). Numpy integer scalars
    implement ``__index__``; floats and numeric strings do not.
    """
    if isinstance(step, bool):
        return None
    dtype = getattr(step, "dtype", None)
    if dtype is not None and getattr(dtype, "kind", None) == "b":
        return None
    try:
        value = operator.index(step)
    except Exception:
        return None
    if value < 0 or value > _UINT32_MAX:
        return None
    return value


def coerce_value(value):
    """``float(value)`` for real numbers, else None.

    Strings are rejected even when ``float("1")`` would succeed: a metric
    that arrived as text is a bug, not a number. Bools are accepted (0.0 / 1.0).
    """
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _pack_record(step: int, value: float) -> tuple[bytes, bool]:
    """Pack one record. Huge finite values become signed inf (they do not fit in f32)."""
    try:
        return RECORD_STRUCT.pack(step, value), False
    except OverflowError:
        return RECORD_STRUCT.pack(step, math.copysign(math.inf, value)), True


def _heal_bin(path: Path) -> None:
    """Drop a torn tail so the next append stays 8-byte aligned."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return
        size = st.st_size
    except FileNotFoundError:
        return
    extra = size % RECORD_SIZE
    if extra:
        with open_nofollow(path, "r+b") as handle:
            handle.truncate(size - extra)


def _heal_events(path: Path) -> None:
    """Make ``events.jsonl`` end on a record boundary before the next append.

    A last line that is valid JSON but missing its newline is closed, not
    deleted: the writer crashed between the object and the terminator.
    Anything that does not parse is chopped back to the previous newline.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return
    if not data or data.endswith(b"\n"):
        return
    cut = data.rfind(b"\n")
    tail = data[cut + 1 :]
    try:
        json.loads(tail.decode("utf-8"))
    except (ValueError, RecursionError):
        keep = 0 if cut < 0 else cut + 1
        with open_nofollow(path, "r+b") as handle:
            handle.truncate(keep)
        return
    with open_nofollow(path, "ab", buffering=0) as handle:
        handle.write(b"\n")


def _read_bounds(path: Path, monotonic: bool) -> tuple[int | None, int | None]:
    """``(last_appended_step, max_step)`` from a series file.

    Monotonic series are non-decreasing, so the last record is the max and
    the file does not need a scan. A non-monotonic series is scanned; the
    last record is still remembered so the next decrease can be detected.
    """
    _heal_bin(path)
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return None, None
        size = st.st_size
    except (FileNotFoundError, OSError):
        return None, None
    count = size // RECORD_SIZE
    if count == 0:
        return None, None
    if monotonic:
        with open_nofollow(path, "rb") as handle:
            handle.seek((count - 1) * RECORD_SIZE)
            raw = handle.read(4)
        if len(raw) < 4:
            return None, None
        step = struct.unpack("<I", raw)[0]
        return step, step
    last = None
    max_step = None
    with open_nofollow(path, "rb") as handle:
        data = handle.read(count * RECORD_SIZE)
    for step, _value in RECORD_STRUCT.iter_unpack(data):
        last = step
        if max_step is None or step > max_step:
            max_step = step
    return last, max_step


def _store_blob(root: Path, blob) -> tuple[str, int]:
    if isinstance(blob, (bytes, bytearray, memoryview)):
        data = bytes(blob)
        return put_bytes(root, data), len(data)
    return put_file(root, blob)


def _write_all(handle, data: bytes) -> None:
    """Finish a write. Unbuffered ``FileIO.write`` may return a short count."""
    view = memoryview(data)
    while len(view):
        written = handle.write(view)
        if not written:
            raise OSError("failed to write run bytes")
        view = view[written:]


def _fsync_file(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class RunWriter:
    """One open run. Construct with :meth:`create`, :meth:`resume`, or :meth:`amend`."""

    def __init__(self, root, run_id: str) -> None:
        if not valid_run_id(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        self.root = Path(root).expanduser().resolve()
        self.run_dir = run_dir(self.root, run_id)
        self.run_id = run_id
        self._lock = threading.Lock()
        self._path_cache: dict[str, Path] = {}
        self._finished = False
        self._run_lock = None
        self._events_healed = False
        self._handles: dict[int, object] = {}
        self._time_handle = None
        self._series: dict[str, _Series] = {}
        self._next_id = 0
        self._warned: set[tuple[str, str]] = set()
        self._media: dict = {}
        self._next_media_id = 0
        self._artifact_versions: dict = {}
        self._meta: dict = {}
        self._created_at_unix = 0.0
        self._events_path = self._c(self.run_dir / EVENTS_NAME)
        self._meta_path = self._c(self.run_dir / META_NAME)
        self._scalars = self.run_dir / SCALARS_DIRNAME

    def __repr__(self) -> str:
        return f"RunWriter(run_id={self.run_id!r})"

    def _c(self, path: Path) -> Path:
        """Resolved ``path`` inside the store root. Cached after the first check."""
        key = str(path)
        cached = self._path_cache.get(key)
        if cached is not None:
            return cached
        contained(self.root, path.parent)
        if not path.is_symlink():
            contained(self.root, path)
        self._path_cache[key] = path
        return path

    @classmethod
    def create(cls, root, *, run_id: str | None = None, meta: dict | None = None) -> RunWriter:
        """Create a run directory and write ``meta.json`` with status ``running``.

        ``run_id`` defaults to :func:`new_run_id`. System fields (format
        version, id, status, timestamps, pid, hostname) are set here even if
        ``meta`` contains them. Raises ``FileExistsError`` when that id
        already has a ``meta.json``.
        """
        if run_id is None:
            run_id = new_run_id()
        if meta is None:
            user: dict = {}
        elif isinstance(meta, dict):
            user = _copy.deepcopy(meta)
        else:
            raise TypeError("meta must be a dict")
        writer = cls(root, run_id)
        if writer._meta_path.is_file():
            raise FileExistsError(writer._meta_path)
        created_at = format_timestamp()
        writer._created_at_unix = parse_timestamp(created_at).timestamp()
        built = _initial_meta(user, run_id, created_at, os.getpid(), _hostname())
        writer.run_dir.mkdir(parents=True, exist_ok=True)
        try:
            writer._acquire_run_lock()
            if writer._meta_path.is_file():
                raise FileExistsError(writer._meta_path)
            writer._c(writer._scalars).mkdir(parents=True, exist_ok=True)
            writer._c(writer.run_dir / MEDIA_DIRNAME).mkdir(parents=True, exist_ok=True)
            writer._events_path.touch(exist_ok=True)
            writer._heal_events_once()
            # Indexes before meta, so a crash mid-create is invisible to list_runs.
            writer._persist_index()
            writer._persist_media()
            writer._meta_data = _meta_bytes(built)
            atomic_write(writer._meta_path, writer._meta_data)
            writer._meta = built
        except BaseException:
            writer.close()
            raise
        return writer

    @classmethod
    def resume(cls, root, run_id: str, *, must: bool) -> RunWriter | None:
        """Reopen a run, mark it ``running``, and append a ``resume`` event.

        Missing directory: ``must=True`` raises ``FileNotFoundError``,
        ``must=False`` returns None so the caller can ``create(run_id=...)``.
        A directory without ``meta.json`` is not a run and raises either way.
        A pruned run (any series has a ``.ovw`` and no ``.bin``) raises
        ``ValueError``. ``last_step`` is the max step across series (last
        record when the series is still monotonic, otherwise a scan).
        """
        if not valid_run_id(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        directory = run_dir(root, run_id)
        if directory.exists() and not directory.is_dir():
            raise NotADirectoryError(directory)
        if not directory.is_dir():
            if must:
                raise FileNotFoundError(directory)
            return None
        writer = cls(root, run_id)
        try:
            writer._acquire_run_lock()
            if not writer._meta_path.is_file():
                raise FileNotFoundError(writer._meta_path)
            writer._reject_pruned()
            writer._load_existing()
            writer._mark_resumed()
        except BaseException:
            writer.close()
            raise
        return writer

    @classmethod
    def amend(cls, root, run_id: str) -> RunWriter:
        """Reopen a run that is not live to change metadata.

        Takes the exclusive run lock without refreshing ``run.lock``'s mtime,
        so a dead run does not look alive. Status, ``finished_at``, pid, and
        hostname are left untouched; there is no ``resume`` event. Pruned
        runs are allowed (metadata edits do not rewrite overviews). The
        caller applies updates and then ``close()`` (no status change) or
        ``finish()`` (used by ``agenticml finish``).
        """
        if not valid_run_id(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        directory = run_dir(root, run_id)
        if not directory.is_dir():
            raise FileNotFoundError(directory)
        writer = cls(root, run_id)
        try:
            lock_path = writer.run_dir / "run.lock"
            lock_existed = os.path.lexists(lock_path)
            writer._acquire_run_lock(heartbeat=False)
            if not writer._meta_path.is_file():
                raise FileNotFoundError(writer._meta_path)
            if not lock_existed:
                handle = writer._run_lock
                if handle is not None and not handle.closed and os.utime in os.supports_fd:
                    os.utime(handle.fileno(), (0, 0))
                elif os.utime in os.supports_follow_symlinks:
                    os.utime(lock_path, (0, 0), follow_symlinks=False)
                else:
                    os.utime(lock_path, (0, 0))
            if not writer._run_lock.locking_supported:
                from .reader import STALE_AFTER_SECONDS

                try:
                    raw_meta = json.loads(writer._meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, RecursionError, UnicodeError):
                    raw_meta = None
                if isinstance(raw_meta, dict) and raw_meta.get("status") == "running":
                    try:
                        age = time.time() - os.lstat(lock_path).st_mtime
                    except FileNotFoundError:
                        age = STALE_AFTER_SECONDS
                    except OSError:
                        age = 0.0
                    if age < STALE_AFTER_SECONDS:
                        raise RunLockedError(
                            f"run {run_id} is open in another process"
                        )
            writer._load_existing()
        except BaseException:
            writer.close()
            raise
        return writer

    def _acquire_run_lock(self, *, heartbeat: bool = True) -> None:
        # Short shared reader probes can briefly block an exclusive lock.
        for attempt in range(11):
            self._run_lock = try_lock_run(self.run_dir)
            if self._run_lock is not None:
                if heartbeat:
                    self.heartbeat()
                return
            if attempt < 10:
                time.sleep(0.05)
        raise RunLockedError(f"run {self.run_id} is open in another process")

    def heartbeat(self) -> None:
        """Refresh liveness even when the trainer has no metrics to flush."""
        with self._lock:
            handle = self._run_lock
            if handle is not None and not handle.closed and handle.owner_pid == os.getpid():
                if os.utime in os.supports_fd:
                    os.utime(handle.fileno())
                else:
                    os.utime(self.run_dir / "run.lock", follow_symlinks=False)

    def _release_run_lock(self) -> None:
        handle, self._run_lock = self._run_lock, None
        unlock_run(handle)

    def close(self) -> None:
        """Close without finishing, releasing ownership so another writer can resume."""
        with self._lock:
            self._close_handles()
            self._finished = True
            self._release_run_lock()

    def __del__(self):
        if hasattr(self, "_run_lock"):
            self._close_handles()
            self._release_run_lock()

    @property
    def last_step(self) -> int | None:
        """Maximum step stored in any series, or None when none have points."""
        with self._lock:
            best = None
            for series in self._series.values():
                if series.max_step is None:
                    continue
                if best is None or series.max_step > best:
                    best = series.max_step
            return best

    def write_scalars(self, items) -> None:
        """Append ``(step, metrics)`` points.

        A bad step or a value ``float()`` cannot convert is dropped. The
        first drop for a key logs one warning; later drops of that key do
        not. One ``_time.bin`` record is appended per call that stored at
        least one point: the max accepted step, and seconds since
        ``created_at`` as float32.
        """
        normalized = _normalize_items(items)
        with self._lock:
            self._check_open()
            buffers: dict[int, bytearray] = {}
            index_dirty = False
            max_step = None
            accepted = False
            new_keys: list[str] = []
            saved_next = self._next_id
            saved = {
                key: (series.monotonic, series.last_step, series.max_step)
                for key, series in self._series.items()
            }
            # Index publish and the in-memory registry commit together. A
            # failure before that rolls the new keys back so a later batch
            # can persist them.
            published = False
            initial_sizes: dict[int, int] = {}
            time_initial_size = 0
            try:
                for step, metrics in normalized:
                    try:
                        coerced = coerce_step(step)
                    except Exception:
                        coerced = None
                    for key, value in metrics.items():
                        if not isinstance(key, str):
                            self._warn_once(
                                f"<non-str:{type(key).__name__}>",
                                f"dropping scalar points for key {key!r}: key is not a string",
                                reason="bad_key",
                            )
                            continue
                        if coerced is None:
                            self._warn_once(
                                key,
                                f"dropping scalar points for key {key!r}: "
                                f"step {step!r} outside 0..{_UINT32_MAX}",
                                reason="bad_step",
                            )
                            continue
                        try:
                            number = coerce_value(value)
                        except Exception:
                            number = None
                        if number is None:
                            self._warn_once(
                                key,
                                f"dropping scalar points for key {key!r}: value is not a real number",
                                reason="bad_value",
                            )
                            continue
                        packed, overflowed = _pack_record(coerced, number)
                        if overflowed:
                            self._warn_once(
                                key,
                                f"scalar key {key!r} overflowed float32 and was stored as inf",
                                reason="overflow",
                            )
                        series = self._series.get(key)
                        if series is None:
                            series = _Series(self._next_id, True, None, None)
                            self._next_id += 1
                            self._series[key] = series
                            new_keys.append(key)
                            index_dirty = True
                        elif (
                            series.monotonic
                            and series.last_step is not None
                            and coerced < series.last_step
                        ):
                            # Persist the flag before the decreasing bytes hit disk.
                            # A reader that still sees monotonic=true would bisect
                            # a file that is no longer sorted.
                            series.monotonic = False
                            index_dirty = True
                        series.last_step = coerced
                        if series.max_step is None or coerced > series.max_step:
                            series.max_step = coerced
                        buf = buffers.get(series.id)
                        if buf is None:
                            buf = bytearray()
                            buffers[series.id] = buf
                        buf += packed
                        accepted = True
                        if max_step is None or coerced > max_step:
                            max_step = coerced
                if not accepted:
                    return
                if index_dirty:
                    self._persist_index()
                published = True
                initial_sizes = {
                    series_id: self._bin_size(series_id) for series_id in buffers
                }
                time_initial_size = self._time_bin_size()
                for series_id, buf in buffers.items():
                    self._append_series_bytes(series_id, bytes(buf))
                seconds = time.time() - self._created_at_unix
                payload, _overflowed = _pack_record(max_step, seconds)
                self._append_time(payload)
            except Exception:
                if not published:
                    for key in new_keys:
                        self._series.pop(key, None)
                    self._next_id = saved_next
                    for key, state in saved.items():
                        series = self._series.get(key)
                        if series is not None:
                            series.monotonic, series.last_step, series.max_step = state
                else:
                    for series_id, size in initial_sizes.items():
                        self._truncate_bin(series_id, size)
                    self._truncate_time_bin(time_initial_size)
                    for key in new_keys:
                        series = self._series.get(key)
                        if series is not None:
                            series.monotonic = True
                            series.last_step = None
                            series.max_step = None
                    for key, state in saved.items():
                        series = self._series.get(key)
                        if series is not None:
                            series.monotonic, series.last_step, series.max_step = state
                raise

    def set_config(self, config: dict) -> None:
        """Replace ``config``. Append ``config_update`` only when the JSON changed.

        Create does not emit this event; the initial config lives in ``meta.json``.
        """
        if not isinstance(config, dict):
            raise TypeError("config must be a dict")
        config = _copy.deepcopy(config)
        with self._lock:
            self._check_open()
            if _canon(self._meta.get("config")) == _canon(config):
                return
            self._update_meta({"config": config})
            self._append_event({"t": time.time(), "kind": "config_update", "config": config})

    def set_summary(self, summary: dict) -> None:
        """Replace ``summary`` in ``meta.json``. No event; ``finish`` records it."""
        if not isinstance(summary, dict):
            raise TypeError("summary must be a dict")
        summary = _copy.deepcopy(summary)
        with self._lock:
            self._check_open()
            self._update_meta({"summary": summary})

    def set_meta_fields(self, **fields) -> None:
        """Update meta keys when the canonical JSON actually changes.

        ``run_id`` and ``format_version`` stay put: they are the identity of
        the directory.
        """
        for key in fields:
            if key in ("format_version", "run_id"):
                raise ValueError(f"cannot change {key}")
        copied = {key: _copy.deepcopy(value) for key, value in fields.items()}
        with self._lock:
            self._check_open()
            self._update_meta(copied)

    def record_meta_update(self, fields: dict) -> None:
        """Append a ``meta_update`` event with the new full values of changed keys."""
        if not isinstance(fields, dict):
            raise TypeError("fields must be a dict")
        fields = _copy.deepcopy(fields)
        with self._lock:
            self._check_open()
            self._append_event({"t": time.time(), "kind": "meta_update", "fields": fields})

    def add_code(self, manifest_files: list, files: list) -> None:
        """Store code bytes in the object store and append a ``code`` event.

        ``files`` is ``(manifest_path, bytes | Path)``. Entries are matched to
        ``manifest_files`` by ``path``; a file with no manifest row is recorded
        with ``content_type`` ``application/octet-stream`` and ``source``
        ``code``. Later code events win per path when a reader merges them.
        """
        if not isinstance(manifest_files, list):
            raise TypeError("manifest_files must be a list")
        for entry in manifest_files:
            if not isinstance(entry, dict):
                raise TypeError("manifest entries must be dicts")
        if not isinstance(files, list):
            raise TypeError("files must be a list")
        stored: dict[str, tuple[str, int]] = {}
        for item in files:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise TypeError("files must be (path, bytes | Path) pairs")
            manifest_path, blob = item
            if not isinstance(manifest_path, str):
                raise TypeError("manifest path must be a string")
            stored[manifest_path] = _store_blob(self.root, blob)
        with self._lock:
            self._check_open()
            manifest = []
            seen = set()
            for entry in manifest_files:
                record = dict(entry)
                path = record.get("path")
                if path in stored:
                    sha, size = stored[path]
                    record["sha256"] = sha
                    record["size"] = size
                    seen.add(path)
                manifest.append(record)
            for path, (sha, size) in stored.items():
                if path in seen:
                    continue
                manifest.append(
                    {
                        "path": path,
                        "sha256": sha,
                        "size": size,
                        "content_type": "application/octet-stream",
                        "source": "code",
                    }
                )
            self._append_event({"t": time.time(), "kind": "code", "manifest": manifest})

    def add_media(self, key: str, step: int, data: bytes, content_type: str, caption) -> str:
        """Store media bytes and append a ``media/index.json`` row. Returns the digest.

        Capped at MEDIA_MAX_PER_KEY (default 64) entries per key. When an entry exceeds the cap,
        thins evenly by step (drops every other entry, keeping the first and newest).
        Images are downscaled when Pillow is present so the longer side <= MEDIA_MAX_PX (default 1024).
        """
        if not isinstance(key, str):
            raise TypeError("media key must be a string")
        if isinstance(step, bool):
            raise TypeError("step must be an integer")
        try:
            step = operator.index(step)
        except TypeError:
            raise TypeError("step must be an integer") from None
        if isinstance(data, str) or not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes")
        if not isinstance(content_type, str):
            raise TypeError("content_type must be a string")
        if caption is not None and not isinstance(caption, str):
            raise TypeError("caption must be a string or None")
        data_bytes = bytes(data)
        data_bytes, content_type = _downscale_image(data_bytes, content_type)
        sha = put_bytes(self.root, data_bytes)
        entry = {
            "step": step,
            "sha256": sha,
            "content_type": content_type,
            "caption": caption,
        }
        with self._lock:
            self._check_open()
            rows = self._media.get(key)
            if not isinstance(rows, list):
                rows = []
                self._media[key] = rows
            old_rows = list(rows)
            entry["id"] = self._next_media_id
            rows.append(entry)
            max_entries = _media_limit("AGENTICML_MEDIA_MAX_PER_KEY", MEDIA_MAX_PER_KEY)
            if len(rows) > max_entries:
                thinned = rows[::2]
                if thinned[-1] is not rows[-1]:
                    thinned.append(rows[-1])
                rows[:] = thinned
            try:
                self._persist_media()
            except Exception:
                rows[:] = old_rows
                if not rows and key in self._media and self._media[key] is rows:
                    del self._media[key]
                raise
            self._next_media_id += 1
            return sha

    def add_artifact(self, file_path, name, type, metadata, copy: bool = False) -> dict:
        """Record an artifact. ``copy=False`` stores a reference; ``copy=True`` blobs the bytes.

        Local ``version`` is 1 plus the number of earlier artifact events with
        the same name. The returned dict is the event payload (without ``t``
        and ``kind``).
        """
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise TypeError("metadata must be a dict")
        path = Path(file_path)
        if path.is_dir():
            raise IsADirectoryError(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        stat = path.stat()
        absolute = str(path.resolve())
        if copy:
            sha, size = put_file(self.root, path)
        else:
            sha, size = None, stat.st_size
        record = {
            "name": name,
            "type": type,
            "metadata": _copy.deepcopy(metadata),
            "path": absolute,
            "size": size,
            "mtime": stat.st_mtime,
            "sha256": sha,
            "copy": bool(copy),
        }
        with self._lock:
            self._check_open()
            version = self._artifact_versions.get(name, 0) + 1
            record["version"] = version
            self._append_event({"t": time.time(), "kind": "artifact", **record})
            self._artifact_versions[name] = version
            return dict(record)

    def finish(self, status: str, summary: dict | None = None) -> None:
        """Flush, write ``.ovw`` files, fsync, set status, append ``finish``.

        Idempotent in this process: a second call does nothing, and the first
        status and summary stay. ``status`` is ``finished`` or ``crashed``.
        """
        if status not in _FINISHED:
            raise ValueError("status must be 'finished' or 'crashed'")
        if summary is None:
            summary = {}
        if not isinstance(summary, dict):
            raise TypeError("summary must be a dict")
        summary = _copy.deepcopy(summary)
        with self._lock:
            if self._finished:
                return
            self._close_handles()
            self._write_overviews()
            self._fsync_outputs()
            self._update_meta(
                {
                    "status": status,
                    "summary": summary,
                    "finished_at": format_timestamp(),
                },
                fsync=True,
            )
            self._append_event(
                {
                    "t": time.time(),
                    "kind": "finish",
                    "status": status,
                    "summary": summary,
                }
            )
            _fsync_file(self._events_path)
            self._fsync_dir(self.run_dir, "run")
            self._finished = True
            self._release_run_lock()
            from .gc import record_finished_usage

            record_finished_usage(self.root, self.run_id)

    def _check_open(self) -> None:
        if self._finished:
            raise RunFinishedError(f"run {self.run_id} is finished")

    def _warn_once(self, key: str, message: str, reason: str | None = None) -> None:
        token = (reason or message, key)
        if token in self._warned:
            return
        self._warned.add(token)
        logger.warning("%s", message)

    def _update_meta(self, updates: dict, *, fsync: bool = False) -> None:
        new = dict(self._meta)
        new.update(updates)
        data = _meta_bytes(new)
        if data == self._meta_data:
            return
        atomic_write(self._meta_path, data, fsync=fsync)
        self._meta = new
        self._meta_data = data

    def _persist_index(self) -> None:
        series = {
            key: {"id": state.id, "monotonic": state.monotonic}
            for key, state in self._series.items()
        }
        payload = {"version": 1, "series": series}
        text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        atomic_write(self._c(self._scalars / INDEX_NAME), text.encode("utf-8"))

    def _persist_media(self) -> None:
        text = json.dumps(self._media, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        atomic_write(self._c(self.run_dir / MEDIA_DIRNAME / INDEX_NAME), text.encode("utf-8"))

    def _heal_events_once(self) -> None:
        if self._events_healed:
            return
        _heal_events(self._events_path)
        self._events_healed = True

    def _append_event(self, event: dict) -> None:
        self._heal_events_once()
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with open_nofollow(self._events_path, "ab", buffering=0) as handle:
            size = os.fstat(handle.fileno()).st_size
            try:
                _write_all(handle, line + b"\n")
            except BaseException:
                try:
                    os.ftruncate(handle.fileno(), size)
                except BaseException:
                    self._events_healed = False
                    raise
                raise

    def _bin_path(self, series_id: int) -> Path:
        return self._c(self._scalars / f"{series_id}.bin")

    def _reject_pruned(self) -> None:
        index_path = self._c(self._scalars / INDEX_NAME)
        if not index_path.is_file():
            return
        try:
            raw = read_file_nofollow(index_path)
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            return
        if not isinstance(data, dict):
            return
        series = data.get("series")
        if not isinstance(series, dict):
            return
        for key, info in series.items():
            if not isinstance(info, dict):
                continue
            series_id = info.get("id")
            if isinstance(series_id, bool) or not isinstance(series_id, int) or series_id < 0:
                continue
            ovw_path = self._c(self._scalars / f"{series_id}.ovw")
            bin_path = self._bin_path(series_id)
            if ovw_path.is_file() and not bin_path.is_file():
                raise ValueError(
                    f"run {self.run_id} was pruned after sync; it cannot be resumed"
                )

    def _fsync_dir(self, path: Path, label: str) -> None:
        try:
            fsync_dir(path)
        except OSError:
            logger.warning("could not fsync %s directory %s", label, path, exc_info=True)

    def _append_series_bytes(self, series_id: int, data: bytes) -> None:
        if not data:
            return
        handle = self._handles.get(series_id)
        if handle is None:
            path = self._bin_path(series_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            _heal_bin(path)
            opened = open_nofollow(path, "ab", buffering=0)
            if len(self._handles) < MAX_OPEN_HANDLES:
                self._handles[series_id] = opened
                _write_all(opened, data)
                return
            try:
                _write_all(opened, data)
            finally:
                opened.close()
            return
        _write_all(handle, data)

    def _append_time(self, payload: bytes) -> None:
        if self._time_handle is None:
            path = self._c(self._scalars / TIME_BIN_NAME)
            path.parent.mkdir(parents=True, exist_ok=True)
            _heal_bin(path)
            self._time_handle = open_nofollow(path, "ab", buffering=0)
        _write_all(self._time_handle, payload)

    def _bin_size(self, series_id: int) -> int:
        handle = self._handles.get(series_id)
        if handle is not None:
            try:
                return os.fstat(handle.fileno()).st_size
            except (OSError, ValueError):
                pass
        path = self._bin_path(series_id)
        try:
            _heal_bin(path)
            return path.stat().st_size
        except (FileNotFoundError, OSError):
            return 0

    def _time_bin_size(self) -> int:
        if self._time_handle is not None:
            try:
                return os.fstat(self._time_handle.fileno()).st_size
            except (OSError, ValueError):
                pass
        path = self._c(self._scalars / TIME_BIN_NAME)
        try:
            _heal_bin(path)
            return path.stat().st_size
        except (FileNotFoundError, OSError):
            return 0

    def _truncate_bin(self, series_id: int, size: int) -> None:
        handle = self._handles.get(series_id)
        if handle is not None:
            try:
                os.ftruncate(handle.fileno(), size)
                try:
                    handle.seek(size)
                except (OSError, ValueError):
                    pass
                return
            except (OSError, ValueError):
                pass
        path = self._bin_path(series_id)
        if path.is_file() and not path.is_symlink():
            try:
                with open_nofollow(path, "r+b") as h:
                    os.ftruncate(h.fileno(), size)
            except (OSError, ValueError):
                pass

    def _truncate_time_bin(self, size: int) -> None:
        if self._time_handle is not None:
            try:
                os.ftruncate(self._time_handle.fileno(), size)
                try:
                    self._time_handle.seek(size)
                except (OSError, ValueError):
                    pass
                return
            except (OSError, ValueError):
                pass
        path = self._c(self._scalars / TIME_BIN_NAME)
        if path.is_file() and not path.is_symlink():
            try:
                with open_nofollow(path, "r+b") as h:
                    os.ftruncate(h.fileno(), size)
            except (OSError, ValueError):
                pass

    def _close_handles(self) -> None:
        for handle in self._handles.values():
            try:
                handle.close()
            except OSError:
                pass
        self._handles.clear()
        if self._time_handle is not None:
            try:
                self._time_handle.close()
            except OSError:
                pass
            self._time_handle = None

    def _write_overviews(self) -> None:
        for state in self._series.values():
            path = self._bin_path(state.id)
            ovw_path = self._c(self._scalars / f"{state.id}.ovw")
            # A pruned series keeps the overview that was written at the
            # earlier finish. Replacing it from a missing bin would wipe it.
            if ovw_path.is_file() and not path.is_file():
                continue
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            usable = size - (size % RECORD_SIZE)
            point_count = usable // RECORD_SIZE
            if point_count > OVERVIEW_MIN_POINTS:
                overview = Overview()
                chunk_size = 64 * 1024  # 64 KB
                remaining = usable
                with open_nofollow(path, "rb") as handle:
                    while remaining > 0:
                        to_read = min(chunk_size, remaining)
                        buf = handle.read(to_read)
                        if not buf:
                            break
                        buf_usable = len(buf) - (len(buf) % RECORD_SIZE)
                        if not buf_usable:
                            break
                        feed = memoryview(buf)[:buf_usable] if len(buf) != buf_usable else buf
                        overview.add_many(RECORD_STRUCT.iter_unpack(feed))
                        remaining -= buf_usable
                atomic_write(ovw_path, overview.to_bytes(), fsync=True)
            else:
                # Remove any stale .ovw for a series that is now at or below
                # the threshold, only because its .bin exists.
                ovw_path.unlink(missing_ok=True)

    def _fsync_outputs(self) -> None:
        for state in self._series.values():
            _fsync_file(self._bin_path(state.id))
            _fsync_file(self._c(self._scalars / f"{state.id}.ovw"))
        _fsync_file(self._c(self._scalars / TIME_BIN_NAME))
        _fsync_file(self._events_path)
        _fsync_file(self._c(self._scalars / INDEX_NAME))
        self._fsync_dir(self._c(self._scalars), "scalars")
        media = self.run_dir / MEDIA_DIRNAME
        if media.exists():
            _fsync_file(self._c(media / INDEX_NAME))
            self._fsync_dir(self._c(media), "media")

    def _load_existing(self) -> None:
        try:
            raw = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"corrupt meta.json for {self.run_id}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"corrupt meta.json for {self.run_id}")
        created = raw.get("created_at")
        if not isinstance(created, str):
            raise ValueError(f"run {self.run_id} has no created_at")
        self._meta = raw
        self._meta_data = _meta_bytes(raw)
        self._created_at_unix = parse_timestamp(created).timestamp()
        self._c(self._scalars).mkdir(parents=True, exist_ok=True)
        self._c(self.run_dir / MEDIA_DIRNAME).mkdir(parents=True, exist_ok=True)
        self._load_series()
        self._load_media()
        self._heal_events_once()
        self._load_artifact_versions()

    def _load_series(self) -> None:
        index_path = self._c(self._scalars / INDEX_NAME)
        self._series = {}
        self._next_id = 0
        if not index_path.is_file():
            _heal_bin(self._c(self._scalars / TIME_BIN_NAME))
            return
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"corrupt scalars index for {self.run_id}") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError(f"unsupported scalars index for {self.run_id}")
        series = data.get("series")
        if not isinstance(series, dict):
            raise ValueError(f"corrupt scalars index for {self.run_id}")
        best = -1
        for key, info in series.items():
            if not isinstance(key, str) or not isinstance(info, dict):
                raise ValueError(f"corrupt scalars index for {self.run_id}")
            series_id = info.get("id")
            if isinstance(series_id, bool) or not isinstance(series_id, int) or series_id < 0:
                raise ValueError(f"bad series id for {key!r}")
            monotonic = bool(info.get("monotonic", False))
            last, max_step = _read_bounds(self._bin_path(series_id), monotonic)
            self._series[key] = _Series(series_id, monotonic, last, max_step)
            if series_id > best:
                best = series_id
        self._next_id = best + 1
        _heal_bin(self._c(self._scalars / TIME_BIN_NAME))

    def _load_media(self) -> None:
        path = self._c(self.run_dir / MEDIA_DIRNAME / INDEX_NAME)
        if not path.is_file():
            self._media = {}
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"corrupt media index for {self.run_id}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"corrupt media index for {self.run_id}")
        self._media = data
        self._next_media_id = 1 + max(
            (row["id"] for rows in data.values() if isinstance(rows, list)
             for row in rows if isinstance(row, dict)
             and type(row.get("id")) is int and row["id"] >= 0),
            default=-1,
        )

    def _load_artifact_versions(self) -> None:
        counts: dict = {}
        for event in load_events(self._events_path):
            if event.get("kind") != "artifact":
                continue
            name = event.get("name")
            counts[name] = counts.get(name, 0) + 1
        self._artifact_versions = counts

    def _mark_resumed(self) -> None:
        # A finished run is live again. pid/hostname describe this process;
        # created_at stays the original start.
        pid = os.getpid()
        self._update_meta(
            {
                "status": "running",
                "finished_at": None,
                "pid": pid,
                "hostname": _hostname(),
            }
        )
        self._append_event({"t": time.time(), "kind": "resume", "pid": pid})


def _normalize_items(items) -> list:
    normalized = []
    try:
        iterator = iter(items)
    except TypeError:
        raise TypeError("write_scalars items must be an iterable of (step, metrics)") from None
    for item in iterator:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise TypeError("write_scalars items must be (step, metrics) pairs")
        step, metrics = item
        if not isinstance(metrics, dict):
            raise TypeError("metrics must be a dict")
        normalized.append((step, metrics))
    return normalized

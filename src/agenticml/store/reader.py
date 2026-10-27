"""Read-only view of a v1 run directory.

Safe against a writer that is still appending: scalar files are read up to
the last complete 8-byte record, and a torn last line of ``events.jsonl`` is
ignored. This object never creates or truncates files, so a UI can poll it.

A finished or crashed run treats a valid ``.ovw`` as authoritative for an
overview query when the ``.bin`` complete-record count equals that total.
If the ``.bin`` is missing (a later ``sync --prune``), every ``series``
query is answered from the ``.ovw`` alone and ``mode`` is ``overview``,
including one that would otherwise be raw or a step range. If the ``.bin``
exists but its record count disagrees with the overview total, or the
overview is unreadable, the reader logs a warning and rebuilds from the
``.bin``. If that file is missing too, ``series`` returns an empty result.
A running run folds the ``.bin`` incrementally and remembers the byte offset,
so the next call only reads bytes that arrived since. A step range on a
monotonic series is a binary search over the fixed-width records; the reader
stats the file size before it trusts ``monotonic``, because the writer
clears that flag before appending an out-of-order record. A non-monotonic
series is scanned.

``RunReader`` is thread-safe. Several threads may call ``series`` on one
reader while a writer appends; one lock covers the overview cache and the
result built from it.
"""

from __future__ import annotations

import errno
import json
import logging
import math
import operator
import os
import stat
import struct
import threading
import time
from pathlib import Path

from .layout import (
    EVENTS_NAME,
    INDEX_NAME,
    META_NAME,
    RECORD_SIZE,
    RECORD_STRUCT,
    SCALARS_DIRNAME,
    TIME_BIN_NAME,
    contained,
    is_symlink_error,
    load_events,
    open_nofollow,
    read_file_nofollow,
    run_dir,
    valid_run_id,
    valid_sha,
)
from .overview import Overview

logger = logging.getLogger("agenticml.store.reader")

# A run still marked running whose meta and _time.bin have not been touched
# for this long is treated as abandoned. Wall clock, because that is what
# mtime is.
STALE_AFTER_SECONDS = 120

_DONE = frozenset({"finished", "crashed"})
_MISMATCH_WARNED: set[str] = set()


def run_references(root, run_id: str) -> dict[str, set[str]]:
    """Referenced digests by kind, raising on unreadable or corrupt input.

    Unlike the display APIs, collection cannot discard corrupt event lines
    (even a torn tail), since they might contain the only reference to a blob.
    All code events are retained, including superseded versions of a path.
    """
    directory = run_dir(root, run_id)

    def read_json(path):
        data = json.loads(read_file_nofollow(path))
        if not isinstance(data, dict):
            raise ValueError(f"expected object in {path}")
        return data

    read_json(directory / META_NAME)
    media_path = directory / "media" / INDEX_NAME
    contained(root, media_path.parent)
    media = read_json(media_path)
    refs = {"media": set(), "artifacts": set(), "code": set()}

    def add(kind, row):
        if not isinstance(row, dict):
            raise ValueError(f"invalid {kind} reference in run {run_id}")
        sha = row.get("sha256")
        if sha is not None:
            if not valid_sha(sha):
                raise ValueError(f"invalid {kind} digest in run {run_id}")
            refs[kind].add(sha)

    for key, rows in media.items():
        if key == "_next_id" and type(rows) is int:
            continue  # Accept indexes that persist their counter explicitly.
        if not isinstance(rows, list):
            raise ValueError(f"invalid media rows in run {run_id}")
        for row in rows:
            add("media", row)
    for line in read_file_nofollow(directory / EVENTS_NAME).split(b"\n"):
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError(f"invalid event in run {run_id}")
        if event.get("kind") == "artifact" and event.get("copy") is True:
            add("artifacts", event)
        elif event.get("kind") == "code":
            manifest = event.get("manifest")
            if not isinstance(manifest, list):
                raise ValueError(f"invalid code manifest in run {run_id}")
            for row in manifest:
                add("code", row)
    return refs


def list_runs(root) -> list[dict]:
    """One dict per run, newest ``created_at`` first.

    Only ``meta.json`` is parsed. Directories whose names are not run ids,
    and runs whose meta is missing, corrupt, or symlinked, are skipped.
    ``run_id`` is set to the directory name; if meta.json differs, a warning
    is logged once. ``effective_status`` is ``stale`` when ``status`` is
    ``running`` and the newest mtime of ``meta.json`` and ``scalars/_time.bin``
    is older than :data:`STALE_AFTER_SECONDS`; otherwise it is the stored status.
    """
    root_path = Path(root).expanduser().resolve()
    runs_root = str(root_path / "runs")
    if not os.path.isdir(runs_root):
        return []
    now = time.time()
    found: list[dict] = []
    with os.scandir(runs_root) as iterator:
        for entry in iterator:
            if not valid_run_id(entry.name):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            entry_path = entry.path
            meta_path = f"{entry_path}/{META_NAME}"
            try:
                raw = read_file_nofollow(meta_path)
                meta = json.loads(raw.decode("utf-8"))
            except OSError as exc:
                if is_symlink_error(exc) or os.path.islink(meta_path):
                    logger.warning("skipping symlinked meta.json in %s", entry_path)
                continue
            except (ValueError, RecursionError):
                continue
            if not isinstance(meta, dict):
                continue
            dir_run_id = entry.name
            meta_run_id = meta.get("run_id")
            if meta_run_id != dir_run_id:
                if dir_run_id not in _MISMATCH_WARNED:
                    _MISMATCH_WARNED.add(dir_run_id)
                    logger.warning(
                        "run %s has mismatched run_id %r in meta.json; using directory name",
                        dir_run_id,
                        meta_run_id,
                    )
                meta["run_id"] = dir_run_id
            meta["effective_status"] = _effective_status(meta, meta_path, entry_path, now)
            found.append(meta)
    found.sort(key=_run_sort_key, reverse=True)
    return found


def _run_sort_key(meta: dict) -> tuple:
    created = meta.get("created_at")
    if not isinstance(created, str):
        created = ""
    run_id = meta.get("run_id")
    if not isinstance(run_id, str):
        run_id = ""
    return (created, run_id)


def _effective_status(meta: dict, meta_path: str, run_path: str, now: float) -> str:
    status = meta.get("status")
    if not isinstance(status, str):
        status = ""
    if status != "running":
        return status
    try:
        st_meta = os.lstat(meta_path)
        newest = 0 if stat.S_ISLNK(st_meta.st_mode) else st_meta.st_mtime
    except OSError:
        newest = 0
    time_bin = f"{run_path}/{SCALARS_DIRNAME}/{TIME_BIN_NAME}"
    try:
        st_time = os.lstat(time_bin)
        if not stat.S_ISLNK(st_time.st_mode):
            newest = max(newest, st_time.st_mtime)
    except OSError:
        pass
    if now - newest > STALE_AFTER_SECONDS:
        return "stale"
    return "running"


def _bound(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("step bound must be an integer")
    try:
        return operator.index(value)
    except TypeError:
        raise TypeError("step bound must be an integer") from None


def _series_id(info: dict) -> int:
    series_id = info.get("id")
    if isinstance(series_id, bool) or not isinstance(series_id, int) or series_id < 0:
        raise ValueError(f"bad series id: {series_id!r}")
    return series_id


def _file_size(path: Path) -> int:
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            logger.warning("refusing symlinked file: %s", path)
            return 0
        return st.st_size
    except (FileNotFoundError, OSError):
        return 0


def _read_bytes(path: Path, lo: int, hi: int) -> bytes:
    if hi <= lo:
        return b""
    try:
        with open_nofollow(path) as handle:
            handle.seek(lo * RECORD_SIZE)
            data = handle.read((hi - lo) * RECORD_SIZE)
    except OSError as exc:
        if is_symlink_error(exc) or path.is_symlink():
            logger.warning("refusing symlinked scalar file: %s", path)
            return b""
        raise
    usable = len(data) - (len(data) % RECORD_SIZE)
    return data[:usable]


def _records_from(data: bytes) -> list:
    if not data:
        return []
    return list(RECORD_STRUCT.iter_unpack(data))


def _step_at(fd: int, index: int) -> int:
    data = os.pread(fd, 4, index * RECORD_SIZE)
    if len(data) < 4:
        raise ValueError("scalar file ended during range lookup")
    return struct.unpack("<I", data)[0]


def _bisect_left(fd: int, count: int, target: int) -> int:
    lo = 0
    hi = count
    while lo < hi:
        mid = (lo + hi) // 2
        if _step_at(fd, mid) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_right(fd: int, count: int, target: int) -> int:
    lo = 0
    hi = count
    while lo < hi:
        mid = (lo + hi) // 2
        if _step_at(fd, mid) <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bounds(path: Path, count: int, start, end) -> tuple[int, int]:
    """Inclusive ``[start, end]`` as a half-open record range. ``count`` is trusted.

    The series must be monotonic (non-decreasing). Duplicate steps are kept:
    the left bound is the first record with ``step >= start``, the right bound
    is the first with ``step > end``.
    """
    try:
        handle = open_nofollow(path)
    except OSError as exc:
        if is_symlink_error(exc) or path.is_symlink():
            logger.warning("refusing symlinked scalar file: %s", path)
            return 0, 0
        raise
    try:
        fd = handle.fileno()
        lo = 0 if start is None else _bisect_left(fd, count, start)
        hi = count if end is None else _bisect_right(fd, count, end)
    finally:
        handle.close()
    if hi < lo:
        return lo, lo
    return lo, hi


def _raw_result(key: str, points: list) -> dict:
    nonfinite = 0
    out = []
    for step, value in points:
        if not math.isfinite(value):
            nonfinite += 1
        out.append([step, value])
    return {
        "key": key,
        "total": len(out),
        "nonfinite": nonfinite,
        "mode": "raw",
        "points": out,
    }


def _overview_result(key: str, overview: Overview, max_points: int) -> dict:
    buckets = overview.buckets()
    if len(buckets) > max_points:
        buckets = overview.downsample(max_points)
    points = [
        [
            bucket.step_min,
            bucket.step_max,
            bucket.vmin,
            bucket.vmax,
            bucket.vsum / bucket.count,
            bucket.last,
        ]
        for bucket in buckets
    ]
    return {
        "key": key,
        "total": overview.point_count,
        "nonfinite": overview.nonfinite,
        "mode": "overview",
        "points": points,
    }


def _empty(key: str) -> dict:
    return {"key": key, "total": 0, "nonfinite": 0, "mode": "raw", "points": []}


class RunReader:
    """Read one run. Thread-safe. Raises ``ValueError`` for a bad id and ``FileNotFoundError`` if it is missing."""

    def __init__(self, root, run_id: str) -> None:
        if not valid_run_id(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        self.root = Path(root).expanduser().resolve()
        self.run_dir = run_dir(self.root, run_id)
        self.run_id = run_id
        self._lock = threading.RLock()
        self._path_cache: dict[str, Path] = {}
        self._meta_path = self._c(self.run_dir / META_NAME)
        if self._meta_path.is_symlink():
            logger.warning("refusing symlink for meta.json: %s", self._meta_path)
            raise FileNotFoundError(self._meta_path)
        if not self._meta_path.is_file():
            raise FileNotFoundError(self._meta_path)
        # key -> ("ovw"|"inc", Overview, offset or point_count)
        self._cache: dict = {}
        # key -> (token, overview or None). token is
        # (status, ovw size, ovw mtime_ns, bin size).
        self._ovw_decision: dict[str, tuple] = {}
        self._mismatch_warned: set[str] = set()
        self._series_ids: dict[str, int] = {}
        self._meta_sig = None
        self._status = ""
        self._index_cache: tuple[tuple[int, int, int], dict] | None = None

    def _c(self, path: Path) -> Path:
        """Resolved ``path`` inside the store root. Cached after the first check."""
        key = str(path)
        with self._lock:
            cached = self._path_cache.get(key)
            if cached is not None:
                return cached
            contained(self.root, path.parent)
            if not path.is_symlink():
                contained(self.root, path)
            self._path_cache[key] = path
            return path

    def meta(self) -> dict:
        """Current ``meta.json``. Re-read on each call so status changes show up."""
        try:
            raw = read_file_nofollow(self._meta_path)
            data = json.loads(raw.decode("utf-8"))
        except OSError as exc:
            if is_symlink_error(exc) or self._meta_path.is_symlink():
                logger.warning("refusing symlinked meta.json: %s", self._meta_path)
                raise ValueError(f"corrupt meta.json for {self.run_id}") from exc
            raise
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"corrupt meta.json for {self.run_id}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"corrupt meta.json for {self.run_id}")
        return data

    def keys(self) -> list:
        """Series keys in first-seen (id) order."""
        ranked = []
        for key, info in (self._index().get("series") or {}).items():
            if not isinstance(info, dict):
                continue
            series_id = info.get("id")
            if isinstance(series_id, bool) or not isinstance(series_id, int):
                series_id = 0
            ranked.append((series_id, key))
        ranked.sort()
        return [key for _series_id, key in ranked]

    def events(self) -> list:
        """Event objects. A torn last line is omitted; a bad earlier line is skipped."""
        return load_events(self._c(self.run_dir / EVENTS_NAME))

    def code_manifest(self) -> list:
        """Code files merged by path. A later ``code`` event replaces an earlier one."""
        merged: dict = {}
        order: list = []
        for event in self.events():
            if event.get("kind") != "code":
                continue
            manifest = event.get("manifest") or []
            if not isinstance(manifest, list):
                continue
            for entry in manifest:
                if not isinstance(entry, dict):
                    continue
                path = entry.get("path")
                if not isinstance(path, str):
                    continue
                if path not in merged:
                    order.append(path)
                merged[path] = dict(entry)
        return [merged[path] for path in order]

    def artifacts(self) -> list:
        """Artifact events in append order, including ``version`` and ``sha256``."""
        return [event for event in self.events() if event.get("kind") == "artifact"]

    def referenced_shas(self) -> set[str]:
        """All referenced objects; unreadable input raises rather than losing refs."""
        return set().union(*run_references(self.root, self.run_id).values())

    def media(self, key: str | None = None):
        """The media index, or the list of rows for ``key`` (empty if unknown)."""
        path = self._c(self.run_dir / "media" / INDEX_NAME)
        try:
            raw = read_file_nofollow(path)
            data = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            data = {}
        except OSError as exc:
            if is_symlink_error(exc) or path.is_symlink():
                logger.warning("refusing symlinked media index: %s", path)
                data = {}
            else:
                raise
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"corrupt media index for {self.run_id}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"corrupt media index for {self.run_id}")
        if key is None:
            return {name: rows for name, rows in data.items() if isinstance(rows, list)}
        rows = data.get(key, [])
        if not isinstance(rows, list):
            return []
        return rows

    def time_index(self) -> list:
        """``(step, seconds_since_created_at)`` from ``_time.bin``, torn tail dropped."""
        path = self._c(self.run_dir / SCALARS_DIRNAME / TIME_BIN_NAME)
        try:
            data = read_file_nofollow(path)
        except FileNotFoundError:
            return []
        except OSError as exc:
            if is_symlink_error(exc) or path.is_symlink():
                logger.warning("refusing symlinked time file: %s", path)
                return []
            raise
        usable = len(data) - (len(data) % RECORD_SIZE)
        if not usable:
            return []
        return list(RECORD_STRUCT.iter_unpack(data[:usable]))

    def read_records(self, key: str, offset: int = 0, limit: int | None = None) -> tuple:
        """Raw records from a byte offset, for a sync cursor.

        ``offset`` is a non-negative multiple of 8. The returned offset is the
        first byte not consumed (a multiple of 8, at or before the file size).
        A partial tail is not consumed, so the next call can resume when the
        writer finishes that record. Unknown keys raise ``KeyError``.
        """
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if offset % RECORD_SIZE != 0:
            raise ValueError("offset must be a multiple of 8")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer")
        if limit == 0:
            return [], offset
        info = self._series_info(key)
        path = self._bin_path(_series_id(info))
        size = _file_size(path)
        complete = size - (size % RECORD_SIZE)
        if offset >= complete:
            return [], offset
        to_read = complete - offset
        if limit is not None:
            to_read = min(to_read, limit * RECORD_SIZE)
        try:
            with open_nofollow(path) as handle:
                handle.seek(offset)
                data = handle.read(to_read)
        except OSError as exc:
            if is_symlink_error(exc) or path.is_symlink():
                logger.warning("refusing symlinked series file: %s", path)
                return [], offset
            raise
        usable = len(data) - (len(data) % RECORD_SIZE)
        if not usable:
            return [], offset
        return list(RECORD_STRUCT.iter_unpack(data[:usable])), offset + usable

    def series(self, key: str, *, max_points: int = 1000, start=None, end=None) -> dict:
        """Points for ``key``, optionally clipped to inclusive ``[start, end]``.

        When the selected records number at most ``max_points``, ``mode`` is
        ``raw`` and ``points`` is ``[[step, value], ...]`` in file order.
        Otherwise ``mode`` is ``overview`` and each point is
        ``[step_min, step_max, vmin, vmax, mean, last]``, downsampled to at
        most ``max_points`` buckets. ``total`` counts every selected record,
        including non-finite values; ``nonfinite`` counts those.

        On a finished or crashed run a valid ``.ovw`` answers an overview
        query when its total matches the ``.bin`` record count. A missing
        ``.bin`` is served from that ``.ovw`` for every query, raw-sized and
        ranged included, with ``mode`` ``overview``. A present ``.bin`` whose
        record count disagrees, or an overview that does not parse, is not
        used: this logs a warning and rebuilds from ``.bin``. If ``.bin`` is
        missing too, the result is empty. Safe to call from several threads
        on this reader.
        """
        if isinstance(max_points, bool) or not isinstance(max_points, int) or max_points < 1:
            raise ValueError("max_points must be an integer >= 1")
        start = _bound(start)
        end = _bound(end)
        with self._lock:
            return self._series_locked(key, max_points, start, end)

    def _series_locked(self, key: str, max_points: int, start, end) -> dict:
        if start is not None and end is not None and start > end:
            return _empty(key)
        series_id = self._cached_series_id(key)
        path = self._bin_path(series_id)
        # Stat the prefix before reading monotonic. The writer clears the
        # flag before it appends an out-of-order record, so a flag that is
        # still true applies to this size.
        size = _file_size(path)
        if not path.is_file() or path.is_symlink():
            # sync --prune deletes the series file after finish. The overview
            # is then the whole answer, including queries that would have
            # been raw or a step range.
            overview = self._authoritative_overview(key, series_id, None, -1)
            if overview is None:
                return _empty(key)
            return _overview_result(key, overview, max_points)
        monotonic = bool(self._series_info(key).get("monotonic", False))
        count = (size - (size % RECORD_SIZE)) // RECORD_SIZE
        if count == 0:
            self._authoritative_overview(key, series_id, 0, size)
            return _empty(key)
        overview = self._authoritative_overview(key, series_id, count, size)

        covers_all = (start is None or start == 0) and end is None
        if not covers_all and monotonic and count > 0:
            try:
                with open_nofollow(path) as handle:
                    fd = handle.fileno()
                    first_step = _step_at(fd, 0)
                    last_step = _step_at(fd, count - 1)
                    if (start is None or start <= first_step) and (end is None or end >= last_step):
                        covers_all = True
            except FileNotFoundError:
                overview = self._authoritative_overview(key, series_id, None, -1)
                if overview is None:
                    return _empty(key)
                return _overview_result(key, overview, max_points)
            except OSError:
                pass

        if covers_all:
            try:
                if count <= max_points:
                    return _raw_result(key, _records_from(_read_bytes(path, 0, count)))
                if overview is not None:
                    return _overview_result(key, overview, max_points)
                return _overview_result(key, self._incremental(key, path), max_points)
            except FileNotFoundError:
                overview = self._authoritative_overview(key, series_id, None, -1)
                if overview is None:
                    return _empty(key)
                return _overview_result(key, overview, max_points)

        if monotonic:
            try:
                lo, hi = _bounds(path, count, start, end)
                if hi - lo <= max_points:
                    return _raw_result(key, _records_from(_read_bytes(path, lo, hi)))
                overview = Overview()
                blob = _read_bytes(path, lo, hi)
                if blob:
                    overview.add_many(RECORD_STRUCT.iter_unpack(blob))
                return _overview_result(key, overview, max_points)
            except FileNotFoundError:
                overview = self._authoritative_overview(key, series_id, None, -1)
                if overview is None:
                    return _empty(key)
                return _overview_result(key, overview, max_points)
        try:
            return self._scan(key, path, count, max_points, start, end)
        except FileNotFoundError:
            overview = self._authoritative_overview(key, series_id, None, -1)
            if overview is None:
                return _empty(key)
            return _overview_result(key, overview, max_points)

    def _scan(self, key: str, path: Path, count: int, max_points: int, start, end) -> dict:
        # Non-monotonic: step order is not file order, so bisect would lie.
        data = _read_bytes(path, 0, count)
        kept = []
        overview = Overview()
        total = 0
        for step, value in RECORD_STRUCT.iter_unpack(data):
            if start is not None and step < start:
                continue
            if end is not None and step > end:
                continue
            total += 1
            if total <= max_points:
                kept.append((step, value))
            overview.add(step, value)
        if total <= max_points:
            return _raw_result(key, kept)
        return _overview_result(key, overview, max_points)

    def _terminal_token(self, ovw_path: Path, bin_size: int) -> tuple:
        """``(status, ovw size, ovw mtime_ns, bin size)``. Missing files use -1."""
        try:
            if self._meta_path.is_symlink():
                meta_sig = (-1, -1)
            else:
                meta_stat = self._meta_path.stat()
                meta_sig = (meta_stat.st_mtime_ns, meta_stat.st_size)
        except OSError:
            meta_sig = (-1, -1)
        if meta_sig != self._meta_sig:
            status = self.meta().get("status")
            if not isinstance(status, str):
                status = ""
            self._meta_sig = meta_sig
            self._status = status
        try:
            ovw_stat = os.lstat(ovw_path)
            if stat.S_ISLNK(ovw_stat.st_mode):
                logger.warning("refusing symlinked overview: %s", ovw_path)
                ovw_size = -1
                ovw_mtime = -1
            else:
                ovw_size = ovw_stat.st_size
                ovw_mtime = ovw_stat.st_mtime_ns
        except OSError:
            ovw_size = -1
            ovw_mtime = -1
        return (self._status, ovw_size, ovw_mtime, bin_size)

    def _authoritative_overview(
        self, key: str, series_id: int, bin_count: int | None, bin_size: int
    ) -> Overview | None:
        """``.ovw`` to trust for a finished or crashed run, or None.

        ``bin_count`` is None when the series file is absent. The cache key is
        ``(status, .ovw size, .ovw mtime_ns, .bin size)``; any change drops the
        entry. A present file whose complete-record count differs from the
        overview total is not used; a warning is logged once per key and the
        caller rebuilds from ``.bin``. A running run, or a missing or
        unreadable overview, returns None. An unreadable overview with no
        ``.bin`` makes ``series`` return an empty result.
        """
        ovw_path = self._c(self.run_dir / SCALARS_DIRNAME / f"{series_id}.ovw")
        token = self._terminal_token(ovw_path, bin_size)
        decision = self._ovw_decision.get(key)
        if decision is not None and decision[0] == token:
            return decision[1]
        if token[1] < 0 or token[0] not in _DONE:
            self._ovw_decision[key] = (token, None)
            return None
        try:
            blob = read_file_nofollow(ovw_path)
            overview = Overview.from_bytes(blob)
        except (ValueError, struct.error, OSError) as exc:
            logger.warning("ignoring unreadable overview %s (%s)", ovw_path, exc)
            self._ovw_decision[key] = (token, None)
            return None
        if bin_count is None or overview.point_count == bin_count:
            self._ovw_decision[key] = (token, overview)
            return overview
        if key not in self._mismatch_warned:
            self._mismatch_warned.add(key)
            logger.warning(
                "overview %s has %s points but %s has %s records; rebuilding from .bin",
                ovw_path.name,
                overview.point_count,
                f"{series_id}.bin",
                bin_count,
            )
        self._ovw_decision[key] = (token, None)
        return None

    def _incremental(self, key: str, path: Path) -> Overview:
        size = _file_size(path)
        complete = size - (size % RECORD_SIZE)
        cached = self._cache.get(key)
        if cached and cached[0] == "inc" and cached[2] <= complete:
            overview = cached[1]
            offset = cached[2]
        else:
            overview = Overview()
            offset = 0
        if complete > offset:
            try:
                with open_nofollow(path) as handle:
                    handle.seek(offset)
                    data = handle.read(complete - offset)
            except OSError as exc:
                if is_symlink_error(exc) or path.is_symlink():
                    logger.warning("refusing symlinked series file: %s", path)
                    data = b""
                else:
                    raise
            usable = len(data) - (len(data) % RECORD_SIZE)
            if usable:
                overview.add_many(RECORD_STRUCT.iter_unpack(data[:usable]))
            offset += usable
        self._cache[key] = ("inc", overview, offset)
        return overview

    def _cached_series_id(self, key: str) -> int:
        series_id = self._series_ids.get(key)
        if series_id is None:
            series_id = _series_id(self._series_info(key))
            self._series_ids[key] = series_id
        return series_id

    def _index(self) -> dict:
        path = self._c(self.run_dir / SCALARS_DIRNAME / INDEX_NAME)
        with self._lock:
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                self._index_cache = None
                return {"version": 1, "series": {}}
            except OSError as exc:
                if is_symlink_error(exc) or path.is_symlink():
                    logger.warning("refusing symlinked scalars index: %s", path)
                    raise ValueError(f"corrupt scalars index for {self.run_id}") from exc
                raise
            if stat.S_ISLNK(st.st_mode):
                logger.warning("refusing symlinked scalars index: %s", path)
                raise ValueError(f"corrupt scalars index for {self.run_id}")

            sig = (st.st_size, st.st_mtime_ns, st.st_ino)
            if self._index_cache is not None and self._index_cache[0] == sig:
                return self._index_cache[1]

            try:
                raw = read_file_nofollow(path)
                data = json.loads(raw.decode("utf-8"))
            except FileNotFoundError:
                self._index_cache = None
                return {"version": 1, "series": {}}
            except OSError as exc:
                if is_symlink_error(exc) or path.is_symlink():
                    logger.warning("refusing symlinked scalars index: %s", path)
                    raise ValueError(f"corrupt scalars index for {self.run_id}") from exc
                raise
            except (ValueError, RecursionError) as exc:
                raise ValueError(f"corrupt scalars index for {self.run_id}") from exc
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError(f"unsupported scalars index for {self.run_id}")
            if not isinstance(data.get("series"), dict):
                raise ValueError(f"corrupt scalars index for {self.run_id}")
            self._index_cache = (sig, data)
            return data

    def _series_info(self, key: str) -> dict:
        if not isinstance(key, str):
            raise KeyError(key)
        info = (self._index().get("series") or {}).get(key)
        if not isinstance(info, dict):
            raise KeyError(key)
        return info

    def _bin_path(self, series_id: int) -> Path:
        return self._c(self.run_dir / SCALARS_DIRNAME / f"{series_id}.bin")

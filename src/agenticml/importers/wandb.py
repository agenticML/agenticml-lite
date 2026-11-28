"""Import local Weights & Biases run directories (``wandb/run-*/run-*.wandb``).

PATH is a ``wandb/`` directory (every ``run-*`` / ``offline-run-*`` child is
one run), one run directory, a ``.wandb`` file, or a project directory that
holds a ``wandb/`` directory. The data is the ``run-<id>.wandb`` file: a 7
byte W&B header followed by LevelDB log blocks (see :mod:`._leveldb_log`)
whose records are protobuf ``Record`` messages. Nothing is written to the
source and no ``wandb`` package is needed.

Field numbers come from ``wandb/proto/wandb_internal.proto`` and
``wandb_base.proto`` (MIT licence), read from the descriptors that wandb
0.31.2 ships and checked against files that version wrote; the layout has been
stable since wandb 0.12 (older runs wrote ``key`` + a JSON ``value_json``
where 0.3x writes ``nested_key`` paths; both are read):

* ``Record``: ``history`` 2, ``summary`` 3, ``config`` 5, ``stats`` 7,
  ``run`` 17, ``exit`` 18, ``header`` 21, ``environment`` 26.
* ``RunRecord``: ``run_id`` 1, ``entity`` 2, ``project`` 3, ``config`` 4,
  ``summary`` 5, ``display_name`` 8, ``notes`` 9, ``tags`` 10,
  ``host`` 13, ``start_time`` 17 (``Timestamp``: ``seconds`` 1, ``nanos`` 2).
* ``ConfigRecord`` / ``SummaryRecord``: ``update`` 1 and ``remove`` 2, each a
  repeated item (``key`` 1, ``nested_key`` 2 repeated, ``value_json`` 16,
  and the typed ``boolean`` 4, ``integer`` 5, ``number`` 6, ``text`` 7,
  ``json`` 8 for history items).
* ``HistoryRecord``: ``item`` 1 (repeated), ``step`` 2 (``num`` 1),
  ``relative_time_ms`` 3.
* ``RunExitRecord``: ``exit_code`` 1, ``runtime`` 2, ``not_complete`` 3.
* ``HeaderRecord``: ``version_info`` 1 (``producer`` 1).
* ``EnvironmentRecord``: ``started_at`` 3 (used when the run record has no start).

A path that does not exist on disk and looks like ``ENTITY/PROJECT[/RUN_ID]``
is handed to :mod:`agenticml.importers.wandb_cloud` (package 41.6).
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import os
import re
import stat
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from . import ImportedRun, ImportOptions, ImportSourceError
from . import _leveldb_log as leveldb
from . import _protobuf as pb
from ._media import sniff_image_type

logger = logging.getLogger("agenticml.import")

SOURCE = "wandb"
CLOUD_MODULE = "agenticml.importers.wandb_cloud"
_CLOUD_PATH = re.compile(r"[\w.-]+/[\w.-]+(/[\w.-]+)?")

# Record (oneof record_type) field numbers.
_HISTORY, _SUMMARY, _CONFIG, _STATS, _RUN, _EXIT, _HEADER, _ENVIRONMENT = 2, 3, 5, 7, 17, 18, 21, 26

MAX_MEDIA_BYTES = 64 * 1024 * 1024
_MAX_PATH_DEPTH = 32
_MAX_CONFIG_BYTES = 16 * 1024 * 1024  # one value_json bigger than this is not parsed
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MISSING = object()


class WandbFormatError(ValueError):
    """A ``.wandb`` file this reader does not understand (the run fails, nothing is imported)."""


# ---------------------------------------------------------------------------
# discovery


def _is_cloud_path(path: str) -> bool:
    """``ENTITY/PROJECT[/RUN_ID]``; ``./a/b`` and ``../a/b`` are relative file paths, not cloud paths."""
    return bool(_CLOUD_PATH.fullmatch(path)) and not any(part in (".", "..") for part in path.split("/"))


def discover(path: str, options: ImportOptions) -> Iterator[ImportedRun]:
    """Yield one :class:`ImportedRun` per W&B run under ``path`` (or the cloud importer's runs)."""
    if not os.path.exists(path):
        if _is_cloud_path(path):
            try:
                module = importlib.import_module(CLOUD_MODULE)
            except ModuleNotFoundError as exc:
                if exc.name == CLOUD_MODULE:
                    raise ImportSourceError("W&B cloud import is not available in this version") from None
                raise
            return module.discover(path, options)
        raise ImportSourceError(
            f"{path} does not exist (expected a local wandb/ directory or run directory, "
            "or ENTITY/PROJECT[/RUN_ID] for W&B cloud)"
        )
    runs = _find_run_files(Path(path))
    if not runs:
        raise ImportSourceError(
            f"no run-*/run-*.wandb files under {path} "
            "(expected a wandb/ directory, one run directory, or a .wandb file)"
        )
    return _discover_local(runs, options)


def _is_regular(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _wandb_files(directory: Path) -> list[Path]:
    try:
        return sorted(p for p in directory.glob("*.wandb") if _is_regular(p))
    except OSError:
        return []


def _run_children(directory: Path) -> list[tuple[Path, Path]]:
    found: list[tuple[Path, Path]] = []
    try:
        children = sorted(directory.iterdir())
    except OSError:
        return found
    for child in children:
        # "latest-run" and other symlinks point at a run that is listed under its own name.
        if child.is_symlink() or not child.name.startswith(("run-", "offline-run-")):
            continue
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        for file in _wandb_files(child):
            found.append((child, file))
    return found


def _find_run_files(path: Path) -> list[tuple[Path, Path]]:
    """``(run directory, .wandb file)`` pairs under ``path``, oldest name first."""
    if path.is_file():
        return [(path.parent, path)] if path.suffix == ".wandb" else []
    direct = _wandb_files(path)
    if direct:
        return [(path, file) for file in direct]
    children = _run_children(path)
    if children:
        return children
    nested = path / "wandb"
    if nested.is_dir() and not nested.is_symlink():
        return _run_children(nested)
    return []


class _Segments:
    """Tells the first run directory of a W&B run id from the later ones (``resume=``).

    A resumed run writes a new ``run-<timestamp>-<id>`` directory with the same
    run id. Segments are NOT merged: each directory is its own AgenticML run,
    and every segment after the first (oldest directory name first) gets
    ``source_run_id`` ``<run id>@<directory name>`` so the ids stay unique and
    a later segment is not reported as "already imported". The first segment
    keeps the plain run id. The sibling directories are looked at, so the
    result does not depend on whether the whole ``wandb/`` directory or only
    one run directory was given.
    """

    def __init__(self) -> None:
        self._parents: dict[Path, dict[str, list[str]]] = {}
        self.seen: set[str] = set()

    def _names(self, parent: Path) -> dict[str, list[str]]:
        index = self._parents.get(parent)
        if index is None:
            index = {}
            try:
                children = list(parent.iterdir())
            except OSError:
                children = []
            for child in children:
                if child.name.startswith(("run-", "offline-run-")) and not child.is_symlink():
                    index.setdefault(child.name.rsplit("-", 1)[-1], []).append(child.name)
            self._parents[parent] = index
        return index

    def source_run_id(self, run_dir: Path, run_id: str) -> str:
        suffix = "-" + run_id
        candidates = [
            name for name in self._names(run_dir.parent).get(run_id.rsplit("-", 1)[-1], ()) if name.endswith(suffix)
        ]
        unique = run_id
        if run_dir.name in candidates and run_dir.name != min(candidates, key=_segment_order):
            unique = f"{run_id}@{run_dir.name}"
        # Runs in odd directory names (or two .wandb files in one directory) still must not collide.
        while unique in self.seen:
            unique += "+"
        self.seen.add(unique)
        return unique


def _segment_order(name: str) -> tuple[str, str]:
    return re.sub(r"^(?:offline-)?run-", "", name), name


def _discover_local(runs: list[tuple[Path, Path]], options: ImportOptions) -> Iterator[ImportedRun]:
    segments = _Segments()
    for run_dir, file in runs:
        yield _load_run(run_dir, file, options, segments)


# ---------------------------------------------------------------------------
# reading records


def _records(file: Path, options: ImportOptions | None) -> Iterator[bytes]:
    on_skip = options.warn if options is not None else None
    with open(file, "rb") as fp:
        leveldb.check_file_header(fp)
        yield from leveldb.read_records(fp, on_skip=on_skip)


def _text(raw) -> str:
    return pb.as_str(raw)


def _timestamp(message: pb.Message, raw) -> float | None:
    stamp = message.child(raw)
    seconds = pb.as_int64(stamp.first(1, 0))
    nanos = pb.as_int32(stamp.first(2, 0))
    value = seconds + nanos / 1e9
    return value if math.isfinite(value) else None


def _json(text: str, lists: bool = True):
    """Parse a JSON value; ``_MISSING`` when it is not valid or too large.

    With ``lists=False`` a top-level list is not parsed (history lists, such as
    histogram bins, are never imported, so decoding them is wasted work).
    """
    if not lists and text.lstrip().startswith("["):
        return []
    if len(text) > _MAX_CONFIG_BYTES:
        return _MISSING
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return _MISSING


def _item_path(item: pb.Message) -> list[str]:
    nested = item.get(2)
    if nested:
        return [_text(part) for part in nested]
    key = item.first(1)
    return [_text(key)] if key is not None else []


def _item_value(item: pb.Message, lists: bool = True):
    raw = item.first(16)
    if raw is not None:
        return _json(_text(raw), lists)
    # Typed history items (very old clients): boolean 4, integer 5 (sint64), number 6, text 7, json 8.
    if 6 in item:
        return pb.as_double(item.last(6))
    if 5 in item:
        zigzag = pb._varint_value(item.last(5))
        return (zigzag >> 1) ^ -(zigzag & 1)
    if 4 in item:
        return pb.as_bool(item.last(4))
    if 7 in item:
        return _text(item.last(7))
    if 8 in item:
        return _json(_text(item.last(8)), lists)
    return _MISSING


def _set_path(tree: dict, path: list[str], value) -> None:
    if not path or len(path) > _MAX_PATH_DEPTH:
        return
    node = tree
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def _remove_path(tree: dict, path: list[str]) -> None:
    if not path or len(path) > _MAX_PATH_DEPTH:
        return
    node = tree
    for part in path[:-1]:
        node = node.get(part)
        if not isinstance(node, dict):
            return
    node.pop(path[-1], None)


def _apply_update_record(message: pb.Message, raw, tree: dict) -> None:
    """Apply a ``ConfigRecord`` / ``SummaryRecord`` (update 1, remove 2) to ``tree``."""
    record = message.child(raw)
    for raw_item in record.get(1, ()):
        item = record.child(raw_item)
        value = _item_value(item)
        if value is not _MISSING:
            _set_path(tree, _item_path(item), value)
    for raw_item in record.get(2, ()):
        _remove_path(tree, _item_path(record.child(raw_item)))


class _Meta:
    """Everything one pass over the record stream learns about a run, except history."""

    def __init__(self) -> None:
        self.version: str | None = None
        self.run_id = ""
        # Parsed but deliberately not stored: ImportedRun and the run meta have no field for a W&B
        # entity, and config / tags / notes are the user's own data (accepted deviation, see
        # test_entity_is_read_but_not_stored). Run record fields 6 (run_group) and 7 (job_type)
        # stay unread, as group and job_type stay null by the 41.1 rule.
        self.entity = ""
        self.project = ""
        self.name = ""
        self.notes = ""
        self.host = ""
        self.tags: list[str] = []
        self.start: float | None = None
        self.env_start: float | None = None
        self.config: dict = {}
        self.summary: dict = {}
        self.has_run = False
        self.exit_code: int | None = None
        self.exit_runtime = 0
        self.not_complete = False
        self.has_exit = False
        self.stats = 0
        self.history = 0
        self.last_history: bytes | None = None


def _scan_meta(file: Path, options: ImportOptions, meta: _Meta) -> None:
    for raw in _records(file, options):
        message = pb.decode(raw)
        if _HISTORY in message:
            meta.history += 1
            meta.last_history = bytes(message[_HISTORY][-1])
        elif _SUMMARY in message:
            _apply_update_record(message, message[_SUMMARY][-1], meta.summary)
        elif _CONFIG in message:
            _apply_update_record(message, message[_CONFIG][-1], meta.config)
        elif _STATS in message:
            meta.stats += 1
        elif _RUN in message:
            _read_run_record(message, message[_RUN][-1], meta)
        elif _EXIT in message:
            record = message.child(message[_EXIT][-1])
            meta.has_exit = True
            meta.exit_code = pb.as_int32(record.first(1, 0))
            meta.exit_runtime = pb.as_int32(record.first(2, 0))
            meta.not_complete = pb.as_bool(record.first(3, 0))
        elif _HEADER in message:
            header = message.child(message[_HEADER][-1])
            info = header.first(1)
            if info is not None:
                producer = header.child(info).first(1)
                if producer is not None:
                    meta.version = _text(producer).split("+", 1)[0]
        elif _ENVIRONMENT in message:
            environment = message.child(message[_ENVIRONMENT][-1])
            started = environment.first(3)
            if started is not None and meta.env_start is None:
                meta.env_start = _timestamp(environment, started)


def _read_run_record(message: pb.Message, raw, meta: _Meta) -> None:
    record = message.child(raw)
    meta.has_run = True
    # A resumed run repeats the run record; keep the first value of each field.
    for field, attr in ((1, "run_id"), (2, "entity"), (3, "project"), (8, "name"), (9, "notes"), (13, "host")):
        value = record.first(field)
        if value is not None and not getattr(meta, attr):
            setattr(meta, attr, _text(value))
    if not meta.tags and 10 in record:
        meta.tags = [_text(tag) for tag in record[10]]
    start = record.first(17)
    if start is not None and meta.start is None:
        meta.start = _timestamp(record, start)
    for field, tree in ((4, meta.config), (5, meta.summary)):
        value = record.first(field)
        if value is not None:
            _apply_update_record(record, value, tree)


# ---------------------------------------------------------------------------
# history


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value)
    except OverflowError:
        return None


class _Stream:
    """History of one run: scalar points, then image references, then the images."""

    def __init__(
        self, file: Path, run_dir: Path, name: str, options: ImportOptions, meta: _Meta
    ) -> None:
        self.file = file
        self.run_dir = run_dir
        self.name = name
        self.options = options
        self.meta = meta
        # key -> (sequence, wall, step, relative path, caption); thinned like the writer does.
        self.refs: dict[str, list[tuple[int, float, int, str, str | None]]] = {}
        self.ref_count = 0
        self.skipped: Counter[str] = Counter()
        self.done = False
        self.media_missing = 0

    # -- points

    def points(self) -> Iterator[tuple[float, int, dict[str, float]]]:
        yield from self._scan()

    def _scan(self) -> Iterator[tuple[float, int, dict[str, float]]]:
        """Second pass over the file; whatever the first pass could not see fails the run the same way."""
        try:
            yield from self._scan_records()
        except (pb.ProtobufError, ValueError, RecursionError, leveldb.LevelDBLogError) as exc:
            logger.debug("cannot read %s", self.file, exc_info=True)
            raise WandbFormatError(
                f"unsupported .wandb format (wandb {_wandb_version(self.run_dir, self.meta)}): {exc}"
            ) from exc

    def _scan_records(self) -> Iterator[tuple[float, int, dict[str, float]]]:
        self.refs = {}
        self.ref_count = 0
        self.skipped = Counter()
        start = self.meta.start
        last_wall = start if start is not None else 0.0
        for raw in _records(self.file, self.options):
            message = pb.decode(raw)
            if _HISTORY not in message:
                continue
            record = message.child(message[_HISTORY][-1])
            tree: dict = {}
            for raw_item in record.get(1, ()):
                item = record.child(raw_item)
                value = _item_value(item, lists=False)
                if value is not _MISSING:
                    _set_path(tree, _item_path(item), value)
            wall = self._wall(record, tree, last_wall)
            last_wall = wall
            step = self._step(record, tree)
            metrics: dict[str, float] = {}
            for key, value in tree.items():
                if not key.startswith("_"):
                    self._walk(key, value, metrics, wall, step)
            if metrics:
                yield wall, step, metrics
        self.done = True
        for kind, count in sorted(self.skipped.items()):
            self.options.warn(
                f"{self.name}: skipped {count} {kind} values (only scalars and images are imported)"
            )
        if self.meta.stats:
            self.options.warn(
                f"{self.name}: skipped {self.meta.stats} system-metrics records "
                "(not imported in this version)"
            )

    def _wall(self, record: pb.Message, tree: dict, previous: float) -> float:
        wall = _number(tree.get("_timestamp"))
        if wall is not None and math.isfinite(wall):
            return wall
        relative = record.first(3)
        if relative is not None and self.meta.start is not None:
            return self.meta.start + pb.as_int64(relative) / 1000.0
        return previous

    @staticmethod
    def _step(record: pb.Message, tree: dict) -> int:
        value = tree.get("_step")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        step = record.first(2)
        if step is not None:
            return pb.as_int64(record.child(step).first(1, 0))
        return 0

    def _walk(self, key: str, value, metrics: dict[str, float], wall: float, step: int) -> None:
        number = _number(value)
        if number is not None:
            metrics[key] = number
        elif isinstance(value, dict):
            kind = value.get("_type")
            if kind is None:
                for sub, inner in value.items():
                    self._walk(f"{key}.{sub}", inner, metrics, wall, step)
            elif kind == "image-file" and isinstance(value.get("path"), str):
                caption = value.get("caption")
                self._add_ref(key, wall, step, value["path"], caption if isinstance(caption, str) else None)
            else:
                self.skipped[str(kind)] += 1

    # -- media

    def _add_ref(self, key: str, wall: float, step: int, rel: str, caption: str | None) -> None:
        """Keep at most the writer's per-key limit of image references (same thinning as ``add_media``)."""
        rows = self.refs.setdefault(key, [])
        rows.append((self.ref_count, wall, step, rel, caption))
        self.ref_count += 1
        if len(rows) > _media_limit():
            thinned = rows[::2]
            if thinned[-1] is not rows[-1]:
                thinned.append(rows[-1])
            rows[:] = thinned

    def media(self) -> Iterator[tuple[float, int, str, bytes, str, str | None]]:
        if not self.done:
            for _ in self._scan():
                pass
        base = self.run_dir.resolve()
        missing = 0
        ordered = sorted(
            ((seq, wall, step, key, rel, caption) for key, rows in self.refs.items() for seq, wall, step, rel, caption in rows)
        )
        for _seq, wall, step, key, rel, caption in ordered:
            found = _read_media(base, rel)
            if found is None:
                missing += 1
                continue
            data, kind = found
            yield wall, step, key, data, kind, caption
        if missing:
            self.options.warn(
                f"{self.name}: skipped {missing} images that were missing, too large, "
                "not images, or outside the run directory"
            )


def _media_limit() -> int:
    """The writer's per-key image limit (``AGENTICML_MEDIA_MAX_PER_KEY``, default 64)."""
    try:
        from ..store.writer import MEDIA_MAX_PER_KEY, _media_limit as limit

        return limit("AGENTICML_MEDIA_MAX_PER_KEY", MEDIA_MAX_PER_KEY)
    except ImportError:  # pragma: no cover - the store is part of this package
        return 64


def _contained_file(base: Path, rel: str) -> Path | None:
    """``base/files/<rel>`` resolved, when it is a regular file inside ``base`` (the resolved run directory)."""
    if not rel or "\x00" in rel:
        return None
    try:
        target = (base / "files" / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return target


def _read_media(base: Path, rel: str) -> tuple[bytes, str] | None:
    """``(bytes, content type)`` of image ``files/<rel>`` when it is a regular image file inside ``base``.

    The first 12 bytes are sniffed before the rest is read, and the size cap comes from ``fstat``,
    so a large file that is not an image is never read in full.
    """
    target = _contained_file(base, rel)
    if target is None:
        return None
    try:
        # O_NONBLOCK: a FIFO swapped in after the check must not hang the open.
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as fp:
            info = os.fstat(fp.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MEDIA_BYTES:
                return None
            head = fp.read(12)
            kind = sniff_image_type(head)
            if kind is None:
                return None
            data = head + fp.read(MAX_MEDIA_BYTES + 1 - len(head))
    except OSError:
        return None
    return (data, kind) if len(data) <= MAX_MEDIA_BYTES else None


# ---------------------------------------------------------------------------
# one run


def _wandb_version(run_dir: Path, meta: _Meta | None) -> str:
    """Best effort W&B version: the header record, else ``files/wandb-metadata.json``."""
    if meta is not None and meta.version:
        return meta.version
    try:
        # Same containment rules as media: a regular file inside the run directory (this runs on the
        # failure path, where hostile files end up; a FIFO here would hang the import).
        target = _contained_file(run_dir.resolve(), "wandb-metadata.json")
        if target is None:
            return "unknown"
        with open(target, "rb") as fp:
            data = json.loads(fp.read(1024 * 1024))
        for key in ("wandb_version", "cli_version", "wandb"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value:
                return value
    except (OSError, ValueError, RecursionError, AttributeError):
        pass
    return "unknown"


def _file_run_id(file: Path) -> str:
    stem = file.stem
    return stem[4:] if stem.startswith("run-") and len(stem) > 4 else stem


def _failed_run(
    location: str, run_dir: Path, file: Path, reason: str, version: str, *, unreadable: bool = False
) -> ImportedRun:
    """A run whose points raise, so the core reports it as one failed run and writes nothing.

    ``unreadable``: the file could not be read at all (permissions, I/O), so nothing is said about
    its format.
    """
    if unreadable:
        error: Exception = OSError(reason)
    else:
        error = WandbFormatError(f"unsupported .wandb format (wandb {version})" + (f": {reason}" if reason else ""))

    def points():
        raise error
        yield  # pragma: no cover - makes this a generator

    return ImportedRun(
        source=SOURCE,
        location=location,
        source_run_id=_file_run_id(file),
        name=run_dir.name,
        project="",
        started_at=_EPOCH,
        ended_at=None,
        status="finished",
        source_status=None,
        config={},
        summary={},
        tags=[],
        points=points(),
        media=iter(()),
    )


_MAX_SUMMARY_DEPTH = 32


def _plain_summary(tree: dict) -> tuple[dict, int]:
    """Numeric and string summary values, without W&B's own top-level ``_`` keys.

    Nested dicts are flattened with ``.`` joins, the same rule as history keys. Returns the summary
    and the number of values left out (typed objects such as histograms, lists, too deep dicts).
    """
    out: dict = {}
    skipped = 0

    def walk(key: str, value, depth: int) -> None:
        nonlocal skipped
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float, str)):
            out[key] = value
        elif isinstance(value, dict) and "_type" not in value and depth < _MAX_SUMMARY_DEPTH:
            for sub, inner in value.items():
                walk(f"{key}.{sub}", inner, depth + 1)
        else:
            skipped += 1

    for key, value in tree.items():
        if not key.startswith("_"):
            walk(key, value, 0)
    return out, skipped


def _history_wall(raw: bytes | None) -> float | None:
    if raw is None:
        return None
    try:
        record = pb.decode(raw)
        for raw_item in record.get(1, ()):
            item = record.child(raw_item)
            if _item_path(item) == ["_timestamp"]:
                return _number(_item_value(item, lists=False))
    except (pb.ProtobufError, ValueError):
        return None
    return None


def _load_run(
    run_dir: Path, file: Path, options: ImportOptions, segments: _Segments | None = None
) -> ImportedRun:
    try:
        location = str(run_dir.resolve().parent)
    except OSError:
        location = str(run_dir.parent)
    meta = _Meta()
    try:
        _scan_meta(file, options, meta)
    except OSError as exc:
        return _failed_run(
            location, run_dir, file, f"cannot read {file.name}: {exc.strerror or exc}", "unknown", unreadable=True
        )
    except (leveldb.LevelDBLogError, pb.ProtobufError, ValueError, RecursionError) as exc:
        logger.debug("cannot read %s", file, exc_info=True)
        return _failed_run(location, run_dir, file, str(exc), _wandb_version(run_dir, meta))
    if not meta.has_run or not meta.run_id:
        return _failed_run(location, run_dir, file, "no run record", _wandb_version(run_dir, meta))
    start = meta.start if meta.start else meta.env_start
    if start is None:
        return _failed_run(location, run_dir, file, "no start time", _wandb_version(run_dir, meta))
    try:
        started = datetime.fromtimestamp(start, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return _failed_run(location, run_dir, file, "bad start time", _wandb_version(run_dir, meta))
    meta.start = start

    if not meta.has_exit:
        status, source_status, ended_at = "running", "running", None
    else:
        crashed = bool(meta.exit_code) or meta.not_complete
        status = "crashed" if crashed else "finished"
        source_status = f"failed (exit code {meta.exit_code})" if crashed else "finished"
        candidates = [t for t in (start + meta.exit_runtime if meta.exit_runtime > 0 else None, _history_wall(meta.last_history)) if t is not None]
        ended_at = None
        if candidates:
            try:
                ended_at = datetime.fromtimestamp(max(candidates), timezone.utc)
            except (OverflowError, OSError, ValueError):
                ended_at = None

    config = {key: value for key, value in meta.config.items() if key != "_wandb"}
    name = meta.name or meta.run_id
    stream = _Stream(file, run_dir, name, options, meta)
    summary, skipped_summary = _plain_summary(meta.summary)
    if skipped_summary:
        options.warn(
            f"{name}: skipped {skipped_summary} summary values (only numbers and text are imported)"
        )
    source_run_id = (segments or _Segments()).source_run_id(run_dir, meta.run_id)
    return ImportedRun(
        source=SOURCE,
        location=location,
        source_run_id=source_run_id,
        name=name,
        project=meta.project or "wandb",
        started_at=started,
        ended_at=ended_at,
        status=status,
        source_status=source_status,
        config=config,
        summary=summary,
        tags=list(meta.tags),
        points=stream.points(),
        media=stream.media(),
        notes=meta.notes,
        hostname=meta.host or None,
    )

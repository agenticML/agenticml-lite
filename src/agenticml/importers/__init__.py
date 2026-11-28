"""Import runs from other trackers into the local v1 store.

A source module (``tensorboard``, ``mlflow``, ``wandb``, ``table``) reads
another tracker's logs on disk and yields :class:`ImportedRun` objects from
``discover(path, options)``. :func:`run_import` writes each one through
:class:`~agenticml.store.RunWriter` as a normal, finished run. Nothing in the
source is changed.

The run id is derived from the source run, so importing the same run twice
gives the same id and the second import skips it. A reader must therefore
derive ``started_at`` from the source data (never from the clock or from a
file it may have touched) and ``location`` from the resolved source path.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import importlib
import json
import logging
import math
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..store.layout import (
    META_NAME,
    RunLockedError,
    format_timestamp,
    read_file_nofollow,
    run_dir,
    try_lock_run,
    unlock_run,
    valid_run_id,
)
from ..store.writer import RunWriter, coerce_step, coerce_value

logger = logging.getLogger("agenticml.import")

# source name -> module that exposes ``discover(path, options)``
REGISTRY: dict[str, str] = {
    "tensorboard": "agenticml.importers.tensorboard",
    "mlflow": "agenticml.importers.mlflow",
    "wandb": "agenticml.importers.wandb",
    "csv": "agenticml.importers.table",
}
SOURCES = tuple(REGISTRY)

# Sources whose PATH may name something that is not on disk (W&B cloud).
_PATH_OPTIONAL = frozenset({"wandb"})
# Options only some sources use.
_CSV_ONLY_OPTIONS = ("step_column", "time_column")

AUTO_TAG_PREFIX = "imported:"
_MAX_UNIX = 253402300799  # 9999-12-31T23:59:59Z


class ImportSourceError(Exception):
    """The whole PATH cannot be imported (missing, wrong format, importer absent)."""


@dataclass
class ImportedRun:
    """One source run, as a reader hands it to the writer."""

    source: str  # "tensorboard", "mlflow", ...
    location: str  # absolute path or URI of the source store
    source_run_id: str  # stable id inside the source
    name: str
    project: str
    started_at: datetime  # UTC
    ended_at: datetime | None
    status: str  # "finished" | "crashed" | "running"
    source_status: str | None
    config: dict
    summary: dict
    tags: list[str]
    # (wall_time, step, {key: value}) in source order; wall_time is Unix seconds.
    points: Iterable[tuple[float, int, dict[str, float]]]
    # (wall_time, step, key, data, content_type, caption) in source order.
    media: Iterable[tuple[float, int, str, bytes, str, str | None]]
    notes: str = ""
    # Keys whose steps the reader renumbered (see renumber_constant_steps). A
    # reader may fill this while ``points`` streams; it is read after the last point.
    renumbered_steps: list[str] = field(default_factory=list)
    # Source id of the parent run (MLflow nested runs). Linked after the import
    # when the parent was in the same import.
    parent_source_run_id: str | None = None


@dataclass
class ImportOptions:
    """Everything the CLI passes to every reader, so later parts never touch the CLI."""

    project: str | None = None
    tags: list[str] = field(default_factory=list)
    run_globs: list[str] = field(default_factory=list)
    include_running: bool = False
    step_column: str | None = None  # csv only
    time_column: str | None = None  # csv only
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        """Record a reader warning for the report."""
        logger.warning("%s", message)
        if message not in self.warnings:
            self.warnings.append(message)


class SourceReader(Protocol):
    """What a source module provides."""

    def discover(self, path: str, options: ImportOptions) -> Iterator[ImportedRun]: ...


@dataclass
class ImportRow:
    """The outcome for one source run."""

    status: str  # "imported" | "skipped" | "failed"
    name: str
    run_id: str | None = None
    source_run_id: str | None = None
    points: int = 0
    media: int = 0
    dropped_points: int = 0
    dropped_media: int = 0
    # Why a run was skipped or failed; for an imported run, a note (e.g. no data).
    reason: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class ImportReport:
    source: str
    path: str
    dry_run: bool = False
    rows: list[ImportRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for row in self.rows if row.status == status)

    @property
    def imported(self) -> int:
        return self.count("imported")

    @property
    def skipped(self) -> int:
        return self.count("skipped")

    @property
    def failed(self) -> int:
        return self.count("failed")

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "path": self.path,
            "dry_run": self.dry_run,
            "runs": [row.to_dict() for row in self.rows],
            "imported": self.imported,
            "skipped": self.skipped,
            "failed": self.failed,
            "warnings": list(self.warnings),
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def import_run_id(run: ImportedRun) -> str:
    """``<start as YYYYmmddTHHMMSS>-<8 hex of sha256(source, location, source_run_id)>``."""
    start = _utc(run.started_at)
    if not 1970 <= start.year <= 9999:
        raise ValueError(f"implausible start time {start.isoformat()}")
    stamp = (
        f"{start.year:04d}{start.month:02d}{start.day:02d}"
        f"T{start.hour:02d}{start.minute:02d}{start.second:02d}"
    )
    key = f"{run.source}\0{run.location}\0{run.source_run_id}".encode("utf-8", "surrogatepass")
    digest = hashlib.sha256(key).hexdigest()[:8]
    run_id = f"{stamp}-{digest}"
    if not valid_run_id(run_id):
        raise ValueError(f"cannot build a run id from {run_id!r}")
    return run_id


def renumber_constant_steps(points: Iterable) -> tuple[list, list[str]]:
    """Give a series whose every point has the same step the steps ``0..n-1``.

    Some sources (MLflow ``log_metric`` without ``step``) log step 0 every
    time, so the order cannot be read from the steps. For each key that has
    more than one point and one single step, the points are numbered in time
    order (wall time, then source order). Other keys keep their steps.

    ``points`` is ``(wall_time, step, {key: value})``; the whole input is held in
    memory, so call it per series or per run, not on a huge log. Returns
    ``(points, renumbered_keys)``. A point with keys that end up on different
    steps is split, one point per step, in source order.
    """
    items = list(points)
    steps_by_key: dict[str, set] = {}
    count_by_key: dict[str, int] = {}
    for _wall, step, metrics in items:
        for key in metrics:
            steps_by_key.setdefault(key, set()).add(step)
            count_by_key[key] = count_by_key.get(key, 0) + 1
    constant = [
        key for key, steps in steps_by_key.items() if len(steps) == 1 and count_by_key[key] > 1
    ]
    if not constant:
        return items, []
    constant_set = set(constant)
    positions: dict[str, list[tuple[float, int]]] = {key: [] for key in constant}
    for index, (wall, _step, metrics) in enumerate(items):
        for key in metrics:
            if key in constant_set:
                positions[key].append((wall, index))
    new_step: dict[tuple[str, int], int] = {}
    for key, entries in positions.items():
        for rank, (_wall, index) in enumerate(sorted(entries)):
            new_step[(key, index)] = rank
    out: list = []
    for index, (wall, step, metrics) in enumerate(items):
        by_step: dict[int, dict] = {}
        for key, value in metrics.items():
            target = new_step.get((key, index), step) if key in constant_set else step
            by_step.setdefault(target, {})[key] = value
        for target, group in by_step.items():
            out.append((wall, target, group))
    return out, sorted(constant)


def _load_reader(source: str):
    module_name = REGISTRY.get(source)
    if module_name is None:
        raise ImportSourceError(
            f"unknown source {source!r} (choose from {', '.join(SOURCES)})"
        )
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            raise ImportSourceError(
                f"the {source} importer is not available in this version"
            ) from None
        raise ImportSourceError(
            f"the {source} importer could not be loaded: {exc}"
        ) from exc
    except ImportError as exc:
        raise ImportSourceError(f"the {source} importer could not be loaded: {exc}") from exc
    discover = getattr(module, "discover", None)
    if not callable(discover):
        raise ImportSourceError(f"the {source} importer is not available in this version")
    return discover


def _read_meta(path: Path) -> dict | None:
    try:
        data = json.loads(read_file_nofollow(path).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _unique(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if isinstance(item, str) and item not in seen:
            seen[item] = None
    return list(seen)


_NO_DATA_NOTE = "no points or media (config only)"
_MAX_PLAIN_DEPTH = 64


def _clean_str(text: str) -> str:
    """Replace what UTF-8 cannot encode (lone surrogates) so the text is valid JSON."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def json_plain(value, _depth: int = 0, _seen: frozenset = frozenset()):
    """Deep-copy ``value`` into plain JSON types (what native runs store).

    Non-finite floats become ``None``, mapping keys become ``str``, bytes are
    decoded as UTF-8 with replacement, other containers become lists, anything
    else becomes ``str`` (``"<unserializable>"`` if that raises), and strings
    lose lone surrogates. Cycles and very deep values become ``None``.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    marker = id(value)
    if _depth >= _MAX_PLAIN_DEPTH or marker in _seen:
        return None
    seen = _seen | {marker}
    try:
        if isinstance(value, Mapping):
            out: dict = {}
            for key, item in list(value.items()):
                text = key if isinstance(key, str) else json_plain(key, _depth + 1, seen)
                if not isinstance(text, str):
                    text = "null" if text is None else json.dumps(text)
                out[_clean_str(text)] = json_plain(item, _depth + 1, seen)
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            return [json_plain(item, _depth + 1, seen) for item in list(value)]
        item = getattr(value, "item", None)
        if callable(item) and getattr(value, "shape", None) == ():
            extracted = item()
            if extracted is None or isinstance(extracted, (bool, int, float, str)):
                return json_plain(extracted, _depth + 1, seen)
        return _clean_str(str(value))
    except Exception:
        return "<unserializable>"


def _plain_dict(value) -> dict:
    plain = json_plain(value) if value else {}
    return plain if isinstance(plain, dict) else {}


def _remove_run_dir(root, run_id: str) -> None:
    shutil.rmtree(run_dir(root, run_id), ignore_errors=True)


def _lock_existing(directory: Path):
    """Try the run lock a few times (short reader probes can hold it briefly)."""
    for attempt in range(6):
        handle = try_lock_run(directory)
        if handle is not None:
            return handle
        if attempt < 5:
            time.sleep(0.05)
    return None


def _consume_dry(run: ImportedRun) -> tuple[int, int, int, int]:
    points = dropped_points = media = dropped_media = 0
    for _wall, step, metrics in run.points:
        ok_step = coerce_step(step) is not None
        for key, value in metrics.items():
            if ok_step and isinstance(key, str) and coerce_value(value) is not None:
                points += 1
            else:
                dropped_points += 1
    for _wall, step, key, _data, _content_type, _caption in run.media:
        if coerce_step(step) is not None and isinstance(key, str):
            media += 1
        else:
            dropped_media += 1
    return points, dropped_points, media, dropped_media


def _restore_aside(directory: Path, aside: Path | None, row: ImportRow) -> None:
    """Put the old run back after a failed replace."""
    if aside is None:
        return
    try:
        os.rename(aside, directory)
    except OSError as exc:
        suffix = f"; the old run is kept at {aside} ({exc})"
        row.reason = (row.reason or "failed") + suffix


def write_imported_run(root, run: ImportedRun, *, replace: bool = False, dry_run: bool = False) -> ImportRow:
    """Write one source run as a finished run. Never raises for a bad run.

    The returned row says ``imported``, ``skipped`` (already there, or being
    written by another process) or ``failed``. A failed run leaves no run
    directory behind; with ``replace`` the old run is moved aside first and put
    back when the new one fails. A run left ``running`` by an interrupted
    import is replaced without ``replace``. With ``dry_run`` the reader is
    consumed and counted but nothing is written.
    """
    name = _clean_str(run.name or run.source_run_id)
    try:
        run_id = import_run_id(run)
    except Exception as exc:
        return ImportRow("failed", name, source_run_id=run.source_run_id, reason=str(exc))
    row = ImportRow("imported", name, run_id, run.source_run_id)

    try:
        directory = run_dir(root, run_id)
        meta_path = directory / META_NAME
        exists = meta_path.is_file()
    except Exception as exc:
        row.status, row.reason = "failed", str(exc)
        return row

    lock = None
    aside: Path | None = None
    stale = False
    if exists:
        existing = _read_meta(meta_path)
        imported = isinstance(existing, dict) and isinstance(existing.get("imported_from"), dict)
        stale = imported and existing.get("status") == "running"
        if not replace and not stale:
            row.status, row.reason = "skipped", "already imported"
            return row
        if not imported:
            row.status, row.reason = "failed", "not an imported run; refusing to replace it"
            return row
        if os.path.lexists(directory / "sync.json"):
            if replace:
                row.status, row.reason = "failed", "already synced to a server; refusing to replace it"
            else:
                row.status, row.reason = "skipped", "incomplete import, already synced; use --replace"
            return row
        if not dry_run:
            try:
                lock = _lock_existing(directory)
            except OSError as exc:
                row.status, row.reason = "failed", f"cannot check the old run: {exc}"
                return row
            if lock is None:
                if replace:
                    row.status, row.reason = "failed", "run is open in another process; not replacing it"
                else:
                    row.status, row.reason = "skipped", "being imported by another process"
                return row
            # A sync may have published sync.json between the check above and the lock.
            if os.path.lexists(directory / "sync.json"):
                unlock_run(lock)
                if replace:
                    row.status, row.reason = "failed", "already synced to a server; refusing to replace it"
                else:
                    row.status, row.reason = "skipped", "incomplete import, already synced; use --replace"
                return row
            # Move the old run aside; it is deleted only after the new one is finished.
            aside = directory.with_name(f".replaced-{run_id}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
            try:
                if os.name == "nt":
                    unlock_run(lock)
                    lock = None
                os.rename(directory, aside)
            except OSError as exc:
                unlock_run(lock)
                row.status, row.reason = "failed", f"cannot replace the old run: {exc}"
                return row

    try:
        return _write_new(root, run, row, directory, meta_path, aside, dry_run)
    finally:
        unlock_run(lock)


def _write_new(root, run, row, directory, meta_path, aside, dry_run):
    name = row.name
    run_id = row.run_id
    if dry_run:
        try:
            row.points, row.dropped_points, row.media, row.dropped_media = _consume_dry(run)
        except Exception as exc:
            logger.debug("import of %s failed", name, exc_info=True)
            row.status, row.reason = "failed", f"{type(exc).__name__}: {exc}"
            row.points = row.media = row.dropped_points = row.dropped_media = 0
            return row
        if not (row.points or row.media):
            row.reason = _NO_DATA_NOTE
        return row

    from .. import __version__

    started = _utc(run.started_at)
    imported_from = json_plain({
        "source": run.source,
        "location": run.location,
        "source_run_id": run.source_run_id,
        "source_status": run.source_status or (run.status if run.status == "running" else None),
        "imported_at": format_timestamp(),
        "agenticml_version": __version__,
    })
    meta = {
        "project": json_plain(run.project or ""),
        "name": json_plain(run.name or ""),
        "config": _plain_dict(run.config),
        "tags": _unique(json_plain([*(run.tags or []), f"{AUTO_TAG_PREFIX}{run.source}"])),
        "notes": json_plain(run.notes or ""),
        "group": None,
        "job_type": None,
        "imported_from": imported_from,
    }
    existed_before = directory.exists()
    writer = None
    try:
        try:
            writer = RunWriter.create(root, run_id=run_id, meta=meta, created_at=started)
        except FileExistsError:
            row.status, row.reason = "skipped", "already imported"
            _restore_aside(directory, aside, row)
            return row
        except RunLockedError:
            # Another process is inside create() for this run; leave its directory alone.
            row.status, row.reason = "skipped", "being imported by another process"
            _restore_aside(directory, aside, row)
            return row
        except BaseException:
            if not existed_before and not meta_path.is_file():
                _remove_run_dir(root, run_id)
            raise
        created_unix = started.timestamp()
        last_wall = created_unix
        group_key = None
        group: dict[str, float] = {}

        def flush() -> None:
            nonlocal group, group_key
            if group_key is not None and group:
                wall, step = group_key
                writer.write_scalars([(step, group)], wall_time=wall)
            group = {}
            group_key = None

        for wall, step, metrics in run.points:
            if not (isinstance(wall, (int, float)) and math.isfinite(wall) and 0 <= wall < _MAX_UNIX):
                wall = last_wall
            wall = float(wall)
            last_wall = wall
            coerced = coerce_step(step)
            for key, value in metrics.items():
                number = coerce_value(value) if isinstance(key, str) else None
                if coerced is None or number is None:
                    row.dropped_points += 1
                    continue
                if group_key != (wall, coerced) or key in group:
                    flush()
                    group_key = (wall, coerced)
                group[key] = number
                row.points += 1
        flush()

        for wall, step, key, data, content_type, caption in run.media:
            if isinstance(wall, (int, float)) and math.isfinite(wall) and 0 <= wall < _MAX_UNIX:
                last_wall = max(last_wall, float(wall))
            coerced = coerce_step(step)
            if coerced is None or not isinstance(key, str):
                row.dropped_media += 1
                continue
            writer.add_media(key, coerced, data, content_type, caption)
            row.media += 1

        if run.renumbered_steps:
            writer.set_meta_fields(
                imported_from={
                    **imported_from,
                    "renumbered_steps": json_plain(sorted(set(run.renumbered_steps))),
                }
            )
        ended = _utc(run.ended_at) if run.ended_at is not None else datetime.fromtimestamp(last_wall, timezone.utc)
        if ended < started:
            ended = started
        writer.finish(
            "crashed" if run.status == "crashed" else "finished",
            _plain_dict(run.summary),
            finished_at=ended,
        )
        if not (row.points or row.media):
            row.reason = _NO_DATA_NOTE
        if aside is not None:
            shutil.rmtree(aside, ignore_errors=True)
        return row
    except BaseException as exc:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            _remove_run_dir(root, run_id)
        _restore_aside(directory, aside, row)
        if not isinstance(exc, Exception):
            raise
        logger.debug("import of %s failed", name, exc_info=True)
        row.status = "failed"
        base = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        row.reason = base if row.reason is None else f"{base}; {row.reason}"
        row.points = row.media = row.dropped_points = row.dropped_media = 0
        return row


def _safe_attr(obj, attr: str) -> str:
    """``obj.attr`` as clean text, or "" when it is missing or its getter fails."""
    try:
        return _clean_str(str(getattr(obj, attr) or ""))
    except Exception:
        return ""


def _check_root(root, dry_run: bool) -> None:
    """One clear error when the local store root cannot be used, instead of a failed row per run."""
    path = Path(root)
    try:
        if path.exists() and not path.is_dir():
            raise NotADirectoryError(f"{path} is not a directory")
        if not dry_run:
            os.makedirs(path / "runs", exist_ok=True)
    except OSError as exc:
        raise ImportSourceError(f"cannot use {path}: {exc}") from exc


def _matches(run: ImportedRun, globs: list[str]) -> bool:
    if not globs:
        return True
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for pattern in globs
        for candidate in (run.name, run.source_run_id)
    )


def run_import(
    root,
    source: str,
    path: str,
    options: ImportOptions | None = None,
    *,
    replace: bool = False,
    dry_run: bool = False,
    on_row: Callable[[ImportRow], None] | None = None,
) -> ImportReport:
    """Import every source run under ``path`` into the store at ``root``.

    Raises :class:`ImportSourceError` when the whole path cannot be read or the
    source importer is missing. A problem with one run is a ``failed`` row and
    the next run still imports. ``on_row`` is called as each row is final.
    """
    options = options or ImportOptions()
    if source != "csv" and any(getattr(options, name) for name in _CSV_ONLY_OPTIONS):
        raise ImportSourceError("--step-column and --time-column only apply to the csv source")
    discover = _load_reader(source)
    if source not in _PATH_OPTIONAL and not os.path.exists(path):
        raise ImportSourceError(f"{path} does not exist")

    report = ImportReport(source, str(path), dry_run=dry_run)

    def add(row: ImportRow) -> None:
        report.rows.append(row)
        if on_row is not None:
            on_row(row)

    imported_ids: dict[str, str] = {}  # source_run_id -> run id (imported or already there)
    parents: list[tuple[str, str]] = []  # (run id, parent source_run_id), newly imported only
    try:
        runs = iter(discover(str(path), options))
    except ImportSourceError:
        raise
    except Exception as exc:
        logger.debug("reading %s failed", path, exc_info=True)
        raise ImportSourceError(f"cannot read {path}: {type(exc).__name__}: {exc}") from exc
    yielded = False
    root_checked = False
    while True:
        try:
            run = next(runs)
        except StopIteration:
            break
        except ImportSourceError:
            raise
        except Exception as exc:
            logger.debug("reading %s failed", path, exc_info=True)
            if not yielded:
                # Nothing could be read from the path at all (e.g. a scan that is not allowed).
                raise ImportSourceError(f"cannot read {path}: {type(exc).__name__}: {exc}") from exc
            add(ImportRow("failed", str(path), reason=f"{type(exc).__name__}: {exc}"))
            break
        yielded = True
        try:
            if not _matches(run, options.run_globs):
                continue
            if run.status == "running" and not options.include_running:
                try:
                    run_id = import_run_id(run)
                except Exception:
                    run_id = None
                add(
                    ImportRow(
                        "skipped",
                        run.name or run.source_run_id,
                        run_id,
                        run.source_run_id,
                        reason="still running; use --include-running",
                    )
                )
                continue
            if not root_checked:
                _check_root(root, dry_run)
                root_checked = True
            run = dataclasses.replace(
                run,
                project=options.project if options.project is not None else run.project,
                tags=_unique([*(run.tags or []), *options.tags]),
            )
        except ImportSourceError:
            raise
        except Exception as exc:
            logger.debug("import of a run from %s failed", path, exc_info=True)
            add(
                ImportRow(
                    "failed",
                    _safe_attr(run, "name") or _safe_attr(run, "source_run_id") or str(path),
                    source_run_id=_safe_attr(run, "source_run_id") or None,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        row = write_imported_run(root, run, replace=replace, dry_run=dry_run)
        if row.run_id is not None and row.status != "failed":
            imported_ids[run.source_run_id] = row.run_id
            if row.status == "imported" and run.parent_source_run_id:
                parents.append((row.run_id, run.parent_source_run_id))
        add(row)

    if not dry_run:
        for run_id, parent_source_id in parents:
            parent_id = imported_ids.get(parent_source_id)
            if parent_id is None or parent_id == run_id:
                continue
            try:
                child = RunWriter.amend(root, run_id)
            except (OSError, ValueError, RunLockedError):
                logger.debug("could not link parent of %s", run_id, exc_info=True)
                continue
            try:
                child.set_meta_fields(parent_run_id=parent_id)
            finally:
                child.close()
    report.warnings = list(options.warnings)
    return report

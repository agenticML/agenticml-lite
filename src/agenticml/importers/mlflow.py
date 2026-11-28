"""MLflow importer: a ``mlruns`` file store or a ``mlflow.db`` SQLite database.

Both stores are read with the standard library only; ``mlflow`` is not needed.
Nothing in the source is changed: SQLite opens read-only, and the file store
is only read (symlinks and special files are never followed).

File store layout (MLflow 1.x to 3.x)::

    mlruns/<experiment_id>/meta.yaml                 experiment (name, lifecycle_stage)
    mlruns/<experiment_id>/<run_id>/meta.yaml        run (run_name, start_time, status, ...)
    mlruns/<experiment_id>/<run_id>/metrics/<key>    lines "timestamp_ms value [step ...]"
    mlruns/<experiment_id>/<run_id>/params/<key>     the parameter value
    mlruns/<experiment_id>/<run_id>/tags/<key>       the tag value

Keys with ``/`` are nested directories. SQLite uses the ``experiments``,
``runs``, ``metrics``, ``params`` and ``tags`` tables. Field names were checked
against MLflow 3.18 (``mlflow/store/tracking/file_store.py`` and
``mlflow/store/tracking/dbmodels/models.py``).
"""

from __future__ import annotations

import heapq
import math
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from . import (
    ImportedRun,
    ImportOptions,
    ImportSourceError,
    renumber_constant_steps,
)

SOURCE = "mlflow"

# MLflow RunStatus enum (mlflow/protos/service.proto). Older file stores wrote the
# number, newer ones and the SQL store write the name.
_STATUS_NAMES = {1: "RUNNING", 2: "SCHEDULED", 3: "FINISHED", 4: "FAILED", 5: "KILLED"}
_STATUS_KIND = {
    "FINISHED": "finished",
    "FAILED": "crashed",
    "KILLED": "crashed",
    "RUNNING": "running",
    "SCHEDULED": "running",
}

_MAX_META_BYTES = 1 << 20  # meta.yaml, one param or tag value
_MAX_METRIC_LINE = 4096
_MAX_WALK_DEPTH = 16  # nested metric / param / tag key directories
_MAX_FILES_PER_DIR = 100_000
_BATCH_LINES = 2048  # metric lines read per open(), so many series never hold many handles
_BATCH_BUDGET = 200_000  # parsed metric lines held at once across all streamed series of a run
_MIN_BATCH_LINES = 64
_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")

# table -> columns the importer reads
_REQUIRED_COLUMNS = {
    "experiments": ("experiment_id", "name", "lifecycle_stage"),
    "runs": (
        "run_uuid",
        "name",
        "status",
        "start_time",
        "end_time",
        "lifecycle_stage",
        "experiment_id",
    ),
    "metrics": ("key", "value", "timestamp", "run_uuid", "step", "is_nan"),
    "params": ("key", "value", "run_uuid"),
    "tags": ("key", "value", "run_uuid"),
}

_NOTE_TAG = "mlflow.note.content"
_NAME_TAG = "mlflow.runName"
_PARENT_TAG = "mlflow.parentRunId"


# --------------------------------------------------------------------- flat YAML


_YAML_INT = re.compile(r"^[-+]?\d+$")
# YAML 1.1 as PyYAML reads it: a float needs a "."; "1e-4" is a plain string.
_YAML_FLOAT = re.compile(r"^[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?$")
_YAML_INF = re.compile(r"^([-+]?)\.(?:inf|Inf|INF)$")
_TEXT_KEYS = ("name", "run_name", "experiment_id")  # keep the text of a plain scalar
_YAML_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": " ",
    "P": " ",
}


def _fold_lines(raw: list[str]) -> str:
    """Fold a multi-line flow scalar: a line break becomes one space, an empty line a newline."""
    out: list[str] = []
    pending_breaks = 0
    for index, line in enumerate(raw):
        text = line.strip()
        if index == 0:
            out.append(text)
            continue
        if not text:
            pending_breaks += 1
            continue
        if pending_breaks:
            out.append("\n" * pending_breaks)
            pending_breaks = 0
        elif out and not out[-1].endswith("\n"):
            out.append(" ")
        out.append(text)
    return "".join(out)


def _unescape_double(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(text):
            break
        esc = text[i]
        i += 1
        if esc in _YAML_ESCAPES:
            out.append(_YAML_ESCAPES[esc])
        elif esc in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[esc]
            digits = text[i : i + width]
            try:
                out.append(chr(int(digits, 16)) if len(digits) == width else "")
            except (ValueError, OverflowError):
                out.append("")
            i += width
        elif esc == "\n":
            continue  # escaped line break joins the lines
        else:
            out.append(esc)
    return "".join(out)


def _split_yaml_key(line: str) -> tuple[str, str] | None:
    """``(key, value text)`` of one ``key: value`` line, or None when it is not one.

    Linear time: the key ends at its closing quote, or at the first ``:`` of a
    plain key. That ``:`` must be followed by a blank or the end of the line.
    """
    first = line[0]
    if first == "'":
        i = 1
        chars: list[str] = []
        while i < len(line):
            if line[i] == "'":
                if line[i + 1 : i + 2] == "'":
                    chars.append("'")
                    i += 2
                    continue
                break
            chars.append(line[i])
            i += 1
        else:
            return None
        key = "".join(chars)
        end = i + 1
    elif first == '"':
        i = 1
        while i < len(line) and line[i] != '"':
            i += 2 if line[i] == "\\" else 1
        if i >= len(line):
            return None
        key = _unescape_double(line[1:i])
        end = i + 1
    else:
        if first in "#-?:" and not (first == "-" and line[1:2].strip()):
            return None
        colon = line.find(":")
        if colon <= 0:
            return None
        key = line[:colon].strip()
        end = colon
        if not key:
            return None
    rest = line[end:].lstrip()
    if not rest.startswith(":"):
        return None
    after = rest[1:]
    if after and not after[0].isspace():
        return None
    return key, after.strip()


def _yaml_scalar(first: str, rest: list[str], text_only: bool = False):
    """The value of one ``key: value`` entry; ``rest`` are its continuation lines.

    With ``text_only`` a plain scalar that is not null, a boolean or a flow
    ``[]`` / ``{}`` keeps its text even when it looks like a number.
    """
    value = first.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        joined = _fold_lines([first, *rest]) if rest else value
        body = joined.strip()
        if quote == "'":
            end = 1
            chars: list[str] = []
            while end < len(body):
                if body[end] == "'":
                    if body[end + 1 : end + 2] == "'":
                        chars.append("'")
                        end += 2
                        continue
                    break
                chars.append(body[end])
                end += 1
            return "".join(chars)
        end = 1
        while end < len(body) and body[end] != '"':
            end += 2 if body[end] == "\\" else 1
        return _unescape_double(body[1:end])
    if rest:
        value = _fold_lines([first, *rest])
    else:
        # a plain scalar ends at " #"
        hash_at = value.find(" #")
        if hash_at >= 0:
            value = value[:hash_at].rstrip()
    if value in ("", "~", "null", "Null", "NULL"):
        return None
    if value in ("true", "True", "TRUE"):
        return True
    if value in ("false", "False", "FALSE"):
        return False
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if text_only:
        return value
    inf = _YAML_INF.match(value)
    if inf:
        return -math.inf if inf.group(1) == "-" else math.inf
    if value in (".nan", ".NaN", ".NAN"):
        return math.nan
    if _YAML_INT.match(value):
        try:
            return int(value)
        except ValueError:
            return value
    if _YAML_FLOAT.match(value):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def parse_flat_yaml(text: str) -> dict:
    """Parse the flat ``key: value`` YAML MLflow writes for ``meta.yaml``.

    Understands plain, single-quoted and double-quoted scalars (also folded over
    several lines), ``null``, booleans, numbers, ``[]`` and ``{}``. Nested blocks
    (a key with no value followed by indented lines) read as ``None``. Anchors,
    aliases and tags (``!!python/...``) are never resolved: they stay plain
    text, so nothing in the file can run code or expand into a large structure.
    """
    entries: list[tuple[str, str, list[str]]] = []
    for line in text.splitlines():
        if not line.strip():
            if entries:
                entries[-1][2].append("")  # a blank line inside a multi-line scalar
            continue
        if line.lstrip().startswith("#"):
            continue
        if line.startswith(("---", "...")) and not line.strip("-. "):
            continue
        if line[0] in " \t":
            if entries:
                entries[-1][2].append(line)
            continue
        split = _split_yaml_key(line)
        if split is None:
            entries.append(("", "", []))  # a line we cannot place; keep order, ignore it
            continue
        entries.append((split[0], split[1], []))
    out: dict = {}
    for key, first, rest in entries:
        while rest and not rest[-1].strip():
            rest.pop()
        if not key:
            continue
        if not first.strip():
            out[key] = None  # a nested block we do not need
            continue
        out[key] = _yaml_scalar(first, rest, key in _TEXT_KEYS)
    return out


# --------------------------------------------------------------------- file helpers


def _open_regular(path):
    """Open a regular file for reading; refuse symlinks, FIFOs and devices."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if not hasattr(os, "O_NOFOLLOW") and stat.S_ISLNK(os.lstat(path).st_mode):
        raise OSError(f"refusing symlink: {path}")
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _read_text(path, limit: int = _MAX_META_BYTES) -> str | None:
    """File contents as text, or None when unreadable or larger than ``limit``."""
    try:
        with _open_regular(path) as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    return data.decode("utf-8", "replace")


def _is_real_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False) and not entry.is_symlink()
    except OSError:
        return False


def _is_regular_file(path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _child_dirs(path) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as entries:
            return sorted((e for e in entries if _is_real_dir(e)), key=lambda e: e.name)
    except OSError:
        return []


def _walk_files(base: Path, options: ImportOptions | None = None) -> list[tuple[str, Path]]:
    """``(key, path)`` of every regular file under ``base``; ``/`` joins nested directories."""
    found: list[tuple[str, Path]] = []
    try:
        # lstat, not stat: a symlinked metrics/params/tags directory is never followed
        if not stat.S_ISDIR(os.lstat(base).st_mode):
            return found
    except OSError:
        return found
    stack: list[tuple[str, str]] = [(str(base), "")]
    truncated = False
    while stack:
        directory, prefix = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            key = prefix + entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if prefix.count("/") + 1 < _MAX_WALK_DEPTH:
                        stack.append((entry.path, key + "/"))
                elif entry.is_file(follow_symlinks=False):
                    if len(found) >= _MAX_FILES_PER_DIR:
                        truncated = True
                        continue
                    found.append((key, Path(entry.path)))
            except OSError:
                continue
    if truncated and options is not None:
        options.warn(f"{base}: more than {_MAX_FILES_PER_DIR} files; the rest were ignored")
    found.sort(key=lambda item: item[0])
    return found


# --------------------------------------------------------------------- conversions


def _to_datetime(ms) -> datetime | None:
    """MLflow milliseconds since the epoch as a UTC datetime, or None if not usable."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    if isinstance(ms, float) and not math.isfinite(ms):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _status_name(raw) -> str | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return _STATUS_NAMES.get(raw)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.isdigit():
            return _STATUS_NAMES.get(int(text), text)
        return text.upper()
    return None


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    return int(number)


def _as_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _raise_points(reason: str) -> Iterator[tuple[float, int, dict[str, float]]]:
    raise ImportSourceError(reason)
    yield  # pragma: no cover  (makes this a generator, so the error shows when it is read)


def _failed_run(
    *, location: str, run_id: str, name: str, project: str, reason: str
) -> ImportedRun:
    """A run the importer cannot read: the writer reports it as ``failed`` with ``reason``."""
    return ImportedRun(
        source=SOURCE,
        location=location,
        source_run_id=run_id,
        name=name or run_id,
        project=project,
        started_at=datetime.fromtimestamp(0, timezone.utc),
        ended_at=None,
        status="finished",
        source_status=None,
        config={},
        summary={},
        tags=[],
        points=_raise_points(reason),
        media=[],
    )


class _Stats:
    """Counts of what a run's metric reader had to skip."""

    def __init__(self) -> None:
        self.malformed = 0


def _build_run(
    *,
    location: str,
    run_id: str,
    experiment_id: str,
    experiment_name: str,
    meta_name,
    start_ms,
    end_ms,
    status_raw,
    params: dict[str, str],
    tags: dict[str, str],
    points,
    options: ImportOptions,
    renumbered: list[str],
    fallback_start_ms=None,
) -> ImportedRun:
    """Common mapping of one MLflow run to an :class:`ImportedRun`."""
    name = meta_name if isinstance(meta_name, str) and meta_name else tags.get(_NAME_TAG) or run_id
    project = experiment_name or f"experiment {experiment_id}"
    started = _to_datetime(start_ms)
    if started is None:
        started = _to_datetime(end_ms)
    if started is None:
        started = _to_datetime(fallback_start_ms)
    if started is None:
        return _failed_run(
            location=location,
            run_id=run_id,
            name=name,
            project=project,
            reason="run has no usable start time",
        )
    ended = _to_datetime(end_ms)
    if ended is not None and ended < started:
        ended = None

    status_name = _status_name(status_raw)
    kind = _STATUS_KIND.get(status_name or "")
    if kind is None:
        options.warn(
            f"{run_id}: unknown MLflow status {status_raw!r}; imported as finished"
        )
        kind = "finished"

    config: dict = dict(params)
    other_tags = {k: v for k, v in sorted(tags.items()) if not k.startswith("mlflow.")}
    if other_tags and "mlflow.tags" not in config:
        config["mlflow.tags"] = other_tags
    parent = tags.get(_PARENT_TAG) or None
    run = ImportedRun(
        source=SOURCE,
        location=location,
        source_run_id=run_id,
        name=name,
        project=project,
        started_at=started,
        ended_at=ended,
        status=kind,
        source_status=status_name or (str(status_raw) if status_raw is not None else None),
        config=config,
        summary={},
        tags=[],
        points=points,
        media=[],
        notes=tags.get(_NOTE_TAG, ""),
        renumbered_steps=renumbered,
        parent_source_run_id=parent if parent != run_id else None,
    )
    return run


# --------------------------------------------------------------------- file store


def _looks_like_file_store(path: Path) -> bool:
    """True when a child directory holds an experiment ``meta.yaml``."""
    for entry in _child_dirs(path):
        if entry.name.startswith("."):
            continue
        meta = os.path.join(entry.path, "meta.yaml")
        if os.path.isfile(meta) and not os.path.islink(meta):
            return True
    return False


def _parse_metric_line(line: bytes):
    """``(timestamp_ms, step, value)`` from one metric line, or None when malformed."""
    parts = line.decode("utf-8", "replace").split()
    # MLflow writes 2, 3 or 5 fields (timestamp value [step [model_id dataset ...]]).
    if len(parts) < 2:
        return None
    timestamp = _as_float(parts[0])
    value = _as_float(parts[1])
    if timestamp is None or not math.isfinite(timestamp) or value is None:
        return None
    step = 0
    if len(parts) >= 3:
        step = _as_int(parts[2])
        if step is None:
            return None
    return timestamp, step, value


def _read_metric_file(
    path: Path, stats: _Stats, batch_lines: int = _BATCH_LINES
) -> Iterator[tuple[float, int, float]]:
    """Parsed lines of one metric file, opened in short batches (one handle at a time)."""
    offset = 0
    while True:
        batch: list[tuple[float, int, float]] = []
        done = False
        try:
            with _open_regular(path) as handle:
                handle.seek(offset)
                for _ in range(batch_lines):
                    line = handle.readline(_MAX_METRIC_LINE + 1)
                    if not line:
                        done = True
                        break
                    if len(line) > _MAX_METRIC_LINE and not line.endswith(b"\n"):
                        # an overlong line: drop the rest of it
                        stats.malformed += 1
                        while True:
                            rest = handle.readline(_MAX_METRIC_LINE + 1)
                            if not rest or rest.endswith(b"\n"):
                                break
                        continue
                    if not line.strip():
                        continue
                    parsed = _parse_metric_line(line)
                    if parsed is None:
                        stats.malformed += 1
                    else:
                        batch.append(parsed)
                offset = handle.tell()
        except OSError:
            stats.malformed += 1
            return
        yield from batch
        if done:
            return


def _series_points(
    key: str, path: Path, stats: _Stats, batch_lines: int = _BATCH_LINES
) -> Iterator[tuple[float, int, dict[str, float]]]:
    for timestamp, step, value in _read_metric_file(path, stats, batch_lines):
        yield timestamp / 1000.0, step, {key: value}


def _file_store_points(
    metrics_dir: Path,
    renumbered: list[str],
    run_label: str,
    options: ImportOptions,
) -> Iterator[tuple[float, int, dict[str, float]]]:
    """Every series of one run, merged in time order, streaming where possible.

    A first pass over each file finds out whether the series has one single step
    and whether its timestamps are in order. Such a series is held in memory (and
    renumbered or sorted); every other series is streamed.
    """
    stats = _Stats()  # malformed lines, counted on the first pass only
    later = _Stats()
    streams: list[Iterator] = []
    files = _walk_files(metrics_dir, options)
    # Each streamed series holds one batch of parsed lines, so the batch shrinks as
    # the series count grows and the total stays near _BATCH_BUDGET lines.
    batch_lines = min(_BATCH_LINES, max(_MIN_BATCH_LINES, _BATCH_BUDGET // max(1, len(files))))
    for key, path in files:
        count = 0
        first_step = None
        constant = True
        ordered = True
        previous = None
        for timestamp, step, _value in _read_metric_file(path, stats, batch_lines):
            count += 1
            if first_step is None:
                first_step = step
            elif step != first_step:
                constant = False
            if previous is not None and timestamp < previous:
                ordered = False
            previous = timestamp
        if not count:
            continue
        if constant and count > 1:
            series, keys = renumber_constant_steps(_series_points(key, path, later, batch_lines))
            renumbered.extend(keys)
            series.sort(key=lambda point: point[0])
            streams.append(iter(series))
        elif not ordered:
            series = list(_series_points(key, path, later, batch_lines))
            series.sort(key=lambda point: point[0])
            streams.append(iter(series))
        else:
            streams.append(_series_points(key, path, later, batch_lines))
    yield from heapq.merge(*streams, key=lambda point: (point[0], point[1]))
    if stats.malformed:
        options.warn(f"{run_label}: skipped {stats.malformed} malformed metric lines")


def _read_key_files(directory: Path, options: ImportOptions) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, path in _walk_files(directory, options):
        text = _read_text(path)
        if text is not None:
            out[key] = text
    return out


def _discover_file_store(root: Path, options: ImportOptions) -> Iterator[ImportedRun]:
    location = str(root)
    collected: list[ImportedRun] = []
    for exp_entry in _child_dirs(root):
        if exp_entry.name.startswith("."):  # .trash and friends
            continue
        exp_text = _read_text(os.path.join(exp_entry.path, "meta.yaml"))
        if exp_text is None:
            if _is_regular_file(os.path.join(exp_entry.path, "meta.yaml")):
                options.warn(
                    f"{exp_entry.path}: experiment meta.yaml is unreadable or larger than "
                    f"{_MAX_META_BYTES} bytes; skipped"
                )
            continue
        exp_meta = parse_flat_yaml(exp_text)
        if exp_meta.get("lifecycle_stage") == "deleted":
            continue
        experiment_id = str(exp_meta.get("experiment_id") or exp_entry.name)
        experiment_name = str(exp_meta.get("name") or "") or f"experiment {experiment_id}"
        for run_entry in _child_dirs(exp_entry.path):
            if run_entry.name.startswith("."):
                continue
            run_dir = Path(run_entry.path)
            text = _read_text(run_dir / "meta.yaml")
            if text is None:
                meta_file = run_dir / "meta.yaml"
                if _is_regular_file(meta_file):
                    # a run directory whose meta.yaml is too large or unreadable
                    collected.append(
                        _failed_run(
                            location=location,
                            run_id=run_entry.name,
                            name=run_entry.name,
                            project=experiment_name,
                            reason="meta.yaml is unreadable or larger than "
                            f"{_MAX_META_BYTES} bytes",
                        )
                    )
                continue  # otherwise not a run directory
            meta = parse_flat_yaml(text)
            if meta.get("lifecycle_stage") == "deleted":
                continue
            run_id = run_entry.name
            params = _read_key_files(run_dir / "params", options)
            tags = _read_key_files(run_dir / "tags", options)
            renumbered: list[str] = []
            run = _build_run(
                location=location,
                run_id=run_id,
                experiment_id=experiment_id,
                experiment_name=experiment_name,
                meta_name=meta.get("run_name"),
                start_ms=meta.get("start_time"),
                end_ms=meta.get("end_time"),
                status_raw=meta.get("status"),
                params=params,
                tags=tags,
                points=_file_store_points(run_dir / "metrics", renumbered, run_id, options),
                options=options,
                renumbered=renumbered,
                fallback_start_ms=exp_meta.get("creation_time"),
            )
            collected.append(run)
    collected.sort(key=lambda r: (r.started_at, r.source_run_id))
    yield from collected


# --------------------------------------------------------------------- SQLite


def _sqlite_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_sqlite_uri(path), uri=True)
    conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _check_schema(path: Path) -> None:
    """Fail with one clear message when ``path`` is not an MLflow tracking database."""
    try:
        conn = _connect(path)
    except sqlite3.Error as exc:
        raise ImportSourceError(f"cannot open {path} as an SQLite database: {exc}") from exc
    try:
        for table, columns in _REQUIRED_COLUMNS.items():
            try:
                have = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            except sqlite3.Error as exc:
                raise ImportSourceError(f"cannot read {path}: {exc}") from exc
            if not have:
                raise ImportSourceError(
                    f"{path} is not an MLflow tracking database (no {table!r} table)"
                )
            missing = [column for column in columns if column not in have]
            if missing:
                raise ImportSourceError(
                    f"{path} has an unsupported MLflow schema: {table!r} lacks "
                    f"column {', '.join(missing)}"
                )
    finally:
        conn.close()


def _sqlite_constant_keys(conn: sqlite3.Connection, run_uuid: str) -> set[str]:
    """Keys of this run that have several points but one single step."""
    return {
        key
        for key, count, distinct in conn.execute(
            "SELECT key, COUNT(*), COUNT(DISTINCT step) FROM metrics "
            "WHERE run_uuid = ? GROUP BY key",
            (run_uuid,),
        )
        if count > 1 and distinct <= 1
    }


def _sqlite_points(
    db: Path, run_uuid: str, renumbered: list[str], options: ImportOptions
) -> Iterator[tuple[float, int, dict[str, float]]]:
    stats = _Stats()
    conn = _connect(db)
    try:
        constant = _sqlite_constant_keys(conn, run_uuid)
        renumbered.extend(sorted(constant))
        counters: dict[str, int] = {}
        cursor = conn.execute(
            "SELECT key, timestamp, step, value, is_nan FROM metrics "
            "WHERE run_uuid = ? ORDER BY timestamp, step",
            (run_uuid,),
        )
        for key, timestamp, step, value, is_nan in cursor:
            ts = _as_float(timestamp)
            number = math.nan if is_nan else _as_float(value)
            step_value = 0 if step is None else _as_int(step)
            if ts is None or not math.isfinite(ts) or number is None or step_value is None:
                stats.malformed += 1
                continue
            if key in constant:
                # One step for the whole series: number the points in time order, as
                # renumber_constant_steps does for the file store (streamed here).
                step_value = counters.get(key, 0)
                counters[key] = step_value + 1
            yield ts / 1000.0, step_value, {key: number}
    finally:
        conn.close()
    if stats.malformed:
        options.warn(f"{run_uuid}: skipped {stats.malformed} malformed metric lines")


def _discover_sqlite(db: Path, options: ImportOptions) -> Iterator[ImportedRun]:
    location = str(db)
    conn = _connect(db)
    try:
        has_creation = any(
            row[1] == "creation_time" for row in conn.execute('PRAGMA table_info("experiments")')
        )
        experiments = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT experiment_id, name, lifecycle_stage, "
                + ("creation_time" if has_creation else "NULL")
                + " FROM experiments"
            )
        }
        rows = conn.execute(
            "SELECT run_uuid, name, status, start_time, end_time, lifecycle_stage, experiment_id "
            "FROM runs ORDER BY start_time, run_uuid"
        ).fetchall()
        for run_uuid, name, status, start, end, lifecycle, experiment_id in rows:
            if lifecycle == "deleted":
                continue
            exp_name, exp_lifecycle, exp_created = experiments.get(experiment_id, (None, None, None))
            if exp_lifecycle == "deleted":
                continue
            params = {
                key: "" if value is None else str(value)
                for key, value in conn.execute(
                    "SELECT key, value FROM params WHERE run_uuid = ? ORDER BY key", (run_uuid,)
                )
            }
            tags = {
                key: "" if value is None else str(value)
                for key, value in conn.execute(
                    "SELECT key, value FROM tags WHERE run_uuid = ? ORDER BY key", (run_uuid,)
                )
            }
            renumbered: list[str] = []
            run = _build_run(
                location=location,
                run_id=str(run_uuid),
                experiment_id=str(experiment_id),
                experiment_name=exp_name or "",
                meta_name=name,
                start_ms=start,
                end_ms=end,
                status_raw=status,
                params=params,
                tags=tags,
                points=_sqlite_points(db, str(run_uuid), renumbered, options),
                options=options,
                renumbered=renumbered,
                fallback_start_ms=exp_created,
            )
            yield run
    finally:
        conn.close()


# --------------------------------------------------------------------- entry point


def _is_sqlite_file(path: Path) -> bool:
    if path.suffix.lower() in _SQLITE_SUFFIXES:
        return True
    try:
        with _open_regular(path) as handle:
            return handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def _locate(path: str, options: ImportOptions) -> tuple[str, Path]:
    """``("sqlite", db file)`` or ``("filestore", mlruns directory)`` for PATH."""
    target = Path(path)
    if target.is_file():
        if not _is_sqlite_file(target):
            raise ImportSourceError(
                f"{path} is not an MLflow store: expected an mlruns directory, "
                "a mlflow.db file, or a directory containing either"
            )
        return "sqlite", target.resolve()
    if not target.is_dir():
        raise ImportSourceError(f"{path} is not a file or directory")
    if _looks_like_file_store(target):
        return "filestore", target.resolve()
    db = target / "mlflow.db"
    nested = target / "mlruns"
    if db.is_file():
        if nested.is_dir() and _looks_like_file_store(nested):
            options.warn(
                f"{target} holds both mlflow.db and mlruns/; importing mlflow.db "
                "(pass the mlruns directory to import that instead)"
            )
        return "sqlite", db.resolve()
    if nested.is_dir() and _looks_like_file_store(nested):
        return "filestore", nested.resolve()
    raise ImportSourceError(
        f"no MLflow data under {path}: expected an mlruns directory "
        "(experiment folders with meta.yaml), a mlflow.db file, or a directory containing either"
    )


def discover(path: str, options: ImportOptions) -> Iterator[ImportedRun]:
    """Yield every active run of an MLflow file store or SQLite database."""
    kind, location = _locate(path, options)
    if kind == "sqlite":
        _check_schema(location)
        runs = _discover_sqlite(location, options)
    else:
        runs = _discover_file_store(location, options)
    return _with_empty_note(runs, location, options)


def _with_empty_note(
    runs: Iterator[ImportedRun], location: Path, options: ImportOptions
) -> Iterator[ImportedRun]:
    count = 0
    for run in runs:
        count += 1
        yield run
    if not count:
        options.warn(f"no active MLflow runs found in {location}")

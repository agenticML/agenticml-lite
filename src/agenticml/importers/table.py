"""Import metrics tables: CSV, JSONL, Lightning ``metrics.csv`` and Hugging Face ``trainer_state.json``.

``agenticml import csv PATH`` takes

- a ``.csv`` or ``.jsonl`` file: one run, one row per step (empty cells are skipped,
  so Lightning's sparse rows work);
- a Hugging Face ``trainer_state.json`` (its ``log_history`` rows);
- a directory, searched for Lightning ``CSVLogger`` ``metrics.csv`` files (one run per
  ``version_*`` directory, with a flat ``hparams.yaml`` as the config) and
  ``trainer_state.json`` files.

All input is untrusted: nothing is evaluated, YAML is read by a tiny flat parser (never
``yaml.load``), lines and files are size-capped, and a directory search never follows
symlinks. The source is only ever opened for reading.
"""

from __future__ import annotations

import csv
import fnmatch
import json
import logging
import math
import os
import re
import stat
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from . import ImportedRun, ImportOptions, ImportSourceError

logger = logging.getLogger("agenticml.import")

SOURCE = "csv"

STEP_COLUMNS = ("step", "global_step", "epoch")
TIME_COLUMNS = ("timestamp", "time", "wall_time")

LIGHTNING_FILE = "metrics.csv"
HPARAMS_FILE = "hparams.yaml"
HF_FILE = "trainer_state.json"
_JSONL_SUFFIXES = (".jsonl", ".ndjson")

_MAX_LINE = 16 * 1024 * 1024  # one JSONL line
_MAX_JSON_BYTES = 256 * 1024 * 1024  # trainer_state.json (a single document)
_MAX_YAML_BYTES = 1024 * 1024
_MAX_YAML_KEYS = 1000
_MAX_KEYS = 5000  # metrics per run
_MAX_WALK_DEPTH = 12
_MAX_FILES = 10000  # runs found by a directory search
_MAX_FLATTEN_DEPTH = 4
# Anything before 2000 is not a Unix timestamp (it is an elapsed time or a counter).
_MIN_TIME = 946684800.0
_MAX_TIME = 253402300799.0  # 9999-12-31T23:59:59Z

# trainer_state.json fields that describe the run setup (kept as config).
_HF_CONFIG_KEYS = (
    "train_batch_size",
    "max_steps",
    "num_train_epochs",
    "logging_steps",
    "eval_steps",
    "save_steps",
)

_CHECKPOINT_RE = re.compile(r"^checkpoint-(\d{1,12})$")
_INT_RE = re.compile(r"^[+-]?\d{1,20}$")


def _safe(text) -> str:
    """``text`` with control and other non-printable characters escaped (it is shown on a terminal)."""
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in str(text))


def _warn(options: ImportOptions, message: str) -> None:
    options.warn(_safe(message))


class _TableError(ValueError):
    """One file cannot be read as a table."""

    def __init__(self, message: str) -> None:
        super().__init__(_safe(message))


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------


def _to_float(value) -> float | None:
    """A number (or numeric text) as float, else None. Bools are not numbers here."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip()
            if not text or len(text) > 64 or "_" in text:
                return None
            return float(text)
    except (ValueError, OverflowError):
        return None
    return None


def _parse_step(value) -> int | None:
    """A whole number from a number or text, else None (the row is skipped)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _INT_RE.match(text):
            return int(text)
    number = _to_float(value)
    if number is None or not math.isfinite(number) or number != int(number):
        return None
    return int(number)


def _scale_time(number: float) -> float | None:
    """Seconds from a number that may be seconds, ms, us or ns (by magnitude)."""
    if not math.isfinite(number) or number <= 0:
        return None
    if number >= 1e17:
        number /= 1e9
    elif number >= 1e14:
        number /= 1e6
    elif number >= 1e11:
        number /= 1e3
    return number if _MIN_TIME <= number < _MAX_TIME else None


_FRACTION = re.compile(r"(?<=\d:\d\d)[.,](\d+)")


def _six_digits(match) -> str:
    """Python 3.10 accepts only 3 or 6 fraction digits and a '.': normalise to 6 digits."""
    return "." + match.group(1)[:6].ljust(6, "0")


def _parse_time(value) -> float | None:
    """Unix seconds from a number, numeric text or an ISO 8601 string, else None."""
    if value is None or isinstance(value, bool):
        return None
    number = _to_float(value)
    if number is not None:
        return _scale_time(number)
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 64:
            return None
        try:
            text = _FRACTION.sub(_six_digits, text.replace("Z", "+00:00").replace("z", "+00:00"))
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return _scale_time(parsed.timestamp())
        except (ValueError, OverflowError, OSError):
            return None
    return None


# ---------------------------------------------------------------------------
# Row sources: each yields (row_index, step | None, wall_time | None, {key: value})
# ---------------------------------------------------------------------------


class _Tally:
    """What one pass over a file saw; the second pass turns it into warnings."""

    def __init__(self) -> None:
        self.numeric: set[str] = set()
        self.nonnumeric: dict[str, int] = {}
        self.no_step = 0
        self.bad_rows = 0
        self.saw_step_col = False
        self.saw_time_col = False
        self.unstepped = 0  # JSONL lines without a step key
        # JSONL, default columns: which candidates appear in any line, and which keys the
        # lines actually used (more than one means the choice differed between lines).
        self.step_present: set[str] = set()
        self.time_present: set[str] = set()
        self.step_picked: set[str] = set()
        self.time_picked: set[str] = set()
        self.keys: set[str] = set()
        self.over: set[str] = set()

    def admit(self, key: str) -> bool:
        if key in self.keys:
            return True
        if len(self.keys) >= _MAX_KEYS:
            if len(self.over) < 100000:
                self.over.add(key)
            return False
        self.keys.add(key)
        return True

    def value(self, key: str, raw) -> float | None:
        """The float for ``raw``, or None; empty cells are neither numeric nor counted."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        number = _to_float(raw)
        if number is None:
            self.nonnumeric[key] = self.nonnumeric.get(key, 0) + 1
            return None
        self.numeric.add(key)
        return number

    def ignored_columns(self) -> list[str]:
        return sorted(key for key in self.nonnumeric if key not in self.numeric)


def _match_column(header: list[str], wanted: str) -> int | None:
    for index, name in enumerate(header):
        if name == wanted:
            return index
    lowered = wanted.lower()
    for index, name in enumerate(header):
        if name.lower() == lowered:
            return index
    return None


def _first_default(header: list[str], candidates: tuple[str, ...]) -> int | None:
    for candidate in candidates:
        index = _match_column(header, candidate)
        if index is not None:
            return index
    return None


def _shown(names: list[str], limit: int = 10) -> str:
    text = ", ".join(repr(name) for name in names[:limit])
    return text + (f", ... ({len(names)} columns)" if len(names) > limit else "")


class _CsvTable:
    def __init__(self, path: Path, step_column: str | None, time_column: str | None) -> None:
        self.path = path
        self.header: list[str] = []
        try:
            with self._open() as handle:
                for record in csv.reader(self._clean(handle)):
                    if any(cell.strip() for cell in record):
                        self.header = [cell.strip() for cell in record]
                        break
        except (csv.Error, OSError) as exc:
            raise _TableError(f"cannot read {path.name}: {exc}") from None
        if not self.header:
            raise _TableError(f"{path.name} is empty (no header row)")

        if step_column:
            self.step_idx = _match_column(self.header, step_column)
            if self.step_idx is None:
                raise _TableError(
                    f"{path.name} has no column {step_column!r} (columns: {_shown(self.header)})"
                )
        else:
            self.step_idx = _first_default(self.header, STEP_COLUMNS)
        if time_column:
            self.time_idx = _match_column(self.header, time_column)
            if self.time_idx is None:
                raise _TableError(
                    f"{path.name} has no column {time_column!r} (columns: {_shown(self.header)})"
                )
        else:
            self.time_idx = _first_default(self.header, TIME_COLUMNS)
        self.has_time_source = self.time_idx is not None

        self.columns: list[tuple[int, str]] = []
        seen: set[str] = set()
        self.truncated = 0
        for index, name in enumerate(self.header):
            if index in (self.step_idx, self.time_idx) or not name or name in seen:
                continue
            seen.add(name)
            if len(self.columns) >= _MAX_KEYS:
                self.truncated += 1
                continue
            self.columns.append((index, name))

    def _open(self):
        return open(self.path, "r", encoding="utf-8-sig", errors="replace", newline="")

    def _clean(self, handle):
        """Lines of the file, NULs dropped; a line over the cap is an error (not read into memory)."""
        while True:
            line = handle.readline(_MAX_LINE + 1)
            if not line:
                return
            if len(line) > _MAX_LINE and not line.endswith(("\n", "\r")):
                raise _TableError(f"{self.path.name} has a line longer than {_MAX_LINE // (1024 * 1024)} MiB")
            yield line.replace("\0", "")

    def rows(self, tally: _Tally) -> Iterator[tuple[int, int | None, float | None, dict]]:
        tally.saw_step_col = self.step_idx is not None
        tally.saw_time_col = self.time_idx is not None
        try:
            with self._open() as handle:
                reader = csv.reader(self._clean(handle))
                header_seen = False
                index = -1
                for record in reader:
                    if not any(cell.strip() for cell in record):
                        continue
                    if not header_seen:
                        header_seen = True
                        continue
                    index += 1
                    size = len(record)
                    if self.step_idx is None:
                        step = index
                    else:
                        step = _parse_step(record[self.step_idx] if self.step_idx < size else "")
                    wall = None
                    if self.time_idx is not None and self.time_idx < size:
                        wall = _parse_time(record[self.time_idx])
                    values: dict[str, float] = {}
                    if step is None:
                        tally.no_step += 1
                    else:
                        for col, name in self.columns:
                            if col >= size:
                                continue
                            number = tally.value(name, record[col])
                            if number is not None:
                                values[name] = number
                    yield index, step, wall, values
        except csv.Error as exc:
            raise _TableError(f"{self.path.name} is not valid CSV: {exc}") from None


def _lines(handle) -> Iterator[str | None]:
    """Text lines; a line over the cap is skipped (yielded as None)."""
    while True:
        line = handle.readline(_MAX_LINE + 1)
        if not line:
            return
        if len(line) > _MAX_LINE and not line.endswith("\n"):
            while True:
                rest = handle.readline(_MAX_LINE)
                if not rest or rest.endswith("\n"):
                    break
            yield None
            continue
        yield line


def _flatten(obj: dict, out: dict, prefix: str = "", depth: int = 0) -> None:
    for key, value in obj.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            if depth + 1 < _MAX_FLATTEN_DEPTH:
                _flatten(value, out, f"{name}/", depth + 1)
            continue
        out[name] = value


class _JsonlTable:
    has_time_source = False  # unknown until rows are read; see _Tally.saw_time_col

    def __init__(self, path: Path, step_column: str | None, time_column: str | None) -> None:
        self.path = path
        self.step_column = step_column
        self.time_column = time_column
        # True once some line is known to carry a step: lines without one are then skipped
        # (as in CSV) instead of taking their row number. Set by _build_run after pass 1.
        self.has_step = False
        # Default step/time columns, narrowed to one name per file by _build_run after pass 1.
        self.step_defaults: tuple[str, ...] = STEP_COLUMNS
        self.time_defaults: tuple[str, ...] = TIME_COLUMNS

    def _pick(self, flat: dict, explicit: str | None, defaults: tuple[str, ...]):
        """(key, value) of the column to use in this line, or (None, None)."""
        if explicit:
            if explicit in flat:
                return explicit, flat[explicit]
            lowered = explicit.lower()
            for key in flat:
                if key.lower() == lowered:
                    return key, flat[key]
            return None, None
        lowered = {key.lower(): key for key in flat}
        for candidate in defaults:
            key = lowered.get(candidate)
            if key is not None:
                return key, flat[key]
        return None, None

    def rows(self, tally: _Tally) -> Iterator[tuple[int, int | None, float | None, dict]]:
        index = -1
        try:
            with open(self.path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
                for line in _lines(handle):
                    if line is None:
                        tally.bad_rows += 1
                        continue
                    text = line.strip().replace("\0", "")
                    if not text:
                        continue
                    try:
                        obj = json.loads(text)
                    except (ValueError, RecursionError):
                        tally.bad_rows += 1
                        continue
                    if not isinstance(obj, dict):
                        tally.bad_rows += 1
                        continue
                    flat: dict = {}
                    _flatten(obj, flat)
                    index += 1
                    step_key, step_raw = self._pick(flat, self.step_column, self.step_defaults)
                    time_key, time_raw = self._pick(flat, self.time_column, self.time_defaults)
                    if not self.step_column or not self.time_column:
                        lowered = {key.lower() for key in flat}
                        if not self.step_column:
                            tally.step_present.update(c for c in STEP_COLUMNS if c in lowered)
                            if step_key is not None:
                                tally.step_picked.add(step_key.lower())
                        if not self.time_column:
                            tally.time_present.update(c for c in TIME_COLUMNS if c in lowered)
                            if time_key is not None:
                                tally.time_picked.add(time_key.lower())
                    if step_key is not None:
                        tally.saw_step_col = True
                    else:
                        tally.unstepped += 1
                    if time_key is not None:
                        tally.saw_time_col = True
                    if step_key is None:
                        step = None if self.has_step else index
                    else:
                        step = _parse_step(step_raw)
                    wall = _parse_time(time_raw) if time_key is not None else None
                    values: dict[str, float] = {}
                    if step is None:
                        tally.no_step += 1
                    else:
                        for key, raw in flat.items():
                            if key in (step_key, time_key) or not tally.admit(key):
                                continue
                            number = tally.value(key, raw)
                            if number is not None:
                                values[key] = number
                    yield index, step, wall, values
        except OSError as exc:
            raise _TableError(f"cannot read {self.path.name}: {exc}") from None


class _HfTable:
    """Rows from the ``log_history`` of a Hugging Face ``trainer_state.json``."""

    has_time_source = False

    def __init__(self, path: Path, history: list) -> None:
        self.path = path
        self.history = history

    def rows(self, tally: _Tally) -> Iterator[tuple[int, int | None, float | None, dict]]:
        index = -1
        for entry in self.history:
            if not isinstance(entry, dict):
                tally.bad_rows += 1
                continue
            index += 1
            step = _parse_step(entry.get("step"))
            values: dict[str, float] = {}
            if step is None:
                tally.no_step += 1
            else:
                for key, raw in entry.items():
                    if key == "step" or not isinstance(key, str) or not tally.admit(key):
                        continue
                    number = tally.value(key, raw)
                    if number is not None:
                        values[key] = number
            yield index, step, None, values


# ---------------------------------------------------------------------------
# hparams.yaml (flat keys only; never yaml.load)
# ---------------------------------------------------------------------------

_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+(?=[eE]))([eE][+-]?\d+)?$")
_YAML_INT_RE = re.compile(r"^[+-]?\d{1,18}$")


def _strip_comment(text: str) -> str:
    if text[:1] in ('"', "'"):
        quote = text[0]
        end = 1
        while end < len(text):
            if text[end] == "\\" and quote == '"':
                end += 2
                continue
            if text[end] == quote:
                if quote == "'" and text[end + 1 : end + 2] == "'":
                    end += 2
                    continue
                return text[: end + 1]
            end += 1
        return text
    match = re.search(r"\s#", text)
    return text[: match.start()] if match else text


def _yaml_scalar(rest: str):
    """(ok, value). Unsupported syntax (tags, anchors, block scalars) is not ok."""
    text = _strip_comment(rest.strip()).strip()
    if not text or text in ("~",) or text.lower() == "null":
        return True, None
    first = text[0]
    if first in "!&*|>%@`":
        return False, None
    if first == '"':
        if len(text) < 2 or not text.endswith('"'):
            return False, None
        try:
            return True, json.loads(text)
        except ValueError:
            return True, text[1:-1]
    if first == "'":
        if len(text) < 2 or not text.endswith("'"):
            return False, None
        return True, text[1:-1].replace("''", "'")
    if first in "[{":
        try:
            return True, json.loads(text)
        except (ValueError, RecursionError):
            return True, text
    lowered = text.lower()
    if lowered in ("true", "false"):
        return True, lowered == "true"
    if _YAML_INT_RE.match(text):
        return True, int(text)
    if _FLOAT_RE.match(text):
        try:
            number = float(text)
        except ValueError:
            return True, text
        return True, number
    if lowered in (".inf", "+.inf", "-.inf", ".nan"):
        return True, {".inf": math.inf, "+.inf": math.inf, "-.inf": -math.inf}.get(lowered, math.nan)
    return True, text


def _yaml_key(line: str) -> tuple[str, str] | None:
    """Split ``key: rest`` at the first ``:`` that ends the key."""
    if line[0] in ('"', "'"):
        quote = line[0]
        end = 1
        while end < len(line):
            if line[end] == "\\" and quote == '"':
                end += 2
                continue
            if line[end] == quote:
                break
            end += 1
        else:
            return None
        key = line[1:end]
        rest = line[end + 1 :].lstrip()
        if not rest.startswith(":"):
            return None
        return key, rest[1:]
    if line[0] in "?[{!&*|>%@`#":
        return None
    match = re.search(r":(\s|$)", line)
    if match is None:
        return None
    key = line[: match.start()].strip()
    return (key, line[match.end() :]) if key else None


def parse_flat_yaml(text: str) -> tuple[dict, int]:
    """Top-level ``key: scalar`` entries of a YAML text, and how many entries were skipped.

    Nested mappings, lists, anchors, tags and block scalars are skipped, one count per
    top-level key (its continuation lines are not counted). A plain scalar that continues
    on indented lines is skipped as a whole rather than kept truncated.
    """
    out: dict = {}
    skipped = 0
    lines = text.splitlines()
    last_key: str | None = None  # the accepted key the previous entry produced, if any
    absorbing = False  # the previous entry was skipped: its continuation lines belong to it
    for position, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
            continue
        if line[0] in " \t-":
            # Continuation of the previous entry (a nested value, or a folded scalar), or a stray item.
            if absorbing:
                continue
            if last_key is not None:
                out.pop(last_key, None)
                last_key = None
            skipped += 1
            absorbing = True
            continue
        last_key = None
        absorbing = True
        split = _yaml_key(line)
        if split is None:
            skipped += 1
            continue
        key, rest = split
        if not _strip_comment(rest.strip()).strip():
            # "key:" is null, unless an indented or "- " block follows.
            nested = False
            for following in lines[position + 1 :]:
                follow = following.strip()
                if not follow or follow.startswith("#"):
                    continue
                nested = following[0] in " \t" or follow == "-" or follow.startswith("- ")
                break
            if nested:
                skipped += 1
                continue
        ok, value = _yaml_scalar(rest)
        if not ok:
            skipped += 1
            continue
        if len(out) >= _MAX_YAML_KEYS and key not in out:
            skipped += 1
            continue
        out[key] = value
        last_key = key
        absorbing = False
    return out, skipped


def _read_small_text(path: Path, limit: int) -> str | None:
    """The text of a regular, non-symlink file up to ``limit`` bytes, else None."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with open(path, "rb") as handle:
            return handle.read(limit + 1).decode("utf-8", "replace")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Finding files
# ---------------------------------------------------------------------------


def _is_lightning_dir(directory: Path) -> bool:
    return directory.name.startswith("version_") or (directory / HPARAMS_FILE).is_file()


def _find_files(root: Path, options: ImportOptions) -> list[tuple[Path, str]]:
    """``(file, kind)`` for every metrics.csv and trainer_state.json under ``root``."""
    found: list[tuple[Path, str]] = []
    base_depth = len(root.parts)
    truncated = False
    too_deep = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        if len(here.parts) - base_depth >= _MAX_WALK_DEPTH:
            if any(not d.startswith(".") for d in dirnames):
                too_deep = True
            dirnames[:] = []
        else:
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for filename in sorted(filenames):
            if filename not in (LIGHTNING_FILE, HF_FILE):
                continue
            candidate = here / filename
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if len(found) >= _MAX_FILES:
                truncated = True
                break
            found.append((candidate, "lightning" if filename == LIGHTNING_FILE else "hf"))
        if truncated:
            break
    if truncated:
        _warn(options, f"only the first {_MAX_FILES} files were searched; more were left out")
    if too_deep:
        _warn(options, f"directories deeper than {_MAX_WALK_DEPTH} levels were not searched")
    return _drop_duplicate_checkpoints(found)


def _drop_duplicate_checkpoints(found: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """Hugging Face saves a growing trainer_state.json into every ``checkpoint-N``.

    Keep the output directory's own file when it exists, else the newest checkpoint.
    """
    own = {path.parent for path, kind in found if kind == "hf" and not _CHECKPOINT_RE.match(path.parent.name)}
    best: dict[Path, tuple[int, Path]] = {}
    for path, kind in found:
        match = _CHECKPOINT_RE.match(path.parent.name) if kind == "hf" else None
        if match is None:
            continue
        output_dir = path.parent.parent
        number = int(match.group(1))
        if output_dir not in best or number > best[output_dir][0]:
            best[output_dir] = (number, path)
    keep: list[tuple[Path, str]] = []
    for path, kind in found:
        if kind == "hf" and _CHECKPOINT_RE.match(path.parent.name):
            output_dir = path.parent.parent
            if output_dir in own or best[output_dir][1] != path:
                continue
        keep.append((path, kind))
    return keep


# ---------------------------------------------------------------------------
# Building runs
# ---------------------------------------------------------------------------


def _wanted(options: ImportOptions, name: str, source_run_id: str) -> bool:
    if not options.run_globs:
        return True
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for pattern in options.run_globs
        for candidate in (name, source_run_id)
    )


def _cap_list(names: list[str], limit: int = 8) -> str:
    text = ", ".join(repr(name) for name in names[:limit])
    return text + (f" (+{len(names) - limit} more)" if len(names) > limit else "")


def _build_run(
    table,
    *,
    name: str,
    project: str,
    location: Path,
    config: dict,
    options: ImportOptions,
) -> ImportedRun:
    """Pass 1 over ``table`` (start time, constant-step keys); pass 2 streams the points."""
    # Cheap mtime read first: a missing/vanished file fails here, as a source error.
    mtime = min(max(os.stat(location).st_mtime, 0.0), _MAX_TIME - 1)

    def first_pass() -> tuple[_Tally, float | None, dict[str, list]]:
        scan = _Tally()
        first_time: float | None = None
        per_key: dict[str, list] = {}  # key -> [count, first step, still one step]
        for _index, step, wall, values in table.rows(scan):
            if first_time is None and wall is not None:
                first_time = wall
            if step is None:
                continue
            for key in values:
                info = per_key.get(key)
                if info is None:
                    per_key[key] = [1, step, True]
                else:
                    info[0] += 1
                    if step != info[1]:
                        info[2] = False
        return scan, first_time, per_key

    scan, first_time, per_key = first_pass()
    if isinstance(table, _JsonlTable):
        for option, seen in ((options.step_column, scan.saw_step_col), (options.time_column, scan.saw_time_col)):
            if option and not seen:
                raise _TableError(f"{location.name} has no column {option!r} in any line")
        # Like CSV, the default step and time column is chosen once per file (the first of
        # step, global_step, epoch / timestamp, time, wall_time found in any line), not per
        # line: a line without it is skipped, and e.g. `epoch` stays a metric.
        mixed = False
        if not options.step_column:
            for candidate in STEP_COLUMNS:
                if candidate in scan.step_present:
                    table.step_defaults = (candidate,)
                    break
            mixed = mixed or len(scan.step_picked) > 1
        if not options.time_column:
            for candidate in TIME_COLUMNS:
                if candidate in scan.time_present:
                    table.time_defaults = (candidate,)
                    break
            mixed = mixed or len(scan.time_picked) > 1
        if scan.saw_step_col and (scan.unstepped or mixed):
            # Some lines carry a step and some do not: the others are skipped (as in CSV),
            # not numbered by row. Read again, now that the file is known to have steps.
            table.has_step = True
        if (scan.saw_step_col and scan.unstepped) or mixed:
            scan, first_time, per_key = first_pass()
    constant = {key for key, (count, _step, single) in per_key.items() if single and count > 1}

    if first_time is None:
        started = mtime
        if scan.saw_time_col or table.has_time_source:
            _warn(
                options,
                f"{name}: the time column has no usable timestamps (expected Unix seconds, "
                "milliseconds or ISO 8601); using the file's modification time"
            )
        else:
            _warn(
                options,
                f"{name}: no time column; the run's start time and every point's time "
                "come from the file's modification time",
            )
    else:
        started = first_time

    start_dt = datetime.fromtimestamp(started, timezone.utc)

    def points():
        tally = _Tally()
        wall_now = started
        rank: dict[str, int] = {}
        for _index, step, wall, values in table.rows(tally):
            if wall is not None:
                wall_now = wall
            if step is None or not values:
                continue
            if constant and not constant.isdisjoint(values):
                # Steps cannot order a series that sits on one step: number it in file order.
                by_step: dict[int, dict] = {}
                for key, value in values.items():
                    if key in constant:
                        target = rank.get(key, 0)
                        rank[key] = target + 1
                    else:
                        target = step
                    by_step.setdefault(target, {})[key] = value
                for target, group in by_step.items():
                    yield wall_now, target, group
            else:
                yield wall_now, step, values
        _warn_tally(options, name, tally, table)

    return ImportedRun(
        source=SOURCE,
        location=str(location),
        source_run_id=location.name,
        name=name,
        project=project,
        started_at=start_dt,
        ended_at=None,
        status="finished",
        source_status=None,
        config=config,
        summary={},
        tags=[],
        points=points(),
        media=(),
        renumbered_steps=sorted(constant),
    )


def _warn_tally(options: ImportOptions, name: str, tally: _Tally, table) -> None:
    ignored = tally.ignored_columns()
    if ignored:
        _warn(options, f"{name}: ignored {len(ignored)} non-numeric column(s): {_cap_list(ignored)}")
    if tally.no_step:
        _warn(options, f"{name}: skipped {tally.no_step} row(s) without a usable step")
    if tally.bad_rows:
        _warn(options, f"{name}: skipped {tally.bad_rows} line(s) that are not a JSON object or too long")
    over = len(tally.over) + getattr(table, "truncated", 0)
    if over:
        _warn(options, f"{name}: more than {_MAX_KEYS} metrics; kept the first {_MAX_KEYS}")


def _load_hf(path: Path) -> tuple[list, dict]:
    """``(log_history, setup fields)`` of a trainer_state.json, parsed once."""
    try:
        info = os.stat(path)
        if info.st_size > _MAX_JSON_BYTES:
            limit = (
                f"{_MAX_JSON_BYTES // (1024 * 1024)} MiB"
                if _MAX_JSON_BYTES >= 1024 * 1024
                else f"{_MAX_JSON_BYTES} bytes"
            )
            raise _TableError(f"{path.name} is larger than {limit}")
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8-sig", "replace"))
    except _TableError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise _TableError(f"{path.name} is not valid JSON: {exc}") from None
    history = data.get("log_history") if isinstance(data, dict) else None
    if not isinstance(history, list):
        raise _TableError(f"{path.name} has no log_history list (is it a Hugging Face trainer_state.json?)")
    config = {}
    for key in _HF_CONFIG_KEYS:
        value = data.get(key)
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            config[key] = value
    return history, config


def _lightning_config(file: Path, options: ImportOptions, name: str) -> dict:
    text = _read_small_text(file.parent / HPARAMS_FILE, _MAX_YAML_BYTES)
    if text is None:
        return {}
    config, skipped = parse_flat_yaml(text)
    if skipped:
        _warn(options, f"{name}: {HPARAMS_FILE}: skipped {skipped} nested or unsupported entries")
    return config


def _one_file(
    file: Path,
    kind: str,
    *,
    name: str,
    project: str,
    options: ImportOptions,
) -> ImportedRun | None:
    """A run for one file, or None when the ``--run`` globs exclude it."""
    if not _wanted(options, name, file.name):
        return None
    if kind == "hf":
        history, config = _load_hf(file)
        if options.step_column or options.time_column:
            _warn(options, f"{name}: --step-column and --time-column do not apply to {HF_FILE}")
        table = _HfTable(file, history)
    elif kind == "jsonl":
        table = _JsonlTable(file, options.step_column, options.time_column)
        config = {}
    else:
        table = _CsvTable(file, options.step_column, options.time_column)
        config = _lightning_config(file, options, name) if kind == "lightning" else {}
    return _build_run(table, name=name, project=project, location=file, config=config, options=options)


def _kind_of_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "lightning" if path.name == LIGHTNING_FILE and _is_lightning_dir(path.parent) else "csv"
    if suffix in _JSONL_SUFFIXES:
        return "jsonl"
    if suffix == ".json":
        return "hf"
    raise ImportSourceError(
        f"{path} is not a .csv or .jsonl file, a trainer_state.json, or a directory "
        f"with {LIGHTNING_FILE} / {HF_FILE} files"
    )


def _names_for(file: Path, kind: str, root: Path | None) -> tuple[str, str]:
    """``(name, project)`` of the run for ``file`` (D7).

    A Lightning version directory or a Hugging Face output directory is the run:
    its name is the directory (relative to the searched root), its project the
    directory above it. A plain table is named after its file, in its directory's project.
    """
    if kind not in ("lightning", "hf"):
        return file.stem, file.parent.name or file.stem
    run_dir = file.parent
    if kind == "hf" and root is not None and run_dir != root and _CHECKPOINT_RE.match(run_dir.name):
        run_dir = run_dir.parent  # the output directory that holds checkpoint-N
    if root is not None and run_dir != root and run_dir.is_relative_to(root):
        name = run_dir.relative_to(root).as_posix()
    else:
        name = run_dir.name or file.stem
    return name, run_dir.parent.name or run_dir.name


def _failed_run(file: Path, name: str, project: str, exc: Exception) -> ImportedRun:
    """A run whose points raise ``exc``, so the core records a failed row for ``file``."""
    try:
        stamp = min(max(os.stat(file).st_mtime, 0.0), _MAX_TIME - 1)
    except OSError:
        stamp = _MIN_TIME
    message = str(exc) if isinstance(exc, _TableError) else f"cannot read {file.name}: {exc}"

    def points():
        raise _TableError(message)
        yield  # pragma: no cover - makes this a generator

    return ImportedRun(
        source=SOURCE,
        location=str(file),
        source_run_id=file.name,
        name=name,
        project=project,
        started_at=datetime.fromtimestamp(stamp, timezone.utc),
        ended_at=None,
        status="finished",
        source_status=None,
        config={},
        summary={},
        tags=[],
        points=points(),
        media=(),
    )


def discover(path: str, options: ImportOptions) -> Iterator[ImportedRun]:
    """Yield one run per table file under ``path`` (see the module docstring)."""
    try:
        root = Path(path).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ImportSourceError(f"cannot resolve {path}: {exc}") from None
    if not root.exists():
        raise ImportSourceError(f"{path} does not exist")

    if root.is_file():
        kind = _kind_of_file(root)
        name, project = _names_for(root, kind, None)
        try:
            run = _one_file(root, kind, name=name, project=project, options=options)
        except _TableError as exc:
            raise ImportSourceError(str(exc)) from None
        except OSError as exc:
            raise ImportSourceError(f"cannot read {root}: {exc}") from None
        if run is not None:
            yield run
        return

    if not root.is_dir():
        raise ImportSourceError(f"{path} is not a file or a directory")
    files = _find_files(root, options)
    if not files:
        raise ImportSourceError(
            f"no {LIGHTNING_FILE} or {HF_FILE} files under {path} "
            "(for a plain table, pass the .csv or .jsonl file itself)"
        )
    for file, kind in files:
        name, project = _names_for(file, kind, root)
        try:
            run = _one_file(file, kind, name=name, project=project, options=options)
        except (_TableError, OSError) as exc:
            # A failed row for this file; the other files still import.
            run = _failed_run(file, name, project, exc)
        if run is not None:
            yield run

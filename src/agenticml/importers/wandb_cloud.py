"""Import runs from Weights & Biases cloud (``agenticml import wandb ENTITY/PROJECT[/RUN_ID]``).

Needs the optional ``wandb`` package (``pip install 'agenticml-lite[wandb]'``). It
talks to W&B only through ``wandb.Api()``, which finds the user's own credentials
(``WANDB_API_KEY`` or ``wandb login``); this module never reads, passes or stores
the key. Per run it uses ``run.scan_history()`` for the points, ``run.config``,
``run.summary``, ``run.created_at``, ``run.state`` and ``run.file(path).download()``
for images.

Everything the server sends is untrusted: an image path is checked before it is
handed to ``download`` and the file is read back from a private temporary
directory only when it stays inside it, is a regular file, is small enough and
starts with image magic bytes.

Mapping to :class:`~agenticml.importers.ImportedRun`: the start time is the
run's ``created_at`` (UTC), the end time comes from the summary's
``_timestamp`` (else ``created_at + _runtime``, else the last heartbeat), the
status from ``run.state`` (``finished`` -> finished; ``crashed`` / ``failed`` /
``killed`` / ``preempted`` -> crashed; anything else, ``running`` included ->
running, which the core skips unless ``--include-running``). Scalars,
nested dicts of scalars (keys joined with ``.``) and ``image-file`` values are
imported; histograms, tables, videos, lists and other typed values are skipped
and counted. System metrics are not part of the history API and are not
imported.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import re
import stat
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from . import ImportedRun, ImportOptions, ImportSourceError
from ._media import sniff_image_type

logger = logging.getLogger("agenticml.import")

SOURCE = "wandb"
INSTALL_HINT = "W&B cloud import needs `pip install 'agenticml-lite[wandb]'`"
_CLOUD_PATH = re.compile(r"[\w.-]+/[\w.-]+(/[\w.-]+)?")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

MAX_MEDIA_BYTES = 64 * 1024 * 1024
_MAX_DEPTH = 32
_CRASHED_STATES = frozenset({"crashed", "failed", "killed", "preempted"})
_NONFINITE = {"nan": math.nan, "infinity": math.inf, "-infinity": -math.inf, "inf": math.inf, "-inf": -math.inf}


# ---------------------------------------------------------------------------
# entry point


def parse_path(path: str) -> tuple[str, str, str | None]:
    """``(entity, project, run_id or None)`` from ``ENTITY/PROJECT[/RUN_ID]``."""
    if not _CLOUD_PATH.fullmatch(path or ""):
        raise ImportSourceError(f"{path!r} is not a W&B cloud path (expected ENTITY/PROJECT[/RUN_ID])")
    parts = path.split("/")
    if any(part in (".", "..") for part in parts):
        raise ImportSourceError(f"{path!r} is not a W&B cloud path (expected ENTITY/PROJECT[/RUN_ID])")
    return parts[0], parts[1], (parts[2] if len(parts) > 2 else None)


def _load_wandb():
    try:
        import wandb
    except ImportError:
        raise ImportSourceError(INSTALL_HINT) from None
    # A local ``wandb/`` run directory in the working directory is importable as a namespace
    # package when the real package is missing; it has no ``Api``.
    if not callable(getattr(wandb, "Api", None)):
        raise ImportSourceError(INSTALL_HINT)
    return wandb


def discover(path: str, options: ImportOptions) -> Iterator[ImportedRun]:
    """Yield one :class:`ImportedRun` per W&B run of ``ENTITY/PROJECT`` (or the one ``RUN_ID``)."""
    entity, project, run_id = parse_path(path)
    wandb = _load_wandb()
    try:
        api = wandb.Api()
    except Exception as exc:  # no credentials, bad settings, no network ...
        logger.debug("wandb.Api() failed", exc_info=True)
        raise ImportSourceError(
            f"cannot start the W&B API ({_describe(exc)}); set WANDB_API_KEY or run `wandb login`"
        ) from None
    host = _host(api)
    try:
        # The first fetch is done here so a missing project or no access is one clear error.
        runs = iter([api.run(path)] if run_id is not None else api.runs(f"{entity}/{project}"))
        first = next(runs, None)
    except Exception as exc:
        logger.debug("W&B lookup of %s failed", path, exc_info=True)
        raise ImportSourceError(f"cannot read W&B {path}: {_describe(exc)}") from None
    return _discover(first, runs, host, entity, project, options)


def _discover(first, rest, host: str, entity: str, project: str, options: ImportOptions) -> Iterator[ImportedRun]:
    if first is None:
        options.warn(f"no runs found in W&B project {entity}/{project}")
        return
    for run in itertools.chain([first], rest):
        try:
            yield _build(run, host, entity, project, options)
        except Exception as exc:  # one odd run must not stop the others
            logger.debug("cannot read a W&B run", exc_info=True)
            yield _failed_run(f"wandb://{host}/{entity}/{project}", _safe_id(run), _safe_id(run) or "?", exc)


def _safe_id(run) -> str:
    try:
        return _text(getattr(run, "id", None))
    except Exception:
        return ""


def _describe(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _host(api) -> str:
    """Host of the W&B server (so two servers with the same ENTITY/PROJECT get different run ids)."""
    base = None
    settings = getattr(api, "settings", None)
    if isinstance(settings, Mapping):
        value = settings.get("base_url")
        base = value if isinstance(value, str) else None
    base = base or os.environ.get("WANDB_BASE_URL") or ""
    try:
        host = urlparse(base).hostname if base else None
    except ValueError:
        host = None
    return host or "wandb.ai"


# ---------------------------------------------------------------------------
# value helpers


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except OverflowError:
            return None
    if isinstance(value, str):
        # The history API writes NaN and infinities as strings.
        return _NONFINITE.get(value.strip().lower())
    return None


def _plain_dict(value) -> dict:
    """``value`` as a plain dict, or ``{}`` when it is not a mapping (or its getters fail)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, Mapping):
        try:
            return dict(value.items())
        except Exception:
            return {}
    return {}


def _summary_dict(run) -> dict:
    for attr in ("summary_metrics", "summary"):
        try:
            raw = getattr(run, attr)
        except Exception:
            continue
        inner = getattr(raw, "_json_dict", None)
        found = _plain_dict(inner if inner is not None else raw)
        if found:
            return found
    return {}


def _config_dict(run) -> dict:
    try:
        raw = run.config
    except Exception:
        return {}
    return {key: value for key, value in _plain_dict(raw).items() if key != "_wandb"}


def _plain_summary(tree: dict) -> tuple[dict, int]:
    """Numeric and string summary values (nested dicts flattened with ``.``); and how many were left out."""
    out: dict = {}
    skipped = 0

    def walk(key: str, value, depth: int) -> None:
        nonlocal skipped
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float, str)):
            out[key] = value
        elif isinstance(value, dict) and "_type" not in value and depth < _MAX_DEPTH:
            for sub, inner in value.items():
                walk(f"{key}.{sub}", inner, depth + 1)
        else:
            skipped += 1

    for key, value in tree.items():
        if isinstance(key, str) and not key.startswith("_"):
            walk(key, value, 0)
    return out, skipped


def _timestamp(value) -> datetime | None:
    """UTC datetime from W&B's ISO string (naive means UTC), a datetime or a Unix number."""
    try:
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value), timezone.utc)
        elif isinstance(value, str) and value.strip():
            text = value.strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            match = re.fullmatch(r"(.*?\d\d:\d\d:\d\d)\.(\d+)(.*)", text)
            if match:  # fromisoformat on 3.10 wants exactly 3 or 6 fraction digits
                text = f"{match[1]}.{match[2][:6].ljust(6, '0')}{match[3]}"
            if re.search(r"\d[T ]\d", text):  # an offset without a colon (-0500) is not ISO for 3.10
                text = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", text)
            result = datetime.fromisoformat(text)
        else:
            return None
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _media_limit() -> int:
    """The writer's per-key image limit (``AGENTICML_MEDIA_MAX_PER_KEY``, default 64)."""
    try:
        from ..store.writer import MEDIA_MAX_PER_KEY, _media_limit as limit

        return limit("AGENTICML_MEDIA_MAX_PER_KEY", MEDIA_MAX_PER_KEY)
    except ImportError:  # pragma: no cover - the store is part of this package
        return 64


def _safe_relative(rel: str) -> str | None:
    """``rel`` when it is a plain relative path with no ``..`` / absolute / drive / NUL parts, else None."""
    if not rel or "\x00" in rel or "\\" in rel or len(rel) > 1024:
        return None
    pure = PurePosixPath(rel)
    if pure.is_absolute() or re.match(r"^[A-Za-z]:", rel):
        return None
    parts = pure.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    safe = "/".join(parts)
    return safe if safe == rel else None  # "a//b" and "a/./b" are not what the server named


# ---------------------------------------------------------------------------
# one run


def _failed_run(location: str, source_run_id: str, name: str, error: Exception) -> ImportedRun:
    """A run whose points raise, so the core reports one failed run and writes nothing."""

    def points():
        raise error
        yield  # pragma: no cover - makes this a generator

    return ImportedRun(
        source=SOURCE,
        location=location,
        source_run_id=source_run_id,
        name=name,
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


def _build(run, host: str, entity: str, project: str, options: ImportOptions) -> ImportedRun:
    try:
        run_id = str(run.id)
    except Exception:
        run_id = ""
    run_entity = _text(getattr(run, "entity", None)) or entity
    run_project = _text(getattr(run, "project", None)) or project
    location = f"wandb://{host}/{run_entity}/{run_project}"
    name = _text(getattr(run, "name", None)) or run_id
    if not run_id:
        return _failed_run(location, "", name or "?", ValueError("the W&B run has no id"))

    started = _timestamp(getattr(run, "created_at", None))
    if started is None:
        return _failed_run(location, run_id, name, ValueError("the W&B run has no usable created_at"))

    state = _text(getattr(run, "state", None)).strip().lower()
    if state == "finished":
        status = "finished"
    elif state in _CRASHED_STATES:
        status = "crashed"
    else:
        status = "running"
        if state not in ("running", "pending", "preempting", "queued"):
            options.warn(f"{name}: unknown W&B run state {state!r}; treated as still running")

    summary_raw = _summary_dict(run)
    ended = _end_time(run, started, summary_raw) if status != "running" else None
    summary, skipped = _plain_summary(summary_raw)
    if skipped:
        options.warn(f"{name}: skipped {skipped} summary values (only numbers and text are imported)")
    tags = getattr(run, "tags", None)
    stream = _Stream(run, name, started, options)
    return ImportedRun(
        source=SOURCE,
        location=location,
        source_run_id=run_id,
        name=name,
        project=run_project,
        started_at=started,
        ended_at=ended,
        status=status,
        source_status=state or None,
        config=_config_dict(run),
        summary=summary,
        tags=[tag for tag in tags if isinstance(tag, str)] if isinstance(tags, (list, tuple)) else [],
        points=stream.points(),
        media=stream.media(),
        notes=_text(getattr(run, "notes", None)),
        hostname=_run_host(run),
    )


def _run_host(run) -> str | None:
    """The host the run's record names, when the API object has one."""
    try:
        return _text(getattr(run, "host", None)) or None
    except Exception:
        return None


def _end_time(run, started: datetime, summary: dict) -> datetime | None:
    candidates: list[datetime | None] = []
    stamp = _number(summary.get("_timestamp"))
    if stamp is not None and math.isfinite(stamp):
        candidates.append(_timestamp(stamp))
    runtime = _number(summary.get("_runtime"))
    if runtime is not None and math.isfinite(runtime) and runtime >= 0:
        try:
            candidates.append(_timestamp(started.timestamp() + runtime))
        except (ValueError, OverflowError, OSError):
            pass
    candidates.append(_timestamp(getattr(run, "heartbeat_at", None)))
    for candidate in candidates:
        if candidate is not None and candidate >= started:
            return candidate
    return None


class _Stream:
    """History of one run: scalar points, then image references, then the downloaded images."""

    def __init__(self, run, name: str, started: datetime, options: ImportOptions) -> None:
        self.run = run
        self.name = name
        self.started = started.timestamp()
        self.options = options
        # key -> (sequence, wall, step, path, caption); thinned like the writer does.
        self.refs: dict[str, list[tuple[int, float, int, str, str | None]]] = {}
        self.ref_count = 0
        self.skipped: Counter[str] = Counter()
        self.done = False

    # -- points

    def points(self) -> Iterator[tuple[float, int, dict[str, float]]]:
        self.refs = {}
        self.ref_count = 0
        self.skipped = Counter()
        last_wall = self.started
        position = 0
        for row in self.run.scan_history():
            if not isinstance(row, Mapping):
                continue
            wall = _number(row.get("_timestamp"))
            if wall is None or not math.isfinite(wall):
                wall = last_wall
            last_wall = wall
            step = self._step(row, position)
            position += 1
            metrics: dict[str, float] = {}
            for key, value in row.items():
                if isinstance(key, str) and not key.startswith("_"):
                    self._walk(key, value, metrics, wall, step, 0)
            if metrics:
                yield wall, step, metrics
        self.done = True
        for kind, count in sorted(self.skipped.items()):
            self.options.warn(f"{self.name}: skipped {count} {kind} values (only scalars and images are imported)")

    @staticmethod
    def _step(row: Mapping, position: int) -> int:
        value = row.get("_step")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return position

    def _walk(self, key: str, value, metrics: dict[str, float], wall: float, step: int, depth: int) -> None:
        if value is None:
            return
        if isinstance(value, str):
            # Plain text is not a metric; only the "NaN" / "Infinity" spellings are numbers.
            number = _number(value)
            if number is not None:
                metrics[key] = number
            return
        number = _number(value)
        if number is not None:
            metrics[key] = number
        elif isinstance(value, Mapping):
            kind = value.get("_type")
            if kind is None:
                if depth < _MAX_DEPTH:
                    for sub, inner in value.items():
                        if isinstance(sub, str):
                            self._walk(f"{key}.{sub}", inner, metrics, wall, step, depth + 1)
            elif kind == "image-file" and isinstance(value.get("path"), str):
                caption = value.get("caption")
                self._add_ref(key, wall, step, value["path"], caption if isinstance(caption, str) else None)
            else:
                self.skipped[str(kind)[:64]] += 1
        elif isinstance(value, (list, tuple)):
            self.skipped["list"] += 1

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
            for _ in self.points():
                pass
        ordered = sorted(
            (seq, wall, step, key, rel, caption)
            for key, rows in self.refs.items()
            for seq, wall, step, rel, caption in rows
        )
        if not ordered:
            return
        missing = 0
        with tempfile.TemporaryDirectory(prefix="agenticml-wandb-") as tmp:
            base = Path(tmp).resolve()
            for _seq, wall, step, key, rel, caption in ordered:
                found = self._fetch(base, rel)
                if found is None:
                    missing += 1
                    continue
                data, kind = found
                yield wall, step, key, data, kind, caption
        if missing:
            self.options.warn(
                f"{self.name}: skipped {missing} images that could not be downloaded, were too large, "
                "were not images, or had an unsafe path"
            )

    def _fetch(self, base: Path, rel: str) -> tuple[bytes, str] | None:
        safe = _safe_relative(rel)
        if safe is None:
            return None
        target = base / safe
        try:
            if not target.resolve().is_relative_to(base):
                return None
        except (OSError, ValueError, RuntimeError):
            return None
        try:
            handle = self.run.file(safe)
            size = getattr(handle, "size", None)
            if isinstance(size, int) and not isinstance(size, bool) and size > MAX_MEDIA_BYTES:
                return None
            opened = handle.download(root=str(base), replace=True)
        except Exception:
            logger.debug("download of %s failed", safe, exc_info=True)
            return None
        try:
            close = getattr(opened, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        try:
            return _read_image(base, target)
        finally:
            try:
                # Never delete through a symlinked directory the download may have planted.
                if target.parent.resolve().is_relative_to(base):
                    target.unlink()
            except (OSError, ValueError, RuntimeError):
                pass


def _read_image(base: Path, target: Path) -> tuple[bytes, str] | None:
    """``(bytes, content type)`` of ``target`` when it is a regular image file inside ``base``."""
    try:
        resolved = target.resolve()
        if not resolved.is_relative_to(base) or target.is_symlink():
            return None
        # O_NONBLOCK: a FIFO swapped in after the check must not hang the open.
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as fp:
            info = os.fstat(fp.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MEDIA_BYTES:
                return None
            head = fp.read(12)
            kind = sniff_image_type(head)
            if kind is None:
                return None
            data = head + fp.read(MAX_MEDIA_BYTES + 1 - len(head))
    except (OSError, ValueError, RuntimeError):
        return None
    return (data, kind) if len(data) <= MAX_MEDIA_BYTES else None

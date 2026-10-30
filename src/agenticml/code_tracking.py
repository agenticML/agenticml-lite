from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import logging
import mimetypes
import os
import re
import site
import stat
import sys
import sysconfig
import threading
import unicodedata
import weakref
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .ignore import IgnoreMatcher

logger = logging.getLogger("agenticml.code")

# Hard skip-list — these never get tracked even without an ignore file.
SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".agenticml",
    ".tox",
    ".venv",
    ".eggs",
    "__pycache__",
    "build",
    "dist",
    "env",
    "dist-packages",
    "pip-wheel-metadata",
    "site-packages",
    "venv",
    "__pypackages__",
    "node_modules",
}

# Packaging/install products can appear inside otherwise valid source roots,
# especially after editable installs and wheel builds. They are generated and
# must never enter a source snapshot, even when explicitly opened at runtime.
SKIP_PATH_SUFFIXES = (".egg", ".egg-info", ".dist-info")
SKIP_FILE_SUFFIXES = (".egg-link", ".whl", ".pyc", ".pyo")

PROJECT_MARKERS = ("pyproject.toml", "setup.py", ".git")
IGNORE_FILE_NAMES = (".gitignore", ".agenticmlignore")

# Default size caps — overridable via RuntimeFileCollector kwargs. Anything
# beyond this is dropped with a warning, so large datasets / model weights
# don't sneak into the snapshot.
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB per file
DEFAULT_MAX_TOTAL_BYTES = 100 * 1024 * 1024  # 100 MB total


@dataclass
class TrackedFile:
    absolute_path: Path
    relative_path: str
    source: str

    @property
    def size(self) -> int:
        return self.absolute_path.stat().st_size

    @property
    def sha256(self) -> str:
        digest = hashlib.sha256()
        with self.absolute_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(io.DEFAULT_BUFFER_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @property
    def content_type(self) -> str:
        guessed, _ = mimetypes.guess_type(self.absolute_path.name)
        return guessed or "application/octet-stream"


_AUDIT_LOCK = threading.RLock()
_ACTIVE_COLLECTORS: weakref.WeakSet[RuntimeFileCollector] = weakref.WeakSet()
_AUDIT_HOOK_INSTALLED = False


def _is_read_open(mode, flags) -> bool:
    """Return whether an ``open`` audit event represents an input read."""
    if isinstance(mode, str):
        # r and r+ are inputs. w/a/x modes are generated outputs even though
        # their '+' variants technically permit reads.
        return not mode or mode[0] == "r"
    if isinstance(flags, int):
        return not bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
    return False


def _audit_dispatch(event: str, args: tuple) -> None:
    if event != "open" or not args:
        return
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else None
    if not _is_read_open(mode, flags):
        return
    path = args[0]
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    with _AUDIT_LOCK:
        collectors = list(_ACTIVE_COLLECTORS)
    for collector in collectors:
        try:
            collector._record_path(path, "runtime_read")
        except Exception:
            # Audit hooks must never interfere with the application opening a
            # file, even if tracking encounters a malformed or transient path.
            continue


def _install_audit_hook() -> None:
    global _AUDIT_HOOK_INSTALLED
    with _AUDIT_LOCK:
        if _AUDIT_HOOK_INSTALLED:
            return
        sys.addaudithook(_audit_dispatch)
        _AUDIT_HOOK_INSTALLED = True


_SOURCE_PRIORITY = {
    "runtime_read": 0,
    "runtime_import": 1,
    "entrypoint": 2,
    "extra": 3,
    "model": 4,
    "optimizer": 4,
    "loss": 4,
    "dataloader": 4,
}


def _source_path(path: Path) -> Path:
    if path.suffix in {".pyc", ".pyo"}:
        try:
            source = Path(importlib.util.source_from_cache(str(path)))
        except (ValueError, NotImplementedError):
            return path
        if source.is_file():
            return source
    return path


class RuntimeFileCollector:
    """Collect project files imported or opened for reading during a run."""

    def __init__(
        self,
        entrypoint: str | os.PathLike[str] | None = None,
        *,
        source_roots: list[str | os.PathLike[str]] | None = None,
        extra_files: list[str | os.PathLike[str]] | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ):
        self.entry_path = Path(entrypoint or sys.argv[0] or Path.cwd()).resolve()
        start = self.entry_path.parent if self.entry_path.is_file() else self.entry_path
        self.repo_root = find_repo_root(start)
        roots = [self.repo_root]
        for raw_root in source_roots or []:
            root = Path(raw_root).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"source root is not a directory: {raw_root}")
            if root not in roots:
                roots.append(root)
        self.roots = roots
        self._matchers = {
            root: IgnoreMatcher.from_files(root, list(IGNORE_FILE_NAMES)) for root in roots
        }
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._files: dict[Path, TrackedFile] = {}
        self._lock = threading.RLock()
        self._active = True

        _install_audit_hook()
        with _AUDIT_LOCK:
            _ACTIVE_COLLECTORS.add(self)

        if self.entry_path.is_file():
            self._record_path(self.entry_path, "entrypoint", allow_anywhere=True)
        for path in extra_files or []:
            self.add_explicit(path)
        self._scan_modules()

    def _location(
        self,
        path: Path,
        *,
        allow_anywhere: bool = False,
        explicit: bool = False,
    ) -> tuple[str, Path] | None:
        for index, root in enumerate(self.roots):
            if not _is_relative_to(path, root):
                continue
            relative = path.relative_to(root).as_posix()
            if _should_skip(Path(relative)):
                return None
            if not explicit and self._matchers[root].matches(relative, is_dir=False):
                return None
            if index == 0:
                return relative, root
            return f"external/{index - 1}-{root.name}/{relative}", root
        if allow_anywhere:
            return f"extra/{path.name}", path.parent
        return None

    def _record_path(
        self,
        raw_path,
        source: str,
        *,
        allow_anywhere: bool = False,
        explicit: bool = False,
        manifest_path: str | None = None,
    ) -> None:
        if not self._active:
            return
        try:
            path = _source_path(Path(raw_path).expanduser().resolve())
        except (OSError, RuntimeError, TypeError, ValueError):
            return
        if not path.is_file() or _looks_like_virtualenv(path):
            return
        # Editable installs put this package inside the project tree. Runtime
        # imports and reads are not project code. track_files() uses
        # explicit=True and is still recorded.
        if not explicit and _is_under_agenticml_package(path):
            return
        relative_path = _normalize_manifest_path(manifest_path) if manifest_path else None
        if relative_path is not None:
            if _should_skip(Path(relative_path)):
                return
        else:
            location = self._location(
                path,
                allow_anywhere=allow_anywhere,
                explicit=explicit,
            )
            if location is None:
                return
            relative_path, _root = location
        item = TrackedFile(path, relative_path, source)
        with self._lock:
            previous = self._files.get(path)
            if previous is None or _SOURCE_PRIORITY[source] > _SOURCE_PRIORITY[previous.source]:
                self._files[path] = item

    def _scan_modules(self) -> None:
        for module in list(sys.modules.values()):
            path = getattr(module, "__file__", None)
            if not path:
                spec = getattr(module, "__spec__", None)
                path = getattr(spec, "origin", None)
            if path and path not in {"built-in", "frozen"}:
                self._record_path(path, "runtime_import")

    def add_explicit(
        self,
        path: str | os.PathLike[str],
        *,
        manifest_path: str | None = None,
        source: str = "extra",
    ) -> None:
        self._record_path(
            path,
            source,
            allow_anywhere=True,
            explicit=True,
            manifest_path=manifest_path,
        )

    def close(self) -> tuple[dict, list[TrackedFile]]:
        if self._active:
            self._scan_modules()
            self._active = False
            with _AUDIT_LOCK:
                _ACTIVE_COLLECTORS.discard(self)

        with self._lock:
            candidates = sorted(self._files.values(), key=lambda item: item.relative_path)

        kept: list[TrackedFile] = []
        skipped_oversized: list[str] = []
        skipped_total: list[str] = []
        skipped_missing: list[str] = []
        running_total = 0
        for item in candidates:
            try:
                # Re-resolve immediately before collection so a path replaced
                # by a symlink cannot escape its approved root after access.
                if item.absolute_path.resolve() != item.absolute_path:
                    skipped_missing.append(item.relative_path)
                    continue
                size = item.size
            except OSError:
                skipped_missing.append(item.relative_path)
                continue
            if size > self.max_file_bytes:
                skipped_oversized.append(item.relative_path)
                continue
            if running_total + size > self.max_total_bytes:
                skipped_total.append(item.relative_path)
                continue
            kept.append(item)
            running_total += size

        if skipped_oversized:
            logger.warning(
                "skipped %d runtime files exceeding max_file_bytes=%d (e.g. %s)",
                len(skipped_oversized),
                self.max_file_bytes,
                skipped_oversized[:3],
            )
        if skipped_total:
            logger.warning(
                "skipped %d runtime files after exceeding max_total_bytes=%d (e.g. %s)",
                len(skipped_total),
                self.max_total_bytes,
                skipped_total[:3],
            )
        if skipped_missing:
            logger.warning(
                "skipped %d runtime files missing at finish (e.g. %s)",
                len(skipped_missing),
                skipped_missing[:3],
            )

        manifest = {
            "repo_root": str(self.repo_root),
            "entrypoint": str(self.entry_path),
            "tracking_mode": "runtime",
            "source_roots": [str(root) for root in self.roots[1:]],
            "files": [
                {
                    "path": item.relative_path,
                    "source": item.source,
                    "size": item.size,
                    "sha256": item.sha256,
                    "content_type": item.content_type,
                }
                for item in kept
            ],
            "skipped": {
                "oversized": skipped_oversized,
                "over_total": skipped_total,
                "missing": skipped_missing,
            },
        }
        return manifest, kept


def _normalize_manifest_path(manifest_path: str | os.PathLike[str] | None) -> str | None:
    if manifest_path is None:
        return None
    text = str(manifest_path).replace("\\", "/").strip().lstrip("/")
    parts = [part for part in text.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.resolve().relative_to(other.resolve())
        return True
    except ValueError:
        return False


@lru_cache(maxsize=1)
def _agenticml_package_dir() -> Path | None:
    """Directory of the installed ``agenticml`` package, or None."""
    try:
        import agenticml
        source = agenticml.__file__
    except Exception:
        return None
    if not source:
        return None
    try:
        return Path(source).resolve().parent
    except (OSError, RuntimeError, ValueError):
        return None


def _is_under_agenticml_package(path: Path) -> bool:
    """True when resolved ``path`` is a file inside the agenticml package."""
    package_dir = _agenticml_package_dir()
    if package_dir is None:
        return False
    try:
        path.relative_to(package_dir)
    except ValueError:
        return False
    return True


@lru_cache(maxsize=1)
def _environment_package_roots() -> tuple[Path, ...]:
    """Return active/user package roots that must never enter a snapshot."""
    candidates: set[Path] = set()

    try:
        candidates.update(Path(item).resolve() for item in site.getsitepackages())
    except (AttributeError, OSError):
        pass
    try:
        user_site = site.getusersitepackages()
        if isinstance(user_site, str):
            candidates.add(Path(user_site).resolve())
        else:
            candidates.update(Path(item).resolve() for item in user_site)
    except (AttributeError, OSError):
        pass

    paths = sysconfig.get_paths()
    for key in ("purelib", "platlib"):
        value = paths.get(key)
        if value:
            candidates.add(Path(value).resolve())

    base_prefix = getattr(sys, "base_prefix", sys.prefix)
    base_exec_prefix = getattr(sys, "base_exec_prefix", sys.exec_prefix)
    if sys.prefix != base_prefix:
        candidates.add(Path(sys.prefix).resolve())
    if sys.exec_prefix != base_exec_prefix:
        candidates.add(Path(sys.exec_prefix).resolve())

    return tuple(sorted(candidates, key=str))


def _looks_like_virtualenv(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = path

    if set(resolved.parts) & SKIP_DIR_NAMES:
        return True
    if any(_is_relative_to(resolved, root) for root in _environment_package_roots()):
        return True

    # Virtual environments may have arbitrary directory names. Their root is
    # reliably identified by pyvenv.cfg, so exclude every descendant.
    start = resolved if resolved.is_dir() else resolved.parent
    return any((parent / "pyvenv.cfg").is_file() for parent in (start, *start.parents))


def find_repo_root(start: Path) -> Path:
    start = start.resolve()
    return _project_root(start) or start


def _project_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    return None


def main_file() -> str | Path | None:
    """Return ``__main__.__file__`` when it is a string or path."""
    main = sys.modules.get("__main__")
    main_file = getattr(main, "__file__", None) if main is not None else None
    return main_file if isinstance(main_file, (str, Path)) else None


def _normalize_task(value):
    if not isinstance(value, str):
        return None
    if any(unicodedata.category(c).startswith(("C", "Zl", "Zp")) for c in value):
        return None
    value = value.strip()
    if not value or len(value) > 128:
        return None
    return value


def _marker_task(content):
    try:
        import tomllib
    except ImportError:
        # Python 3.9/3.10: only a top-level, single-line string is needed.
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("["):
                break
            match = re.fullmatch(r'''task\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')\s*(?:#.*)?''', line)
            if match:
                raw = match[1]
                return json.loads(raw) if raw.startswith('"') else raw[1:-1]
        return None
    return tomllib.loads(content).get("task")


def resolve_task(explicit, repo_root, entry_file=None):
    """Resolve the first valid task signal without disrupting the run."""
    for value, source in ((explicit, "explicit"), (os.environ.get("AGENTICML_TASK"), "env")):
        task = _normalize_task(value)
        if task is not None:
            return task, source
        if value is not None:
            logger.warning("invalid %s task; ignoring", source)
    try:
        root = Path(repo_root).resolve()
        start = Path(entry_file).resolve().parent if entry_file else Path.cwd().resolve()
        relative = start.relative_to(root)
        directory = start
        while True:
            marker = directory / "agenticml.toml"
            try:
                st = marker.lstat()
                if stat.S_ISREG(st.st_mode) and st.st_size <= 64 * 1024:
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    with os.fdopen(os.open(marker, flags), "rb") as handle:
                        st = os.fstat(handle.fileno())
                        content = handle.read(64 * 1024 + 1) if stat.S_ISREG(st.st_mode) and st.st_size <= 64 * 1024 else b""
                    if len(content) <= 64 * 1024:
                        task = _normalize_task(_marker_task(content.decode("utf-8")))
                        if task is not None:
                            return task, "marker"
            except Exception:
                logger.debug("cannot read task marker %s", marker, exc_info=True)
            if directory == root:
                break
            directory = directory.parent
        if entry_file and relative != Path("."):
            task = _normalize_task(relative.as_posix())
            if task is not None:
                return task, "entry"
    except Exception:
        logger.debug("cannot resolve task path", exc_info=True)
    return None, None


def _main_directory() -> Path:
    """Directory of ``__main__.__file__`` when that file exists, else the cwd."""
    entry_file = main_file()
    if entry_file:
        return Path(entry_file).resolve().parent
    return Path.cwd()


def default_project(start: Path | None = None) -> str:
    """Project name when the caller did not pass one.

    ``AGENTICML_PROJECT`` wins when it is non-empty. Otherwise this is the
    directory name of the repo that contains ``start`` (or the main file's
    directory, else the working directory). A path whose name is empty
    (``/``) becomes ``"default"``.
    """
    configured = os.environ.get("AGENTICML_PROJECT")
    if configured:
        return configured
    if start is None:
        start = _main_directory()
    name = find_repo_root(Path(start)).name
    return name or "default"


def default_run_name(path: str | Path | None) -> str | None:
    """Run name when the caller did not pass one.

    This is the file's POSIX path relative to its nearest project root, or
    its absolute resolved path when no project marker exists. Missing or
    unreadable files have no default name.
    """
    if not path:
        return None
    try:
        p = Path(path).resolve()
        if not p.is_file():
            return None
        root = _project_root(p.parent)
        return p.relative_to(root).as_posix() if root is not None else p.as_posix()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _should_skip(path: Path) -> bool:
    lowered_parts = [part.lower() for part in path.parts]
    if any(part in SKIP_DIR_NAMES for part in lowered_parts):
        return True
    if any(part.endswith(SKIP_PATH_SUFFIXES) for part in lowered_parts):
        return True
    return bool(lowered_parts) and lowered_parts[-1].endswith(SKIP_FILE_SUFFIXES)


def compute_snapshot_hash(manifest: dict) -> str:
    digest = hashlib.sha256()
    digest.update(manifest_to_json(manifest).encode("utf-8"))
    return digest.hexdigest()


def manifest_to_json(manifest: dict) -> str:
    return json.dumps(manifest, separators=(",", ":"), sort_keys=True)

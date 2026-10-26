from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import logging
import mimetypes
import os
import site
import sys
import sysconfig
import threading
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

# Default size caps — overridable via collect_code_snapshot kwargs. Anything
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


def _split_extra_file(extra):
    if isinstance(extra, (tuple, list)) and len(extra) == 2:
        return extra[0], extra[1]
    return extra, None


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.resolve().relative_to(other.resolve())
        return True
    except ValueError:
        return False


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
    for candidate in (start, *start.parents):
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    return start


def _should_skip(path: Path) -> bool:
    lowered_parts = [part.lower() for part in path.parts]
    if any(part in SKIP_DIR_NAMES for part in lowered_parts):
        return True
    if any(part.endswith(SKIP_PATH_SUFFIXES) for part in lowered_parts):
        return True
    return bool(lowered_parts) and lowered_parts[-1].endswith(SKIP_FILE_SUFFIXES)


def _iter_repo_files(repo_root: Path, ignore: IgnoreMatcher):
    for current_root, dirnames, filenames in os.walk(repo_root):
        current_path = Path(current_root)

        # Prune skip-listed directories first (cheap).
        dirnames[:] = [
            d
            for d in dirnames
            if d not in SKIP_DIR_NAMES and not _looks_like_virtualenv(current_path / d)
        ]

        # Then prune by ignore matcher.
        kept_dirs = []
        for d in dirnames:
            rel = (current_path / d).relative_to(repo_root).as_posix()
            if not ignore.matches(rel, is_dir=True):
                kept_dirs.append(d)
        dirnames[:] = kept_dirs

        if _looks_like_virtualenv(current_path):
            continue

        for filename in filenames:
            file_path = current_path / filename
            if _should_skip(file_path):
                continue
            rel = file_path.relative_to(repo_root).as_posix()
            if ignore.matches(rel, is_dir=False):
                continue
            yield file_path


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _module_parts(module_name: str | None) -> list[str]:
    if not module_name:
        return []
    return [part for part in module_name.split(".") if part]


def _candidate_module_paths(base_dirs: list[Path], module_name: str | None) -> list[Path]:
    parts = _module_parts(module_name)
    candidates = []
    for base_dir in base_dirs:
        module_path = base_dir.joinpath(*parts) if parts else base_dir
        candidates.append(module_path.with_suffix(".py"))
        candidates.append(module_path / "__init__.py")
    return candidates


def _resolve_import_path(
    importer: Path, repo_root: Path, module_name: str | None, level: int
) -> Path | None:
    """Resolve a Python import to an in-repo file using AST + filesystem only.

    We deliberately do NOT call importlib.util.find_spec — it executes package
    __init__.py files, which is unsafe for an SDK that runs on import.
    """
    search_roots: list[Path]
    if level > 0:
        relative_base = importer.parent
        for _ in range(level - 1):
            relative_base = relative_base.parent
        search_roots = [relative_base]
    else:
        search_roots = [repo_root, importer.parent]

    for candidate in _candidate_module_paths(search_roots, module_name):
        if (
            candidate.exists()
            and candidate.is_file()
            and not _looks_like_virtualenv(candidate)
        ):
            return candidate.resolve()
    return None


def _collect_local_imports(entry_files: list[Path], repo_root: Path) -> dict[Path, str]:
    tracked: dict[Path, str] = {}
    pending = [path.resolve() for path in entry_files if path.suffix == ".py" and path.exists()]
    visited: set[Path] = set()

    while pending:
        current = pending.pop()
        if current in visited or _looks_like_virtualenv(current):
            continue
        visited.add(current)

        source = _read_text(current)
        if source is None:
            continue

        try:
            tree = ast.parse(source, filename=str(current))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [(alias.name, 0) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [(node.module, node.level)]
                if node.module is None:
                    imports.extend((alias.name, node.level) for alias in node.names)
            else:
                continue

            for module_name, level in imports:
                resolved = _resolve_import_path(current, repo_root, module_name, level)
                if resolved is None:
                    continue
                if not _is_relative_to(resolved, repo_root):
                    # Only follow imports inside the repo. External packages are
                    # already captured via env.packages.
                    continue
                relative_path = resolved.relative_to(repo_root).as_posix()
                tracked.setdefault(resolved, relative_path)
                pending.append(resolved)

    return {path: relative for path, relative in tracked.items()}


def collect_code_snapshot(
    entrypoint: str | os.PathLike[str] | None = None,
    extra_files: list[str] | None = None,
    include_repo: bool = True,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    extra_source: str | None = None,
) -> tuple[dict, list[TrackedFile]]:
    entry_path = Path(entrypoint or sys.argv[0] or Path.cwd()).resolve()
    repo_root = find_repo_root(entry_path.parent if entry_path.is_file() else entry_path)

    ignore = IgnoreMatcher.from_files(repo_root, list(IGNORE_FILE_NAMES))

    tracked_files: dict[Path, TrackedFile] = {}
    python_files: list[Path] = []

    if include_repo:
        for file_path in _iter_repo_files(repo_root, ignore):
            relative_path = file_path.relative_to(repo_root).as_posix()
            tracked_files[file_path.resolve()] = TrackedFile(
                absolute_path=file_path.resolve(),
                relative_path=relative_path,
                source="repo",
            )
            if file_path.suffix == ".py":
                python_files.append(file_path.resolve())

    if include_repo and entry_path.exists() and entry_path.is_file():
        python_files.append(entry_path.resolve())

    import_roots = list(python_files)
    for extra in extra_files or []:
        extra_path, manifest_override = _split_extra_file(extra)
        resolved = Path(extra_path).expanduser().resolve()
        if (
            not resolved.exists()
            or not resolved.is_file()
            or _looks_like_virtualenv(resolved)
            or _should_skip(resolved)
        ):
            continue
        relative_path = _normalize_manifest_path(manifest_override)
        source_tag = extra_source if extra_source is not None else "model"
        source = source_tag if relative_path else "extra"
        if relative_path is not None:
            if _should_skip(Path(relative_path)):
                continue
        elif _is_relative_to(resolved, repo_root):
            relative_path = resolved.relative_to(repo_root).as_posix()
        else:
            relative_path = f"extra/{resolved.name}"
        tracked_files[resolved] = TrackedFile(
            absolute_path=resolved,
            relative_path=relative_path,
            source=source,
        )
        if resolved.suffix == ".py":
            import_roots.append(resolved)

    for resolved_path, relative_path in _collect_local_imports(import_roots, repo_root).items():
        # Imports can pull in files outside the repo walk; honor ignore rules
        # for them too, otherwise `.agenticmlignore` is bypassed for any file
        # transitively imported from the entrypoint.
        if _should_skip(Path(relative_path)) or ignore.matches(relative_path, is_dir=False):
            continue
        tracked_files.setdefault(
            resolved_path,
            TrackedFile(
                absolute_path=resolved_path,
                relative_path=relative_path,
                source="import",
            ),
        )

    # Apply size caps.
    files_sorted = sorted(tracked_files.values(), key=lambda item: item.relative_path)
    kept: list[TrackedFile] = []
    skipped_oversized: list[str] = []
    skipped_total: list[str] = []
    running_total = 0
    for item in files_sorted:
        size = item.size
        if size > max_file_bytes:
            skipped_oversized.append(item.relative_path)
            continue
        if running_total + size > max_total_bytes:
            skipped_total.append(item.relative_path)
            continue
        kept.append(item)
        running_total += size

    if skipped_oversized:
        logger.warning(
            "skipped %d files exceeding max_file_bytes=%d (e.g. %s)",
            len(skipped_oversized),
            max_file_bytes,
            skipped_oversized[:3],
        )
    if skipped_total:
        logger.warning(
            "skipped %d files after exceeding max_total_bytes=%d (e.g. %s)",
            len(skipped_total),
            max_total_bytes,
            skipped_total[:3],
        )

    manifest = {
        "repo_root": str(repo_root),
        "entrypoint": str(entry_path),
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
        },
    }
    return manifest, kept


def compute_snapshot_hash(manifest: dict) -> str:
    digest = hashlib.sha256()
    digest.update(manifest_to_json(manifest).encode("utf-8"))
    return digest.hexdigest()


def manifest_to_json(manifest: dict) -> str:
    return json.dumps(manifest, separators=(",", ":"), sort_keys=True)

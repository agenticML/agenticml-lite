"""Shared helpers for inspecting live objects and staging defining source.

Prefix-parameterized so model, optimizer, loss, and dataloader tracking can
reuse the same copy/manifest logic. Callers keep kind-specific inspect rules.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger("agenticml.source")

ENV_PATH_MARKERS = ("site-packages", "dist-packages")
MAX_CONFIG_DICT_KEYS = 512
MAX_CONFIG_LIST_LEN = 256
MAX_CONFIG_STRING = 4096
MAX_CONFIG_DEPTH = 8
HF_DROP_KEYS = {
    "added_tokens_decoder",
    "added_tokens_encoder",
    "chat_template",
    "id2label",
    "id2token",
    "label2id",
    "token2id",
    "vocab",
}


def json_safe(value: Any, depth: int = 0) -> Any:
    if depth > MAX_CONFIG_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and value != value:  # NaN
            return None
        return value
    if isinstance(value, str):
        if len(value) <= MAX_CONFIG_STRING:
            return value
        return value[:MAX_CONFIG_STRING] + "…"
    if isinstance(value, dict):
        if len(value) > MAX_CONFIG_DICT_KEYS:
            return None
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in HF_DROP_KEYS:
                continue
            converted = json_safe(item, depth + 1)
            if converted is not None or item is None:
                out[key] = converted
        return out
    if isinstance(value, (list, tuple)):
        items = list(value[:MAX_CONFIG_LIST_LEN])
        return [json_safe(item, depth + 1) for item in items]
    return None


def package_name(module_name: str) -> str | None:
    if not module_name or module_name in {"__main__", "builtins", "__builtin__"}:
        return None
    return module_name.split(".", 1)[0]


def package_version(package: str | None) -> str | None:
    if not package:
        return None
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        return None
    try:
        return version(package)
    except PackageNotFoundError:
        pass
    except Exception:
        return None
    try:
        from importlib.metadata import packages_distributions

        dists = (packages_distributions() or {}).get(package) or []
        if dists:
            return version(dists[0])
    except Exception:
        return None
    return None


def is_torch_package(module_name: str) -> bool:
    return package_name(module_name) == "torch"


def qualname(cls: type) -> str:
    module = getattr(cls, "__module__", "") or ""
    name = getattr(cls, "__qualname__", getattr(cls, "__name__", repr(cls)))
    return f"{module}.{name}" if module else name


def class_name(cls: type) -> str:
    return getattr(cls, "__name__", repr(cls))


def class_identity(cls: type) -> dict[str, Any]:
    module = getattr(cls, "__module__", "") or ""
    return {
        "qualname": getattr(cls, "__qualname__", getattr(cls, "__name__", type(cls).__name__)),
        "module": module,
        "package": package_name(module),
        "version": package_version(package_name(module)),
    }


def callable_identity(obj: Any) -> dict[str, Any]:
    target = obj
    if isinstance(obj, functools.partial):
        target = obj.func
    if inspect.ismethod(target):
        target = target.__func__
    if inspect.isfunction(target) or inspect.isclass(target):
        entity = target
    else:
        entity = type(target)
    return class_identity(entity) if inspect.isclass(entity) else _function_identity(entity)


def _function_identity(fn: Any) -> dict[str, Any]:
    module = getattr(fn, "__module__", "") or ""
    return {
        "qualname": getattr(fn, "__qualname__", getattr(fn, "__name__", type(fn).__name__)),
        "module": module,
        "package": package_name(module),
        "version": package_version(package_name(module)),
    }


def source_file(obj: Any) -> Path | None:
    target = obj
    if isinstance(obj, functools.partial):
        target = obj.func
    path_str: str | None = None
    try:
        path_str = inspect.getsourcefile(target)
    except (TypeError, OSError, ValueError):
        path_str = None
    if not path_str:
        try:
            path_str = inspect.getfile(target)
        except (TypeError, OSError, ValueError):
            return None
    if not path_str:
        return None
    path = Path(path_str)
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        pass
    if not path.is_file() or path.suffix != ".py":
        return None
    try:
        inspect.getsourcelines(target)
    except (OSError, TypeError, SyntaxError, ValueError):
        return None
    return path


def manifest_path_for_source(source_path: Path, module_name: str, prefix: str) -> str:
    prefix = prefix if prefix.endswith("/") else f"{prefix}/"
    parts = source_path.parts
    for marker in ENV_PATH_MARKERS:
        if marker in parts:
            idx = parts.index(marker)
            rel = Path(*parts[idx + 1 :]).as_posix()
            if rel:
                return f"{prefix}{rel}"
    if module_name and module_name not in {"__main__", "builtins"}:
        module_path = module_name.replace(".", "/")
        if source_path.name == "__init__.py":
            return f"{prefix}{module_path}/__init__.py"
        return f"{prefix}{module_path}{source_path.suffix}"
    return f"{prefix}{source_path.name}"


def collect_class_source_files(
    classes: list[type],
    prefix: str,
    *,
    skip_torch: bool = False,
) -> list[tuple[Path, str]]:
    source_files: list[tuple[Path, str]] = []
    seen_paths: set[Path] = set()
    for cls in classes:
        module_name = getattr(cls, "__module__", "") or ""
        if skip_torch and is_torch_package(module_name):
            continue
        path = source_file(cls)
        if path is None or path in seen_paths:
            continue
        seen_paths.add(path)
        source_files.append((path, manifest_path_for_source(path, module_name, prefix)))
    return source_files


def stage_source_and_metadata(
    source_files: list[tuple[Path, str]],
    metadata: dict,
    staging_dir: Path,
    metadata_path: str,
    *,
    log_label: str,
    log: logging.Logger | None = None,
) -> list[tuple[Path, str]]:
    """Copy defining ``.py`` files and write a metadata JSON into ``staging_dir``.

    Returns ``(absolute_staged_path, manifest_path)`` pairs. ``metadata["files"]``
    is updated to successfully copied source paths only.
    """
    log = log or logger
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    staged: list[tuple[Path, str]] = []
    files_meta: list[str] = []
    for source_path, manifest_path in source_files:
        dest = (staging_dir / manifest_path).resolve()
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, dest)
        except OSError as exc:
            log.warning("%s: failed to copy %s: %s", log_label, source_path, exc)
            if "error" not in metadata:
                metadata["error"] = f"failed to copy {source_path}: {exc}"
            continue
        staged.append((dest, manifest_path))
        files_meta.append(manifest_path)

    metadata["files"] = files_meta
    meta_dest = (staging_dir / metadata_path).resolve()
    try:
        meta_dest.parent.mkdir(parents=True, exist_ok=True)
        meta_dest.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staged.append((meta_dest, metadata_path))
    except OSError as exc:
        log.warning("%s: failed to write %s: %s", log_label, metadata_path, exc)

    return staged

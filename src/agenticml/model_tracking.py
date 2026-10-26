"""Resolve architecture source files for a live model object.

Used by ``Run.track_model``: unwrap known wrappers, walk the MRO, copy the
defining ``.py`` files (not site-packages in-place), and emit ``model.json``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._source_inspect import (
    class_name as _class_name,
    json_safe as _json_safe,
    manifest_path_for_source,
    package_name as _package_name,
    package_version as _package_version,
    qualname as _qualname,
    source_file as _class_source_file,
    stage_source_and_metadata,
)
from .component_tracking import (
    _apply_extra_metadata,
    metadata_manifest_path,
    sanitize_component_name,
)

logger = logging.getLogger("agenticml.model")

MODEL_MANIFEST_PREFIX = "model/"
MODEL_METADATA_PATH = "agenticml/model.json"


@dataclass
class ModelInspection:
    """Resolved class files and metadata for an instantiated model."""

    model: Any
    classes: list[type] = field(default_factory=list)
    source_files: list[tuple[Path, str]] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def unwrap_model(model: Any) -> Any:
    """Best-effort unwrap of DDP / DataParallel, ``torch.compile``, and PEFT."""
    current = model
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        nxt = _unwrap_step(current)
        if nxt is None or nxt is current:
            break
        current = nxt
    return current


def inspect_model(model: Any) -> ModelInspection:
    """Locate architecture ``.py`` files and build ``model.json`` fields.

    Inspect failures (missing source, C extensions, TorchScript) are recorded
    on ``metadata["error"]`` and never raised.
    """
    unwrapped = unwrap_model(model)
    cls = type(unwrapped)
    kept = list(_kept_mro_classes(cls))
    source_files: list[tuple[Path, str]] = []
    seen_paths: set[Path] = set()
    inspect_error: str | None = None

    for kept_cls in kept:
        path = _class_source_file(kept_cls)
        if path is None:
            if inspect_error is None and kept_cls is cls:
                inspect_error = (
                    f"could not resolve a .py source file for "
                    f"{_qualname(kept_cls)}"
                )
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        module_name = getattr(kept_cls, "__module__", "") or ""
        source_files.append(
            (path, manifest_path_for_source(path, module_name, MODEL_MANIFEST_PREFIX))
        )

    if not source_files and inspect_error is None:
        inspect_error = (
            f"could not resolve a .py source file for {_qualname(cls)}"
        )

    metadata = {
        "qualname": getattr(cls, "__qualname__", getattr(cls, "__name__", type(cls).__name__)),
        "module": getattr(cls, "__module__", "") or "",
        "package": _package_name(getattr(cls, "__module__", "") or ""),
        "version": _package_version(_package_name(getattr(cls, "__module__", "") or "")),
        "mro": [_class_name(item) for item in kept],
        "files": [manifest for _path, manifest in source_files],
    }
    config = _hf_config(unwrapped)
    if config is not None:
        metadata["config"] = config
    if inspect_error:
        metadata["error"] = inspect_error

    return ModelInspection(
        model=unwrapped,
        classes=kept,
        source_files=source_files,
        metadata=metadata,
    )


def stage_tracked_model(
    model: Any,
    staging_dir: Path,
    *,
    name: str = "default",
    extra_metadata: dict | None = None,
) -> list[tuple[Path, str]]:
    """Copy architecture files and model metadata into ``staging_dir``.

    ``name`` is sanitized and stored on ``metadata["name"]``. The manifest is
    ``agenticml/model.json`` when ``name`` is ``default``, and
    ``agenticml/model.{name}.json`` otherwise. Keys in ``extra_metadata`` are
    added only when inspection has not already set them.

    Returns ``(absolute_staged_path, manifest_path)`` pairs. Staging paths are
    outside the original environment so the collector venv skip can stay intact.
    """
    name = sanitize_component_name(name)
    inspection = inspect_model(model)
    inspection.metadata["name"] = name
    _apply_extra_metadata(inspection.metadata, extra_metadata)
    if inspection.metadata.get("error"):
        logger.warning("track_model: %s", inspection.metadata["error"])

    return stage_source_and_metadata(
        inspection.source_files,
        inspection.metadata,
        staging_dir,
        metadata_manifest_path("model", name),
        log_label="track_model",
        log=logger,
    )


def _unwrap_step(obj: Any) -> Any:
    getter = getattr(obj, "get_base_model", None)
    if callable(getter):
        try:
            base = getter()
        except Exception:
            base = None
        if base is not None and base is not obj:
            return base

    if _is_peft_wrapper(obj):
        base = getattr(obj, "base_model", None)
        if base is not None and not callable(base) and base is not obj:
            return base

    orig = getattr(obj, "_orig_mod", None)
    if orig is not None and not callable(orig) and orig is not obj:
        return orig

    inner = getattr(obj, "module", None)
    if inner is not None and not callable(inner) and inner is not obj:
        if _looks_like_parallel_wrapper(obj):
            return inner

    return obj


def _looks_like_parallel_wrapper(obj: Any) -> bool:
    cls = type(obj)
    name = cls.__name__
    mod = getattr(cls, "__module__", "") or ""
    if "DataParallel" in name or "DistributedDataParallel" in name:
        return True
    if mod.startswith("torch.nn.parallel"):
        return True
    # Test doubles / generic wrappers expose ``.module`` without being nn.Module.
    return hasattr(obj, "module") and not hasattr(obj, "_modules")


def _is_peft_wrapper(obj: Any) -> bool:
    cls = type(obj)
    mod = getattr(cls, "__module__", "") or ""
    name = cls.__name__
    if mod == "peft" or mod.startswith("peft."):
        return True
    return "Peft" in name


def _kept_mro_classes(cls: type) -> list[type]:
    kept: list[type] = []
    for item in cls.__mro__:
        if _is_torch_nn_module_class(item):
            break
        if item is object:
            break
        if _should_skip_mro_class(item):
            continue
        kept.append(item)
    return kept


def _is_torch_nn_module_class(cls: type) -> bool:
    if getattr(cls, "__name__", "") != "Module":
        return False
    mod = getattr(cls, "__module__", "") or ""
    return (
        mod == "torch.nn"
        or mod == "torch.nn.modules.module"
        or mod.startswith("torch.nn.modules.module")
    )


def _should_skip_mro_class(cls: type) -> bool:
    mod = getattr(cls, "__module__", "") or ""
    if not mod or mod == "builtins" or mod.startswith("builtins."):
        return True
    if mod == "__builtin__":
        return True
    if mod == "torch.nn" or mod.startswith("torch.nn."):
        return True
    return False


def _hf_config(model: Any) -> dict | None:
    config = getattr(model, "config", None)
    if config is None:
        return None
    to_dict = getattr(config, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        raw = to_dict()
    except Exception:
        return None
    converted = _json_safe(raw)
    return converted if isinstance(converted, dict) and converted else None

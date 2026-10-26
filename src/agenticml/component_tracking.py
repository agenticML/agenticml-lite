"""Inspect and stage optimizer, loss, and dataloader objects.

Used by ``Run.track_optimizer`` / ``track_loss`` / ``track_dataloader``.
Torch is optional: attributes are recorded when present. Inspect failures set
``metadata["error"]`` and never raise. ``torch`` stdlib source is identified
by package/version and is not copied; custom and third-party classes are.
"""

from __future__ import annotations

import functools
import inspect
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._source_inspect import (
    callable_identity,
    class_identity,
    class_name,
    collect_class_source_files,
    is_torch_package,
    json_safe,
    manifest_path_for_source,
    qualname,
    source_file,
    stage_source_and_metadata,
)

logger = logging.getLogger("agenticml.component")

OPTIMIZER_MANIFEST_PREFIX = "optimizer/"
LOSS_MANIFEST_PREFIX = "loss/"
DATALOADER_MANIFEST_PREFIX = "dataloader/"

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_LOADER_FIELDS = (
    "batch_size",
    "num_workers",
    "drop_last",
    "pin_memory",
    "persistent_workers",
    "prefetch_factor",
    "timeout",
)


@dataclass
class ComponentInspection:
    """Resolved source files and metadata for a training component."""

    obj: Any
    classes: list[type] = field(default_factory=list)
    source_files: list[tuple[Path, str]] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def sanitize_component_name(name: str | None) -> str:
    """Restrict ``name`` to ``[A-Za-z0-9._-]+`` so it cannot escape ``agenticml/``."""
    if name is None:
        return "default"
    text = str(name).strip()
    if not text:
        return "default"
    text = text.replace("..", "_")
    text = _NAME_UNSAFE.sub("_", text)
    text = re.sub(r"_+", "_", text)
    text = text.strip("._-")
    if not text:
        return "default"
    return text[:64]


def metadata_manifest_path(kind: str, name: str) -> str:
    if name == "default":
        return f"agenticml/{kind}.json"
    return f"agenticml/{kind}.{name}.json"


def _apply_extra_metadata(metadata: dict, extra_metadata: dict | None) -> None:
    """Add keys from ``extra_metadata`` that inspection has not already set."""
    if not isinstance(extra_metadata, dict):
        return
    for key, value in extra_metadata.items():
        if key not in metadata:
            metadata[key] = value


def inspect_optimizer(optimizer: Any, *, name: str = "default") -> ComponentInspection:
    """Capture optimizer class identity and hyperparameters, never param tensors."""
    name = sanitize_component_name(name)
    try:
        cls = type(optimizer)
        kept = _kept_optimizer_mro(cls)
        source_files = collect_class_source_files(
            kept, OPTIMIZER_MANIFEST_PREFIX, skip_torch=True
        )
        metadata = class_identity(cls)
        metadata["name"] = name
        metadata["mro"] = [class_name(item) for item in kept]
        metadata["files"] = [manifest for _path, manifest in source_files]
        metadata["defaults"] = json_safe(getattr(optimizer, "defaults", None))
        groups_meta, num_params = _param_groups_meta(getattr(optimizer, "param_groups", None))
        metadata["param_groups"] = groups_meta
        metadata["num_param_groups"] = len(groups_meta)
        metadata["num_params"] = num_params
        if not source_files:
            _maybe_missing_source_error(metadata, cls)
        return ComponentInspection(
            obj=optimizer,
            classes=kept,
            source_files=source_files,
            metadata=metadata,
        )
    except Exception as exc:
        return _failed_inspection(optimizer, name, exc)


def inspect_loss(loss: Any, *, name: str = "default") -> ComponentInspection:
    """Capture loss class/function identity plus constructor-like attributes."""
    name = sanitize_component_name(name)
    try:
        kind = _loss_kind(loss)
        identity_target, classes, source_target = _loss_targets(loss, kind)
        source_files = _loss_source_files(classes, source_target)
        metadata = callable_identity(identity_target)
        metadata["name"] = name
        metadata["kind"] = kind
        metadata["mro"] = [class_name(item) for item in classes]
        metadata["files"] = [manifest for _path, manifest in source_files]
        if kind == "partial":
            metadata["wrapped"] = callable_identity(loss.func)
            metadata["args"] = [json_safe(item) for item in (getattr(loss, "args", None) or ())]
            keywords = getattr(loss, "keywords", None) or {}
            metadata["keywords"] = {
                str(key): json_safe(value) for key, value in keywords.items()
            }
        elif kind in {"module", "callable"} and not inspect.isclass(loss) and not inspect.isfunction(loss):
            metadata["attrs"] = _constructor_attrs(loss)
        if not source_files:
            error_cls = classes[0] if classes else type(loss)
            _maybe_missing_source_error(metadata, error_cls, module_override=_identity_module(identity_target))
        return ComponentInspection(
            obj=loss,
            classes=classes,
            source_files=source_files,
            metadata=metadata,
        )
    except Exception as exc:
        return _failed_inspection(loss, name, exc)


def inspect_dataloader(obj: Any, *, name: str = "default") -> ComponentInspection:
    """Capture loader kwargs and dataset class identity, never dataset contents."""
    name = sanitize_component_name(name)
    try:
        loader, dataset = _split_loader_and_dataset(obj)
        classes: list[type] = []
        source_files: list[tuple[Path, str]] = []
        if loader is not None:
            metadata = class_identity(type(loader))
            metadata["name"] = name
            for field_name in _LOADER_FIELDS:
                if hasattr(loader, field_name):
                    metadata[field_name] = json_safe(getattr(loader, field_name))
            shuffle = _infer_shuffle(loader)
            if shuffle is not None:
                metadata["shuffle"] = shuffle
            for attr in ("sampler", "batch_sampler", "collate_fn"):
                value = getattr(loader, attr, None)
                if value is not None:
                    metadata[attr] = _callable_ref(value)
            collate = getattr(loader, "collate_fn", None)
            if collate is not None:
                source_files.extend(_callable_source_files(collate, DATALOADER_MANIFEST_PREFIX))
        else:
            metadata = class_identity(type(dataset) if dataset is not None else type(obj))
            metadata["name"] = name

        if dataset is not None:
            ds_classes = _kept_dataset_mro(type(dataset))
            classes.extend(ds_classes)
            source_files.extend(
                collect_class_source_files(
                    ds_classes, DATALOADER_MANIFEST_PREFIX, skip_torch=True
                )
            )
            ds_meta = class_identity(type(dataset))
            length = _safe_len(dataset)
            if length is not None:
                ds_meta["length"] = length
            metadata["dataset"] = ds_meta
            metadata["mro"] = [class_name(item) for item in ds_classes]
        else:
            metadata["mro"] = []

        source_files = _dedupe_source_files(source_files)
        metadata["files"] = [manifest for _path, manifest in source_files]
        if not source_files and dataset is not None:
            _maybe_missing_source_error(metadata, type(dataset))
        return ComponentInspection(
            obj=obj,
            classes=classes,
            source_files=source_files,
            metadata=metadata,
        )
    except Exception as exc:
        return _failed_inspection(obj, name, exc)


def stage_tracked_optimizer(
    optimizer: Any,
    staging_dir: Path,
    *,
    name: str = "default",
    extra_metadata: dict | None = None,
) -> list[tuple[Path, str]]:
    name = sanitize_component_name(name)
    inspection = inspect_optimizer(optimizer, name=name)
    _apply_extra_metadata(inspection.metadata, extra_metadata)
    if inspection.metadata.get("error"):
        logger.warning("track_optimizer: %s", inspection.metadata["error"])
    return stage_source_and_metadata(
        inspection.source_files,
        inspection.metadata,
        staging_dir,
        metadata_manifest_path("optimizer", name),
        log_label="track_optimizer",
        log=logger,
    )


def stage_tracked_loss(
    loss: Any,
    staging_dir: Path,
    *,
    name: str = "default",
    extra_metadata: dict | None = None,
) -> list[tuple[Path, str]]:
    name = sanitize_component_name(name)
    inspection = inspect_loss(loss, name=name)
    _apply_extra_metadata(inspection.metadata, extra_metadata)
    if inspection.metadata.get("error"):
        logger.warning("track_loss: %s", inspection.metadata["error"])
    return stage_source_and_metadata(
        inspection.source_files,
        inspection.metadata,
        staging_dir,
        metadata_manifest_path("loss", name),
        log_label="track_loss",
        log=logger,
    )


def stage_tracked_dataloader(
    obj: Any,
    staging_dir: Path,
    *,
    name: str = "default",
    extra_metadata: dict | None = None,
) -> list[tuple[Path, str]]:
    name = sanitize_component_name(name)
    inspection = inspect_dataloader(obj, name=name)
    _apply_extra_metadata(inspection.metadata, extra_metadata)
    if inspection.metadata.get("error"):
        logger.warning("track_dataloader: %s", inspection.metadata["error"])
    return stage_source_and_metadata(
        inspection.source_files,
        inspection.metadata,
        staging_dir,
        metadata_manifest_path("dataloader", name),
        log_label="track_dataloader",
        log=logger,
    )


def _failed_inspection(obj: Any, name: str, exc: BaseException) -> ComponentInspection:
    try:
        metadata = callable_identity(obj)
    except Exception:
        metadata = {
            "qualname": type(obj).__name__,
            "module": "",
            "package": None,
            "version": None,
        }
    metadata["name"] = name
    metadata["files"] = []
    metadata["error"] = str(exc)
    return ComponentInspection(obj=obj, metadata=metadata)


def _maybe_missing_source_error(
    metadata: dict, cls: type, *, module_override: str | None = None
) -> None:
    module = module_override if module_override is not None else (getattr(cls, "__module__", "") or "")
    if is_torch_package(module):
        return
    metadata["error"] = f"could not resolve a .py source file for {qualname(cls)}"


def _identity_module(obj: Any) -> str:
    if inspect.ismethod(obj):
        obj = obj.__func__
    if inspect.isfunction(obj) or inspect.isclass(obj):
        return getattr(obj, "__module__", "") or ""
    return getattr(type(obj), "__module__", "") or ""


def _kept_optimizer_mro(cls: type) -> list[type]:
    kept: list[type] = []
    for item in cls.__mro__:
        if _is_torch_optimizer_base(item):
            break
        if item is object:
            break
        if _should_skip_mro_class(item):
            continue
        kept.append(item)
    return kept


def _is_torch_optimizer_base(cls: type) -> bool:
    if getattr(cls, "__name__", "") != "Optimizer":
        return False
    mod = getattr(cls, "__module__", "") or ""
    return (
        mod == "torch.optim"
        or mod == "torch.optim.optimizer"
        or mod.startswith("torch.optim.")
    )


def _kept_loss_mro(cls: type) -> list[type]:
    kept: list[type] = []
    for item in cls.__mro__:
        if _is_torch_nn_module_class(item):
            break
        if item is object:
            break
        if _should_skip_mro_class(item) or _is_torch_nn_class(item):
            continue
        kept.append(item)
    return kept


def _kept_dataset_mro(cls: type) -> list[type]:
    kept: list[type] = []
    for item in cls.__mro__:
        if _is_torch_dataset_base(item):
            break
        if item is object:
            break
        if _should_skip_mro_class(item):
            continue
        kept.append(item)
    return kept


def _is_torch_dataset_base(cls: type) -> bool:
    if getattr(cls, "__name__", "") not in {"Dataset", "IterableDataset"}:
        return False
    mod = getattr(cls, "__module__", "") or ""
    return mod == "torch.utils.data" or mod.startswith("torch.utils.data.")


def _is_torch_nn_module_class(cls: type) -> bool:
    if getattr(cls, "__name__", "") != "Module":
        return False
    mod = getattr(cls, "__module__", "") or ""
    return (
        mod == "torch.nn"
        or mod == "torch.nn.modules.module"
        or mod.startswith("torch.nn.modules.module")
    )


def _is_torch_nn_class(cls: type) -> bool:
    mod = getattr(cls, "__module__", "") or ""
    return mod == "torch.nn" or mod.startswith("torch.nn.")


def _should_skip_mro_class(cls: type) -> bool:
    mod = getattr(cls, "__module__", "") or ""
    if not mod or mod == "builtins" or mod.startswith("builtins."):
        return True
    return mod == "__builtin__"


def _param_groups_meta(groups: Any) -> tuple[list[dict], int]:
    if groups is None:
        return [], 0
    try:
        iterable = list(groups)
    except TypeError:
        return [], 0
    out: list[dict] = []
    total_params = 0
    for group in iterable:
        meta = _param_group_meta(group)
        out.append(meta)
        total_params += int(meta.get("num_params") or 0)
    return out, total_params


def _param_group_meta(group: Any) -> dict:
    if not isinstance(group, dict):
        converted = json_safe(group)
        return converted if isinstance(converted, dict) else {"value": converted}
    out: dict[str, Any] = {}
    for key, value in group.items():
        if key == "params":
            items = _param_items(value)
            out["num_tensors"] = len(items)
            out["num_params"] = sum(_numel(item) for item in items)
            continue
        converted = json_safe(value)
        if converted is not None or value is None:
            out[key] = converted
    return out


def _param_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if _looks_like_tensor(value):
        return [value]
    if isinstance(value, (str, bytes, dict)):
        return []
    try:
        return list(value)
    except TypeError:
        return [value]


def _numel(tensor: Any) -> int:
    numel = getattr(tensor, "numel", None)
    if callable(numel):
        try:
            return int(numel())
        except Exception:
            return 0
    nelement = getattr(tensor, "nelement", None)
    if callable(nelement):
        try:
            return int(nelement())
        except Exception:
            return 0
    return 0


def _looks_like_tensor(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str, bytes, dict, list, tuple)):
        return False
    return hasattr(value, "dtype") and hasattr(value, "shape") and (
        hasattr(value, "numel") or hasattr(value, "nelement")
    )


def _tensor_meta(value: Any) -> dict[str, Any]:
    dtype = getattr(value, "dtype", None)
    shape = getattr(value, "shape", None)
    meta: dict[str, Any] = {
        "dtype": str(dtype) if dtype is not None else None,
        "shape": _shape_list(shape),
        "numel": _numel(value),
    }
    return meta


def _shape_list(shape: Any) -> list[int] | None:
    if shape is None:
        return None
    try:
        return [int(dim) for dim in list(shape)]
    except TypeError:
        try:
            return [int(shape)]
        except (TypeError, ValueError):
            return None


def _loss_kind(obj: Any) -> str:
    if isinstance(obj, functools.partial):
        return "partial"
    if inspect.isfunction(obj) or inspect.ismethod(obj):
        return "function"
    if inspect.isclass(obj):
        return "callable"
    if _looks_like_module(obj):
        return "module"
    if callable(obj):
        return "callable"
    return "callable"


def _looks_like_module(obj: Any) -> bool:
    if hasattr(obj, "forward") and callable(getattr(obj, "forward")):
        return True
    for item in type(obj).__mro__:
        if _is_torch_nn_module_class(item):
            return True
    return False


def _loss_targets(loss: Any, kind: str) -> tuple[Any, list[type], Any]:
    if kind == "partial":
        wrapped = loss.func
        if inspect.isclass(wrapped):
            return wrapped, _kept_loss_mro(wrapped), wrapped
        return wrapped, [], wrapped
    if kind == "function":
        target = loss.__func__ if inspect.ismethod(loss) else loss
        return target, [], target
    cls = loss if inspect.isclass(loss) else type(loss)
    return cls, _kept_loss_mro(cls), cls


def _loss_source_files(
    classes: list[type], source_target: Any
) -> list[tuple[Path, str]]:
    if classes:
        return collect_class_source_files(classes, LOSS_MANIFEST_PREFIX, skip_torch=True)
    return _callable_source_files(source_target, LOSS_MANIFEST_PREFIX)


def _callable_source_files(obj: Any, prefix: str) -> list[tuple[Path, str]]:
    target = obj.func if isinstance(obj, functools.partial) else obj
    if inspect.ismethod(target):
        target = target.__func__
    if not inspect.isfunction(target) and not inspect.isclass(target):
        target = type(target)
    module_name = getattr(target, "__module__", "") or ""
    if is_torch_package(module_name):
        return []
    path = source_file(target)
    if path is None:
        return []
    return [(path, manifest_path_for_source(path, module_name, prefix))]


def _constructor_attrs(obj: Any) -> dict[str, Any]:
    cls = type(obj)
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return {}
    out: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if not hasattr(obj, param_name):
            continue
        value = getattr(obj, param_name)
        if _looks_like_tensor(value):
            out[param_name] = _tensor_meta(value)
            continue
        converted = json_safe(value)
        if converted is not None or value is None:
            out[param_name] = converted
    return out


def _split_loader_and_dataset(obj: Any) -> tuple[Any | None, Any | None]:
    dataset = getattr(obj, "dataset", None)
    if _looks_like_dataloader(obj) and dataset is not None:
        return obj, dataset
    if _looks_like_dataloader(obj):
        return obj, None
    return None, obj


def _looks_like_dataloader(obj: Any) -> bool:
    cls = type(obj)
    name = getattr(cls, "__name__", "")
    mod = getattr(cls, "__module__", "") or ""
    if name == "DataLoader":
        return True
    if mod.startswith("torch.utils.data") and "DataLoader" in name:
        return True
    if getattr(obj, "dataset", None) is not None and (
        hasattr(obj, "batch_size") or hasattr(obj, "collate_fn") or hasattr(obj, "batch_sampler")
    ):
        return True
    return False


def _infer_shuffle(loader: Any) -> bool | None:
    shuffle = getattr(loader, "shuffle", None)
    if isinstance(shuffle, bool):
        return shuffle
    sampler = getattr(loader, "sampler", None)
    if sampler is None:
        return None
    name = type(sampler).__name__
    if name == "RandomSampler" or name.endswith("RandomSampler"):
        return True
    if name == "SequentialSampler" or name.endswith("SequentialSampler"):
        return False
    return None


def _callable_ref(obj: Any) -> dict[str, Any]:
    if isinstance(obj, functools.partial):
        ident = callable_identity(obj.func)
        ident["kind"] = "partial"
        return ident
    if inspect.isfunction(obj) or inspect.ismethod(obj):
        ident = callable_identity(obj)
        ident["kind"] = "function"
        return ident
    if inspect.isclass(obj):
        ident = class_identity(obj)
        ident["kind"] = "class"
        return ident
    ident = callable_identity(obj)
    ident["kind"] = "callable"
    return ident


def _safe_len(obj: Any) -> int | None:
    try:
        return len(obj)
    except Exception:
        return None


def _dedupe_source_files(files: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    seen: set[Path] = set()
    out: list[tuple[Path, str]] = []
    for path, manifest in files:
        if path in seen:
            continue
        seen.add(path)
        out.append((path, manifest))
    return out

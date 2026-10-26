"""Detect model, optimizer, loss, and dataloader objects from a PyTorch run.

Hooks are installed without importing torch. Discovery is deterministic:
parameter overlap, loss modules, backward producers, and the caller frame.
Explicit ``track_*`` calls win because they record ``run._tracked`` first.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
import weakref
from typing import Any

from .auto_metrics import StepMetrics
from ._frame_inspect import (
    infer_variable_name,
    normalize_component_name,
    resolve_loss_from_backward_frame,
)

logger = logging.getLogger("agenticml.auto")

_LOCK = threading.RLock()
_TRACKERS: list[weakref.ref] = []
_FINDER: "_TorchImportFinder | None" = None
_PRE_HANDLE: Any = None
_POST_HANDLE: Any = None
_STEP_HANDLE: Any = None
_ORIG_BACKWARD: Any = None
_BACKWARD_WRAPPED = False
_ORIG_LOADER_ITER: Any = None
_LOADER_WRAPPED = False
_SCALER_ORIGINALS: list[tuple[Any, Any]] = []
_LOSS_TYPE: Any = None
_MAX_PRODUCERS = 128
_SKIP_MODULE_PREFIXES = ("agenticml", "torch")


class AutoTracker:
    """Watch one run and snapshot components on the first optimizer step."""

    def __init__(self, run: Any, metrics_every: int | None = None):
        self.run = run
        # Loss / lr / epoch logging; keeps the backward wrapper installed.
        self._metrics = StepMetrics(run, metrics_every) if metrics_every else None
        self._dead = False
        self._finalized = False
        self._forward_detached = False
        self._any_step = False
        self._inline_tracked = False
        self._tls = threading.local()
        self._roots: dict[int, Any] = {}
        self._losses: dict[int, Any] = {}
        self._producers: dict[int, Any] = {}
        self._loaders: dict[int, dict[str, Any]] = {}
        self._opt_param_ids: set[int] = set()
        self._resolved_opts: set[int] = set()
        self._opt_steps: dict[int, int] = {}
        self._claimed: dict[str, set[str]] = {}
        self._pending_loss: dict[str, Any] | None = None

    def install(self) -> None:
        if self._dead:
            return
        with _LOCK:
            _TRACKERS.append(weakref.ref(self))
            if sys.modules.get("torch") is not None:
                _ensure_torch_hooks()
            else:
                _ensure_finder()

    def mark_dead(self) -> None:
        """Disable this tracker without taking locks. Safe after fork."""
        self._dead = True
        self._forward_detached = True

    def detach(self) -> None:
        """Remove this tracker and restore process-wide hooks when none remain."""
        self.mark_dead()
        owner = getattr(self.run, "_owner_pid", None)
        if owner is not None and os.getpid() != owner:
            return
        _release_tracker(self)

    def finalize(self) -> None:
        """Snapshot anything the step hook never saw, then detach."""
        if self._finalized:
            return
        self._finalized = True
        try:
            if self._dead:
                return
            if not self._any_step:
                _scan_main(self)
            else:
                self._track_frozen()
                self._flush_loaders()
        except Exception:
            logger.debug("auto-track finalize failed", exc_info=True)
        finally:
            self.detach()

    def flush_metrics(self) -> None:
        """Log automatic metrics accumulated since the last log interval."""
        if self._metrics is not None and not self._dead:
            self._metrics.flush()

    def _on_forward_pre(self, module: Any) -> None:
        if self._forward_detached or self._dead:
            return
        depth = int(getattr(self._tls, "depth", 0) or 0)
        try:
            if depth == 0:
                self._producers.clear()
                if not _is_loss_module(module):
                    _remember(self._roots, module)
            if _is_loss_module(module):
                _remember(self._losses, module)
        finally:
            self._tls.depth = depth + 1

    def _on_forward_post(self, module: Any, output: Any) -> None:
        if self._forward_detached or self._dead:
            return
        depth = int(getattr(self._tls, "depth", 0) or 0)
        self._tls.depth = max(0, depth - 1)
        if len(self._producers) >= _MAX_PRODUCERS:
            return
        for tensor in _scalar_tensors(output):
            if id(tensor) not in self._producers:
                _remember(self._producers, tensor, module)

    def _on_backward(self, tensor: Any) -> None:
        if self._dead:
            return
        if self._metrics is not None:
            self._metrics.on_backward(tensor)
        if self._forward_detached:
            return
        frame = _external_frame()
        resolved = (
            resolve_loss_from_backward_frame(frame)
            if frame is not None
            else {
                "evidence": "inline_expression",
                "callable": None,
                "expression": None,
                "file": None,
                "line": None,
            }
        )
        grad_fn = getattr(tensor, "grad_fn", None)
        grad_name = type(grad_fn).__name__ if grad_fn is not None else None
        self._pending_loss = {
            "tensor_id": id(tensor),
            "scaled": grad_name == "MulBackward0",
            "grad_fn": grad_name,
            "resolved": resolved,
        }
        self._tls.depth = 0

    def _on_step(self, optimizer: Any) -> None:
        if self._dead:
            return
        self._tls.depth = 0
        self._any_step = True
        opt_id = id(optimizer)
        self._opt_steps[opt_id] = self._opt_steps.get(opt_id, 0) + 1
        self._opt_param_ids |= _optimizer_param_ids(optimizer)
        if opt_id not in self._resolved_opts:
            self._resolved_opts.add(opt_id)
            self._resolve(optimizer)
        loader = getattr(self._tls, "loader", None)
        if loader is not None:
            self._mark_loader(loader, "train", "optimizer_step")
        if self._metrics is not None:
            self._metrics.on_step(optimizer)
        if self._opt_steps[opt_id] >= 2 and not self._forward_detached:
            self._stop_forward()

    def _on_scale(self, scaled: Any, unscaled: Any) -> None:
        if self._dead or self._metrics is None:
            return
        self._metrics.on_scale(scaled, unscaled)

    def _note_loader(self, loader: Any) -> None:
        if self._dead:
            return
        slot = self._loaders.get(id(loader))
        if slot is None:
            self._loaders[id(loader)] = {"ref": _ref(loader), "role": None}

    def _set_loader(self, loader: Any) -> None:
        if self._dead:
            return
        self._note_loader(loader)
        self._tls.loader = loader

    def _finish_loader(self, loader: Any) -> None:
        if self._dead:
            return
        if getattr(self._tls, "loader", None) is loader:
            self._tls.loader = None
        slot = self._loaders.get(id(loader))
        if self._metrics is not None and slot is not None and slot["role"] == "train":
            self._metrics.on_epoch_end(loader)
        if slot is None or slot["role"] is not None:
            return
        self._mark_loader(loader, "eval", "iteration")

    def _resolve(self, optimizer: Any) -> None:
        model = self._match_model(optimizer)
        if model is not None:
            self._emit("model", model, evidence="parameter_overlap")
        self._emit_loss()
        self._emit("optimizer", optimizer, evidence="optimizer_step")

    def _match_model(self, optimizer: Any) -> Any:
        opt_ids = _optimizer_param_ids(optimizer)
        best = None
        best_n = 0
        for ref in list(self._roots.values()):
            module = _deref(ref)
            if module is None:
                continue
            overlap = len(_module_param_ids(module) & opt_ids)
            if overlap > best_n:
                best = module
                best_n = overlap
        return best

    def _emit_loss(self) -> None:
        for ref in list(self._losses.values()):
            module = _deref(ref)
            if module is not None:
                self._emit("loss", module, evidence="loss_module")
        if any(_deref(ref) is not None for ref in self._losses.values()):
            return
        pending = self._pending_loss
        if pending and not pending.get("scaled"):
            ref = self._producers.get(pending["tensor_id"])
            module = _deref(ref) if ref is not None else None
            if module is not None:
                self._emit("loss", module, evidence="backward_producer")
                return
        resolved = (pending or {}).get("resolved") or {}
        if resolved.get("evidence") == "ast_assignment" and resolved.get("callable") is not None:
            self._emit("loss", resolved["callable"], evidence="ast_assignment")
            return
        extra = {
            "expression": resolved.get("expression"),
            "file": resolved.get("file"),
            "line": resolved.get("line"),
            "grad_fn": (pending or {}).get("grad_fn"),
        }
        self._emit("loss", None, evidence="inline_expression", extra=extra)

    def _track_frozen(self) -> None:
        trained: list[Any] = []
        frozen: list[Any] = []
        for ref in list(self._roots.values()):
            module = _deref(ref)
            if module is None:
                continue
            overlap = len(_module_param_ids(module) & self._opt_param_ids)
            if overlap > 0:
                trained.append(module)
            else:
                frozen.append(module)
        for module in frozen:
            if _is_child_of_any(module, trained) or _is_child_of_any(module, frozen):
                continue
            if ("loss", id(module)) in getattr(self.run, "_tracked", {}):
                continue
            self._emit("model", module, evidence="no_optimizer_params", role="frozen")

    def _flush_loaders(self) -> None:
        for slot in list(self._loaders.values()):
            if slot["role"] is not None:
                continue
            loader = _deref(slot["ref"])
            if loader is None:
                continue
            self._mark_loader(loader, "eval", "iteration")

    def _mark_loader(self, loader: Any, role: str, evidence: str) -> None:
        slot = self._loaders.get(id(loader))
        if slot is None:
            self._note_loader(loader)
            slot = self._loaders[id(loader)]
        if slot["role"] is not None:
            return
        slot["role"] = role
        self._emit("dataloader", loader, evidence=evidence, role=role)

    def _stop_forward(self) -> None:
        self._forward_detached = True
        _maybe_unhook_forward()

    def _emit(
        self,
        kind: str,
        obj: Any,
        *,
        evidence: str,
        role: str | None = None,
        extra: dict | None = None,
        name_hint: str | None = None,
    ) -> None:
        tracked = getattr(self.run, "_tracked", {})
        if obj is not None and (kind, id(obj)) in tracked:
            return
        if obj is None:
            if self._inline_tracked:
                return
            self._inline_tracked = True
        if name_hint is not None:
            base = normalize_component_name(name_hint)
        elif obj is not None:
            base = _object_name(obj)
        else:
            base = "default"
        name = self._claim_name(kind, base)
        meta: dict[str, Any] = {"detected_by": "auto", "evidence": evidence}
        if role:
            meta["role"] = role
        if extra:
            for key, value in extra.items():
                if key not in meta:
                    meta[key] = value
        method = getattr(self.run, "track_" + kind)
        try:
            method(obj, name=name, extra_metadata=meta)
        except Exception:
            logger.debug("auto-track %s failed", kind, exc_info=True)

    def _claim_name(self, kind: str, base: str) -> str:
        used = set(self._claimed.get(kind, ()))
        tracked = getattr(self.run, "_tracked", {})
        for (tracked_kind, _obj_id), existing in list(tracked.items()):
            if tracked_kind == kind and isinstance(existing, str):
                used.add(existing)
        name = base or "default"
        number = 2
        while name in used:
            name = f"{base or 'default'}_{number}"
            number += 1
        self._claimed.setdefault(kind, set()).add(name)
        return name


def force_uninstall() -> None:
    """Restore torch hooks. Used by tests after a failed run."""
    with _LOCK:
        _TRACKERS.clear()
        _uninstall_forward()
        _uninstall_backward()
        _uninstall_step_and_loader()
        _remove_finder()


class _TorchImportFinder:
    """Install hooks once torch is imported. Does not import torch itself."""

    def __init__(self):
        self._inside = False

    def find_spec(self, fullname, path, target=None):
        if fullname != "torch" or self._inside:
            return None
        self._inside = True
        removed = False
        try:
            try:
                sys.meta_path.remove(self)
                removed = True
            except ValueError:
                pass
            spec = _find_spec_from_others("torch", path, target)
            if spec is None or getattr(spec, "loader", None) is None:
                return None
            spec.loader = _ExecWrapper(spec.loader)
            removed = False
            return spec
        finally:
            self._inside = False
            if removed and self not in sys.meta_path:
                sys.meta_path.insert(0, self)


class _ExecWrapper:
    def __init__(self, loader: Any):
        self._loader = loader

    def create_module(self, spec):
        create = getattr(self._loader, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module):
        exec_module = getattr(self._loader, "exec_module", None)
        if exec_module is not None:
            exec_module(module)
        else:
            self._loader.load_module(module.__name__)
        try:
            with _LOCK:
                _ensure_torch_hooks()
        except Exception:
            logger.debug("failed to install torch hooks", exc_info=True)


class _LoaderIter:
    def __init__(self, loader: Any, inner: Any):
        self._loader = loader
        self._inner = inner

    def __iter__(self):
        return self

    def __next__(self):
        for tracker in _live_trackers():
            tracker._set_loader(self._loader)
        try:
            return next(self._inner)
        except StopIteration:
            for tracker in _live_trackers():
                tracker._finish_loader(self._loader)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _ensure_finder() -> None:
    global _FINDER
    if _FINDER is not None and _FINDER in sys.meta_path:
        return
    _FINDER = _TorchImportFinder()
    sys.meta_path.insert(0, _FINDER)


def _remove_finder() -> None:
    global _FINDER
    if _FINDER is not None:
        try:
            sys.meta_path.remove(_FINDER)
        except ValueError:
            pass
    _FINDER = None


def _ensure_torch_hooks() -> None:
    torch = sys.modules.get("torch")
    if torch is None:
        return
    _install_forward(torch)
    _install_backward(torch)
    _install_step(torch)
    _install_loader(torch)
    _install_scaler(torch)


def _install_forward(torch: Any) -> None:
    global _PRE_HANDLE, _POST_HANDLE
    if _PRE_HANDLE is not None or _POST_HANDLE is not None:
        return
    module_api = importlib.import_module("torch.nn.modules.module")
    pre = module_api.register_module_forward_pre_hook
    post = module_api.register_module_forward_hook
    _PRE_HANDLE = pre(_dispatch_forward_pre)
    try:
        _POST_HANDLE = post(_dispatch_forward_post, always_call=True)
    except TypeError:
        _POST_HANDLE = post(_dispatch_forward_post)


def _install_backward(torch: Any) -> None:
    global _ORIG_BACKWARD, _BACKWARD_WRAPPED
    current = torch.Tensor.backward
    if getattr(current, "_agenticml_wrapped", False):
        _BACKWARD_WRAPPED = True
        return
    _ORIG_BACKWARD = current

    def wrapped(tensor, *args, **kwargs):
        try:
            _dispatch_backward(tensor)
        except Exception:
            logger.debug("backward hook failed", exc_info=True)
        return _ORIG_BACKWARD(tensor, *args, **kwargs)

    wrapped._agenticml_wrapped = True
    torch.Tensor.backward = wrapped
    _BACKWARD_WRAPPED = True


def _install_step(torch: Any) -> None:
    global _STEP_HANDLE
    if _STEP_HANDLE is not None:
        return
    optim_mod = importlib.import_module("torch.optim.optimizer")
    register = getattr(optim_mod, "register_optimizer_step_pre_hook", None)
    if register is None:
        logger.debug("optimizer step hook is unavailable; detection waits for finish()")
        return
    _STEP_HANDLE = register(_dispatch_step)


def _install_loader(torch: Any) -> None:
    global _ORIG_LOADER_ITER, _LOADER_WRAPPED
    if _LOADER_WRAPPED:
        return
    data_mod = importlib.import_module("torch.utils.data")
    dataloader = data_mod.DataLoader
    current = dataloader.__iter__
    if getattr(current, "_agenticml_wrapped", False):
        _LOADER_WRAPPED = True
        return
    _ORIG_LOADER_ITER = current

    def wrapped(loader):
        for tracker in _live_trackers():
            tracker._note_loader(loader)
        return _LoaderIter(loader, _ORIG_LOADER_ITER(loader))

    wrapped._agenticml_wrapped = True
    dataloader.__iter__ = wrapped
    _LOADER_WRAPPED = True


def _install_scaler(torch: Any) -> None:
    """Wrap ``GradScaler.scale`` so auto metrics log the unscaled loss."""
    if _SCALER_ORIGINALS:
        return
    classes = []
    for module_name in ("torch.amp", "torch.cuda.amp"):
        try:
            cls = getattr(importlib.import_module(module_name), "GradScaler", None)
        except Exception:
            cls = None
        if cls is not None and cls not in classes:
            classes.append(cls)
    for cls in classes:
        current = cls.__dict__.get("scale")
        if current is None or getattr(current, "_agenticml_wrapped", False):
            continue
        _SCALER_ORIGINALS.append((cls, current))
        cls.scale = _wrap_scale(current)


def _wrap_scale(original: Any) -> Any:
    def wrapped(scaler, outputs, *args, **kwargs):
        result = original(scaler, outputs, *args, **kwargs)
        if result is not outputs:
            for tracker in _live_trackers():
                try:
                    tracker._on_scale(result, outputs)
                except Exception:
                    logger.debug("scale hook failed", exc_info=True)
        return result

    wrapped._agenticml_wrapped = True
    return wrapped


def _uninstall_forward() -> None:
    global _PRE_HANDLE, _POST_HANDLE
    for handle in (_PRE_HANDLE, _POST_HANDLE):
        if handle is not None:
            try:
                handle.remove()
            except Exception:
                logger.debug("failed to remove forward hook", exc_info=True)
    _PRE_HANDLE = None
    _POST_HANDLE = None


def _uninstall_backward() -> None:
    global _ORIG_BACKWARD, _BACKWARD_WRAPPED
    torch = sys.modules.get("torch")
    if (
        _BACKWARD_WRAPPED
        and torch is not None
        and _ORIG_BACKWARD is not None
        and getattr(torch.Tensor.backward, "_agenticml_wrapped", False)
    ):
        torch.Tensor.backward = _ORIG_BACKWARD
    _BACKWARD_WRAPPED = False
    _ORIG_BACKWARD = None


def _uninstall_step_and_loader() -> None:
    global _STEP_HANDLE, _ORIG_LOADER_ITER, _LOADER_WRAPPED
    if _STEP_HANDLE is not None:
        try:
            _STEP_HANDLE.remove()
        except Exception:
            logger.debug("failed to remove optimizer hook", exc_info=True)
    _STEP_HANDLE = None
    torch = sys.modules.get("torch")
    if _LOADER_WRAPPED and torch is not None and _ORIG_LOADER_ITER is not None:
        try:
            data_mod = importlib.import_module("torch.utils.data")
            if getattr(data_mod.DataLoader.__iter__, "_agenticml_wrapped", False):
                data_mod.DataLoader.__iter__ = _ORIG_LOADER_ITER
        except Exception:
            logger.debug("failed to restore DataLoader.__iter__", exc_info=True)
    _LOADER_WRAPPED = False
    _ORIG_LOADER_ITER = None
    while _SCALER_ORIGINALS:
        cls, original = _SCALER_ORIGINALS.pop()
        try:
            if getattr(cls.__dict__.get("scale"), "_agenticml_wrapped", False):
                cls.scale = original
        except Exception:
            logger.debug("failed to restore GradScaler.scale", exc_info=True)


def _maybe_unhook_forward() -> None:
    with _LOCK:
        if not _any_wants_forward_unlocked():
            _uninstall_forward()
        if not _any_wants_backward_unlocked():
            _uninstall_backward()


def _release_tracker(tracker: AutoTracker) -> None:
    with _LOCK:
        _drop_tracker(tracker)
        if _live_trackers_unlocked():
            if not _any_wants_forward_unlocked():
                _uninstall_forward()
            if not _any_wants_backward_unlocked():
                _uninstall_backward()
            return
        _uninstall_forward()
        _uninstall_backward()
        _uninstall_step_and_loader()
        _remove_finder()


def _drop_tracker(tracker: AutoTracker) -> None:
    kept: list[weakref.ref] = []
    for ref in _TRACKERS:
        found = ref()
        if found is None or found is tracker:
            continue
        kept.append(ref)
    _TRACKERS[:] = kept


def _any_wants_forward_unlocked() -> bool:
    for tracker in _live_trackers_unlocked():
        if not tracker._forward_detached:
            return True
    return False


def _any_wants_backward_unlocked() -> bool:
    for tracker in _live_trackers_unlocked():
        if not tracker._forward_detached or tracker._metrics is not None:
            return True
    return False


def _live_trackers() -> list[AutoTracker]:
    with _LOCK:
        return _live_trackers_unlocked()


def _live_trackers_unlocked() -> list[AutoTracker]:
    alive: list[AutoTracker] = []
    kept: list[weakref.ref] = []
    for ref in _TRACKERS:
        tracker = ref()
        if tracker is None or tracker._dead:
            continue
        kept.append(ref)
        run = tracker.run
        if getattr(run, "_finished", False) and not getattr(run, "_auto_finalizing", False):
            continue
        owner = getattr(run, "_owner_pid", None)
        if owner is not None and os.getpid() != owner:
            continue
        alive.append(tracker)
    _TRACKERS[:] = kept
    return alive


def _dispatch_forward_pre(module, *args, **kwargs) -> None:
    for tracker in _live_trackers():
        try:
            tracker._on_forward_pre(module)
        except Exception:
            logger.debug("forward pre hook failed", exc_info=True)


def _dispatch_forward_post(module, inputs, output=None, *extra, **kwargs) -> None:
    if output is None and extra:
        output = extra[0]
    for tracker in _live_trackers():
        try:
            tracker._on_forward_post(module, output)
        except Exception:
            logger.debug("forward post hook failed", exc_info=True)


def _dispatch_backward(tensor: Any) -> None:
    for tracker in _live_trackers():
        try:
            tracker._on_backward(tensor)
        except Exception:
            logger.debug("backward dispatch failed", exc_info=True)


def _dispatch_step(optimizer, *args, **kwargs) -> None:
    for tracker in _live_trackers():
        try:
            tracker._on_step(optimizer)
        except Exception:
            logger.debug("optimizer step hook failed", exc_info=True)


def _scan_main(tracker: AutoTracker) -> None:
    torch = sys.modules.get("torch")
    if torch is None:
        return
    main = sys.modules.get("__main__")
    if main is None:
        return
    try:
        items = list(vars(main).items())
    except Exception:
        logger.debug("failed to read __main__", exc_info=True)
        return
    values: list[tuple[str | None, Any]] = []
    for key, value in items:
        if not isinstance(key, str) or key.startswith("__"):
            continue
        values.append((key, value))
        if isinstance(value, dict):
            for inner in list(value.values())[:64]:
                values.append((None, inner))
        elif isinstance(value, (list, tuple)):
            for inner in list(value)[:64]:
                values.append((None, inner))
        if len(values) >= 500:
            break

    modules = []
    for _key, obj in values:
        if isinstance(obj, torch.nn.Module):
            modules.append(obj)
    child_ids: set[int] = set()
    for module in modules:
        try:
            children = list(module.modules())
        except Exception:
            continue
        for child in children:
            if child is not module:
                child_ids.add(id(child))
    loss_type = _loss_class()
    for module in modules:
        if id(module) in child_ids:
            continue
        hint = _main_name(module)
        if loss_type is not None and isinstance(module, loss_type):
            tracker._emit(
                "loss",
                module,
                evidence="main_globals_scan",
                name_hint=hint,
            )
        else:
            tracker._emit(
                "model",
                module,
                evidence="main_globals_scan",
                name_hint=hint,
            )

    optim_type = getattr(torch.optim, "Optimizer", None)
    data_mod = importlib.import_module("torch.utils.data")
    loader_type = data_mod.DataLoader
    dataset_type = data_mod.Dataset
    loaders = []
    for key, obj in values:
        if optim_type is not None and isinstance(obj, optim_type):
            tracker._emit(
                "optimizer",
                obj,
                evidence="main_globals_scan",
                name_hint=key if isinstance(key, str) else _main_name(obj),
            )
        elif isinstance(obj, loader_type):
            loaders.append(obj)
            tracker._emit(
                "dataloader",
                obj,
                evidence="main_globals_scan",
                name_hint=key if isinstance(key, str) else _main_name(obj),
            )
    covered = set()
    for loader in loaders:
        dataset = getattr(loader, "dataset", None)
        if dataset is not None:
            covered.add(id(dataset))
    for key, obj in values:
        if not isinstance(obj, dataset_type) or isinstance(obj, loader_type):
            continue
        if id(obj) in covered:
            continue
        tracker._emit(
            "dataloader",
            obj,
            evidence="main_globals_scan",
            name_hint=key if isinstance(key, str) else _main_name(obj),
        )


def _find_spec_from_others(fullname: str, path, target):
    for finder in list(sys.meta_path):
        if isinstance(finder, _TorchImportFinder):
            continue
        find = getattr(finder, "find_spec", None)
        if find is None:
            continue
        try:
            spec = find(fullname, path, target)
        except Exception:
            continue
        if spec is not None:
            return spec
    return None


def _external_frame():
    try:
        frame = sys._getframe(1)
    except ValueError:
        return None
    seen: set[int] = set()
    while frame is not None and id(frame) not in seen:
        seen.add(id(frame))
        module = ""
        try:
            module = frame.f_globals.get("__name__", "") or ""
        except Exception:
            module = ""
        if not any(module == prefix or module.startswith(prefix + ".") for prefix in _SKIP_MODULE_PREFIXES):
            return frame
        frame = getattr(frame, "f_back", None)
    return None


def _object_name(obj: Any) -> str:
    hinted = _main_name(obj)
    if hinted:
        return normalize_component_name(hinted)
    frame = _external_frame()
    if frame is None:
        return "default"
    return normalize_component_name(infer_variable_name(obj, start_frame=frame))


def _main_name(obj: Any) -> str | None:
    main = sys.modules.get("__main__")
    if main is None:
        return None
    try:
        items = list(vars(main).items())
    except Exception:
        return None
    for key, value in items:
        if isinstance(key, str) and not key.startswith("__") and value is obj:
            return key
    return None


def _remember(store: dict[int, Any], key_obj: Any, target: Any | None = None) -> None:
    store[id(key_obj)] = _ref(key_obj if target is None else target)


def _ref(obj: Any):
    try:
        return weakref.ref(obj)
    except TypeError:
        return lambda obj=obj: obj


def _deref(ref: Any) -> Any:
    if ref is None:
        return None
    try:
        return ref()
    except Exception:
        return None


def _loss_class():
    global _LOSS_TYPE
    if _LOSS_TYPE is not None:
        return _LOSS_TYPE
    try:
        loss_mod = importlib.import_module("torch.nn.modules.loss")
    except Exception:
        return None
    _LOSS_TYPE = getattr(loss_mod, "_Loss", None)
    return _LOSS_TYPE


def _is_loss_module(module: Any) -> bool:
    loss_type = _loss_class()
    if loss_type is None:
        return False
    try:
        return isinstance(module, loss_type)
    except Exception:
        return False


def _scalar_tensors(output: Any) -> list[Any]:
    found: list[Any] = []
    if _is_scalar_tensor(output):
        found.append(output)
        return found
    if isinstance(output, tuple) and len(output) <= 8:
        for item in output:
            if _is_scalar_tensor(item):
                found.append(item)
    return found


def _is_scalar_tensor(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return False
    try:
        dim = value.dim() if callable(getattr(value, "dim", None)) else None
        if dim is None:
            shape = getattr(value, "shape", None)
            if shape is None:
                return False
            return len(shape) == 0
        return int(dim) == 0
    except Exception:
        return False


def _module_param_ids(module: Any) -> set[int]:
    params = getattr(module, "parameters", None)
    if not callable(params):
        return set()
    found: set[int] = set()
    try:
        for param in params():
            found.add(id(param))
    except Exception:
        return found
    return found


def _optimizer_param_ids(optimizer: Any) -> set[int]:
    found: set[int] = set()
    groups = getattr(optimizer, "param_groups", None) or []
    try:
        for group in groups:
            if not isinstance(group, dict):
                continue
            for param in group.get("params", ()):
                found.add(id(param))
    except Exception:
        return found
    return found


def _is_child_of_any(module: Any, parents: list[Any]) -> bool:
    for parent in parents:
        if parent is module:
            continue
        modules = getattr(parent, "modules", None)
        if not callable(modules):
            continue
        try:
            for child in modules():
                if child is parent:
                    continue
                if child is module:
                    return True
        except Exception:
            continue
    return False

"""Mirror TensorBoard ``SummaryWriter`` calls into an agenticml run.

The writer keeps working exactly as before; each call is also sent to the run.
Patches ``torch.utils.tensorboard`` and ``tensorboardX`` when they are imported,
without importing them.

| SummaryWriter call            | agenticml                                        |
| ----------------------------- | ------------------------------------------------ |
| constructor                   | ``config["_tensorboard/log_dir"]``               |
| add_scalar / add_scalars      | ``run.log(..., step=global_step, commit=False)`` |
| add_hparams                   | hparams into ``config``, metrics into ``summary`` |
| add_image / add_images        | ``agenticml.Image`` at the same step             |
| add_figure                    | rendered PNG as ``agenticml.Image``              |
| flush / close                 | commit the pending step                          |
"""

from __future__ import annotations

import functools
import inspect
import io
import logging
import threading
import weakref
from typing import Any

from .._import_hooks import cancel, when_imported
from .._source_inspect import json_safe
from ..media import Image

logger = logging.getLogger("agenticml.tensorboard")

_WRITER_MODULES = ("torch.utils.tensorboard.writer", "tensorboardX.writer")
_LOCK = threading.RLock()
_RUN: Any = None
_PATCHED: list[tuple[type, str, Any]] = []
_TLS = threading.local()


def install_tensorboard_sync(run: Any) -> None:
    """Mirror every SummaryWriter in this process into ``run``."""
    global _RUN
    with _LOCK:
        _RUN = weakref.ref(run)
        for name in _WRITER_MODULES:
            when_imported(name, _patch_module)


def uninstall_tensorboard_sync(run: Any) -> None:
    global _RUN
    with _LOCK:
        if _RUN is None or _RUN() is not run:
            return
        _RUN = None
        for name in _WRITER_MODULES:
            cancel(name, _patch_module)
        while _PATCHED:
            cls, attr, original = _PATCHED.pop()
            if getattr(cls.__dict__.get(attr), "_agenticml_wrapped", False):
                setattr(cls, attr, original)


def _active_run() -> Any:
    ref = _RUN
    run = ref() if ref is not None else None
    if run is None or getattr(run, "_finished", False):
        return None
    return run


def _patch_module(module: Any) -> None:
    cls = getattr(module, "SummaryWriter", None)
    if not isinstance(cls, type):
        return
    with _LOCK:
        for attr, mirror, before in (
            ("__init__", _mirror_init, False),
            ("add_scalar", _mirror_scalar, False),
            ("add_scalars", _mirror_scalars, False),
            ("add_hparams", _mirror_hparams, False),
            ("add_image", _mirror_image, False),
            ("add_images", _mirror_images, False),
            # add_figure closes the figure by default, so render it first.
            ("add_figure", _mirror_figure, True),
            ("flush", _mirror_flush, False),
            ("close", _mirror_flush, False),
        ):
            _wrap(cls, attr, mirror, before)


def _wrap(cls: type, attr: str, mirror, before: bool) -> None:
    original = cls.__dict__.get(attr)
    if original is None or getattr(original, "_agenticml_wrapped", False):
        return
    try:
        signature = inspect.signature(original)
    except (TypeError, ValueError):
        return

    @functools.wraps(original)
    def wrapped(self, *args, **kwargs):
        # Only the outermost call is mirrored: add_hparams writes its metrics
        # through an inner writer's add_scalar, which must not log twice.
        outer = not getattr(_TLS, "depth", 0)
        if outer and before:
            _mirror(mirror, signature, self, args, kwargs)
        _TLS.depth = getattr(_TLS, "depth", 0) + 1
        try:
            result = original(self, *args, **kwargs)
        finally:
            _TLS.depth -= 1
        if outer and not before:
            _mirror(mirror, signature, self, args, kwargs)
        return result

    wrapped._agenticml_wrapped = True
    _PATCHED.append((cls, attr, original))
    setattr(cls, attr, wrapped)


def _mirror(mirror, signature, writer, args, kwargs) -> None:
    run = _active_run()
    if run is None:
        return
    try:
        bound = signature.bind(writer, *args, **kwargs)
        bound.apply_defaults()
        mirror(run, writer, bound.arguments)
    except Exception:
        logger.debug("TensorBoard mirror %s failed", getattr(mirror, "__name__", mirror), exc_info=True)


def _step(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _mirror_init(run, writer, a) -> None:
    log_dir = getattr(writer, "log_dir", None) or getattr(writer, "logdir", None)
    if log_dir:
        run.config.setdefault("_tensorboard/log_dir", str(log_dir))


def _mirror_scalar(run, writer, a) -> None:
    value = _number(a.get("scalar_value"))
    if value is None:
        return
    run.log({str(a["tag"]): value}, step=_step(a.get("global_step")), commit=False)


def _mirror_scalars(run, writer, a) -> None:
    main = str(a["main_tag"])
    metrics = {}
    for key, raw in (a.get("tag_scalar_dict") or {}).items():
        value = _number(raw)
        if value is not None:
            metrics[f"{main}/{key}"] = value
    if metrics:
        run.log(metrics, step=_step(a.get("global_step")), commit=False)


def _mirror_hparams(run, writer, a) -> None:
    hparams = json_safe(dict(a.get("hparam_dict") or {}))
    if hparams:
        run.config.update(hparams)
    for key, raw in (a.get("metric_dict") or {}).items():
        value = _number(raw)
        if value is not None:
            run.summary[str(key)] = value


def _mirror_image(run, writer, a) -> None:
    _log_image(run, writer, a["tag"], a["img_tensor"], a.get("dataformats") or "CHW", a)


def _mirror_images(run, writer, a) -> None:
    _log_image(run, writer, a["tag"], a["img_tensor"], a.get("dataformats") or "NCHW", a)


def _log_image(run, writer, tag, tensor, dataformats, a) -> None:
    if not _have_pillow():
        return
    array = _to_hwc(writer, tensor, dataformats)
    if array is None:
        return
    run.log({str(tag): Image(array)}, step=_step(a.get("global_step")), commit=False)


def _mirror_figure(run, writer, a) -> None:
    if not _have_pillow():
        return
    figure = a.get("figure")
    figures = list(figure) if isinstance(figure, (list, tuple)) else [figure]
    tag = str(a["tag"])
    images = {}
    for index, fig in enumerate(figures):
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png")
        key = tag if len(figures) == 1 else f"{tag}/{index}"
        images[key] = Image(buffer.getvalue())
    if images:
        run.log(images, step=_step(a.get("global_step")), commit=False)


def _mirror_flush(run, writer, a) -> None:
    with run._state_lock:
        if run._pending_metrics:
            run._commit_pending()


def _to_hwc(writer: Any, tensor: Any, dataformats: str) -> Any:
    """Convert like TensorBoard does, using the writer library's own helpers."""
    module = type(writer).__module__ or ""
    try:
        if module.startswith("tensorboardX"):
            from tensorboardX.utils import convert_to_HWC
            from tensorboardX.x2num import make_np
        else:
            from torch.utils.tensorboard._convert_np import make_np
            from torch.utils.tensorboard._utils import convert_to_HWC
        return convert_to_HWC(make_np(tensor), dataformats)
    except Exception:
        logger.debug("could not convert image for agenticml", exc_info=True)
        return None


@functools.lru_cache(maxsize=1)
def _have_pillow() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        logger.info("TensorBoard images are not mirrored to agenticml: Pillow is not installed")
        return False
    return True

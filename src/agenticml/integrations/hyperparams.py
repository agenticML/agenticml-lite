"""Capture command-line hyperparameters (argparse, Hydra) into ``run.config``.

Used by ``agenticml run``. Captured values never overwrite keys the script set
explicitly through ``init(config=...)``; a later parse of ``sys.argv``
replaces values from an earlier one (scripts often pre-parse, then parse).
"""

from __future__ import annotations

import argparse
import functools
import logging
import sys
import threading
import weakref
from typing import Any

from .._import_hooks import cancel, when_imported
from .._source_inspect import json_safe

logger = logging.getLogger("agenticml.hyperparams")

_LOCK = threading.RLock()
_RUN: Any = None
_CAPTURED: set[str] = set()
_ORIGINAL_PARSE: Any = None
_HYDRA_ORIGINAL: Any = None
_HYDRA_MODULE: Any = None
_HYDRA_CAPTURED = False
# Libraries whose own argparse parsers read sys.argv but do not hold hyperparameters.
_SKIP_CALLERS = ("hydra",)


def install_hyperparam_capture(run: Any) -> None:
    global _RUN, _ORIGINAL_PARSE, _HYDRA_CAPTURED
    with _LOCK:
        _RUN = weakref.ref(run)
        _CAPTURED.clear()
        _HYDRA_CAPTURED = False
        current = argparse.ArgumentParser.parse_known_args
        if not getattr(current, "_agenticml_wrapped", False):
            _ORIGINAL_PARSE = current
            argparse.ArgumentParser.parse_known_args = _wrap_parse(current)
        when_imported("hydra", _patch_hydra)


def uninstall_hyperparam_capture(run: Any) -> None:
    global _RUN, _ORIGINAL_PARSE, _HYDRA_ORIGINAL, _HYDRA_MODULE
    with _LOCK:
        if _RUN is None or _RUN() is not run:
            return
        _RUN = None
        if _ORIGINAL_PARSE is not None and getattr(
            argparse.ArgumentParser.parse_known_args, "_agenticml_wrapped", False
        ):
            argparse.ArgumentParser.parse_known_args = _ORIGINAL_PARSE
        _ORIGINAL_PARSE = None
        cancel("hydra", _patch_hydra)
        if _HYDRA_MODULE is not None and getattr(_HYDRA_MODULE.main, "_agenticml_wrapped", False):
            _HYDRA_MODULE.main = _HYDRA_ORIGINAL
        _HYDRA_MODULE = None
        _HYDRA_ORIGINAL = None


def _active_run() -> Any:
    ref = _RUN
    run = ref() if ref is not None else None
    if run is None or getattr(run, "_finished", False):
        return None
    return run


def merge_captured(values: dict[str, Any], *, argv: list[str] | None = None) -> None:
    """Merge captured hyperparameters into the active run's config."""
    run = _active_run()
    if run is None:
        return
    explicit = getattr(run, "_explicit_config_keys", set())
    with _LOCK:
        for key, value in values.items():
            if not isinstance(key, str) or key in explicit:
                continue
            if key in run.config and key not in _CAPTURED:
                continue  # set by the script, e.g. agenticml.config[key] = ...
            run.config[key] = _config_value(value)
            _CAPTURED.add(key)
        if argv is not None:
            run.config["_argv"] = list(argv)


def _config_value(value: Any) -> Any:
    converted = json_safe(value)
    if converted is None and value is not None:
        text = str(value)
        return text if len(text) <= 1000 else text[:1000] + "…"
    return converted


# ----- argparse -----


def _wrap_parse(original):
    @functools.wraps(original)
    def parse_known_args(self, args=None, namespace=None):
        result = original(self, args, namespace)
        # Only parses of the real command line; subparsers and parses of
        # explicit lists are someone else's business.
        if args is None and not _called_from_skipped_library():
            try:
                merge_captured(vars(result[0]), argv=sys.argv[1:])
            except Exception:
                logger.debug("argparse capture failed", exc_info=True)
        return result

    parse_known_args._agenticml_wrapped = True
    return parse_known_args


def _called_from_skipped_library() -> bool:
    frame = sys._getframe(2)
    while frame is not None:
        module = frame.f_globals.get("__name__", "") or ""
        if module != "argparse" and not module.startswith("agenticml"):
            return module.split(".", 1)[0] in _SKIP_CALLERS
        frame = frame.f_back
    return False


# ----- Hydra -----


def _patch_hydra(module: Any) -> None:
    global _HYDRA_ORIGINAL, _HYDRA_MODULE
    original = getattr(module, "main", None)
    if not callable(original) or getattr(original, "_agenticml_wrapped", False):
        return

    @functools.wraps(original)
    def main(*args, **kwargs):
        decorator = original(*args, **kwargs)

        def wrap(task_function):
            # functools.wraps sets __wrapped__, which Hydra follows to find the
            # calling file and config path.
            @functools.wraps(task_function)
            def task(cfg, *task_args, **task_kwargs):
                _capture_hydra(cfg)
                return task_function(cfg, *task_args, **task_kwargs)

            return decorator(task)

        return wrap

    main._agenticml_wrapped = True
    with _LOCK:
        _HYDRA_ORIGINAL = original
        _HYDRA_MODULE = module
        module.main = main


def _capture_hydra(cfg: Any) -> None:
    global _HYDRA_CAPTURED
    try:
        if _HYDRA_CAPTURED:
            return
        _HYDRA_CAPTURED = True
        if _is_multirun():
            logger.warning(
                "Hydra multirun under `agenticml run` records one run; "
                "config comes from the first job"
            )
        from omegaconf import OmegaConf

        try:
            values = OmegaConf.to_container(cfg, resolve=True)
        except Exception:
            values = OmegaConf.to_container(cfg, resolve=False)
        if isinstance(values, dict):
            merge_captured(values, argv=sys.argv[1:])
    except Exception:
        logger.debug("Hydra capture failed", exc_info=True)


def _is_multirun() -> bool:
    try:
        from hydra.core.hydra_config import HydraConfig
        from hydra.types import RunMode

        return HydraConfig.get().mode == RunMode.MULTIRUN
    except Exception:
        return False

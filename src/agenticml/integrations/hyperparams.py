"""Capture command-line hyperparameters (argparse, Hydra) into ``run.config``.

Used by ``agenticml run``. Captured values never overwrite keys the script set
explicitly through ``init(config=...)``; a later parse of ``sys.argv``
replaces values from an earlier one (scripts often pre-parse, then parse).
"""

from __future__ import annotations

import argparse
import functools
import logging
import re
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


_SECRET_SEGMENTS = {"token", "secret", "password", "passwd", "pwd", "credential", "credentials", "apikey", "bearer", "pat"}
_SECRET_PAIRS = {("api", "key"), ("access", "key"), ("secret", "key"),
                 ("private", "key"), ("auth", "token"), ("client", "secret")}


def _secret_key(key):
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(key))
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    segments = re.findall(r"[a-z0-9]+", name.lower())
    return bool(_SECRET_SEGMENTS.intersection(segments) or
                _SECRET_PAIRS.intersection(zip(segments, segments[1:])))


def redact_config(value):
    """Copy config, masking secret keys and command-line flag values."""
    if isinstance(value, dict):
        return {key: "***" if _secret_key(key) else
                _redact_argv(child) if key == "_argv" and isinstance(child, (list, tuple)) else
                redact_config(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_config(child) for child in value]
    return value


def _redact_argv(argv, parser=None):
    options = {}
    def add_actions(parser):
        for action in parser._actions:
            for option in action.option_strings:
                options[option] = action
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    add_actions(child)
    if parser is not None:
        add_actions(parser)
    clean = []
    remaining = 0
    for arg in argv:
        if not isinstance(arg, str):
            clean.append(arg)
            continue
        if arg == "--":
            clean.append(arg)
            remaining = 0
            break
        name, sep, value = arg.partition("=")
        action = options.get(name)
        attached = False
        if action is None and arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
            name = arg[:2]
            action = options.get(name)
            attached = action is not None and action.nargs != 0
        if action is not None:
            remaining = 0
            secret = action.nargs != 0 and any(_secret_key(key) for key in [action.dest, *action.option_strings])
            if secret:
                if attached or sep:
                    clean.append(name + ("=" if sep and not attached else "") + "***")
                    continue
                remaining = action.nargs if isinstance(action.nargs, int) else 1 if action.nargs in (None, "?") else float("inf")
        elif remaining and (not arg.startswith("-") or re.match(r"^-\d", arg)):
            clean.append("***")
            remaining -= 1
            continue
        else:
            remaining = 0
        if parser is None and arg.startswith("--") and sep and _secret_key(name):
            arg = name + "=***"
        clean.append(arg)
    clean.extend(argv[len(clean):])
    return clean


def merge_captured(values: dict[str, Any], *, argv: list[str] | None = None, parser=None) -> None:
    """Merge captured hyperparameters into the active run's config."""
    run = _active_run()
    if run is None:
        return
    explicit = getattr(run, "_explicit_config_keys", set())
    with _LOCK:
        for key, value in redact_config(values).items():
            if not isinstance(key, str) or key in explicit:
                continue
            if key in run.config and key not in _CAPTURED:
                continue  # set by the script, e.g. agenticml.config[key] = ...
            run.config[key] = _config_value(value)
            _CAPTURED.add(key)
        if argv is not None:
            run.config["_argv"] = _redact_argv(argv, parser)


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
                merge_captured(vars(result[0]), argv=sys.argv[1:], parser=self)
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

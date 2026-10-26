"""Run a callback when a module is imported, without importing it ourselves."""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Callable

logger = logging.getLogger("agenticml.imports")

_LOCK = threading.RLock()
_CALLBACKS: dict[str, list[Callable[[Any], None]]] = {}
_FINDER: "_PostImportFinder | None" = None


def when_imported(name: str, callback: Callable[[Any], None]) -> None:
    """Call ``callback(module)`` now if ``name`` is imported, else right after its import."""
    with _LOCK:
        module = sys.modules.get(name)
        if module is None:
            _CALLBACKS.setdefault(name, []).append(callback)
            _ensure_finder()
            return
    _call(callback, module)


def cancel(name: str, callback: Callable[[Any], None]) -> None:
    with _LOCK:
        callbacks = _CALLBACKS.get(name)
        if callbacks and callback in callbacks:
            callbacks.remove(callback)
        if not callbacks:
            _CALLBACKS.pop(name, None)
        if not _CALLBACKS:
            _remove_finder()


def _call(callback: Callable[[Any], None], module: Any) -> None:
    try:
        callback(module)
    except Exception:
        logger.debug("post-import hook for %s failed", getattr(module, "__name__", module), exc_info=True)


def _ensure_finder() -> None:
    global _FINDER
    if _FINDER is None:
        _FINDER = _PostImportFinder()
    if _FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _FINDER)


def _remove_finder() -> None:
    global _FINDER
    if _FINDER is not None:
        try:
            sys.meta_path.remove(_FINDER)
        except ValueError:
            pass
    _FINDER = None


class _PostImportFinder:
    def __init__(self):
        self._busy: set[str] = set()

    def find_spec(self, fullname, path, target=None):
        if fullname not in _CALLBACKS or fullname in self._busy:
            return None
        self._busy.add(fullname)
        try:
            spec = _find_spec_from_others(self, fullname, path, target)
        finally:
            self._busy.discard(fullname)
        if spec is None or getattr(spec, "loader", None) is None:
            return None
        spec.loader = _NotifyingLoader(spec.loader, fullname)
        return spec


class _NotifyingLoader:
    def __init__(self, loader: Any, fullname: str):
        self._loader = loader
        self._fullname = fullname

    def create_module(self, spec):
        create = getattr(self._loader, "create_module", None)
        return None if create is None else create(spec)

    def exec_module(self, module):
        # Hand the real loader back before the module body runs, so
        # __loader__ / __spec__.loader look exactly as without this hook.
        module.__loader__ = self._loader
        spec = getattr(module, "__spec__", None)
        if spec is not None:
            spec.loader = self._loader
        exec_module = getattr(self._loader, "exec_module", None)
        if exec_module is not None:
            exec_module(module)
        else:
            self._loader.load_module(module.__name__)
        with _LOCK:
            callbacks = _CALLBACKS.pop(self._fullname, [])
            if not _CALLBACKS:
                _remove_finder()
        for callback in callbacks:
            _call(callback, sys.modules.get(self._fullname, module))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)


def _find_spec_from_others(skip: Any, fullname: str, path, target):
    for finder in list(sys.meta_path):
        if finder is skip:
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

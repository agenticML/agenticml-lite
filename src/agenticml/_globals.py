"""Module-level singleton: `agenticml.init()`, `agenticml.log()`, `agenticml.finish()`.

Mirrors the wandb / neptune ergonomics: one active run per process. Power
users can still construct `Run` directly for multi-run scenarios.
"""

import logging
import os
import threading

from .distributed import is_main_process
from .run import Run

logger = logging.getLogger("agenticml")

_VALID_MODES = {"online", "offline", "disabled"}

_lock = threading.Lock()
_active_run = None  # may be a Run or _DisabledRun


class _DisabledRun:
    run_id = None
    notes = None
    group = None
    job_type = None
    _launcher_owned = False

    def __init__(self, verbose=False):
        # Per-instance — class-level mutable defaults would leak state
        # across init()/finish() cycles in the same process.
        self.config = {}
        self.summary = {}
        self.tags = []
        self._finished = False
        if verbose:
            print("agenticml: tracking disabled")

    def log(self, metrics, step=None, commit=True):
        return

    def log_artifact(self, file_path, name=None, type="file", metadata=None):
        return None

    def finish(self, status="finished"):
        if self._launcher_owned:
            return
        self._finished = True

    def track_files(self, *paths):
        return

    def track_model(self, model, name="default", extra_metadata=None):
        return

    def track_optimizer(self, optimizer, name="default", extra_metadata=None):
        return

    def track_loss(self, loss, name="default", extra_metadata=None):
        return

    def track_dataloader(self, dataloader, name="default", extra_metadata=None):
        return

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _resolve_mode(explicit):
    mode = (explicit or os.environ.get("AGENTICML_MODE") or "online").lower()
    if mode not in _VALID_MODES:
        logger.warning("unknown AGENTICML_MODE=%r; falling back to 'online'", mode)
        mode = "online"
    return mode


def init(
    project,
    name,
    mode=None,
    only_main_process=True,
    offline_dir=None,
    verbose=False,
    **kwargs,
):
    """Start a run and register it as the module-level active run.

    With `only_main_process=True` (default), non-rank-0 ranks in distributed
    training get a `_DisabledRun` automatically, so user code keeps calling
    `agenticml.log(...)` without coordinating logging duties manually.

    `mode` precedence: explicit arg > AGENTICML_MODE env > 'online'.
    `offline_dir` is only valid when the resolved mode is `"offline"`
    (explicit arg > AGENTICML_OFFLINE_DIR > ``~/.agenticml/offline``).
    `verbose=True` prints the run id and asset paths (or host) to stdout.
    """
    global _active_run

    if only_main_process and not is_main_process():
        with _lock:
            _active_run = _DisabledRun()
        return _active_run

    resolved_mode = _resolve_mode(mode)
    if offline_dir is not None and resolved_mode != "offline":
        raise ValueError(
            f"offline_dir is only valid when mode is 'offline', got mode={resolved_mode!r}"
        )

    with _lock:
        active = _active_run
        if (
            active is not None
            and getattr(active, "_launcher_owned", False)
            and not getattr(active, "_finished", False)
        ):
            _attach(active, project, name, kwargs)
            return active

        if _active_run is not None and not getattr(_active_run, "_finished", False):
            logger.warning("init() called with an active run; finishing previous run first")
            try:
                _active_run.finish()
            except Exception:
                logger.exception("failed to finish previous active run")

        if resolved_mode == "disabled":
            _active_run = _DisabledRun(verbose=verbose)
            return _active_run

        _active_run = Run(
            project=project,
            name=name,
            mode=resolved_mode,
            offline_dir=offline_dir,
            verbose=verbose,
            **kwargs,
        )
        return _active_run


def _attach(run, project, name, kwargs):
    """Merge a script's own init() into the run `agenticml run` started."""
    if (project, name) != (getattr(run, "project", project), getattr(run, "name", name)):
        logger.warning(
            "agenticml run already started run %r in project %r; "
            "keeping it instead of project=%r name=%r",
            getattr(run, "name", None),
            getattr(run, "project", None),
            project,
            name,
        )
    config = kwargs.get("config")
    if config:
        run.config.update(config)
        explicit = getattr(run, "_explicit_config_keys", None)
        if explicit is not None:
            explicit.update(config)
    for tag in kwargs.get("tags") or []:
        if tag not in run.tags:
            run.tags.append(tag)
    for attr in ("notes", "group", "job_type"):
        value = kwargs.get(attr)
        if value is not None and getattr(run, attr, None) is None:
            setattr(run, attr, value)


def _finish_launched(run, status):
    """Finalize the run `agenticml run` owns, after the script has returned."""
    global _active_run
    run._launcher_owned = False
    try:
        run.finish(status=status)
    finally:
        with _lock:
            if _active_run is run:
                _active_run = None


def get_active_run():
    return _active_run


def _require_active(method):
    run = _active_run
    if run is None:
        raise RuntimeError(f"agenticml.{method}() called before agenticml.init()")
    return run


def log(metrics, step=None, commit=True):
    _require_active("log").log(metrics, step=step, commit=commit)


def log_artifact(file_path, name=None, type="file", metadata=None):
    return _require_active("log_artifact").log_artifact(
        file_path, name=name, type=type, metadata=metadata
    )


def finish(status="finished"):
    global _active_run
    run = _active_run
    if run is None:
        return
    try:
        run.finish(status=status)
    finally:
        # A run owned by `agenticml run` stays active until the script returns.
        if not getattr(run, "_launcher_owned", False):
            _active_run = None


def track_files(*paths):
    _require_active("track_files").track_files(*paths)


def track_model(model, name="default", extra_metadata=None):
    _require_active("track_model").track_model(
        model, name=name, extra_metadata=extra_metadata
    )


def track_optimizer(optimizer, name="default", extra_metadata=None):
    _require_active("track_optimizer").track_optimizer(
        optimizer, name=name, extra_metadata=extra_metadata
    )


def track_loss(loss, name="default", extra_metadata=None):
    _require_active("track_loss").track_loss(
        loss, name=name, extra_metadata=extra_metadata
    )


def track_dataloader(dataloader, name="default", extra_metadata=None):
    _require_active("track_dataloader").track_dataloader(
        dataloader, name=name, extra_metadata=extra_metadata
    )


class _ProxyDict:
    """Forward dict ops to the active run's attribute (config or summary)."""

    def __init__(self, attr):
        self._attr = attr

    def _target(self):
        run = _active_run
        if run is None:
            raise RuntimeError(f"agenticml.{self._attr} accessed before agenticml.init()")
        return getattr(run, self._attr)

    def __getitem__(self, key): return self._target()[key]
    def __setitem__(self, key, value): self._target()[key] = value
    def __delitem__(self, key): del self._target()[key]
    def __contains__(self, key): return key in self._target()
    def __iter__(self): return iter(self._target())
    def __len__(self): return len(self._target())
    def get(self, key, default=None): return self._target().get(key, default)
    def update(self, *args, **kwargs): self._target().update(*args, **kwargs)
    def keys(self): return self._target().keys()
    def values(self): return self._target().values()
    def items(self): return self._target().items()

    def __repr__(self):
        run = _active_run
        if run is None:
            return f"<agenticml.{self._attr} (no active run)>"
        return repr(getattr(run, self._attr))


config = _ProxyDict("config")
summary = _ProxyDict("summary")

"""Run a script or module as ``__main__`` inside a tracked run.

This is the engine behind ``agenticml run``. It is kept separate from argument
parsing so other entry points (for example a future ``sitecustomize``
bootstrap) can reuse it with settings read from the environment.
"""

from __future__ import annotations

import builtins
import importlib.machinery
import importlib.util
import logging
import os
import signal
import sys
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path

from . import _globals
from .code_tracking import default_project, default_run_name
from .integrations.hyperparams import install_hyperparam_capture, uninstall_hyperparam_capture
from .store.layout import valid_run_id

logger = logging.getLogger("agenticml.launch")

_FALSE_VALUES = {"0", "false", "no", "off"}


class LaunchError(Exception):
    """The target could not be resolved; nothing was executed."""


class _Terminated(BaseException):
    """Raised in the main thread when SIGTERM arrives."""


@dataclass
class Target:
    kind: str  # "script" or "module"
    value: str
    argv: list[str] = field(default_factory=list)


def project_start_dir(target: Target) -> Path:
    """Directory used as ``default_project(start)`` for this target, without compiling it."""
    if target.kind == "script":
        return Path(target.value).expanduser().resolve().parent
    return Path(os.getcwd())


@dataclass
class RunSettings:
    project: str | None = None
    name: str | None = None
    tags: list[str] = field(default_factory=list)
    notes: str | None = None
    group: str | None = None
    job_type: str | None = None
    task: str | None = None
    parent_run_id: str | None = None
    allow_cross_task: bool = False
    mode: str | None = None
    dir: str | None = None
    auto_track: bool = True
    auto_metrics: bool = True
    log_every: int = 1
    sync_tensorboard: bool = True
    capture_args: bool = True
    verbose: bool = False

    @classmethod
    def from_env(cls) -> "RunSettings":
        env = os.environ
        tags = [t.strip() for t in env.get("AGENTICML_TAGS", "").split(",") if t.strip()]
        parent_run_id = env.get("AGENTICML_PARENT") or None
        if parent_run_id is not None and not valid_run_id(parent_run_id):
            raise ValueError("AGENTICML_PARENT must be a valid run id")

        def flag(name: str) -> bool:
            return env.get(name, "1").strip().lower() not in _FALSE_VALUES

        return cls(
            project=env.get("AGENTICML_PROJECT") or None,
            name=env.get("AGENTICML_NAME") or None,
            tags=tags,
            notes=env.get("AGENTICML_NOTES") or None,
            group=env.get("AGENTICML_GROUP") or None,
            job_type=env.get("AGENTICML_JOB_TYPE") or None,
            parent_run_id=parent_run_id,
            allow_cross_task=(
                env.get("AGENTICML_ALLOW_CROSS_TASK", "").strip().lower() in {"1", "true", "yes", "on"}
            ),
            auto_track=flag("AGENTICML_AUTO_TRACK"),
            auto_metrics=flag("AGENTICML_AUTO_METRICS"),
            log_every=parse_log_every(env.get("AGENTICML_LOG_EVERY", "1")),
            sync_tensorboard=flag("AGENTICML_SYNC_TENSORBOARD"),
            capture_args=flag("AGENTICML_CAPTURE_ARGS"),
        )


def parse_log_every(raw: str) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"log-every must be a positive integer, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"log-every must be a positive integer, got {raw!r}")
    return value


@dataclass
class _Resolved:
    code: types.CodeType
    filename: str
    argv0: str
    path0: str
    default_name: str
    spec: importlib.machinery.ModuleSpec | None


def run_target(target: Target, settings: RunSettings) -> int:
    """Init a run, execute ``target`` as ``__main__``, finish, return the exit code."""
    resolved = _resolve(target)

    sys.argv = [resolved.argv0, *target.argv]
    if not getattr(sys.flags, "safe_path", False):
        if sys.path:
            sys.path[0] = resolved.path0
        else:
            sys.path.insert(0, resolved.path0)

    original_main = sys.modules.get("__main__")
    main = _make_main_module(resolved)
    sys.modules["__main__"] = main

    run = _start_run(settings, resolved)
    restore_sigterm = _install_sigterm_handler()
    try:
        exit_code, exception_name = _execute(resolved.code, main.__dict__)
    finally:
        restore_sigterm()
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass

    try:
        if run is not None:
            _finish_run(run, exit_code, exception_name)
    finally:
        if original_main is not None:
            sys.modules["__main__"] = original_main
    return exit_code


def _resolve(target: Target) -> _Resolved:
    if target.kind == "script":
        path = Path(target.value).expanduser().resolve()
        if not path.is_file():
            raise LaunchError(f"can't open file {str(path)!r}: no such file")
        source = path.read_bytes()
        code = compile(source, str(path), "exec", dont_inherit=True)
        return _Resolved(
            code=code,
            filename=str(path),
            argv0=str(path),
            path0=str(project_start_dir(target)),
            default_name=default_run_name(path) or path.name,
            spec=None,
        )
    if target.kind == "module":
        cwd = os.getcwd()
        if not getattr(sys.flags, "safe_path", False):
            if sys.path:
                sys.path[0] = cwd
            else:
                sys.path.insert(0, cwd)
        name = target.value
        try:
            spec = importlib.util.find_spec(name)
            if spec is not None and spec.submodule_search_locations is not None:
                name = f"{name}.__main__"
                spec = importlib.util.find_spec(name)
        except (ImportError, ValueError) as exc:
            raise LaunchError(f"error while finding module specification for {name!r}: {exc}")
        if spec is None or spec.loader is None:
            raise LaunchError(f"no module named {name!r}")
        get_code = getattr(spec.loader, "get_code", None)
        code = get_code(name) if get_code is not None else None
        if code is None:
            raise LaunchError(f"no code object available for {name!r}")
        origin = spec.origin or name
        return _Resolved(
            code=code,
            filename=origin,
            argv0=origin,
            path0=str(project_start_dir(target)),
            default_name=default_run_name(spec.origin) or target.value,
            spec=spec,
        )
    raise LaunchError(f"unknown target kind {target.kind!r}")


def _make_main_module(resolved: _Resolved) -> types.ModuleType:
    main = types.ModuleType("__main__")
    main.__file__ = resolved.filename
    main.__builtins__ = builtins
    main.__cached__ = None
    if resolved.spec is not None:
        main.__spec__ = resolved.spec
        main.__loader__ = resolved.spec.loader
        main.__package__ = resolved.spec.parent
    else:
        main.__spec__ = None
        main.__loader__ = importlib.machinery.SourceFileLoader("__main__", resolved.filename)
        main.__package__ = None
    return main


def _start_run(settings: RunSettings, resolved: _Resolved):
    project = settings.project
    if not project:
        project = default_project(Path(resolved.path0))
    name = settings.name or resolved.default_name
    try:
        run = _globals.init(
            project=project,
            name=name,
            mode=settings.mode,
            dir=settings.dir,
            verbose=settings.verbose,
            tags=settings.tags or None,
            notes=settings.notes,
            group=settings.group,
            job_type=settings.job_type,
            task=settings.task,
            parent_run_id=settings.parent_run_id,
            allow_cross_task=settings.allow_cross_task,
            auto_track=settings.auto_track,
            auto_metrics=settings.auto_metrics,
            log_every=settings.log_every,
            sync_tensorboard=settings.sync_tensorboard,
        )
    except Exception:
        logger.exception("agenticml: failed to start a run; running the script untracked")
        return None
    run._launcher_owned = True
    if getattr(run, "_disabled_reason", None):
        print(f"agenticml: tracking disabled: {run._disabled_reason}", file=sys.stderr)
    if settings.capture_args and getattr(run, "run_id", None) is not None:
        install_hyperparam_capture(run)
    return run


def _execute(code: types.CodeType, namespace: dict) -> tuple[int, str | None]:
    """Run ``code`` like the interpreter would; return (exit code, exception name)."""
    try:
        exec(code, namespace)
    except SystemExit as exc:
        return _system_exit_code(exc), None
    except _Terminated:
        return 143, "SIGTERM"
    except KeyboardInterrupt as exc:
        _report_exception(exc)
        return 130, "KeyboardInterrupt"
    except BaseException as exc:
        _report_exception(exc)
        return 1, type(exc).__name__
    return 0, None


def _system_exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, bool):
        return int(code)
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def _report_exception(exc: BaseException) -> None:
    tb = exc.__traceback__
    # Drop this module's frames so the traceback starts in the user's code.
    while tb is not None and tb.tb_frame.f_globals.get("__name__") == __name__:
        tb = tb.tb_next
    # The default hook prints exc.__traceback__, not the tb argument.
    exc = exc.with_traceback(tb)
    try:
        sys.excepthook(type(exc), exc, tb)
    except Exception:
        import traceback

        traceback.print_exception(type(exc), exc, tb)


def _install_sigterm_handler():
    """Turn SIGTERM into an exception so the run is finalized. No-op off the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is None:
        return lambda: None
    try:
        previous = signal.getsignal(sigterm)
    except (ValueError, OSError):
        return lambda: None
    if previous not in (signal.SIG_DFL, None):
        return lambda: None

    def _handler(signum, frame):
        raise _Terminated()

    try:
        signal.signal(sigterm, _handler)
    except (ValueError, OSError):
        return lambda: None

    def _restore():
        try:
            if signal.getsignal(sigterm) is _handler:
                signal.signal(sigterm, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    return _restore


def _finish_run(run, exit_code: int, exception_name: str | None) -> None:
    requested = getattr(run, "_deferred_status", None)
    if exit_code != 0:
        status = "crashed"
    else:
        status = requested or "finished"
    summary = getattr(run, "summary", None)
    if isinstance(summary, dict):
        if exit_code != 0:
            summary["_exit_code"] = exit_code
        if exception_name:
            summary["_exception"] = exception_name
    try:
        uninstall_hyperparam_capture(run)
    finally:
        _globals._finish_launched(run, status)

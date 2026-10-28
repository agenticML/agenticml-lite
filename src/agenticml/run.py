from __future__ import annotations

import atexit
import hashlib
import logging
import math
import mimetypes
import os
import shlex
import shutil
import tempfile
import threading
import time
import weakref
from datetime import date, datetime
from pathlib import Path

from .store import RunReader, RunWriter, RunFinishedError, check_budget
from .store.layout import resolve_root, valid_run_id
from .code_tracking import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    RuntimeFileCollector,
    compute_snapshot_hash,
    default_project,
    find_repo_root,
)
from .env import capture_environment
from .lineage import get_git_metadata, snapshot as snapshot_lineage
from .media import _Media, is_media
from ._source_inspect import stage_source_and_metadata
from .auto_tracking import AutoTracker
from .component_tracking import (
    metadata_manifest_path,
    sanitize_component_name,
    stage_tracked_dataloader,
    stage_tracked_loss,
    stage_tracked_optimizer,
)
from .integrations.tensorboard import install_tensorboard_sync, uninstall_tensorboard_sync
from .model_tracking import stage_tracked_model
from .system_metrics import SystemMetricsSampler


logger = logging.getLogger("agenticml.run")

_VALID_RESUME = {"never", "allow", "must"}
_VALID_RUN_MODES = {"local", "offline"}
_MAX_BUFFER_ITEMS = 1_000_000
_SNAPSHOT_ATTEMPTS = 5
_MAX_FLUSH_BACKOFF = 60.0
_FLUSH_LOG_INTERVAL = 60.0
_UNSERIALIZABLE = "<unserializable>"

_LIVE_RUNS = weakref.WeakSet()
_FORK_HOOK_LOCK = threading.Lock()
_FORK_HOOK_REGISTERED = False
_budget_warned = False


def _warn_store_budget(root) -> None:
    """Warn once per process when a newly created run's store is over budget.

    ``check_budget`` reads cached usage and refreshes stale data in a daemon.
    """
    global _budget_warned
    message = check_budget(root)
    if not message or _budget_warned:
        return
    _budget_warned = True
    logger.warning("%s", message)


def _copy_mapping(mapping) -> dict:
    """Shallow-copy a mapping, retrying if it changes size mid-iteration."""
    last_error: RuntimeError | None = None
    for _ in range(_SNAPSHOT_ATTEMPTS):
        try:
            return dict(mapping)
        except RuntimeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _copy_sequence(values) -> list:
    last_error: RuntimeError | None = None
    for _ in range(_SNAPSHOT_ATTEMPTS):
        try:
            return list(values)
        except RuntimeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _is_zero_dim(value) -> bool:
    """Numpy scalars and 0-d tensors expose ``item()`` and ``ndim == 0``."""
    item = getattr(value, "item", None)
    if not callable(item):
        return False
    ndim = getattr(value, "ndim", None)
    if ndim is None:
        shape = getattr(value, "shape", None)
        if shape is None:
            return False
        try:
            return len(shape) == 0
        except Exception:
            return False
    try:
        return int(ndim) == 0
    except Exception:
        return False


def _json_key(key, seen: set[int] | None = None) -> str:
    if isinstance(key, str):
        return key
    safe = _json_safe(key, seen)
    if isinstance(safe, str):
        return safe
    try:
        return str(safe)
    except Exception:
        return _UNSERIALIZABLE


def _json_safe(value, seen: set[int] | None = None):
    """Copy ``value`` into objects ``json.dumps`` accepts.

    Numpy scalars and 0-d tensors become Python numbers via ``.item()``.
    Other ``__float__`` objects become floats. Non-finite floats become
    ``None``. Datetimes become ISO strings. Anything else becomes ``str``.
    Cycles, and values whose ``__float__``, ``item``, or ``__str__`` raise,
    become ``"<unserializable>"``.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        try:
            return value.isoformat()
        except Exception:
            return _UNSERIALIZABLE

    if seen is None:
        seen = set()
    marker = id(value)
    if marker in seen:
        return _UNSERIALIZABLE
    seen.add(marker)
    try:
        if isinstance(value, dict):
            copied = _copy_mapping(value)
            return {
                _json_key(key, seen): _json_safe(item, seen)
                for key, item in copied.items()
            }
        if isinstance(value, (list, tuple)):
            return [_json_safe(item, seen) for item in _copy_sequence(value)]
        if _is_zero_dim(value):
            try:
                extracted = value.item()
            except Exception:
                return _UNSERIALIZABLE
            if extracted is not value:
                return _json_safe(extracted, seen)
        if getattr(type(value), "__float__", None) is not None:
            try:
                number = float(value)
            except Exception:
                return _UNSERIALIZABLE
            return number if math.isfinite(number) else None
        try:
            return str(value)
        except Exception:
            return _UNSERIALIZABLE
    finally:
        seen.discard(marker)


def _after_fork_in_child() -> None:
    """Disable every live run in the forked child. Registered once per process."""
    for run in list(_LIVE_RUNS):
        try:
            run._after_in_child()
        except Exception:
            pass


def _register_fork_hook() -> None:
    global _FORK_HOOK_REGISTERED
    if _FORK_HOOK_REGISTERED or not hasattr(os, "register_at_fork"):
        return
    with _FORK_HOOK_LOCK:
        if _FORK_HOOK_REGISTERED or not hasattr(os, "register_at_fork"):
            return
        os.register_at_fork(after_in_child=_after_fork_in_child)
        _FORK_HOOK_REGISTERED = True


class _RunLock:
    """Reentrant lock. ``finish`` can run while ``log`` holds it on this thread."""

    def __init__(self) -> None:
        self._rlock = threading.RLock()
        self._local = threading.local()

    def __enter__(self):
        self._rlock.acquire()
        self._local.depth = getattr(self._local, "depth", 0) + 1
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._local.depth = getattr(self._local, "depth", 1) - 1
        finally:
            self._rlock.release()
        return False

    def held_by_current_thread(self) -> bool:
        return getattr(self._local, "depth", 0) > 0


class _StoreError(Exception):
    pass


class _RunType(type):
    def __call__(cls, *args, **kwargs):
        try:
            return super().__call__(*args, **kwargs)
        except _StoreError as exc:
            from ._globals import _DisabledRun

            logger.exception("could not create tracking store; tracking disabled")
            run = _DisabledRun()
            run._disabled_reason = (str(exc.__cause__ or exc).splitlines() or ["store unavailable"])[0]
            if kwargs.get("verbose"):
                print(f"agenticml: tracking disabled: {run._disabled_reason}")
            return run


class Run(metaclass=_RunType):
    def __init__(
        self,
        project,
        name,
        config=None,
        flush_interval=2,
        track_source="runtime",
        source_roots=None,
        extra_files=None,
        # Phase 1 metadata
        tags=None,
        notes=None,
        group=None,
        job_type=None,
        capture_env=True,
        # Phase 2: explicit parent for lineage.
        parent_run_id=None,
        # Phase 2: code-snapshot size caps (passed through).
        max_file_bytes=None,
        max_total_bytes=None,
        # Phase 3
        id=None,
        resume="never",
        mode=None,
        system_metrics=True,
        system_metrics_interval=10.0,
        dir=None,
        verbose=False,
        auto_track=True,
        auto_metrics=False,
        log_every=1,
        sync_tensorboard=False,
        lineage=True,
    ):
        if resume not in _VALID_RESUME:
            raise ValueError(f"resume must be one of {_VALID_RESUME}")
        mode = "local" if mode is None else mode.lower()
        if mode not in _VALID_RUN_MODES:
            raise ValueError(
                f"mode must be one of {_VALID_RUN_MODES}, got {mode!r}. "
                "('disabled' is handled by agenticml.init(), not Run directly.)"
            )
        if mode == "offline":
            mode = "local"
        if id is not None and resume == "never":
            raise ValueError("Run.id passed with resume='never'; pass resume='allow' or 'must'")

        self.mode = mode
        self.verbose = bool(verbose)
        try:
            self.root = resolve_root(dir)
        except Exception as exc:
            raise _StoreError() from exc
        if id is not None and not valid_run_id(id):
            raise ValueError(f"invalid run id: {id!r}")
        if id is not None and parent_run_id == id:
            parent_run_id = None
        if not project:
            project = default_project()
        self.project = project
        self.name = name
        self.flush_interval = flush_interval
        self._flush_wait = float(flush_interval)
        self._flush_failing = False
        self._flush_fail_logged_at = 0.0
        self._logged_finished_drop = False
        if track_source is True:
            track_source = "runtime"
        if track_source not in {"runtime", False}:
            raise ValueError("track_source must be 'runtime', True, or False")
        self.code_tracking_enabled = track_source is not False
        self.code_manifest = None
        self.tracked_files = []
        self._runtime_collector = None
        self._object_staging_dir = None
        self.lineage = {}
        self._lineage_enabled = bool(lineage) and os.environ.get(
            "AGENTICML_LINEAGE", "1"
        ).strip().lower() not in {"0", "false", "no", "off"}
        self._finished = False
        self._finishing = False
        self._finish_done = threading.Event()
        self._finish_thread: int | None = None
        # Depth of this thread's calls into the store writer. finish() from a
        # signal handler must not join or take the writer lock while one of
        # those calls still holds it.
        self._writer_calls = threading.local()
        self._finish_requested = False
        self._finish_requested_thread: int | None = None
        self._finish_requested_status = "finished"
        self._store_finish_attempted = False
        self._persisted_fields: dict = {}
        self._field_errors_logged: set[str] = set()
        self._buffer_overflow_warned = False
        self._tracked: dict[tuple[str, int], str] = {}
        self._auto_finalizing = False
        self._auto_tracker: AutoTracker | None = None
        # Set by `agenticml run`: finish() from the script is deferred until
        # the script returns, so the launcher can finalize with the exit status.
        self._launcher_owned = False
        self._deferred_status: str | None = None

        # None means "not passed" so resume can keep the stored value.
        requested_tags = tags
        requested_notes = notes
        requested_group = group
        requested_job_type = job_type

        self.tags = list(tags) if tags else []
        self.notes = notes
        self.group = group
        self.job_type = job_type

        self.config = dict(config) if config else {}
        # Keys the script set itself; captured CLI hyperparameters never replace them.
        self._explicit_config_keys = set(self.config)
        self.summary: dict = {}

        # Auto-step + commit accumulation. User threads mutate these under
        # `_state_lock`. System samples go through `_log_system` and do not
        # take a step. Reentrant because `_commit_pending` may run while the
        # lock is already held. `_finished` is set under this same lock, and
        # every enqueue checks it before touching the buffer.
        self._state_lock = threading.RLock()
        self._auto_step = 0
        self._pending_metrics: dict = {}
        self._pending_step: int | None = None

        if self.code_tracking_enabled:
            resolved_max_file_bytes = (
                DEFAULT_MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes
            )
            resolved_max_total_bytes = (
                DEFAULT_MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes
            )
            self._runtime_collector = RuntimeFileCollector(
                source_roots=source_roots or [],
                extra_files=extra_files or [],
                max_file_bytes=resolved_max_file_bytes,
                max_total_bytes=resolved_max_total_bytes,
            )
            repo_root = str(self._runtime_collector.repo_root)
            self.lineage = {
                "snapshot_hash": None,
                "repo_root": repo_root,
                "parent_run_id": parent_run_id,
                "parent_inference": "explicit" if parent_run_id else None,
            }

        env_payload = capture_environment() if capture_env else {}

        # Sync maps these nested fields to the server's /init payload.
        repo_root = self.lineage.get("repo_root") or str(find_repo_root(Path.cwd()))
        try:
            git_payload = {**get_git_metadata(repo_root), "repo_root": repo_root}
        except Exception:
            logger.warning("failed to read project Git metadata", exc_info=True)
            git_payload = {"repo_root": repo_root}
        if self.code_tracking_enabled:
            self.lineage.update(git_payload)
        meta = {
            "project": project,
            "name": name,
            "config": self.config,
            "tags": self.tags,
            "notes": self.notes or "",
            "group": self.group,
            "job_type": self.job_type,
            "parent_run_id": parent_run_id,
            "env": env_payload,
            "git": git_payload,
        }
        self.writer = None
        if id is not None:
            self.writer = RunWriter.resume(self.root, id, must=(resume == "must"))
        if self.writer is None:
            try:
                self.writer = RunWriter.create(self.root, run_id=id, meta=meta)
            except Exception as exc:
                raise _StoreError() from exc
            if self._lineage_enabled:
                try:
                    result = snapshot_lineage(
                        self.root, project, repo_root, self.writer.run_id,
                        parent_run_id=parent_run_id,
                        created_at=self.writer._meta["created_at"],
                        max_file_bytes=DEFAULT_MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes,
                        max_total_bytes=DEFAULT_MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes,
                    )
                    self.writer.set_meta_fields(lineage=result)
                    self.lineage.update(result)
                except Exception:
                    logger.exception("failed to capture run lineage")
            _warn_store_budget(self.root)
        else:
            previous = RunReader(self.root, id).meta()
            self.project = previous.get("project", project)
            self.name = previous.get("name", name)
            self.config = {**previous.get("config", {}), **self.config}
            self.summary = dict(previous.get("summary", {}))
            if requested_tags is not None:
                self.tags = list(requested_tags)
            else:
                stored_tags = previous.get("tags")
                self.tags = list(stored_tags) if stored_tags else []
            self.notes = requested_notes if requested_notes is not None else previous.get("notes")
            self.group = requested_group if requested_group is not None else previous.get("group")
            self.job_type = (
                requested_job_type if requested_job_type is not None else previous.get("job_type")
            )
            last_step = self.writer.last_step
            self._auto_step = 0 if last_step is None else last_step + 1
        self.run_id = self.writer.run_id

        if self.verbose:
            self._print_init_info()

        # Fork-safety before any background thread exists. After fork the
        # child cannot reuse the parent's threads; public methods no-op there.
        # One process-wide hook walks a WeakSet so finished runs can be collected.
        self._owner_pid = os.getpid()
        self._seed_persisted_fields()
        _LIVE_RUNS.add(self)
        _register_fork_hook()

        self.buffer = []
        # RLock: finish() may run on the main thread from a signal handler
        # while log() holds this lock. Joining the flush thread in that case
        # deadlocks, so finish() skips the join when this thread already holds it.
        self.lock = _RunLock()
        self.stop_event = threading.Event()

        self.thread = threading.Thread(
            target=self._flush_loop, name=f"agenticml-flush-{self.run_id}", daemon=True
        )
        self.thread.start()

        # System metrics sampler (Phase 3).
        self._system_sampler = None
        if system_metrics:
            self._system_sampler = SystemMetricsSampler(self, interval=system_metrics_interval)
            self._system_sampler.start()

        if auto_track:
            try:
                self._auto_tracker = AutoTracker(
                    self, metrics_every=log_every if auto_metrics else None
                )
                self._auto_tracker.install()
            except Exception:
                logger.exception("failed to install automatic component tracking")
                tracker = self._auto_tracker
                self._auto_tracker = None
                if tracker is not None:
                    tracker.detach()

        # atexit safety net.
        self_ref = weakref.ref(self)

        def _atexit_finish():
            run = self_ref()
            if run is not None and not run._finished:
                try:
                    run.finish(status="crashed")
                except Exception:
                    logger.exception("atexit finish() failed")

        atexit.register(_atexit_finish)
        self._atexit_callback = _atexit_finish

        self._tensorboard_sync = False
        if sync_tensorboard:
            try:
                install_tensorboard_sync(self)
                self._tensorboard_sync = True
            except Exception:
                logger.exception("failed to install TensorBoard sync")

    # ----- public API -----

    def log(self, metrics: dict, step: int | None = None, commit: bool = True):
        if self._in_fork_child():
            return
        if not isinstance(metrics, dict):
            raise TypeError("metrics must be a dict")

        # Split media values from numeric metrics.
        numeric: dict = {}
        media_items: list[tuple[str, _Media]] = []
        for key, value in metrics.items():
            if is_media(value):
                media_items.append((key, value))
            else:
                numeric[key] = value

        # Step state and the enqueue share `_state_lock` with finish(), so a
        # point cannot enter the buffer after finish() has sealed the run.
        payload_metrics: dict | None = None
        with self._state_lock:
            if self._finished:
                logger.warning("log() called after finish(); ignoring")
                return
            if step is None:
                target_step = (
                    self._pending_step if self._pending_step is not None else self._auto_step
                )
            else:
                if self._pending_step is not None and step != self._pending_step:
                    self._commit_pending()
                target_step = step

            if numeric or commit:
                if not commit:
                    self._pending_step = target_step
                    self._pending_metrics.update(numeric)
                else:
                    if self._pending_step is not None and self._pending_step == target_step:
                        payload_metrics = {**self._pending_metrics, **numeric}
                        self._pending_metrics = {}
                        self._pending_step = None
                    elif self._pending_step is not None:
                        self._commit_pending()
                        payload_metrics = dict(numeric)
                    else:
                        payload_metrics = dict(numeric)

                    if step is None:
                        self._auto_step = target_step + 1
                    else:
                        self._auto_step = max(self._auto_step, step + 1)

            if payload_metrics:
                self._enqueue(target_step, payload_metrics)

        # Media writes happen outside the lock — they access disk and
        # must not block other log() callers.
        for key, media in media_items:
            try:
                self._upload_media(key, target_step, media)
            except RunFinishedError:
                logger.debug(
                    "skipping media upload after finish key=%s step=%d",
                    key,
                    target_step,
                )
            except Exception:
                logger.exception("failed to upload media key=%s step=%d", key, target_step)

    def log_artifact(self, file_path, name=None, type="file", metadata=None, copy=False):
        """Record a versioned artifact reference, or snapshot it with copy=True."""
        if self._in_fork_child():
            return None
        if self._finished:
            raise RuntimeError("log_artifact() called after finish()")
        path = Path(file_path)
        return self._writer_call(
            self.writer.add_artifact, path, name or path.stem, type, metadata, copy=copy
        )

    def _writer_depth(self) -> int:
        return getattr(self._writer_calls, "depth", 0)

    def _writer_call(self, method, *args, **kwargs):
        """Call the store writer on this thread.

        If ``finish()`` runs re-entrantly (a signal handler interrupting the
        call), it only sets ``_finish_requested``. After the writer releases
        its lock and this thread's outermost call returns, that finish runs
        for real. Other threads leave the request pending.
        """
        local = self._writer_calls
        local.depth = getattr(local, "depth", 0) + 1
        try:
            return method(*args, **kwargs)
        finally:
            local.depth -= 1
            if (local.depth == 0 and self._finish_requested
                    and self._finish_requested_thread == threading.get_ident()):
                status = self._finish_requested_status
                self._finish_requested = False
                self._finish_requested_thread = None
                if not self._finishing and not self._finished:
                    self.finish(status)

    def finish(self, status: str = "finished"):
        """Stop background work and persist the final run status.

        Calling finish() from a signal handler is supported only on the
        main thread and may drop the point being logged at that instant.
        """
        if self._in_fork_child():
            return
        if self._writer_depth() > 0:
            # The signal interrupted add_media / add_artifact / add_code / write
            # on this thread. Joining or calling writer.finish() would deadlock
            # on the writer lock that call still holds.
            self._finish_requested_status = status
            self._finish_requested_thread = threading.get_ident()
            self._finish_requested = True
            return
        with self._state_lock:
            if self._launcher_owned:
                if not self._finished:
                    self._deferred_status = status
                    if self._pending_metrics:
                        self._commit_pending()
                return
            if self._finished or self._finishing:
                # A nested call on the owner thread (signal during finish)
                # cannot wait: the owner is the one that sets the event.
                if (
                    self._finish_thread == threading.get_ident()
                    and not self._finish_done.is_set()
                ):
                    return
                done = self._finish_done
                owner = False
            else:
                self._finishing = True
                self._finish_thread = threading.get_ident()
                done = self._finish_done
                owner = True
        if not owner:
            done.wait()
            return

        try:
            # Integrations and the sampler still need log() / _log_system here.
            self._stop_integrations()
            sampler = self._system_sampler
            if sampler is not None:
                sampler.stop()

            with self._state_lock:
                if self._pending_metrics:
                    self._commit_pending()
                self._finished = True

            self._finish_body(status)
        finally:
            self._seal_finish()

    def _stop_integrations(self):
        """Log what auto metrics hold and stop mirroring, while log() still works."""
        tracker = self._auto_tracker
        if tracker is not None:
            try:
                tracker.flush_metrics()
            except Exception:
                logger.exception("failed to flush automatic metrics")
        if self._tensorboard_sync:
            self._tensorboard_sync = False
            try:
                uninstall_tensorboard_sync(self)
            except Exception:
                logger.exception("failed to remove TensorBoard sync")

    def _seal_finish(self) -> None:
        """``_finished`` is True, and the store is finalized, before waiters wake."""
        self._finished = True
        try:
            try:
                with self._state_lock:
                    self._finished = True
                    self._finishing = False
                self.stop_event.set()
            except Exception:
                logger.exception("failed to seal run state")
            if not self._store_finish_attempted:
                self._mark_crashed()
        finally:
            try:
                self._cleanup_object_staging()
            except Exception:
                logger.exception("failed to clean up staged objects")
            self._unregister_atexit()
            self._finish_done.set()

    def _unregister_atexit(self) -> None:
        callback = getattr(self, "_atexit_callback", None)
        if callback is None:
            return
        try:
            atexit.unregister(callback)
        except Exception:
            pass

    def _finish_body(self, status: str):
        self.stop_event.set()
        # Joining while this thread holds ``self.lock`` deadlocks: the flush
        # thread needs that lock to exit. A signal handler hits this when it
        # calls finish() from inside log(). The store lock serializes a late
        # write with writer.finish(); RunFinishedError drops that batch.
        if not self.lock.held_by_current_thread():
            self.thread.join()
        self._drain_buffer()
        try:
            self._persist_metadata()
        except Exception:
            logger.exception("failed to flush run metadata")

        tracker = self._auto_tracker
        if tracker is not None:
            self._auto_finalizing = True
            try:
                tracker.finalize()
            except Exception:
                logger.exception("automatic component tracking finalize failed")
            finally:
                self._auto_finalizing = False

        snapshot_hash = None
        if self._runtime_collector is not None:
            try:
                self.code_manifest, self.tracked_files = self._runtime_collector.close()
                snapshot_hash = compute_snapshot_hash(self.code_manifest)
                self._upload_tracked_files(self.code_manifest, self.tracked_files)
            except Exception:
                logger.exception("failed to finalize runtime source snapshot")
                snapshot_hash = None
            else:
                if snapshot_hash:
                    self.lineage["snapshot_hash"] = snapshot_hash

        if snapshot_hash:
            try:
                self.writer.set_meta_fields(snapshot_hash=snapshot_hash)
            except Exception:
                logger.exception("failed to store snapshot hash")

        summary = self._converted_field("summary", self._convert_summary, {})
        self._store_finish_attempted = True
        try:
            self.writer.finish(status, summary)
        except Exception:
            logger.exception("failed to finalize run")
            self._mark_crashed()
        else:
            self._persisted_fields["summary"] = summary
        self._ensure_not_running()

    def _mark_crashed(self) -> None:
        try:
            self.writer.set_meta_fields(status="crashed")
        except Exception:
            logger.exception("failed to mark run as crashed")

    def _ensure_not_running(self) -> None:
        try:
            status = RunReader(self.root, self.run_id).meta().get("status")
        except Exception:
            logger.exception("failed to read run status after finish")
            self._mark_crashed()
            return
        if status == "running" or not status:
            self._mark_crashed()

    def track_files(self, *paths):
        if self._in_fork_child():
            return
        if not paths:
            return
        if self._runtime_collector is not None:
            for path in paths:
                self._runtime_collector.add_explicit(path)
            return
        self._upload_explicit_files([(path, None) for path in paths], source="extra")

    def _accepting_tracks(self) -> bool:
        return not self._finished or self._auto_finalizing

    def track_model(self, model, name="default", extra_metadata=None):
        """Snapshot the architecture source of an instantiated module.

        Copies defining ``.py`` files into a run-owned staging directory and
        records them under ``model/...`` plus ``agenticml/model.json`` (or
        ``model.{name}.json``). Does not raise if inspect cannot find source.
        """
        if self._in_fork_child():
            return
        if not self._accepting_tracks():
            logger.warning("track_model() called after finish(); ignoring")
            return
        if model is not None:
            self._tracked[("model", id(model))] = name
        try:
            staging = self._ensure_object_staging()
            staged = stage_tracked_model(
                model, staging, name=name, extra_metadata=extra_metadata
            )
            self._attach_staged_files(staged, source="model")
        except Exception:
            logger.exception("failed to track model")

    def track_optimizer(self, optimizer, name="default", extra_metadata=None):
        """Snapshot optimizer class identity and hyperparameters.

        Records ``agenticml/optimizer.json`` (or ``optimizer.{name}.json``) and
        copies custom defining ``.py`` files under ``optimizer/``. Does not copy
        parameter tensors or ``torch.optim`` stdlib source. Never raises.
        """
        if self._in_fork_child():
            return
        if not self._accepting_tracks():
            logger.warning("track_optimizer() called after finish(); ignoring")
            return
        if optimizer is not None:
            self._tracked[("optimizer", id(optimizer))] = name
        try:
            staging = self._ensure_object_staging()
            staged = stage_tracked_optimizer(
                optimizer, staging, name=name, extra_metadata=extra_metadata
            )
            self._attach_staged_files(staged, source="optimizer")
        except Exception:
            logger.exception("failed to track optimizer")

    def track_loss(self, loss, name="default", extra_metadata=None):
        """Snapshot loss class or function identity and constructor attributes.

        Records ``agenticml/loss.json`` (or ``loss.{name}.json``) and copies
        custom defining ``.py`` files under ``loss/``. Does not copy weight
        tensors or ``torch.nn`` stdlib source. ``loss=None`` writes metadata
        only. Never raises.
        """
        if self._in_fork_child():
            return
        if not self._accepting_tracks():
            logger.warning("track_loss() called after finish(); ignoring")
            return
        if loss is not None:
            self._tracked[("loss", id(loss))] = name
            try:
                staging = self._ensure_object_staging()
                staged = stage_tracked_loss(
                    loss, staging, name=name, extra_metadata=extra_metadata
                )
                self._attach_staged_files(staged, source="loss")
            except Exception:
                logger.exception("failed to track loss")
            return
        try:
            safe_name = sanitize_component_name(name)
            metadata: dict = {"name": safe_name, "files": []}
            if isinstance(extra_metadata, dict):
                for key, value in extra_metadata.items():
                    if key not in metadata:
                        metadata[key] = value
            staging = self._ensure_object_staging()
            staged = stage_source_and_metadata(
                [],
                metadata,
                staging,
                metadata_manifest_path("loss", safe_name),
                log_label="track_loss",
                log=logger,
            )
            self._attach_staged_files(staged, source="loss")
        except Exception:
            logger.exception("failed to track loss")

    def track_dataloader(self, dataloader, name="default", extra_metadata=None):
        """Snapshot dataloader kwargs and dataset class identity.

        Records ``agenticml/dataloader.json`` (or ``dataloader.{name}.json``)
        and copies custom dataset / collate source under ``dataloader/``. Does
        not copy dataset contents. Never raises.
        """
        if self._in_fork_child():
            return
        if not self._accepting_tracks():
            logger.warning("track_dataloader() called after finish(); ignoring")
            return
        if dataloader is not None:
            self._tracked[("dataloader", id(dataloader))] = name
        try:
            staging = self._ensure_object_staging()
            staged = stage_tracked_dataloader(
                dataloader, staging, name=name, extra_metadata=extra_metadata
            )
            self._attach_staged_files(staged, source="dataloader")
        except Exception:
            logger.exception("failed to track dataloader")

    def _attach_staged_files(self, staged, source: str) -> None:
        if not staged:
            return
        if self._runtime_collector is not None:
            for path, manifest_path in staged:
                self._runtime_collector.add_explicit(
                    path,
                    manifest_path=manifest_path,
                    source=source,
                )
        else:
            self._upload_explicit_files(staged, source=source)

    def _ensure_object_staging(self) -> Path:
        if self._object_staging_dir is None:
            self._object_staging_dir = Path(
                tempfile.mkdtemp(prefix="agenticml-object-")
            ).resolve()
        return self._object_staging_dir

    def _cleanup_object_staging(self) -> None:
        staging = self._object_staging_dir
        if staging is None:
            return
        self._object_staging_dir = None
        shutil.rmtree(staging, ignore_errors=True)

    # ----- context manager -----

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        status = "finished" if exc_type is None else "crashed"
        self.finish(status=status)
        return False

    # ----- internals -----

    def _upload_explicit_files(self, staged, source):
        try:
            repo_root = find_repo_root(Path.cwd())
            manifest_files = []
            payload_files = []
            for path, manifest_path in staged:
                path = Path(path).expanduser().resolve()
                if not path.is_file():
                    continue
                if manifest_path is None:
                    try:
                        manifest_path = path.relative_to(repo_root).as_posix()
                    except ValueError:
                        manifest_path = f"extra/{path.name}"
                content = path.read_bytes()
                spec = {
                    "path": manifest_path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    "source": source,
                }
                manifest_files.append({**spec, "size": len(content)})
                payload_files.append({**spec, "content": content})
            if payload_files:
                self._writer_call(
                    self.writer.add_code,
                    manifest_files,
                    [(item["path"], item["content"]) for item in payload_files],
                )
        except Exception:
            logger.exception("failed to track explicit files")

    def _print_init_info(self):
        """Print the run identity and how to open its local UI."""
        project = "-" if self.project is None or self.project == "" else self.project
        name = "-" if self.name is None or self.name == "" else self.name
        print("\n".join([
            f"agenticml: run_id={self.run_id}",
            f"  project={project} name={name} mode={self.mode}",
            f"  run_dir={self.writer.run_dir}",
            f"  agenticml ui --dir {shlex.quote(str(self.root))}",
        ]))

    def _in_fork_child(self) -> bool:
        return os.getpid() != self._owner_pid

    def _after_in_child(self):
        """Disable this run in a forked child. The process-wide hook calls this."""
        self._finished = True
        if self._runtime_collector is not None:
            self._runtime_collector._active = False
        tracker = getattr(self, "_auto_tracker", None)
        if tracker is not None:
            tracker.detach()
        # Don't touch threading primitives — they're inherited but unsafe.

    def _commit_pending(self):
        if not self._pending_metrics or self._pending_step is None:
            self._pending_metrics = {}
            self._pending_step = None
            return
        step = self._pending_step
        metrics = self._pending_metrics
        self._pending_metrics = {}
        self._pending_step = None
        if self._enqueue(step, metrics):
            self._auto_step = max(self._auto_step, step + 1)

    def _enqueue(self, step: int, metrics: dict) -> bool:
        """Append one batch item unless finish() has already sealed the run."""
        item = {
            "run_id": self.run_id,
            "step": step,
            "metrics": metrics,
        }
        with self._state_lock:
            if self._finished:
                return False
            with self.lock:
                self.buffer.append(item)
        return True

    def _log_system(self, samples: dict):
        """Enqueue `_system/*` samples at the current step without committing it.

        The step is the pending training step when one exists, otherwise the
        last committed step (`max(_auto_step - 1, 0)`). `_pending_step`,
        `_pending_metrics`, and `_auto_step` are left unchanged.
        """
        if self._in_fork_child() or not samples:
            return
        with self._state_lock:
            if self._finished:
                return
            if self._pending_step is not None:
                step = self._pending_step
            else:
                step = max(self._auto_step - 1, 0)
            self._enqueue(step, dict(samples))

    def _flush_loop(self):
        try:
            while not self.stop_event.wait(self._flush_wait):
                try:
                    self.writer.heartbeat()
                except Exception:
                    logger.exception("failed to refresh run heartbeat")
                self._flush()
            self._flush()
        except Exception:
            logger.exception("flush thread crashed")

    def _flush(self) -> bool:
        """Write the buffered batch, then config, summary, and run meta.

        A scalar-write failure puts the batch back and stretches the flush
        interval, up to 60 seconds. ``RunFinishedError`` will not succeed on
        retry, so that batch is dropped. Returns False when the batch was
        not written.
        """
        with self.lock:
            batch = list(self.buffer)
            self.buffer.clear()

        wrote = True
        if batch:
            try:
                self._writer_call(
                    self.writer.write_scalars,
                    [(item["step"], item["metrics"]) for item in batch],
                )
            except RunFinishedError as exc:
                if not self._logged_finished_drop:
                    self._logged_finished_drop = True
                    logger.error(
                        "failed to flush metric batch (%d items); "
                        "dropping because the run is finished (%s)",
                        len(batch),
                        exc,
                    )
                wrote = False
            except Exception as exc:
                self._requeue_batch(batch)
                with self.lock:
                    pending = len(self.buffer)
                self._log_flush_failure(pending, exc)
                self._note_flush_failure()
                wrote = False
            else:
                self._note_flush_success()
        try:
            self._persist_metadata()
        except Exception:
            logger.exception("failed to flush run metadata")
        return wrote

    def _note_flush_success(self) -> None:
        self._flush_wait = float(self.flush_interval)
        self._flush_failing = False

    def _note_flush_failure(self) -> None:
        cap = max(_MAX_FLUSH_BACKOFF, float(self.flush_interval))
        delay = float(self._flush_wait) * 2
        if delay <= 0:
            delay = min(1.0, cap)
        self._flush_wait = min(delay, cap)

    def _log_flush_failure(self, pending: int, exc: BaseException) -> None:
        """Full traceback on the first failure, then one line per minute."""
        now = time.monotonic()
        if not self._flush_failing:
            self._flush_failing = True
            self._flush_fail_logged_at = now
            logger.exception(
                "failed to flush metric batch (%d items); requeueing", pending
            )
            return
        if now - self._flush_fail_logged_at >= _FLUSH_LOG_INTERVAL:
            self._flush_fail_logged_at = now
            logger.error(
                "failed to flush metric batch; %d items pending (%s)",
                pending,
                exc,
            )

    def _requeue_batch(self, batch: list) -> None:
        if not batch:
            return
        dropped = 0
        warn = False
        with self.lock:
            self.buffer[:0] = batch
            overflow = len(self.buffer) - _MAX_BUFFER_ITEMS
            if overflow > 0:
                del self.buffer[:overflow]
                dropped = overflow
                warn = not self._buffer_overflow_warned
                self._buffer_overflow_warned = True
        if warn and dropped:
            logger.warning(
                "metric buffer exceeded %d items; dropped %d oldest",
                _MAX_BUFFER_ITEMS,
                dropped,
            )

    def _drain_buffer(self) -> None:
        """Flush until the buffer is empty, or one flush keeps failing."""
        while True:
            with self.lock:
                pending = len(self.buffer)
                if pending == 0:
                    return
            if not self._flush():
                self._drop_unflushed()
                return
            with self.lock:
                if not self.buffer:
                    return
                if len(self.buffer) >= pending:
                    self._drop_unflushed()
                    return

    def _drop_unflushed(self) -> None:
        with self.lock:
            lost = len(self.buffer)
            self.buffer.clear()
        if lost:
            logger.error("failed to flush metrics; %d points lost", lost)

    def _seed_persisted_fields(self) -> None:
        seeds = (
            ("config", self._convert_config, {}),
            ("summary", self._convert_summary, {}),
            ("tags", self._convert_tags, []),
            ("notes", self._convert_notes, ""),
            ("group", self._convert_group, None),
            ("job_type", self._convert_job_type, None),
        )
        for name, convert, default in seeds:
            try:
                self._persisted_fields[name] = convert()
            except Exception:
                self._persisted_fields[name] = default

    def _convert_config(self):
        return _json_safe(_copy_mapping(self.config))

    def _convert_summary(self):
        return _json_safe(_copy_mapping(self.summary))

    def _convert_tags(self):
        return _json_safe(_copy_sequence(self.tags))

    def _convert_notes(self):
        notes = self.notes or ""
        if isinstance(notes, str):
            return notes
        safe_notes = _json_safe(notes)
        if isinstance(safe_notes, str):
            return safe_notes
        return str(safe_notes)

    def _convert_group(self):
        if self.group is None:
            return None
        return _json_safe(self.group)

    def _convert_job_type(self):
        if self.job_type is None:
            return None
        return _json_safe(self.job_type)

    def _converted_field(self, name: str, convert, default):
        """Convert one metadata field. On failure, keep the last value written."""
        try:
            value = convert()
        except Exception:
            token = f"convert:{name}"
            if token not in self._field_errors_logged:
                self._field_errors_logged.add(token)
                logger.exception(
                    "failed to convert %s; keeping the last persisted value",
                    name,
                )
            return self._persisted_fields.get(name, default)
        self._field_errors_logged.discard(f"convert:{name}")
        return value

    def _write_field(self, name: str, write) -> bool:
        try:
            write()
        except Exception:
            token = f"write:{name}"
            if token not in self._field_errors_logged:
                self._field_errors_logged.add(token)
                logger.exception("failed to persist %s", name)
            return False
        self._field_errors_logged.discard(f"write:{name}")
        return True

    def _persist_metadata(self) -> None:
        config = self._converted_field("config", self._convert_config, {})
        summary = self._converted_field("summary", self._convert_summary, {})
        tags = self._converted_field("tags", self._convert_tags, [])
        notes = self._converted_field("notes", self._convert_notes, "")
        group = self._converted_field("group", self._convert_group, None)
        job_type = self._converted_field("job_type", self._convert_job_type, None)
        if self._write_field("config", lambda: self.writer.set_config(config)):
            self._persisted_fields["config"] = config
        if self._write_field("summary", lambda: self.writer.set_summary(summary)):
            self._persisted_fields["summary"] = summary
        if self._write_field(
            "meta",
            lambda: self.writer.set_meta_fields(
                tags=tags, notes=notes, group=group, job_type=job_type
            ),
        ):
            self._persisted_fields["tags"] = tags
            self._persisted_fields["notes"] = notes
            self._persisted_fields["group"] = group
            self._persisted_fields["job_type"] = job_type

    def _upload_media(self, key, step, media: _Media):
        file_path, content_type, _suggested = media._to_upload()
        try:
            self._writer_call(
                self.writer.add_media,
                key, int(step), Path(file_path).read_bytes(), content_type, media.caption(),
            )
        finally:
            if media._upload_is_temporary():
                Path(file_path).unlink(missing_ok=True)

    def _upload_tracked_files(self, manifest, tracked_files):
        self._writer_call(
            self.writer.add_code,
            manifest.get("files", []),
            [(item.relative_path, item.absolute_path) for item in tracked_files],
        )

from __future__ import annotations

import atexit
import hashlib
import logging
import mimetypes
import os
import shutil
import tempfile
import threading
import weakref
from pathlib import Path

from .client import make_client
from .code_tracking import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    RuntimeFileCollector,
    compute_snapshot_hash,
    find_repo_root,
    manifest_to_json,
)
from .env import capture_environment
from .lineage import get_git_metadata
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


class Run:
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
        self.client = make_client(mode, run_id_hint=id, dir=dir)
        self.project = project
        self.name = name
        self.flush_interval = flush_interval
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
        self._finished = False
        self._tracked: dict[tuple[str, int], str] = {}
        self._auto_finalizing = False
        self._auto_tracker: AutoTracker | None = None
        # Set by `agenticml run`: finish() from the script is deferred until
        # the script returns, so the launcher can finalize with the exit status.
        self._launcher_owned = False
        self._deferred_status: str | None = None

        self.tags = list(tags) if tags else []
        self.notes = notes
        self.group = group
        self.job_type = job_type

        self.config = dict(config) if config else {}
        # Keys the script set itself; captured CLI hyperparameters never replace them.
        self._explicit_config_keys = set(self.config)
        self.summary: dict = {}

        # Auto-step + commit accumulation. Mutated from both user threads and
        # the SystemMetricsSampler daemon, so all reads/writes go through
        # `_state_lock`. Reentrant because `_commit_pending` may be called
        # while the lock is already held.
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
                **get_git_metadata(repo_root),
            }

        env_payload = capture_environment() if capture_env else {}

        init_payload = {
            "project": project,
            "name": name,
            "config": self.config,
            "tags": self.tags,
            "notes": self.notes,
            "group": self.group,
            "job_type": self.job_type,
            "parent_run_id": parent_run_id,
            **env_payload,
            **self.lineage,
        }
        self.run_id = self.client.init_run(init_payload)

        if self.verbose:
            self._print_init_info()

        self.buffer = []
        self.lock = threading.Lock()
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

        # Phase 4: fork-safety. After fork, the child cannot reuse the parent's
        # background thread. We disable logging in the child
        # (the user can re-init explicitly if they want per-process tracking).
        self._owner_pid = os.getpid()
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._after_in_child)

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
        if self._finished:
            logger.warning("log() called after finish(); ignoring")
            return
        if os.getpid() != self._owner_pid:
            return  # forked child — silently no-op

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

        # All accesses to _pending_step / _pending_metrics / _auto_step happen
        # under _state_lock — the system-metrics daemon also calls log().
        payload_metrics: dict | None = None
        with self._state_lock:
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

        # Media writes happen outside the lock — they access disk and
        # must not block other log() callers.
        for key, media in media_items:
            try:
                self._upload_media(key, target_step, media)
            except Exception:
                logger.exception("failed to upload media key=%s step=%d", key, target_step)

        if payload_metrics:
            self._enqueue(target_step, payload_metrics)

    def log_artifact(self, file_path, name=None, type="file", metadata=None):
        """Store a file as a versioned artifact attached to this run.

        Returns the journal client's response.
        """
        if self._finished:
            raise RuntimeError("log_artifact() called after finish()")
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"artifact file not found: {file_path}")

        artifact_name = name or path.stem
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return self.client.log_artifact(
            self.run_id,
            artifact_name,
            type,
            metadata or {},
            str(path),
            content_type,
        )

    def finish(self, status: str = "finished"):
        if self._finished:
            return
        if self._launcher_owned:
            self._deferred_status = status
            with self._state_lock:
                if self._pending_metrics:
                    self._commit_pending()
            return
        self._stop_integrations()
        self._finished = True

        try:
            self._finish_body(status)
        finally:
            self._cleanup_object_staging()

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

    def _finish_body(self, status: str):
        # Drop the atexit hook — it's no longer needed and would otherwise
        # leak per-Run for the lifetime of the process.
        atexit_cb = getattr(self, "_atexit_callback", None)
        if atexit_cb is not None:
            try:
                atexit.unregister(atexit_cb)
            except Exception:
                pass

        if self._system_sampler is not None:
            self._system_sampler.stop()

        with self._state_lock:
            if self._pending_metrics:
                self._commit_pending()

        self.stop_event.set()
        self._flush()
        self.thread.join(timeout=self.flush_interval + 1)

        tracker = self._auto_tracker
        if tracker is not None:
            self._auto_finalizing = True
            try:
                tracker.finalize()
            except Exception:
                logger.exception("automatic component tracking finalize failed")
            finally:
                self._auto_finalizing = False

        snapshot_hash = self.lineage.get("snapshot_hash") if self.lineage else None
        if self._runtime_collector is not None:
            try:
                self.code_manifest, self.tracked_files = self._runtime_collector.close()
                snapshot_hash = compute_snapshot_hash(self.code_manifest)
                self._upload_tracked_files(self.code_manifest, self.tracked_files)
                self.lineage["snapshot_hash"] = snapshot_hash
            except Exception:
                snapshot_hash = None
                logger.exception("failed to finalize runtime source snapshot")

        try:
            self.client.finalize(
                self.run_id,
                self.summary,
                status=status,
                snapshot_hash=snapshot_hash,
            )
        except Exception:
            logger.exception("failed to finalize run")

    def track_files(self, *paths):
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
                self.client.upload_code(
                    self.run_id,
                    manifest_to_json({"files": manifest_files}),
                    payload_files,
                )
        except Exception:
            logger.exception("failed to track explicit files")

    def _print_init_info(self):
        """Print run id and local asset paths to stdout (W&B-style)."""
        lines = [
            f"agenticml: run_id={self.run_id}",
            f"  project={self.project} name={self.name} mode={self.mode}",
        ]
        root = getattr(self.client, "root", None)
        if root is not None:
            run_dir = Path(root) / "runs" / self.run_id
            lines.extend([
                f"  dir={root}",
                f"  run_dir={run_dir}",
                f"  journal={run_dir / 'journal.jsonl'}",
                f"  code={run_dir / 'code'}",
                f"  artifacts={run_dir / 'artifacts'}",
                f"  media={run_dir / 'media'}",
            ])
        print("\n".join(lines))

    def _after_in_child(self):
        """Called by the OS in the child after fork(). Disable this run."""
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
        self._enqueue(step, metrics)
        self._auto_step = max(self._auto_step, step + 1)

    def _enqueue(self, step: int, metrics: dict):
        item = {
            "run_id": self.run_id,
            "step": step,
            "metrics": metrics,
        }
        with self.lock:
            self.buffer.append(item)

    def _flush_loop(self):
        while not self.stop_event.wait(self.flush_interval):
            self._flush()

    def _flush(self):
        with self.lock:
            if not self.buffer:
                return
            batch = self.buffer[:]
            self.buffer.clear()

        try:
            self.client.log_batch(batch)
        except Exception:
            logger.exception("failed to flush metric batch (%d items); dropping batch", len(batch))

    def _upload_media(self, key, step, media: _Media):
        file_path, content_type, _suggested = media._to_upload()
        self.client.log_media(
            self.run_id,
            key,
            int(step),
            media.kind,
            media.caption() or "",
            file_path,
            content_type,
        )

    def _upload_tracked_files(self, manifest, tracked_files):
        sha_by_path = {
            item["path"]: item["sha256"] for item in manifest.get("files", [])
        }

        payload_files = []
        for item in tracked_files:
            sha = sha_by_path[item.relative_path]
            with item.absolute_path.open("rb") as handle:
                content = handle.read()
            payload_files.append({
                "path": item.relative_path,
                "content_type": item.content_type,
                "sha256": sha,
                "source": "code",
                "content": content,
            })

        self.client.upload_code(
            self.run_id,
            manifest_to_json(manifest),
            payload_files,
        )

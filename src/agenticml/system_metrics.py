"""Background system metrics sampler.

Samples CPU%, memory%, disk usage at a fixed cadence and writes them to the
run as `_system/...` metrics. GPU is best-effort via pynvml when present.

psutil is optional: if unavailable, the sampler logs a warning and exits.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("agenticml.system")


class _NvmlInitFailed(Exception):
    """``nvmlInit`` failed. The sampler disables GPU reads after one of these."""


def _gpu_samples():
    try:
        import pynvml  # type: ignore
    except ImportError:
        return {}
    initialized = False
    try:
        try:
            pynvml.nvmlInit()
        except Exception as exc:
            raise _NvmlInitFailed("nvmlInit failed") from exc
        initialized = True
        out = {}
        count = pynvml.nvmlDeviceGetCount()
        for idx in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            out[f"_system/gpu.{idx}.utilization"] = float(util.gpu)
            out[f"_system/gpu.{idx}.memory_percent"] = (
                100.0 * mem.used / mem.total if mem.total else 0.0
            )
        return out
    except _NvmlInitFailed:
        raise
    except Exception:  # pragma: no cover - depends on env
        logger.exception("pynvml sampling failed")
        return {}
    finally:
        if initialized:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                logger.debug("nvmlShutdown failed")


def _cpu_mem_samples():
    try:
        import psutil  # type: ignore
    except ImportError:
        return {}
    try:
        return {
            "_system/cpu.percent": float(psutil.cpu_percent(interval=None)),
            "_system/memory.percent": float(psutil.virtual_memory().percent),
            "_system/disk.percent": float(psutil.disk_usage("/").percent),
        }
    except Exception:
        logger.exception("psutil sampling failed")
        return {}


_STOP_JOIN_TIMEOUT = 5.0


class SystemMetricsSampler:
    """Owns a daemon thread that records `_system/*` metrics on the run.

    Samples do not commit a training step. ``stop`` sets a flag under the
    sampler lock and joins the thread for 5 seconds. On timeout it logs one
    warning and returns. A sample that lands after that still cannot commit:
    ``Run._log_system`` re-checks ``_finished`` under ``_state_lock``.
    """

    def __init__(self, run, interval: float = 10.0):
        self.run = run
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopped = False
        self._gpu_disabled = False

    def start(self):
        try:
            import psutil  # noqa: F401
        except ImportError:
            logger.info("psutil not installed; skipping system metrics")
            return

        self.thread = threading.Thread(
            target=self._run, name="agenticml-system", daemon=True
        )
        self.thread.start()

    def stop(self):
        with self._lock:
            self._stopped = True
        self.stop_event.set()
        thread = self.thread
        if thread is None:
            return
        thread.join(timeout=_STOP_JOIN_TIMEOUT)
        if thread.is_alive():
            logger.warning(
                "system metrics sampler did not stop within 5s; continuing"
            )

    def _run(self):
        # Prime psutil so the first sample isn't 0.0.
        try:
            import psutil  # type: ignore

            psutil.cpu_percent(interval=None)
        except Exception:
            pass

        while not self.stop_event.wait(self.interval):
            self._emit_samples()

    def _emit_samples(self):
        with self._lock:
            if self._stopped:
                return
        samples: dict = {}
        samples.update(_cpu_mem_samples())
        if not self._gpu_disabled:
            try:
                samples.update(_gpu_samples())
            except _NvmlInitFailed:
                self._gpu_disabled = True
                logger.debug("nvmlInit failed; disabling GPU sampling for this run")
        if not samples:
            return
        with self._lock:
            if self._stopped:
                return
        try:
            self.run._log_system(samples)
        except Exception:
            logger.exception("failed to log system metrics")

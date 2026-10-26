"""Background system metrics sampler.

Samples CPU%, memory%, disk usage at a fixed cadence and writes them to the
run as `_system/...` metrics. GPU is best-effort via pynvml when present.

psutil is optional: if unavailable, the sampler logs a warning and exits.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("agenticml.system")


def _gpu_samples():
    try:
        import pynvml  # type: ignore
    except ImportError:
        return {}
    try:
        pynvml.nvmlInit()
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
        pynvml.nvmlShutdown()
        return out
    except Exception:  # pragma: no cover - depends on env
        logger.exception("pynvml sampling failed")
        return {}


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


class SystemMetricsSampler:
    """Owns a daemon thread that calls run.log() with system metrics."""

    def __init__(self, run, interval: float = 10.0):
        self.run = run
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

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

    def stop(self, timeout: float = 2.0):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def _run(self):
        # Prime psutil so the first sample isn't 0.0.
        try:
            import psutil  # type: ignore

            psutil.cpu_percent(interval=None)
        except Exception:
            pass

        while not self.stop_event.wait(self.interval):
            samples: dict = {}
            samples.update(_cpu_mem_samples())
            samples.update(_gpu_samples())
            if not samples:
                continue
            try:
                self.run.log(samples)
            except Exception:
                logger.exception("failed to log system metrics")

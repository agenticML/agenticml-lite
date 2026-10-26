"""Loss, learning-rate, and epoch metrics from the torch hooks in ``auto_tracking``.

Values are logged under ``auto/`` at the optimizer step index, with
``commit=False`` so metrics the script logs itself at the same step merge in.
Losses stay on the device between logs; one ``.item()`` per optimizer is paid
every ``every`` steps.
"""

from __future__ import annotations

import logging
import math
import weakref
from typing import Any

logger = logging.getLogger("agenticml.auto")

_MAX_SCALED = 8


class StepMetrics:
    def __init__(self, run: Any, every: int = 1):
        self.run = run
        self.every = max(1, int(every))
        self._pending: list[Any] = []
        self._scaled: dict[int, tuple[Any, Any]] = {}
        self._optimizers: dict[int, Any] = {}
        self._sums: dict[int, Any] = {}
        self._counts: dict[int, int] = {}
        self._primary: int | None = None
        self._steps = 0
        self._epochs = 0
        self._train_loader: int | None = None

    def on_scale(self, scaled: Any, unscaled: Any) -> None:
        """Remember the unscaled loss behind a ``GradScaler.scale`` result."""
        if len(self._scaled) >= _MAX_SCALED:
            self._scaled.clear()
        try:
            ref = weakref.ref(scaled)
        except TypeError:
            return
        self._scaled[id(scaled)] = (ref, unscaled.detach())

    def on_backward(self, tensor: Any) -> None:
        entry = self._scaled.pop(id(tensor), None)
        if entry is not None and entry[0]() is tensor:
            value = entry[1]
        elif _is_scalar(tensor):
            value = tensor.detach()
        else:
            return
        self._pending.append(value)

    def on_step(self, optimizer: Any) -> None:
        opt_id = id(optimizer)
        if opt_id not in self._optimizers:
            self._optimizers[opt_id] = _ref(optimizer)
            if self._primary is None:
                self._primary = opt_id
        if self._pending:
            # Mean over backward() calls since the last step (gradient accumulation).
            value = self._pending[0]
            for extra in self._pending[1:]:
                value = value + extra
            if len(self._pending) > 1:
                value = value / len(self._pending)
            previous = self._sums.get(opt_id)
            self._sums[opt_id] = value if previous is None else previous + value
            self._counts[opt_id] = self._counts.get(opt_id, 0) + 1
            self._pending = []
        self._scaled.clear()
        if opt_id != self._primary:
            return
        self._steps += 1
        if self._steps % self.every == 0:
            self.emit()

    def on_epoch_end(self, loader: Any) -> None:
        if self._train_loader is None:
            self._train_loader = id(loader)
        if id(loader) == self._train_loader:
            self._epochs += 1

    def emit(self) -> None:
        """Log what has accumulated since the last log. No-op before the first step."""
        if self._steps == 0:
            return
        metrics: dict[str, float] = {}
        multi = len(self._optimizers) > 1
        for opt_id, ref in list(self._optimizers.items()):
            optimizer = ref()
            prefix = f"auto/{self._name(opt_id)}/" if multi else "auto/"
            count = self._counts.get(opt_id, 0)
            if count:
                _put(metrics, prefix + "loss", _to_float(self._sums[opt_id]), count)
            if optimizer is not None:
                metrics.update(_learning_rates(optimizer, prefix))
        self._sums.clear()
        self._counts.clear()
        metrics["auto/epoch"] = self._epochs
        try:
            self.run.log(metrics, step=self._steps - 1, commit=False)
        except Exception:
            logger.debug("auto metrics log failed", exc_info=True)

    def flush(self) -> None:
        if self._sums:
            self.emit()

    def _name(self, opt_id: int) -> str:
        tracked = getattr(self.run, "_tracked", {}) or {}
        name = tracked.get(("optimizer", opt_id))
        if isinstance(name, str) and name:
            return name
        index = list(self._optimizers).index(opt_id)
        return f"optimizer{index}" if index else "optimizer"


def _learning_rates(optimizer: Any, prefix: str) -> dict[str, float]:
    rates: list[float] = []
    for group in getattr(optimizer, "param_groups", None) or []:
        if isinstance(group, dict) and "lr" in group:
            value = _to_float(group["lr"])
            if value is not None:
                rates.append(value)
    if not rates:
        return {}
    if len(set(rates)) == 1:
        return {prefix + "lr": rates[0]}
    return {f"{prefix}lr/group{index}": rate for index, rate in enumerate(rates)}


def _put(metrics: dict, key: str, total: float | None, count: int) -> None:
    if total is None:
        return
    value = total / count
    if math.isfinite(value):
        metrics[key] = value


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _is_scalar(tensor: Any) -> bool:
    try:
        return int(tensor.numel()) == 1
    except Exception:
        return False


def _ref(obj: Any):
    try:
        return weakref.ref(obj)
    except TypeError:
        return lambda obj=obj: obj

#!/usr/bin/env python3
"""Time scalar writes, overview reads, and list_runs.

Stdlib only. Writes under a temporary directory and deletes it on the way out.
"""

from __future__ import annotations

import shutil
import statistics
import tempfile
import time
from pathlib import Path

from agenticml.store import RunReader, RunWriter, list_runs

_BATCH = 1000
_READS = 20


def _scalar_bytes(run_dir: Path) -> int:
    total = 0
    scalars = run_dir / "scalars"
    for path in scalars.iterdir():
        if path.is_file():
            total += path.stat().st_size
    return total


def _write_one(writer: RunWriter, count: int) -> None:
    for start in range(0, count, _BATCH):
        stop = start + _BATCH
        writer.write_scalars([(step, {"loss": float(step)}) for step in range(start, stop)])


def _write_many(writer: RunWriter, series: int, count: int) -> None:
    keys = [f"s{index}" for index in range(series)]
    for start in range(0, count, _BATCH):
        stop = start + _BATCH
        writer.write_scalars(
            [(step, {key: float(step) for key in keys}) for step in range(start, stop)]
        )


def _median_ms(samples: list[float]) -> float:
    return statistics.median(samples) * 1e3


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="agenticml-bench-"))
    try:
        root = tmp / "bench"
        root.mkdir()

        one_count = 1_000_000
        one = RunWriter.create(root, run_id="bench1m", meta={})
        started = time.perf_counter()
        _write_one(one, one_count)
        one_seconds = time.perf_counter() - started
        one_bytes = _scalar_bytes(one.run_dir)
        print(
            f"1x{one_count} write: {one_bytes / one_count:.6f} bytes/point (scalars), "
            f"{one_count / one_seconds:.1f} points/s",
            flush=True,
        )

        many_series = 10
        many_count = 100_000
        many_points = many_series * many_count
        many = RunWriter.create(root, run_id="bench10x", meta={})
        started = time.perf_counter()
        _write_many(many, many_series, many_count)
        many_seconds = time.perf_counter() - started
        many_bytes = _scalar_bytes(many.run_dir)
        print(
            f"10x{many_count} write: {many_bytes / many_points:.6f} bytes/point (scalars), "
            f"{many_points / many_seconds:.1f} points/s",
            flush=True,
        )
        many.finish("finished", {})
        one.finish("finished", {})
        one_ovw_bytes = (one.run_dir / "scalars" / "0.ovw").stat().st_size
        print(f"1M .ovw size: {one_ovw_bytes} bytes", flush=True)

        fresh: list[float] = []
        fresh_result = None
        for _ in range(_READS):
            reader = RunReader(root, one.run_id)
            started = time.perf_counter()
            fresh_result = reader.series("loss", max_points=1000)
            fresh.append(time.perf_counter() - started)
        print(
            f"series(loss, max_points=1000) fresh median: {_median_ms(fresh):.3f} ms "
            f"(mode={fresh_result['mode']}, total={fresh_result['total']})",
            flush=True,
        )

        warm = RunReader(root, one.run_id)
        warm.series("loss", max_points=1000)
        warm_samples: list[float] = []
        for _ in range(_READS):
            started = time.perf_counter()
            warm.series("loss", max_points=1000)
            warm_samples.append(time.perf_counter() - started)
        print(
            f"series(loss, max_points=1000) warm median: {_median_ms(warm_samples):.3f} ms",
            flush=True,
        )

        start = 499_500
        end = 500_499
        range_reader = RunReader(root, one.run_id)
        range_samples: list[float] = []
        ranged = None
        for _ in range(_READS):
            started = time.perf_counter()
            ranged = range_reader.series("loss", max_points=1000, start=start, end=end)
            range_samples.append(time.perf_counter() - started)
        print(
            f"series(loss, start={start}, end={end}) median: {_median_ms(range_samples):.3f} ms "
            f"(mode={ranged['mode']}, total={ranged['total']})",
            flush=True,
        )

        list_root = tmp / "listing"
        list_root.mkdir()
        for index in range(1000):
            RunWriter.create(list_root, run_id=f"t{index:04d}", meta={})
        started = time.perf_counter()
        found = list_runs(list_root)
        list_ms = (time.perf_counter() - started) * 1e3
        print(f"list_runs ({len(found)} runs): {list_ms:.3f} ms", flush=True)
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    main()

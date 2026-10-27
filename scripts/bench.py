#!/usr/bin/env python3
"""Benchmark agenticml against its targets. Stdlib only; temp dir is deleted.

    python scripts/bench.py [--quick]
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

IMPORT_SNIPPET = "import time;t=time.perf_counter();import agenticml;print(time.perf_counter()-t)"
KEYS = ["loss"] + [f"m{i}" for i in range(1, 10)]
ROWS: list[tuple[str, str, str]] = []  # (metric, value, verdict)


def row(metric, value, ok=None, target=""):
    verdict = "" if ok is None else ("PASS" if ok else "FAIL")
    ROWS.append((metric, value, f"{verdict} (target {target})" if verdict else "info"))


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.iterdir() if p.is_file())


def bench_import() -> float:
    samples = []
    for _ in range(5):
        out = subprocess.run([sys.executable, "-c", IMPORT_SNIPPET], capture_output=True, text=True, check=True)
        samples.append(float(out.stdout.strip().splitlines()[-1]) * 1e3)
    return statistics.median(samples)


def bench_log(root: Path, calls: int):
    import agenticml

    agenticml.init(project="bench", name="log", dir=str(root), system_metrics=False,
                   track_source=False, auto_track=False)
    run_id = agenticml.get_active_run().run_id
    rng = random.Random(0)
    values = [rng.random() for _ in KEYS]
    lat = []
    clock = time.perf_counter_ns
    for i in range(calls):
        metrics = {k: v + i for k, v in zip(KEYS, values)}  # built before the timer starts
        t0 = clock()
        agenticml.log(metrics, step=i)
        lat.append(clock() - t0)
    agenticml.finish()
    lat.sort()
    median_us = statistics.median(lat) / 1e3
    p99_us = lat[int(len(lat) * 0.99) - 1] / 1e3
    scalar_bytes = dir_bytes(root / "runs" / run_id / "scalars")
    return median_us, p99_us, scalar_bytes / (calls * len(KEYS))


def write_big_run(root: Path, points: int) -> str:
    import agenticml

    agenticml.init(project="bench", name="big", dir=str(root), system_metrics=False,
                   track_source=False, auto_track=False)
    run_id = agenticml.get_active_run().run_id
    rng = random.Random(1)
    v = 1.0
    for i in range(points):
        v += rng.gauss(0, 0.01)
        agenticml.log({"loss": v}, step=i)
    agenticml.finish()
    return run_id


def write_small_runs(root: Path, n: int) -> None:
    from agenticml.store import RunWriter

    for i in range(n):
        w = RunWriter.create(root, run_id=f"small-{i:05d}", meta={"project": "bench", "name": f"small{i}"})
        w.write_scalars([(s, {"loss": 1.0 / (s + 1)}) for s in range(20)])
        w.finish("finished", {"loss": 0.05})


def get(url: str):
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())


def bench_ui(root: Path, big_id: str):
    from agenticml.ui.server import make_server

    t0 = time.perf_counter()
    server = make_server(root, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    runs = get(base + "/api/runs")
    cold_ms = (time.perf_counter() - t0) * 1e3
    url = f"{base}/api/runs/{big_id}/scalars?key=loss&max_points=1000"
    get(url)  # first request builds caches; the target is about steady-state
    samples = []
    for _ in range(20):
        t = time.perf_counter()
        get(url)
        samples.append((time.perf_counter() - t) * 1e3)
    server.shutdown()
    server.server_close()
    return cold_ms, len(runs), statistics.median(samples)


def bench_tensorboard(tmp: Path, points: int):
    try:
        from torch.utils.tensorboard import SummaryWriter
    except Exception:
        row("tensorboard", "not installed (skipped)")
        return
    out = tmp / "tb"
    w = SummaryWriter(str(out))
    for i in range(points):
        w.add_scalar("loss", float(i), i)
    w.close()
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    row("tensorboard tfevents", f"{size / points:.1f} B/pt ({size / 1e6:.1f} MB for {points:,} pts)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="smaller run (10k log calls, 100k-point run, 100 runs)")
    args = ap.parse_args()
    calls, big_points, small_runs = (10_000, 100_000, 100) if args.quick else (100_000, 1_000_000, 1_000)
    points = calls * len(KEYS)

    tmp = Path(tempfile.mkdtemp(prefix="agenticml-bench-"))
    try:
        ms = bench_import()
        row("import agenticml", f"{ms:.1f} ms (median of 5)", ms < 100, "< 100 ms")

        med, p99, bpp = bench_log(tmp / "log", calls)
        row(f"log() x10 scalars, median ({calls:,} calls)", f"{med:.2f} us", med < 10, "< 10 us")
        row("log() x10 scalars, p99", f"{p99:.2f} us")
        row(f"disk bytes/point ({points:,} pts)", f"{bpp:.2f} B/pt", bpp <= 9.0, "<= 9.0 B/pt (8 B raw + overview)")

        root = tmp / "ui"
        big_id = write_big_run(root, big_points)
        write_small_runs(root, small_runs)
        cold, n, req = bench_ui(root, big_id)
        row(f"ui cold start to /api/runs ({n} runs)", f"{cold:.0f} ms", cold < 1000, "< 1 s")
        row(f"scalars request ({big_points:,}-pt run, median of 20)", f"{req:.2f} ms", req < 20, "< 20 ms")

        bench_tensorboard(tmp, big_points)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    w0 = max(len(r[0]) for r in ROWS)
    w1 = max(len(r[1]) for r in ROWS)
    for metric, value, verdict in ROWS:
        print(f"{metric.ljust(w0)}  {value.ljust(w1)}  {verdict}")


if __name__ == "__main__":
    main()

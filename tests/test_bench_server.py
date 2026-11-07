"""Smoke test for scripts/bench_server.py --quick."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bench_server_help_mentions_data_cleanup():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench_server.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "removes bench-* runs and keys" in proc.stdout


def test_bench_server_quick():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench_server.py"), "--quick"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    header = next(
        (line for line in proc.stdout.splitlines()
         if line.startswith("metric ") and line.rstrip().endswith("value")),
        None,
    )
    assert header is not None, proc.stdout
    assert "object PUT p50" in proc.stdout
    assert "objects/s (aggregate, initial)" in proc.stdout

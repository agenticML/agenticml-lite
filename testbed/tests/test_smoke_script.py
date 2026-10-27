"""The documented shell smoke flow runs in the same isolated environment."""
import os
import subprocess

import pytest

from conftest import TESTBED

pytestmark = pytest.mark.cli


def test_smoke_script(tmp_path):
    sentinel = tmp_path / "sentinel-store"
    sentinel.mkdir()
    env = dict(os.environ, AGENTICML_DIR=str(sentinel), AGENTICML_MODE="disabled", RANK="1")
    result = subprocess.run(["bash", str(TESTBED / "scripts" / "smoke.sh")], cwd=TESTBED,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("SMOKE OK")
    assert not sentinel.exists() or not any(sentinel.iterdir())

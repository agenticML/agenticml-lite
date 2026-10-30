"""Isolated stores and reusable CLI/dashboard session fixtures."""
from contextlib import contextmanager
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

TESTBED = Path(__file__).resolve().parents[1]
BIN = Path(sys.executable).parent
CONSOLE = BIN / "agenticml"
# Resolve this before any fixture sandboxes HOME, so browser tests still run.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
# Makes the project importable without requiring a second editable install.
sys.path.insert(0, str(TESTBED))


def clean_env(root, config):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTICML_")}
    for key in ("RANK", "PMI_RANK", "OMPI_COMM_WORLD_RANK", "SLURM_PROCID", "LOCAL_RANK"):
        env.pop(key, None)
    home = Path(config).parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home),
               AGENTICML_DIR=str(root), AGENTICML_CONFIG_DIR=str(config),
               XDG_CONFIG_HOME=str(Path(config).parent / "xdg"),
               PATH=str(BIN) + os.pathsep + env.get("PATH", ""),
               PYTHONPATH=str(TESTBED) + os.pathsep + env.get("PYTHONPATH", ""),
               PYTHONUNBUFFERED="1", NO_PROXY="127.0.0.1,localhost,::1",
               no_proxy="127.0.0.1,localhost,::1")
    return env


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("AGENTICML_") or key in (
            "RANK", "PMI_RANK", "OMPI_COMM_WORLD_RANK", "SLURM_PROCID", "LOCAL_RANK"
        ):
            monkeypatch.delenv(key, raising=False)
    env = clean_env(tmp_path / "store", tmp_path / "config")
    for key in ("HOME", "USERPROFILE", "AGENTICML_DIR", "AGENTICML_CONFIG_DIR", "XDG_CONFIG_HOME",
                "PATH", "PYTHONPATH", "PLAYWRIGHT_BROWSERS_PATH", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, env[key])
    import agenticml
    if agenticml.get_active_run() is not None:
        agenticml.finish()
    yield
    if agenticml.get_active_run() is not None:
        agenticml.finish()


@pytest.fixture
def cli():
    def invoke(*args, env=None, input=None, timeout=60, check=True, module=False):
        command = [sys.executable, "-m", "agenticml"] if module else [str(CONSOLE)]
        child_env = dict(os.environ)
        child_env.update(env or {})
        child_env["PATH"] = str(BIN) + os.pathsep + child_env.get("PATH", "")
        return subprocess.run([*command, *map(str, args)], cwd=TESTBED, env=child_env,
                              input=input, text=True, capture_output=True, timeout=timeout, check=check)
    return invoke


def stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if proc.stdout:
        proc.stdout.close()


@pytest.fixture(scope="session")
def trained_store(tmp_path_factory):
    base = tmp_path_factory.mktemp("trained")
    root = base / "store"
    env = clean_env(root, base / "config")
    names = ["explicit-baseline", "explicit-highlr", "zero-code", "crashed", "other-project"]
    for name in names:
        project = "other-project" if name == "other-project" else "testbed"
        training = ["-m", "dummytrain.train", "--project", project, "--name", name,
                    "--out", str(base / name), "--tags", "fixture,baseline"]
        if name == "zero-code":
            command = [str(CONSOLE), "run", "--project", project, "--name", name,
                       "--", sys.executable, *training]
        else:
            command = [sys.executable, *training, "--explicit"]
        if name == "explicit-highlr":
            command += ["--lr", "0.4", "--tags", "fixture,highlr"]
        if name == "crashed":
            command += ["--fail-at-epoch", "2"]
        result = subprocess.run(command, cwd=TESTBED, env=env, text=True,
                                capture_output=True, timeout=30)
        assert result.returncode == (1 if name == "crashed" else 0), result.stdout + result.stderr
    from agenticml.store import list_runs
    runs = {row["name"]: row["run_id"] for row in list_runs(root)}
    assert set(runs) == set(names)
    return SimpleNamespace(root=root, runs=runs)


@contextmanager
def serve_ui(root, config, runs=None, extra_args=()):
    env = clean_env(root, config)
    proc = subprocess.Popen([str(CONSOLE), "ui", "--dir", str(root), "--port", "0", *extra_args],
                            cwd=TESTBED, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    lines = queue.Queue()

    def read_output():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    thread = threading.Thread(target=read_output, daemon=True)
    thread.start()
    try:
        line = lines.get(timeout=15)
        assert line is not None, "UI exited before announcing its URL"
        match = re.search(r"AgenticML UI: (http://127\.0\.0\.1:\d+)", line)
        assert match, line
        yield SimpleNamespace(url=match.group(1), root=root, runs=runs, proc=proc)
    finally:
        stop_process(proc)
        thread.join(timeout=2)


@pytest.fixture(scope="session")
def ui_server(trained_store, tmp_path_factory):
    with serve_ui(trained_store.root, tmp_path_factory.mktemp("ui-config"), trained_store.runs) as server:
        yield server


@pytest.fixture
def ui_server_factory(tmp_path):
    """Mutation tests own their stores and servers; the session store is read-only."""
    def factory(root, extra_args=()):
        return serve_ui(root, tmp_path / "ui-config", extra_args=extra_args)
    return factory

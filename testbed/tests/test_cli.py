"""Real console commands, launcher lifecycle, credentials, and local uploads."""
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from agenticml.server.app import make_server
from agenticml.server.ingest import IngestHandler
from agenticml.store import RunReader, RunWriter, list_runs
from agenticml.store.layout import object_path
from agenticml.store.reader import run_references
import pytest

from conftest import CONSOLE, TESTBED, stop_process

pytestmark = pytest.mark.cli


@pytest.mark.parametrize("command", [None, "run", "ls", "du", "gc", "ui", "sync", "login", "logout", "whoami"])
def test_help(cli, command):
    result = cli(*([command, "-h"] if command else ["--help"]))
    assert "usage:" in result.stdout
    if command:
        assert command in result.stdout


def test_module_entrypoint(cli):
    assert "usage: agenticml" in cli("--help", module=True).stdout


@pytest.fixture
def training_script(tmp_path):
    path = tmp_path / "train.py"
    path.write_text("""import argparse
p = argparse.ArgumentParser()
p.add_argument('--lr', type=float, default=0.125)
p.add_argument('--epochs', type=int, default=2)
args = p.parse_args()
print('training', args.lr, args.epochs)
""")
    return path


@pytest.mark.parametrize("form", ["python-script", "script", "module", "short"])
def test_launcher_forms(form, cli, training_script, tmp_path):
    if form == "module":
        command = ["run", "--project", "forms", "-m", "dummytrain.train", "--epochs", "1",
                   "--steps-per-epoch", "2", "--out", str(tmp_path / "checkpoint")]
    elif form == "short":
        command = ["python", str(training_script), "--lr", "0.25"]
    else:
        command = ["run", "--project", "forms", "--"]
        if form == "python-script":
            command += ["python"]
        command += [str(training_script), "--lr", "0.25"]
    result = cli(*command)
    rows = list_runs(Path(os.environ["AGENTICML_DIR"]))
    assert len(rows) == 1 and rows[0]["status"] == "finished"
    assert ("loss" if form == "module" else "training") in result.stdout
    rr = RunReader(Path(os.environ["AGENTICML_DIR"]), rows[0]["run_id"])
    assert rr.meta()["config"]["lr"] == (0.15 if form == "module" else 0.25)
    assert any(e["path"].endswith("train.py") for e in rr.code_manifest())
    assert not any(k.startswith("train/") for k in rr.keys())


@pytest.mark.parametrize("source", ["flags", "environment", "flags-win"])
def test_launcher_metadata_and_dir(source, cli, training_script, tmp_path):
    root = tmp_path / "chosen-store"
    values = {"project": "chosen", "name": "metadata", "tags": "a,b", "notes": "some notes",
              "group": "sweep", "job-type": "train", "dir": str(root)}
    env = {}
    options = []
    if source in ("environment", "flags-win"):
        env = {"AGENTICML_" + k.upper().replace("-", "_"): v for k, v in values.items()}
    if source != "environment":
        options = [item for key, value in values.items() for item in ("--" + key, value)]
    if source == "flags-win":
        env.update(AGENTICML_PROJECT="wrong", AGENTICML_NAME="wrong", AGENTICML_DIR=str(tmp_path / "wrong"))
    cli("run", *options, "--", str(training_script), env=env)
    row, = list_runs(root)
    for key in ("project", "name", "notes", "group", "job-type"):
        assert row[key.replace("-", "_")] == values[key]
    assert row["tags"] == ["a", "b"]
    assert row["config"]["lr"] == 0.125 and row["config"]["epochs"] == 2
    assert row["env"] and row["git"]["repo_root"]
    assert not (tmp_path / "wrong" / "runs").exists()


@pytest.mark.parametrize("source", ["flag", "environment"])
def test_no_capture_args(source, cli, training_script):
    options = ["--no-capture-args"] if source == "flag" else []
    env = {"AGENTICML_CAPTURE_ARGS": "0"} if source == "environment" else {}
    cli("run", *options, "--", training_script, "--lr", "0.6", env=env)
    row, = list_runs(Path(os.environ["AGENTICML_DIR"]))
    assert "lr" not in row["config"] and "epochs" not in row["config"]


def test_script_init_joins_launcher(cli, tmp_path):
    script = tmp_path / "explicit.py"
    script.write_text("""import agenticml
r = agenticml.init('ignored', 'ignored', config={'lr': 0.4}, tags=['script'])
import argparse
p = argparse.ArgumentParser()
p.add_argument('--lr', type=float, default=0.9)
p.parse_args()
agenticml.log({'loss': 1}, step=2)
agenticml.finish()
assert agenticml.get_active_run() is r
agenticml.log({'loss': 0.5})
""")
    cli("run", "--project", "launcher", "--name", "joined", "--tags", "outer", "--", script)
    row, = list_runs(Path(os.environ["AGENTICML_DIR"]))
    assert row["project"] == "launcher" and row["name"] == "joined"
    assert row["config"]["lr"] == 0.4 and row["tags"] == ["outer", "script"]
    rr = RunReader(Path(os.environ["AGENTICML_DIR"]), row["run_id"])
    assert rr.series("loss")["points"] == [[2, 1.0], [3, 0.5]]
    assert row["status"] == "finished"


@pytest.mark.parametrize("body,code,exception", [
    ("print('ok')", 0, None), ("raise SystemExit(23)", 23, None),
    ("raise RuntimeError('broken')", 1, "RuntimeError"),
])
def test_launcher_exit_status(body, code, exception, cli, tmp_path):
    script = tmp_path / "exit.py"
    script.write_text(body + "\n")
    result = cli("run", "--", script, check=False)
    assert result.returncode == code
    row, = list_runs(Path(os.environ["AGENTICML_DIR"]))
    assert row["status"] == ("crashed" if code else "finished")
    assert row["finished_at"]
    assert row["summary"].get("_exit_code") == (code or None)
    assert row["summary"].get("_exception") == exception
    if exception:
        assert "RuntimeError: broken" in result.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGTERM exit convention")
def test_sigterm_finishes_run(tmp_path):
    ready = tmp_path / "ready"
    script = tmp_path / "wait.py"
    script.write_text(f"import time\nfrom pathlib import Path\nPath({str(ready)!r}).touch()\nwhile True: time.sleep(0.05)\n")
    proc = subprocess.Popen([str(CONSOLE), "run", "--", str(script)], cwd=TESTBED,
                            env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.02)
        assert ready.exists(), "script did not start"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 143
        row, = list_runs(Path(os.environ["AGENTICML_DIR"]))
        assert row["status"] == "crashed" and row["finished_at"]
        assert row["summary"]["_exit_code"] == 143
        assert row["summary"]["_exception"] == "SIGTERM"
    finally:
        stop_process(proc)


@pytest.mark.parametrize("source", ["flag", "environment"])
def test_disabled_launcher(source, cli, training_script):
    options = ["--mode", "disabled"] if source == "flag" else []
    env = {"AGENTICML_MODE": "disabled"} if source == "environment" else {}
    assert "training" in cli("run", *options, "--", training_script, env=env).stdout
    assert not Path(os.environ["AGENTICML_DIR"]).exists()
    assert not (Path.home() / ".agenticml").exists()
    assert not (Path.home() / ".config/agenticml").exists()


def test_listing_and_usage(cli, trained_store):
    rows = json.loads(cli("ls", "--dir", trained_store.root, "--json").stdout)
    assert len(rows) == 5
    assert [(r["created_at"], r["run_id"]) for r in rows] == sorted(
        [(r["created_at"], r["run_id"]) for r in rows], reverse=True)
    output = cli("ls", "--dir", trained_store.root).stdout
    assert all(column in output for column in ("RUN ID", "PROJECT", "NAME", "STATUS", "CREATED", "LAST STEP"))
    assert output.rstrip().endswith("5 runs")
    assert [line.split()[0] for line in output.splitlines()[1:-1]] == [r["run_id"] for r in rows]
    baseline_id = trained_store.runs["explicit-baseline"]
    baseline_line = next(line for line in output.splitlines() if line.startswith(baseline_id))
    assert baseline_line.split()[-1] == str(RunReader(trained_store.root, baseline_id).last_step())
    selected = json.loads(cli("ls", "--dir", trained_store.root, "--json", "--project", "testbed").stdout)
    assert len(selected) == 4 and all(r["project"] == "testbed" for r in selected)
    assert "4 runs" in cli("ls", "--dir", trained_store.root, "--project", "testbed").stdout
    report = json.loads(cli("du", "--dir", trained_store.root, "--json").stdout)
    assert len(report["runs"]) == 5 and report["total"] > 0 and report["objects_total"] > 0
    assert all(not r["synced"] for r in report["runs"])
    assert "Total store usage:" in cli("du", "--dir", trained_store.root).stdout


def test_empty_store(cli):
    assert json.loads(cli("ls", "--json").stdout) == []
    assert cli("ls").stdout.rstrip().endswith("0 runs")
    assert json.loads(cli("du", "--json").stdout)["total"] == 0
    assert "Deleted 0 run(s)" in cli("gc", "-y").stdout


@pytest.fixture
def copied_store(trained_store, tmp_path):
    root = tmp_path / "copy"
    shutil.copytree(trained_store.root, root)
    return root


def test_gc_protects_unsynced_and_dry_run(cli, copied_store):
    def contents():
        return {str(p.relative_to(copied_store)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in copied_store.rglob("*") if p.is_file()}
    before = contents()
    assert "Would delete 5 run(s)" in cli("gc", "--dir", copied_store, "--dry-run", "--include-unsynced").stdout
    assert contents() == before
    assert "Deleted 0 run(s)" in cli("gc", "--dir", copied_store, "-y").stdout
    assert len(list_runs(copied_store)) == 5
    assert cli("gc", "--dir", copied_store, "--include-unsynced", check=False).returncode == 2
    assert "Deleted 5 run(s)" in cli("gc", "--dir", copied_store, "--include-unsynced", "-y").stdout
    assert list_runs(copied_store) == []


def test_gc_keep_last_per_project(cli, copied_store):
    rows = list_runs(copied_store)
    protected = {next(r["run_id"] for r in rows if r["project"] == project)
                 for project in ("testbed", "other-project")}
    cli("gc", "--dir", copied_store, "--include-unsynced", "--keep-last", "1", "-y")
    assert {r["run_id"] for r in list_runs(copied_store)} == protected


def test_gc_age_selection(cli, copied_store):
    rows = list_runs(copied_store)
    old = rows[-1]
    path = copied_store / "runs" / old["run_id"] / "meta.json"
    meta = json.loads(path.read_text())
    meta["created_at"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(meta))
    cli("gc", "--dir", copied_store, "--include-unsynced", "--older-than", "5", "-y")
    assert {r["run_id"] for r in list_runs(copied_store)} == {r["run_id"] for r in rows} - {old["run_id"]}


def test_login_offline_whoami_logout(cli):
    key = "testbed-private-api-key-123456"
    output = cli("login", "--host", "https://example.invalid", "--no-verify", input=key + "\n").stdout
    path = Path(os.environ["AGENTICML_CONFIG_DIR"]) / "credentials.json"
    assert json.loads(path.read_text())["hosts"]["https://example.invalid"]["api_key"] == key
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    who = cli("whoami", "--offline").stdout
    assert "https://example.invalid" in who and key[:4] in who and key not in who + output
    assert "unchecked (offline)" in who
    cli("logout")
    # Logout removes this host's credential, retaining the credential container.
    assert "https://example.invalid" not in json.loads(path.read_text())["hosts"]
    assert cli("whoami", "--host", "https://example.invalid", "--offline", check=False).returncode == 1


@pytest.mark.parametrize("command", ["login", "whoami", "sync"])
def test_nonlocal_http_rejected(command, cli):
    args = {"login": ["--no-verify"], "whoami": ["--offline"], "sync": ["--all", "--dry-run"]}[command]
    result = cli(command, *args, "--host", "http://example.invalid", input="test-key\n", check=False)
    assert result.returncode == 1 and "--insecure" in result.stderr


def test_sync_dry_run_and_no_key(cli, copied_store):
    run_id = list_runs(copied_store)[0]["run_id"]
    options = ["--dir", copied_store, "--host", "http://127.0.0.1:1"]
    result = cli("sync", run_id, *options, "--dry-run")
    assert "dry-run (would send; objects unchecked)" in result.stdout and "files=" in result.stdout
    assert not list(copied_store.rglob("sync.json"))
    result = cli("sync", run_id, *options, check=False)
    assert result.returncode == 1 and "not logged in" in result.stderr
    result = cli("sync", run_id, "--dir", copied_store, check=False)
    assert result.returncode == 1 and "no server host" in result.stderr
    assert not list(copied_store.rglob("sync.json"))


@pytest.fixture
def sync_server(tmp_path):
    data = tmp_path / "data"
    calls = []

    class RecordingHandler(IngestHandler):
        def _dispatch(self):
            calls.append((self.command, self.path, self.headers.get("Authorization"), time.monotonic()))
            return super()._dispatch()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _dispatch

    server = make_server(data, port=0)
    server.RequestHandlerClass = RecordingHandler

    def create_key(name):
        return server.keys.create(name, scope="sync")[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(host=f"http://127.0.0.1:{server.server_port}", key=create_key("testbed"),
                              store=data / "store", create_key=create_key, calls=calls)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive(), "ingest server did not stop"


def assert_synced_run(root, run_id, server, *, dead=False):
    """Compare protocol files and objects, excluding local bookkeeping."""
    def files(directory):
        return {path.relative_to(directory).as_posix(): path.read_bytes()
                for path in directory.rglob("*") if path.is_file() and re.fullmatch(
                    r"meta\.json|events\.jsonl|(?:scalars|media)/index\.json|"
                    r"scalars/(?:_time\.bin|(?:0|[1-9][0-9]{0,8})\.(?:bin|ovw))",
                    path.relative_to(directory).as_posix())}

    expected = files(root / "runs" / run_id)
    actual = files(server.store / "runs" / run_id)
    if dead:
        local_meta = json.loads(expected.pop("meta.json"))
        remote_meta = json.loads(actual.pop("meta.json"))
        assert local_meta["status"] == "running" and remote_meta["status"] == "crashed"
        assert remote_meta == {**local_meta, "status": "crashed"}
    assert actual == expected
    for hashes in run_references(root, run_id).values():
        for sha in hashes:
            assert object_path(server.store, sha).read_bytes() == object_path(root, sha).read_bytes()
    return json.loads((server.store / "runs" / run_id / "remote.json").read_text())


def test_verified_login_and_online_whoami(cli, sync_server):
    login = cli("login", "--host", sync_server.host, input=sync_server.key + "\n")
    assert "Logged in" in login.stdout
    whoami = cli("whoami")
    assert "valid" in whoami.stdout
    assert [(method, path) for method, path, _, _ in sync_server.calls] == [
        ("GET", "/v2/whoami"), ("GET", "/v2/whoami")]
    wrong = cli("login", "--host", sync_server.host, input="wrong-key\n", check=False)
    assert wrong.returncode == 1 and "invalid API key" in wrong.stderr
    assert sync_server.key not in login.stdout + login.stderr + whoami.stdout + whoami.stderr
    assert "wrong-key" not in wrong.stdout + wrong.stderr


def test_credentials_stdin_env_file_precedence_and_host_logout(cli, sync_server, tmp_path):
    file_key, env_key = sync_server.create_key("file"), sync_server.create_key("env-key")
    options = ["--host", sync_server.host]
    cli("login", *options, input=file_key + "\n", env={"AGENTICML_API_KEY": env_key})
    path = Path(os.environ["AGENTICML_CONFIG_DIR"]) / "credentials.json"
    assert json.loads(path.read_text())["hosts"][sync_server.host]["api_key"] == file_key
    assert sync_server.calls[-1][2] == "Bearer " + file_key

    before = len(sync_server.calls)
    result = cli("whoami", *options, env={"AGENTICML_API_KEY": env_key})
    assert "source=env" in result.stdout and "valid" in result.stdout
    assert [auth for _, _, auth, _ in sync_server.calls[before:]] == ["Bearer " + env_key]

    root = tmp_path / "credential-sync-store"
    for name, env, expected in (("env", {"AGENTICML_API_KEY": env_key}, env_key), ("file", {}, file_key)):
        writer = RunWriter.create(root, meta={"project": "credentials", "name": name})
        writer.write_scalars([(0, {"loss": 1})])
        writer.finish("finished")
        before = len(sync_server.calls)
        cli("sync", writer.run_id, "--dir", root, *options, env=env)
        calls = sync_server.calls[before:]
        assert assert_synced_run(root, writer.run_id, sync_server)["status"] == "finished"
        assert calls and all(auth == "Bearer " + expected for _, _, auth, _ in calls)

    other_host = "https://second.example.invalid"
    cli("login", "--host", other_host, "--no-verify", input="second-host-key\n")
    cli("logout", *options)
    assert json.loads(path.read_text())["hosts"] == {other_host: {"api_key": "second-host-key"}}
    assert "source=file" in cli("whoami", "--host", other_host, "--offline").stdout


def test_full_incremental_sync_reset_and_all(cli, copied_store, sync_server):
    row = next(r for r in list_runs(copied_store) if r["name"] == "explicit-baseline")
    run_id = row["run_id"]
    options = ["--dir", copied_store, "--host", sync_server.host]
    env = {"AGENTICML_API_KEY": sync_server.key}
    result = cli("sync", run_id, *options, env=env)
    assert "failed" not in result.stdout
    assert assert_synced_run(copied_store, run_id, sync_server)["status"] == "finished"
    path = copied_store / "runs" / run_id / "sync.json"
    state = json.loads(path.read_text())["hosts"][sync_server.host]
    assert state["protocol"] == 2 and state["complete"]
    remote_meta = RunReader(sync_server.store, run_id).meta()
    assert remote_meta["project"] == "testbed" and remote_meta["name"] == "explicit-baseline"
    assert remote_meta["config"]["lr"] == 0.15 and remote_meta["tags"] == row["tags"]
    assert remote_meta["summary"]["final_loss"] < 0.2
    series_id = json.loads((copied_store / "runs" / run_id / "scalars/index.json").read_text())[
        "series"]["train/loss"]["id"]
    series = f"scalars/{series_id}.bin"
    previous_size = (sync_server.store / "runs" / run_id / series).stat().st_size
    assert previous_size == 32 * 8 and state["files"][series]["size"] == previous_size
    before = len(sync_server.calls)
    assert "up to date" in cli("sync", run_id, *options, env=env).stdout
    idle_calls = sync_server.calls[before:]
    # Task B requires a command-level whoami even when the run is idle.
    assert len(idle_calls) <= 1
    assert all((method, path) == ("GET", "/v2/whoami") for method, path, _, _ in idle_calls)
    before = len(sync_server.calls)
    writer = RunWriter.resume(copied_store, run_id, must=True)
    try:
        writer.write_scalars([(40, {"train/loss": 0.01})])
        writer.finish("finished")
    finally:
        writer.close()
    cli("sync", run_id, *options, env=env)
    assert assert_synced_run(copied_store, run_id, sync_server)["status"] == "finished"
    appends = [urlsplit(url) for method, url, _, _ in sync_server.calls[before:]
               if method == "POST" and urlsplit(url).path == f"/v2/runs/{run_id}/append/{series}"]
    assert len(appends) == 1 and parse_qs(appends[0].query) == {"offset": [str(previous_size)]}
    cli("sync", "--all", *options, env=env)
    for run in list_runs(copied_store):
        state = json.loads((copied_store / "runs" / run["run_id"] / "sync.json").read_text())[
            "hosts"][sync_server.host]
        assert state["protocol"] == 2 and state["complete"]
        assert_synced_run(copied_store, run["run_id"], sync_server)
    before = len(sync_server.calls)
    cli("sync", run_id, "--reset", *options, env=env)
    assert sync_server.host not in json.loads(path.read_text())["hosts"]
    assert len(sync_server.calls) == before
    result = cli("sync", run_id, *options, env=env)
    assert "bytes=0" in result.stdout
    assert not any("truncate=1" in url or "/append/" in url
                   for _, url, _, _ in sync_server.calls[before:])
    assert assert_synced_run(copied_store, run_id, sync_server)["status"] == "finished"
    state = json.loads(path.read_text())["hosts"][sync_server.host]
    assert state["protocol"] == 2 and state["complete"]
    assert cli("sync", "--all", "--reset", *options, env=env, check=False).returncode == 2
    assert cli("sync", "--reset", *options, env=env, check=False).returncode == 2


def test_ui_occupied_port(cli):
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as occupied:
        result = cli("ui", "--port", occupied.server_port, check=False)
    assert result.returncode == 1
    assert "agenticml ui:" in result.stderr and "Traceback" not in result.stderr


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_launcher_capture_args_off_preserves_code(value, cli, training_script):
    env = {"AGENTICML_CAPTURE_ARGS": value}
    cli("run", "--", training_script, env=env)
    row, = list_runs(Path(os.environ["AGENTICML_DIR"]))
    assert "lr" not in row["config"]
    rr = RunReader(Path(os.environ["AGENTICML_DIR"]), row["run_id"])
    assert rr.code_manifest()  # Runtime code remains enabled.


@pytest.mark.parametrize("args", [[], ["--unknown"], ["-m"], ["--log-every", "0"],
                                  ["--log-every", "bad"], ["--project"], ["python", "-c", "print(1)"]])
def test_launcher_usage_errors(args, cli):
    result = cli("run", *args, check=False)
    assert result.returncode == 2 and "agenticml run:" in result.stderr
    assert list_runs(Path(os.environ["AGENTICML_DIR"])) == []


def test_launcher_verbose(cli, training_script):
    output = cli("run", "--verbose", "--", training_script).stdout
    root = Path(os.environ["AGENTICML_DIR"])
    row, = list_runs(root)
    assert row["status"] == "finished"
    assert row["run_id"] in output and str(root) in output and "agenticml ui" in output


def test_gc_keeps_active_run(cli, tmp_path):
    root = tmp_path / "active-store"
    writer = RunWriter.create(root, meta={"project": "active", "name": "live"})
    try:
        cli("gc", "--dir", root, "--include-unsynced", "-y")
        assert list_runs(root)[0]["effective_status"] == "running"
        assert (root / "runs" / writer.run_id / "meta.json").exists()
    finally:
        writer.finish("finished")


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX lifetime run locks")
def test_dead_process_status_and_sync_finalization(cli, tmp_path, sync_server):
    root = tmp_path / "dead-store"
    writer = RunWriter.create(root, run_id="abandoned", meta={"project": "p", "name": "dead"})
    writer.write_scalars([(0, {"loss": 1})])
    writer.close()  # Simulates process death: persisted meta remains running.
    lock_path = root / "runs" / "abandoned" / "run.lock"
    now = time.time()
    os.utime(lock_path, (now, now))
    assert list_runs(root)[0]["effective_status"] == "running"
    cli("sync", "abandoned", "--dir", root, "--host", sync_server.host, env={"AGENTICML_API_KEY": sync_server.key})
    assert list_runs(root)[0]["effective_status"] == "running"
    assert assert_synced_run(root, "abandoned", sync_server)["status"] == "running"
    state_path = root / "runs" / "abandoned" / "sync.json"
    state = json.loads(state_path.read_text())["hosts"][sync_server.host]
    assert state["complete"] is False
    os.utime(lock_path, (0, 0))
    rows = json.loads(cli("ls", "--json", "--dir", root).stdout)
    assert rows[0]["status"] == "running" and rows[0]["effective_status"] == "crashed"
    cli("sync", "abandoned", "--dir", root, "--host", sync_server.host,
        env={"AGENTICML_API_KEY": sync_server.key})
    assert assert_synced_run(root, "abandoned", sync_server, dead=True)["status"] == "crashed"
    assert RunReader(root, "abandoned").meta()["status"] == "running"
    assert list_runs(root)[0]["effective_status"] == "crashed"
    assert json.loads(state_path.read_text())["hosts"][sync_server.host]["complete"] is True


@pytest.mark.parametrize("locked,expired_status", [(False, "crashed"), (None, "stale")])
def test_heartbeat_grace_boundary(tmp_path, monkeypatch, locked, expired_status):
    import importlib
    store_reader = importlib.import_module("agenticml.store.reader")
    root = tmp_path / "unsupported-lock-store"
    writer = RunWriter.create(root, meta={"project": "p", "name": "stale"})
    writer.close()
    heartbeat = 1000.0
    os.utime(root / "runs" / writer.run_id / "run.lock", (heartbeat, heartbeat))
    monkeypatch.setattr(store_reader, "is_run_locked", lambda path: locked)
    assert store_reader.STALE_AFTER_SECONDS == 120
    clock = SimpleNamespace(time=lambda: heartbeat + 119)
    monkeypatch.setattr(store_reader, "time", clock)
    assert list_runs(root)[0]["effective_status"] == "running"
    clock.time = lambda: heartbeat + 120
    assert list_runs(root)[0]["effective_status"] == expired_status


def test_sync_watch_repeats_after_delay_and_completes(tmp_path, sync_server):
    root = tmp_path / "watch-store"
    writer = RunWriter.create(root, meta={"project": "watch", "name": "live"})
    writer.write_scalars([(0, {"loss": 2})])
    interval = 0.4
    proc = subprocess.Popen(
        [str(CONSOLE), "sync", writer.run_id, "--dir", str(root), "--host", sync_server.host,
         "--watch", str(interval)], cwd=TESTBED,
        env=dict(os.environ, AGENTICML_API_KEY=sync_server.key),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    lines = queue.Queue()

    def read_output():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    thread = threading.Thread(target=read_output, daemon=True)
    thread.start()
    try:
        first = lines.get(timeout=10)
        assert first and "partial (running)" in first, first
        assert proc.poll() is None
        state_path = root / "runs" / writer.run_id / "sync.json"
        state = json.loads(state_path.read_text())["hosts"][sync_server.host]
        assert state["complete"] is False
        writer.write_scalars([(1, {"loss": 1})])
        writer.finish("finished")
        assert proc.wait(timeout=10) == 0
        thread.join(timeout=2)
        state = json.loads(state_path.read_text())["hosts"][sync_server.host]
        assert state["complete"] is True
        assert assert_synced_run(root, writer.run_id, sync_server)["status"] == "finished"
        assert RunReader(sync_server.store, writer.run_id).series("loss")["points"] == [[0, 2.0], [1, 1.0]]
        commits = [when for method, path, _, when in sync_server.calls
                   if method == "POST" and path == f"/v2/runs/{writer.run_id}/commit"]
        assert len(commits) >= 2
        assert commits[1] - commits[0] >= interval * 0.9
    finally:
        writer.close()
        stop_process(proc)
        thread.join(timeout=2)


def test_synced_gc_and_object_grace(cli, tmp_path, sync_server):
    from agenticml.store import put_bytes
    root = tmp_path / "gc-store"
    writer = RunWriter.create(root, meta={"project": "p", "name": "synced"})
    writer.write_scalars([(0, {"loss": 1})])
    writer.finish("finished")
    cli("sync", writer.run_id, "--dir", root, "--host", sync_server.host,
        env={"AGENTICML_API_KEY": sync_server.key})
    old, fresh = put_bytes(root, b"old orphan"), put_bytes(root, b"fresh orphan")
    old_path = root / "objects" / old[:2] / old
    fresh_path = root / "objects" / fresh[:2] / fresh
    os.utime(old_path, (0, 0))
    assert json.loads(cli("du", "--dir", root, "--json").stdout)["runs"][0]["synced"] is True
    cli("gc", "--dir", root, "-y")
    assert list_runs(root) == [] and not old_path.exists() and fresh_path.is_file()


def test_sync_prune_overview_and_resume_refusal(cli, tmp_path, sync_server):
    from agenticml.store import OVERVIEW_MIN_POINTS
    root = tmp_path / "pruned-store"
    writer = RunWriter.create(root, run_id="large-run", meta={"project": "p", "name": "large"})
    count = OVERVIEW_MIN_POINTS + 1
    writer.write_scalars([(i, {"large": float(i)}) for i in range(count)])
    writer.write_scalars([(i, {"small": 1.0}) for i in range(10)])
    writer.finish("finished")
    scalar_dir = root / "runs" / writer.run_id / "scalars"
    index = json.loads((scalar_dir / "index.json").read_text())["series"]
    large_id, small_id = index["large"]["id"], index["small"]["id"]
    assert (scalar_dir / f"{large_id}.ovw").is_file()
    assert not (scalar_dir / f"{small_id}.ovw").exists()
    options = ["--dir", root, "--host", sync_server.host]
    env = {"AGENTICML_API_KEY": sync_server.key}
    remote_raw = sync_server.store / "runs" / writer.run_id / "scalars" / f"{large_id}.bin"
    raw_bytes = (scalar_dir / f"{large_id}.bin").read_bytes()
    cli("sync", writer.run_id, *options, "--prune", env=env)
    assert remote_raw.read_bytes() == raw_bytes
    assert not (scalar_dir / f"{large_id}.bin").exists()
    assert (scalar_dir / f"{small_id}.bin").is_file()
    rr = RunReader(root, writer.run_id)
    assert rr.series("large")["mode"] == "overview" and rr.series("large")["total"] == count
    assert rr.series("small")["mode"] == "raw"
    with pytest.raises(ValueError, match="pruned after sync"):
        RunWriter.resume(root, writer.run_id, must=True)
    assert "up to date" in cli("sync", writer.run_id, *options, env=env).stdout
    cli("sync", writer.run_id, *options, "--reset", env=env)
    result = cli("sync", writer.run_id, *options, env=env, check=False)
    # Task B preserves pruned_hosts on reset, allowing this host to recover.
    assert result.returncode == 0, result.stdout + result.stderr
    assert remote_raw.read_bytes() == raw_bytes
    assert not (scalar_dir / f"{large_id}.bin").exists()


def test_config_xdg_fallback_and_insecure_opt_in(cli, monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTICML_CONFIG_DIR")
    xdg = tmp_path / "xdg-credentials"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    result = cli("login", "--host", "http://example.invalid", "--insecure", "--no-verify", input="private-key\n")
    assert result.returncode == 0
    assert (xdg / "agenticml" / "credentials.json").is_file()
    assert "http://example.invalid" in cli("whoami", "--offline", "--insecure").stdout
    cli("logout")
    assert json.loads((xdg / "agenticml" / "credentials.json").read_text())["hosts"] == {}

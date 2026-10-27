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
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from agenticml.store import RunReader, RunWriter, list_runs
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
    result = cli("sync", run_id, "--dir", copied_store, "--dry-run")
    assert "dry-run (would send)" in result.stdout and "points=" in result.stdout
    assert not list(copied_store.rglob("sync.json"))
    result = cli("sync", run_id, "--dir", copied_store, check=False)
    assert result.returncode == 1 and "not logged in" in result.stderr
    assert not list(copied_store.rglob("sync.json"))


@pytest.fixture
def fake_server():
    calls = []
    call_times = []
    accepted_keys = {"testbed-key"}
    remote_count = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            nonlocal remote_count
            path = urlsplit(self.path).path
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.loads(raw) if raw and "application/json" in self.headers.get("Content-Type", "") else raw
            calls.append((self.command, path, payload, dict(self.headers)))
            call_times.append(time.monotonic())
            status, result = 200, {}
            if path.startswith("/upload/") and self.command == "PUT":
                pass
            elif self.headers.get("x-api-key") not in accepted_keys:
                status, result = 401, {"detail": "invalid key"}
            elif self.command == "GET" and path == "/runs":
                result = {"runs": []}
            elif path == "/init":
                remote_count += 1
                result = {"run_id": f"remote-{remote_count}"}
            elif path == "/artifacts/exists":
                result = {"existing": []}
            elif re.fullmatch(r"/runs/remote-\d+/artifacts/presign-upload", path):
                result = {"uploads": [{"path": f["path"], "url": host + "/upload/" + f["sha256"]}
                                      for f in payload["files"]]}
            elif path == "/log/batch" or re.fullmatch(
                r"/runs/remote-\d+/(config|finalize|log_media|artifacts/register|artifacts/register-version)", path
            ):
                pass
            else:
                status, result = 404, {"detail": "unimplemented fake endpoint"}
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(host=host, calls=calls, call_times=call_times, accepted_keys=accepted_keys)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_verified_login_and_online_whoami(cli, fake_server):
    assert "Logged in" in cli("login", "--host", fake_server.host, input="testbed-key\n").stdout
    assert "valid" in cli("whoami").stdout
    assert [path for method, path, _, _ in fake_server.calls] == ["/runs", "/runs"]
    assert cli("login", "--host", fake_server.host, input="wrong-key\n", check=False).returncode == 1
    assert "testbed-key" not in cli("whoami").stdout


def test_credentials_stdin_env_file_precedence_and_host_logout(cli, fake_server, tmp_path):
    file_key, env_key = "file-testbed-key", "environment-testbed-key"
    fake_server.accepted_keys.update({file_key, env_key})
    options = ["--host", fake_server.host]
    cli("login", *options, input=file_key + "\n", env={"AGENTICML_API_KEY": env_key})
    path = Path(os.environ["AGENTICML_CONFIG_DIR"]) / "credentials.json"
    assert json.loads(path.read_text())["hosts"][fake_server.host]["api_key"] == file_key
    assert fake_server.calls[-1][3]["x-api-key"] == file_key

    before = len(fake_server.calls)
    result = cli("whoami", *options, env={"AGENTICML_API_KEY": env_key})
    assert "source=env" in result.stdout and "valid" in result.stdout
    assert [headers["x-api-key"] for _, _, _, headers in fake_server.calls[before:]] == [env_key]

    root = tmp_path / "credential-sync-store"
    for name, env, expected in (("env", {"AGENTICML_API_KEY": env_key}, env_key), ("file", {}, file_key)):
        writer = RunWriter.create(root, meta={"project": "credentials", "name": name})
        writer.write_scalars([(0, {"loss": 1})])
        writer.finish("finished")
        before = len(fake_server.calls)
        cli("sync", writer.run_id, "--dir", root, *options, env=env)
        calls = fake_server.calls[before:]
        assert any(url.endswith("/finalize") for _, url, _, _ in calls)
        assert all(headers["x-api-key"] == expected for _, _, _, headers in calls)

    other_host = "https://second.example.invalid"
    cli("login", "--host", other_host, "--no-verify", input="second-host-key\n")
    cli("logout", *options)
    assert json.loads(path.read_text())["hosts"] == {other_host: {"api_key": "second-host-key"}}
    assert "source=file" in cli("whoami", "--host", other_host, "--offline").stdout


def test_full_incremental_sync_reset_and_all(cli, copied_store, fake_server):
    row = next(r for r in list_runs(copied_store) if r["name"] == "explicit-baseline")
    run_id = row["run_id"]
    options = ["--dir", copied_store, "--host", fake_server.host]
    env = {"AGENTICML_API_KEY": "testbed-key"}
    result = cli("sync", run_id, *options, env=env)
    assert "failed" not in result.stdout
    path = copied_store / "runs" / run_id / "sync.json"
    state = json.loads(path.read_text())["hosts"][fake_server.host]
    assert state["complete"] and state["finalized"]
    assert state["remote_run_id"] == "remote-1"
    assert state["offsets"]["train/loss"] == 32 * 8
    assert state["media"] and state["artifacts"] and state["code"]
    init = next(body for method, url, body, _ in fake_server.calls if url == "/init")
    assert init["project"] == "testbed" and init["name"] == "explicit-baseline"
    assert init["config"]["lr"] == 0.15 and init["tags"] == row["tags"]
    finalize = next(body for _, url, body, _ in fake_server.calls if url.endswith("/finalize"))
    assert finalize["status"] == "finished" and finalize["summary"]["final_loss"] < 0.2
    puts = [call for call in fake_server.calls if call[0] == "PUT"]
    assert puts and all("x-api-key" not in headers for _, _, _, headers in puts)
    before = len(fake_server.calls)
    assert "up to date" in cli("sync", run_id, *options, env=env).stdout
    assert len(fake_server.calls) == before
    writer = RunWriter.resume(copied_store, run_id, must=True)
    writer.write_scalars([(40, {"train/loss": 0.01})])
    writer.finish("finished")
    cli("sync", run_id, *options, env=env)
    batches = [body for _, url, body, _ in fake_server.calls[before:] if url == "/log/batch"]
    assert batches == [{"items": [{"run_id": "remote-1", "step": 40,
                                    "metrics": {"train/loss": pytest.approx(0.01)}}]}]
    cli("sync", "--all", *options, env=env)
    assert all(json.loads((copied_store / "runs" / r["run_id"] / "sync.json").read_text())
               ["hosts"][fake_server.host]["complete"] for r in list_runs(copied_store))
    assert len([c for c in fake_server.calls if c[1] == "/init"]) == 5
    before = len(fake_server.calls)
    cli("sync", run_id, "--reset", *options, env=env)
    assert fake_server.host not in json.loads(path.read_text())["hosts"]
    assert len(fake_server.calls) == before
    cli("sync", run_id, *options, env=env)
    assert len([c for c in fake_server.calls if c[1] == "/init"]) == 6
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
def test_dead_process_status_and_sync_finalization(cli, tmp_path, fake_server):
    root = tmp_path / "dead-store"
    writer = RunWriter.create(root, run_id="abandoned", meta={"project": "p", "name": "dead"})
    writer.write_scalars([(0, {"loss": 1})])
    writer.close()  # Simulates process death: persisted meta remains running.
    lock_path = root / "runs" / "abandoned" / "run.lock"
    now = time.time()
    os.utime(lock_path, (now, now))
    assert list_runs(root)[0]["effective_status"] == "running"
    cli("sync", "abandoned", "--dir", root, "--host", fake_server.host, env={"AGENTICML_API_KEY": "testbed-key"})
    assert list_runs(root)[0]["effective_status"] == "running"
    assert not any(path.endswith("/finalize") for _, path, _, _ in fake_server.calls)
    state_path = root / "runs" / "abandoned" / "sync.json"
    state = json.loads(state_path.read_text())["hosts"][fake_server.host]
    assert state["finalized"] is False and state["complete"] is False
    os.utime(root / "runs" / "abandoned" / "run.lock", (0, 0))
    rows = json.loads(cli("ls", "--json", "--dir", root).stdout)
    assert rows[0]["status"] == "running" and rows[0]["effective_status"] == "crashed"
    cli("sync", "abandoned", "--dir", root, "--host", fake_server.host, env={"AGENTICML_API_KEY": "testbed-key"})
    rr = RunReader(root, "abandoned")
    # Sync finalizes the remote run; local metadata remains resumable.
    assert rr.meta()["status"] == "running"
    assert list_runs(root)[0]["effective_status"] == "crashed"
    final = next(body for _, path, body, _ in fake_server.calls if path.endswith("/finalize"))
    assert final["status"] == "crashed" and final["summary"]["_exit_reason"] == "process died"


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


def test_sync_watch_repeats_after_delay_and_completes(tmp_path, fake_server):
    root = tmp_path / "watch-store"
    writer = RunWriter.create(root, meta={"project": "watch", "name": "live"})
    writer.write_scalars([(0, {"loss": 2})])
    interval = 0.4
    proc = subprocess.Popen(
        [str(CONSOLE), "sync", writer.run_id, "--dir", str(root), "--host", fake_server.host,
         "--watch", str(interval)], cwd=TESTBED,
        env=dict(os.environ, AGENTICML_API_KEY="testbed-key"),
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
        state = json.loads(state_path.read_text())["hosts"][fake_server.host]
        assert state["complete"] is False and state["finalized"] is False
        writer.write_scalars([(1, {"loss": 1})])
        writer.finish("finished")
        assert proc.wait(timeout=10) == 0
        thread.join(timeout=2)
        state = json.loads(state_path.read_text())["hosts"][fake_server.host]
        assert state["complete"] is True and state["finalized"] is True
        batches = [(body, when) for (_, path, body, _), when in zip(fake_server.calls, fake_server.call_times)
                   if path == "/log/batch"]
        assert len(batches) == 2
        assert batches[0][0]["items"][0]["step"] == 0
        assert batches[1][0]["items"][0]["step"] == 1
        assert batches[1][1] - batches[0][1] >= interval * 0.9
        assert sum(path.endswith("/finalize") for _, path, _, _ in fake_server.calls) == 1
    finally:
        writer.close()
        stop_process(proc)
        thread.join(timeout=2)


def test_synced_gc_and_object_grace(cli, tmp_path, fake_server):
    from agenticml.store import put_bytes
    root = tmp_path / "gc-store"
    writer = RunWriter.create(root, meta={"project": "p", "name": "synced"})
    writer.write_scalars([(0, {"loss": 1})])
    writer.finish("finished")
    cli("sync", writer.run_id, "--dir", root, "--host", fake_server.host,
        env={"AGENTICML_API_KEY": "testbed-key"})
    old, fresh = put_bytes(root, b"old orphan"), put_bytes(root, b"fresh orphan")
    old_path = root / "objects" / old[:2] / old
    fresh_path = root / "objects" / fresh[:2] / fresh
    os.utime(old_path, (0, 0))
    assert json.loads(cli("du", "--dir", root, "--json").stdout)["runs"][0]["synced"] is True
    cli("gc", "--dir", root, "-y")
    assert list_runs(root) == [] and not old_path.exists() and fresh_path.is_file()


def test_sync_prune_overview_and_resume_refusal(cli, tmp_path, fake_server):
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
    options = ["--dir", root, "--host", fake_server.host]
    env = {"AGENTICML_API_KEY": "testbed-key"}
    cli("sync", writer.run_id, *options, "--prune", env=env)
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
    assert result.returncode == 1 and "pruned" in result.stdout


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

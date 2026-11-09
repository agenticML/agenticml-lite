"""Client support window (D4): old agenticml-lite clients get 426 on /v2."""
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading

import pytest

from agenticml import cli, server
from agenticml.server import clientwindow
from agenticml.server.app import make_server

TEXT = "this server needs agenticml-lite >= 2.2; run: pip install -U agenticml-lite"
ROOT = Path(__file__).resolve().parents[1]


def _start(data, **kwargs):
    httpd = make_server(data, port=0, **kwargs)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    _, httpd.test_key = httpd.keys.create("one")
    return httpd, thread


@pytest.fixture
def start(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    started = []

    def make(**kwargs):
        pair = _start(tmp_path / f"data{len(started)}", **kwargs)
        started.append(pair)
        return pair[0]
    yield make
    for httpd, thread in started:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def call(httpd, path, agent=None, method="GET", body=None, key=True):
    headers = {"Authorization": "Bearer " + httpd.test_key} if key else {}
    if agent is not None:
        headers["User-Agent"] = agent
    conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        return response.status, json.loads(payload) if payload else None
    finally:
        conn.close()


@pytest.mark.parametrize("agent", ["agenticml-lite/2.0.0", "agenticml-lite/2.1.3", "agenticml-lite/1.0.0",
                                   "agenticml-lite/2.1", "agenticml-lite/0.9.9rc1", "agenticml-lite/1.10.0"])
def test_old_clients_get_426_with_the_text(start, agent):
    httpd = start()
    for path, method, body in (("/v2/runs/run-1/state", "GET", None),
                               ("/v2/objects/missing", "POST", b'{"objects": []}'),
                               ("/v2/runs/run-1/commit", "POST", b"{}")):
        status, payload = call(httpd, path, agent, method, body)
        assert (status, payload) == (426, {"error": TEXT}), (path, status, payload)


def test_old_client_is_refused_before_authentication(start):
    httpd = start()
    assert call(httpd, "/v2/runs/run-1/state", "agenticml-lite/2.0.0", key=False)[0] == 426
    assert call(httpd, "/v2/runs/run-1/state", "agenticml-lite/3.0.0", key=False)[0] == 401


@pytest.mark.parametrize("agent", ["agenticml-lite/2.2.0", "agenticml-lite/2.2", "agenticml-lite/2.10.0",
                                   "agenticml-lite/3.0.0", "agenticml-lite/3.0.0.dev1", "agenticml-lite/12.0.0",
                                   None, "curl/8.5.0", "python-requests/2.31", "Mozilla/5.0",
                                   "agenticml-lite", "agenticml-lite/", "agenticml-lite/dev",
                                   "my-agenticml-lite/1.0.0", ""])
def test_current_missing_and_foreign_clients_are_accepted(start, agent):
    httpd = start()
    assert call(httpd, "/v2/whoami", agent)[0] == 200
    status, payload = call(httpd, "/v2/runs/run-1/state", agent)
    assert status == 404, (agent, status, payload)  # authenticated, run unknown


def test_whoami_and_healthz_are_not_blocked_for_old_clients(start):
    # 2.x clients turn any 4xx on whoami into "not an agenticml server with
    # protocol 2", so the 426 waits for the first call that shows its text.
    httpd = start()
    status, payload = call(httpd, "/v2/whoami", "agenticml-lite/2.0.0")
    assert status == 200 and payload["protocol"] == 2
    assert call(httpd, "/healthz", "agenticml-lite/1.0.0", key=False) == (200, {"ok": True})


def test_flag_raises_the_bar(start):
    httpd = start(min_client_version="3.1")
    text = TEXT.replace("2.2", "3.1")
    assert call(httpd, "/v2/runs/run-1/state", "agenticml-lite/3.0.0") == (426, {"error": text})
    assert call(httpd, "/v2/runs/run-1/state", "agenticml-lite/2.2.0") == (426, {"error": text})
    assert call(httpd, "/v2/runs/run-1/state", "agenticml-lite/3.1.0")[0] == 404
    assert call(httpd, "/healthz", "agenticml-lite/2.2.0", key=False)[0] == 200


def test_flag_lowers_the_bar(start):
    httpd = start(min_client_version="1.0")
    assert call(httpd, "/v2/runs/run-1/state", "agenticml-lite/1.0.0")[0] == 404
    assert call(httpd, "/v2/runs/run-1/state", "agenticml-lite/2.0.0")[0] == 404
    assert call(httpd, "/v2/runs/run-1/state", "agenticml-lite/0.9.0")[0] == 426


def test_dashboard_routes_ignore_the_user_agent(start):
    httpd = start(min_client_version="99.0")
    conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=5)
    try:
        conn.request("GET", "/login", headers={"User-Agent": "agenticml-lite/1.0.0"})
        assert conn.getresponse().status != 426
    finally:
        conn.close()


def test_parse_min_client():
    assert clientwindow.parse_min_client(None) == (2, 2)
    assert clientwindow.parse_min_client("2.2") == (2, 2)
    assert clientwindow.parse_min_client(" 3.10.4 ") == (3, 10)
    for bad in ("2", "x.y", "2.2.", "v2.2", "2.2-rc1", "", "2.2.1.1", "-1.0"):
        with pytest.raises(ValueError, match="--min-client-version"):
            clientwindow.parse_min_client(bad)


def test_make_server_rejects_a_bad_minimum(tmp_path):
    with pytest.raises(ValueError, match="--min-client-version"):
        make_server(tmp_path / "data", port=0, min_client_version="two")


@pytest.mark.parametrize("env, flag, expected", [
    (None, None, None), ("", None, None), ("2.3", None, "2.3"),
    (None, "3.0", "3.0"), ("2.3", "3.0", "3.0")])
def test_serve_min_client_version_flag_and_env(monkeypatch, env, flag, expected):
    if env is None:
        monkeypatch.delenv("AGENTICML_SERVER_MIN_CLIENT_VERSION", raising=False)
    else:
        monkeypatch.setenv("AGENTICML_SERVER_MIN_CLIENT_VERSION", env)
    calls = []
    monkeypatch.setattr(server, "serve", lambda *args, **kwargs: calls.append(kwargs))
    args = ["server", "serve", "--data", "/data"]
    if flag is not None:
        args += ["--min-client-version", flag]
    assert cli.main(args) == 0
    assert calls[0].get("min_client_version") == expected


def test_serve_bad_minimum_exits_1(tmp_path, capsys):
    assert cli.main(["server", "serve", "--data", str(tmp_path), "--port", "0",
                     "--min-client-version", "abc"]) == 1
    assert "--min-client-version must look like 2.2" in capsys.readouterr().err


def test_serve_help_names_the_flag(capsys):
    with pytest.raises(SystemExit):
        cli.main(["server", "serve", "--help"])
    out = capsys.readouterr().out
    assert "--min-client-version" in out and "AGENTICML_SERVER_MIN_CLIENT_VERSION" in out


# --- real clients -----------------------------------------------------------------

def _agent_env(tmp_path, source=None):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AGENTICML_") and key != "PYTHONPATH"}
    env.update(AGENTICML_DIR=str(tmp_path / "local"), AGENTICML_CONFIG_DIR=str(tmp_path / "config"),
               HOME=str(tmp_path / "home"))
    if source is not None:
        env["PYTHONPATH"] = str(source)
    else:
        env["PYTHONPATH"] = str(ROOT / "src")
    return env


def _client_source(tmp_path):
    """The agenticml package of release 2.2.0, exported from git (no network)."""
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    for ref in ("v2.2.0", "bd0d499"):  # the tag once it exists, else the last 2.2.0 commit
        probe = subprocess.run([git, "-C", str(ROOT), "cat-file", "-e", f"{ref}:src/agenticml/client.py"],
                               capture_output=True)
        if probe.returncode == 0:
            break
    else:
        pytest.skip("the 2.2.0 release is not in this clone (shallow checkout?)")
    archive = subprocess.run([git, "-C", str(ROOT), "archive", "--format=tar", ref, "src/agenticml"],
                             capture_output=True, check=True).stdout
    target = tmp_path / "client-2.2.0"
    target.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(target, filter="data")
        else:  # older 3.10 patch releases; the archive is our own repository
            tar.extractall(target)
    return target / "src"


def _run_old(source, tmp_path, *args, stdin=None, code=None):
    return subprocess.run([sys.executable, "-I", "-c", code] if code else [sys.executable, "-m", "agenticml", *args],
                          input=stdin, capture_output=True, text=True, timeout=120,
                          env=_agent_env(tmp_path, source), cwd=tmp_path)


def _old_client_sync(start, tmp_path, **server_kwargs):
    source = _client_source(tmp_path)
    httpd = start(**server_kwargs)
    host = "http://%s:%s" % httpd.server_address[:2]
    out = subprocess.run(
        [sys.executable, "-c", "import agenticml; print(agenticml.__version__)"],
        capture_output=True, text=True, env=_agent_env(tmp_path, source), cwd=tmp_path)
    assert out.stdout.strip() == "2.2.0", out
    code = ("import agenticml\n"
            "run = agenticml.init(project='compat', name='old client', system_metrics=False,\n"
            "                     capture_env=False, auto_track=False, lineage=False)\n"
            "for step in range(3):\n"
            "    run.log({'loss': 1.0 / (step + 1)}, step=step)\n"
            "run.finish()\n"
            "print(run.run_id)\n")
    made = _run_old(source, tmp_path, code=code)
    assert made.returncode == 0, made.stderr
    run_id = made.stdout.strip().splitlines()[-1]
    login = _run_old(source, tmp_path, "login", "--host", host, stdin=httpd.test_key + "\n")
    assert login.returncode == 0, login.stderr
    return httpd, run_id, _run_old(source, tmp_path, "sync", "--all")


def test_release_2_2_client_syncs_a_run_to_a_3_0_server(start, tmp_path):
    httpd, run_id, synced = _old_client_sync(start, tmp_path)
    assert synced.returncode == 0, synced.stdout + synced.stderr
    assert run_id in synced.stdout and "synced" in synced.stdout
    status, state = call(httpd, f"/v2/runs/{run_id}/state", "agenticml-lite/3.0.0")
    assert status == 200 and state["files"]["meta.json"]["size"] > 0


def test_release_2_2_client_shows_the_upgrade_text_when_the_bar_is_raised(start, tmp_path):
    httpd, run_id, synced = _old_client_sync(start, tmp_path, min_client_version="2.3")
    assert synced.returncode != 0
    assert "HTTP 426: this server needs agenticml-lite >= 2.3; run: pip install -U agenticml-lite" in (
        synced.stdout + synced.stderr)
    assert call(httpd, f"/v2/runs/{run_id}/state", "agenticml-lite/3.0.0")[0] == 404

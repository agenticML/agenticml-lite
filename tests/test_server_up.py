"""agenticml server up: Tailscale checks, key bootstrap, Funnel, --install."""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import plistlib
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

import pytest

from agenticml import cli
from agenticml.server import app
from agenticml.server.keys import KeyStore
from agenticml.server import up as up_mod
from agenticml.server.users import UserStore
from test_server_users import write_v1_keys
from agenticml.server.up import (
    UpError, _ADMIN_NAME_NOT_ADMIN, _ENV_KEY_NOT_INHERITED, _INSTALL_NEEDS_PORT,
    _NO_ADMIN_TTY, _NO_DNS, _NO_FUNNEL, _NO_HTTPS, _NOT_INSTALLED, _NOT_RUNNING,
    _PORT_443_TAKEN, _SETUP_CLOSED, _SYSTEMD_CONTROL, _systemd_quote,
    write_launchd_plist,
)

pytest.importorskip("fcntl")

FAKE = r'''
import json, os, sys, time
from pathlib import Path

log = Path(os.environ["FAKE_TAILSCALE_LOG"])
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")

args = sys.argv[1:]
if args[:1] == ["status"]:
    path = os.environ.get("FAKE_TAILSCALE_STATUS")
    sys.stdout.write(Path(path).read_text(encoding="utf-8") if path else
                     os.environ.get("FAKE_TAILSCALE_STATUS_JSON", "{}"))
    err = os.environ.get("FAKE_TAILSCALE_STATUS_STDERR", "")
    if err:
        sys.stderr.write(err if err.endswith("\n") else err + "\n")
    sys.exit(int(os.environ.get("FAKE_TAILSCALE_STATUS_EXIT", "0")))
if args[:1] == ["funnel"] and "status" in args:
    path = os.environ.get("FAKE_TAILSCALE_FUNNEL_STATUS")
    sys.stdout.write(Path(path).read_text(encoding="utf-8") if path else
                     os.environ.get("FAKE_TAILSCALE_FUNNEL_STATUS_JSON", "{}"))
    err = os.environ.get("FAKE_TAILSCALE_FUNNEL_STATUS_STDERR", "")
    if err:
        sys.stderr.write(err if err.endswith("\n") else err + "\n")
    sys.exit(int(os.environ.get("FAKE_TAILSCALE_FUNNEL_STATUS_EXIT", "0")))
if args[:1] == ["funnel"]:
    kind = "OFF" if "off" in args else "ON"
    if kind == "ON":
        delay = float(os.environ.get("FAKE_TAILSCALE_FUNNEL_ON_SLEEP", "0") or 0)
        if delay:
            time.sleep(delay)
    err = os.environ.get("FAKE_TAILSCALE_FUNNEL_%s_STDERR" % kind, "")
    if err:
        sys.stderr.write(err if err.endswith("\n") else err + "\n")
    sys.exit(int(os.environ.get("FAKE_TAILSCALE_FUNNEL_%s_EXIT" % kind, "0")))
sys.stderr.write("unexpected %s\n" % " ".join(args))
sys.exit(2)
'''


def _self(dns="machine.tailnet.ts.net.", caps=("https", "funnel")):
    return {
        "DNSName": dns,
        "CapMap": {name: None for name in caps},
        "Capabilities": list(caps),
    }


READY = {
    "BackendState": "Running",
    "Self": _self(),
    "CertDomains": ["machine.tailnet.ts.net"],
}

PUBLIC_URL = "https://machine.tailnet.ts.net"


def install_fake(tmp_path, monkeypatch, status=None, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "tailscale"
    script.write_text(f"#!{sys.executable}\n{FAKE}")
    script.chmod(0o755)
    log = tmp_path / "tailscale.log"
    log.write_text("")
    payload = READY if status is None else status
    status_path = tmp_path / "status.json"
    status_path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_TAILSCALE_LOG", str(log))
    monkeypatch.setenv("FAKE_TAILSCALE_STATUS", str(status_path))
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    monkeypatch.setattr(up_mod, "_stdout_is_tty", lambda: True)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    found = shutil.which("tailscale")
    assert found is not None and Path(found).resolve() == script.resolve(), found
    return log


def invocations(log):
    return [json.loads(line) for line in log.read_text().splitlines() if line]


def funnel_calls(log):
    return [row for row in invocations(log) if row[:1] == ["funnel"]]


def funnel_toggles(log):
    return [row for row in funnel_calls(log) if row[1:2] != ["status"]]


def other_443(proxy="http://127.0.0.1:9"):
    return {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "machine.tailnet.ts.net:443": {"Handlers": {"/": {"Proxy": proxy}}},
        },
    }


def run_up(data, *flags):
    return cli.main(["server", "up", "--data", str(data), *flags])


def assert_prereq_failed(data, log, capsys, message):
    assert run_up(data) == 1
    err = capsys.readouterr().err
    assert err.splitlines()[0] == message
    assert not data.exists()
    assert funnel_calls(log) == []


@contextmanager
def running_up(tmp_path, data, extra=None, port=0, wait_for="live", ignore_sighup=False):
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("AGENTICML_SERVER_KEY", None)
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["PATH"] = str(tmp_path / "bin") + os.pathsep + env.get("PATH", "")
    if extra:
        env.update(extra)
    ignore = "signal.signal(signal.SIGHUP, signal.SIG_IGN); " if ignore_sighup else ""
    entry = (
        "import signal; "
        "signal.signal(signal.SIGINT, signal.default_int_handler); "
        + ignore +
        "import agenticml.server.up as _up; _up._stdout_is_tty = lambda: True; "
        "from agenticml.cli import main; raise SystemExit(main())"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", entry, "server", "up",
         "--data", str(data), "--port", str(port)],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    lines = []
    local = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                rest = process.stdout.read()
                raise AssertionError(
                    f"server up exited {process.returncode}: {''.join(lines)}{rest}")
            if not select.select([process.stdout], [], [], 0.1)[0]:
                continue
            line = process.stdout.readline()
            if not line:
                continue
            lines.append(line)
            if line.startswith("AgenticML server: http://127.0.0.1:"):
                local = urlsplit(line.split()[2])
                if wait_for == "server":
                    yield process, local, lines
                    return
            if wait_for == "live" and line.startswith("AgenticML is live at "):
                yield process, local, lines
                return
        raise AssertionError("timeout waiting for live: " + "".join(lines))
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_not_installed(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("AGENTICML_SERVER_KEY", raising=False)
    assert shutil.which("tailscale") is None
    data = tmp_path / "data"
    assert run_up(data) == 1
    assert capsys.readouterr().err.splitlines()[0] == _NOT_INSTALLED
    assert not data.exists()


@pytest.mark.parametrize("status, message", [
    ({"BackendState": "NeedsLogin"}, f"{_NOT_RUNNING} (NeedsLogin)"),
    ({"BackendState": "Stopped"}, f"{_NOT_RUNNING} (Stopped)"),
    ({"BackendState": "Running", "Self": _self(dns=""),
      "CertDomains": ["x"]}, _NO_DNS),
    ({"BackendState": "Running", "Self": _self(caps=("funnel",)),
      "CertDomains": []}, _NO_HTTPS),
    ({"BackendState": "Running", "Self": _self(caps=("https",)),
      "CertDomains": ["machine.tailnet.ts.net"]}, _NO_FUNNEL),
    ("{not-json", f"{_NOT_RUNNING} (unknown)"),
])
def test_failed_prerequisites(tmp_path, monkeypatch, capsys, status, message):
    log = install_fake(tmp_path, monkeypatch, status=status)
    data = tmp_path / "data"
    assert_prereq_failed(data, log, capsys, message)
    assert invocations(log) == [["status", "--json"]]


def test_happy_path_and_second_start(tmp_path, monkeypatch):
    log = install_fake(tmp_path, monkeypatch)
    data = tmp_path / "data"
    with running_up(tmp_path, data) as (process, local, lines):
        text = "".join(lines)
        assert f"AgenticML is live at {PUBLIC_URL}" in text
        assert "/setup?token=" not in text
        invite = [line.strip() for line in lines if "/invite?token=" in line]
        assert len(invite) == 1
        assert any("72 hours" in line for line in lines)
        conn = http.client.HTTPConnection(local.hostname, local.port, timeout=3)
        try:
            conn.request("GET", "/healthz")
            assert json.loads(conn.getresponse().read()) == {"ok": True}
        finally:
            conn.close()
        assert funnel_toggles(log) == [
            ["funnel", "--bg", "--https=443", f"http://127.0.0.1:{local.port}"],
        ]
        process.send_signal(signal.SIGTERM)
        rest, _ = process.communicate(timeout=10)
        output = text + rest
        assert process.returncode == 0, output
        assert "stopped" in output
        assert "/setup?token=" not in output
        assert output.count(invite[0]) == 1
    assert ["funnel", "status", "--json"] in invocations(log)
    assert funnel_toggles(log) == [
        ["funnel", "--bg", "--https=443", f"http://127.0.0.1:{local.port}"],
        ["funnel", "--https=443", "off"],
    ]
    assert all("reset" not in row for row in invocations(log))
    users = json.loads((data / "server" / "users.json").read_text())
    assert users["users"][0]["name"] == "admin" and users["users"][0]["role"] == "admin"
    assert users["users"][0]["created_by"] == "server up"
    assert users["users"][0]["invite"] is not None
    log.write_text("")
    with running_up(tmp_path, data) as (process, _local, lines):
        assert not any("/invite?token=" in line for line in lines)
        process.send_signal(signal.SIGTERM)
        rest, _ = process.communicate(timeout=10)
        assert process.returncode == 0, rest
        assert "/setup?token=" not in "".join(lines) + rest
    assert json.loads((data / "server" / "users.json").read_text()) == users


def test_funnel_on_failure_still_turns_off(tmp_path, monkeypatch, capsys):
    log = install_fake(
        tmp_path, monkeypatch,
        FAKE_TAILSCALE_FUNNEL_ON_EXIT=1,
        FAKE_TAILSCALE_FUNNEL_ON_STDERR="funnel denied\nand more",
    )
    data = tmp_path / "data"
    assert run_up(data, "--port", "0") == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines()[0] == "funnel denied"
    assert "stopped" not in captured.out
    assert funnel_toggles(log)[0][:3] == ["funnel", "--bg", "--https=443"]
    assert funnel_toggles(log)[-1] == ["funnel", "--https=443", "off"]


def test_funnel_off_failure_warns_and_exits_0(tmp_path, monkeypatch):
    log = install_fake(
        tmp_path, monkeypatch,
        FAKE_TAILSCALE_FUNNEL_OFF_EXIT=1,
        FAKE_TAILSCALE_FUNNEL_OFF_STDERR="off failed",
    )
    data = tmp_path / "data"
    with running_up(tmp_path, data) as (process, _local, lines):
        process.send_signal(signal.SIGTERM)
        rest, _ = process.communicate(timeout=10)
        output = "".join(lines) + rest
        assert process.returncode == 0, output
        assert "warning:" in output
        assert "tailscale funnel --https=443 off" in output
    assert funnel_toggles(log)[-1] == ["funnel", "--https=443", "off"]


def test_locked_data_dir_skips_funnel(tmp_path, monkeypatch, capsys):
    log = install_fake(tmp_path, monkeypatch)
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    KeyStore(data / "server").create("admin", "admin")
    with app.make_server(data, port=0):
        assert run_up(data, "--port", "0") == 1
    err = capsys.readouterr().err
    assert "another agenticml server is using" in err
    assert funnel_calls(log) == []
    assert invocations(log) == [["status", "--json"]]


def test_install_linux_quotes_spaces_and_does_not_serve(tmp_path, monkeypatch, capsys):
    log = install_fake(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    python = tmp_path / "py thon" / "python"
    monkeypatch.setattr(sys, "executable", str(python))
    data = tmp_path / "my data"
    assert run_up(data, "--port", "9090", "--install") == 0
    captured = capsys.readouterr()
    assert "systemctl --user daemon-reload && systemctl --user enable --now agenticml-server-up" in captured.out
    assert "loginctl enable-linger $USER" in captured.out
    assert "AgenticML is live" not in captured.out
    assert funnel_calls(log) == []
    unit = home / ".config/systemd/user/agenticml-server-up.service"
    text = unit.read_text()
    assert "# Managed by agenticml server up" in text
    assert f'ExecStart="{python}" -m agenticml server up --data "{data.resolve()}" --port 9090' in text
    assert "Restart=on-failure" in text
    assert "RestartSec=30" in text
    assert "WantedBy=default.target" in text
    assert "RestartPreventExitStatus" not in text
    assert stat.S_IMODE(unit.stat().st_mode) == 0o644
    foreign = tmp_path / "other" / ".config/systemd/user/agenticml-server-up.service"
    monkeypatch.setenv("HOME", str(tmp_path / "other"))
    foreign.parent.mkdir(parents=True)
    foreign.write_text("[Unit]\nDescription=nope\n")
    assert run_up(data, "--install") == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert foreign.read_text() == "[Unit]\nDescription=nope\n"
    assert run_up(data, "--install", "--force") == 0
    assert "# Managed by agenticml server up" in foreign.read_text()


def test_launchd_writer(tmp_path, monkeypatch, capsys):
    home = tmp_path / "mac-home"
    data = tmp_path / "data dir"
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    path = write_launchd_plist(data, 8080, home=home)
    payload = plistlib.loads(path.read_bytes())
    assert payload["Label"] == "xyz.agenticml.server-up"
    assert payload["ProgramArguments"] == [
        "/usr/bin/python3", "-m", "agenticml", "server", "up",
        "--data", str(data), "--port", "8080",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    log_path = str(home / "Library/Logs/agenticml-server-up.log")
    assert payload["StandardOutPath"] == log_path
    assert payload["StandardErrorPath"] == log_path
    assert capsys.readouterr().out.strip() == f"launchctl load -w {path}"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    path.write_bytes(plistlib.dumps({"Label": "other.job"}))
    with pytest.raises(UpError, match="refusing to overwrite"):
        write_launchd_plist(data, 8080, home=home)
    write_launchd_plist(data, 8080, home=home, force=True)
    assert plistlib.loads(path.read_bytes())["Label"] == "xyz.agenticml.server-up"


def test_env_key_skips_create(tmp_path, monkeypatch):
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTICML_SERVER_KEY", "a" * 32)
    data = tmp_path / "data"
    with running_up(tmp_path, data, extra={"AGENTICML_SERVER_KEY": "a" * 32}) as (
            process, local, lines):
        text = "".join(lines)
        assert not any(line.startswith("aml_") for line in lines)
        conn = http.client.HTTPConnection(local.hostname, local.port, timeout=3)
        try:
            conn.request("GET", "/healthz")
            assert json.loads(conn.getresponse().read()) == {"ok": True}
        finally:
            conn.close()
        process.send_signal(signal.SIGTERM)
        rest, _ = process.communicate(timeout=10)
        assert process.returncode == 0, text + rest
        assert "aml_" not in text + rest
    assert not (data / "server" / "keys.json").exists()


def test_existing_admin_user_skips_bootstrap(tmp_path, monkeypatch):
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    KeyStore(data / "server").create("admin", "sync")
    assert run_up(data, "--install") == 0
    names = {record.name: record for record in KeyStore(data / "server").list()}
    assert list(names) == ["admin"] and names["admin"].scope == "sync"
    users = json.loads((data / "server" / "users.json").read_text())
    assert users["users"][0]["name"] == "admin"


def test_non_admin_named_admin_refused(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setattr(up_mod, "_stdout_is_tty", lambda: True)
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    UserStore(data / "server").add("admin", role="member")
    assert run_up(data, "--install") == 1
    assert capsys.readouterr().err.splitlines()[0] == _ADMIN_NAME_NOT_ADMIN


def test_systemd_quote_percent_dollar_newline_space():
    assert _systemd_quote("/data/foo") == "/data/foo"
    assert _systemd_quote("/data/foo bar") == '"/data/foo bar"'
    assert _systemd_quote("/Users/o'brien/data") == '"/Users/o\'brien/data"'
    assert _systemd_quote("/data/100%done") == "/data/100%%done"
    assert _systemd_quote("/data/$HOME") == "/data/$$HOME"
    assert _systemd_quote("/data/foo\nbar") == '"/data/foo\\nbar"'
    assert _systemd_quote("/data/foo\tbar") == '"/data/foo\\tbar"'
    assert _systemd_quote("/data/% and $x") == '"/data/%% and $$x"'
    with pytest.raises(UpError, match=_SYSTEMD_CONTROL):
        _systemd_quote("/data/foo\x01bar")
    with pytest.raises(UpError, match=_SYSTEMD_CONTROL):
        _systemd_quote("/data/foo\x7fbar")


def test_install_quotes_percent_and_dollar(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    python = tmp_path / "py$thon" / "python"
    monkeypatch.setattr(sys, "executable", str(python))
    data = tmp_path / "data%dir"
    assert run_up(data, "--port", "9090", "--install") == 0
    text = (home / ".config/systemd/user/agenticml-server-up.service").read_text()
    escaped_python = str(python).replace("$", "$$")
    escaped_data = str(data.resolve()).replace("%", "%%")
    assert f"ExecStart={escaped_python} -m agenticml server up --data {escaped_data} --port 9090" in text


def test_no_tty_does_not_create_admin(tmp_path, monkeypatch, capsys):
    log = install_fake(tmp_path, monkeypatch)
    monkeypatch.setattr(up_mod, "_stdout_is_tty", lambda: False)
    data = tmp_path / "data"
    assert run_up(data, "--install") == 1
    assert capsys.readouterr().err.splitlines()[0] == _NO_ADMIN_TTY
    assert not (data / "server" / "keys.json").exists()
    assert funnel_calls(log) == []


def test_install_with_env_key_creates_file_admin(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTICML_SERVER_KEY", "a" * 32)
    data = tmp_path / "data"
    assert run_up(data, "--install") == 0
    out = capsys.readouterr().out
    assert out.splitlines().count(_ENV_KEY_NOT_INHERITED) == 1
    assert any("/invite?token=" in line for line in out.splitlines())
    users = json.loads((data / "server" / "users.json").read_text())
    assert users["users"][0]["name"] == "admin" and users["users"][0]["invite"] is not None


def test_install_migrates_v1_admin_and_does_not_create(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    write_v1_keys(data / "server" / "keys.json", ["admin"], scopes=["admin"])
    assert run_up(data, "--install") == 0
    out = capsys.readouterr().out
    assert not any(line.startswith("aml_") for line in out.splitlines())
    keys = KeyStore(data / "server")
    assert [record.name for record in keys.list()] == ["admin"]
    assert keys.list()[0].scope == "admin" and keys.list()[0].user == "admin"
    users = json.loads((data / "server" / "users.json").read_text())
    assert [row["name"] for row in users["users"]] == ["admin"]


def test_install_env_key_silent_when_file_admin_exists(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTICML_SERVER_KEY", "a" * 32)
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    KeyStore(data / "server").create("admin", "admin")
    assert run_up(data, "--install") == 0
    out = capsys.readouterr().out
    assert _ENV_KEY_NOT_INHERITED not in out
    assert not any(line.startswith("aml_") for line in out.splitlines())


def test_setup_closed_refuses_bootstrap(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    keys = KeyStore(data / "server")
    (data / "server" / "setup-closed").write_bytes(b"")
    assert not KeyStore(data / "server").has_admin()
    assert run_up(data, "--install") == 1
    assert capsys.readouterr().err.splitlines()[0] == _SETUP_CLOSED
    assert not (data / "server" / "users.json").exists() or json.loads(
        (data / "server" / "users.json").read_text()).get("users") in (None, [])


def test_existing_other_443_refused(tmp_path, monkeypatch, capsys):
    log = install_fake(
        tmp_path, monkeypatch,
        FAKE_TAILSCALE_FUNNEL_STATUS_JSON=json.dumps(other_443()),
    )
    data = tmp_path / "data"
    assert run_up(data, "--port", "0") == 1
    assert capsys.readouterr().err.splitlines()[0] == _PORT_443_TAKEN
    assert funnel_toggles(log) == []
    assert ["funnel", "status", "--json"] in invocations(log)


def test_stale_own_443_allowed(tmp_path, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = install_fake(
        tmp_path, monkeypatch,
        FAKE_TAILSCALE_FUNNEL_STATUS_JSON=json.dumps(
            other_443(f"http://127.0.0.1:{port}")),
    )
    data = tmp_path / "data"
    with running_up(tmp_path, data, port=port) as (process, local, lines):
        assert local.port == port
        assert funnel_toggles(log) == [
            ["funnel", "--bg", "--https=443", f"http://127.0.0.1:{port}"],
        ]
        process.send_signal(signal.SIGTERM)
        rest, _ = process.communicate(timeout=10)
        assert process.returncode == 0, "".join(lines) + rest
    assert funnel_toggles(log)[-1] == ["funnel", "--https=443", "off"]


def test_sighup_turns_funnel_off(tmp_path, monkeypatch):
    log = install_fake(tmp_path, monkeypatch)
    data = tmp_path / "data"
    with running_up(tmp_path, data) as (process, _local, lines):
        process.send_signal(signal.SIGHUP)
        rest, _ = process.communicate(timeout=10)
        output = "".join(lines) + rest
        assert process.returncode == 0, output
    assert funnel_toggles(log)[-1] == ["funnel", "--https=443", "off"]


def _sighup_ignored(pid):
    status = Path(f"/proc/{pid}/status").read_text()
    for line in status.splitlines():
        if line.startswith("SigIgn:"):
            return bool(int(line.split()[1], 16) & 1)
    raise AssertionError(f"SigIgn not found for pid {pid}")


def test_ignored_sighup_left_alone(tmp_path, monkeypatch):
    log = install_fake(tmp_path, monkeypatch)
    data = tmp_path / "data"
    with running_up(tmp_path, data, ignore_sighup=True) as (process, local, lines):
        assert _sighup_ignored(process.pid)
        conn = http.client.HTTPConnection(local.hostname, local.port, timeout=3)
        try:
            conn.request("GET", "/healthz")
            assert json.loads(conn.getresponse().read()) == {"ok": True}
        finally:
            conn.close()
        process.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.1)
        assert _sighup_ignored(process.pid)
        process.send_signal(signal.SIGTERM)
        rest, _ = process.communicate(timeout=10)
        output = "".join(lines) + rest
        assert process.returncode == 0, output
        assert "stopped" in output
    assert funnel_toggles(log)[-1] == ["funnel", "--https=443", "off"]


def test_install_port_0_rejected(tmp_path, monkeypatch, capsys):
    log = install_fake(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    data = tmp_path / "data"
    assert run_up(data, "--port", "0", "--install") == 1
    assert capsys.readouterr().err.splitlines()[0] == _INSTALL_NEEDS_PORT
    assert not (home / ".config/systemd/user/agenticml-server-up.service").exists()
    assert funnel_calls(log) == []


def test_sigterm_during_slow_funnel_on_still_offs(tmp_path, monkeypatch):
    log = install_fake(tmp_path, monkeypatch, FAKE_TAILSCALE_FUNNEL_ON_SLEEP="2")
    data = tmp_path / "data"
    with running_up(tmp_path, data, wait_for="server") as (process, _local, lines):
        process.send_signal(signal.SIGTERM)
        rest, _ = process.communicate(timeout=10)
        output = "".join(lines) + rest
        assert process.returncode == 0, output
    assert funnel_toggles(log)[-1] == ["funnel", "--https=443", "off"]


def test_port_out_of_range(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["server", "up", "--data", str(tmp_path / "data"), "--port", "65536"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["server", "up", "--data", str(tmp_path / "data"),
                  "--port", "65536", "--install"])
    assert exc.value.code == 2

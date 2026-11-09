"""agenticml server up: Tailscale checks, key bootstrap, Funnel, --install."""
from server_db_helpers import user_object
from agenticml.server.users import UserStore
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
    UpError, _ADMIN_NAME_NOT_ADMIN, _DATABASE_URL_NEWLINE, _ENV_DATABASE_KEY,
    _ENV_FILE_MARKER, _ENV_KEY_NOT_INHERITED, _ENV_PATH_CONTROL, _ENV_PATH_GLOB,
    _INSTALL_ENV_DATABASE, _INSTALL_NEEDS_PORT, _NO_ADMIN_TTY, _NO_DNS,
    _NO_FUNNEL, _NO_HTTPS, _NOT_INSTALLED, _NOT_RUNNING, _PORT_443_TAKEN,
    _SETUP_CLOSED, _SHELL_NEED_ESCAPE, _SYSTEMD_CONTROL, _UNSAFE_DIR_SYMLINK,
    _UNSAFE_DIR_WRITABLE, _environment_file_assignment, _remove_our_env_file,
    _systemd_quote, launchd_plist_path, systemd_env_path, systemd_unit_path,
    write_launchd_plist, write_systemd_unit,
)
from server_db_helpers import default_workspace_id

pytest.importorskip("fcntl")


@pytest.fixture(autouse=True)
def _up_tests_independent_of_umask(tmp_path):
    previous = os.umask(0o022)
    os.chmod(tmp_path, 0o755)
    try:
        yield
    finally:
        os.umask(previous)


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
    users = user_object(UserStore(data / "server", workspace_id=default_workspace_id(data / "server")))
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
    assert user_object(UserStore(data / "server", workspace_id=default_workspace_id(data / "server"))) == users


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
    KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).create("admin", "admin")
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
    assert "--database" not in text
    assert "EnvironmentFile=" not in text
    assert stat.S_IMODE(unit.stat().st_mode) == 0o600
    foreign = tmp_path / "other" / ".config/systemd/user/agenticml-server-up.service"
    monkeypatch.setenv("HOME", str(tmp_path / "other"))
    foreign.parent.mkdir(parents=True)
    foreign.write_text("[Unit]\nDescription=nope\n")
    assert run_up(data, "--install") == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert foreign.read_text() == "[Unit]\nDescription=nope\n"
    assert run_up(data, "--install", "--force") == 0
    assert "# Managed by agenticml server up" in foreign.read_text()


def test_install_omits_environment_database_url(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    env_path = tmp_path / "secret-password" / "state.db"
    env_path.parent.mkdir()
    monkeypatch.setenv("AGENTICML_SERVER_DATABASE_URL", "sqlite:///" + str(env_path))
    data = tmp_path / "data"
    assert run_up(data, "--install") == 0
    unit = home / ".config/systemd/user/agenticml-server-up.service"
    text = unit.read_text()
    captured = capsys.readouterr()
    assert "--database" not in text
    assert "EnvironmentFile=" not in text
    assert "secret-password" not in text
    assert "secret-password" not in captured.out
    assert not systemd_env_path(home).exists()
    assert _INSTALL_ENV_DATABASE in captured.out
    assert captured.out.count(_INSTALL_ENV_DATABASE) == 1
    assert stat.S_IMODE(unit.stat().st_mode) == 0o600


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
    assert "EnvironmentVariables" not in payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_bytes(plistlib.dumps({"Label": "other.job"}))
    with pytest.raises(UpError, match="refusing to overwrite"):
        write_launchd_plist(data, 8080, home=home)
    write_launchd_plist(data, 8080, home=home, force=True)
    assert plistlib.loads(path.read_bytes())["Label"] == "xyz.agenticml.server-up"


def test_install_explicit_database_uses_env_file_not_argv(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    data = tmp_path / "data"
    database = "sqlite:///" + str(tmp_path / "private state.db")
    assert run_up(data, "--port", "9090", "--install", "--database", database) == 0
    captured = capsys.readouterr()
    unit = home / ".config/systemd/user/agenticml-server-up.service"
    env_path = systemd_env_path(home)
    text = unit.read_text()
    assert "--database" not in text and database not in text
    assert f"EnvironmentFile={env_path}\n" in text
    assert f'EnvironmentFile="{env_path}"' not in text
    assert "EnvironmentFile=-" not in text
    assert env_path.read_text() == (
        f"{_ENV_FILE_MARKER}\n"
        f"{_environment_file_assignment(_ENV_DATABASE_KEY, database)}")
    assert database not in captured.out
    assert stat.S_IMODE(unit.stat().st_mode) == stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_launchd_database_goes_in_environment_variables(tmp_path, capsys):
    database = "sqlite:///" + str(tmp_path / "private state.db")
    path = write_launchd_plist(tmp_path / "data", 8080, home=tmp_path, database=database)
    payload = plistlib.loads(path.read_bytes())
    assert "--database" not in payload["ProgramArguments"]
    assert database not in payload["ProgramArguments"]
    assert payload["EnvironmentVariables"][_ENV_DATABASE_KEY] == database
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert database not in capsys.readouterr().out


def _parse_environment_file(text):
    """systemd 249 parse_env_file_internal as used by load_env_file (no $ expansion)."""
    whitespace, newline, comments = " \t\n\r", "\n\r", "#;"
    pre_key, key_st, pre_value, value_st, value_esc = range(5)
    single_q, double_q, double_esc, comment, comment_esc = range(5, 10)
    state = pre_key
    key, value = [], []
    last_key_ws = last_value_ws = None
    env = {}

    def push(trim_value_ws):
        k = "".join(key)
        if last_key_ws is not None:
            k = k[:last_key_ws]
        v = "".join(value)
        if trim_value_ws and last_value_ws is not None:
            v = v[:last_value_ws]
        env[k] = v

    for char in text:
        if state == pre_key:
            if char in comments:
                state = comment
            elif char not in whitespace:
                state = key_st
                key, value = [char], []
                last_key_ws = last_value_ws = None
        elif state == key_st:
            if char in newline:
                state = pre_key
                key = []
            elif char == "=":
                state = pre_value
                last_value_ws = None
            else:
                if char not in whitespace:
                    last_key_ws = None
                elif last_key_ws is None:
                    last_key_ws = len(key)
                key.append(char)
        elif state == pre_value:
            if char in newline:
                push(False)
                state = pre_key
                key, value = [], []
            elif char == "'":
                state = single_q
            elif char == '"':
                state = double_q
            elif char == "\\":
                state = value_esc
            elif char not in whitespace:
                state = value_st
                value = [char]
        elif state == value_st:
            if char in newline:
                push(True)
                state = pre_key
                key, value = [], []
            elif char == "\\":
                state = value_esc
                last_value_ws = None
            else:
                if char not in whitespace:
                    last_value_ws = None
                elif last_value_ws is None:
                    last_value_ws = len(value)
                value.append(char)
        elif state == value_esc:
            state = value_st
            if char not in newline:
                value.append(char)
        elif state == single_q:
            if char == "'":
                state = pre_value
            else:
                value.append(char)
        elif state == double_q:
            if char == '"':
                state = pre_value
            elif char == "\\":
                state = double_esc
            else:
                value.append(char)
        elif state == double_esc:
            state = double_q
            if char in _SHELL_NEED_ESCAPE:
                value.append(char)
            elif char != "\n":
                value.append("\\")
                value.append(char)
        elif state == comment:
            if char == "\\":
                state = comment_esc
            elif char in newline:
                state = pre_key
        elif state == comment_esc:
            state = comment
    if state in (pre_value, value_st, value_esc, single_q, double_q, double_esc):
        push(state == value_st)
    return env


def test_environment_file_assignment_quoting_and_refuses_newline_nul(tmp_path):
    assert _environment_file_assignment(_ENV_DATABASE_KEY, 'sqlite:///a"b\\c') == (
        'AGENTICML_SERVER_DATABASE_URL="sqlite:///a\\"b\\\\c"\n')
    for value in (
        'sqlite:///a"b\\c',
        "postgresql://u:p$ass@h/db",
        "postgresql://u:p%cent@h/db",
        "postgresql://u:p#hash@h/db",
        "postgresql://u:p`tick@h/db",
        "postgresql://u:p$a%b#c`d\"e\\f@h/db",
    ):
        text = (
            f"{_ENV_FILE_MARKER}\n"
            f"{_environment_file_assignment(_ENV_DATABASE_KEY, value)}")
        parsed = _parse_environment_file(text)[_ENV_DATABASE_KEY]
        assert parsed == value
        assert parsed.encode("utf-8") == value.encode("utf-8")
    for writer in (write_systemd_unit, write_launchd_plist):
        home = tmp_path / "h"
        for bad in ("postgresql://x\ny", "postgresql://x\ry", "postgresql://x\0y"):
            with pytest.raises(UpError, match=_DATABASE_URL_NEWLINE):
                writer(tmp_path / "data", 8080, home=home, database=bad)
            assert not systemd_env_path(home).exists()
            assert not launchd_plist_path(home).exists()
            assert not (home / "Library" / "Logs").exists()


def test_install_env_file_path_unquoted_with_space_dollar_percent(tmp_path):
    home = tmp_path / "user name$x%y"
    database = "sqlite:///" + str(tmp_path / "private.db")
    path = write_systemd_unit(tmp_path / "data", 8080, home=home, database=database)
    env_path = systemd_env_path(home)
    text = path.read_text()
    line = "EnvironmentFile=" + str(env_path).replace("%", "%%")
    assert line + "\n" in text
    assert f'EnvironmentFile="{env_path}"' not in text
    assert "EnvironmentFile=-" not in text
    env_file_line = [row for row in text.splitlines() if row.startswith("EnvironmentFile=")]
    assert env_file_line == [line]
    assert "$$" not in env_file_line[0]


def test_env_file_refuse_overwrite_without_marker(tmp_path, capsys):
    home = tmp_path / "home"
    env_path = systemd_env_path(home)
    env_path.parent.mkdir(parents=True)
    env_path.write_text("SECRETS=keep\n")
    with pytest.raises(UpError, match="refusing to overwrite"):
        write_systemd_unit(tmp_path / "data", 8080, home=home, database="sqlite:///x")
    assert env_path.read_text() == "SECRETS=keep\n"
    assert not systemd_unit_path(home).exists()
    write_systemd_unit(
        tmp_path / "data", 8080, home=home, force=True, database="sqlite:///x")
    assert env_path.read_text().startswith(_ENV_FILE_MARKER + "\n")
    assert "sqlite:///x" in env_path.read_text()
    write_systemd_unit(
        tmp_path / "data", 8080, home=home, database="sqlite:///second")
    assert "sqlite:///second" in env_path.read_text()
    assert "sqlite:///x" not in env_path.read_text()
    capsys.readouterr()


def test_reinstall_without_database_removes_our_env_file(tmp_path, capsys):
    home = tmp_path / "home"
    write_systemd_unit(
        tmp_path / "data", 8080, home=home, database="sqlite:///secret")
    env_path = systemd_env_path(home)
    assert env_path.exists()
    write_systemd_unit(tmp_path / "data", 8080, home=home)
    assert not env_path.exists()
    assert "EnvironmentFile=" not in systemd_unit_path(home).read_text()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("NOTOURS=1\n")
    write_systemd_unit(tmp_path / "data", 8080, home=home)
    assert env_path.read_text() == "NOTOURS=1\n"
    capsys.readouterr()


def test_failed_unit_write_leaves_no_env_file(tmp_path):
    home = tmp_path / "home"
    unit = systemd_unit_path(home)
    unit.parent.mkdir(parents=True)
    unit.mkdir()
    env_path = systemd_env_path(home)
    with pytest.raises(OSError):
        write_systemd_unit(
            tmp_path / "data", 8080, home=home, force=True,
            database="postgresql://u:secret@h/db")
    assert not env_path.exists()


def test_install_rewrites_0644_files_to_0600(tmp_path, capsys):
    home = tmp_path / "home"
    unit = systemd_unit_path(home)
    env_path = systemd_env_path(home)
    plist = launchd_plist_path(home)
    unit.parent.mkdir(parents=True)
    env_path.parent.mkdir(parents=True)
    plist.parent.mkdir(parents=True)
    unit.write_text("# Managed by agenticml server up\n[Unit]\n")
    unit.chmod(0o644)
    env_path.write_text(_ENV_FILE_MARKER + "\nOLD=1\n")
    env_path.chmod(0o644)
    plist.write_bytes(plistlib.dumps({"Label": "xyz.agenticml.server-up"}))
    plist.chmod(0o644)
    write_systemd_unit(
        tmp_path / "data", 8080, home=home, database="sqlite:///x")
    assert stat.S_IMODE(unit.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    write_launchd_plist(tmp_path / "data", 8080, home=home)
    assert stat.S_IMODE(plist.stat().st_mode) == 0o600
    capsys.readouterr()


def test_environment_file_path_refuses_control_characters(tmp_path):
    for bad in ("\x01", "\t", "\x7f"):
        home = tmp_path / f"h{bad}ome"
        with pytest.raises(UpError, match=_ENV_PATH_CONTROL):
            write_systemd_unit(
                tmp_path / "data", 8080, home=home, database="sqlite:///x")
        assert not systemd_env_path(home).exists()
        assert not systemd_unit_path(home).exists()


@pytest.mark.parametrize("bad", ["*", "?", "[", "]", "\\"])
def test_environment_file_path_refuses_glob_characters(tmp_path, bad):
    home = tmp_path / f"h{bad}ome"
    with pytest.raises(UpError) as exc:
        write_systemd_unit(
            tmp_path / "data", 8080, home=home, database="sqlite:///x")
    assert str(exc.value) == _ENV_PATH_GLOB
    assert not systemd_env_path(home).exists()
    assert not systemd_unit_path(home).exists()


def test_atomic_write_refuses_symlinked_agenticml_dir(tmp_path):
    home = tmp_path / "home"
    real = tmp_path / "captured"
    real.mkdir()
    env_dir = home / ".config" / "agenticml"
    env_dir.parent.mkdir(parents=True)
    env_dir.symlink_to(real)
    with pytest.raises(UpError, match=_UNSAFE_DIR_SYMLINK):
        write_systemd_unit(
            tmp_path / "data", 8080, home=home, database="sqlite:///x")
    assert list(real.iterdir()) == []
    assert not systemd_env_path(home).is_file()


def test_atomic_write_refuses_group_writable_agenticml_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    env_dir = home / ".config" / "agenticml"
    env_dir.mkdir(parents=True)
    env_dir.chmod(0o770)
    monkeypatch.setattr(up_mod, "_is_user_private_group", lambda gid: False)
    with pytest.raises(UpError, match=_UNSAFE_DIR_WRITABLE):
        write_systemd_unit(
            tmp_path / "data", 8080, home=home, database="sqlite:///x")
    assert not systemd_env_path(home).exists()


def test_atomic_write_creates_private_dirs_and_env_file(tmp_path, capsys):
    home = tmp_path / "home"
    write_systemd_unit(
        tmp_path / "data", 8080, home=home, database="sqlite:///x")
    env_dir = home / ".config" / "agenticml"
    unit_dir = home / ".config" / "systemd" / "user"
    assert env_dir.is_dir() and not env_dir.is_symlink()
    assert unit_dir.is_dir() and not unit_dir.is_symlink()
    assert stat.S_IMODE(env_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(unit_dir.stat().st_mode) == 0o700
    env_path = systemd_env_path(home)
    assert env_path.read_text().startswith(_ENV_FILE_MARKER + "\n")
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    capsys.readouterr()


def test_atomic_write_refuses_symlinked_config_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    real = tmp_path / "captured"
    real.mkdir()
    (real / "agenticml").mkdir()
    (home / ".config").symlink_to(real)
    with pytest.raises(UpError) as exc:
        write_systemd_unit(
            tmp_path / "data", 8080, home=home, database="sqlite:///x")
    assert str(home / ".config") in str(exc.value)
    assert _UNSAFE_DIR_SYMLINK in str(exc.value)
    assert list((real / "agenticml").iterdir()) == []
    assert not (real / "systemd").exists()
    assert not systemd_env_path(home).is_file()


def test_atomic_write_refuses_group_writable_config_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = home / ".config"
    config.mkdir(parents=True)
    config.chmod(0o770)
    monkeypatch.setattr(up_mod, "_is_user_private_group", lambda gid: False)
    with pytest.raises(UpError) as exc:
        write_systemd_unit(
            tmp_path / "data", 8080, home=home, database="sqlite:///x")
    assert str(config) in str(exc.value)
    assert _UNSAFE_DIR_WRITABLE in str(exc.value)
    assert not systemd_env_path(home).exists()


def test_atomic_write_refuses_group_writable_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o770)
    monkeypatch.setattr(up_mod, "_is_user_private_group", lambda gid: False)
    with pytest.raises(UpError) as exc:
        write_systemd_unit(
            tmp_path / "data", 8080, home=home, database="sqlite:///x")
    assert str(home) in str(exc.value)
    assert _UNSAFE_DIR_WRITABLE in str(exc.value)
    assert not systemd_env_path(home).exists()


def test_atomic_write_accepts_group_writable_user_private_group(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for path in (home, home / ".config", home / ".config" / "systemd", unit_dir):
        path.chmod(0o775)
    monkeypatch.setattr(up_mod, "_is_user_private_group", lambda gid: gid == os.getgid())
    write_systemd_unit(
        tmp_path / "data", 8080, home=home, database="sqlite:///x")
    env_dir = home / ".config" / "agenticml"
    assert env_dir.is_dir() and stat.S_IMODE(env_dir.stat().st_mode) == 0o700
    assert systemd_env_path(home).is_file()
    capsys.readouterr()


def test_atomic_write_refuses_group_writable_other_group(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o775)
    monkeypatch.setattr(up_mod, "_is_user_private_group", lambda gid: False)
    with pytest.raises(UpError, match=_UNSAFE_DIR_WRITABLE):
        write_systemd_unit(
            tmp_path / "data", 8080, home=home, database="sqlite:///x")
    assert not systemd_env_path(home).exists()


def test_atomic_write_refuses_world_writable_even_with_private_group(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o777)
    monkeypatch.setattr(up_mod, "_is_user_private_group", lambda gid: True)
    with pytest.raises(UpError, match=_UNSAFE_DIR_WRITABLE):
        write_systemd_unit(
            tmp_path / "data", 8080, home=home, database="sqlite:///x")
    assert not systemd_env_path(home).exists()


def test_user_private_group_rule(monkeypatch):
    class User:
        pw_name = "alice"

    class Group:
        def __init__(self, name, members):
            self.gr_name = name
            self.gr_mem = members

    monkeypatch.setattr(up_mod.os, "getgid", lambda: 1000)
    monkeypatch.setattr(up_mod.os, "geteuid", lambda: 42)
    monkeypatch.setattr(up_mod.pwd, "getpwuid", lambda uid: User())
    monkeypatch.setattr(up_mod.grp, "getgrgid", lambda gid: Group("alice", []))
    assert up_mod._is_user_private_group(1000) is True
    monkeypatch.setattr(up_mod.grp, "getgrgid", lambda gid: Group("alice", ["alice"]))
    assert up_mod._is_user_private_group(1000) is True
    monkeypatch.setattr(up_mod.grp, "getgrgid", lambda gid: Group("alice", ["bob"]))
    assert up_mod._is_user_private_group(1000) is False
    monkeypatch.setattr(up_mod.grp, "getgrgid", lambda gid: Group("users", []))
    assert up_mod._is_user_private_group(1000) is False
    assert up_mod._is_user_private_group(1001) is False


def test_remove_our_env_file_does_not_follow_symlinked_dir(tmp_path, capsys):
    home = tmp_path / "home"
    write_systemd_unit(
        tmp_path / "data", 8080, home=home, database="sqlite:///secret")
    env_path = systemd_env_path(home)
    payload = env_path.read_text()
    captured = tmp_path / "captured"
    captured.mkdir()
    env_path.rename(captured / "server.env")
    env_path.parent.rmdir()
    env_path.parent.symlink_to(captured)
    with pytest.raises(UpError, match=_UNSAFE_DIR_SYMLINK):
        write_systemd_unit(tmp_path / "data", 8080, home=home)
    assert (captured / "server.env").read_text() == payload
    capsys.readouterr()


def test_remove_our_env_file_refuses_symlinked_config_dir(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    write_systemd_unit(
        tmp_path / "data", 8080, home=home, database="sqlite:///secret")
    env_path = systemd_env_path(home)
    payload = env_path.read_text()
    config = home / ".config"
    captured = tmp_path / "captured"
    config.rename(captured)
    config.symlink_to(captured)
    with pytest.raises(UpError) as exc:
        _remove_our_env_file(env_path, home=home)
    assert str(config) in str(exc.value)
    assert _UNSAFE_DIR_SYMLINK in str(exc.value)
    assert (captured / "agenticml" / "server.env").read_text() == payload
    capsys.readouterr()


def test_remove_our_env_file_refuses_group_writable_config_dir(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    write_systemd_unit(
        tmp_path / "data", 8080, home=home, database="sqlite:///secret")
    env_path = systemd_env_path(home)
    config = home / ".config"
    config.chmod(0o770)
    monkeypatch.setattr(up_mod, "_is_user_private_group", lambda gid: False)
    with pytest.raises(UpError) as exc:
        _remove_our_env_file(env_path, home=home)
    assert str(config) in str(exc.value)
    assert _UNSAFE_DIR_WRITABLE in str(exc.value)
    assert env_path.exists()
    capsys.readouterr()


def test_remove_our_env_file_refuses_group_writable_home(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    write_systemd_unit(
        tmp_path / "data", 8080, home=home, database="sqlite:///secret")
    env_path = systemd_env_path(home)
    home.chmod(0o770)
    monkeypatch.setattr(up_mod, "_is_user_private_group", lambda gid: False)
    with pytest.raises(UpError) as exc:
        _remove_our_env_file(env_path, home=home)
    assert str(home) in str(exc.value)
    assert _UNSAFE_DIR_WRITABLE in str(exc.value)
    assert env_path.exists()
    capsys.readouterr()


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
    KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).create("admin", "sync")
    assert run_up(data, "--install") == 0
    names = {record.name: record for record in KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).list()}
    assert list(names) == ["admin"] and names["admin"].scope == "sync"
    users = user_object(UserStore(data / "server", workspace_id=default_workspace_id(data / "server")))
    assert users["users"][0]["name"] == "admin"


def test_non_admin_named_admin_refused(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setattr(up_mod, "_stdout_is_tty", lambda: True)
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    UserStore(data / "server", workspace_id=default_workspace_id(data / "server")).add("admin", role="member")
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
    users = user_object(UserStore(data / "server", workspace_id=default_workspace_id(data / "server")))
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
    keys = KeyStore(data / "server", workspace_id=default_workspace_id(data / "server"))
    assert [record.name for record in keys.list()] == ["admin"]
    assert keys.list()[0].scope == "admin" and keys.list()[0].user == "admin"
    users = user_object(UserStore(data / "server", workspace_id=default_workspace_id(data / "server")))
    assert [row["name"] for row in users["users"]] == ["admin"]


def test_install_env_key_silent_when_file_admin_exists(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTICML_SERVER_KEY", "a" * 32)
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).create("admin", "admin")
    assert run_up(data, "--install") == 0
    out = capsys.readouterr().out
    assert _ENV_KEY_NOT_INHERITED not in out
    assert not any(line.startswith("aml_") for line in out.splitlines())


def test_setup_closed_refuses_bootstrap(tmp_path, monkeypatch, capsys):
    install_fake(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    data = tmp_path / "data"
    app.prepare_data_dir(data)
    keys = KeyStore(data / "server", workspace_id=default_workspace_id(data / "server"))
    KeyStore(data / "server", workspace_id=default_workspace_id(data / "server"))._write_setup_closed()
    assert not KeyStore(data / "server", workspace_id=default_workspace_id(data / "server")).has_admin()
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

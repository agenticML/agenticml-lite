"""Credentials and CLI authentication, using the actual HTTP Client."""
import io
import json
import os
import stat

import pytest
import requests
import responses

from agenticml import auth
from agenticml.cli import main
from agenticml.client import Client

HOST = "https://auth.test"
KEY = "abcdefgh-very-secret-api-key"


@pytest.fixture(autouse=True)
def credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICML_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path / "project-data"))
    monkeypatch.delenv("AGENTICML_HOST", raising=False)
    monkeypatch.delenv("AGENTICML_API_KEY", raising=False)
    return auth.credentials_path()


def saved(key=KEY, host=HOST):
    auth.write_credentials({"default_host": host, "hosts": {host: {"api_key": key}}})


def test_permissions_and_atomic_replace(credentials, monkeypatch):
    saved()
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert stat.S_IMODE(credentials.parent.stat().st_mode) == 0o700
    original = credentials.read_bytes()
    replace = os.replace
    observations = []

    def observe(source, target):
        assert credentials.read_bytes() == original
        assert source.parent == target.parent
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        assert json.loads(source.read_text())["hosts"][HOST]["api_key"] == "replacement"
        observations.append(source)
        replace(source, target)

    monkeypatch.setattr(auth.os, "replace", observe)
    saved("replacement")
    assert observations and not observations[0].exists()
    assert len(list(credentials.parent.iterdir())) == 1


def test_failed_replace_preserves_original(credentials, monkeypatch):
    saved()
    original = credentials.read_bytes()

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(auth.os, "replace", fail)
    with pytest.raises(OSError):
        saved("replacement")
    assert credentials.read_bytes() == original
    assert list(credentials.parent.iterdir()) == [credentials]


def test_resolution_precedence(monkeypatch):
    saved()
    assert auth.resolve() == (HOST, KEY)
    monkeypatch.setenv("AGENTICML_DIR", "/different/project")
    assert auth.resolve() == (HOST, KEY)
    monkeypatch.setenv("AGENTICML_HOST", "https://env.test///")
    assert auth.resolve() == ("https://env.test", None)
    monkeypatch.setenv("AGENTICML_API_KEY", "env-key")
    assert auth.resolve() == ("https://env.test", "env-key")
    assert auth.resolve(HOST + "/", "flag-key") == (HOST, "flag-key")
    assert auth.resolve(HOST) == (HOST, "env-key")
    monkeypatch.delenv("AGENTICML_HOST")
    monkeypatch.delenv("AGENTICML_API_KEY")
    assert auth.resolve(api_key="flag") == (HOST, "flag")


def test_default_host(credentials):
    assert auth.resolve() == (auth.DEFAULT_HOST, None)
    assert not credentials.exists()


@responses.activate
def test_login_validates_before_saving(credentials, capsys):
    def verify(request):
        assert not credentials.exists()
        assert request.headers["x-api-key"] == KEY
        return 200, {}, "[]"

    responses.add_callback(responses.GET, HOST + "/runs?limit=1", callback=verify)
    assert main(["login", "--host", HOST + "/", "--key", KEY]) == 0
    assert auth.resolve() == (HOST, KEY)
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
    assert "abcdefgh…" in output.out


@pytest.mark.parametrize("status", [401, 403])
@responses.activate
def test_login_rejected_leaves_credentials_unchanged(credentials, capsys, status):
    saved("old-key")
    original = credentials.read_bytes()
    responses.add(responses.GET, HOST + "/runs", status=status)
    assert main(["login", "--host", HOST, "--key", KEY]) == 1
    assert credentials.read_bytes() == original
    output = capsys.readouterr()
    assert "invalid API key" in output.err
    assert KEY not in output.out + output.err


@responses.activate
def test_login_network_failure(credentials, capsys):
    responses.add(responses.GET, HOST + "/runs", body=requests.ConnectionError("secret detail"))
    assert main(["login", "--host", HOST, "--key", KEY]) == 1
    assert not credentials.exists()
    assert "ConnectionError" in capsys.readouterr().err


@responses.activate
def test_login_no_verify_stdin(credentials, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert main(["login", "--host", HOST, "--no-verify"]) == 0
    assert auth.resolve() == (HOST, KEY)
    assert not responses.calls
    assert KEY not in capsys.readouterr().out


def test_login_getpass(monkeypatch):
    class TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", TTY())
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: KEY)
    assert main(["login", "--host", HOST, "--no-verify"]) == 0
    assert auth.resolve() == (HOST, KEY)


def test_logout_clears_default_only_for_target(monkeypatch):
    auth.write_credentials({"default_host": HOST, "hosts": {
        HOST: {"api_key": KEY}, "https://other.test": {"api_key": "other"},
    }})
    assert main(["logout", "--host", "https://other.test/"]) == 0
    assert auth.read_credentials()["default_host"] == HOST
    monkeypatch.setenv("AGENTICML_HOST", "https://ignored.test")
    assert main(["logout"]) == 0
    assert not auth.read_credentials().get("default_host")
    assert auth.read_credentials()["hosts"] == {}


@pytest.mark.parametrize("status,exit_code", [(200, 0), (401, 1), (403, 1), (500, 1)])
@responses.activate
def test_whoami_verification(status, exit_code, capsys):
    saved()
    responses.add(responses.GET, HOST + "/runs", status=status, json=[])
    assert main(["whoami"]) == exit_code
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
    if status == 200:
        assert "source=file" in output.out and "valid" in output.out


@responses.activate
def test_whoami_offline_and_no_credentials(capsys, monkeypatch):
    assert main(["whoami", "--offline"]) == 1
    saved()
    assert main(["whoami", "--offline", "--host", HOST + "/"]) == 0
    assert "unchecked (offline)" in capsys.readouterr().out
    monkeypatch.setenv("AGENTICML_API_KEY", KEY)
    assert main(["whoami", "--offline"]) == 0
    assert "source=env" in capsys.readouterr().out
    assert not responses.calls


@pytest.mark.parametrize("status", [201, 204, 400, 500])
@responses.activate
def test_check_auth_raises_for_other_statuses(status):
    responses.add(responses.GET, HOST + "/runs", status=status)
    with pytest.raises((ValueError, requests.HTTPError)):
        Client(base_url=HOST).check_auth()


@pytest.mark.parametrize("key", ["x", "short", KEY])
def test_short_keys_also_redacted(key, capsys):
    assert main(["login", "--host", HOST, "--key", key, "--no-verify"]) == 0
    assert f"key {key}" not in capsys.readouterr().out


def test_host_normalisation():
    assert auth.normalize_host("HTTPS://API.Example.com:443/v1/") == "https://api.example.com/v1"
    assert auth.normalize_host("http://127.0.0.1:80") == "http://127.0.0.1"
    assert auth.normalize_host("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert auth.normalize_host("http://[::1]:80/") == "http://[::1]"
    assert auth.normalize_host("HTTP://LocalHost/api/") == "http://localhost/api"
    assert auth.normalize_host("http://example.com", insecure=True) == "http://example.com"
    with pytest.raises(ValueError, match="insecure"):
        auth.normalize_host("http://example.com")
    with pytest.raises(ValueError, match="scheme"):
        auth.normalize_host("ftp://example.com")


def test_login_http_requires_insecure_except_localhost(credentials, capsys):
    assert main(["login", "--host", "http://example.com", "--key", KEY, "--no-verify"]) == 1
    assert "insecure" in capsys.readouterr().err
    assert not credentials.exists()
    assert main(["login", "--host", "http://Example.com/", "--key", KEY, "--no-verify", "--insecure"]) == 0
    assert auth.resolve(insecure=True) == ("http://example.com", KEY)
    assert main(["login", "--host", "HTTP://LocalHost:80/", "--key", "local-key", "--no-verify"]) == 0
    assert auth.resolve("http://localhost") == ("http://localhost", "local-key")


def test_whoami_rejects_remote_http_without_insecure(capsys):
    assert main(["whoami", "--offline", "--host", "http://example.com"]) == 1
    assert "insecure" in capsys.readouterr().err
    assert main(["whoami", "--offline", "--host", "http://example.com", "--insecure"]) == 1
    assert "not logged in" in capsys.readouterr().err


def test_logout_normalises_host(capsys):
    auth.write_credentials({"default_host": "https://other.test", "hosts": {
        "https://other.test": {"api_key": "other"},
    }})
    assert main(["logout", "--host", "HTTPS://Other.Test:443/"]) == 0
    assert auth.read_credentials()["hosts"] == {}
    assert "https://other.test" in capsys.readouterr().out


def test_umask_002_owned_directory_is_tightened(credentials):
    previous = os.umask(0o002)
    try:
        credentials.parent.mkdir()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(credentials.parent.stat().st_mode) == 0o775
    assert main(["login", "--host", HOST, "--key", KEY, "--no-verify"]) == 0
    assert stat.S_IMODE(credentials.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert auth.resolve() == (HOST, KEY)


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_owned_writable_config_dir_is_tightened(credentials, mode):
    credentials.parent.mkdir()
    os.chmod(credentials.parent, mode)
    assert main(["login", "--host", HOST, "--key", KEY, "--no-verify"]) == 0
    assert stat.S_IMODE(credentials.parent.stat().st_mode) == 0o700
    assert not (stat.S_IMODE(credentials.stat().st_mode) & 0o077)


def test_refuses_config_dir_owned_by_another_user(credentials, monkeypatch):
    credentials.parent.mkdir(mode=0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(auth.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(PermissionError, match=f"chmod 700 {credentials.parent}"):
        saved()


def test_login_interrupt_is_quiet(monkeypatch, capsys):
    class TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", TTY())

    def boom(_prompt):
        raise KeyboardInterrupt()

    monkeypatch.setattr(auth.getpass, "getpass", boom)
    assert main(["login", "--host", HOST, "--no-verify"]) == 130
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_config_dir_prefers_xdg_then_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert auth.credentials_path() == tmp_path / "xdg" / "agenticml" / "credentials.json"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    home = tmp_path / "home"
    monkeypatch.setattr(auth.Path, "home", lambda: home)
    assert auth.credentials_path() == home / ".config" / "agenticml" / "credentials.json"


def test_read_ignores_group_writable_file_or_dir(credentials, capsys):
    saved()
    os.chmod(credentials, 0o664)
    assert auth.read_credentials() == {"hosts": {}}
    err = capsys.readouterr().err
    assert "writable" in err
    assert KEY not in err
    os.chmod(credentials, 0o600)
    os.chmod(credentials.parent, 0o775)
    assert auth.read_credentials() == {"hosts": {}}
    err = capsys.readouterr().err
    assert "writable" in err
    assert stat.S_IMODE(credentials.parent.stat().st_mode) == 0o775
    assert main(["whoami", "--offline"]) == 1
    captured = capsys.readouterr()
    assert "not logged in" in captured.err
    assert KEY not in captured.out + captured.err


def test_read_ignores_credentials_owned_by_someone_else(credentials, monkeypatch, capsys):
    saved()
    real_uid = os.getuid()
    monkeypatch.setattr(auth.os, "getuid", lambda: real_uid + 1)
    assert auth.read_credentials() == {"hosts": {}}
    err = capsys.readouterr().err
    assert "owned" in err
    assert KEY not in err
    assert main(["whoami", "--offline"]) == 1
    assert "not logged in" in capsys.readouterr().err








@pytest.mark.parametrize("bad", ["abc\r\nSECRET-value", "bad key", "a\x00b", "a\x7fb"])
def test_crlf_and_whitespace_keys_are_rejected_without_echo(bad, capsys):
    assert main(["login", "--host", HOST, "--key", bad, "--no-verify"]) == 1
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert bad not in blob
    assert "SECRET" not in blob
    assert "invalid API key" in captured.err
    with pytest.raises(ValueError, match="^invalid API key$") as caught:
        Client(base_url=HOST, api_key=bad)
    assert bad not in str(caught.value)
    assert "SECRET" not in str(caught.value)


def test_login_strips_surrounding_whitespace(credentials):
    assert main(["login", "--host", HOST, "--key", f"  {KEY}\n", "--no-verify"]) == 0
    assert auth.resolve() == (HOST, KEY)
    client = Client(base_url=HOST, api_key=f"\n{KEY}  ")
    try:
        assert client.api_key == KEY
        assert client.session.headers["x-api-key"] == KEY
    finally:
        client.close()


def test_report_hides_invalid_header_text(capsys):
    secret = "header-secret-value"
    exc = requests.exceptions.InvalidHeader(f"x-api-key: {secret}\r\n")
    assert isinstance(exc, ValueError)
    assert auth._report("login", exc) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "x-api-key" not in captured.out + captured.err
    assert "InvalidHeader" in captured.err


def test_login_does_not_echo_invalid_header(monkeypatch, capsys):
    secret = "live-header-secret"

    def boom(self):
        raise requests.exceptions.InvalidHeader(f"x-api-key: {secret}")

    monkeypatch.setattr(Client, "check_auth", boom)
    assert main(["login", "--host", HOST, "--key", KEY]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert KEY not in captured.out + captured.err
    assert "InvalidHeader" in captured.err


def test_warns_when_env_or_flag_key_uses_saved_host(monkeypatch, capsys):
    saved()
    monkeypatch.setenv("AGENTICML_API_KEY", "env-secret-key")
    assert auth.resolve() == (HOST, "env-secret-key")
    err = capsys.readouterr().err
    assert HOST in err and "credentials file" in err
    assert "env-secret-key" not in err
    assert auth.resolve(HOST, "flag-secret") == (HOST, "flag-secret")
    assert capsys.readouterr().err == ""
    monkeypatch.delenv("AGENTICML_API_KEY")
    assert auth.resolve(api_key="flag-secret") == (HOST, "flag-secret")
    err = capsys.readouterr().err
    assert HOST in err
    assert "flag-secret" not in err


def test_no_host_warning_for_builtin_default(monkeypatch, capsys):
    monkeypatch.setenv("AGENTICML_API_KEY", "env-secret-key")
    assert auth.resolve() == (auth.DEFAULT_HOST, "env-secret-key")
    assert capsys.readouterr().err == ""


@responses.activate
def test_whoami_warns_before_request_to_saved_host(monkeypatch, capsys):
    saved()
    monkeypatch.setenv("AGENTICML_API_KEY", "env-secret-key")
    responses.add(responses.GET, HOST + "/runs", json=[])
    assert main(["whoami"]) == 0
    err = capsys.readouterr().err
    assert HOST in err
    assert "env-secret-key" not in err
    assert responses.calls


def test_whoami_and_logout_interrupts_are_quiet(monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(auth, "read_credentials", boom)
    assert main(["whoami"]) == 130
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert main(["logout"]) == 130
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""

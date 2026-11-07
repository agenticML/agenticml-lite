import pytest

from agenticml import cli, server


@pytest.mark.parametrize("env, flag, expected", [
    (None, None, None),
    ("", None, None),
    ("https://env.example.com", None, "https://env.example.com"),
    (None, "https://flag.example.com", "https://flag.example.com"),
    ("https://env.example.com", "https://flag.example.com", "https://flag.example.com"),
    ("invalid", "https://flag.example.com", "https://flag.example.com"),
])
def test_serve_public_url(monkeypatch, env, flag, expected):
    if env is None:
        monkeypatch.delenv("AGENTICML_SERVER_PUBLIC_URL", raising=False)
    else:
        monkeypatch.setenv("AGENTICML_SERVER_PUBLIC_URL", env)
    calls = []
    monkeypatch.setattr(server, "serve", lambda *args, **kwargs: calls.append(args))
    args = ["server", "serve", "--data", "/data", "--host", "0.0.0.0", "--port", "8123"]
    if flag is not None:
        args += ["--public-url", flag]
    assert cli.main(args) == 0
    assert calls == [("/data", "0.0.0.0", 8123, expected)]


def test_serve_invalid_env_url_matches_flag(tmp_path, monkeypatch, capsys):
    args = ["server", "serve", "--data", str(tmp_path), "--port", "0"]
    monkeypatch.setenv("AGENTICML_SERVER_PUBLIC_URL", "invalid")
    assert cli.main(args) == 1
    env_error = capsys.readouterr().err
    monkeypatch.delenv("AGENTICML_SERVER_PUBLIC_URL")
    assert cli.main(args + ["--public-url", "invalid"]) == 1
    assert capsys.readouterr().err == env_error
    assert "public URL must be http:// or https://" in env_error


def test_serve_help_names_environment(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["server", "serve", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "AGENTICML_SERVER_DATA" in help_text
    assert "AGENTICML_SERVER_PUBLIC_URL" in help_text

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


@pytest.mark.parametrize("env, flag, expected", [
    (None, None, None),
    ("", None, None),
    ("  ", None, None),
    ("off", None, None),
    ("cloudwatch", None, "cloudwatch"),
    (" CloudWatch ", None, "cloudwatch"),
    (None, "cloudwatch", "cloudwatch"),
    (None, "CLOUDWATCH", "cloudwatch"),
    ("cloudwatch", "off", None),
    ("off", "cloudwatch", "cloudwatch"),
    ("invalid", "cloudwatch", "cloudwatch"),
])
def test_serve_metrics_flag_and_env(monkeypatch, env, flag, expected):
    if env is None:
        monkeypatch.delenv("AGENTICML_SERVER_METRICS", raising=False)
    else:
        monkeypatch.setenv("AGENTICML_SERVER_METRICS", env)
    calls = []
    monkeypatch.setattr(server, "serve", lambda *args, **kwargs: calls.append(kwargs))
    args = ["server", "serve", "--data", "/data", "--port", "8123"]
    if flag is not None:
        args += ["--metrics", flag]
    assert cli.main(args) == 0
    assert calls[0].get("metrics") == expected
    assert ("metrics" in calls[0]) == (expected is not None)


def test_serve_invalid_metrics_env_matches_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server, "serve", lambda *args, **kwargs: pytest.fail("serve called"))
    args = ["server", "serve", "--data", str(tmp_path), "--port", "0"]
    monkeypatch.setenv("AGENTICML_SERVER_METRICS", "bogus")
    assert cli.main(args) == 1
    env_error = capsys.readouterr().err
    monkeypatch.delenv("AGENTICML_SERVER_METRICS")
    assert cli.main(args + ["--metrics", "bogus"]) == 1
    assert capsys.readouterr().err == env_error
    assert "AGENTICML_SERVER_METRICS must be off or cloudwatch" in env_error


def test_serve_help_names_metrics_environment(capsys):
    with pytest.raises(SystemExit):
        cli.main(["server", "serve", "--help"])
    help_text = capsys.readouterr().out
    assert "--metrics" in help_text
    assert "AGENTICML_SERVER_METRICS" in help_text

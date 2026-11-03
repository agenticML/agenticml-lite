"""Tests for `agenticml run`: argument parsing and running scripts as __main__."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenticml import cli
from agenticml.launch import RunSettings, Target, _resolve
from agenticml.store import RunReader


# ----- argument parsing -----


_NEW_LAUNCH_ENV = (
    "AGENTICML_RUN_ID", "AGENTICML_RESUME", "AGENTICML_CONFIG_FILE",
    "AGENTICML_SYSTEM_METRICS", "AGENTICML_SYSTEM_METRICS_INTERVAL", "AGENTICML_LINEAGE",
    "AGENTICML_TRACK_CODE", "AGENTICML_CAPTURE_ENV", "AGENTICML_TRACK_FILES",
    "AGENTICML_SOURCE_ROOTS", "AGENTICML_MAX_FILE_BYTES", "AGENTICML_MAX_TOTAL_BYTES",
    "AGENTICML_FLUSH_INTERVAL", "AGENTICML_ALL_RANKS",
)


@pytest.fixture(autouse=True)
def clean_parent_env(monkeypatch):
    monkeypatch.delenv("AGENTICML_PARENT", raising=False)
    monkeypatch.delenv("AGENTICML_ALLOW_CROSS_TASK", raising=False)
    for name in _NEW_LAUNCH_ENV:
        monkeypatch.delenv(name, raising=False)


def test_launch_defaults(monkeypatch):
    for name in os.environ:
        if name.startswith("AGENTICML_"):
            monkeypatch.delenv(name)
    settings = RunSettings.from_env()
    assert settings == RunSettings()
    assert settings.track_source == "runtime"
    assert settings.system_metrics_interval == 10.0
    assert settings.flush_interval == 2
    assert settings.max_file_bytes is settings.max_total_bytes is None
    assert settings.auto_track is settings.auto_metrics is settings.sync_tensorboard is True


@pytest.mark.parametrize("value", ["", " \t\n "])
def test_empty_launch_env_uses_defaults(monkeypatch, value):
    for name in os.environ:
        if name.startswith("AGENTICML_"):
            monkeypatch.delenv(name)
    for name in (
        "AGENTICML_RUN_ID", "AGENTICML_RESUME", "AGENTICML_MAX_FILE_BYTES",
        "AGENTICML_MAX_TOTAL_BYTES", "AGENTICML_SYSTEM_METRICS_INTERVAL",
        "AGENTICML_FLUSH_INTERVAL", "AGENTICML_CONFIG_FILE",
    ):
        monkeypatch.setenv(name, value)
    assert RunSettings.from_env() == RunSettings()
    settings, _, _ = cli._parse_run_options(["t.py"])
    assert settings == RunSettings()


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("mode", ["allow", "MUST", " Allow "])
def test_resume_options(attached, mode):
    options = (["--id=run-1", f"--resume={mode}"] if attached
               else ["--id", "run-1", "--resume", mode])
    settings, raw, rest = cli._parse_run_options([*options, "t.py"])
    assert (settings.id, settings.resume) == ("run-1", mode.strip().lower())
    assert raw == options
    assert rest == ["t.py"]


def test_resume_env_and_cli_merge(monkeypatch):
    monkeypatch.setenv("AGENTICML_RUN_ID", "env-id")
    monkeypatch.setenv("AGENTICML_RESUME", " ALLOW ")
    assert (RunSettings.from_env().id, RunSettings.from_env().resume) == ("env-id", "allow")
    settings, _, _ = cli._parse_run_options(["t.py"])
    assert (settings.id, settings.resume) == ("env-id", "allow")
    settings, _, _ = cli._parse_run_options(["--id=cli-id", "--resume=must", "t.py"])
    assert (settings.id, settings.resume) == ("cli-id", "must")
    monkeypatch.delenv("AGENTICML_RESUME")
    settings, _, _ = cli._parse_run_options(["--resume=must", "t.py"])
    assert (settings.id, settings.resume) == ("env-id", "must")
    monkeypatch.delenv("AGENTICML_RUN_ID")
    monkeypatch.setenv("AGENTICML_RESUME", "allow")
    settings, _, _ = cli._parse_run_options(["--id=cli-id", "t.py"])
    assert (settings.id, settings.resume) == ("cli-id", "allow")


@pytest.mark.parametrize("options, message", [
    (["--id=../bad", "--resume=allow"], "--id must be a valid run id"),
    (["--id=", "--resume=allow"], "--id must be a valid run id"),
    (["--resume=bad"], "--resume must be never, allow, or must"),
    (["--id=run-1"], "--id needs --resume allow or must"),
    (["--id=run-1", "--resume=never"], "--id needs --resume allow or must"),
    (["--resume=allow"], "--resume needs --id"),
    (["--resume=must"], "--resume needs --id"),
])
def test_invalid_resume_options(options, message, capsys):
    with pytest.raises(cli._UsageError, match=message):
        cli._parse_run_options([*options, "t.py"])
    assert cli.main(["run", *options, "t.py"]) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("env", [
    {"AGENTICML_RUN_ID": "run-1"},
    {"AGENTICML_RUN_ID": "run-1", "AGENTICML_RESUME": "never"},
    {"AGENTICML_RESUME": "allow"}, {"AGENTICML_RESUME": "must"},
])
def test_invalid_resume_env_combination(monkeypatch, env, capsys):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(cli._UsageError):
        cli._parse_run_options(["t.py"])
    assert cli.main(["run", "t.py"]) == 2
    assert "needs --" in capsys.readouterr().err


def test_resume_never_without_id():
    settings, _, _ = cli._parse_run_options(["--resume=NEVER", "t.py"])
    assert settings.resume == "never"
    assert settings.id is None


def test_resume_env_error_names_env_var(monkeypatch, capsys):
    message = "AGENTICML_RESUME must be never, allow, or must"
    monkeypatch.setenv("AGENTICML_RESUME", "bad")
    with pytest.raises(ValueError, match=f"^{message}$"):
        RunSettings.from_env()
    assert cli.main(["run", "t.py"]) == 2
    assert message in capsys.readouterr().err
    monkeypatch.delenv("AGENTICML_RESUME")
    with pytest.raises(cli._UsageError, match="^--resume must be never, allow, or must$"):
        cli._parse_run_options(["--resume=bad", "t.py"])


def test_config_pairs():
    options = ["--config", "lr=1e-3", "--config=amp=true", "--config", "name=resnet",
               "--config", 'quoted="1e-3"', "--config", "lr=0.5", "--config", "data=[1,2]",
               "--config", 'mapping={"a":1}', "--config", "none=null", "--config", "eq=a=b"]
    settings, raw, rest = cli._parse_run_options([*options, "t.py"])
    assert settings.config == {"lr": 0.5, "amp": True, "name": "resnet", "quoted": "1e-3",
                               "data": [1, 2], "mapping": {"a": 1}, "none": None, "eq": "a=b"}
    assert raw == options
    assert rest == ["t.py"]


@pytest.mark.parametrize("value", ["lr", "=1"])
def test_invalid_config_pair(value, capsys):
    with pytest.raises(cli._UsageError, match="--config expects KEY=VALUE"):
        cli._parse_run_options(["--config", value, "t.py"])
    assert cli.main(["run", "--config", value, "t.py"]) == 2
    assert "--config expects KEY=VALUE" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "[NaN]", "1e999"])
def test_nonfinite_config_pair_is_usage_error(value, capsys):
    message = "--config value for 'k' must not be NaN or Infinity"
    with pytest.raises(cli._UsageError, match=f"^{message}$"):
        cli._parse_run_options(["--config", f"k={value}", "t.py"])
    assert cli.main(["run", "--config", f"k={value}", "t.py"]) == 2
    assert message in capsys.readouterr().err


def test_deep_config_pair_falls_back_to_string():
    value = "[" * 10000 + "0" + "]" * 10000
    settings, _, _ = cli._parse_run_options(["--config", f"k={value}", "t.py"])
    assert settings.config == {"k": value}


@pytest.mark.parametrize("source", ["env", "separate", "attached"])
def test_config_file_merge(tmp_path, monkeypatch, source):
    path = tmp_path / "config.json"
    path.write_text('{"lr": 0.1, "batch_size": 32}')
    if source == "env":
        monkeypatch.setenv("AGENTICML_CONFIG_FILE", str(path))
        options = []
    else:
        monkeypatch.setenv("AGENTICML_CONFIG_FILE", str(tmp_path / "missing.json"))
        options = [f"--config-file={path}"] if source == "attached" else ["--config-file", str(path)]
    # CLI file replaces the env file; pairs override file values even when given first.
    options = ["--config=lr=0.5", *options]
    settings, raw, _ = cli._parse_run_options([*options, "t.py"])
    assert settings.config == {"lr": 0.5, "batch_size": 32}
    assert raw == options


@pytest.mark.parametrize("contents", [
    None, "{bad json", "[]", "null", "1", b"\xff",
    '{"k": NaN}', '{"k": Infinity}', '{"k": 1e999}',
    '{"k":' + "[" * 10000 + "0" + "]" * 10000 + "}",
])
@pytest.mark.parametrize("source", ["env", "cli"])
def test_invalid_config_file(tmp_path, monkeypatch, contents, source, capsys):
    path = tmp_path / "bad-config.json"
    if isinstance(contents, bytes):
        path.write_bytes(contents)
    elif contents is not None:
        path.write_text(contents)
    if source == "env":
        monkeypatch.setenv("AGENTICML_CONFIG_FILE", str(path))
        options = []
    else:
        options = ["--config-file", str(path)]
    with pytest.raises(cli._UsageError, match="bad-config.json"):
        cli._parse_run_options([*options, "t.py"])
    assert cli.main(["run", *options, "t.py"]) == 2
    assert str(path) in capsys.readouterr().err


@pytest.mark.parametrize("option, attr", [
    ("--no-system-metrics", "system_metrics"), ("--no-lineage", "lineage"),
    ("--no-code", "track_source"), ("--no-env", "capture_env"),
    ("--all-ranks", "only_main_process"),
])
def test_capture_switches(option, attr):
    settings, raw, _ = cli._parse_run_options([option, "t.py"])
    assert getattr(settings, attr) is False
    assert raw == [option]


@pytest.mark.parametrize("option, attr, env_name, value", [
    ("--system-metrics-interval", "system_metrics_interval", "AGENTICML_SYSTEM_METRICS_INTERVAL", "0.5"),
    ("--flush-interval", "flush_interval", "AGENTICML_FLUSH_INTERVAL", "0.2"),
    ("--max-file-bytes", "max_file_bytes", "AGENTICML_MAX_FILE_BYTES", "42"),
    ("--max-total-bytes", "max_total_bytes", "AGENTICML_MAX_TOTAL_BYTES", "128"),
])
@pytest.mark.parametrize("attached", [False, True])
def test_capture_numbers(monkeypatch, option, attr, env_name, value, attached):
    monkeypatch.setenv(env_name, value)
    assert getattr(RunSettings.from_env(), attr) == float(value)
    options = [f"{option}=3"] if attached else [option, "3"]
    settings, raw, _ = cli._parse_run_options([*options, "t.py"])
    assert getattr(settings, attr) == 3
    assert isinstance(getattr(settings, attr), int if "bytes" in attr else float)
    assert raw == options


def test_cli_size_and_flush_interval_override_env(monkeypatch):
    monkeypatch.setenv("AGENTICML_MAX_FILE_BYTES", "1024")
    monkeypatch.setenv("AGENTICML_FLUSH_INTERVAL", "30")
    settings, _, _ = cli._parse_run_options([
        "--max-file-bytes=4096", "--flush-interval", "0.5", "t.py",
    ])
    assert settings.max_file_bytes == 4096
    assert settings.flush_interval == 0.5


@pytest.mark.parametrize("option", [
    "--system-metrics-interval", "--flush-interval", "--max-file-bytes", "--max-total-bytes",
])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "bad", ""])
def test_invalid_capture_numbers(option, value, capsys):
    with pytest.raises(cli._UsageError, match="must be a positive"):
        cli._parse_run_options([option, value, "t.py"])
    assert cli.main(["run", option, value, "t.py"]) == 2
    assert "must be a positive" in capsys.readouterr().err


@pytest.mark.parametrize("name, value", [
    ("AGENTICML_RUN_ID", "../bad"),
    ("AGENTICML_RESUME", "bad"),
    ("AGENTICML_SYSTEM_METRICS_INTERVAL", "nan"), ("AGENTICML_SYSTEM_METRICS_INTERVAL", "0"),
    ("AGENTICML_FLUSH_INTERVAL", "inf"), ("AGENTICML_FLUSH_INTERVAL", "-1"),
    ("AGENTICML_MAX_FILE_BYTES", "0"), ("AGENTICML_MAX_FILE_BYTES", "1.5"),
    ("AGENTICML_MAX_TOTAL_BYTES", "bad"), ("AGENTICML_MAX_TOTAL_BYTES", "-1"),
])
def test_bad_launch_env(monkeypatch, name, value, capsys):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        RunSettings.from_env()
    with pytest.raises(cli._UsageError):
        cli._parse_run_options(["t.py"])
    assert cli.main(["run", "t.py"]) == 2
    assert "agenticml run: error:" in capsys.readouterr().err


@pytest.mark.parametrize("value, enabled", [
    ("0", False), ("false", False), (" NO ", False), ("off", False),
    ("1", True), ("true", True), ("anything", True),
])
def test_capture_env_flags(monkeypatch, value, enabled):
    for name in ("AGENTICML_SYSTEM_METRICS", "AGENTICML_TRACK_CODE", "AGENTICML_CAPTURE_ENV"):
        monkeypatch.setenv(name, value)
    settings = RunSettings.from_env()
    assert settings.system_metrics is settings.capture_env is enabled
    assert settings.track_source == ("runtime" if enabled else False)
    settings, _, _ = cli._parse_run_options(["--no-system-metrics", "--no-code", "--no-env", "t.py"])
    assert settings.system_metrics is settings.track_source is settings.capture_env is False


@pytest.mark.parametrize("value, enabled", [
    ("1", True), (" TrUe ", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("anything", False),
])
def test_all_ranks_env(monkeypatch, value, enabled):
    monkeypatch.setenv("AGENTICML_ALL_RANKS", value)
    assert RunSettings.from_env().only_main_process is (not enabled)


@pytest.mark.parametrize("option, attr, env_name", [
    ("--track-file", "extra_files", "AGENTICML_TRACK_FILES"),
    ("--source-root", "source_roots", "AGENTICML_SOURCE_ROOTS"),
])
def test_repeatable_paths(monkeypatch, option, attr, env_name):
    paths = ["relative path", "../other"]
    monkeypatch.setenv(env_name, os.pathsep.join(paths))
    assert getattr(RunSettings.from_env(), attr) == paths
    settings, _, _ = cli._parse_run_options(["t.py"])
    assert getattr(settings, attr) == paths
    options = [option, "cli path", f"{option}=./other"]
    settings, raw, _ = cli._parse_run_options([*options, "t.py"])
    assert getattr(settings, attr) == ["cli path", "./other"]
    assert raw == options


def test_options_stop_at_first_non_option():
    settings, raw, rest = cli._parse_run_options(
        ["--project", "demo", "--tags=a,b", "--no-auto-track", "train.py", "--project", "x"]
    )
    assert settings.project == "demo"
    assert settings.tags == ["a", "b"]
    assert settings.auto_track is False
    assert raw == ["--project", "demo", "--tags=a,b", "--no-auto-track"]
    assert rest == ["train.py", "--project", "x"]


def test_double_dash_ends_options():
    settings, _raw, rest = cli._parse_run_options(["--name", "n", "--", "--weird.py", "-v"])
    assert settings.name == "n"
    assert rest == ["--weird.py", "-v"]


def test_options_fall_back_to_env(monkeypatch):
    monkeypatch.setenv("AGENTICML_PROJECT", "from-env")
    monkeypatch.setenv("AGENTICML_TAGS", "x, y")
    monkeypatch.setenv("AGENTICML_AUTO_TRACK", "0")
    monkeypatch.setenv("AGENTICML_NAME", "env-name")
    settings, _raw, _rest = cli._parse_run_options(["t.py"])
    assert settings.name == "env-name"
    settings, _raw, _rest = cli._parse_run_options(["--name", "n", "t.py"])
    assert settings.name == "n"
    assert settings.project == "from-env"
    assert settings.tags == ["x", "y"]
    assert settings.auto_track is False


def test_unknown_option_is_a_usage_error():
    with pytest.raises(cli._UsageError):
        cli._parse_run_options(["--bogus", "t.py"])


@pytest.mark.parametrize("options", [["--task", "asr"], ["--task=asr"]])
def test_task_option(options):
    settings, raw, rest = cli._parse_run_options([*options, "t.py"])
    assert settings.task == "asr"
    assert raw == options
    assert rest == ["t.py"]


def test_task_option_is_normalized():
    settings, _raw, _rest = cli._parse_run_options(["--task", " asr ", "t.py"])
    assert settings.task == "asr"


@pytest.mark.parametrize("value", ["", "a" * 129])
def test_bad_task_is_a_usage_error(value):
    with pytest.raises(cli._UsageError, match="--task must be a non-empty name"):
        cli._parse_run_options(["--task", value, "t.py"])


def test_task_env_is_not_explicit(monkeypatch):
    monkeypatch.setenv("AGENTICML_TASK", "asr")
    settings, _raw, _rest = cli._parse_run_options(["t.py"])
    assert settings.task is None


@pytest.mark.parametrize("options", [["--parent", "run-1"], ["--parent=run-1"]])
def test_parent_option(options):
    settings, raw, rest = cli._parse_run_options([*options, "t.py"])
    assert settings.parent_run_id == "run-1"
    assert raw == options
    assert rest == ["t.py"]


@pytest.mark.parametrize("value", ["", "../bad", "a/b", "a b", "x" * 129])
@pytest.mark.parametrize("attached", [False, True])
def test_bad_parent_is_a_usage_error(value, attached, capsys):
    options = [f"--parent={value}"] if attached else ["--parent", value]
    with pytest.raises(cli._UsageError, match="--parent must be a valid run id"):
        cli._parse_run_options([*options, "t.py"])
    assert cli.main(["run", *options, "t.py"]) == 2
    assert "--parent must be a valid run id" in capsys.readouterr().err


def test_parent_missing_value_is_a_usage_error(capsys):
    with pytest.raises(cli._UsageError, match="--parent expects a value"):
        cli._parse_run_options(["--parent"])
    assert cli.main(["run", "--parent"]) == 2
    assert "--parent expects a value" in capsys.readouterr().err


@pytest.mark.parametrize("value, expected", [("", None), ("env-parent", "env-parent")])
def test_parent_env(monkeypatch, value, expected):
    monkeypatch.setenv("AGENTICML_PARENT", value)
    assert RunSettings.from_env().parent_run_id == expected
    settings, _raw, _rest = cli._parse_run_options(["t.py"])
    assert settings.parent_run_id == expected
    settings, _raw, _rest = cli._parse_run_options(["--parent", "cli-parent", "t.py"])
    assert settings.parent_run_id == "cli-parent"


def test_invalid_parent_env_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setenv("AGENTICML_PARENT", "../bad")
    with pytest.raises(ValueError, match="AGENTICML_PARENT must be a valid run id"):
        RunSettings.from_env()
    with pytest.raises(cli._UsageError, match="AGENTICML_PARENT must be a valid run id"):
        cli._parse_run_options(["t.py"])
    assert cli.main(["run", "t.py"]) == 2
    assert "AGENTICML_PARENT must be a valid run id" in capsys.readouterr().err


@pytest.mark.parametrize("value, expected", [
    (None, False), ("1", True), ("true", True), ("yes", True), ("on", True),
    (" TrUe ", True), (" YES ", True), (" ON ", True), (" 1 ", True),
    ("", False), ("0", False), ("false", False), ("no", False), ("off", False),
    ("anything", False), ("2", False),
])
def test_allow_cross_task_env(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("AGENTICML_ALLOW_CROSS_TASK", value)
    assert RunSettings.from_env().allow_cross_task is expected
    settings, raw, rest = cli._parse_run_options(["--allow-cross-task", "t.py"])
    assert settings.allow_cross_task is True
    assert raw == ["--allow-cross-task"]
    assert rest == ["t.py"]


def test_missing_option_value_is_a_usage_error():
    with pytest.raises(cli._UsageError):
        cli._parse_run_options(["--project"])


def test_module_target():
    assert cli._parse_target(["-m", "pkg.mod", "a"]) == Target("module", "pkg.mod", ["a"])
    assert cli._parse_target(["t.py", "-m", "x"]) == Target("script", "t.py", ["-m", "x"])


@pytest.mark.parametrize("name", ["python", "python3", "python3.12", "/usr/bin/python3", "python.exe"])
def test_python_executable_names(name):
    assert cli._is_python_executable(name)


@pytest.mark.parametrize("name", ["train.py", "pythonic.py", "ipython", "run"])
def test_non_python_names(name):
    assert not cli._is_python_executable(name)


def test_python_command_keeps_interpreter_flags():
    flags, target = cli._parse_python_command(["python", "-u", "-W", "ignore", "t.py", "-u"])
    assert flags == ["-u", "-W", "ignore"]
    assert target == Target("script", "t.py", ["-u"])


def test_python_command_module():
    flags, target = cli._parse_python_command(["python", "-O", "-m", "pkg", "a"])
    assert flags == ["-O"]
    assert target == Target("module", "pkg", ["a"])


def test_python_dash_c_is_rejected():
    with pytest.raises(cli._UsageError):
        cli._parse_python_command(["python", "-c", "print(1)"])


def test_help_exits_zero(capsys):
    assert cli.main(["run", "--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: agenticml run" in out
    assert "--task NAME" in out
    assert "--parent RUN_ID" in out
    assert "--allow-cross-task" in out
    assert "default: entry file path relative to the repo root (absolute outside a repo), or the module name" in out


def test_usage_error_exits_two(capsys):
    assert cli.main(["run", "--bogus", "t.py"]) == 2
    assert "unknown option" in capsys.readouterr().err


# ----- running scripts (subprocess, offline mode) -----


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'proj'\n")
    return root


def _launch(
    project: Path, *args: str, script: str | None = None, name: str = "train.py",
    env: dict | None = None,
):
    if script is not None:
        (project / name).write_text(textwrap.dedent(script))
    out = project.parent / "out"
    process_env = {
        **os.environ,
        "AGENTICML_MODE": "offline",
        "AGENTICML_DIR": str(out),
    }
    process_env.pop("AGENTICML_PROJECT", None)
    process_env.pop("AGENTICML_NAME", None)
    process_env.pop("AGENTICML_TASK", None)
    process_env.pop("AGENTICML_PARENT", None)
    process_env.pop("AGENTICML_ALLOW_CROSS_TASK", None)
    for key in _NEW_LAUNCH_ENV:
        process_env.pop(key, None)
    process_env.update(env or {})
    proc = subprocess.run(
        [sys.executable, "-m", "agenticml", "run", *args],
        cwd=project,
        env=process_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc, _read_runs(out)


def _read_runs(out: Path) -> list[dict]:
    if not (out / "runs").exists():
        return []
    return [RunReader(out, path.name) for path in sorted(
        (out / "runs").iterdir(), key=lambda path: path.stat().st_mtime
    ) if (path / "meta.json").is_file()]


@pytest.mark.parametrize("reexec", [False, True])
def test_resume_round_trip(project, reexec):
    script = """
        import agenticml
        for i in range(3):
            agenticml.log({"loss": i})
    """
    proc, runs = _launch(
        project, "--project=original", "--name=original-name", "--task=original-task",
        "--parent=parent-run", "--config=stored=1", "--config=lr=0.1", "train.py", script=script,
    )
    assert proc.returncode == 0, proc.stderr
    rid = runs[0].run_id
    assert runs[0].last_step() == 2
    target = ["--", sys.executable, "-u", "train.py"] if reexec else ["train.py"]
    proc, runs = _launch(
        project, "--id", rid, "--resume", "must", "--project=changed", "--name=changed",
        "--task=changed", "--parent=changed-parent", "--config=lr=0.5", *target,
    )
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 1
    run = runs[0]
    assert run.run_id == rid
    meta = run.meta()
    assert meta["project"] == "original"
    assert meta["name"] == "original-name"
    assert meta["task"] == "original-task"
    assert meta["parent_run_id"] == "parent-run"
    assert "keeping it instead of parent_run_id=" in proc.stderr
    assert meta["config"]["stored"] == 1
    assert meta["config"]["lr"] == 0.5
    assert meta["status"] == "finished"
    assert run.last_step() == 5
    assert [point[0] for point in run.series("loss")["points"]] == list(range(6))
    assert any(event["kind"] == "resume" for event in run.events())


@pytest.mark.parametrize("env_options", [False, True])
def test_resume_must_missing_does_not_execute(project, env_options):
    options = [] if env_options else ["--id=unknown", "--resume=must"]
    env = {"AGENTICML_RUN_ID": "unknown", "AGENTICML_RESUME": "must"} if env_options else {}
    proc, runs = _launch(project, *options, "train.py", env=env, script="""
        from pathlib import Path
        Path("executed").touch()
    """)
    assert proc.returncode == 2
    assert proc.stderr.strip() == "agenticml run: run unknown not found (--resume must)"
    assert not (project / "executed").exists()
    assert runs == []
    assert not (project.parent / "out").exists()


@pytest.mark.parametrize("options, env", [
    (["--mode=disabled"], {}), ([], {"AGENTICML_MODE": "disabled"}),
])
def test_resume_must_missing_in_disabled_mode(project, options, env):
    proc, runs = _launch(
        project, "--id=unknown", "--resume=must", *options, "train.py", env=env, script="""
            from pathlib import Path
            Path("executed").touch()
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert (project / "executed").exists()
    assert runs == []


@pytest.mark.parametrize("attached", [False, True])
def test_resume_allow_unknown_creates_id_after_reexec(project, attached):
    options = (["--id=chosen-id", "--resume=allow", "--config=lr=0.5"] if attached else
               ["--id", "chosen-id", "--resume", "allow", "--config", "lr=0.5"])
    proc, runs = _launch(
        project, *options, "--", sys.executable, "-u", "train.py", script="""
            import agenticml
            assert agenticml.get_active_run().run_id == "chosen-id"
            assert agenticml.config["lr"] == 0.5
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 1
    assert runs[0].run_id == "chosen-id"
    assert runs[0].meta()["config"]["lr"] == 0.5


@pytest.mark.skipif(os.name == "nt", reason="POSIX run locks")
@pytest.mark.parametrize("mode", ["allow", "must"])
def test_resume_locked_run_does_not_execute(project, mode):
    from agenticml.store import RunWriter

    root = project.parent / "out"
    writer = RunWriter.create(root, run_id="locked-run", meta={"project": "proj"})
    try:
        before = (writer.run_dir / "meta.json").read_bytes()
        proc, runs = _launch(project, "--id=locked-run", f"--resume={mode}", "train.py", script="""
            from pathlib import Path
            Path("executed").touch()
        """)
        assert proc.returncode == 2
        assert proc.stderr.strip() == (
            "agenticml run: cannot resume run locked-run: run locked-run is open in another process"
        )
        assert not (project / "executed").exists()
        assert len(runs) == 1
        assert (writer.run_dir / "meta.json").read_bytes() == before
    finally:
        writer.close()


def test_resume_allow_directory_without_meta_does_not_execute(project):
    directory = project.parent / "out" / "runs" / "incomplete-run"
    directory.mkdir(parents=True)
    proc, runs = _launch(project, "--id=incomplete-run", "--resume=allow", "train.py", script="""
        from pathlib import Path
        Path("executed").touch()
    """)
    assert proc.returncode == 2
    assert proc.stderr.startswith("agenticml run: cannot resume run incomplete-run: ")
    assert not (project / "executed").exists()
    assert runs == []
    assert not (directory / "meta.json").exists()


@pytest.mark.parametrize("mode", ["allow", "must"])
def test_resume_pruned_run_does_not_execute(project, mode):
    from agenticml.store import RunWriter
    from agenticml.store.overview import OVERVIEW_MIN_POINTS

    writer = RunWriter.create(project.parent / "out", run_id="pruned-run")
    writer.write_scalars([(i, {"loss": float(i)}) for i in range(OVERVIEW_MIN_POINTS + 1)])
    writer.finish("finished")
    (writer.run_dir / "scalars" / "0.bin").unlink()
    before = (writer.run_dir / "meta.json").read_bytes()
    proc, _ = _launch(project, "--id=pruned-run", f"--resume={mode}", "train.py", script="""
        from pathlib import Path
        Path("executed").touch()
    """)
    assert proc.returncode == 2
    assert proc.stderr.startswith("agenticml run: cannot resume run pruned-run: ")
    assert "was pruned after sync" in proc.stderr
    assert not (project / "executed").exists()
    assert (writer.run_dir / "meta.json").read_bytes() == before


@pytest.mark.parametrize("resume", [None, "allow", "must"])
@pytest.mark.parametrize("error, detail", [
    (RuntimeError("first line\nsecond line"), "first line"),
    (ValueError(), "ValueError"),
    (RuntimeError("\nsecond line"), "RuntimeError"),
    (FileNotFoundError("meta disappeared"), "meta disappeared"),
    (OSError("store unavailable"), "store unavailable"),
])
def test_init_failure_requires_resume_to_abort(tmp_path, monkeypatch, capsys, resume, error, detail):
    from agenticml import _globals

    script = tmp_path / "train.py"
    marker = tmp_path / "executed"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    root = tmp_path / "store"
    directory = root / "runs" / "run-1"
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text("{}")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(_globals, "Run", fail)
    monkeypatch.setattr(_globals, "_active_run", None)
    monkeypatch.setattr(sys, "argv", sys.argv[:])
    monkeypatch.setattr(sys, "path", sys.path[:])
    options = ["--id=run-1", f"--resume={resume}"] if resume is not None else []
    result = cli.main(["run", "--dir", str(root), *options, str(script)])
    if resume is not None:
        assert result == 2
        assert not marker.exists()
        assert capsys.readouterr().err == f"agenticml run: cannot resume run run-1: {detail}\n"
    else:
        assert result == 0
        assert marker.exists()


@pytest.mark.parametrize("id_expr, resume, warns", [
    ('"other"', "must", True), ('"other"', "never", True),
    ("run.run_id", "must", False), ("run.run_id", "never", False),
    ("None", "never", False), ("None", None, False), ("None", "allow", True),
])
def test_script_init_resume_keeps_launcher_run(project, id_expr, resume, warns):
    proc, runs = _launch(project, "train.py", script=f"""
        import agenticml
        run = agenticml.get_active_run()
        attached = agenticml.init(id={id_expr}, resume={resume!r})
        assert attached is run
    """)
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 1
    assert ("ignoring id=" in proc.stderr) == warns
    if warns:
        assert "(pass --id/--resume to agenticml run)" in proc.stderr


def test_config_file_and_pairs_protect_from_argparse(project):
    (project / "cfg.json").write_text('{"lr": 0.1, "batch_size": 32}')
    proc, runs = _launch(
        project, "--config", "lr=0.5", "--config-file", "cfg.json", "train.py", script="""
            import argparse
            import agenticml
            parser = argparse.ArgumentParser()
            parser.add_argument("--lr", type=float, default=0.01)
            parser.add_argument("--batch-size", type=int, default=8)
            parser.add_argument("--epochs", type=int, default=10)
            parser.parse_args()
            assert agenticml.config["lr"] == 0.5
            assert agenticml.config["batch_size"] == 32
            assert agenticml.config["epochs"] == 10
            agenticml.init(config={"batch_size": 64})
        """,
    )
    assert proc.returncode == 0, proc.stderr
    config = runs[0].meta()["config"]
    assert config["lr"] == 0.5
    assert config["batch_size"] == 64
    assert config["epochs"] == 10


@pytest.mark.parametrize("env_options", [False, True])
def test_capture_disabled(project, env_options):
    proc, runs = _launch(project, "train.py", script="""
        import agenticml
        run = agenticml.get_active_run()
        assert run._system_sampler is not None
        assert run._runtime_collector is not None
        assert run._lineage_enabled
        agenticml.log({"loss": 1})
    """)
    assert proc.returncode == 0, proc.stderr
    default_run = runs[0]
    assert default_run.meta()["env"]
    assert default_run.meta()["lineage"]
    assert default_run.meta()["snapshot_hash"]
    assert default_run.code_manifest()
    assert any(event["kind"] == "code" for event in default_run.events())

    options = [] if env_options else ["--no-system-metrics", "--no-lineage", "--no-code", "--no-env"]
    env = ({"AGENTICML_SYSTEM_METRICS": "0", "AGENTICML_LINEAGE": "0",
            "AGENTICML_TRACK_CODE": "0", "AGENTICML_CAPTURE_ENV": "0"} if env_options else {})
    proc, runs = _launch(project, *options, "train.py", env=env, script="""
        import agenticml
        run = agenticml.get_active_run()
        assert run._system_sampler is None
        assert run._runtime_collector is None
        assert not run._lineage_enabled
        agenticml.log({"loss": 1})
    """)
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 2
    run = runs[1]
    assert run.meta()["env"] == {}
    assert not run.meta().get("lineage")
    assert not run.meta().get("snapshot_hash")
    assert run.code_manifest() == []
    assert not any(event["kind"] == "code" for event in run.events())


@pytest.mark.parametrize("env_options", [False, True])
def test_track_file_in_code_manifest(project, env_options):
    (project / "extra.txt").write_text("tracked content\n")
    options = [] if env_options else ["--track-file", "extra.txt"]
    env = {"AGENTICML_TRACK_FILES": "extra.txt"} if env_options else {}
    proc, runs = _launch(project, *options, "train.py", script="pass\n", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "extra.txt" in {entry["path"] for entry in runs[0].code_manifest()}


@pytest.mark.parametrize("env_options", [False, True])
def test_capture_intervals_roots_and_caps_reach_run(project, env_options):
    source_root = project.parent / "library"
    source_root.mkdir()
    (source_root / "data.txt").write_text("source root content")
    options = ([] if env_options else [
        "--source-root", str(source_root), "--system-metrics-interval=0.5",
        "--flush-interval=0.25", "--max-file-bytes=4096", "--max-total-bytes=16384",
    ])
    env = ({"AGENTICML_SOURCE_ROOTS": str(source_root), "AGENTICML_SYSTEM_METRICS_INTERVAL": "0.5",
            "AGENTICML_FLUSH_INTERVAL": "0.25", "AGENTICML_MAX_FILE_BYTES": "4096",
            "AGENTICML_MAX_TOTAL_BYTES": "16384"} if env_options else {})
    proc, runs = _launch(project, *options, "train.py", env=env, script="""
        from pathlib import Path
        import agenticml
        run = agenticml.get_active_run()
        assert run.flush_interval == 0.25
        assert run._system_sampler.interval == 0.5
        collector = run._runtime_collector
        assert collector.max_file_bytes == 4096
        assert collector.max_total_bytes == 16384
        source_root = Path.cwd().parent / "library"
        assert source_root in collector.roots
        assert (source_root / "data.txt").read_text() == "source root content"
    """)
    assert proc.returncode == 0, proc.stderr
    assert any(entry["path"].endswith("data.txt") for entry in runs[0].code_manifest())


@pytest.mark.parametrize("options, env, tracked", [
    ([], {"RANK": "1", "LOCAL_RANK": "1"}, False),
    (["--all-ranks"], {"RANK": "1", "LOCAL_RANK": "1"}, True),
    ([], {"RANK": "1", "LOCAL_RANK": "1", "AGENTICML_ALL_RANKS": "1"}, True),
])
def test_launcher_all_ranks(project, options, env, tracked):
    proc, runs = _launch(project, *options, "train.py", script="pass\n", env=env)
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == int(tracked)


def test_script_runs_as_main(project):
    (project / "helper.py").write_text("VALUE = 7\n")
    proc, runs = _launch(
        project,
        "--no-auto-track",
        "train.py",
        "--lr",
        "0.1",
        script="""
            import pickle, sys
            from pathlib import Path

            class Config:
                pass

            assert __name__ == "__main__"
            assert Path(__file__).name == "train.py"
            assert sys.argv[1:] == ["--lr", "0.1"], sys.argv
            assert Path(sys.argv[0]).name == "train.py"
            assert sys.path[0] == str(Path(__file__).parent)
            assert isinstance(pickle.loads(pickle.dumps(Config())), Config)
            import helper
            print("ok", helper.VALUE)
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok 7" in proc.stdout
    (run,) = runs
    assert run.meta()["project"] == "proj"
    assert run.meta()["name"] == "train.py"
    assert run.meta()["status"] == "finished"
    code_paths = {f["path"] for f in run.code_manifest()}
    assert {"train.py", "helper.py"} <= code_paths, code_paths


def test_project_and_name_options(project):
    proc, runs = _launch(project, "--project", "demo", "--name", "exp", "--tags", "a,b", "t.py",
                         script="pass\n", name="t.py")
    assert proc.returncode == 0, proc.stderr
    init = runs[0].meta()
    assert (init["project"], init["name"], init["tags"]) == ("demo", "exp", ["a", "b"])


def test_task_option_records_explicit_task(project):
    proc, runs = _launch(project, "--task", "asr", "t.py", script="pass\n", name="t.py")
    assert proc.returncode == 0, proc.stderr
    assert runs[0].meta()["task"] == "asr"
    assert runs[0].meta()["task_source"] == "explicit"


def test_task_option_survives_interpreter_reexec(project):
    proc, runs = _launch(
        project, "--task", "asr", "--", sys.executable, "-u", "t.py",
        script="pass\n", name="t.py",
    )
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 1
    assert runs[0].meta()["task"] == "asr"
    assert runs[0].meta()["task_source"] == "explicit"


@pytest.mark.parametrize("options, env, task, source", [
    ([], {}, "a", "parent"),
    (["--task", "b", "--allow-cross-task"], {}, "b", "explicit"),
    (["--allow-cross-task"], {"AGENTICML_TASK": "env-task"}, "env-task", "env"),
])
def test_parent_task_from_another_folder(project, options, env, task, source):
    for folder in ("a", "b"):
        (project / folder).mkdir()
    proc, runs = _launch(project, "a/train.py", script="pass\n", name="a/train.py")
    assert proc.returncode == 0, proc.stderr
    parent = runs[0].run_id
    assert runs[0].meta()["task"] == "a"
    proc, runs = _launch(
        project, "--parent", parent, *options, "b/train.py", script="pass\n",
        name="b/train.py", env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 2
    meta = runs[1].meta()
    assert meta["parent_run_id"] == meta["lineage"]["parent_run_id"] == parent
    assert meta["lineage"]["parent_inference"] == "explicit"
    assert meta["task"] == meta["lineage"]["task"] == task
    assert meta["task_source"] == source


@pytest.mark.parametrize("task_source", ["--task", "AGENTICML_TASK"])
@pytest.mark.parametrize("parent_source", ["--parent", "AGENTICML_PARENT"])
def test_cross_task_warns_and_runs(project, task_source, parent_source):
    proc, runs = _launch(project, "--task", "a", "parent.py", script="pass\n", name="parent.py")
    assert proc.returncode == 0, proc.stderr
    parent = runs[0].run_id
    options, env = [], {}
    for source, value in ((parent_source, parent), (task_source, "b")):
        if source.startswith("--"):
            options.extend([source, value])
        else:
            env[source] = value
    proc, children = _launch(project, *options, "child.py", name="child.py", env=env, script="""
        from pathlib import Path
        Path("child-ran").touch()
    """)
    assert proc.returncode == 0, proc.stderr
    message = (f"run task 'b' differs from parent run {parent!r} task 'a'; "
               "the lineage link crosses tasks")
    assert proc.stderr.splitlines().count(message) == 1
    assert len(children) == 2
    assert (project / "child-ran").exists()
    meta = children[-1].meta()
    assert meta["task"] == "b"
    assert meta["task_source"] == ("explicit" if task_source == "--task" else "env")
    assert meta["parent_run_id"] == parent


@pytest.mark.parametrize("options, env", [
    (["--mode", "OFFLINE"], {}),
    ([], {"AGENTICML_MODE": "bogus"}),
])
def test_cross_task_warns_in_local_like_modes(project, options, env):
    proc, runs = _launch(project, "--task", "a", "parent.py", script="pass\n", name="parent.py")
    assert proc.returncode == 0, proc.stderr
    parent = runs[0].run_id
    proc, children = _launch(
        project, "--parent", parent, "--task", "b", *options, "child.py",
        name="child.py", env=env, script="""
            from pathlib import Path
            Path("child-ran").touch()
        """,
    )
    assert proc.returncode == 0, proc.stderr
    message = (f"run task 'b' differs from parent run {parent!r} task 'a'; "
               "the lineage link crosses tasks")
    assert proc.stderr.splitlines().count(message) == 1
    assert len(children) == 2
    assert (project / "child-ran").exists()
    meta = children[-1].meta()
    assert meta["task"] == "b"
    assert meta["task_source"] == "explicit"
    assert meta["parent_run_id"] == parent


@pytest.mark.parametrize("options, env, task, source, recorded", [
    (["--task", "a"], {}, "a", "explicit", True),
    (["--task", "a"], {"AGENTICML_TASK": "b"}, "a", "explicit", True),
    ([], {"AGENTICML_TASK": " a "}, "a", "env", True),
    (["--task", "b", "--allow-cross-task"], {}, "b", "explicit", True),
    (["--task", "b"], {"AGENTICML_ALLOW_CROSS_TASK": "1"}, "b", "explicit", True),
    ([], {"AGENTICML_TASK": "x" * 129}, "a", "parent", True),
    (["--mode", "disabled", "--task", "b"], {}, None, None, False),
    (["--task", "b"], {"AGENTICML_MODE": "disabled"}, None, None, False),
])
def test_parent_task_compatible_or_allowed(project, options, env, task, source, recorded):
    proc, runs = _launch(project, "--task", "a", "parent.py", script="pass\n", name="parent.py")
    assert proc.returncode == 0, proc.stderr
    parent = runs[0].run_id
    proc, runs = _launch(
        project, "--parent", parent, *options, "child.py", env=env, name="child.py", script="""
            from pathlib import Path
            Path("child-ran").touch()
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert (project / "child-ran").exists()
    assert "lineage link crosses tasks" not in proc.stderr
    assert len(runs) == (2 if recorded else 1)
    if recorded:
        meta = runs[-1].meta()
        assert meta["task"] == meta["lineage"]["task"] == task
        assert meta["task_source"] == source
        assert meta["parent_run_id"] == meta["lineage"]["parent_run_id"] == parent


def test_parent_without_task_allows_explicit_task(project):
    proc, runs = _launch(project, "parent.py", script="pass\n", name="parent.py")
    assert proc.returncode == 0, proc.stderr
    parent = runs[0].run_id
    assert not runs[0].meta().get("task")
    proc, runs = _launch(
        project, "--parent", parent, "--task", "b", "child.py", name="child.py", script="""
            from pathlib import Path
            Path("child-ran").touch()
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert (project / "child-ran").exists()
    assert "lineage link crosses tasks" not in proc.stderr
    meta = runs[-1].meta()
    assert meta["task"] == "b"
    assert meta["task_source"] == "explicit"
    assert meta["parent_run_id"] == parent


def test_missing_parent_allows_explicit_task(project):
    proc, runs = _launch(
        project, "--parent", "missing", "--task", "b", "child.py",
        script="pass\n", name="child.py",
    )
    assert proc.returncode == 0, proc.stderr
    assert "parent run 'missing' not found" in proc.stderr
    assert "lineage link crosses tasks" not in proc.stderr
    assert len(runs) == 1
    meta = runs[0].meta()
    assert meta["task"] == "b"
    assert meta["parent_run_id"] == meta["lineage"]["parent_run_id"] == "missing"


def test_cross_task_warns_with_explicit_store_same_project(project):
    store = project.parent / "explicit-store"
    proc, _runs = _launch(
        project, "--dir", str(store), "--project", "same", "--task", "a", "parent.py",
        script="pass\n", name="parent.py",
    )
    assert proc.returncode == 0, proc.stderr
    parent = _read_runs(store)[0].run_id
    proc, runs = _launch(
        project, "--dir", str(store), "--project", "same", "--parent", parent, "--task", "b",
        "child.py", script="from pathlib import Path; Path('child-ran').touch()\n", name="child.py",
    )
    assert proc.returncode == 0, proc.stderr
    message = (f"run task 'b' differs from parent run {parent!r} task 'a'; "
               "the lineage link crosses tasks")
    assert proc.stderr.splitlines().count(message) == 1
    assert (project / "child-ran").exists()
    stored = _read_runs(store)
    assert len(stored) == 2
    meta = stored[-1].meta()
    assert meta["task"] == "b"
    assert meta["task_source"] == "explicit"
    assert meta["parent_run_id"] == parent
    assert runs == []


def test_parent_in_another_project_allows_explicit_task(project):
    store = project.parent / "explicit-store"
    proc, _runs = _launch(
        project, "--dir", str(store), "--project", "other", "--task", "a", "parent.py",
        script="pass\n", name="parent.py",
    )
    assert proc.returncode == 0, proc.stderr
    parent = _read_runs(store)[0].run_id
    proc, _runs = _launch(
        project, "--dir", str(store), "--parent", parent, "--task", "b", "child.py",
        script="pass\n", name="child.py",
    )
    assert proc.returncode == 0, proc.stderr
    runs = _read_runs(store)
    assert len(runs) == 2
    meta = runs[-1].meta()
    assert meta["task"] == "b"
    assert meta["parent_run_id"] == parent
    assert "is in another project" in proc.stderr
    assert "lineage link crosses tasks" not in proc.stderr


def test_cross_task_warns_after_interpreter_reexec(project):
    proc, runs = _launch(project, "--task", "a", "parent.py", script="pass\n", name="parent.py")
    assert proc.returncode == 0, proc.stderr
    parent = runs[0].run_id
    proc, runs = _launch(
        project, "--parent", parent, "--task", "b",
        "--", sys.executable, "-u", "child.py", name="child.py", script="""
            from pathlib import Path
            Path("child-ran").touch()
        """,
    )
    assert proc.returncode == 0, proc.stderr
    message = (f"run task 'b' differs from parent run {parent!r} task 'a'; "
               "the lineage link crosses tasks")
    assert proc.stderr.splitlines().count(message) == 1
    assert (project / "child-ran").exists()
    assert len(runs) == 2
    meta = runs[-1].meta()
    assert meta["task"] == "b"
    assert meta["task_source"] == "explicit"
    assert meta["parent_run_id"] == parent


def test_allow_cross_task_survives_interpreter_reexec(project):
    proc, runs = _launch(project, "--task", "a", "parent.py", script="pass\n", name="parent.py")
    assert proc.returncode == 0, proc.stderr
    parent = runs[0].run_id
    proc, runs = _launch(
        project, "--parent", parent, "--task", "b", "--allow-cross-task",
        "--", sys.executable, "-u", "child.py", script="pass\n", name="child.py",
    )
    assert proc.returncode == 0, proc.stderr
    assert "lineage link crosses tasks" not in proc.stderr
    assert len(runs) == 2
    meta = runs[-1].meta()
    assert meta["task"] == "b"
    assert meta["task_source"] == "explicit"
    assert meta["parent_run_id"] == parent


@pytest.mark.parametrize("cli_parent", [False, True])
def test_parent_env_and_cli_precedence_end_to_end(project, cli_parent):
    parents = []
    for name in ("a.py", "b.py"):
        proc, runs = _launch(project, name, script="pass\n", name=name)
        assert proc.returncode == 0, proc.stderr
        parents.append(runs[-1].run_id)
    options = ["--parent", parents[1]] if cli_parent else []
    proc, runs = _launch(
        project, *options, "child.py", script="pass\n", name="child.py",
        env={"AGENTICML_PARENT": parents[0]},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 3
    meta = runs[-1].meta()
    expected = parents[1] if cli_parent else parents[0]
    assert meta["parent_run_id"] == meta["lineage"]["parent_run_id"] == expected
    assert meta["lineage"]["parent_inference"] == "explicit"


@pytest.mark.parametrize("attached", [False, True])
def test_parent_option_survives_interpreter_reexec(project, attached):
    proc, runs = _launch(project, "--task", "a", "a.py", script="pass\n", name="a.py")
    assert proc.returncode == 0, proc.stderr
    parent = runs[0].run_id
    options = [f"--parent={parent}"] if attached else ["--parent", parent]
    proc, runs = _launch(
        project, *options, "--", sys.executable, "-u", "b.py", script="pass\n", name="b.py",
    )
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 2
    assert runs[1].meta()["parent_run_id"] == parent
    assert runs[1].meta()["task_source"] == "parent"


@pytest.mark.parametrize("parent, script_parent, warns", [
    ("launcher-parent", "script-parent", True),
    ("launcher-parent", "launcher-parent", False),
    ("launcher-parent", None, False),
    (None, "script-parent", True),
])
def test_script_init_parent_keeps_launcher_parent(project, parent, script_parent, warns):
    options = ["--parent", parent] if parent else []
    proc, runs = _launch(project, *options, "t.py", name="t.py", script=f"""
        import agenticml
        run = agenticml.init(parent_run_id={script_parent!r})
        assert run.writer._meta["parent_run_id"] == {parent!r}
    """)
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 1
    assert runs[0].meta()["parent_run_id"] == parent
    assert ("keeping it instead of parent_run_id=" in proc.stderr) == warns


def test_task_env_records_env_task(project):
    proc, runs = _launch(
        project, "t.py", script="pass\n", name="t.py", env={"AGENTICML_TASK": "asr"},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 1
    assert runs[0].meta()["task"] == "asr"
    assert runs[0].meta()["task_source"] == "env"


def test_task_option_beats_entry_folder(project):
    (project / "scripts").mkdir()
    proc, runs = _launch(project, "--task", "asr", "scripts/train.py", script="pass\n",
                         name="scripts/train.py")
    assert proc.returncode == 0, proc.stderr
    assert runs[0].meta()["task"] == "asr"
    assert runs[0].meta()["task_source"] == "explicit"


def test_task_option_beats_marker(project):
    (project / "agenticml.toml").write_text('task = "marker"\n')
    proc, runs = _launch(project, "--task", "asr", "t.py", script="pass\n", name="t.py")
    assert proc.returncode == 0, proc.stderr
    assert runs[0].meta()["task"] == "asr"
    assert runs[0].meta()["task_source"] == "explicit"


def test_script_init_task_keeps_explicit_launcher_task(project):
    proc, runs = _launch(project, "--task", "asr", "t.py", name="t.py", script="""
        import agenticml
        run = agenticml.init(task="imagegen")
        assert run.task == "asr"
    """)
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 1
    assert runs[0].meta()["task"] == "asr"
    assert runs[0].meta()["task_source"] == "explicit"
    assert "keeping it instead of task='imagegen'" in proc.stderr


@pytest.mark.parametrize("explicit", [False, True])
def test_script_in_subfolder_name(project, explicit):
    (project / "scripts").mkdir()
    options = ["--name", "x"] if explicit else []
    proc, runs = _launch(project, *options, "scripts/train.py", script="pass\n",
                         name="scripts/train.py")
    assert proc.returncode == 0, proc.stderr
    assert runs[0].meta()["name"] == ("x" if explicit else "scripts/train.py")
    assert runs[0].meta()["task"] == "scripts"
    assert runs[0].meta()["task_source"] == "entry"


@pytest.mark.parametrize("task", ["imagegen", "asr", " asr "])
def test_script_init_task_keeps_launcher_task(project, task):
    (project / "asr").mkdir()
    proc, runs = _launch(project, "asr/train.py", name="asr/train.py", script=f"""
        import agenticml
        run = agenticml.init(task={task!r})
        assert run.task == "asr"
    """)
    assert proc.returncode == 0, proc.stderr
    assert len(runs) == 1
    assert runs[0].meta()["task"] == "asr"
    if task == "imagegen":
        assert "keeping it instead of task='imagegen'" in proc.stderr
    else:
        assert "already started task" not in proc.stderr


def test_unnamed_init_attaches_without_warning(project):
    proc, runs = _launch(project, "--name", "launcher-name", "train.py", script="""
        import agenticml
        run = agenticml.init()
        assert run.name == "launcher-name"
        agenticml.finish()
    """)
    assert proc.returncode == 0, proc.stderr
    assert "keeping it instead of" not in proc.stderr
    assert len(runs) == 1
    assert runs[0].meta()["name"] == "launcher-name"


@pytest.mark.parametrize(
    "name_arg, expected",
    [("", "scripts/train.py"), ("name=''", ""), ("name='explicit'", "explicit")],
)
def test_plain_python_init_name(project, name_arg, expected):
    scripts = project / "scripts"
    scripts.mkdir()
    script = scripts / "train.py"
    script.write_text(textwrap.dedent(f"""
        import agenticml
        run = agenticml.init({name_arg})
        assert run.name == {expected!r}, run.name
        agenticml.finish()
    """))
    out = project.parent / "out"
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENTICML_")}
    env["AGENTICML_DIR"] = str(out)
    proc = subprocess.run([sys.executable, str(script)], cwd=project, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert _read_runs(out)[0].meta()["name"] == expected


def test_plain_python_init_without_main_file(project):
    out = project.parent / "out"
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENTICML_")}
    env["AGENTICML_DIR"] = str(out)
    proc = subprocess.run(
        [sys.executable, "-c", "import agenticml; r = agenticml.init(); assert r.name is None; agenticml.finish()"],
        cwd=project, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert _read_runs(out)[0].meta()["name"] is None


def test_exception_marks_run_crashed(project):
    proc, runs = _launch(project, "train.py", script="raise ValueError('boom')\n")
    assert proc.returncode == 1
    assert "ValueError: boom" in proc.stderr
    assert "launch.py" not in proc.stderr
    final = runs[0].meta()
    assert final["status"] == "crashed"
    assert final["summary"] == {"_exit_code": 1, "_exception": "ValueError"}


@pytest.mark.parametrize(
    "body, code",
    [
        ("raise SystemExit(0)", 0),
        ("raise SystemExit(3)", 3),
        ("raise SystemExit('bad config')", 1),
        ("import sys; sys.exit()", 0),
    ],
)
def test_system_exit_codes(project, body, code):
    proc, runs = _launch(project, "train.py", script=body + "\n")
    assert proc.returncode == code
    final = runs[0].meta()
    assert final["status"] == ("finished" if code == 0 else "crashed")
    if code:
        assert final["summary"]["_exit_code"] == code


def test_keyboard_interrupt(project):
    proc, runs = _launch(project, "train.py", script="raise KeyboardInterrupt\n")
    assert proc.returncode == 130
    assert runs[0].meta()["status"] == "crashed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_sigterm_finalizes_run(project):
    proc, runs = _launch(
        project,
        "train.py",
        script="""
            import os, signal, time
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(10)
        """,
    )
    assert proc.returncode == 143
    final = runs[0].meta()
    assert final["status"] == "crashed"
    assert final["summary"]["_exception"] == "SIGTERM"


def test_script_init_and_finish_attach_to_launcher_run(project):
    proc, runs = _launch(
        project,
        "train.py",
        script="""
            import agenticml
            run = agenticml.init(project="other", name="other", config={"lr": 0.1}, tags=["script"], notes="attached")
            agenticml.log({"acc": 0.5})
            agenticml.finish()
            agenticml.log({"acc": 0.7})
            assert agenticml.get_active_run() is run
            assert run.config["lr"] == 0.1
        """,
    )
    assert proc.returncode == 0, proc.stderr
    (run,) = runs
    assert run.meta()["name"] == "train.py"
    assert len([event for event in run.events() if event["kind"] == "finish"]) == 1
    assert run.meta()["config"]["lr"] == 0.1
    assert run.meta()["tags"] == ["script"]
    assert run.meta()["notes"] == "attached"
    assert run.series("acc")["points"] == [[0, 0.5], [1, pytest.approx(0.7)]]


def test_script_requested_failure_status_is_kept(project):
    proc, runs = _launch(
        project,
        "train.py",
        script="import agenticml\nagenticml.init(project='p', name='n')\nagenticml.finish(status='crashed')\n",
    )
    assert proc.returncode == 0
    assert runs[0].meta()["status"] == "crashed"


def test_module_target_runs_package_main(project):
    pkg = project / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(
        "import sys\nassert __name__ == '__main__'\nprint('pkg', __package__, sys.argv[1:])\n"
    )
    proc, runs = _launch(project, "-m", "pkg", "a", "b")
    assert proc.returncode == 0, proc.stderr
    assert "pkg pkg ['a', 'b']" in proc.stdout
    assert runs[0].meta()["name"] == "pkg/__main__.py"


def test_module_target_uses_resolved_file_name(project):
    pkg = project / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").touch()
    (pkg / "train.py").write_text("pass\n")
    proc, runs = _launch(project, "-m", "pkg.train")
    assert proc.returncode == 0, proc.stderr
    assert runs[0].meta()["name"] == "pkg/train.py"


@pytest.mark.parametrize("origin", [None, "built-in", "frozen", "archive.zip/pkg/train.py"])
def test_module_without_file_keeps_module_name(project, tmp_path, monkeypatch, origin):
    monkeypatch.chdir(tmp_path)
    spec = SimpleNamespace(
        origin=origin, submodule_search_locations=None,
        loader=SimpleNamespace(get_code=lambda name: compile("pass", name, "exec")),
    )
    monkeypatch.setattr("agenticml.launch.importlib.util.find_spec", lambda name: spec)
    monkeypatch.setattr(sys, "path", list(sys.path))
    resolved = _resolve(Target("module", "pkg.train"))
    assert resolved.default_name == "pkg.train"


def test_missing_script_exits_two(project):
    proc, runs = _launch(project, "nope.py")
    assert proc.returncode == 2
    assert "can't open file" in proc.stderr
    assert runs == []


def test_shorthand_with_current_interpreter(project):
    (project / "t.py").write_text("print('hi')\n")
    proc, runs = _launch(project, "--", sys.executable, "-u", "t.py")
    assert proc.returncode == 0, proc.stderr
    assert "hi" in proc.stdout
    assert runs[0].meta()["status"] == "finished"


def test_auto_tracking_under_launcher(project):
    pytest.importorskip("torch")
    proc, runs = _launch(
        project,
        "train.py",
        script="""
            import torch
            from torch.utils.data import DataLoader, TensorDataset

            train_loader = DataLoader(
                TensorDataset(torch.randn(16, 4), torch.randint(0, 2, (16,))), batch_size=4
            )
            net = torch.nn.Linear(4, 2)
            optimizer = torch.optim.SGD(net.parameters(), lr=0.1)
            criterion = torch.nn.CrossEntropyLoss()
            for xb, yb in train_loader:
                optimizer.zero_grad()
                criterion(net(xb), yb).backward()
                optimizer.step()
        """,
    )
    assert proc.returncode == 0, proc.stderr
    files = {f["path"] for f in runs[0].code_manifest()}
    assert {
        "agenticml/model.net.json",
        "agenticml/optimizer.optimizer.json",
        "agenticml/loss.criterion.json",
        "agenticml/dataloader.train.json",
    } <= files, files


def test_torchrun_creates_one_run(project):
    pytest.importorskip("torch.distributed.run")
    (project / "ddp.py").write_text(textwrap.dedent("""
        import os
        from pathlib import Path

        import torch, torch.distributed as dist
        from torch.nn.parallel import DistributedDataParallel as DDP

        Path("rank-" + os.environ["RANK"]).write_text("ok\\n")
        dist.init_process_group("gloo")
        model = DDP(torch.nn.Linear(4, 2))
        optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
        for _ in range(2):
            optimizer.zero_grad()
            model(torch.randn(4, 4)).sum().backward()
            optimizer.step()
        dist.destroy_process_group()
    """))
    out = project.parent / "out"
    proc = subprocess.run(
        [
            sys.executable, "-m", "torch.distributed.run",
            "--standalone", "--nproc_per_node", "2",
            "-m", "agenticml", "run", "--mode", "offline", "--dir", str(out),
            "ddp.py",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert (project / "rank-0").is_file()
    assert (project / "rank-1").is_file()
    run_dirs = [path for path in (out / "runs").iterdir() if (path / "meta.json").is_file()]
    assert len(run_dirs) == 1
    runs = _read_runs(out)
    assert len(runs) == 1
    assert runs[0].meta()["status"] == "finished"


def test_integration_flags_parse(monkeypatch):
    for name in ("AGENTICML_AUTO_METRICS", "AGENTICML_SYNC_TENSORBOARD", "AGENTICML_CAPTURE_ARGS", "AGENTICML_LOG_EVERY"):
        monkeypatch.delenv(name, raising=False)
    settings, _raw, _rest = cli._parse_run_options(["t.py"])
    assert (settings.auto_metrics, settings.sync_tensorboard, settings.capture_args, settings.log_every) == (
        True, True, True, 1,
    )
    settings, _raw, _rest = cli._parse_run_options(
        ["--no-auto-metrics", "--no-tensorboard", "--no-capture-args", "--log-every", "10", "t.py"]
    )
    assert (settings.auto_metrics, settings.sync_tensorboard, settings.capture_args, settings.log_every) == (
        False, False, False, 10,
    )


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_bad_log_every_is_a_usage_error(value):
    with pytest.raises(cli._UsageError):
        cli._parse_run_options(["--log-every", value, "t.py"])


def test_bad_log_every_env_is_a_usage_error(monkeypatch):
    monkeypatch.setenv("AGENTICML_LOG_EVERY", "zero")
    with pytest.raises(cli._UsageError):
        cli._parse_run_options(["t.py"])


def test_launcher_logs_auto_metrics_tensorboard_and_args(project):
    pytest.importorskip("torch")
    pytest.importorskip("tensorboard")
    proc, runs = _launch(
        project,
        "train.py",
        "--lr",
        "0.05",
        script="""
            import argparse
            import agenticml
            import torch
            from torch.utils.tensorboard import SummaryWriter

            parser = argparse.ArgumentParser()
            parser.add_argument("--lr", type=float, default=0.1)
            args = parser.parse_args()

            writer = SummaryWriter("tb")
            net = torch.nn.Linear(4, 2)
            optimizer = torch.optim.SGD(net.parameters(), lr=args.lr)
            for step in range(3):
                optimizer.zero_grad()
                loss = net(torch.randn(2, 4)).pow(2).mean()
                loss.backward()
                optimizer.step()
                writer.add_scalar("train/loss", loss.item(), step)
            writer.add_hparams({"lr": args.lr}, {"hparam/final": 1.5})
            writer.close()
            config = agenticml.get_active_run().config
            assert config["lr"] == 0.05, config
            assert config["_tensorboard/log_dir"] == "tb", config
        """,
    )
    assert proc.returncode == 0, proc.stderr
    (run,) = runs
    steps: dict[int, dict] = {}
    for key in run.keys():
        for step, value in run.series(key)["points"]:
            steps.setdefault(step, {})[key] = value
    assert sorted(steps) == [0, 1, 2]
    for metrics in steps.values():
        assert {"auto/loss", "auto/lr", "auto/epoch", "train/loss"} <= set(metrics)
        assert metrics["auto/loss"] == pytest.approx(metrics["train/loss"])
        assert metrics["auto/lr"] == pytest.approx(0.05)
    assert run.meta()["summary"] == {"hparam/final": 1.5}


def test_launcher_integrations_can_be_disabled(project):
    pytest.importorskip("torch")
    pytest.importorskip("tensorboard")
    proc, runs = _launch(
        project,
        "--no-auto-metrics",
        "--no-tensorboard",
        "--no-capture-args",
        "train.py",
        "--lr",
        "0.05",
        script="""
            import argparse
            import agenticml
            import torch
            from torch.utils.tensorboard import SummaryWriter

            argparse.ArgumentParser().parse_known_args()
            writer = SummaryWriter("tb")
            net = torch.nn.Linear(4, 2)
            optimizer = torch.optim.SGD(net.parameters(), lr=0.1)
            optimizer.zero_grad()
            net(torch.randn(2, 4)).sum().backward()
            optimizer.step()
            writer.add_scalar("x", 1.0, 0)
            writer.close()
            assert agenticml.get_active_run().config == {}
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert runs[0].keys() == []


def test_launcher_captures_hydra_config(project):
    pytest.importorskip("hydra")
    (project / "conf").mkdir()
    (project / "conf" / "config.yaml").write_text("lr: 0.01\nmodel:\n  hidden: 64\n")
    proc, _runs = _launch(
        project,
        "train.py",
        "lr=0.5",
        "model.hidden=128",
        script="""
            import agenticml
            import hydra

            @hydra.main(version_base=None, config_path="conf", config_name="config")
            def main(cfg):
                assert cfg.lr == 0.5

            main()
            config = agenticml.get_active_run().config
            assert config == {
                "lr": 0.5,
                "model": {"hidden": 128},
                "_argv": ["lr=0.5", "model.hidden=128"],
            }, config
        """,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("verbose", [False, True])
def test_store_failure_prints_reason_to_stderr_and_runs_script(tmp_path, monkeypatch, capsys, verbose):
    from agenticml.store import RunWriter
    script = tmp_path / "train.py"
    script.write_text('print("trained")\n')
    def fail(*args, **kwargs):
        raise OSError("store unavailable")
    monkeypatch.setattr(RunWriter, "create", fail)
    options = ["--verbose"] if verbose else []
    assert cli.main(["run", *options, "--dir", str(tmp_path / "store"), str(script)]) == 0
    captured = capsys.readouterr()
    assert "trained" in captured.out
    assert "agenticml: tracking disabled: store unavailable\n" in captured.err

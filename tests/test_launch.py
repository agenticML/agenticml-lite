"""Tests for `agenticml run`: argument parsing and running scripts as __main__."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agenticml import cli
from agenticml.launch import Target
from agenticml.store import RunReader


# ----- argument parsing -----


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
    settings, _raw, _rest = cli._parse_run_options(["--name", "n", "t.py"])
    assert settings.project == "from-env"
    assert settings.tags == ["x", "y"]
    assert settings.auto_track is False


def test_unknown_option_is_a_usage_error():
    with pytest.raises(cli._UsageError):
        cli._parse_run_options(["--bogus", "t.py"])


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
    assert "usage: agenticml run" in capsys.readouterr().out


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


def _launch(project: Path, *args: str, script: str | None = None, name: str = "train.py"):
    if script is not None:
        (project / name).write_text(textwrap.dedent(script))
    out = project.parent / "out"
    env = {
        **os.environ,
        "AGENTICML_MODE": "offline",
        "AGENTICML_DIR": str(out),
    }
    env.pop("AGENTICML_PROJECT", None)
    env.pop("AGENTICML_NAME", None)
    proc = subprocess.run(
        [sys.executable, "-m", "agenticml", "run", *args],
        cwd=project,
        env=env,
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
    )]


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
    assert run.meta()["name"] == "train"
    assert run.meta()["status"] == "finished"
    code_paths = {f["path"] for f in run.code_manifest()}
    assert {"train.py", "helper.py"} <= code_paths, code_paths


def test_project_and_name_options(project):
    proc, runs = _launch(project, "--project", "demo", "--name", "exp", "--tags", "a,b", "t.py",
                         script="pass\n", name="t.py")
    assert proc.returncode == 0, proc.stderr
    init = runs[0].meta()
    assert (init["project"], init["name"], init["tags"]) == ("demo", "exp", ["a", "b"])


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
    assert run.meta()["name"] == "train"
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
    assert runs[0].meta()["name"] == "pkg"


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

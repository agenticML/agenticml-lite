"""Tests for capturing argparse / Hydra hyperparameters into run.config."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from agenticml.integrations import hyperparams


class _FakeRun:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self._explicit_config_keys = set(self.config)
        self._finished = False


@pytest.fixture
def capture(monkeypatch):
    run = _FakeRun()
    monkeypatch.setattr(sys, "argv", ["train.py", "--lr", "0.5", "--data", "/d"])
    hyperparams.install_hyperparam_capture(run)
    yield run
    hyperparams.uninstall_hyperparam_capture(run)


def _parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lr", type=float, default=0.1)
    parser.add_argument("--data", type=Path, default=Path("x"))
    parser.add_argument("--layers", type=int, nargs="+", default=[1, 2])
    return parser


def test_parse_args_of_sys_argv_is_captured(capture):
    _parser().parse_args()
    assert capture.config == {
        "lr": 0.5,
        "data": "/d",
        "layers": [1, 2],
        "_argv": ["--lr", "0.5", "--data", "/d"],
    }


def test_explicit_lists_are_not_captured(capture):
    _parser().parse_args(["--lr", "9"])
    assert capture.config == {}


def test_explicit_config_wins(capture):
    capture.config["lr"] = 123
    capture._explicit_config_keys.add("lr")
    _parser().parse_args()
    assert capture.config["lr"] == 123


def test_script_set_values_are_not_overwritten(capture):
    capture.config["data"] = "mine"
    _parser().parse_args()
    assert capture.config["data"] == "mine"


def test_later_parse_replaces_earlier_capture(capture, monkeypatch):
    parser = _parser()
    parser.parse_known_args()
    monkeypatch.setattr(sys, "argv", ["train.py", "--lr", "0.7"])
    parser.parse_args()
    assert capture.config["lr"] == 0.7


def test_subparsers_are_merged_into_top_level(capture, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["train.py", "fit", "--epochs", "3"])
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    fit = sub.add_parser("fit")
    fit.add_argument("--epochs", type=int)
    parser.parse_args()
    assert capture.config["cmd"] == "fit"
    assert capture.config["epochs"] == 3


def test_finished_run_is_ignored(capture):
    capture._finished = True
    _parser().parse_args()
    assert capture.config == {}


def test_uninstall_restores_argparse():
    original = argparse.ArgumentParser.parse_known_args
    run = _FakeRun()
    hyperparams.install_hyperparam_capture(run)
    assert argparse.ArgumentParser.parse_known_args is not original
    hyperparams.uninstall_hyperparam_capture(run)
    assert argparse.ArgumentParser.parse_known_args is original


def test_exact_secret_segments_and_camel_case():
    public = {key: "keep" for key in ("tokenizer", "max_tokens", "author", "auth_mode", "use-auth", "monkey")}
    secrets = {key: "hide" for key in ("hfToken", "apiKey", "APIKey", "access_key", "privateKey",
                                        "authToken", "client_secret", "pwd", "PAT", "credentials", "bearer")}
    assert hyperparams.redact_config({**public, **secrets}) == {**public, **dict.fromkeys(secrets, "***")}


@pytest.mark.parametrize("password", [["-p", "x"], ["-px"], ["-p=x"]])
def test_parser_redacts_long_short_and_attached_values(capture, monkeypatch, password):
    argv = ["--tokenizer", "vocab", "--max_tokens", "100", "--author", "me", "--auth_mode", "local",
            "--api_key", "x", "--hf-token", "x", *password, "--db-password=x", "--use-auth", "positional"]
    monkeypatch.setattr(sys, "argv", ["train.py", *argv])
    parser = argparse.ArgumentParser()
    for flag in ("tokenizer", "max_tokens", "author", "auth_mode", "api_key", "hf-token", "db-password"):
        parser.add_argument("--" + flag)
    parser.add_argument("-p", dest="password")
    parser.add_argument("--use-auth", action="store_true")
    parser.add_argument("position")
    parser.parse_args()
    assert capture.config["tokenizer"] == "vocab"
    assert capture.config["max_tokens"] == "100"
    assert capture.config["author"] == "me"
    assert capture.config["auth_mode"] == "local"
    assert capture.config["use_auth"] is True
    assert capture.config["position"] == "positional"
    for key in ("api_key", "hf_token", "password", "db_password"):
        assert capture.config[key] == "***"
    redacted_password = ["-p", "***"] if len(password) == 2 else ["-p=***"] if "=" in password[0] else ["-p***"]
    assert capture.config["_argv"] == [*argv[:8], "--api_key", "***", "--hf-token", "***",
                                       *redacted_password, "--db-password=***", "--use-auth", "positional"]


def test_secret_boolean_flag_does_not_hide_position(capture, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["train.py", "--token", "position"])
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", action="store_true")
    parser.add_argument("position")
    parser.parse_args()
    assert capture.config["_argv"] == ["--token", "position"]


def test_no_parser_only_redacts_equals_values():
    argv = ["--password", "position", "-psecret", "--token=x", "--max_tokens=10", "--auth_mode=local"]
    assert hyperparams._redact_argv(argv) == [*argv[:3], "--token=***", *argv[4:]]


def test_parser_redacts_all_values_of_multi_value_secret():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokens", dest="credentials", nargs="+")
    parser.add_argument("--epochs")
    assert hyperparams._redact_argv(["--tokens", "a", "b", "--epochs", "2"], parser) == [
        "--tokens", "***", "***", "--epochs", "2"]

"""Tests for running a callback after a module is imported."""

from __future__ import annotations

import sys

from agenticml import _import_hooks


def _module(tmp_path, monkeypatch, name):
    (tmp_path / f"{name}.py").write_text("VALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)


def test_callback_runs_after_import(tmp_path, monkeypatch):
    _module(tmp_path, monkeypatch, "agml_hook_mod")
    seen = []
    _import_hooks.when_imported("agml_hook_mod", lambda m: seen.append(m.VALUE))
    assert seen == []
    import agml_hook_mod

    assert seen == [1]
    assert agml_hook_mod.__loader__ is agml_hook_mod.__spec__.loader
    assert type(agml_hook_mod.__loader__).__name__ != "_NotifyingLoader"
    assert _import_hooks._FINDER is None


def test_callback_runs_now_if_already_imported():
    seen = []
    _import_hooks.when_imported("json", lambda m: seen.append(m.__name__))
    assert seen == ["json"]


def test_cancel_prevents_callback(tmp_path, monkeypatch):
    _module(tmp_path, monkeypatch, "agml_hook_cancel")
    seen = []

    def callback(module):
        seen.append(module)

    _import_hooks.when_imported("agml_hook_cancel", callback)
    _import_hooks.cancel("agml_hook_cancel", callback)
    import agml_hook_cancel  # noqa: F401

    assert seen == []
    assert _import_hooks._FINDER is None


def test_failing_callback_does_not_break_import(tmp_path, monkeypatch):
    _module(tmp_path, monkeypatch, "agml_hook_fail")

    def callback(module):
        raise RuntimeError("boom")

    _import_hooks.when_imported("agml_hook_fail", callback)
    import agml_hook_fail

    assert agml_hook_fail.VALUE == 1

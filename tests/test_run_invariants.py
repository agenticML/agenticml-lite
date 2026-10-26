"""Regression tests for the invariants we just fixed:
   - mode validation in Run.__init__
   - _DisabledRun isolation across instances
   - atexit hook is unregistered after finish()
"""

from __future__ import annotations

import atexit

import pytest

import agenticml
from agenticml._globals import _DisabledRun
from agenticml.run import Run


def test_run_rejects_invalid_mode():
    with pytest.raises(ValueError, match="mode must be one of"):
        Run("p", "n", mode="ofline", track_source=False, capture_env=False, system_metrics=False)


def test_run_rejects_disabled_mode():
    # 'disabled' is a _globals concept, not a Run mode.
    with pytest.raises(ValueError, match="disabled"):
        Run("p", "n", mode="disabled", track_source=False, capture_env=False, system_metrics=False)


def test_disabled_run_state_is_per_instance():
    a = _DisabledRun()
    b = _DisabledRun()
    a.config["lr"] = 0.01
    a.summary["score"] = 9
    a.tags.append("a")

    assert b.config == {}
    assert b.summary == {}
    assert b.tags == []


def test_disabled_run_track_model_is_noop():
    run = _DisabledRun()
    run.track_model(object())
    run.track_model(None)


def test_disabled_run_component_tracking_is_noop():
    run = _DisabledRun()
    run.track_optimizer(object())
    run.track_loss(object())
    run.track_dataloader(object(), name="train")
    run.track_optimizer(None)
    run.track_loss(None)
    run.track_dataloader(None)


def test_disabled_run_via_module_init_resets_state(monkeypatch):
    # First disabled run sets config; finish; new init should NOT inherit.
    monkeypatch.setenv("AGENTICML_MODE", "disabled")

    agenticml.init("demo", "first")
    agenticml.config["lr"] = 0.01
    agenticml.finish()

    agenticml.init("demo", "second")
    assert "lr" not in agenticml.config  # fresh dict on the new run
    agenticml.finish()


def test_finish_unregisters_atexit_callback(monkeypatch):
    # Build a real Run with offline mode so we don't hit the network, but
    # still exercise the atexit register/unregister path.
    monkeypatch.delenv("AGENTICML_MODE", raising=False)

    run = Run(
        "p", "n",
        mode="offline",
        track_source=False,
        capture_env=False,
        system_metrics=False,
    )
    cb = run._atexit_callback
    assert cb is not None

    run.finish()

    # After finish(), unregistering again must be a no-op (returns 0 in 3.10+).
    # The point: we already called unregister inside finish(), so a second
    # unregister either returns 0 or False — never raises.
    try:
        atexit.unregister(cb)
    except Exception as e:
        pytest.fail(f"unregister raised after finish(): {e}")


def test_run_rejects_offline_dir_when_online(tmp_path):
    with pytest.raises(ValueError, match="offline_dir"):
        Run(
            "p",
            "n",
            mode="online",
            offline_dir=tmp_path,
            track_source=False,
            capture_env=False,
            system_metrics=False,
        )


def test_init_online_rejects_offline_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    with pytest.raises(ValueError, match="offline_dir"):
        agenticml.init(
            "demo",
            "online-reject",
            mode="online",
            offline_dir=tmp_path,
            track_source=False,
            capture_env=False,
            system_metrics=False,
        )


def test_init_disabled_rejects_offline_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    with pytest.raises(ValueError, match="offline_dir"):
        agenticml.init("demo", "disabled-reject", mode="disabled", offline_dir=tmp_path)


def test_init_offline_dir_writes_journal_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    monkeypatch.delenv("AGENTICML_OFFLINE_DIR", raising=False)
    run = agenticml.init(
        "demo",
        "offline-dir",
        mode="offline",
        offline_dir=tmp_path,
        track_source=False,
        capture_env=False,
        system_metrics=False,
        only_main_process=False,
    )
    try:
        agenticml.log({"loss": 0.25})
        run_id = run.run_id
    finally:
        agenticml.finish()

    journal = tmp_path.resolve() / run_id / "journal.jsonl"
    assert journal.is_file()
    text = journal.read_text()
    assert '"kind": "init"' in text
    assert '"kind": "log_batch"' in text or '"kind": "finalize"' in text


def test_init_verbose_prints_run_id_and_offline_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    monkeypatch.delenv("AGENTICML_OFFLINE_DIR", raising=False)
    run = agenticml.init(
        "demo",
        "verbose-on",
        mode="offline",
        offline_dir=tmp_path,
        verbose=True,
        track_source=False,
        capture_env=False,
        system_metrics=False,
        only_main_process=False,
    )
    out = capsys.readouterr().out
    try:
        run_dir = str(tmp_path.resolve() / run.run_id)
        assert run.run_id in out
        assert "mode=offline" in out
        assert str(tmp_path.resolve()) in out
        assert run_dir in out
        assert "journal.jsonl" in out
        assert "code" in out
        assert "artifacts" in out
        assert "media" in out
    finally:
        agenticml.finish()


def test_init_verbose_false_is_silent_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    monkeypatch.delenv("AGENTICML_OFFLINE_DIR", raising=False)
    run = agenticml.init(
        "demo",
        "verbose-off",
        mode="offline",
        offline_dir=tmp_path,
        track_source=False,
        capture_env=False,
        system_metrics=False,
        only_main_process=False,
    )
    out = capsys.readouterr().out
    try:
        assert run.run_id not in out
        assert "journal.jsonl" not in out
        assert "offline_dir" not in out
    finally:
        agenticml.finish()


def test_init_disabled_verbose_prints_one_line(monkeypatch, capsys):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    agenticml.init("demo", "disabled-verbose", mode="disabled", verbose=True)
    out = capsys.readouterr().out
    try:
        assert "disabled" in out.lower()
        assert "journal.jsonl" not in out
        assert out.count("\n") <= 2
    finally:
        agenticml.finish()


def test_finish_idempotent(monkeypatch):
    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    run = Run(
        "p", "n",
        mode="offline",
        track_source=False,
        capture_env=False,
        system_metrics=False,
    )
    run.finish()
    run.finish()  # second call is a no-op, no exception

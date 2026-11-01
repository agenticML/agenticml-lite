"""CLI commands that edit a finished run."""

from __future__ import annotations

import io
import os
import time

import pytest

from agenticml import cli
from agenticml.store import OVERVIEW_MIN_POINTS, RunWriter, list_runs
from agenticml.store.notes import NoteConflict, read_note
from agenticml.store.reader import RunReader


def _run(argv):
    try:
        return cli.main(argv)
    except SystemExit as exc:
        return 0 if exc.code is None else exc.code


def _finished(root, run_id="edit-run", summary=None, **meta):
    writer = RunWriter.create(root, run_id=run_id, meta={"project": "p", **meta})
    writer.finish("finished", {"best": 0.1} if summary is None else summary)
    return writer


def _events(root, run_id):
    return RunReader(root, run_id).events()


def _age_lock(writer, seconds=300):
    lock = writer.run_dir / "run.lock"
    old = time.time() - seconds
    os.utime(lock, (old, old))


def _dead_status():
    return "stale" if os.name == "nt" else "crashed"


def test_help_lists_amend_commands(capsys):
    assert _run(["-h"]) == 0
    output = capsys.readouterr().out
    for command in ("edit", "artifact", "finish", "note"):
        assert command in output


def test_note_help_says_local_only(capsys):
    assert _run(["note", "-h"]) == 0
    output = capsys.readouterr().out.lower()
    assert "not synced" in output
    assert "dashboard" in output


def test_edit_updates_meta_and_events(tmp_path, capsys):
    writer = RunWriter.create(
        tmp_path,
        run_id="ed",
        meta={
            "tags": ["a"],
            "notes": "n",
            "group": "g",
            "job_type": "train",
            "config": {"lr": 0.1},
        },
    )
    writer.finish("finished", {"best": 0.1, "acc": 0.2})
    assert _run([
        "edit", "ed", "--dir", str(tmp_path),
        "--tag", "b", "--untag", "a",
        "--notes", "nn",
        "--group", "gg",
        "--job-type", "eval",
        "--config", "lr=0.2", "--config", "amp=true",
        "--summary", "best=1",
        "--unset-summary", "acc",
    ]) == 0
    out = capsys.readouterr().out
    meta = RunReader(tmp_path, "ed").meta()
    assert meta["tags"] == ["b"]
    assert meta["notes"] == "nn"
    assert meta["group"] == "gg"
    assert meta["job_type"] == "eval"
    assert meta["config"] == {"lr": 0.2, "amp": True}
    assert meta["summary"] == {"best": 1}
    events = _events(tmp_path, "ed")
    updates = [event for event in events if event["kind"] == "meta_update"]
    assert len(updates) == 1
    assert updates[0]["fields"] == {
        "tags": ["b"],
        "notes": "nn",
        "group": "gg",
        "job_type": "eval",
        "summary": {"best": 1},
    }
    configs = [event for event in events if event["kind"] == "config_update"]
    assert len(configs) == 1
    assert configs[0]["config"] == {"lr": 0.2, "amp": True}
    assert "tags: a -> b" in out
    assert "summary.best: 1" in out
    assert "summary.acc:" in out
    assert "config.lr: 0.2" in out


def test_edit_nothing_to_change(tmp_path, capsys):
    writer = _finished(tmp_path, tags=["a"], config={"k": 1}, summary={"k": 1})
    size = (writer.run_dir / "events.jsonl").stat().st_size
    assert _run(["edit", writer.run_id, "--tag", "a", "--dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "nothing to change"
    assert (writer.run_dir / "events.jsonl").stat().st_size == size
    assert _run(["edit", writer.run_id, "--unset-summary", "missing", "--dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "nothing to change"
    assert "is not set" in captured.err
    assert (writer.run_dir / "events.jsonl").stat().st_size == size
    assert _run(["edit", writer.run_id, "--config", "k=1", "--dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "nothing to change"
    assert (writer.run_dir / "events.jsonl").stat().st_size == size
    assert _run(["edit", writer.run_id, "--summary", "k=1", "--dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "nothing to change"
    assert (writer.run_dir / "events.jsonl").stat().st_size == size


def test_edit_tags_same_notes_omits_notes_from_event(tmp_path):
    writer = _finished(tmp_path, run_id="tags-notes", tags=["a"], notes="n")
    assert _run([
        "edit", writer.run_id, "--tag", "b", "--notes", "n", "--dir", str(tmp_path),
    ]) == 0
    updates = [event for event in _events(tmp_path, writer.run_id) if event["kind"] == "meta_update"]
    assert len(updates) == 1
    assert updates[0]["fields"] == {"tags": ["a", "b"]}


def test_edit_notes_none_and_empty_are_equal(tmp_path, capsys):
    writer = _finished(tmp_path, run_id="none-notes", notes=None)
    size = (writer.run_dir / "events.jsonl").stat().st_size
    assert _run(["edit", writer.run_id, "--notes", "", "--dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "nothing to change"
    assert (writer.run_dir / "events.jsonl").stat().st_size == size
    assert RunReader(tmp_path, writer.run_id).meta().get("notes") is None


def test_edit_stale_running_does_not_refresh_heartbeat(tmp_path):
    writer = RunWriter.create(tmp_path, run_id="stale")
    writer.close()
    _age_lock(writer)
    assert _run(["edit", "stale", "--tag", "x", "--dir", str(tmp_path)]) == 0
    runs = list_runs(tmp_path)
    assert runs[0]["effective_status"] == _dead_status()
    assert RunReader(tmp_path, "stale").meta()["tags"] == ["x"]


def test_edit_missing_run_lock_reads_as_dead(tmp_path):
    writer = RunWriter.create(tmp_path, run_id="copied")
    writer.close()
    (writer.run_dir / "run.lock").unlink()
    assert _run(["edit", "copied", "--tag", "x", "--dir", str(tmp_path)]) == 0
    assert list_runs(tmp_path)[0]["effective_status"] == _dead_status()
    assert RunReader(tmp_path, "copied").meta()["tags"] == ["x"]


@pytest.mark.skipif(os.name == "nt", reason="Windows has no-op run locks")
def test_edit_artifact_finish_reject_open_run(tmp_path, capsys):
    live = RunWriter.create(tmp_path, run_id="live")
    path = tmp_path / "weights.bin"
    path.write_bytes(b"weights")
    events_size = (live.run_dir / "events.jsonl").stat().st_size
    meta_before = (live.run_dir / "meta.json").read_bytes()
    assert _run(["edit", "live", "--tag", "x", "--dir", str(tmp_path)]) == 1
    assert "still running" in capsys.readouterr().err
    assert _run(["artifact", "add", "live", str(path), "--dir", str(tmp_path)]) == 1
    assert "still running" in capsys.readouterr().err
    assert _run(["finish", "live", "--status", "finished", "--dir", str(tmp_path)]) == 1
    assert "still running" in capsys.readouterr().err
    assert (live.run_dir / "events.jsonl").stat().st_size == events_size
    assert (live.run_dir / "meta.json").read_bytes() == meta_before
    live.close()


def test_edit_usage_errors(tmp_path, capsys):
    writer = _finished(tmp_path)
    assert _run(["edit", writer.run_id, "--dir", str(tmp_path)]) == 2
    assert _run(["edit", writer.run_id, "--group", "", "--dir", str(tmp_path)]) == 2
    assert "cannot be cleared" in capsys.readouterr().err
    assert _run(["edit", writer.run_id, "--tag", "", "--dir", str(tmp_path)]) == 2
    assert "--tag must not be empty" in capsys.readouterr().err
    assert _run(["edit", writer.run_id, "--config", "novalue", "--dir", str(tmp_path)]) == 2
    capsys.readouterr()
    assert _run(["edit", writer.run_id, "--config", "k=NaN", "--dir", str(tmp_path)]) == 2


def test_edit_untag_removes_every_occurrence(tmp_path):
    writer = _finished(tmp_path, run_id="dup-tags", tags=["x", "y", "x"])
    assert _run(["edit", writer.run_id, "--untag", "x", "--dir", str(tmp_path)]) == 0
    assert RunReader(tmp_path, writer.run_id).meta()["tags"] == ["y"]


def test_edit_missing_and_bad_id(tmp_path, capsys):
    assert _run(["edit", "missing-run", "--tag", "x", "--dir", str(tmp_path)]) == 1
    assert "no run" in capsys.readouterr().err
    assert _run(["edit", "../x", "--tag", "x", "--dir", str(tmp_path)]) == 1
    assert "invalid run id" in capsys.readouterr().err


def test_artifact_add_reference_and_copy(tmp_path, capsys):
    writer = _finished(tmp_path, run_id="art")
    path = tmp_path / "model.bin"
    path.write_bytes(b"weights")
    assert _run(["artifact", "add", "art", str(path), "--dir", str(tmp_path)]) == 0
    assert "recorded artifact model v1 (by reference) in art" in capsys.readouterr().out
    first = [event for event in _events(tmp_path, "art") if event["kind"] == "artifact"][-1]
    assert first["name"] == "model"
    assert first["type"] == "file"
    assert first["version"] == 1
    assert first["copy"] is False
    assert first["sha256"] is None
    assert first["metadata"] == {}
    assert _run([
        "artifact", "add", "art", str(path), "--copy", "--name", "weights",
        "--type", "model", "--metadata", '{"k": 1}', "--dir", str(tmp_path),
    ]) == 0
    assert "recorded artifact weights v1 (copied) in art" in capsys.readouterr().out
    copied = [event for event in _events(tmp_path, "art") if event["kind"] == "artifact"][-1]
    assert copied["name"] == "weights"
    assert copied["type"] == "model"
    assert copied["version"] == 1
    assert copied["copy"] is True
    assert copied["sha256"]
    assert copied["metadata"] == {"k": 1}
    assert _run(["artifact", "add", "art", str(path), "--copy", "--name", "weights", "--dir", str(tmp_path)]) == 0
    again = [event for event in _events(tmp_path, "art") if event["kind"] == "artifact"][-1]
    assert again["version"] == 2
    assert _run(["artifact", "add", "art", str(path), "--name", "", "--dir", str(tmp_path)]) == 0
    empty_name = [event for event in _events(tmp_path, "art") if event["kind"] == "artifact"][-1]
    assert empty_name["name"] == "model"
    assert empty_name["version"] == 2


def test_artifact_usage_and_missing_file(tmp_path, capsys):
    writer = _finished(tmp_path, run_id="art")
    path = tmp_path / "model.bin"
    path.write_bytes(b"weights")
    size = (writer.run_dir / "events.jsonl").stat().st_size
    assert _run(["artifact"]) == 2
    assert _run(["artifact", "add", "art", str(path), "--metadata", "[]", "--dir", str(tmp_path)]) == 2
    assert _run(["artifact", "add", "art", str(tmp_path / "missing.bin"), "--dir", str(tmp_path)]) == 1
    assert "no such file" in capsys.readouterr().err
    assert (writer.run_dir / "events.jsonl").stat().st_size == size
    assert _run(["artifact", "add", "art", str(tmp_path), "--dir", str(tmp_path)]) == 1
    assert (writer.run_dir / "events.jsonl").stat().st_size == size
    assert _run(["artifact", "add", "../x", str(path), "--dir", str(tmp_path)]) == 1
    assert "invalid run id" in capsys.readouterr().err
    assert (writer.run_dir / "events.jsonl").stat().st_size == size


def test_finish_stale_running_preserves_summary(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="dead")
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(OVERVIEW_MIN_POINTS + 1)])
    writer.set_summary({"best": 0.42, "kept": True})
    writer.close()
    _age_lock(writer)
    assert _run(["finish", "dead", "--status", "finished", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.strip() == f"dead: {_dead_status()} -> finished"
    meta = RunReader(tmp_path, "dead").meta()
    assert meta["status"] == "finished"
    assert meta["summary"] == {"best": 0.42, "kept": True}
    assert meta["finished_at"]
    assert (writer.run_dir / "scalars" / "0.ovw").is_file()
    assert list_runs(tmp_path)[0]["effective_status"] == "finished"


def test_finish_crashed_to_finished_and_already_done(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="crashed")
    writer.finish("crashed", {"best": 1})
    assert _run(["finish", "crashed", "--status", "finished", "--dir", str(tmp_path)]) == 0
    assert RunReader(tmp_path, "crashed").meta()["status"] == "finished"
    capsys.readouterr()
    events_size = (writer.run_dir / "events.jsonl").stat().st_size
    meta_before = (writer.run_dir / "meta.json").read_bytes()
    assert _run(["finish", "crashed", "--status", "finished", "--dir", str(tmp_path)]) == 0
    assert "already finished" in capsys.readouterr().out
    assert (writer.run_dir / "events.jsonl").stat().st_size == events_size
    assert (writer.run_dir / "meta.json").read_bytes() == meta_before


def test_finish_pruned_and_missing_status(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="pruned")
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(OVERVIEW_MIN_POINTS + 1)])
    writer.finish("finished")
    (writer.run_dir / "scalars" / "0.bin").unlink()
    before = {path: path.read_bytes() for path in writer.run_dir.rglob("*") if path.is_file()}
    assert _run(["finish", "pruned", "--status", "crashed", "--dir", str(tmp_path)]) == 1
    assert "was pruned after sync" in capsys.readouterr().err
    after = {path: path.read_bytes() for path in writer.run_dir.rglob("*") if path.is_file()}
    assert before == after
    assert _run(["finish", "pruned", "--dir", str(tmp_path)]) == 2
    assert _run(["finish", "../x", "--status", "finished", "--dir", str(tmp_path)]) == 1
    assert "invalid run id" in capsys.readouterr().err


def test_edit_and_artifact_reject_pruned(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="pruned-edit")
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(OVERVIEW_MIN_POINTS + 1)])
    writer.finish("finished")
    (writer.run_dir / "scalars" / "0.bin").unlink()
    events_before = (writer.run_dir / "events.jsonl").read_bytes()
    meta_before = (writer.run_dir / "meta.json").read_bytes()
    path = tmp_path / "weights.bin"
    path.write_bytes(b"weights")
    assert _run(["edit", "pruned-edit", "--tag", "x", "--dir", str(tmp_path)]) == 1
    assert "run pruned-edit was pruned after sync; edits would not reach the server" in capsys.readouterr().err
    assert (writer.run_dir / "events.jsonl").read_bytes() == events_before
    assert (writer.run_dir / "meta.json").read_bytes() == meta_before
    assert _run(["artifact", "add", "pruned-edit", str(path), "--dir", str(tmp_path)]) == 1
    assert "run pruned-edit was pruned after sync; edits would not reach the server" in capsys.readouterr().err
    assert (writer.run_dir / "events.jsonl").read_bytes() == events_before
    assert (writer.run_dir / "meta.json").read_bytes() == meta_before


def test_artifact_and_finish_keep_lock_mtime(tmp_path):
    writer = _finished(tmp_path, run_id="lock-mtime")
    _age_lock(writer)
    lock = writer.run_dir / "run.lock"
    before = lock.stat().st_mtime_ns
    path = tmp_path / "weights.bin"
    path.write_bytes(b"weights")
    assert _run(["artifact", "add", "lock-mtime", str(path), "--dir", str(tmp_path)]) == 0
    assert lock.stat().st_mtime_ns == before
    assert _run(["finish", "lock-mtime", "--status", "crashed", "--dir", str(tmp_path)]) == 0
    assert lock.stat().st_mtime_ns == before


def test_note_read_sources_and_writes(tmp_path, monkeypatch, capsys):
    none = _finished(tmp_path, run_id="none")
    assert _run(["note", "none", "--dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""

    launch = RunWriter.create(tmp_path, run_id="launch", meta={"notes": "from init"})
    launch.finish("finished")
    assert _run(["note", "launch", "--dir", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert out == "from init\n"
    assert "(launch note from agenticml.init)" in err

    assert _run(["note", "launch", "--set", "hello\n", "--dir", str(tmp_path)]) == 0
    assert read_note(tmp_path, "launch")["text"] == "hello\n"
    assert read_note(tmp_path, "launch")["source"] == "file"
    assert _run(["note", "launch", "--dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "hello\n"

    note_path = tmp_path / "note.txt"
    note_path.write_text("from file", encoding="utf-8")
    assert _run(["note", "launch", "--file", str(note_path), "--dir", str(tmp_path)]) == 0
    assert read_note(tmp_path, "launch")["text"] == "from file"

    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert _run(["note", "launch", "--file", "-", "--dir", str(tmp_path)]) == 0
    assert read_note(tmp_path, "launch")["text"] == "from stdin"

    assert _run(["note", "launch", "--clear", "--dir", str(tmp_path)]) == 0
    cleared = read_note(tmp_path, "launch")
    assert cleared["text"] == ""
    assert cleared["source"] == "file"

    live = RunWriter.create(tmp_path, run_id="live")
    assert _run(["note", "live", "--set", "while live", "--dir", str(tmp_path)]) == 0
    assert read_note(tmp_path, "live")["text"] == "while live"
    live.close()


def test_note_conflict_and_errors(tmp_path, monkeypatch, capsys):
    writer = _finished(tmp_path, run_id="n")

    def boom(root, run_id, text, base_revision):
        raise NoteConflict({"text": "x", "revision": "r", "source": "file", "updated_at": None})

    monkeypatch.setattr("agenticml.store.notes.write_note", boom)
    assert _run(["note", "n", "--set", "nope", "--dir", str(tmp_path)]) == 1
    assert "note changed while saving" in capsys.readouterr().err
    assert _run(["note", "missing-run", "--dir", str(tmp_path)]) == 1
    assert "no run" in capsys.readouterr().err
    assert _run(["note", "../x", "--dir", str(tmp_path)]) == 1
    assert _run(["note", "n", "--file", "missing.txt", "--dir", str(tmp_path)]) == 1
    assert "cannot read missing.txt" in capsys.readouterr().err

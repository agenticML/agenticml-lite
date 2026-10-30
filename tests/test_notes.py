"""Per-run notes: one notes.md, compare-and-swap, and the notebook list."""

import errno
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from agenticml.store import layout
from agenticml.store import notes as note_store
from agenticml.store.notes import (
    MAX_NOTE_BYTES,
    NOTE_NAME,
    NoteConflict,
    NoteTooLarge,
    list_notes,
    read_note,
    write_note,
)


def _make_run(root, run_id="run-1", *, project="demo", name="Run", status="finished",
              created_at="2026-10-27T00:00:00.000000Z"):
    directory = root / "runs" / run_id
    directory.mkdir(parents=True)
    meta = {
        "format_version": 1,
        "run_id": run_id,
        "project": project,
        "name": name,
        "status": status,
        "created_at": created_at,
    }
    (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return directory


def _empty():
    return {"text": "", "updated_at": None, "revision": None}


def test_missing_note_is_empty_and_round_trip_changes_revision(tmp_path):
    directory = _make_run(tmp_path)
    assert read_note(tmp_path, "run-1") == _empty()

    first = write_note(tmp_path, "run-1", "alpha", None)
    assert first["text"] == "alpha"
    assert first["revision"] == hashlib.sha256(b"alpha").hexdigest()
    assert first["updated_at"].endswith("Z") and "T" in first["updated_at"]
    assert read_note(tmp_path, "run-1") == first
    assert (directory / NOTE_NAME).read_bytes() == b"alpha"
    assert not any(path.name.startswith(".") for path in directory.iterdir())

    second = write_note(tmp_path, "run-1", "beta", first["revision"])
    assert second["text"] == "beta"
    assert second["revision"] != first["revision"]
    assert read_note(tmp_path, "run-1")["revision"] == second["revision"]

    planted = directory / NOTE_NAME
    planted.write_bytes(b"")
    blank = read_note(tmp_path, "run-1")
    assert blank["text"] == ""
    assert blank["revision"] == hashlib.sha256(b"").hexdigest()
    assert blank["updated_at"] is not None
    assert list_notes(tmp_path) == {"notes": [], "truncated": False}


def test_conflict_on_stale_or_missing_revision(tmp_path):
    _make_run(tmp_path)
    first = write_note(tmp_path, "run-1", "one", None)
    with pytest.raises(NoteConflict) as missing:
        write_note(tmp_path, "run-1", "two", None)
    assert missing.value.note["text"] == "one"
    assert missing.value.note["revision"] == first["revision"]
    assert read_note(tmp_path, "run-1")["text"] == "one"

    second = write_note(tmp_path, "run-1", "two", first["revision"])
    with pytest.raises(NoteConflict) as stale:
        write_note(tmp_path, "run-1", "three", first["revision"])
    assert stale.value.note["revision"] == second["revision"]
    assert read_note(tmp_path, "run-1")["text"] == "two"

    with pytest.raises(NoteConflict):
        write_note(tmp_path, "run-1", "   ", "not-the-revision")
    assert (tmp_path / "runs" / "run-1" / NOTE_NAME).read_text(encoding="utf-8") == "two"


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n\n", "\r\n", " \r\n\t"])
def test_empty_or_whitespace_deletes_the_file(tmp_path, blank):
    directory = _make_run(tmp_path)
    current = write_note(tmp_path, "run-1", "keep", None)
    deleted = write_note(tmp_path, "run-1", blank, current["revision"])
    assert deleted == _empty()
    assert not (directory / NOTE_NAME).exists()
    assert read_note(tmp_path, "run-1") == _empty()


def test_size_limit_nul_and_crlf(tmp_path):
    directory = _make_run(tmp_path)
    exact = write_note(tmp_path, "run-1", "x" * MAX_NOTE_BYTES, None)
    assert len(exact["text"].encode("utf-8")) == MAX_NOTE_BYTES
    assert "too_large" not in exact
    assert exact["revision"] == hashlib.sha256(b"x" * MAX_NOTE_BYTES).hexdigest()
    wide = "é" * (MAX_NOTE_BYTES // 2)
    replaced = write_note(tmp_path, "run-1", wide, exact["revision"])
    assert len(replaced["text"].encode("utf-8")) == MAX_NOTE_BYTES

    with pytest.raises(NoteTooLarge):
        write_note(tmp_path, "run-1", "x" * (MAX_NOTE_BYTES + 1), replaced["revision"])
    with pytest.raises(NoteTooLarge):
        write_note(tmp_path, "run-1", "é" * (MAX_NOTE_BYTES // 2 + 1), "stale")
    assert (directory / NOTE_NAME).read_text(encoding="utf-8") == wide
    assert isinstance(NoteTooLarge("x"), ValueError)

    with pytest.raises(ValueError) as nul:
        write_note(tmp_path, "run-1", "a\x00b", replaced["revision"])
    assert not isinstance(nul.value, NoteTooLarge)
    assert (directory / NOTE_NAME).read_text(encoding="utf-8") == wide

    saved = write_note(tmp_path, "run-1", "a\r\nb\rc\n", replaced["revision"])
    assert saved["text"] == "a\nb\nc\n"
    raw = (directory / NOTE_NAME).read_bytes()
    assert raw == b"a\nb\nc\n"
    assert b"\r" not in raw
    assert saved["revision"] == hashlib.sha256(raw).hexdigest()


def test_invalid_utf8_uses_replacement_and_hashes_stored_bytes(tmp_path):
    directory = _make_run(tmp_path)
    raw = b"caf\xff"
    (directory / NOTE_NAME).write_bytes(raw)
    loaded = read_note(tmp_path, "run-1")
    assert loaded["text"] == "caf\ufffd"
    assert loaded["revision"] == hashlib.sha256(raw).hexdigest()


def test_symlink_and_directory_are_ignored_on_read_and_refused_on_write(tmp_path):
    directory = _make_run(tmp_path)
    secret = directory / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    note = directory / NOTE_NAME
    note.symlink_to(secret)
    assert read_note(tmp_path, "run-1") == _empty()
    with pytest.raises(ValueError):
        write_note(tmp_path, "run-1", "nope", None)
    with pytest.raises(ValueError):
        write_note(tmp_path, "run-1", "  \n", None)
    with pytest.raises(NoteConflict) as stale:
        write_note(tmp_path, "run-1", "nope", "stale")
    assert stale.value.note["text"] == ""
    assert "secret" not in stale.value.note["text"]
    assert note.is_symlink()
    assert secret.read_text(encoding="utf-8") == "secret"
    assert list_notes(tmp_path)["notes"] == []

    note.unlink()
    note.mkdir()
    assert read_note(tmp_path, "run-1")["revision"] is None
    with pytest.raises(ValueError):
        write_note(tmp_path, "run-1", "x", None)
    assert note.is_dir()


def test_regular_file_at_run_path_is_missing(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    target = runs / "file-run"
    target.write_bytes(b"not-a-directory")
    with pytest.raises(FileNotFoundError):
        read_note(tmp_path, "file-run")
    with pytest.raises(FileNotFoundError):
        write_note(tmp_path, "file-run", "x", None)
    assert target.read_bytes() == b"not-a-directory"
    assert list(runs.iterdir()) == [target]


def test_in_store_run_symlink_is_refused(tmp_path):
    if not note_store._pin_run_dirs():
        pytest.skip("directory fd pinning is unavailable")
    real = _make_run(tmp_path, "real")
    (tmp_path / "runs" / "alias").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        read_note(tmp_path, "alias")
    with pytest.raises(ValueError, match="symlinked"):
        write_note(tmp_path, "alias", "x", None)
    assert not (real / NOTE_NAME).exists()


def test_symlink_run_dir_cannot_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "runs").mkdir(parents=True)
    (root / "runs" / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        read_note(root, "linked")
    with pytest.raises(ValueError):
        write_note(root, "linked", "x", None)
    assert list(outside.iterdir()) == []


def test_invalid_run_id_and_missing_run(tmp_path):
    for bad in ("../outside", "bad id", "", "a/b", ".hidden"):
        with pytest.raises(ValueError):
            read_note(tmp_path, bad)
        with pytest.raises(ValueError):
            write_note(tmp_path, bad, "x", None)
    assert not (tmp_path / "runs").exists()

    with pytest.raises(FileNotFoundError):
        read_note(tmp_path, "missing-run")
    with pytest.raises(FileNotFoundError):
        write_note(tmp_path, "missing-run", "x", None)
    assert not (tmp_path / "runs" / "missing-run").exists()

    directory = tmp_path / "runs" / "no-meta"
    directory.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        write_note(tmp_path, "no-meta", "x", None)
    assert not (directory / NOTE_NAME).exists()
    (directory / "meta.json").symlink_to(directory / "elsewhere")
    with pytest.raises(FileNotFoundError):
        read_note(tmp_path, "no-meta")
    with pytest.raises(FileNotFoundError):
        write_note(tmp_path, "no-meta", "x", None)


def test_text_must_be_a_string(tmp_path):
    _make_run(tmp_path)
    for bad in (None, b"abc", 1):
        with pytest.raises(ValueError):
            write_note(tmp_path, "run-1", bad, None)
    assert read_note(tmp_path, "run-1") == _empty()


def test_compare_and_swap_is_serialized(tmp_path):
    _make_run(tmp_path)
    base = write_note(tmp_path, "run-1", "base", None)["revision"]
    saved = []
    conflicts = []
    barrier = threading.Barrier(8)

    def attempt(text):
        barrier.wait(timeout=5)
        try:
            saved.append(write_note(tmp_path, "run-1", text, base)["text"])
        except NoteConflict as exc:
            conflicts.append(exc.note["text"])

    threads = [threading.Thread(target=attempt, args=(f"edit-{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert saved and len(saved) == 1
    assert len(conflicts) == 7
    assert set(conflicts) == set(saved)
    assert read_note(tmp_path, "run-1")["text"] == saved[0]


def test_list_notes_skips_empty_sorts_filters_and_truncates(tmp_path):
    _make_run(tmp_path, "newer-created", project="alpha", name="New",
              created_at="2026-10-02T00:00:00.000000Z")
    _make_run(tmp_path, "older-created", project="beta", name=None, status="finished",
              created_at="2026-10-01T00:00:00.000000Z")
    _make_run(tmp_path, "quiet", project="alpha", name="No note")
    _make_run(tmp_path, "spaces", project="alpha", name="Spaces")
    # On-disk whitespace used to be listed; it is skipped, same as a deleted note.
    (tmp_path / "runs" / "spaces" / NOTE_NAME).write_text(" \n\t", encoding="utf-8")
    _make_run(tmp_path, "running-run", project="alpha", name="Live", status="running")
    write_note(tmp_path, "newer-created", "aa", None)
    write_note(tmp_path, "older-created", "bbbb", None)
    write_note(tmp_path, "running-run", "cc", None)
    os.utime(tmp_path / "runs" / "newer-created" / NOTE_NAME, (10, 10))
    os.utime(tmp_path / "runs" / "running-run" / NOTE_NAME, (20, 20))
    os.utime(tmp_path / "runs" / "older-created" / NOTE_NAME, (30, 30))
    os.utime(tmp_path / "runs" / "spaces" / NOTE_NAME, (100, 100))

    found = list_notes(tmp_path)
    assert [row["run_id"] for row in found["notes"]] == [
        "older-created", "running-run", "newer-created",
    ]
    assert found["truncated"] is False
    newest = found["notes"][0]
    assert newest["name"] == ""
    assert newest["project"] == "beta"
    assert newest["status"] == "finished"
    assert newest["text"] == "bbbb"
    assert newest["created_at"] == "2026-10-01T00:00:00.000000Z"
    assert set(newest) == {
        "run_id", "name", "project", "status", "created_at", "text", "updated_at", "revision",
    }
    live = found["notes"][1]
    assert live["status"] == "crashed"
    assert live["status"] != "running"

    assert [row["run_id"] for row in list_notes(tmp_path, project="alpha")["notes"]] == [
        "running-run", "newer-created",
    ]
    assert list_notes(tmp_path, project="Alpha")["notes"] == []
    assert list_notes(tmp_path, project="missing") == {"notes": [], "truncated": False}

    capped = list_notes(tmp_path, limit_bytes=4)
    assert [row["run_id"] for row in capped["notes"]] == ["older-created"]
    assert capped["truncated"] is True
    both = list_notes(tmp_path, limit_bytes=6)
    assert [row["run_id"] for row in both["notes"]] == ["older-created", "running-run"]
    assert both["truncated"] is True
    assert list_notes(tmp_path, limit_bytes=8)["truncated"] is False

    _make_run(tmp_path, "wide", project="alpha", name="Wide")
    write_note(tmp_path, "wide", "ééé", None)
    os.utime(tmp_path / "runs" / "wide" / NOTE_NAME, (40, 40))
    hidden = list_notes(tmp_path, project="alpha", limit_bytes=5)
    assert hidden["notes"] == []
    assert hidden["truncated"] is True

    _make_run(tmp_path, "other-big", project="other", name="Big")
    write_note(tmp_path, "other-big", "x" * 50, None)
    os.utime(tmp_path / "runs" / "other-big" / NOTE_NAME, (50, 50))
    kept = list_notes(tmp_path, project="beta", limit_bytes=4)
    assert [row["run_id"] for row in kept["notes"]] == ["older-created"]
    assert kept["truncated"] is False


def test_list_notes_skips_an_unreadable_run(tmp_path, monkeypatch):
    _make_run(tmp_path, "good", project="p", name="Good")
    _make_run(tmp_path, "bad", project="p", name="Bad")
    write_note(tmp_path, "good", "visible", None)
    write_note(tmp_path, "bad", "hidden", None)
    original = note_store._load

    def flaky(directory, dir_fd=None):
        if Path(directory).name == "bad":
            raise OSError(errno.EIO, "read failed")
        return original(directory, dir_fd)

    monkeypatch.setattr(note_store, "_load", flaky)
    found = list_notes(tmp_path)
    assert [row["run_id"] for row in found["notes"]] == ["good"]
    assert found["notes"][0]["text"] == "visible"
    assert found["truncated"] is False


def _count_note_reads(monkeypatch):
    reads = []
    original = note_store._load

    def counting(directory, dir_fd=None):
        reads.append(Path(directory).name)
        return original(directory, dir_fd)

    monkeypatch.setattr(note_store, "_load", counting)
    return reads


def test_list_notes_skips_whitespace_only_notes(tmp_path, monkeypatch):
    _make_run(tmp_path, "spaces", name="Spaces")
    _make_run(tmp_path, "real", name="Real")
    (tmp_path / "runs" / "spaces" / NOTE_NAME).write_text(" \n\t", encoding="utf-8")
    write_note(tmp_path, "real", "kept", None)
    os.utime(tmp_path / "runs" / "spaces" / NOTE_NAME, (20, 20))
    os.utime(tmp_path / "runs" / "real" / NOTE_NAME, (10, 10))
    reads = _count_note_reads(monkeypatch)
    found = list_notes(tmp_path)
    assert [row["run_id"] for row in found["notes"]] == ["real"]
    assert found["notes"][0]["text"] == "kept"
    assert found["truncated"] is False
    assert reads == ["spaces", "real"]


def test_list_notes_skips_an_oversized_note_without_reading(tmp_path, monkeypatch):
    _make_run(tmp_path, "huge", name="Huge")
    _make_run(tmp_path, "older", name="Older")
    write_note(tmp_path, "older", "visible", None)
    (tmp_path / "runs" / "huge" / NOTE_NAME).write_bytes(b"x" * (MAX_NOTE_BYTES + 1))
    os.utime(tmp_path / "runs" / "huge" / NOTE_NAME, (50, 50))
    os.utime(tmp_path / "runs" / "older" / NOTE_NAME, (10, 10))
    reads = _count_note_reads(monkeypatch)
    found = list_notes(tmp_path)
    assert [row["run_id"] for row in found["notes"]] == ["older"]
    assert found["notes"][0]["text"] == "visible"
    assert found["truncated"] is True
    assert reads == ["older"]


def test_list_notes_stops_before_reading_past_the_budget(tmp_path, monkeypatch):
    _make_run(tmp_path, "newest", name="New")
    _make_run(tmp_path, "older", name="Old")
    write_note(tmp_path, "newest", "abcd", None)
    write_note(tmp_path, "older", "efgh", None)
    os.utime(tmp_path / "runs" / "newest" / NOTE_NAME, (20, 20))
    os.utime(tmp_path / "runs" / "older" / NOTE_NAME, (10, 10))
    reads = _count_note_reads(monkeypatch)
    found = list_notes(tmp_path, limit_bytes=4)
    assert [row["run_id"] for row in found["notes"]] == ["newest"]
    assert found["notes"][0]["text"] == "abcd"
    assert found["truncated"] is True
    assert reads == ["newest"]


def test_write_note_does_not_recreate_a_removed_run_directory(tmp_path, monkeypatch):
    directory = _make_run(tmp_path)

    def fail_atomic(*args, **kwargs):
        raise AssertionError("notes must not create parent directories")

    monkeypatch.setattr(layout, "atomic_write", fail_atomic)
    original_require = note_store._require_run

    def drop(path, dir_fd=None):
        original_require(path, dir_fd)
        for child in list(path.iterdir()):
            child.unlink()
        path.rmdir()

    monkeypatch.setattr(note_store, "_require_run", drop)
    with pytest.raises(FileNotFoundError):
        write_note(tmp_path, "run-1", "orphan", None)
    assert not directory.exists()
    assert not (tmp_path / "runs" / "run-1" / NOTE_NAME).exists()


def test_write_note_accepts_a_directory_that_reports_nlink_zero(tmp_path, monkeypatch):
    """A live directory may report ``st_nlink == 0`` on some FUSE filesystems."""
    if not note_store._pin_run_dirs():
        pytest.skip("directory fd pinning is unavailable")
    directory = _make_run(tmp_path)
    real_fstat = os.fstat

    def zero_nlink(fd):
        info = real_fstat(fd)
        return os.stat_result(info[:3] + (0,) + info[4:])

    monkeypatch.setattr(os, "fstat", zero_nlink)
    saved = write_note(tmp_path, "run-1", "kept", None)
    assert saved["text"] == "kept"
    assert (directory / NOTE_NAME).read_text(encoding="utf-8") == "kept"


def _finish(call, timeout):
    """Run ``call`` on a daemon thread so a blocked open fails the test."""
    box = {}

    def run():
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "note operation blocked"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def test_fifo_note_is_empty_on_read_and_refused_on_write(tmp_path):
    if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("nonblocking fifo open is unavailable")
    directory = _make_run(tmp_path)
    os.mkfifo(directory / NOTE_NAME)
    assert _finish(lambda: read_note(tmp_path, "run-1"), 2) == _empty()
    with pytest.raises(ValueError):
        _finish(lambda: write_note(tmp_path, "run-1", "nope", None), 2)
    with pytest.raises(ValueError):
        _finish(lambda: write_note(tmp_path, "run-1", " \n", None), 2)
    assert (directory / NOTE_NAME).is_fifo()
    assert _finish(lambda: list_notes(tmp_path), 2) == {"notes": [], "truncated": False}
    other = _make_run(tmp_path, "run-2")
    saved = _finish(lambda: write_note(tmp_path, "run-2", "free", None), 2)
    assert saved["text"] == "free"
    assert (other / NOTE_NAME).read_text(encoding="utf-8") == "free"


def test_oversized_note_read_is_bounded_and_cas_uses_the_prefix(tmp_path, monkeypatch):
    directory = _make_run(tmp_path)
    path = directory / NOTE_NAME
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.write(fd, b"PREFIX")
        os.ftruncate(fd, 4 * 1024 * 1024 * 1024)
    finally:
        os.close(fd)
    requested = []
    real_read = os.read

    def counting(handle, size):
        requested.append(size)
        if sum(requested) > MAX_NOTE_BYTES + 1:
            raise AssertionError(f"note read asked for {sum(requested)} bytes")
        return real_read(handle, size)

    monkeypatch.setattr(os, "read", counting)
    loaded = read_note(tmp_path, "run-1")
    monkeypatch.setattr(os, "read", real_read)
    assert loaded["too_large"] is True
    assert len(loaded["text"].encode("utf-8")) == MAX_NOTE_BYTES
    assert loaded["text"].startswith("PREFIX")
    prefix = b"PREFIX" + b"\x00" * (MAX_NOTE_BYTES - len(b"PREFIX"))
    bare = hashlib.sha256(prefix).hexdigest()
    assert loaded["revision"] == hashlib.sha256(prefix + b"\0too_large").hexdigest()
    assert loaded["revision"] != bare
    assert loaded["updated_at"]
    assert sum(requested) <= MAX_NOTE_BYTES + 1
    assert path.stat().st_size == 4 * 1024 * 1024 * 1024

    with pytest.raises(NoteConflict) as conflict:
        write_note(tmp_path, "run-1", "trim", None)
    assert conflict.value.note["too_large"] is True
    assert conflict.value.note["revision"] == loaded["revision"]
    assert len(conflict.value.note["text"].encode("utf-8")) == MAX_NOTE_BYTES
    assert path.stat().st_size == 4 * 1024 * 1024 * 1024

    with pytest.raises(NoteConflict) as same_prefix:
        write_note(tmp_path, "run-1", "trim", bare)
    assert same_prefix.value.note["revision"] == loaded["revision"]
    assert path.stat().st_size == 4 * 1024 * 1024 * 1024

    replaced = write_note(tmp_path, "run-1", "trim", loaded["revision"])
    assert replaced["text"] == "trim"
    assert "too_large" not in replaced
    assert path.read_bytes() == b"trim"

    fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, b"PREFIX")
        os.ftruncate(fd, 4 * 1024 * 1024 * 1024)
    finally:
        os.close(fd)
    again = read_note(tmp_path, "run-1")
    deleted = write_note(tmp_path, "run-1", " \n", again["revision"])
    assert deleted == _empty()
    assert not path.exists()


def test_list_notes_bounds_a_file_that_grows_after_stat(tmp_path, monkeypatch):
    _make_run(tmp_path, "huge", name="Huge")
    _make_run(tmp_path, "older", name="Older")
    write_note(tmp_path, "older", "visible", None)
    (tmp_path / "runs" / "huge" / NOTE_NAME).write_bytes(b"Z" * (MAX_NOTE_BYTES + 100_000))
    os.utime(tmp_path / "runs" / "huge" / NOTE_NAME, (50, 50))
    os.utime(tmp_path / "runs" / "older" / NOTE_NAME, (10, 10))
    original = note_store._note_stat

    def lie(directory, dir_fd=None):
        found = original(directory, dir_fd)
        if Path(directory).name == "huge" and found is not None:
            return (found[0], 8)
        return found

    monkeypatch.setattr(note_store, "_note_stat", lie)
    returned = 0
    real_read = os.read

    def counting(handle, size):
        nonlocal returned
        if size > MAX_NOTE_BYTES + 1:
            raise AssertionError(f"note read asked for {size} bytes")
        chunk = real_read(handle, size)
        returned += len(chunk)
        # Meta plus the small note fit in the slack. The oversized body must not.
        if returned > MAX_NOTE_BYTES + 1 + 8192:
            raise AssertionError(f"note read returned {returned} bytes")
        return chunk

    monkeypatch.setattr(os, "read", counting)
    found = list_notes(tmp_path)
    assert [row["run_id"] for row in found["notes"]] == ["older"]
    assert found["notes"][0]["text"] == "visible"
    assert found["truncated"] is True
    assert "too_large" not in found["notes"][0]


def test_failed_replace_removes_the_temp_file(tmp_path, monkeypatch):
    directory = _make_run(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError(errno.EIO, "replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_note(tmp_path, "run-1", "x", None)
    assert {path.name for path in directory.iterdir()} == {"meta.json"}


def _restore_swapped_run(root):
    link = root / "runs" / "run-1"
    moved = root / "runs" / "run-1-real"
    if link.is_symlink():
        link.unlink()
    if moved.exists() and not link.exists():
        moved.rename(link)


def test_swapped_run_directory_symlink_is_not_followed(tmp_path, monkeypatch):
    if not note_store._pin_run_dirs():
        pytest.skip("directory fd pinning is unavailable")
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "meta.json").write_text("{}", encoding="utf-8")
    (outside / "precious").write_text("keep", encoding="utf-8")
    _make_run(root)
    write_note(root, "run-1", "inside", None)
    real_run_dir = layout.run_dir

    def swap_after_validation(root_arg, run_id):
        path = real_run_dir(root_arg, run_id)
        if path.is_symlink():
            return path
        moved = path.with_name(path.name + "-real")
        path.rename(moved)
        path.symlink_to(outside, target_is_directory=True)
        return path

    monkeypatch.setattr(layout, "run_dir", swap_after_validation)

    def unchanged(before):
        now = sorted((path.name, path.read_bytes()) for path in outside.iterdir() if path.is_file())
        assert now == before

    before = sorted((path.name, path.read_bytes()) for path in outside.iterdir() if path.is_file())
    with pytest.raises(ValueError):
        write_note(root, "run-1", "pwned", None)
    unchanged(before)
    assert not (outside / "notes.md").exists()
    _restore_swapped_run(root)

    planted = b"outside-note\n"
    (outside / "notes.md").write_bytes(planted)
    before = sorted((path.name, path.read_bytes()) for path in outside.iterdir() if path.is_file())
    with pytest.raises(ValueError):
        write_note(root, "run-1", " \n", hashlib.sha256(planted).hexdigest())
    unchanged(before)
    _restore_swapped_run(root)

    before = sorted((path.name, path.read_bytes()) for path in outside.iterdir() if path.is_file())
    with pytest.raises(ValueError):
        read_note(root, "run-1")
    unchanged(before)
    _restore_swapped_run(root)

    before = sorted((path.name, path.read_bytes()) for path in outside.iterdir() if path.is_file())
    found = list_notes(root)
    assert all("outside-note" not in row["text"] for row in found["notes"])
    unchanged(before)

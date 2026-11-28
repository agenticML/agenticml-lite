"""Import core: writer time options, run writing, dedupe, CLI, and the byte helpers."""

import json
import logging
import shutil
import socket
import struct
import sys
import types
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from agenticml import cli
from agenticml import importers
from agenticml.importers import (
    ImportedRun,
    ImportOptions,
    ImportSourceError,
    import_run_id,
    renumber_constant_steps,
    run_import,
    write_imported_run,
)
from agenticml.importers import _protobuf as pb
from agenticml.importers._crc32c import crc32c, masked_crc32c
from agenticml.importers._media import sniff_image_type
from agenticml.store import RunReader, RunWriter, list_runs
from agenticml.store.layout import try_lock_run, unlock_run

START = datetime(2024, 3, 5, 12, 30, 15, 250000, tzinfo=timezone.utc)
START_UNIX = START.timestamp()


def png_bytes() -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")


def make_run(name="run-a", *, points=None, media=None, **fields) -> ImportedRun:
    if points is None:
        points = [
            (START_UNIX + 1.0, 0, {"loss": 1.0, "acc": 0.1}),
            (START_UNIX + 2.5, 1, {"loss": 0.5}),
            (START_UNIX + 4.0, 2, {"loss": 0.25, "acc": 0.9}),
        ]
    values = dict(
        source="tensorboard",
        location="/logs/exp",
        source_run_id=name,
        name=name,
        project="resnet",
        started_at=START,
        ended_at=START + timedelta(seconds=10),
        status="finished",
        source_status=None,
        config={"lr": 0.1, "layers": [1, 2]},
        summary={"best": 0.25},
        tags=["baseline"],
        points=points,
        media=media if media is not None else [],
        notes="from tb",
    )
    values.update(fields)
    return ImportedRun(**values)


def fake_source(monkeypatch, runs_factory, source="tensorboard", error=None):
    """Register a fake in-memory reader under ``source``; ``runs_factory(path, options)`` -> runs."""
    module = types.ModuleType("fake_import_reader")

    def discover(path, options):
        if error is not None:
            raise error
        yield from runs_factory(path, options)

    module.discover = discover
    monkeypatch.setitem(sys.modules, "fake_import_reader", module)
    monkeypatch.setitem(importers.REGISTRY, source, "fake_import_reader")


def seconds(reader):
    return {step: value for step, value in reader.time_index()}


# --- writer time options -------------------------------------------------


def test_writer_defaults_use_the_clock(tmp_path):
    before = datetime.now(timezone.utc)
    writer = RunWriter.create(tmp_path, run_id="native")
    writer.write_scalars([(1, {"x": 1.0})])
    writer.finish("finished", {"a": 1})
    after = datetime.now(timezone.utc)
    reader = RunReader(tmp_path, "native")
    meta = reader.meta()
    created = datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
    finished = datetime.fromisoformat(meta["finished_at"].replace("Z", "+00:00"))
    assert before <= created <= finished <= after
    assert 0.0 <= seconds(reader)[1] <= (after - before).total_seconds() + 1
    finish_event = [e for e in reader.events() if e["kind"] == "finish"][0]
    assert before.timestamp() <= finish_event["t"] <= after.timestamp()


def test_writer_time_options_override_the_clock(tmp_path):
    writer = RunWriter.create(tmp_path, run_id="old", created_at=START)
    writer.write_scalars([(3, {"x": 1.0})], wall_time=START_UNIX + 7.5)
    writer.write_scalars([(4, {"x": 2.0})], wall_time=START_UNIX - 100)
    ended = START + timedelta(seconds=30)
    writer.finish("finished", finished_at=ended)
    reader = RunReader(tmp_path, "old")
    meta = reader.meta()
    assert meta["created_at"] == "2024-03-05T12:30:15.250000Z"
    assert meta["finished_at"] == "2024-03-05T12:30:45.250000Z"
    assert seconds(reader) == {3: 7.5, 4: 0.0}
    finish_event = [e for e in reader.events() if e["kind"] == "finish"][0]
    assert finish_event["t"] == ended.timestamp()


def test_writer_hostname_default_is_this_machine_and_override_is_stored(tmp_path):
    default = RunWriter.create(tmp_path, run_id="default")
    default.finish("finished")
    assert RunReader(tmp_path, "default").meta()["hostname"] == socket.gethostname()
    named = RunWriter.create(tmp_path, run_id="named", hostname="gpu-1")
    named.finish("finished")
    assert RunReader(tmp_path, "named").meta()["hostname"] == "gpu-1"
    none = RunWriter.create(tmp_path, run_id="none", hostname=None)
    none.finish("finished")
    assert RunReader(tmp_path, "none").meta()["hostname"] is None


def test_imported_run_hostname_is_the_sources_or_null(tmp_path):
    plain = write_imported_run(tmp_path, make_run("plain"))
    assert RunReader(tmp_path, plain.run_id).meta()["hostname"] is None
    named = write_imported_run(tmp_path, make_run("named", hostname="h1"))
    assert RunReader(tmp_path, named.run_id).meta()["hostname"] == "h1"
    blank = write_imported_run(tmp_path, make_run("blank", hostname="  "))
    assert RunReader(tmp_path, blank.run_id).meta()["hostname"] is None


def test_writer_naive_created_at_is_utc(tmp_path):
    writer = RunWriter.create(tmp_path, run_id="naive", created_at=datetime(2020, 1, 2, 3, 4, 5))
    writer.finish("finished")
    assert RunReader(tmp_path, "naive").meta()["created_at"] == "2020-01-02T03:04:05.000000Z"


# --- writing an imported run --------------------------------------------


def test_imported_run_reads_back_with_original_times_and_data(tmp_path):
    png = png_bytes()
    run = make_run(
        media=[
            (START_UNIX + 1.0, 0, "sample", png, "image/png", "first"),
            (START_UNIX + 4.0, 2, "sample", png, "image/png", None),
        ],
        source_status="FINISHED",
    )
    row = write_imported_run(tmp_path, run)
    assert row.status == "imported"
    assert (row.points, row.media, row.dropped_points) == (5, 2, 0)
    assert row.run_id == import_run_id(run)
    assert row.run_id.startswith("20240305T123015-")

    reader = RunReader(tmp_path, row.run_id)
    meta = reader.meta()
    assert meta["created_at"] == "2024-03-05T12:30:15.250000Z"
    assert meta["finished_at"] == "2024-03-05T12:30:25.250000Z"
    assert meta["status"] == "finished"
    assert meta["project"] == "resnet" and meta["name"] == "run-a"
    assert meta["config"] == {"lr": 0.1, "layers": [1, 2]}
    assert meta["summary"] == {"best": 0.25}
    assert meta["notes"] == "from tb"
    assert meta["tags"] == ["baseline", "imported:tensorboard"]
    assert meta["group"] is None and meta["job_type"] is None
    imported = meta["imported_from"]
    assert imported["source"] == "tensorboard"
    assert imported["location"] == "/logs/exp"
    assert imported["source_run_id"] == "run-a"
    assert imported["source_status"] == "FINISHED"
    assert imported["agenticml_version"] and imported["imported_at"]
    assert "renumbered_steps" not in imported

    assert sorted(reader.keys()) == ["acc", "loss"]
    assert reader.series("loss")["points"] == [[0, 1.0], [1, 0.5], [2, 0.25]]
    assert reader.series("acc")["points"] == [[0, pytest.approx(0.1)], [2, pytest.approx(0.9)]]
    rows = reader.media("sample")
    assert [r["step"] for r in rows] == [0, 2]
    assert rows[0]["caption"] == "first" and rows[0]["content_type"] == "image/png"
    # No code or lineage for imported runs.
    assert not any(e["kind"] == "code" for e in reader.events())
    assert [r["run_id"] for r in list_runs(tmp_path)] == [row.run_id]


def test_time_bin_seconds_are_wall_time_minus_start(tmp_path):
    row = write_imported_run(tmp_path, make_run())
    assert seconds(RunReader(tmp_path, row.run_id)) == {0: 1.0, 1: 2.5, 2: 4.0}


def test_points_group_by_wall_time_and_step(tmp_path):
    points = [
        (START_UNIX + 1, 5, {"a": 1.0}),
        (START_UNIX + 1, 5, {"b": 2.0}),
        (START_UNIX + 2, 5, {"c": 3.0}),
        (START_UNIX + 3, 6, {"a": 4.0}),
        (START_UNIX + 3, 6, {"a": 5.0}),  # same key again: a second record, in order
    ]
    row = write_imported_run(tmp_path, make_run(points=points))
    reader = RunReader(tmp_path, row.run_id)
    assert [list(p) for p in reader.time_index()] == [[5, 1.0], [5, 2.0], [6, 3.0], [6, 3.0]]
    assert reader.series("a")["points"] == [[5, 1.0], [6, 4.0], [6, 5.0]]


def test_status_and_end_time_fallbacks(tmp_path):
    crashed = make_run("c", status="crashed", ended_at=None)
    row = write_imported_run(tmp_path, crashed)
    meta = RunReader(tmp_path, row.run_id).meta()
    assert meta["status"] == "crashed"
    # finished_at falls back to the last wall time seen.
    assert meta["finished_at"] == "2024-03-05T12:30:19.250000Z"

    empty = make_run("e", points=[], ended_at=None)
    meta = RunReader(tmp_path, write_imported_run(tmp_path, empty).run_id).meta()
    assert meta["finished_at"] == meta["created_at"]
    assert meta["status"] == "finished"


def test_media_wall_times_extend_the_fallback_finish_time(tmp_path):
    png = png_bytes()
    media = [
        (START_UNIX + 500.0, 0, "img", png, "image/png", None),
        (float("nan"), 1, "img", png, "image/png", None),
        (START_UNIX + 9e12, 2, "img", png, "image/png", None),
    ]
    run = make_run("m", points=iter([]), media=media, ended_at=None)
    row = write_imported_run(tmp_path, run)
    assert row.status == "imported" and row.media == 3
    meta = RunReader(tmp_path, row.run_id).meta()
    assert meta["finished_at"] == "2024-03-05T12:38:35.250000Z"  # start + 500 s


def test_bad_steps_and_values_are_dropped_and_counted(tmp_path):
    points = [
        (START_UNIX + 1, -1, {"x": 1.0}),
        (START_UNIX + 2, 2**32, {"x": 1.0}),
        (START_UNIX + 3, 2**32 - 1, {"x": 2.0}),
        (START_UNIX + 4, True, {"x": 3.0}),
        (START_UNIX + 5, 7, {"x": "text", "y": float("nan"), 5: 1.0}),
    ]
    media = [
        (START_UNIX, -3, "img", png_bytes(), "image/png", None),
        (START_UNIX, 3, "img", png_bytes(), "image/png", None),
    ]
    row = write_imported_run(tmp_path, make_run(points=points, media=media))
    assert row.status == "imported"
    assert (row.points, row.dropped_points) == (2, 5)
    assert (row.media, row.dropped_media) == (1, 1)
    reader = RunReader(tmp_path, row.run_id)
    assert reader.series("x")["points"] == [[2**32 - 1, 2.0]]
    assert reader.series("y")["nonfinite"] == 1


def test_clock_skew_and_bad_wall_times_do_not_fail_the_run(tmp_path):
    points = [
        (START_UNIX - 50, 0, {"x": 1.0}),
        (float("nan"), 1, {"x": 2.0}),
        (START_UNIX + 9, 2, {"x": 3.0}),
    ]
    row = write_imported_run(tmp_path, make_run(points=points, ended_at=None))
    assert row.status == "imported"
    assert seconds(RunReader(tmp_path, row.run_id)) == {0: 0.0, 1: 0.0, 2: 9.0}


def test_renumbered_steps_are_recorded_in_meta(tmp_path):
    run = make_run(points=[])
    run.points = iter([(START_UNIX + 1, 0, {"x": 1.0})])
    run.renumbered_steps.append("x")
    row = write_imported_run(tmp_path, run)
    meta = RunReader(tmp_path, row.run_id).meta()
    assert meta["imported_from"]["renumbered_steps"] == ["x"]
    assert meta["imported_from"]["source"] == "tensorboard"


# --- dedupe, replace, failure, dry run ---------------------------------


def test_run_id_is_stable_and_depends_on_source_location_and_id():
    base = make_run()
    assert import_run_id(base) == import_run_id(make_run())
    other_ids = {
        import_run_id(make_run(location="/elsewhere")),
        import_run_id(make_run(source="mlflow")),
        import_run_id(make_run(source_run_id="other")),
    }
    assert import_run_id(base) not in other_ids and len(other_ids) == 3
    stamp, digest = import_run_id(base).split("-")
    assert stamp == "20240305T123015" and len(digest) == 8


def test_same_source_run_with_a_new_start_time_is_the_same_run(tmp_path):
    later = START + timedelta(days=3)
    first = write_imported_run(tmp_path, make_run())
    again = write_imported_run(tmp_path, make_run(started_at=later, summary={"best": 99}))
    assert first.status == "imported"
    assert again.status == "skipped" and again.reason == "already imported"
    assert again.run_id == first.run_id
    assert len(list_runs(tmp_path)) == 1
    assert RunReader(tmp_path, first.run_id).meta()["summary"] == {"best": 0.25}


def test_replace_with_a_new_start_time_keeps_the_old_id(tmp_path):
    later = START + timedelta(days=3)
    first = write_imported_run(tmp_path, make_run())
    assert import_run_id(make_run(started_at=later)) != first.run_id
    replaced = write_imported_run(tmp_path, make_run(started_at=later, summary={"best": 99}), replace=True)
    assert replaced.status == "imported" and replaced.run_id == first.run_id
    assert RunReader(tmp_path, first.run_id).meta()["summary"] == {"best": 99}
    assert [r["run_id"] for r in list_runs(tmp_path)] == [first.run_id]


def test_stale_running_import_is_replaced_under_its_old_id(tmp_path):
    later = START + timedelta(days=3)
    first = write_imported_run(tmp_path, make_run())
    meta_path = tmp_path / "runs" / first.run_id / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["status"] = "running"
    meta_path.write_text(json.dumps(meta))
    row = write_imported_run(tmp_path, make_run(started_at=later))
    assert row.status == "imported" and row.run_id == first.run_id
    assert [r["run_id"] for r in list_runs(tmp_path)] == [first.run_id]


def test_a_run_that_only_ends_in_the_digest_is_not_matched(tmp_path):
    later = START + timedelta(days=3)
    digest = import_run_id(make_run()).split("-")[1]
    lookalike = f"mine-{digest}"
    native = RunWriter.create(tmp_path, run_id=lookalike, meta={"project": "p", "name": "mine"})
    native.finish("finished", {})
    # an imported run of ANOTHER source run with the same digest suffix
    other = tmp_path / "runs" / f"20200101T000000-{digest}"
    other.mkdir()
    (other / "meta.json").write_text(json.dumps({"imported_from": {"source": "tensorboard", "location": "/x", "source_run_id": "y"}}))
    row = write_imported_run(tmp_path, make_run(started_at=later))
    assert row.status == "imported"
    assert row.run_id == import_run_id(make_run(started_at=later))
    assert row.run_id not in (lookalike, other.name)


def test_several_matching_runs_use_the_newest_and_warn(tmp_path):
    first = write_imported_run(tmp_path, make_run())
    older_id = import_run_id(make_run(started_at=START - timedelta(days=1)))
    older = write_imported_run(tmp_path, make_run(started_at=START - timedelta(days=1)), replace=False)
    # the older start time asked for its own id only when the first is not found by digest
    assert older.status == "skipped" and older.run_id == first.run_id
    shutil.copytree(tmp_path / "runs" / first.run_id, tmp_path / "runs" / older_id)
    meta_path = tmp_path / "runs" / older_id / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["created_at"] = "2020-01-01T00:00:00.000Z"
    meta_path.write_text(json.dumps(meta))
    warnings = []
    row = write_imported_run(
        tmp_path, make_run(started_at=START + timedelta(days=1)), warn=warnings.append
    )
    assert row.status == "skipped" and row.run_id == first.run_id
    assert len(warnings) == 1 and "newest" in warnings[0]


def test_reimport_skips_and_replace_rewrites(tmp_path):
    first = write_imported_run(tmp_path, make_run())
    assert first.status == "imported"
    again = write_imported_run(tmp_path, make_run(summary={"best": 99}))
    assert again.status == "skipped" and again.run_id == first.run_id
    assert RunReader(tmp_path, first.run_id).meta()["summary"] == {"best": 0.25}

    replaced = write_imported_run(tmp_path, make_run(summary={"best": 99}), replace=True)
    assert replaced.status == "imported" and replaced.run_id == first.run_id
    assert RunReader(tmp_path, first.run_id).meta()["summary"] == {"best": 99}
    assert len(list_runs(tmp_path)) == 1


def test_replace_refuses_a_synced_run(tmp_path):
    first = write_imported_run(tmp_path, make_run())
    (tmp_path / "runs" / first.run_id / "sync.json").write_text("{}")
    row = write_imported_run(tmp_path, make_run(summary={"best": 99}), replace=True)
    assert row.status == "failed" and "synced" in row.reason
    assert RunReader(tmp_path, first.run_id).meta()["summary"] == {"best": 0.25}


def test_replace_rechecks_sync_json_after_taking_the_lock(tmp_path, monkeypatch):
    first = write_imported_run(tmp_path, make_run())
    directory = tmp_path / "runs" / first.run_id
    real_lock = importers._lock_existing

    def lock_then_sync_publishes(path):
        handle = real_lock(path)
        (path / "sync.json").write_text("{}")  # a sync finished its first upload meanwhile
        return handle

    monkeypatch.setattr(importers, "_lock_existing", lock_then_sync_publishes)
    row = write_imported_run(tmp_path, make_run(summary={"best": 99}), replace=True)
    assert row.status == "failed" and "synced" in row.reason
    assert directory.is_dir() and RunReader(tmp_path, first.run_id).meta()["summary"] == {"best": 0.25}
    assert not [p for p in (tmp_path / "runs").iterdir() if p.name.startswith(".replaced-")]
    handle = try_lock_run(directory)  # the lock was released
    assert handle is not None
    unlock_run(handle)


def test_replace_refuses_a_native_run(tmp_path):
    run = make_run()
    native = RunWriter.create(tmp_path, run_id=import_run_id(run), meta={"project": "mine"})
    native.finish("finished", {"keep": True})
    row = write_imported_run(tmp_path, run, replace=True)
    assert row.status == "failed" and "not an imported run" in row.reason
    assert RunReader(tmp_path, row.run_id).meta()["summary"] == {"keep": True}
    # Without --replace it is also left alone.
    assert write_imported_run(tmp_path, run).status == "skipped"


def test_failure_mid_stream_leaves_no_run_directory(tmp_path):
    def broken():
        yield (START_UNIX + 1, 0, {"x": 1.0})
        raise RuntimeError("disk on fire")

    run = make_run("bad", points=broken())
    row = write_imported_run(tmp_path, run)
    assert row.status == "failed" and "disk on fire" in row.reason
    assert not (tmp_path / "runs" / import_run_id(run)).exists()
    assert list_runs(tmp_path) == []


def test_failure_in_media_leaves_no_run_directory(tmp_path):
    def broken():
        raise ValueError("bad image")
        yield

    run = make_run("bad", media=broken())
    row = write_imported_run(tmp_path, run)
    assert row.status == "failed"
    assert not (tmp_path / "runs" / import_run_id(run)).exists()


def test_unwritable_meta_fails_the_run_not_the_import(tmp_path, monkeypatch):
    run = make_run("odd")
    real_create = RunWriter.create

    def failing_create(root, **kwargs):
        (tmp_path / "runs" / kwargs["run_id"]).mkdir(parents=True)
        raise OSError("disk full")

    monkeypatch.setattr(RunWriter, "create", staticmethod(failing_create))
    row = write_imported_run(tmp_path, run)
    assert row.status == "failed" and "disk full" in row.reason
    assert not (tmp_path / "runs" / import_run_id(run)).exists()
    monkeypatch.setattr(RunWriter, "create", real_create)


def test_config_and_summary_become_plain_json(tmp_path):
    nan, inf = float("nan"), float("inf")
    run = make_run(
        name="x\ud800y",
        notes="n\ud800",
        config={"a": nan, ("t", 1): b"by\xffte", 3: {"deep": [inf, 2, {1}]}, "s": "ok\udfff"},
        summary={"loss": inf, "bad": nan, "raw": b"abc", "k": (1, 2)},
    )
    row = write_imported_run(tmp_path, run)
    assert row.status == "imported", row.reason
    directory = tmp_path / "runs" / row.run_id
    # Strict parsers reject NaN/Infinity and lone surrogates.
    def strict(text):
        return json.loads(text, parse_constant=lambda c: pytest.fail(f"non-finite {c}"))

    meta = strict((directory / "meta.json").read_text(encoding="utf-8"))
    assert meta["config"]["a"] is None
    assert meta["config"]['["t", 1]'] == "by\ufffdte"
    assert meta["config"]["3"]["deep"][0] is None
    assert meta["name"] == "x?y" and meta["notes"] == "n?" and meta["config"]["s"] == "ok?"
    assert meta["summary"] == {"loss": None, "bad": None, "raw": "abc", "k": [1, 2]}
    for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines():
        strict(line)


def test_json_plain_handles_cycles_and_unprintable_values():
    loop: list = []
    loop.append(loop)

    class Boom:
        def __str__(self):
            raise RuntimeError("no")

    assert importers.json_plain({"loop": loop}) == {"loop": [None]}
    assert importers.json_plain(Boom()) == "<unserializable>"
    assert importers.json_plain({1: 2, None: 3, 1.5: 4}) == {"1": 2, "null": 3, "1.5": 4}


def test_failing_replace_keeps_the_old_run(tmp_path):
    first = write_imported_run(tmp_path, make_run())

    def broken():
        yield (START_UNIX + 1, 0, {"x": 1.0})
        raise RuntimeError("boom")

    row = write_imported_run(tmp_path, make_run(points=broken()), replace=True)
    assert row.status == "failed" and "boom" in row.reason
    assert row.points == row.media == row.dropped_points == row.dropped_media == 0
    reader = RunReader(tmp_path, first.run_id)
    assert reader.meta()["status"] == "finished" and reader.meta()["summary"] == {"best": 0.25}
    assert reader.series("loss")["points"] == [[0, 1.0], [1, 0.5], [2, 0.25]]
    assert [p.name for p in (tmp_path / "runs").iterdir()] == [first.run_id]


def test_failed_row_resets_dropped_counts(tmp_path):
    def broken():
        yield (START_UNIX + 1, -1, {"x": 1.0})
        yield (START_UNIX + 2, 0, {"x": 1.0})
        raise RuntimeError("boom")

    row = write_imported_run(tmp_path, make_run(points=broken()))
    assert row.status == "failed"
    assert row.dropped_points == 0 and row.points == 0


def test_replace_refuses_a_run_open_in_another_process(tmp_path):
    first = write_imported_run(tmp_path, make_run())
    directory = tmp_path / "runs" / first.run_id
    handle = try_lock_run(directory)
    try:
        row = write_imported_run(tmp_path, make_run(summary={"best": 99}), replace=True)
    finally:
        unlock_run(handle)
    assert row.status == "failed" and "open in another process" in row.reason
    assert RunReader(tmp_path, first.run_id).meta()["summary"] == {"best": 0.25}


def test_a_racing_import_in_create_is_skipped_and_left_alone(tmp_path):
    run = make_run()
    directory = tmp_path / "runs" / import_run_id(run)
    directory.mkdir(parents=True)
    (directory / "partial").write_text("other process")
    handle = try_lock_run(directory)
    try:
        row = write_imported_run(tmp_path, run)
    finally:
        unlock_run(handle)
    assert row.status == "skipped" and "another process" in row.reason
    assert (directory / "partial").read_text() == "other process"


def test_interrupted_import_is_replaced_on_the_next_run(tmp_path):
    run = make_run()
    first = write_imported_run(tmp_path, run)
    directory = tmp_path / "runs" / first.run_id
    meta_path = directory / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["status"] = "running"  # what a killed importer leaves behind
    meta_path.write_text(json.dumps(meta))
    row = write_imported_run(tmp_path, run)
    assert row.status == "imported" and row.run_id == first.run_id
    assert RunReader(tmp_path, first.run_id).meta()["status"] == "finished"


def test_interrupted_import_that_is_open_or_synced_is_skipped(tmp_path):
    run = make_run()
    first = write_imported_run(tmp_path, run)
    directory = tmp_path / "runs" / first.run_id
    meta = json.loads((directory / "meta.json").read_text())
    meta["status"] = "running"
    (directory / "meta.json").write_text(json.dumps(meta))
    handle = try_lock_run(directory)
    try:
        assert write_imported_run(tmp_path, run).status == "skipped"
    finally:
        unlock_run(handle)
    (directory / "sync.json").write_text("{}")
    row = write_imported_run(tmp_path, run)
    assert row.status == "skipped" and "--replace" in row.reason


def test_empty_run_gets_a_config_only_note(tmp_path, capsys):
    row = write_imported_run(tmp_path, make_run("e", points=[]))
    assert row.status == "imported" and "config only" in row.reason
    assert row.to_dict()["reason"] == row.reason
    assert "config only" in cli._import_line(row, False)
    dry = write_imported_run(tmp_path, make_run("f", points=[]), dry_run=True)
    assert "config only" in dry.reason
    assert write_imported_run(tmp_path, make_run("g")).reason is None


def test_implausible_start_time_fails_the_run(tmp_path):
    row = write_imported_run(tmp_path, make_run(started_at=datetime(1900, 1, 1, tzinfo=timezone.utc)))
    assert row.status == "failed" and "start time" in row.reason


def test_dry_run_writes_nothing(tmp_path):
    media = [(START_UNIX, 0, "img", png_bytes(), "image/png", None)]
    row = write_imported_run(tmp_path, make_run(media=media), dry_run=True)
    assert (row.status, row.points, row.media) == ("imported", 5, 1)
    assert list(tmp_path.iterdir()) == []

    write_imported_run(tmp_path, make_run())
    row = write_imported_run(tmp_path, make_run(), dry_run=True)
    assert row.status == "skipped"
    synced = tmp_path / "runs" / row.run_id / "sync.json"
    synced.write_text("{}")
    row = write_imported_run(tmp_path, make_run(), replace=True, dry_run=True)
    assert row.status == "failed"
    synced.unlink()
    row = write_imported_run(tmp_path, make_run(), replace=True, dry_run=True)
    assert row.status == "imported"
    assert (tmp_path / "runs" / row.run_id / "meta.json").is_file()  # old run untouched


def test_dry_run_reports_reader_errors(tmp_path):
    def broken():
        yield (START_UNIX, 0, {"x": 1.0})
        raise RuntimeError("boom")

    row = write_imported_run(tmp_path, make_run(points=broken()), dry_run=True)
    assert row.status == "failed" and "boom" in row.reason
    assert list(tmp_path.iterdir()) == []


# --- renumber helper ----------------------------------------------------


def test_renumber_constant_steps_numbers_a_flat_series_in_time_order():
    points = [
        (30.0, 0, {"x": 3.0}),
        (10.0, 0, {"x": 1.0}),
        (20.0, 0, {"x": 2.0, "y": 9.0}),
    ]
    out, keys = renumber_constant_steps(points)
    assert keys == ["x"]
    by_step = sorted((step, value) for _wall, step, metrics in out for key, value in metrics.items() if key == "x")
    assert by_step == [(0, 1.0), (1, 2.0), (2, 3.0)]
    # y has a single point: left alone. The mixed point is split by step.
    assert (20.0, 0, {"y": 9.0}) in out and (20.0, 1, {"x": 2.0}) in out
    assert [wall for wall, _s, _m in out if _m == {"x": 3.0}] == [30.0]


def test_renumber_leaves_varying_and_single_point_series_alone():
    points = [(1.0, 0, {"a": 1.0, "b": 1.0}), (2.0, 1, {"a": 2.0}), (3.0, 0, {"c": 1.0})]
    out, keys = renumber_constant_steps(points)
    assert keys == [] and out == points


def test_renumber_through_a_run_records_the_keys(tmp_path):
    flat = [(START_UNIX + i, 0, {"x": float(i)}) for i in range(4)]
    points, keys = renumber_constant_steps(flat)
    run = make_run(points=points)
    run.renumbered_steps.extend(keys)
    row = write_imported_run(tmp_path, run)
    reader = RunReader(tmp_path, row.run_id)
    assert reader.series("x")["points"] == [[0, 0.0], [1, 1.0], [2, 2.0], [3, 3.0]]
    assert reader.meta()["imported_from"]["renumbered_steps"] == ["x"]


# --- run_import ---------------------------------------------------------


def test_run_import_continues_after_a_failed_run(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("nope")
        yield

    runs = [make_run("one"), make_run("two", points=broken()), make_run("three")]
    fake_source(monkeypatch, lambda path, options: iter(runs))
    seen = []
    report = run_import(tmp_path / "store", "tensorboard", str(tmp_path), on_row=seen.append)
    assert [r.status for r in report.rows] == ["imported", "failed", "imported"]
    assert seen == report.rows
    assert (report.imported, report.skipped, report.failed) == (2, 0, 1)
    assert len(list_runs(tmp_path / "store")) == 2


def test_run_import_filters_tags_project_and_running(tmp_path, monkeypatch):
    runs = [
        make_run("keep-1"),
        make_run("keep-2", source_run_id="id-2"),
        make_run("drop"),
        make_run("live", status="running", source_status="RUNNING"),
    ]
    fake_source(monkeypatch, lambda path, options: iter(runs))
    store = tmp_path / "store"
    report = run_import(
        store, "tensorboard", str(tmp_path),
        ImportOptions(project="over", tags=["x", "baseline"], run_globs=["keep-*", "live", "id-2"]),
    )
    rows = {row.name: row for row in report.rows}
    assert set(rows) == {"keep-1", "keep-2", "live"}
    assert rows["live"].status == "skipped" and "running" in rows["live"].reason
    metas = {m["name"]: m for m in (RunReader(store, r["run_id"]).meta() for r in list_runs(store))}
    assert set(metas) == {"keep-1", "keep-2"}
    for meta in metas.values():
        assert meta["project"] == "over"
        assert meta["tags"] == ["baseline", "x", "imported:tensorboard"]

    fake_source(monkeypatch, lambda path, options: iter([make_run("live", status="running", source_status="RUNNING")]))
    report = run_import(tmp_path / "s2", "tensorboard", str(tmp_path), ImportOptions(include_running=True))
    assert report.imported == 1
    meta = RunReader(tmp_path / "s2", report.rows[0].run_id).meta()
    assert meta["status"] == "finished"
    assert meta["imported_from"]["source_status"] == "RUNNING"


def test_run_import_links_parent_runs_from_the_same_import(tmp_path, monkeypatch):
    runs = [
        make_run("child", parent_source_run_id="parent"),
        make_run("parent", started_at=START - timedelta(hours=1)),
        make_run("orphan", parent_source_run_id="elsewhere"),
    ]
    fake_source(monkeypatch, lambda path, options: iter(runs))
    report = run_import(tmp_path / "store", "tensorboard", str(tmp_path))
    ids = {row.name: row.run_id for row in report.rows}
    metas = {n: RunReader(tmp_path / "store", i).meta() for n, i in ids.items()}
    assert metas["child"]["parent_run_id"] == ids["parent"]
    assert metas["parent"]["parent_run_id"] is None
    assert metas["orphan"]["parent_run_id"] is None
    assert metas["child"]["status"] == "finished"


def test_run_import_second_pass_skips_everything(tmp_path, monkeypatch):
    fake_source(monkeypatch, lambda path, options: iter([make_run("a"), make_run("b")]))
    store = tmp_path / "store"
    assert run_import(store, "tensorboard", str(tmp_path)).imported == 2
    report = run_import(store, "tensorboard", str(tmp_path))
    assert (report.imported, report.skipped, report.failed) == (0, 2, 0)


def test_run_import_dry_run_writes_nothing(tmp_path, monkeypatch):
    fake_source(monkeypatch, lambda path, options: iter([make_run("a")]))
    store = tmp_path / "store"
    report = run_import(store, "tensorboard", str(tmp_path), dry_run=True)
    assert report.imported == 1 and report.dry_run
    assert not store.exists()


def test_reader_warnings_reach_the_report(tmp_path, monkeypatch):
    def runs(path, options):
        options.warn("may still be writing")
        options.warn("may still be writing")
        yield make_run("a")

    fake_source(monkeypatch, runs)
    report = run_import(tmp_path / "store", "tensorboard", str(tmp_path))
    assert report.warnings == ["may still be writing"]


def test_cli_prints_each_reader_warning_once(tmp_path, monkeypatch, capsys):
    def runs(path, options):
        options.warn("reader says hello")
        yield make_run("a")

    fake_source(monkeypatch, runs)
    # pytest installs root handlers, which would turn cli.main's basicConfig into a no-op.
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(tmp_path / "s")]) == 0
    captured = capsys.readouterr()
    assert (captured.out + captured.err).count("reader says hello") == 1
    assert "warning: reader says hello" in captured.err


def test_reader_failing_before_the_first_run_is_a_source_error(tmp_path, monkeypatch):
    fake_source(monkeypatch, None, error=ImportSourceError("no event files under x"))
    with pytest.raises(ImportSourceError, match="no event files"):
        run_import(tmp_path / "store", "tensorboard", str(tmp_path))


def test_reader_crash_between_runs_is_one_failed_row(tmp_path, monkeypatch):
    def runs(path, options):
        yield make_run("a")
        raise RuntimeError("listing broke")

    fake_source(monkeypatch, runs)
    report = run_import(tmp_path / "store", "tensorboard", str(tmp_path))
    assert [r.status for r in report.rows] == ["imported", "failed"]


def test_reader_crash_before_the_first_run_is_a_source_error(tmp_path, monkeypatch, capsys):
    fake_source(monkeypatch, None, error=PermissionError(13, "Permission denied", "/logs"))
    with pytest.raises(ImportSourceError, match="cannot read .*Permission denied"):
        run_import(tmp_path / "store", "tensorboard", str(tmp_path))
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(tmp_path / "s")]) == 2
    captured = capsys.readouterr()
    assert "Permission denied" in captured.err and "Traceback" not in captured.err
    assert not (tmp_path / "s").exists()


def test_run_import_survives_an_unusable_run_object(tmp_path, monkeypatch):
    far = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5)))
    runs = [make_run("far", status="running", started_at=far), make_run("fine")]
    fake_source(monkeypatch, lambda path, options: iter(runs))
    seen = []
    report = run_import(tmp_path / "store", "tensorboard", str(tmp_path), on_row=seen.append)
    assert [r.status for r in report.rows] == ["skipped", "imported"]
    assert report.rows[0].run_id is None and seen == report.rows

    class Broken(ImportedRun):
        @property
        def name(self):
            raise RuntimeError("bad run object")

        @name.setter
        def name(self, value):
            pass

    broken = Broken(**{**make_run("x").__dict__})
    fake_source(monkeypatch, lambda path, options: iter([broken, make_run("after")]))
    report = run_import(tmp_path / "store2", "tensorboard", str(tmp_path), ImportOptions(run_globs=["*"]))
    assert [r.status for r in report.rows] == ["failed", "imported"]
    assert "bad run object" in report.rows[0].reason


def test_reader_that_raises_on_discover_is_a_source_error(tmp_path, monkeypatch):
    module = types.ModuleType("fake_plain_reader")

    def discover(path, options):  # a plain function, not a generator
        raise ValueError("boom")

    module.discover = discover
    monkeypatch.setitem(sys.modules, "fake_plain_reader", module)
    monkeypatch.setitem(importers.REGISTRY, "tensorboard", "fake_plain_reader")
    with pytest.raises(ImportSourceError, match="boom"):
        run_import(tmp_path / "store", "tensorboard", str(tmp_path))
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(tmp_path / "s")]) == 2


def test_bad_inputs_are_source_errors(tmp_path, monkeypatch):
    fake_source(monkeypatch, lambda path, options: iter([]))
    with pytest.raises(ImportSourceError, match="unknown source"):
        run_import(tmp_path, "nope", str(tmp_path))
    with pytest.raises(ImportSourceError, match="does not exist"):
        run_import(tmp_path, "tensorboard", str(tmp_path / "missing"))
    with pytest.raises(ImportSourceError, match="only apply to the csv source"):
        run_import(tmp_path, "mlflow", str(tmp_path), ImportOptions(step_column="s"))


def test_unavailable_importer_message(tmp_path, monkeypatch):
    monkeypatch.setitem(importers.REGISTRY, "mlflow", "agenticml.importers._no_such_module")
    with pytest.raises(ImportSourceError, match="the mlflow importer is not available in this version"):
        run_import(tmp_path, "mlflow", str(tmp_path))
    module = types.ModuleType("broken_reader")
    monkeypatch.setitem(sys.modules, "broken_reader", module)
    monkeypatch.setitem(importers.REGISTRY, "mlflow", "broken_reader")
    with pytest.raises(ImportSourceError, match="not available"):
        run_import(tmp_path, "mlflow", str(tmp_path))


def test_registry_covers_every_source():
    assert importers.REGISTRY == {
        "tensorboard": "agenticml.importers.tensorboard",
        "mlflow": "agenticml.importers.mlflow",
        "wandb": "agenticml.importers.wandb",
        "csv": "agenticml.importers.table",
    }


# --- CLI ---------------------------------------------------------------


def test_cli_imports_prints_rows_and_total(tmp_path, monkeypatch, capsys):
    png = png_bytes()
    runs = [make_run("a", media=[(START_UNIX, 0, "img", png, "image/png", None)]), make_run("b")]
    fake_source(monkeypatch, lambda path, options: iter(runs))
    store = tmp_path / "store"
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(store), "--tag", "t1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"imported {import_run_id(runs[0])}  a  5 points, 1 images"
    assert lines[1].startswith("imported ") and lines[1].endswith("b  5 points, 0 images")
    assert lines[-1] == "2 imported, 0 skipped, 0 failed"
    assert "t1" in RunReader(store, import_run_id(runs[0])).meta()["tags"]

    fake_source(monkeypatch, lambda path, options: iter([make_run("a"), make_run("b")]))
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(store)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"skipped {import_run_id(runs[0])}  a  (already imported)"
    assert lines[-1] == "0 imported, 2 skipped, 0 failed"


def test_cli_uses_agenticml_dir_by_default(tmp_path, monkeypatch, capsys):
    fake_source(monkeypatch, lambda path, options: iter([make_run("a")]))
    assert cli.main(["import", "tensorboard", str(tmp_path)]) == 0
    capsys.readouterr()
    assert [r["name"] for r in list_runs(tmp_path / "agenticml")] == ["a"]


def test_cli_exit_one_when_a_run_fails(tmp_path, monkeypatch, capsys):
    def broken():
        raise RuntimeError("nope")
        yield

    fake_source(monkeypatch, lambda path, options: iter([make_run("ok"), make_run("bad", points=broken())]))
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(tmp_path / "s")]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "failed bad: RuntimeError: nope" in out
    assert out[-1] == "1 imported, 0 skipped, 1 failed"


def test_cli_exit_two_for_bad_path_unknown_source_and_missing_importer(tmp_path, monkeypatch, capsys):
    fake_source(monkeypatch, lambda path, options: iter([]))
    assert cli.main(["import", "tensorboard", str(tmp_path / "missing"), "--dir", str(tmp_path / "s")]) == 2
    captured = capsys.readouterr()
    assert "does not exist" in captured.err and "Traceback" not in captured.err

    with pytest.raises(SystemExit) as exc:
        cli.main(["import", "aim", str(tmp_path)])
    assert exc.value.code == 2
    capsys.readouterr()

    monkeypatch.setitem(importers.REGISTRY, "csv", "agenticml.importers._no_such_module")
    assert cli.main(["import", "csv", str(tmp_path), "--dir", str(tmp_path / "s")]) == 2
    assert "the csv importer is not available in this version" in capsys.readouterr().err

    assert cli.main(["import", "tensorboard", str(tmp_path), "--step-column", "s"]) == 2
    assert "only apply to the csv source" in capsys.readouterr().err

    fake_source(monkeypatch, None, error=ImportSourceError("no `events.out.tfevents.*` files under x"))
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(tmp_path / "s")]) == 2
    assert "no `events.out.tfevents.*` files under x" in capsys.readouterr().err
    assert not (tmp_path / "s").exists()


def test_cli_skipped_row_without_a_run_id_prints_a_dash(tmp_path, monkeypatch, capsys):
    far = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5)))
    fake_source(monkeypatch, lambda path, options: iter([make_run("far", status="running", started_at=far)]))
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(tmp_path / "s")]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "skipped -  far  (still running; use --include-running)"


def test_cli_unusable_store_root_is_one_message_and_exit_two(tmp_path, monkeypatch, capsys):
    runs = [make_run("a"), make_run("b")]
    fake_source(monkeypatch, lambda path, options: iter(runs))
    afile = tmp_path / "afile"
    afile.write_text("x")
    for extra in ([], ["--dry-run"]):
        assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(afile), *extra]) == 2
        captured = capsys.readouterr()
        assert captured.err.count("cannot use") == 1 and "failed" not in captured.out
    # a root below a regular file cannot be created either
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(afile / "sub")]) == 2
    assert "cannot use" in capsys.readouterr().err


def test_cli_csv_options_are_passed_to_the_reader(tmp_path, monkeypatch, capsys):
    seen = {}

    def runs(path, options):
        seen.update(step=options.step_column, time=options.time_column, path=path)
        return iter([])

    fake_source(monkeypatch, runs, source="csv")
    assert cli.main(["import", "csv", str(tmp_path), "--step-column", "it", "--time-column", "ts"]) == 0
    assert seen == {"step": "it", "time": "ts", "path": str(tmp_path)}
    assert capsys.readouterr().out.strip() == "0 imported, 0 skipped, 0 failed"


def test_cli_json_report(tmp_path, monkeypatch, capsys):
    def runs(path, options):
        options.warn("careful")
        return iter([make_run("a")])

    fake_source(monkeypatch, runs)
    store = tmp_path / "store"
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(store), "--json"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["source"] == "tensorboard" and report["dry_run"] is False
    assert (report["imported"], report["skipped"], report["failed"]) == (1, 0, 0)
    assert report["warnings"] == ["careful"]
    assert report["runs"][0]["status"] == "imported" and report["runs"][0]["points"] == 5
    assert report["runs"][0]["run_id"] == import_run_id(make_run("a"))
    assert "warning: careful" in captured.err

    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(store), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["runs"][0]["status"] == "skipped" and report["runs"][0]["reason"] == "already imported"


def test_cli_dry_run_and_replace(tmp_path, monkeypatch, capsys):
    fake_source(monkeypatch, lambda path, options: iter([make_run("a")]))
    store = tmp_path / "store"
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(store), "--dry-run"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("would import ") and out[-1] == "1 would import, 0 skipped, 0 failed"
    assert not store.exists()

    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(store)]) == 0
    capsys.readouterr()
    run_id = import_run_id(make_run("a"))
    (store / "runs" / run_id / "sync.json").write_text("{}")
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(store), "--replace"]) == 1
    assert "failed a: already synced" in capsys.readouterr().out
    (store / "runs" / run_id / "sync.json").unlink()
    assert cli.main(["import", "tensorboard", str(tmp_path), "--dir", str(store), "--replace"]) == 0
    assert capsys.readouterr().out.startswith(f"imported {run_id}")


def test_cli_parser_lists_every_source_and_option(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["import", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for word in ("tensorboard", "mlflow", "wandb", "csv", "--dir", "--project", "--tag", "--run",
                 "--replace", "--include-running", "--dry-run", "--json", "--step-column", "--time-column"):
        assert word in text


# --- protobuf ----------------------------------------------------------


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field(number: int, wire: int, payload: bytes) -> bytes:
    return varint(number << 3 | wire) + payload


def test_protobuf_decodes_every_wire_type():
    buf = (
        field(1, 0, varint(300))
        + field(2, 1, struct.pack("<d", 2.5))
        + field(3, 2, varint(3) + b"abc")
        + field(4, 5, struct.pack("<f", 1.5))
        + field(1, 0, varint(7))
    )
    msg = pb.decode(buf)
    assert msg[1] == [300, 7]
    assert pb.as_double(msg[2][0]) == 2.5
    assert msg[3] == [b"abc"] and pb.as_str(msg[3][0]) == "abc"
    assert pb.as_float(msg[4][0]) == 1.5
    assert msg.first(1) == 300 and msg.last(1) == 7 and msg.first(9, "none") == "none"


def test_protobuf_signed_and_fixed_helpers():
    negative = (1 << 64) - 5
    assert pb.as_int64(negative) == -5
    assert pb.as_int64(2**63 - 1) == 2**63 - 1
    assert pb.as_int64(2**63) == -(2**63)
    assert pb.as_int32(0xFFFFFFFB) == -5 and pb.as_int32(negative) == -5
    msg = pb.decode(field(1, 0, varint(negative)))
    assert pb.as_int64(msg[1][0]) == -5
    assert pb.as_fixed32(struct.pack("<I", 9)) == 9 and pb.as_fixed64(struct.pack("<Q", 9)) == 9
    assert pb.as_str(b"\xff\xfeok") == "��ok"


def test_protobuf_packed_and_unpacked_repeated_fields_match():
    floats = [1.0, -2.5, 3.25]
    unpacked = b"".join(field(5, 5, struct.pack("<f", f)) for f in floats)
    packed = field(5, 2, varint(12) + struct.pack("<3f", *floats))
    assert pb.repeated_floats(pb.decode(unpacked)[5]) == floats
    assert pb.repeated_floats(pb.decode(packed)[5]) == floats
    doubles = b"".join(field(6, 1, struct.pack("<d", d)) for d in floats)
    assert pb.repeated_doubles(pb.decode(doubles)[6]) == floats
    assert pb.repeated_doubles(pb.decode(field(6, 2, varint(24) + struct.pack("<3d", *floats)))[6]) == floats

    values = [0, 1, 300, (1 << 64) - 2]
    unpacked = b"".join(field(7, 0, varint(v)) for v in values)
    body = b"".join(varint(v) for v in values)
    packed = field(7, 2, varint(len(body)) + body)
    assert pb.repeated_varints(pb.decode(unpacked)[7]) == values
    assert pb.repeated_varints(pb.decode(packed)[7]) == values
    assert pb.repeated_int64(pb.decode(packed)[7]) == [0, 1, 300, -2]
    mixed = unpacked + packed
    assert pb.repeated_varints(pb.decode(mixed)[7]) == values + values


def test_protobuf_rejects_malformed_input():
    bad = [
        b"\x80",  # truncated varint
        b"\x08" + b"\xff" * 10 + b"\x01",  # varint longer than 10 bytes
        field(1, 2, varint(10) + b"abc"),  # length past the end
        field(1, 2, varint(2**40)),  # absurd length
        field(1, 1, b"1234567"),  # short 64-bit
        field(1, 5, b"123"),  # short 32-bit
        b"\x00\x01",  # field number 0
        field(1, 3, b""),  # start group
        field(1, 4, b""),  # end group
        field(1, 6, b""),  # reserved wire type
        varint((1 << 29) << 3),  # field number out of range
    ]
    for buf in bad:
        with pytest.raises(pb.ProtobufError):
            pb.decode(buf)
    with pytest.raises(pb.ProtobufError):
        pb.repeated_floats([b"123"])
    assert issubclass(pb.ProtobufError, ValueError)


def test_protobuf_helpers_reject_the_wrong_wire_type():
    huge = 3 * 10**9  # a varint where fixed or length-delimited data is expected
    for helper in (pb.repeated_floats, pb.repeated_doubles, pb.repeated_fixed32, pb.repeated_fixed64):
        with pytest.raises(pb.ProtobufError):
            helper([huge])
    for helper in (pb.as_str, pb.as_double, pb.as_float, pb.as_fixed32, pb.as_fixed64):
        with pytest.raises(pb.ProtobufError):
            helper(huge)
    # and the reverse: bytes where a varint is expected, or the wrong width
    for helper in (pb.as_int64, pb.as_int32, pb.as_bool):
        with pytest.raises(pb.ProtobufError):
            helper(b"\x01")
    with pytest.raises(pb.ProtobufError):
        pb.as_double(b"1234")
    with pytest.raises(pb.ProtobufError):
        pb.as_float(b"12345678")
    with pytest.raises(pb.ProtobufError):
        pb.repeated_varints([1.5])
    with pytest.raises(pb.ProtobufError):
        pb.repeated_int64([None])


def test_protobuf_decoding_does_not_copy_nested_messages():
    import tracemalloc

    payload = b"x" * (2 * 1024 * 1024)
    inner = field(2, 2, varint(len(payload)) + payload)
    for _ in range(pb.MAX_DEPTH):
        inner = field(1, 2, varint(len(inner)) + inner)
    tracemalloc.start()
    try:
        message = pb.decode(inner)
        chain = [message]
        for _ in range(pb.MAX_DEPTH - 1):
            message = message.child(message.first(1))
            chain.append(message)  # keep every level, like a hostile reader of the result might
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 2 * len(inner)  # one buffer plus the input, not one copy per level
    assert bytes(chain[-1].first(1)[:0]) == b"" and len(chain[-1].first(1)) > len(payload)


def test_protobuf_caps_the_number_of_fields(monkeypatch):
    monkeypatch.setattr(pb, "MAX_FIELDS", 10)
    pb.decode(b"\x0d\x00\x00\x00\x00" * 10)
    with pytest.raises(pb.ProtobufError, match="more than 10 fields"):
        pb.decode(b"\x0d\x00\x00\x00\x00" * 11)


def test_protobuf_accepts_memoryview_and_bytearray():
    buf = field(1, 2, varint(70) + b"y" * 70) + field(2, 0, varint(5))
    for source in (buf, bytearray(buf), memoryview(buf)):
        message = pb.decode(source)
        assert pb.as_str(message[1][0]) == "y" * 70 and message[2] == [5]


def test_protobuf_nesting_is_capped():
    inner = b""
    for _ in range(pb.MAX_DEPTH + 8):
        inner = field(1, 2, varint(len(inner)) + inner)
    message = pb.decode(inner)
    depth = 0
    with pytest.raises(pb.ProtobufError, match="nested deeper"):
        while True:
            message = message.child(message[1][0])
            depth += 1
    assert depth == pb.MAX_DEPTH
    with pytest.raises(pb.ProtobufError):
        pb.decode(b"", depth=pb.MAX_DEPTH + 1)
    with pytest.raises(pb.ProtobufError):
        pb.decode(field(1, 0, varint(1))).child(1)


# --- crc32c and media sniffing ---------------------------------------


def test_crc32c_known_answers():
    assert crc32c(b"123456789") == 0xE3069283
    assert crc32c(b"") == 0
    assert crc32c(b"\x00" * 32) == 0x8A9136AA
    assert crc32c(b"\xff" * 32) == 0x62A8AB43
    assert crc32c(b"6789", crc32c(b"12345")) == 0xE3069283
    assert crc32c(memoryview(b"123456789")) == 0xE3069283


def test_masked_crc32c_matches_the_tfrecord_formula():
    crc = crc32c(b"123456789")
    expected = (((crc >> 15) | (crc << 17)) + 0xA282EAD8) & 0xFFFFFFFF
    assert masked_crc32c(b"123456789") == expected
    assert 0 <= masked_crc32c(struct.pack("<Q", 12)) <= 0xFFFFFFFF


def test_sniff_image_type():
    assert sniff_image_type(png_bytes()) == "image/png"
    assert sniff_image_type(b"\xff\xd8\xff\xe0" + b"\0" * 20) == "image/jpeg"
    assert sniff_image_type(b"GIF89a" + b"\0" * 20) == "image/gif"
    assert sniff_image_type(b"GIF87a") == "image/gif"
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None
    assert sniff_image_type(b"") is None
    assert sniff_image_type(b"<svg></svg>") is None
    assert sniff_image_type(memoryview(png_bytes())) == "image/png"

"""W&B local importer: real offline fixtures, hand built files, hostile input."""

import json
import os
import shutil
import struct
import sys
import types
import zlib
from datetime import datetime
from pathlib import Path

import pytest

from agenticml.importers import ImportOptions, ImportSourceError, run_import
from agenticml.importers import _leveldb_log as leveldb
from agenticml.importers import wandb as wandb_importer
from agenticml.store import RunReader, list_runs

FIXTURE = Path(__file__).parent / "fixtures" / "import" / "wandb" / "wandb"

START = 1_700_000_000.0  # 2023-11-14T22:13:20Z


# --- hand built .wandb files ---------------------------------------------------


def varint(number: int) -> bytes:
    out = bytearray()
    while True:
        byte = number & 0x7F
        number >>= 7
        if number:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def vi(field: int, number: int) -> bytes:
    return varint(field << 3) + varint(number)


def ld(field: int, data: bytes) -> bytes:
    return varint(field << 3 | 2) + varint(len(data)) + data


def st(field: int, text: str) -> bytes:
    return ld(field, text.encode())


def timestamp(seconds: float) -> bytes:
    whole = int(seconds)
    return vi(1, whole) + vi(2, int((seconds - whole) * 1e9))


def item(path, value, *, legacy: bool = False, raw: str | None = None) -> bytes:
    """A ConfigItem / SummaryItem / HistoryItem: ``key`` for one path part, else ``nested_key`` parts.

    ``legacy`` is documentation only: older clients wrote a whole JSON object under ``key``.
    """
    path = [path] if isinstance(path, str) else list(path)
    if len(path) == 1:
        body = st(1, path[0])
    else:
        body = b"".join(st(2, part) for part in path)
    return body + st(16, raw if raw is not None else json.dumps(value))


def update(*items: bytes, remove=()) -> bytes:
    return b"".join(ld(1, i) for i in items) + b"".join(ld(2, i) for i in remove)


def run_record(
    run_id="abc12345",
    *,
    project="proj",
    entity="team",
    name="run one",
    notes="",
    tags=(),
    start: float | None = START,
    config: bytes | None = None,
    host: str | None = None,
) -> bytes:
    body = st(1, run_id) + st(2, entity) + st(3, project)
    if config is not None:
        body += ld(4, config)
    body += st(8, name) + st(9, notes) + b"".join(st(10, tag) for tag in tags)
    if host is not None:
        body += st(13, host)
    if start is not None:
        body += ld(17, timestamp(start))
    return ld(17, body)


def history(*items: bytes, step: int | None = None, rel_ms: int | None = None) -> bytes:
    body = b"".join(ld(1, i) for i in items)
    if step is not None:
        body += ld(2, vi(1, step))
    if rel_ms is not None:
        body += vi(3, rel_ms)
    return ld(2, body)


def point(step: int, wall: float, **metrics) -> bytes:
    items = [item(k, v) for k, v in metrics.items()]
    items += [item("_step", step), item("_timestamp", wall)]
    return history(*items, step=step)


def summary_record(*items: bytes, remove=()) -> bytes:
    return ld(3, update(*items, remove=remove))


def config_record(*items: bytes, remove=()) -> bytes:
    return ld(5, update(*items, remove=remove))


def exit_record(code: int = 0, runtime: int = 0) -> bytes:
    return ld(18, vi(1, code) + vi(2, runtime))


def header_record(version="0.31.2+abcdef") -> bytes:
    return ld(21, ld(1, st(1, version)))


def stats_record() -> bytes:
    return ld(7, vi(1, 1))


def log_bytes(records, *, chunk: int | None = None, corrupt_crc_at: int | None = None) -> bytes:
    """A W&B file: the 7 byte header and LevelDB blocks (optionally forcing small fragments)."""
    out = bytearray(leveldb.FILE_HEADER)
    emitted = 0

    def emit(kind: int, payload: bytes) -> None:
        nonlocal emitted
        crc = zlib.crc32(payload, zlib.crc32(bytes([kind]))) & 0xFFFFFFFF
        if corrupt_crc_at is not None and emitted == corrupt_crc_at:
            crc ^= 1
        emitted += 1
        out.extend(struct.pack("<IHB", crc, len(payload), kind))
        out.extend(payload)

    for record in records:
        position = 0
        first = True
        while True:
            left = leveldb.BLOCK_SIZE - len(out) % leveldb.BLOCK_SIZE
            if left < leveldb.RECORD_HEADER_SIZE:
                out.extend(b"\x00" * left)
                left = leveldb.BLOCK_SIZE
            room = left - leveldb.RECORD_HEADER_SIZE
            if chunk:
                room = min(room, chunk)
            piece = record[position : position + room]
            position += len(piece)
            last = position >= len(record)
            kind = (
                leveldb.FULL if first and last else leveldb.FIRST if first else leveldb.LAST if last else leveldb.MIDDLE
            )
            emit(kind, piece)
            first = False
            if last:
                break
    return bytes(out)


def basic_records(steps=5, *, exit_code: int | None = 0, **run_kwargs):
    records = [
        header_record(),
        run_record(**run_kwargs),
        config_record(item("lr", 0.5), item("_wandb", {})),
    ]
    for step in range(steps):
        records.append(point(step, START + step + 0.5, loss=1.0 / (step + 1)))
    if exit_code is not None:
        records.append(exit_record(exit_code, runtime=steps))
    return records


def write_run(
    wandb_dir: Path,
    records,
    *,
    run_id="abc12345",
    dir_name=None,
    chunk=None,
    data: bytes | None = None,
) -> Path:
    run_dir = wandb_dir / (dir_name or f"offline-run-20231114_221320-{run_id}")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"run-{run_id}.wandb").write_bytes(data if data is not None else log_bytes(records, chunk=chunk))
    return run_dir


def do_import(tmp_path, source: Path, **kwargs):
    options = kwargs.pop("options", None) or ImportOptions()
    store = tmp_path / "store"
    report = run_import(store, "wandb", str(source), options, **kwargs)
    return report, store


def tree_snapshot(root: Path) -> dict:
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
    }


def by_name(report):
    return {row.name: row for row in report.rows}


# --- the real fixtures ---------------------------------------------------------


def test_fixture_runs_import_with_their_data(tmp_path):
    before = tree_snapshot(FIXTURE)
    report, store = do_import(tmp_path, FIXTURE)
    assert tree_snapshot(FIXTURE) == before  # the source is untouched
    assert [r.status for r in report.rows] == ["imported", "imported"]
    rows = by_name(report)
    good = rows["good-run"]
    assert (good.points, good.media, good.dropped_points) == (60, 1, 0)

    reader = RunReader(store, good.run_id)
    meta = reader.meta()
    assert meta["project"] == "demo-proj" and meta["name"] == "good-run"
    assert meta["notes"] == "fixture run"
    assert meta["hostname"] == "fixture-host"  # the run record's host, not this machine
    assert meta["tags"] == ["baseline", "fixture", "imported:wandb"]
    assert meta["config"] == {"lr": 0.01, "layers": [64, 32], "optimizer": {"name": "sgd"}}
    assert meta["status"] == "finished"
    assert meta["summary"]["best_acc"] == 0.97 and meta["summary"]["note"] == "done"
    assert not any(key.startswith("_") for key in meta["summary"])
    imported = meta["imported_from"]
    assert imported["source"] == "wandb"
    assert imported["location"] == str(FIXTURE.resolve())
    assert imported["source_status"] == "finished"
    assert imported["source_run_id"] and len(imported["source_run_id"]) == 8

    assert sorted(reader.keys()) == ["acc", "loss"]
    loss = reader.series("loss")["points"]
    assert [p[0] for p in loss] == list(range(30))
    assert loss[0][1] == 1.0 and loss[9][1] == pytest.approx(0.1)
    assert reader.series("acc")["points"][29][1] == pytest.approx(29 / 30)
    media = reader.media("sample")
    assert len(media) == 1
    assert media[0]["step"] == 10 and media[0]["caption"] == "step ten"
    assert media[0]["content_type"] == "image/png"
    assert not any(e["kind"] == "code" for e in reader.events())

    failed = RunReader(store, rows["failed-run"].run_id).meta()
    assert failed["status"] == "crashed"
    assert failed["imported_from"]["source_status"] == "failed (exit code 1)"
    assert failed["finished_at"] and failed["finished_at"] >= failed["created_at"]


def test_run_without_a_host_field_has_no_hostname(tmp_path):
    write_run(tmp_path / "src", basic_records())
    report, store = do_import(tmp_path, tmp_path / "src")
    (row,) = report.rows
    assert RunReader(store, row.run_id).meta()["hostname"] is None
    write_run(tmp_path / "src2", basic_records(host="lab-box"), run_id="def67890")
    report, store = do_import(tmp_path, tmp_path / "src2", replace=True)
    (row,) = report.rows
    assert RunReader(store, row.run_id).meta()["hostname"] == "lab-box"


def test_warnings_count_what_was_skipped(tmp_path):
    options = ImportOptions()
    report, _store = do_import(tmp_path, FIXTURE, options=options)
    assert "good-run: skipped 1 histogram values (only scalars and images are imported)" in report.warnings


def test_times_come_from_the_source(tmp_path):
    options = ImportOptions()
    runs = list(wandb_importer.discover(str(FIXTURE), options))
    first = next(r for r in runs if r.name == "good-run")
    walls = [wall for wall, _step, _metrics in first.points]
    started = first.started_at.timestamp()
    assert first.status == "finished" and first.ended_at is not None
    assert started <= walls[0] <= walls[-1] <= first.ended_at.timestamp() + 1

    report, store = do_import(tmp_path, FIXTURE)
    reader = RunReader(store, by_name(report)["good-run"].run_id)
    assert reader.meta()["created_at"] == first.started_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    seconds = [round(sec, 3) for _step, sec in reader.time_index()]
    assert seconds[0] == pytest.approx(walls[0] - started, abs=1e-3)
    assert seconds[-1] == pytest.approx(walls[-1] - started, abs=1e-3)


def test_second_import_skips_and_replace_rewrites(tmp_path):
    report, store = do_import(tmp_path, FIXTURE)
    again = run_import(store, "wandb", str(FIXTURE), ImportOptions())
    assert [r.status for r in again.rows] == ["skipped", "skipped"]
    assert {r.run_id for r in again.rows} == {r.run_id for r in report.rows}
    replaced = run_import(store, "wandb", str(FIXTURE), ImportOptions(), replace=True)
    assert [r.status for r in replaced.rows] == ["imported", "imported"]
    assert len(list_runs(store)) == 2


def test_path_forms_give_the_same_run_ids(tmp_path):
    report, store = do_import(tmp_path, FIXTURE)
    ids = {r.run_id for r in report.rows}
    run_dir = next(p for p in sorted(FIXTURE.iterdir()) if p.is_dir())
    one = run_import(store, "wandb", str(run_dir), ImportOptions())
    assert [r.status for r in one.rows] == ["skipped"] and one.rows[0].run_id in ids
    wandb_file = next(run_dir.glob("*.wandb"))
    assert run_import(store, "wandb", str(wandb_file), ImportOptions()).rows[0].status == "skipped"
    # The project directory that holds wandb/ works too.
    project = FIXTURE.parent
    assert [r.status for r in run_import(store, "wandb", str(project), ImportOptions()).rows] == ["skipped"] * 2


def test_symlinks_and_other_directories_are_not_runs(tmp_path):
    source = tmp_path / "wandb"
    shutil.copytree(FIXTURE, source)
    first = next(p for p in sorted(source.iterdir()) if p.is_dir())
    (source / "latest-run").symlink_to(first.name)
    (source / "debug.log").write_text("x")
    (source / "settings").mkdir()
    report, _store = do_import(tmp_path, source)
    assert len(report.rows) == 2


def test_run_filter_and_project_override(tmp_path):
    report, store = do_import(
        tmp_path, FIXTURE, options=ImportOptions(project="mine", run_globs=["fail*"], tags=["x"])
    )
    assert [r.name for r in report.rows] == ["failed-run"]
    meta = RunReader(store, report.rows[0].run_id).meta()
    assert meta["project"] == "mine" and "x" in meta["tags"]


# --- discovery errors ------------------------------------------------------------


def test_empty_directory_names_what_is_expected(tmp_path):
    with pytest.raises(ImportSourceError, match=r"no run-\*/run-\*\.wandb files under"):
        run_import(tmp_path / "store", "wandb", str(tmp_path), ImportOptions())


def test_missing_path_that_is_not_a_cloud_path(tmp_path):
    for bad in (str(tmp_path / "nope"), "just-one-name", "a/b/c/d", "./a/b", "../a/b", "a b/c"):
        with pytest.raises(ImportSourceError, match="does not exist"):
            run_import(tmp_path / "store", "wandb", bad, ImportOptions())


def test_a_file_that_is_not_wandb_is_not_a_run(tmp_path):
    other = tmp_path / "log.txt"
    other.write_text("x")
    with pytest.raises(ImportSourceError, match="no run-"):
        run_import(tmp_path / "store", "wandb", str(other), ImportOptions())


@pytest.mark.parametrize("path", ["team/proj", "team/proj/abc123", "a.b/c-d_e/f.g"])
def test_cloud_path_without_the_cloud_module(tmp_path, monkeypatch, path):
    monkeypatch.setitem(sys.modules, wandb_importer.CLOUD_MODULE, None)  # as if the module is missing
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ImportSourceError, match="W&B cloud import is not available in this version"):
        run_import(tmp_path / "store", "wandb", path, ImportOptions())


def test_cloud_path_goes_to_the_cloud_module(tmp_path, monkeypatch):
    seen = []

    def discover(path, options):
        seen.append(path)
        return iter(())

    module = types.ModuleType(wandb_importer.CLOUD_MODULE)
    module.discover = discover
    monkeypatch.setitem(sys.modules, wandb_importer.CLOUD_MODULE, module)
    monkeypatch.chdir(tmp_path)
    report = run_import(tmp_path / "store", "wandb", "team/proj/run1", ImportOptions())
    assert seen == ["team/proj/run1"] and report.rows == []


def test_existing_local_path_wins_over_the_cloud_pattern(tmp_path, monkeypatch):
    called = []
    module = types.ModuleType(wandb_importer.CLOUD_MODULE)
    module.discover = lambda path, options: called.append(path)
    monkeypatch.setitem(sys.modules, wandb_importer.CLOUD_MODULE, module)
    monkeypatch.chdir(tmp_path)
    write_run(tmp_path / "team" / "proj", basic_records())
    report = run_import(tmp_path / "store", "wandb", "team/proj", ImportOptions())
    assert not called and report.imported == 1


# --- hand built runs: layouts, status, time ------------------------------------------


def test_legacy_key_layout_and_nested_values(tmp_path):
    records = [
        header_record("0.12.9"),
        run_record(config=update(item("lr", 0.1, legacy=True))),
        summary_record(item("final", 0.25, legacy=True), item("flag", True, legacy=True)),
        history(
            item("loss", 1.5, legacy=True),
            item("acc", 0.5, legacy=True),
            item("flag", True, legacy=True),
            item("words", "text", legacy=True),
            item("curve", [1, 2, 3], legacy=True),
            item("train", {"a": 1.0, "b": {"c": 2.0}, "tag": "x"}, legacy=True),
            item("_step", 3, legacy=True),
            item("_timestamp", START + 7.5, legacy=True),
            step=3,
        ),
        exit_record(0, 8),
    ]
    run_dir = write_run(tmp_path / "wandb", records)
    report, store = do_import(tmp_path, run_dir)
    row = report.rows[0]
    assert row.status == "imported" and row.points == 4
    reader = RunReader(store, row.run_id)
    assert sorted(reader.keys()) == ["acc", "loss", "train.a", "train.b.c"]
    assert reader.series("loss")["points"] == [[3, 1.5]]
    meta = reader.meta()
    assert meta["config"] == {"lr": 0.1}
    assert meta["summary"] == {"final": 0.25}  # booleans are not summary values
    assert datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00")).timestamp() == START
    assert RunReader(store, row.run_id).time_index()[0][1] == pytest.approx(7.5)


def test_step_and_wall_time_fallbacks(tmp_path):
    records = [
        run_record(),
        history(item("a", 1.0), step=7, rel_ms=2500),  # no _step, no _timestamp
        history(item("a", 2.0), item("_step", 9.0), item("_timestamp", "bad")),  # float step, bad time
        history(item("a", 3.0), item("_step", 2**33), item("_timestamp", START + 4)),  # step out of range
        exit_record(),
    ]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    row = report.rows[0]
    assert (row.points, row.dropped_points) == (2, 1)
    reader = RunReader(store, row.run_id)
    assert reader.series("a")["points"] == [[7, 1.0], [9, 2.0]]
    assert [round(sec, 3) for _s, sec in reader.time_index()] == [2.5, 2.5]


def test_nan_and_infinity_values(tmp_path):
    records = [
        run_record(),
        history(item("x", None, raw="NaN"), item("y", None, raw="Infinity"), item("_step", 0)),
        exit_record(),
    ]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    reader = RunReader(store, report.rows[0].run_id)
    assert reader.series("x")["nonfinite"] == 1 and reader.series("y")["nonfinite"] == 1


def test_config_and_summary_replay_updates_and_removals(tmp_path):
    records = [
        run_record(config=update(item("a", 1), item("b", 2))),
        config_record(item("c", {"d": 3}), item(["c", "e"], 4), remove=[st(1, "a")]),
        summary_record(item("m", 1), item("n", "text"), item(["p", "q"], 1), item("_runtime", 5)),
        summary_record(item("m", 2), remove=[st(1, "n")]),
        point(0, START + 1, loss=1.0),
        exit_record(),
    ]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    meta = RunReader(store, report.rows[0].run_id).meta()
    assert meta["config"] == {"b": 2, "c": {"d": 3, "e": 4}}
    assert meta["summary"] == {"m": 2, "p.q": 1}


def test_no_exit_record_is_running_and_skipped(tmp_path):
    run_dir = write_run(tmp_path / "w", basic_records(exit_code=None))
    report, store = do_import(tmp_path, run_dir)
    assert [r.status for r in report.rows] == ["skipped"]
    assert "still running" in report.rows[0].reason
    assert list_runs(store) == []
    report, store = do_import(tmp_path / "again", run_dir, options=ImportOptions(include_running=True))
    meta = RunReader(store, report.rows[0].run_id).meta()
    assert meta["status"] == "finished" and meta["imported_from"]["source_status"] == "running"
    assert report.rows[0].points == 5


def test_exit_code_decides_status_and_end_time(tmp_path):
    records = basic_records(steps=3, exit_code=3)
    records[-1] = exit_record(3, runtime=60)
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    meta = RunReader(store, report.rows[0].run_id).meta()
    assert meta["status"] == "crashed" and meta["imported_from"]["source_status"] == "failed (exit code 3)"
    assert meta["finished_at"] == "2023-11-14T22:14:20.000000Z"  # start + runtime


def test_start_time_falls_back_to_the_environment_record(tmp_path):
    environment = ld(26, ld(3, timestamp(START + 2)))
    records = [run_record(start=None), environment, point(0, START + 3, loss=1.0), exit_record()]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    assert RunReader(store, report.rows[0].run_id).meta()["created_at"] == "2023-11-14T22:13:22.000000Z"
    records = [run_record(start=None), point(0, START + 3, loss=1.0), exit_record()]
    report, _ = do_import(tmp_path / "x", write_run(tmp_path / "w2", records))
    assert report.rows[0].status == "failed" and "no start time" in report.rows[0].reason


def test_run_without_name_uses_the_run_id_and_project_default(tmp_path):
    records = [run_record(name="", project=""), point(0, START + 1, v=1.0), exit_record()]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    meta = RunReader(store, report.rows[0].run_id).meta()
    assert meta["name"] == "abc12345" and meta["project"] == "wandb"


def test_run_with_no_history_imports_config_only(tmp_path):
    report, store = do_import(tmp_path, write_run(tmp_path / "w", [run_record(), exit_record()]))
    row = report.rows[0]
    assert row.status == "imported" and row.points == 0 and "no points" in row.reason


def test_system_metrics_are_counted_not_imported(tmp_path):
    records = [run_record(), stats_record(), stats_record(), point(0, START + 1, loss=1.0), exit_record()]
    report, _store = do_import(tmp_path, write_run(tmp_path / "w", records))
    assert "run one: skipped 2 system-metrics records (not imported in this version)" in report.warnings


def test_unknown_records_and_fields_are_ignored(tmp_path):
    records = [run_record(), ld(100, b"request"), ld(1000, b"future"), vi(1, 5), point(0, START + 1, v=1.0)]
    report, _store = do_import(
        tmp_path, write_run(tmp_path / "w", [*records, exit_record()]), options=ImportOptions()
    )
    assert report.rows[0].status == "imported" and report.rows[0].points == 1


# --- log framing: blocks, fragments, padding, truncation ----------------------------------


def many_points(count, text=""):
    return [point(i, START + i, loss=float(i), **({"pad": 1.0} if text else {})) for i in range(count)]


def test_records_spanning_blocks_and_fragments(tmp_path):
    big = history(item("big", 1.0), item("blob", "x" * 70_000), item("_step", 0), item("_timestamp", START))
    records = [run_record(), big, *many_points(500), exit_record()]
    for chunk in (None, 100):
        run_dir = write_run(tmp_path / f"w{chunk}", records, chunk=chunk)
        raw = next(run_dir.glob("*.wandb")).read_bytes()
        assert len(raw) > 3 * leveldb.BLOCK_SIZE
        report, store = do_import(tmp_path / f"s{chunk}", run_dir)
        assert report.rows[0].status == "imported" and report.rows[0].points == 501
        assert RunReader(store, report.rows[0].run_id).series("big")["points"] == [[0, 1.0]]


def test_block_trailer_padding_is_skipped(tmp_path):
    # A filler record that leaves 3 bytes at the end of the first block (too few for a header).
    size = leveldb.BLOCK_SIZE - leveldb.FILE_HEADER_SIZE - leveldb.RECORD_HEADER_SIZE - 3
    filler = varint(100 << 3 | 2) + varint(size - 2 - 3)  # tag (2 bytes) + a 3 byte length
    filler += b"\x00" * (size - len(filler))
    assert len(filler) == size
    records = [filler, run_record(), *many_points(3), exit_record()]
    data = log_bytes(records)
    assert data[leveldb.BLOCK_SIZE - 3 : leveldb.BLOCK_SIZE] == b"\x00\x00\x00"
    report, _store = do_import(tmp_path, write_run(tmp_path / "w", None, data=data))
    assert report.rows[0].status == "imported" and report.rows[0].points == 3


def test_zero_filled_tail_is_padding(tmp_path):
    data = log_bytes([*basic_records(3)])
    data += b"\x00" * (leveldb.BLOCK_SIZE - len(data) % leveldb.BLOCK_SIZE + 100)
    report, _store = do_import(tmp_path, write_run(tmp_path / "w", None, data=data))
    assert report.rows[0].status == "imported" and report.rows[0].points == 3


def test_truncated_tail_ends_quietly(tmp_path):
    full = log_bytes(basic_records(5, exit_code=0))
    # Cut inside the last record (the exit record): the run has no exit, so it is running.
    cut = full[:-3]
    report, _store = do_import(
        tmp_path, write_run(tmp_path / "w", None, data=cut), options=ImportOptions(include_running=True)
    )
    assert report.rows[0].status == "imported" and report.rows[0].points == 5
    assert report.warnings == []
    # Cut inside a header of the next record.
    cut2 = full + b"\x01\x02\x03"
    report, _store = do_import(tmp_path / "b", write_run(tmp_path / "w2", None, data=cut2))
    assert report.rows[0].status == "imported" and report.rows[0].points == 5
    # A started but unfinished fragmented record is dropped.
    partial = log_bytes([*basic_records(5, exit_code=None), ld(100, b"y" * 500)], chunk=100)[:-120]
    report, _store = do_import(
        tmp_path / "c", write_run(tmp_path / "w3", None, data=partial), options=ImportOptions(include_running=True)
    )
    assert report.rows[0].status == "imported" and report.rows[0].points == 5


def test_oversize_record_is_skipped_with_one_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(leveldb, "MAX_RECORD", 400)
    huge = point(1, START + 1, loss=9.0, blob="x" * 3000)
    records = [run_record(), point(0, START, loss=1.0), huge, huge, point(2, START + 2, loss=2.0), exit_record()]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records, chunk=200))
    assert report.rows[0].status == "imported"
    assert RunReader(store, report.rows[0].run_id).series("loss")["points"] == [[0, 1.0], [2, 2.0]]
    assert sum("skipped a record longer than" in w for w in report.warnings) == 1
    # A single FULL record over the limit too.
    report, _store = do_import(tmp_path / "b", write_run(tmp_path / "w2", [run_record(), huge, exit_record()]))
    assert report.rows[0].points == 0 and any("skipped a record longer than" in w for w in report.warnings)


# --- hostile files: the run fails, nothing is imported ------------------------------------------


def assert_failed_cleanly(report, store, *, version="unknown"):
    assert [r.status for r in report.rows] == ["failed"]
    assert f"unsupported .wandb format (wandb {version})" in report.rows[0].reason
    assert list_runs(store) == []
    runs_dir = Path(store) / "runs"
    assert not runs_dir.exists() or list(runs_dir.iterdir()) == []


def test_bad_header_fails_the_run(tmp_path):
    run_dir = write_run(tmp_path / "w", None, data=b"this is not a wandb file at all")
    report, store = do_import(tmp_path, run_dir)
    assert_failed_cleanly(report, store)


def test_header_only_file_has_no_run_record(tmp_path):
    report, store = do_import(tmp_path, write_run(tmp_path / "w", None, data=leveldb.FILE_HEADER))
    assert_failed_cleanly(report, store)
    assert "no run record" in report.rows[0].reason


def test_version_comes_from_the_header_record_or_the_metadata_file(tmp_path):
    bad = log_bytes([header_record("0.19.4+deadbeef"), run_record()], corrupt_crc_at=1)
    report, store = do_import(tmp_path, write_run(tmp_path / "w", None, data=bad))
    assert_failed_cleanly(report, store, version="0.19.4")
    run_dir = write_run(tmp_path / "w2", None, data=b"garbage")
    (run_dir / "files").mkdir()
    (run_dir / "files" / "wandb-metadata.json").write_text(json.dumps({"cli_version": "0.15.0"}))
    report, store = do_import(tmp_path / "x", run_dir)
    assert_failed_cleanly(report, store, version="0.15.0")


def test_checksum_mismatch_fails_that_run_only(tmp_path):
    wandb_dir = tmp_path / "w"
    write_run(wandb_dir, basic_records(run_id="good0001"), run_id="good0001")
    data = log_bytes(basic_records(run_id="bad00001"), corrupt_crc_at=3)
    write_run(wandb_dir, None, run_id="bad00001", data=data)
    report, store = do_import(tmp_path, wandb_dir)
    assert sorted(r.status for r in report.rows) == ["failed", "imported"]
    failed = next(r for r in report.rows if r.status == "failed")
    assert "checksum mismatch" in failed.reason
    assert len(list_runs(store)) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d[: leveldb.FILE_HEADER_SIZE] + b"\x00\x00\x00\x00\x07\x00\x09" + b"x" * 7,  # unknown type
        lambda d: d[: leveldb.FILE_HEADER_SIZE] + struct.pack("<IHB", 0, 100, 1) + b"x" * 100,  # bad crc
    ],
)
def test_corrupt_physical_records_fail_the_run(tmp_path, mutate):
    data = mutate(log_bytes(basic_records()))
    report, store = do_import(tmp_path, write_run(tmp_path / "w", None, data=data))
    assert_failed_cleanly(report, store)


def test_record_longer_than_its_block_fails(tmp_path):
    payload = b"x" * 10
    bad = leveldb.FILE_HEADER + struct.pack("<IHB", zlib.crc32(payload, zlib.crc32(b"\x01")), 40000, 1) + payload
    report, store = do_import(tmp_path, write_run(tmp_path / "w", None, data=bad))
    assert_failed_cleanly(report, store)


def test_middle_without_first_fails(tmp_path):
    payload = b"abc"
    crc = zlib.crc32(payload, zlib.crc32(bytes([leveldb.MIDDLE]))) & 0xFFFFFFFF
    bad = leveldb.FILE_HEADER + struct.pack("<IHB", crc, 3, leveldb.MIDDLE) + payload
    report, store = do_import(tmp_path, write_run(tmp_path / "w", None, data=bad))
    assert_failed_cleanly(report, store)


@pytest.mark.parametrize(
    "garbage",
    [
        b"\x0a\xff\xff\xff\xff\x0f",  # field 1 claims 4 GiB
        b"\xff" * 12,  # varint longer than 10 bytes
        b"\x00",  # field number 0
        ld(17, b"\x0a\x05ab"),  # run record whose first field runs past its end
        ld(2, b"\x0a\x7f\x0a"),  # history record with a truncated item
    ],
)
def test_malformed_protobuf_fails_the_run(tmp_path, garbage):
    records = [header_record("0.31.2+abc"), run_record(), point(0, START + 1, loss=1.0), garbage, exit_record()]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    assert_failed_cleanly(report, store, version="0.31.2")


@pytest.mark.parametrize(
    "bad_history",
    [
        ld(2, vi(1, 5)),  # history record whose field 1 is a varint, not a message
        ld(2, ld(1, b"\x0a\x7f\x0a")),  # history item with a truncated key
        ld(2, b"\x0a\x7f\x0a"),  # truncated item
    ],
)
def test_history_errors_found_in_the_second_pass_get_the_format_message(tmp_path, bad_history):
    # History items are decoded only while the points are read, never in the metadata pass.
    records = [header_record("0.31.2+abc"), run_record(), bad_history, point(1, START + 1, loss=1.0), exit_record()]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    assert_failed_cleanly(report, store, version="0.31.2")
    assert report.rows[0].reason.startswith("WandbFormatError: unsupported .wandb format")


def test_bad_values_inside_good_records_are_ignored(tmp_path):
    deep = "[" * 100_000
    nested = '{"a":' * 5000 + "1" + "}" * 5000
    records = [
        run_record(),
        history(
            item("loss", 1.0),
            item("list", None, raw=deep),  # never parsed
            item("nest", None, raw=nested),  # too deep: dropped, no crash
            item("junk", None, raw="{not json"),
            item("empty", None, raw=""),
            item(["a"] * 100, 1),  # path deeper than 32: dropped
            item("_step", 1),
            item("_timestamp", START + 1),
        ),
        exit_record(),
    ]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    assert report.rows[0].status == "imported" and report.rows[0].points == 1
    assert RunReader(store, report.rows[0].run_id).keys() == ["loss"]


def test_json_guard_drops_deep_values_on_every_python():
    # Python 3.12's decoder parses this without raising; the depth guard must still reject it.
    nested = '{"a":' * 5000 + "1" + "}" * 5000
    assert wandb_importer._json(nested) is wandb_importer._MISSING
    assert wandb_importer._json("[" * 5000 + "]" * 5000) is wandb_importer._MISSING
    assert wandb_importer._json('{"a": {"b": [1, 2, {"c": 3}]}}') == {"a": {"b": [1, 2, {"c": 3}]}}
    limit = wandb_importer._MAX_JSON_DEPTH
    assert wandb_importer._json("[" * limit + "]" * limit) is not wandb_importer._MISSING
    assert wandb_importer._json("[" * (limit + 1) + "]" * (limit + 1)) is wandb_importer._MISSING


def test_unparseable_start_time_fails_the_run(tmp_path):
    records = [ld(17, st(1, "abc12345") + ld(17, vi(1, 2**62))), exit_record()]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    assert report.rows[0].status == "failed" and list_runs(store) == []


# --- media ----------------------------------------------------------------------------------------


def png_bytes() -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")


def image_value(path: str, caption=None) -> dict:
    value = {"_type": "image-file", "path": path, "format": "png", "width": 1, "height": 1}
    if caption is not None:
        value["caption"] = caption
    return value


def media_run(tmp_path, paths, *, setup=None):
    """A run with one history record per path; ``setup(run_dir)`` creates files."""
    records = [run_record()]
    for step, path in enumerate(paths):
        records.append(history(item("img", image_value(path, f"c{step}")), item("_step", step), item("_timestamp", START + step)))
    records.append(exit_record())
    run_dir = write_run(tmp_path / "w", records)
    (run_dir / "files" / "media").mkdir(parents=True)
    if setup:
        setup(run_dir)
    return run_dir


def test_media_images_are_imported_with_content_type_and_caption(tmp_path):
    def setup(run_dir):
        (run_dir / "files" / "media" / "a.png").write_bytes(png_bytes())
        (run_dir / "files" / "media" / "b.png").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)  # JPEG magic

    run_dir = media_run(tmp_path, ["media/a.png", "media/b.png"], setup=setup)
    report, store = do_import(tmp_path, run_dir)
    row = report.rows[0]
    assert (row.points, row.media) == (0, 2)
    rows = RunReader(store, row.run_id).media("img")
    assert [r["step"] for r in rows] == [0, 1]
    assert [r["caption"] for r in rows] == ["c0", "c1"]
    assert [r["content_type"] for r in rows] == ["image/png", "image/jpeg"]


def test_media_paths_outside_the_run_directory_are_skipped(tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes())

    def setup(run_dir):
        (run_dir / "files" / "media" / "ok.png").write_bytes(png_bytes())
        (run_dir / "files" / "media" / "link.png").symlink_to(outside)
        (run_dir / "files" / "dirlink").symlink_to(tmp_path)

    paths = [
        "media/ok.png",
        "../../../outside.png",
        str(outside),  # absolute
        "media/link.png",  # symlink out of the run directory
        "dirlink/outside.png",
        "media/../../../../outside.png",
        "bad\x00name.png",
        "",
    ]
    run_dir = media_run(tmp_path, paths, setup=setup)
    report, store = do_import(tmp_path, run_dir)
    assert report.rows[0].media == 1
    assert [r["step"] for r in RunReader(store, report.rows[0].run_id).media("img")] == [0]
    assert any("skipped 7 images" in w for w in report.warnings)


def test_missing_non_image_and_oversize_media_are_skipped(tmp_path, monkeypatch):
    def setup(run_dir):
        (run_dir / "files" / "media" / "text.png").write_text("not an image")
        (run_dir / "files" / "media" / "big.png").write_bytes(png_bytes() + b"\x00" * 100)
        (run_dir / "files" / "media" / "dir.png").mkdir()
        (run_dir / "files" / "media" / "ok.png").write_bytes(png_bytes())

    monkeypatch.setattr(wandb_importer, "MAX_MEDIA_BYTES", len(png_bytes()) + 10)
    run_dir = media_run(
        tmp_path, ["media/missing.png", "media/text.png", "media/big.png", "media/dir.png", "media/ok.png"], setup=setup
    )
    report, store = do_import(tmp_path, run_dir)
    assert report.rows[0].status == "imported" and report.rows[0].media == 1
    assert any("skipped 4 images" in w for w in report.warnings)


def test_other_value_types_are_counted(tmp_path):
    records = [
        run_record(),
        history(
            item("table", {"_type": "table-file", "path": "x"}),
            item("hist", {"_type": "histogram", "values": [1], "bins": [1, 2]}),
            item("video", {"_type": "video-file", "path": "v.mp4"}),
            item("loss", 1.0),
            item("_step", 0),
        ),
        history(item("hist", {"_type": "histogram"}), item("_step", 1)),
        exit_record(),
    ]
    report, _store = do_import(tmp_path, write_run(tmp_path / "w", records))
    assert report.rows[0].points == 1
    assert "run one: skipped 2 histogram values (only scalars and images are imported)" in report.warnings
    assert "run one: skipped 1 table-file values (only scalars and images are imported)" in report.warnings
    assert any("video-file" in w for w in report.warnings)


def test_media_consumed_before_points_still_works(tmp_path):
    def setup(run_dir):
        (run_dir / "files" / "media" / "a.png").write_bytes(png_bytes())

    run_dir = media_run(tmp_path, ["media/a.png"], setup=setup)
    run = next(iter(wandb_importer.discover(str(run_dir), ImportOptions())))
    assert [m[2] for m in run.media] == ["img"]


# --- resumed runs, entity, summary, unreadable files -------------------------------------------------------


def test_resumed_run_segments_import_as_separate_runs_with_unique_ids(tmp_path):
    wandb_dir = tmp_path / "w"
    first = write_run(wandb_dir, basic_records(run_id="same0001", name="r1"), run_id="same0001",
                      dir_name="offline-run-20231114_221320-same0001")
    # The later segment has no display name or config, and starts in the same second as the first.
    later = [run_record("same0001", name=""), point(5, START + 9.5, loss=0.25), exit_record(0, runtime=9)]
    second = write_run(wandb_dir, later, run_id="same0001", dir_name="offline-run-20231114_221321-same0001")
    report, store = do_import(tmp_path, wandb_dir)
    assert [r.status for r in report.rows] == ["imported", "imported"]
    assert [r.source_run_id for r in report.rows] == ["same0001", f"same0001@{second.name}"]
    assert len({r.run_id for r in report.rows}) == 2
    assert [r.points for r in report.rows] == [5, 1]
    assert len(list_runs(store)) == 2
    # Importing only the later segment gives the same ids as importing the whole directory.
    report2, _store2 = do_import(tmp_path / "again", second)
    assert [r.run_id for r in report2.rows] == [report.rows[1].run_id]
    report3, _store3 = do_import(tmp_path / "third", first)
    assert [r.run_id for r in report3.rows] == [report.rows[0].run_id]
    # A second run of the whole import skips both.
    again = run_import(store, "wandb", str(wandb_dir), ImportOptions())
    assert [r.status for r in again.rows] == ["skipped", "skipped"]


def test_same_run_id_in_unrelated_directory_names_still_gets_unique_ids(tmp_path):
    wandb_dir = tmp_path / "w"
    write_run(wandb_dir, basic_records(run_id="dup00001"), run_id="dup00001", dir_name="run-a")
    write_run(wandb_dir, basic_records(run_id="dup00001"), run_id="dup00001", dir_name="run-b")
    report, _store = do_import(tmp_path, wandb_dir)
    assert [r.status for r in report.rows] == ["imported", "imported"]
    assert len({r.source_run_id for r in report.rows}) == 2


def test_entity_is_read_but_not_stored(tmp_path):
    # ImportedRun has no entity field; the W&B entity is intentionally not copied anywhere (accepted deviation).
    records = basic_records(entity="demo-team", tags=["t"])
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    meta = RunReader(store, report.rows[0].run_id).meta()
    assert "demo-team" not in json.dumps(meta)


def test_nested_summary_values_are_flattened_and_typed_values_counted(tmp_path):
    nested = {"x": 1.5, "deep": {"y": 2, "name": "s"}, "flag": True, "hist": {"_type": "histogram", "values": [1]}}
    records = [
        run_record(),
        summary_record(
            item("loss", 0.5),
            item("nested", nested),
            item("typed", {"_type": "histogram"}),
            item("alist", [1, 2]),
            item("_wandb", {"runtime": 3}),
        ),
        point(0, START + 1, loss=1.0),
        exit_record(),
    ]
    report, store = do_import(tmp_path, write_run(tmp_path / "w", records))
    meta = RunReader(store, report.rows[0].run_id).meta()
    assert meta["summary"] == {"loss": 0.5, "nested.x": 1.5, "nested.deep.y": 2, "nested.deep.name": "s"}
    assert "run one: skipped 3 summary values (only numbers and text are imported)" in report.warnings


def test_unreadable_wandb_file_is_not_called_an_unsupported_format(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read everything")
    run_dir = write_run(tmp_path / "w", basic_records())
    file = next(run_dir.glob("*.wandb"))
    file.chmod(0)
    try:
        report, store = do_import(tmp_path, run_dir)
    finally:
        file.chmod(0o644)
    assert [r.status for r in report.rows] == ["failed"]
    assert "unsupported" not in report.rows[0].reason
    assert f"cannot read {file.name}" in report.rows[0].reason
    assert list_runs(store) == []


@pytest.mark.parametrize("path", ["team/proj\n", "team/proj/abc\n", "team/proj\n/x"])
def test_cloud_path_pattern_must_match_the_whole_string(path):
    assert not wandb_importer._is_cloud_path(path)


def test_version_file_that_is_a_fifo_or_outside_the_run_directory_is_not_read(tmp_path):
    fifo_dir = write_run(tmp_path / "w1", None, data=b"garbage")
    (fifo_dir / "files").mkdir()
    os.mkfifo(fifo_dir / "files" / "wandb-metadata.json")
    report, store = do_import(tmp_path / "s1", fifo_dir)  # would hang if the FIFO were opened
    assert_failed_cleanly(report, store)

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"cli_version": "9.9.9"}))
    link_dir = write_run(tmp_path / "w2", None, data=b"garbage")
    (link_dir / "files").mkdir()
    (link_dir / "files" / "wandb-metadata.json").symlink_to(outside)
    report, store = do_import(tmp_path / "s2", link_dir)
    assert_failed_cleanly(report, store)  # version stays "unknown"


# --- the log reader on its own ------------------------------------------------------------------------


def test_leveldb_reader_roundtrip_and_header_checks(tmp_path):
    import io

    records = [b"", b"a" * 10, b"b" * 70_000, b"c"]
    data = log_bytes(records)
    fp = io.BytesIO(data)
    leveldb.check_file_header(fp)
    assert list(leveldb.read_records(fp)) == records
    for bad in (b"", b":W&B", b":W&C\xe1\xbe\x00", b":W&B\xe1\xbe\x01", b":W&B\xe2\xbe\x00"):
        with pytest.raises(leveldb.LevelDBLogError):
            leveldb.check_file_header(io.BytesIO(bad))


def test_leveldb_reader_rejects_a_nonzero_padding_header(tmp_path):
    import io

    rest = leveldb.BLOCK_SIZE - leveldb.FILE_HEADER_SIZE - leveldb.RECORD_HEADER_SIZE
    data = leveldb.FILE_HEADER + b"\x00" * leveldb.RECORD_HEADER_SIZE + b"\x00" * (rest - 1) + b"x"
    fp = io.BytesIO(data)
    leveldb.check_file_header(fp)
    with pytest.raises(leveldb.LevelDBLogError):
        list(leveldb.read_records(fp))


def test_source_is_never_modified_by_a_failed_import(tmp_path):
    source = tmp_path / "w"
    write_run(source, None, data=b"garbage")
    write_run(source, basic_records(run_id="good0001"), run_id="good0001")
    before = tree_snapshot(source)
    report, _store = do_import(tmp_path, source)
    assert sorted(r.status for r in report.rows) == ["failed", "imported"]
    assert tree_snapshot(source) == before
    assert os.path.exists(source / "offline-run-20231114_221320-abc12345")


def test_non_image_media_is_not_read_beyond_its_first_bytes(tmp_path, monkeypatch):
    reads: list[int] = []
    real_open = os.fdopen

    def counting(fd, *args, **kwargs):
        fp = real_open(fd, *args, **kwargs)
        original = fp.read

        class Spy:
            def __getattr__(self, name):
                return getattr(fp, name)

            def __enter__(self):
                fp.__enter__()
                return self

            def __exit__(self, *exc):
                return fp.__exit__(*exc)

            def read(self, size=-1):
                reads.append(size)
                return original(size)

        return Spy()

    monkeypatch.setattr(wandb_importer.os, "fdopen", counting)
    base = tmp_path / "run"
    (base / "files").mkdir(parents=True)
    (base / "files" / "big.png").write_bytes(b"x" * 1000)
    assert wandb_importer._read_media(base.resolve(), "big.png") is None
    assert reads == [12]


def test_image_references_are_thinned_to_the_writer_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICML_MEDIA_MAX_PER_KEY", "4")
    paths = [f"media/{i}.png" for i in range(20)]

    def setup(run_dir):
        for name in paths:
            (run_dir / "files" / name).write_bytes(png_bytes())

    run_dir = media_run(tmp_path, paths, setup=setup)
    run = next(iter(wandb_importer.discover(str(run_dir), ImportOptions())))
    steps = [m[1] for m in run.media]
    assert len(steps) <= 4 and steps[0] == 0 and steps[-1] == 19 and steps == sorted(steps)
    report, store = do_import(tmp_path, run_dir)
    assert len(RunReader(store, report.rows[0].run_id).media("img")) <= 4

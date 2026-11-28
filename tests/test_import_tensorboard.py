"""TensorBoard importer: real event files, hand-built TF2 records, hostile input."""

import os
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from agenticml.importers import ImportOptions, ImportSourceError, run_import
from agenticml.importers import _protobuf as pb
from agenticml.importers._crc32c import masked_crc32c
from agenticml.importers.tensorboard import MAX_RECORD, discover
from agenticml.store import RunReader

FIXTURES = Path(__file__).parent / "fixtures" / "import" / "tensorboard" / "logdir"
OLD = 1_600_000_000  # an mtime far in the past, so no "may still be writing" warning


# ------------------------------------------------------------ encoders


def varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def f_varint(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def f_bytes(number: int, data) -> bytes:
    if isinstance(data, str):
        data = data.encode()
    return varint(number << 3 | 2) + varint(len(data)) + bytes(data)


def f_double(number: int, value: float) -> bytes:
    return varint(number << 3 | 1) + struct.pack("<d", value)


def f_float(number: int, value: float) -> bytes:
    return varint(number << 3 | 5) + struct.pack("<f", value)


def metadata(plugin: str, content: bytes = b"") -> bytes:
    return f_bytes(9, f_bytes(1, f_bytes(1, plugin) + (f_bytes(2, content) if content else b"")))


def value_simple(tag: str, number: float) -> bytes:
    return f_bytes(1, tag) + f_float(2, number)


def value_tensor(tag: str, tensor: bytes, plugin: str | None = "scalars") -> bytes:
    return f_bytes(1, tag) + f_bytes(8, tensor) + (metadata(plugin) if plugin else b"")


def value_image(tag: str, data: bytes) -> bytes:
    return f_bytes(1, tag) + f_bytes(4, f_varint(1, 1) + f_varint(2, 1) + f_bytes(4, data))


def event(wall: float, step: int = 0, *values: bytes, summary: bytes | None = None) -> bytes:
    out = f_double(1, wall) + f_varint(2, step)
    if values:
        out += f_bytes(5, b"".join(f_bytes(1, v) for v in values))
    elif summary is not None:
        out += f_bytes(5, summary)
    return out


def version_event(wall: float) -> bytes:
    return f_double(1, wall) + f_bytes(3, "brain.Event:2")


def record(payload: bytes) -> bytes:
    header = struct.pack("<Q", len(payload))
    return (
        header
        + struct.pack("<I", masked_crc32c(header))
        + payload
        + struct.pack("<I", masked_crc32c(payload))
    )


def write_events(path: Path, payloads, *, mtime: float | None = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(record(p) for p in payloads))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def tensor(dtype: int, *parts: bytes, dims=()) -> bytes:
    shape = b"".join(f_bytes(2, f_varint(1, d)) for d in dims)
    return f_varint(1, dtype) + f_bytes(2, shape) + b"".join(parts)


def png_bytes() -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")


def hparams_value(as_tensor: bool = False, **params) -> bytes:
    entries = b""
    for key, item in params.items():
        if isinstance(item, bool):
            inner = f_varint(4, int(item))
        elif isinstance(item, str):
            inner = f_bytes(3, item)
        else:
            inner = f_double(2, float(item))
        entries += f_bytes(1, f_bytes(1, key) + f_bytes(2, inner))
    content = f_bytes(3, entries)  # HParamsPluginData.session_start_info
    tag = f_bytes(1, "_hparams_/session_start_info")
    if as_tensor:  # the TF2 writer (summary_v2.hparams_pb): a DT_STRING tensor plus the metadata
        return tag + f_bytes(8, f_varint(1, 7) + f_bytes(8, b"")) + metadata("hparams", content)
    return tag + metadata("hparams", content)


def read(path, options=None):
    options = options or ImportOptions()
    runs = list(discover(str(path), options))
    return runs, options


def points_of(run):
    return [(wall, step, dict(metrics)) for wall, step, metrics in run.points]


# ------------------------------------------------------------ real fixtures


def by_name(runs):
    return {run.name: run for run in runs}


def test_fixture_runs_are_discovered_by_directory():
    runs, options = read(FIXTURES)
    names = sorted(run.name for run in runs)
    # the add_hparams child directory is folded into torch_hparams, not a run
    assert names == [
        "tbx_basic",
        "torch_hparams",
        "torch_scalars",
        "torch_scalars/loss_train",
        "torch_scalars/loss_val",
    ]
    for run in runs:
        assert run.source == "tensorboard"
        assert run.project == "logdir"
        assert run.source_run_id == run.name
        assert run.location == str(FIXTURES.resolve())
        assert run.status == "finished" and run.source_status is None
        assert run.tags == [] and run.notes == ""


def test_fixture_scalars_match_the_tensorboard_loader():
    runs = by_name(read(FIXTURES)[0])
    basic = points_of(runs["tbx_basic"])
    assert len(basic) == 60  # 50 train/loss + 10 val/acc
    steps = [step for _wall, step, metrics in basic if "train/loss" in metrics]
    assert steps == list(range(50))
    first = basic[0]
    assert first[0] == pytest.approx(1795860212.9893889)
    assert first[2] == {"train/loss": 1.0}
    acc = [m["val/acc"] for _w, _s, m in basic if "val/acc" in m]
    assert len(acc) == 10 and acc[0] == 0.0
    loss = [m["loss"] for _w, _s, m in points_of(runs["torch_hparams"])]
    assert len(loss) == 50 and loss[0] == 2.0 and loss[1] == pytest.approx(1.0)


def test_fixture_start_and_end_come_from_the_first_and_last_event():
    runs = by_name(read(FIXTURES)[0])
    basic = runs["tbx_basic"]
    assert basic.started_at.timestamp() == pytest.approx(1795860212.9887586)
    assert basic.ended_at.timestamp() == pytest.approx(1795860213.067494, abs=1e-5)
    assert basic.started_at.tzinfo is not None
    for run in runs.values():
        assert run.ended_at >= run.started_at


def test_fixture_image_is_a_png_media_row():
    runs = by_name(read(FIXTURES)[0])
    for name in ("tbx_basic", "torch_hparams"):
        media = list(runs[name].media)
        assert len(media) == 1
        wall, step, key, data, content_type, caption = media[0]
        assert (step, key, content_type, caption) == (49, "sample", "image/png", None)
        assert data.startswith(b"\x89PNG") and len(data) == 268


def test_fixture_histograms_and_text_are_skipped_and_counted():
    runs, options = read(FIXTURES)
    list(by_name(runs)["tbx_basic"].points)
    assert "tbx_basic: skipped 2 histogram tags, 1 text tag" in options.warnings


def test_fixture_hparams_child_is_merged_into_its_parent():
    run = by_name(read(FIXTURES)[0])["torch_hparams"]
    assert run.config["lr"] == 0.01
    assert run.config["batch_size"] == 32 and isinstance(run.config["batch_size"], int)
    assert run.config["optimizer"] == "adam"
    assert run.summary == {
        "hparam/accuracy": pytest.approx(0.91, abs=1e-6),
        "hparam/loss": pytest.approx(0.12, abs=1e-6),
    }
    # the metric scalars are not also points of the parent
    assert all("hparam/accuracy" not in m for _w, _s, m in points_of(run))


def test_fixture_add_scalars_children_import_as_separate_runs():
    runs = by_name(read(FIXTURES)[0])
    assert len(points_of(runs["torch_scalars/loss_train"])) == 20
    assert len(points_of(runs["torch_scalars/loss_val"])) == 20
    # the writer's own directory holds only the file_version event: config-only run
    assert points_of(runs["torch_scalars"]) == []


def test_fixture_runs_are_not_modified(tmp_path):
    copy = tmp_path / "logs"
    shutil.copytree(FIXTURES, copy)
    before = {p: p.read_bytes() for p in copy.rglob("*") if p.is_file()}
    run_import(tmp_path / "store", "tensorboard", str(copy))
    after = {p: p.read_bytes() for p in copy.rglob("*") if p.is_file()}
    assert before == after


def test_fixture_import_into_a_store_and_dedupe(tmp_path):
    root = tmp_path / "store"
    report = run_import(root, "tensorboard", str(FIXTURES), ImportOptions(tags=["t"]))
    assert (report.imported, report.skipped, report.failed) == (5, 0, 0)
    rows = {row.name: row for row in report.rows}
    run_id = rows["torch_hparams"].run_id
    reader = RunReader(root, run_id)
    meta = reader.meta()
    assert meta["project"] == "logdir" and meta["name"] == "torch_hparams"
    assert meta["config"]["optimizer"] == "adam"
    assert meta["summary"]["hparam/loss"] == pytest.approx(0.12, abs=1e-6)
    assert meta["status"] == "finished"
    assert meta["imported_from"]["source"] == "tensorboard"
    assert meta["imported_from"]["source_run_id"] == "torch_hparams"
    assert meta["tags"] == ["t", "imported:tensorboard"]
    assert len(reader.series("loss")["points"]) == 50
    assert len(list(reader.media("sample"))) == 1
    again = run_import(root, "tensorboard", str(FIXTURES))
    assert (again.imported, again.skipped) == (0, 5)
    assert {row.run_id for row in again.rows} == {row.run_id for row in report.rows}


def test_fixture_import_keeps_original_times(tmp_path):
    root = tmp_path / "store"
    report = run_import(root, "tensorboard", str(FIXTURES))
    run_id = {row.name: row.run_id for row in report.rows}["tbx_basic"]
    assert run_id.startswith("20261128T100332-")
    meta = RunReader(root, run_id).meta()
    assert meta["created_at"].startswith("2026-11-28T10:03:32")
    assert meta["finished_at"].startswith("2026-11-28T10:03:33")


def test_image_is_stored_with_its_step(tmp_path):
    root = tmp_path / "store"
    report = run_import(root, "tensorboard", str(FIXTURES), ImportOptions(run_globs=["tbx_basic"]))
    (row,) = report.rows
    assert row.media == 1 and row.points == 60
    items = list(RunReader(root, row.run_id).media("sample"))
    assert len(items) == 1


# ------------------------------------------------------------ hand-built logs


def tf2_log(tmp_path):
    root = tmp_path / "tf2"
    write_events(
        root / "train" / "events.out.tfevents.100.h.1.0",
        [
            version_event(100.0),
            # float_val packed, with the plugin metadata on the first event only
            event(101.0, 1, value_tensor("epoch_loss", tensor(1, f_bytes(5, struct.pack("<f", 0.5))))),
            event(102.0, 2, value_tensor("epoch_loss", tensor(1, f_bytes(5, struct.pack("<f", 0.25))), None)),
            # float_val unpacked (wire type 5)
            event(103.0, 3, value_tensor("unpacked", tensor(1, f_float(5, 1.5)))),
            # tensor_content: float32, float64, int64
            event(104.0, 4, value_tensor("c32", tensor(1, f_bytes(4, struct.pack("<f", 2.5))))),
            event(105.0, 5, value_tensor("c64", tensor(2, f_bytes(4, struct.pack("<d", 3.5))))),
            event(106.0, 6, value_tensor("ci64", tensor(9, f_bytes(4, struct.pack("<q", -7))))),
            # double_val, int_val (negative sign-extended), int64_val
            event(107.0, 7, value_tensor("dv", tensor(2, f_bytes(6, struct.pack("<d", 4.5))))),
            event(108.0, 8, value_tensor("iv", tensor(3, f_bytes(7, varint(-3))))),
            event(109.0, 9, value_tensor("i64v", tensor(9, f_bytes(10, varint(2**40))))),
            # half and bfloat16 as half_val bit patterns
            event(110.0, 10, value_tensor("half", tensor(19, f_bytes(13, varint(0x3C00))))),
            event(111.0, 11, value_tensor("bf16", tensor(14, f_bytes(13, varint(0x3FC0))))),
            # shape [1] is still one element
            event(112.0, 12, value_tensor("shape1", tensor(1, f_bytes(5, struct.pack("<f", 9.0)), dims=(1,)))),
            # three elements: not a scalar; histograms plugin is counted
            event(113.0, 13, value_tensor("many", tensor(1, f_bytes(5, struct.pack("<3f", 1, 2, 3)), dims=(3,)), "histograms")),
        ],
    )
    return root


def test_tf2_tensor_scalars_of_every_supported_dtype(tmp_path):
    runs, options = read(tf2_log(tmp_path))
    (run,) = runs
    assert run.name == "train"
    got = {k: v for _w, _s, m in run.points for k, v in m.items()}
    assert got == {
        "epoch_loss": 0.25,  # later step overwrote; see steps below
        "unpacked": 1.5,
        "c32": 2.5,
        "c64": 3.5,
        "ci64": -7.0,
        "dv": 4.5,
        "iv": -3.0,
        "i64v": float(2**40),
        "half": 1.0,
        "bf16": 1.5,
        "shape1": 9.0,
    }
    assert any("skipped 1 histogram tag" in w for w in options.warnings)
    assert run.started_at.timestamp() == 100.0
    assert run.ended_at.timestamp() == 113.0


def test_tf2_points_keep_steps_and_wall_times(tmp_path):
    (run,), _ = read(tf2_log(tmp_path))
    pts = points_of(run)
    assert pts[0] == (101.0, 1, {"epoch_loss": 0.5})
    assert pts[1] == (102.0, 2, {"epoch_loss": 0.25})  # no metadata on this event


def test_tf2_images_plugin_gives_one_media_row_per_image(tmp_path):
    png = png_bytes()
    strings = f_bytes(8, "1") + f_bytes(8, "1") + f_bytes(8, png) + f_bytes(8, png) + f_bytes(8, b"not an image")
    write_events(
        tmp_path / "img" / "events.out.tfevents.1.h.1.0",
        [
            version_event(200.0),
            event(201.0, 5, value_tensor("samples", f_varint(1, 7) + strings, "images")),
        ],
    )
    (run,), options = read(tmp_path / "img")
    assert points_of(run) == []
    media = list(run.media)
    assert [(m[1], m[2], m[4], m[5]) for m in media] == [(5, "samples", "image/png", None)] * 2
    assert all(m[3] == png for m in media)
    assert any("skipped 1 image in an unsupported format" in w for w in options.warnings)


def test_image_bytes_in_an_unknown_format_are_skipped(tmp_path):
    write_events(
        tmp_path / "e" / "events.out.tfevents.1.h.1.0",
        [version_event(1.0), event(2.0, 1, value_image("bad", b"BM-not-a-supported-format"))],
    )
    (run,), options = read(tmp_path / "e")
    assert list(run.media) == []
    assert any("unsupported format" in w for w in options.warnings)


def test_media_read_before_points_still_works(tmp_path):
    png = png_bytes()
    write_events(
        tmp_path / "e" / "events.out.tfevents.1.h.1.0",
        [version_event(1.0), event(2.0, 1, value_image("a", png), value_simple("x", 1.0))],
    )
    (run,), _ = read(tmp_path / "e")
    assert [m[2] for m in run.media] == ["a"]
    assert points_of(run) == [(2.0, 1, {"x": 1.0})]


def test_audio_text_and_graph_are_skipped_and_counted(tmp_path):
    audio = f_bytes(1, "clip") + f_bytes(6, f_float(1, 16000.0))
    text = value_tensor("notes", f_varint(1, 7) + f_bytes(8, "hello"), "text")
    write_events(
        tmp_path / "e" / "events.out.tfevents.1.h.1.0",
        [
            version_event(1.0),
            f_double(1, 1.5) + f_bytes(4, b"graph-def-bytes"),
            event(2.0, 1, audio, text, value_simple("x", 1.0)),
        ],
    )
    (run,), options = read(tmp_path / "e")
    assert points_of(run) == [(2.0, 1, {"x": 1.0})]
    assert "e: skipped 1 audio tag, 1 graph tag, 1 text tag" in options.warnings


def test_two_scalars_with_the_same_tag_in_one_event_are_two_points(tmp_path):
    write_events(
        tmp_path / "e" / "events.out.tfevents.1.h.1.0",
        [version_event(1.0), event(2.0, 3, value_simple("x", 1.0), value_simple("x", 2.0), value_simple("y", 5.0))],
    )
    (run,), _ = read(tmp_path / "e")
    assert points_of(run) == [(2.0, 3, {"x": 1.0}), (2.0, 3, {"x": 2.0, "y": 5.0})]


def test_two_runs_in_one_logdir_and_the_logdir_itself(tmp_path):
    for sub in ("a", "b/c", ""):
        write_events(
            tmp_path / "logs" / sub / "events.out.tfevents.1.h.1.0",
            [version_event(10.0), event(11.0, 1, value_simple("loss", 1.0))],
        )
    runs, _ = read(tmp_path / "logs")
    assert sorted(run.name for run in runs) == ["a", "b/c", "logs"]
    assert {run.project for run in runs} == {"logs"}
    assert len({run.source_run_id for run in runs}) == 3


def test_event_files_are_read_in_filename_order(tmp_path):
    base = tmp_path / "e"
    write_events(base / "events.out.tfevents.200.h.1.0", [version_event(200.0), event(201.0, 2, value_simple("x", 2.0))])
    write_events(base / "events.out.tfevents.100.h.1.0", [version_event(100.0), event(101.0, 1, value_simple("x", 1.0))])
    (run,), _ = read(base)
    assert [step for _w, step, _m in run.points] == [1, 2]
    assert run.started_at.timestamp() == 100.0 and run.ended_at.timestamp() == 201.0


def test_resumed_training_keeps_append_order(tmp_path):
    base = tmp_path / "e"
    write_events(
        base / "events.out.tfevents.1.h.1.0",
        [version_event(1.0), event(2.0, 5, value_simple("x", 1.0)), event(3.0, 2, value_simple("x", 2.0))],
    )
    (run,), _ = read(base)
    assert [step for _w, step, _m in run.points] == [5, 2]


def test_recent_event_file_gets_a_may_still_be_writing_warning(tmp_path):
    path = write_events(
        tmp_path / "live" / "events.out.tfevents.1.h.1.0",
        [version_event(1.0), event(2.0, 1, value_simple("x", 1.0))],
        mtime=None,
    )
    runs, options = read(tmp_path / "live")
    assert len(runs) == 1  # still imported
    assert any("may still be writing" in w for w in options.warnings)
    os.utime(path, (OLD, OLD))
    _, options = read(tmp_path / "live")
    assert options.warnings == []


# ------------------------------------------------------------ hparams


def test_hparams_in_the_run_itself_become_config(tmp_path):
    write_events(
        tmp_path / "e" / "events.out.tfevents.1.h.1.0",
        [
            version_event(1.0),
            event(2.0, 0, hparams_value(lr=0.5, layers=3, name="net", flag=True)),
            event(3.0, 1, value_simple("x", 1.0)),
        ],
    )
    (run,), _ = read(tmp_path / "e")
    assert run.config == {"lr": 0.5, "layers": 3, "name": "net", "flag": True}
    assert isinstance(run.config["layers"], int)
    assert points_of(run) == [(3.0, 1, {"x": 1.0})]
    assert run.summary == {}


def make_hparams_child(parent: Path, name: str, metrics: dict, *, start: float = 50.0, **params):
    write_events(
        parent / name / "events.out.tfevents.5.h.1.0",
        [
            version_event(start),
            event(start + 1, 0, hparams_value(**params)),
            *[event(start + 2, 0, value_simple(tag, number)) for tag, number in metrics.items()],
        ],
    )


def test_hparams_child_with_a_second_child_merges_both_and_warns(tmp_path):
    parent = tmp_path / "exp"
    write_events(parent / "events.out.tfevents.1.h.1.0", [version_event(1.0), event(2.0, 1, value_simple("x", 1.0))])
    make_hparams_child(parent, "t1", {"acc": 0.5}, lr=1.0)
    make_hparams_child(parent, "t2", {"acc": 0.75}, lr=2.0)
    runs, options = read(parent)
    (run,) = runs
    assert run.config == {"lr": 2} and run.summary == {"acc": 0.75}
    assert any("2 add_hparams directories were merged" in w for w in options.warnings)


def test_hparams_children_merge_oldest_first_whatever_their_names(tmp_path):
    parent = tmp_path / "exp"
    write_events(parent / "events.out.tfevents.1.h.1.0", [version_event(1.0), event(2.0, 1, value_simple("x", 1.0))])
    # "0a" sorts before "1" by name but was written later: its values must win
    make_hparams_child(parent, "0a", {"acc": 0.8}, start=200.0, lr=0.2)
    make_hparams_child(parent, "1", {"acc": 0.9}, start=100.0, lr=0.1)
    (run,) = read(parent)[0]
    assert run.config == {"lr": 0.2}
    assert run.summary == {"acc": pytest.approx(0.8, abs=1e-6)}


def test_hparams_child_summary_values_are_not_float32_noise(tmp_path):
    parent = tmp_path / "exp"
    write_events(parent / "events.out.tfevents.1.h.1.0", [version_event(1.0), event(2.0, 1, value_simple("x", 1.0))])
    make_hparams_child(parent, "t1", {"hparam/acc": 0.9, "hparam/loss": 0.1, "big": 123456.5}, lr=1.0)
    (run,) = read(parent)[0]
    assert run.summary == {"hparam/acc": 0.9, "hparam/loss": 0.1, "big": 123456.5}


def test_hparams_child_double_tensor_metrics_stay_exact(tmp_path):
    parent = tmp_path / "exp"
    write_events(parent / "events.out.tfevents.1.h.1.0", [version_event(1.0), event(2.0, 1, value_simple("x", 1.0))])
    exact = 0.1 + 0.2
    write_events(
        parent / "t1" / "events.out.tfevents.5.h.1.0",
        [
            version_event(50.0),
            event(51.0, 0, hparams_value(lr=1.0)),
            event(52.0, 0, value_tensor("m", tensor(2, f_bytes(4, struct.pack("<d", exact))))),
        ],
    )
    (run,) = read(parent)[0]
    assert run.summary == {"m": exact}


def test_tf2_hparams_tensor_is_config_and_not_reported_as_skipped(tmp_path):
    write_events(
        tmp_path / "e" / "events.out.tfevents.1.h.1.0",
        [
            version_event(1.0),
            event(2.0, 0, hparams_value(as_tensor=True, lr=0.5, flag=True)),
            event(3.0, 1, value_simple("x", 1.0)),
        ],
    )
    (run,), options = read(tmp_path / "e")
    assert run.config == {"lr": 0.5, "flag": True}
    assert points_of(run) == [(3.0, 1, {"x": 1.0})]
    assert not any("hparams" in w or "skipped" in w for w in options.warnings)


def test_tf2_hparams_child_is_not_merged_into_its_parent(tmp_path):
    parent = tmp_path / "exp"
    write_events(parent / "events.out.tfevents.1.h.1.0", [version_event(1.0)])
    write_events(
        parent / "child" / "events.out.tfevents.5.h.1.0",
        [
            version_event(50.0),
            event(51.0, 0, hparams_value(as_tensor=True, lr=1.0)),
            event(52.0, 0, value_simple("acc", 0.5)),
        ],
    )
    runs, _ = read(parent)
    assert sorted(run.name for run in runs) == ["child", "exp"]


def test_hparams_child_with_images_stays_a_run(tmp_path):
    parent = tmp_path / "exp"
    write_events(parent / "events.out.tfevents.1.h.1.0", [version_event(1.0)])
    write_events(
        parent / "child" / "events.out.tfevents.1.h.1.0",
        [version_event(2.0), event(3.0, 0, hparams_value(lr=1.0)), event(4.0, 0, value_image("im", png_bytes()))],
    )
    runs, _ = read(parent)
    assert sorted(run.name for run in runs) == ["child", "exp"]
    assert by_name(runs)["child"].config == {"lr": 1}


def test_hparams_child_with_a_training_curve_stays_a_run(tmp_path):
    parent = tmp_path / "exp"
    write_events(parent / "events.out.tfevents.1.h.1.0", [version_event(1.0)])
    write_events(
        parent / "child" / "events.out.tfevents.1.h.1.0",
        [version_event(2.0), event(3.0, 0, hparams_value(lr=1.0))]
        + [event(4.0 + i, i, value_simple("loss", 1.0 / (i + 1))) for i in range(5)],
    )
    runs, _ = read(parent)
    assert sorted(run.name for run in runs) == ["child", "exp"]
    assert by_name(runs)["exp"].summary == {}


def test_hparams_child_without_a_parent_run_stays_a_run(tmp_path):
    parent = tmp_path / "exp"
    make_hparams_child(parent, "t1", {"acc": 0.5}, lr=1.0)
    runs, _ = read(parent)
    assert [run.name for run in runs] == ["t1"]
    assert runs[0].config == {"lr": 1}
    assert points_of(runs[0]) == [(52.0, 0, {"acc": 0.5})]


# ------------------------------------------------------------ errors and hostile input


def test_empty_directory_and_bad_paths_are_errors(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ImportSourceError, match="no `events.out.tfevents.\\*` files under"):
        read(tmp_path / "empty")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "notes.txt").write_text("x")
    with pytest.raises(ImportSourceError, match="no `events.out.tfevents"):
        read(tmp_path / "other")
    with pytest.raises(ImportSourceError, match="not a directory"):
        read(tmp_path / "missing")
    afile = tmp_path / "events.out.tfevents.1.h.1.0"
    write_events(afile, [version_event(1.0)])
    with pytest.raises(ImportSourceError, match="not a directory"):
        read(afile)


def test_event_files_without_a_readable_event_are_an_error(tmp_path):
    base = tmp_path / "e"
    base.mkdir()
    (base / "events.out.tfevents.1.h.1.0").write_bytes(b"this is not a tfrecord file at all")
    with pytest.raises(ImportSourceError, match="no readable events"):
        read(base)


def test_run_import_raises_for_a_missing_path(tmp_path):
    with pytest.raises(ImportSourceError):
        run_import(tmp_path / "store", "tensorboard", str(tmp_path / "empty-nowhere"))


def test_crc_mismatch_skips_one_record_with_one_warning_per_file(tmp_path):
    base = tmp_path / "e"
    good = [event(float(i + 2), i, value_simple("x", float(i))) for i in range(6)]
    blob = bytearray(b"".join(record(p) for p in [version_event(1.0), *good]))
    # flip one byte inside the data of two different records
    for target in (2, 4):
        offset = sum(len(record(p)) for p in [version_event(1.0), *good][:target]) + 12 + 3
        blob[offset] ^= 0xFF
    (base).mkdir()
    path = base / "events.out.tfevents.1.h.1.0"
    path.write_bytes(bytes(blob))
    os.utime(path, (OLD, OLD))
    (run,), options = read(base)
    got = [step for _w, step, _m in run.points]
    assert got == [0, 2, 4, 5]  # records 2 and 4 (steps 1 and 3) were skipped
    messages = [w for w in options.warnings if "wrong checksum" in w]
    assert messages == ["e: events.out.tfevents.1.h.1.0: skipped 2 records with a wrong checksum"]


def test_truncated_tail_ends_the_file_quietly(tmp_path):
    base = tmp_path / "e"
    payloads = [version_event(1.0)] + [event(2.0 + i, i, value_simple("x", float(i))) for i in range(4)]
    blob = b"".join(record(p) for p in payloads)
    for cut in (1, 5, 11, 12, 20, len(record(payloads[-1])) - 1):
        base.mkdir(exist_ok=True)
        path = base / "events.out.tfevents.1.h.1.0"
        path.write_bytes(blob[: len(blob) - cut])
        os.utime(path, (OLD, OLD))
        (run,), options = read(base)
        steps = [s for _w, s, _m in run.points]
        assert steps == [0, 1, 2], cut
        assert options.warnings == []


def test_bad_length_checksum_drops_the_rest_of_the_file(tmp_path):
    base = tmp_path / "e"
    base.mkdir()
    first = [version_event(1.0), event(2.0, 0, value_simple("x", 0.0))]
    rest = record(event(3.0, 1, value_simple("x", 1.0)))
    broken = bytearray(rest)
    broken[9] ^= 0xFF  # inside the length checksum
    path = base / "events.out.tfevents.1.h.1.0"
    path.write_bytes(b"".join(record(p) for p in first) + bytes(broken) + rest)
    os.utime(path, (OLD, OLD))
    (run,), options = read(base)
    assert [s for _w, s, _m in run.points] == [0]
    assert any("bad record header" in w for w in options.warnings)


def test_a_claimed_two_gib_record_is_never_read(tmp_path, monkeypatch):
    base = tmp_path / "e"
    base.mkdir()
    claimed = 2 * 1024**3
    header = struct.pack("<Q", claimed)
    bogus = header + struct.pack("<I", masked_crc32c(header)) + b"only a few bytes"
    path = base / "events.out.tfevents.1.h.1.0"
    path.write_bytes(
        record(version_event(1.0)) + record(event(2.0, 0, value_simple("x", 1.0))) + bogus
    )
    os.utime(path, (OLD, OLD))
    assert claimed > MAX_RECORD

    import agenticml.importers.tensorboard as tb

    requested = []

    class SpyHandle:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            requested.append(size)
            return self._handle.read(size)

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

    monkeypatch.setattr(tb, "open", lambda *args, **kwargs: SpyHandle(open(*args, **kwargs)), raising=False)
    (run,), options = read(base)
    assert [s for _w, s, _m in run.points] == [0]
    assert any("longer than 64 MiB" in w for w in options.warnings)
    assert requested and all(0 <= size <= 1024 * 1024 for size in requested)


def test_an_oversize_record_that_fits_in_the_file_is_skipped(tmp_path, monkeypatch):
    import agenticml.importers.tensorboard as tb

    monkeypatch.setattr(tb, "MAX_RECORD", 100)
    base = tmp_path / "e"
    big = event(2.0, 0, value_simple("big", 1.0), value_simple("pad" * 60, 2.0))
    assert len(big) > 100
    write_events(
        base / "events.out.tfevents.1.h.1.0",
        [version_event(1.0), big, event(3.0, 1, value_simple("x", 1.0))],
    )
    (run,), options = read(base)
    assert points_of(run) == [(3.0, 1, {"x": 1.0})]
    assert any("longer than 64 MiB" in w for w in options.warnings)


def test_a_nesting_bomb_does_not_stop_the_import(tmp_path):
    depth = 4000
    bomb = b""
    for _ in range(depth):
        bomb = f_bytes(5, bomb)
    message = pb.decode(bomb)
    with pytest.raises(pb.ProtobufError):
        while True:  # the decoder itself refuses to walk this deep
            message = message.child(message[5][0])
    # the bomb sits where the importer decodes: Event.summary and a Value's tensor
    bad_value = f_bytes(1, "bomb") + f_bytes(8, bomb) + f_bytes(9, bomb)
    base = tmp_path / "e"
    write_events(
        base / "events.out.tfevents.1.h.1.0",
        [
            version_event(1.0),
            event(2.0, 0, bad_value),
            f_double(1, 2.5) + f_bytes(5, bomb),
            event(3.0, 1, value_simple("x", 1.0)),
        ],
    )
    (run,), options = read(base)
    assert points_of(run) == [(3.0, 1, {"x": 1.0})]


def test_malformed_messages_are_skipped_with_a_count(tmp_path):
    base = tmp_path / "e"
    write_events(
        base / "events.out.tfevents.1.h.1.0",
        [
            version_event(1.0),
            b"\xff\xff\xff\xff",  # not a protobuf message
            f_double(1, 2.0) + f_bytes(5, f_bytes(1, b"\x0a\xff")),  # truncated Value
            event(3.0, 1, f_bytes(1, "x") + f_varint(2, 7)),  # simple_value with a varint
            event(4.0, 2, value_simple("x", 1.0)),
        ],
    )
    (run,), options = read(base)
    assert points_of(run) == [(4.0, 2, {"x": 1.0})]
    assert any("skipped 3 events that could not be decoded" in w for w in options.warnings)


def test_a_wrong_wire_type_in_a_tensor_is_not_a_crash(tmp_path):
    write_events(
        tmp_path / "e" / "events.out.tfevents.1.h.1.0",
        [
            version_event(1.0),
            event(2.0, 0, value_tensor("t", f_varint(1, 1) + f_varint(5, 3))),  # float_val as varint
            event(3.0, 1, value_simple("x", 1.0)),
        ],
    )
    (run,), _ = read(tmp_path / "e")
    assert points_of(run) == [(3.0, 1, {"x": 1.0})]


def _wrong_wire_events():
    """``(id, bad event payload, is it counted as a bad event)`` with a wrong wire type in one field."""
    hparams_content = f_bytes(1, "h") + f_bytes(9, f_bytes(1, f_bytes(1, "hparams") + f_varint(2, 5)))
    image_strings = f_varint(8, 1 << 40) + f_varint(8, 1 << 40) + f_varint(8, 5)
    return [
        ("tensor_content_2^40", event(2.0, 0, value_tensor("t", f_varint(1, 1) + f_varint(4, 1 << 40))), True),
        ("tensor_content_2^63", event(2.0, 0, value_tensor("t", f_varint(1, 1) + f_varint(4, 1 << 63))), True),
        ("string_val_images", event(2.0, 0, value_tensor("t", f_varint(1, 7) + image_strings, "images")), True),
        ("image_bytes", event(2.0, 0, f_bytes(1, "i") + f_bytes(4, f_varint(4, 1 << 40))), True),
        ("plugin_content", event(2.0, 0, hparams_content), False),
        ("tag", event(2.0, 0, f_varint(1, 7) + f_float(2, 1.0)), True),
        ("wall_time", f_bytes(1, b"xy") + f_varint(2, 0) + f_bytes(5, f_bytes(1, value_simple("w", 1.0))), True),
        ("step", f_double(1, 2.0) + f_bytes(2, b"xy") + f_bytes(5, f_bytes(1, value_simple("s", 1.0))), True),
    ]


@pytest.mark.parametrize(
    "bad,counted",
    [pytest.param(bad, counted, id=name) for name, bad, counted in _wrong_wire_events()],
)
def test_wrong_wire_types_skip_one_event_and_leave_other_runs_importable(tmp_path, bad, counted):
    logs = tmp_path / "logs"
    write_events(
        logs / "good" / "events.out.tfevents.1.h.1.0",
        [version_event(1.0), event(2.0, 0, value_simple("x", 1.0)), event(3.0, 1, value_simple("x", 2.0))],
    )
    write_events(
        logs / "bad" / "events.out.tfevents.1.h.1.0",
        [
            version_event(1.0),
            event(2.0, 0, value_simple("y", 1.0)),
            bad,
            event(4.0, 2, value_simple("y", 3.0)),
        ],
    )
    root = tmp_path / "store"
    report = run_import(root, "tensorboard", str(logs))  # never an ImportSourceError
    rows = {row.name: row for row in report.rows}
    assert {row.status for row in rows.values()} == {"imported"}
    assert rows["good"].points == 2
    assert rows["bad"].points == 2  # the valid events around the bad one
    messages = [w for w in report.warnings if w.startswith("bad:")]
    assert any("skipped 1 event that could not be decoded" in w for w in messages) is counted


def test_wrong_wire_type_in_plugin_content_does_not_stop_discovery(tmp_path):
    hostile = f_bytes(1, "h") + f_bytes(9, f_bytes(1, f_bytes(1, "hparams") + f_varint(2, 5)))
    write_events(tmp_path / "logs" / "bad" / "events.out.tfevents.1.h.1.0", [version_event(1.0), event(2.0, 0, hostile)])
    write_events(
        tmp_path / "logs" / "good" / "events.out.tfevents.1.h.1.0",
        [version_event(1.0), event(2.0, 0, value_simple("x", 1.0))],
    )
    runs, _ = read(tmp_path / "logs")
    assert sorted(run.name for run in runs) == ["bad", "good"]


def test_nan_and_inf_scalars_are_kept_as_values(tmp_path):
    write_events(
        tmp_path / "e" / "events.out.tfevents.1.h.1.0",
        [
            version_event(1.0),
            event(2.0, 0, value_simple("x", float("nan"))),
            event(3.0, 1, value_simple("x", float("inf"))),
        ],
    )
    (run,), _ = read(tmp_path / "e")
    pts = points_of(run)
    assert pts[0][2]["x"] != pts[0][2]["x"] and pts[1][2]["x"] == float("inf")


def test_steps_outside_the_store_range_are_dropped_and_counted(tmp_path):
    write_events(
        tmp_path / "logs" / "e" / "events.out.tfevents.1.h.1.0",
        [
            version_event(1.0),
            event(2.0, 0, value_simple("x", 1.0)),
            event(3.0, 2**32, value_simple("x", 2.0)),
            event(4.0, 2**32 - 1, value_simple("x", 3.0)),
            event(5.0, -4, value_simple("x", 4.0)),
            event(6.0, 2**62, value_simple("y", 5.0)),
        ],
    )
    root = tmp_path / "store"
    report = run_import(root, "tensorboard", str(tmp_path / "logs"))
    (row,) = report.rows
    assert (row.status, row.points, row.dropped_points) == ("imported", 2, 3)
    points = RunReader(root, row.run_id).series("x")["points"]
    assert points == [[0, 1.0], [2**32 - 1, 3.0]]


def test_wall_time_garbage_does_not_break_the_import(tmp_path):
    write_events(
        tmp_path / "logs" / "e" / "events.out.tfevents.1.h.1.0",
        [
            version_event(100.0),
            event(float("nan"), 0, value_simple("x", 1.0)),
            event(-5.0, 1, value_simple("x", 2.0)),
            event(1e300, 2, value_simple("x", 3.0)),
            event(101.0, 3, value_simple("x", 4.0)),
        ],
    )
    root = tmp_path / "store"
    report = run_import(root, "tensorboard", str(tmp_path / "logs"))
    (row,) = report.rows
    assert (row.status, row.points) == ("imported", 4)
    meta = RunReader(root, row.run_id).meta()
    assert meta["created_at"].startswith("1970-01-01T00:01:40")


def test_symlinked_directories_are_not_followed(tmp_path):
    base = tmp_path / "logs"
    write_events(base / "real" / "events.out.tfevents.1.h.1.0", [version_event(1.0)])
    try:
        os.symlink(base / "real", base / "loop")
        os.symlink(base, base / "real" / "up")
    except OSError:
        pytest.skip("symlinks not available")
    runs, _ = read(base)
    assert [run.name for run in runs] == ["real"]


def test_run_filter_and_project_options_work_through_run_import(tmp_path):
    root = tmp_path / "store"
    report = run_import(
        root,
        "tensorboard",
        str(FIXTURES),
        ImportOptions(project="mine", run_globs=["torch_scalars/*"]),
    )
    assert sorted(row.name for row in report.rows) == ["torch_scalars/loss_train", "torch_scalars/loss_val"]
    assert RunReader(root, report.rows[0].run_id).meta()["project"] == "mine"


def test_same_logdir_copied_elsewhere_gets_different_ids(tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(FIXTURES, copy)
    root = tmp_path / "store"
    first = run_import(root, "tensorboard", str(FIXTURES))
    second = run_import(root, "tensorboard", str(copy))
    assert second.imported == 5
    assert not {r.run_id for r in first.rows} & {r.run_id for r in second.rows}

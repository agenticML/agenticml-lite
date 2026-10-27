"""On-disk store: round trip, crashes, resume, and a reader beside a writer."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import re
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from agenticml.store import (
    OVERVIEW_MIN_POINTS,
    Overview,
    RunFinishedError,
    RunReader,
    RunWriter,
    has,
    list_runs,
    new_run_id,
    object_path,
    open_object,
    path_for,
    put_bytes,
    put_file,
    resolve_root,
    valid_run_id,
    valid_sha,
)
from agenticml.store.reader import STALE_AFTER_SECONDS

_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{8}$")
# finish persists <id>.ovw only strictly above OVERVIEW_MIN_POINTS.
_OVW_SERIES = OVERVIEW_MIN_POINTS + 1
_SHA_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def _index(writer: RunWriter) -> dict:
    return json.loads((writer.run_dir / "scalars" / "index.json").read_text(encoding="utf-8"))


def _files(root: Path) -> dict:
    snap = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        stat = path.stat()
        snap[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return snap


def _strip_last(points):
    return [point[:-1] for point in points]


def test_resolve_root_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_DIR", raising=False)
    assert resolve_root() == Path("~/.agenticml").expanduser().resolve()
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path / "from-env"))
    assert resolve_root() == (tmp_path / "from-env").resolve()
    explicit = tmp_path / "explicit"
    assert resolve_root(explicit) == explicit.resolve()
    assert resolve_root(explicit) != (tmp_path / "from-env").resolve()


def test_new_run_id_shape():
    ids = {new_run_id() for _ in range(20)}
    assert len(ids) == 20
    for run_id in ids:
        assert valid_run_id(run_id)
        assert _RUN_ID_RE.fullmatch(run_id)


@pytest.mark.parametrize(
    "run_id",
    ["../x", "a/b", ".hidden", "", "..", ".", "-a", "a" * 129, "has space", "a\\b", "_hidden"],
)
def test_invalid_run_id_rejected(tmp_path, run_id):
    assert not valid_run_id(run_id)
    root = tmp_path / "root"
    with pytest.raises(ValueError):
        RunWriter.create(root, run_id=run_id, meta={})
    with pytest.raises(ValueError):
        RunReader(root, run_id)
    with pytest.raises(ValueError):
        RunWriter.resume(root, run_id, must=True)
    assert not (tmp_path / "x").exists()


def test_long_run_id_accepted(tmp_path):
    run_id = "a" * 128
    assert valid_run_id(run_id)
    assert valid_run_id("foo_bar.baz-1")
    writer = RunWriter.create(tmp_path, run_id=run_id, meta={})
    assert writer.run_id == run_id
    assert writer.root == tmp_path.resolve()
    assert writer.run_dir == tmp_path.resolve() / "runs" / run_id


@pytest.mark.parametrize("sha", ["../x", "a/b", "A" * 64, "g" * 64, "a" * 63, "a" * 65, ""])
def test_invalid_sha_rejected(tmp_path, sha):
    assert not valid_sha(sha)
    with pytest.raises(ValueError):
        object_path(tmp_path, sha)
    with pytest.raises(ValueError):
        has(tmp_path, sha)
    with pytest.raises(ValueError):
        path_for(tmp_path, sha)


def test_object_dedup_and_chunked_copy(tmp_path):
    blob = b"abc"
    sha = put_bytes(tmp_path, blob)
    assert sha == _SHA_ABC
    dest = path_for(tmp_path, sha)
    inode = dest.stat().st_ino
    assert put_bytes(tmp_path, blob) == sha
    assert dest.stat().st_ino == inode
    assert has(tmp_path, sha)
    assert dest.read_bytes() == blob

    missing = "ab" * 32
    assert valid_sha(missing)
    assert not has(tmp_path, missing)
    assert not path_for(tmp_path, missing).exists()

    payload = b"x" * (1024 * 1024 + 17)
    src = tmp_path / "checkpoint.bin"
    src.write_bytes(payload)
    file_sha, size = put_file(tmp_path, src)
    assert size == len(payload)
    assert file_sha == hashlib.sha256(payload).hexdigest()
    assert path_for(tmp_path, file_sha).read_bytes() == payload
    inode = path_for(tmp_path, file_sha).stat().st_ino
    again, _ = put_file(tmp_path, src)
    assert again == file_sha
    assert path_for(tmp_path, file_sha).stat().st_ino == inode

    same, same_size = put_file(tmp_path, src)
    assert same == file_sha
    # put_bytes of the same payload must not create a second object.
    assert put_bytes(tmp_path, payload) == file_sha
    names = [
        path.name
        for path in (tmp_path / "objects").rglob("*")
        if path.is_file() and not path.name.startswith(".")
    ]
    assert names.count(file_sha) == 1
    assert same_size == len(payload)


def test_round_trip(tmp_path):
    src = tmp_path / "model.bin"
    src.write_bytes(b"weights-v1")
    copied = tmp_path / "copied.bin"
    copied.write_bytes(b"snapshot")
    writer = RunWriter.create(
        tmp_path,
        meta={
            "project": "proj",
            "name": "exp",
            "config": {"lr": 0.01, "layers": [1, 2]},
            "tags": ["a", "b"],
            "notes": "héllo",
            "group": "g",
            "job_type": "train",
            "parent_run_id": "parent1",
            "env": {"python": "3.9"},
            "git": {"commit": "abc"},
        },
    )
    assert writer.last_step is None
    reader = RunReader(tmp_path, writer.run_id)
    meta = reader.meta()
    assert meta["format_version"] == 1
    assert meta["status"] == "running"
    assert meta["project"] == "proj"
    assert meta["notes"] == "héllo"
    assert meta["config"] == {"lr": 0.01, "layers": [1, 2]}
    assert meta["env"] == {"python": "3.9"}
    assert meta["git"] == {"commit": "abc"}
    assert meta["finished_at"] is None
    assert meta["pid"] == os.getpid()
    assert meta["hostname"] == socket.gethostname()
    assert meta["created_at"].endswith("Z")
    assert reader.events() == []
    assert "effective_status" not in meta

    writer.write_scalars([(0, {"loss": 1.0}), (1, {"loss": 2.0})])
    writer.write_scalars([(2, {"loss": 3.0, "acc": 1.0})])
    assert writer.last_step == 2

    with pytest.raises(ValueError):
        writer.set_meta_fields(run_id="nope")
    with pytest.raises(ValueError):
        writer.set_meta_fields(format_version=2)
    writer.set_summary({"mid": 1})
    assert RunReader(tmp_path, writer.run_id).meta()["summary"] == {"mid": 1}

    writer.set_config({"lr": 0.02, "momentum": 0.9})
    code_sha = hashlib.sha256(b"print(1)\n").hexdigest()
    writer.add_code(
        [
            {"path": "a.py", "content_type": "text/x-python", "source": "user"},
            {"path": "b.py", "content_type": "text/x-python", "source": "code"},
        ],
        [("a.py", b"print(1)\n"), ("b.py", b"bbb"), ("c.py", b"extra")],
    )
    writer.add_code(
        [{"path": "a.py", "content_type": "text/x-python", "source": "rewrite"}],
        [("a.py", b"print(2)\n")],
    )
    ref = writer.add_artifact(src, "model", "model", {"epoch": 1}, copy=False)
    copied_rec = writer.add_artifact(copied, "model", "model", {"epoch": 2}, copy=True)
    other = writer.add_artifact(src, "data", "dataset", None, copy=False)
    media_sha = writer.add_media("pred", 3, b"png-bytes", "image/png", "first")
    media_sha_2 = writer.add_media("pred", 4, b"png-bytes", "image/png", None)
    assert media_sha == media_sha_2
    assert ref["version"] == 1 and ref["sha256"] is None and ref["copy"] is False
    assert copied_rec["version"] == 2
    assert copied_rec["sha256"] == hashlib.sha256(b"snapshot").hexdigest()
    assert other["version"] == 1
    assert Path(ref["path"]).is_absolute()
    artifact_events = [
        event
        for event in RunReader(tmp_path, writer.run_id).events()
        if event["kind"] == "artifact"
    ]
    for record, event in (
        (ref, artifact_events[0]),
        (copied_rec, artifact_events[1]),
        (other, artifact_events[2]),
    ):
        for key, value in record.items():
            assert event[key] == value

    writer.finish("finished", {"best": 1})
    assert writer.last_step == 2
    writer.finish("crashed", {"best": 9})

    reader = RunReader(tmp_path, writer.run_id)
    done = reader.meta()
    assert done["status"] == "finished"
    assert done["summary"] == {"best": 1}
    assert done["finished_at"].endswith("Z")
    assert [event["kind"] for event in reader.events()] == [
        "config_update",
        "code",
        "code",
        "artifact",
        "artifact",
        "artifact",
        "finish",
    ]
    assert reader.events()[0]["config"] == {"lr": 0.02, "momentum": 0.9}
    assert reader.events()[-1]["status"] == "finished"
    assert reader.events()[-1]["summary"] == {"best": 1}

    manifest = {entry["path"]: entry for entry in reader.code_manifest()}
    assert set(manifest) == {"a.py", "b.py", "c.py"}
    assert manifest["a.py"]["source"] == "rewrite"
    assert manifest["a.py"]["sha256"] == hashlib.sha256(b"print(2)\n").hexdigest()
    assert manifest["b.py"]["sha256"] == hashlib.sha256(b"bbb").hexdigest()
    assert manifest["b.py"]["content_type"] == "text/x-python"
    assert manifest["c.py"]["source"] == "code"
    assert path_for(tmp_path, code_sha).read_bytes() == b"print(1)\n"
    assert has(tmp_path, copied_rec["sha256"])
    assert not has(tmp_path, "0" * 64)

    assert reader.artifacts()[0]["name"] == "model"
    assert reader.artifacts()[2]["name"] == "data"
    media = reader.media("pred")
    assert [row["step"] for row in media] == [3, 4]
    assert media[0]["sha256"] == media_sha
    assert media[0]["caption"] == "first"
    assert media[1]["caption"] is None
    assert reader.media("missing") == []
    assert "pred" in reader.media()

    assert reader.keys() == ["loss", "acc"]
    raw = reader.series("loss", max_points=10)
    assert raw["mode"] == "raw"
    assert raw["total"] == 3
    assert raw["points"] == [[0, 1.0], [1, 2.0], [2, 3.0]]
    chart = reader.series("loss", max_points=1)
    assert chart["mode"] == "overview"
    assert chart["total"] == 3
    assert chart["nonfinite"] == 0
    assert chart["points"] == [[0, 2, 1.0, 3.0, 2.0, 3.0]]
    times = reader.time_index()
    assert [step for step, _seconds in times] == [1, 2]
    assert all(seconds >= 0 for _step, seconds in times)

    with pytest.raises(KeyError):
        reader.series("nope")
    with pytest.raises(RuntimeError):
        writer.write_scalars([(3, {"loss": 1.0})])

    snap = _files(tmp_path)
    reader.meta()
    reader.keys()
    reader.events()
    reader.code_manifest()
    reader.artifacts()
    reader.media()
    reader.time_index()
    reader.series("loss")
    reader.read_records("loss", 0)
    list_runs(tmp_path)
    assert _files(tmp_path) == snap


def test_config_noop_does_not_append_or_touch_meta(tmp_path):
    writer = RunWriter.create(tmp_path, meta={"config": {"lr": 0.1, "b": 1}})
    writer.set_config({"b": 1, "lr": 0.1})
    assert RunReader(tmp_path, writer.run_id).events() == []
    meta_path = writer.run_dir / "meta.json"
    stamped = meta_path.stat().st_mtime_ns
    time.sleep(0.01)
    writer.set_config({"lr": 0.1, "b": 1})
    assert meta_path.stat().st_mtime_ns == stamped
    assert RunReader(tmp_path, writer.run_id).events() == []
    writer.set_config({"lr": 0.2})
    kinds = [event["kind"] for event in RunReader(tmp_path, writer.run_id).events()]
    assert kinds == ["config_update"]


def test_monotonic_flag_and_equal_steps(tmp_path):
    writer = RunWriter.create(tmp_path, meta={"project": "p"})
    writer.write_scalars([(0, {"a": 1.0}), (2, {"a": 2.0}), (2, {"a": 3.0})])
    assert _index(writer)["series"]["a"]["monotonic"] is True
    writer.write_scalars([(1, {"a": 0.0})])
    assert _index(writer)["series"]["a"]["monotonic"] is False


def test_invalid_points_dropped_once_and_nonfinite_kept(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agenticml.store.writer")
    writer = RunWriter.create(tmp_path, meta={"project": "p"})
    writer.write_scalars([])
    writer.write_scalars([(-1, {"loss": 1.0}), (2**32, {"loss": 1.0}), (1.5, {"loss": 1.0})])
    writer.write_scalars([(1, {"loss": "bad", "acc": 0.5, "flag": True})])
    writer.write_scalars([(2, {"loss": "still", "big": 1e39, "neg": -1e39})])
    writer.write_scalars([(3, {"loss": float("nan")}), (4, {"loss": float("inf")}), (5, {"loss": 4.0})])
    reader = RunReader(tmp_path, writer.run_id)
    # loss was dropped until the nan/inf/4.0 points. acc and flag and big exist.
    assert "acc" in reader.keys()
    acc, _ = reader.read_records("acc", 0)
    assert acc == [(1, 0.5)]
    flag, _ = reader.read_records("flag", 0)
    assert flag == [(1, 1.0)]
    big, _ = reader.read_records("big", 0)
    assert math.isinf(big[0][1]) and big[0][1] > 0
    neg, _ = reader.read_records("neg", 0)
    assert math.isinf(neg[0][1]) and neg[0][1] < 0
    loss = reader.series("loss", max_points=10)
    assert loss["mode"] == "raw"
    assert loss["total"] == 3
    assert loss["nonfinite"] == 2
    assert loss["points"][2] == [5, 4.0]
    assert math.isnan(loss["points"][0][1])
    chart = reader.series("loss", max_points=1)
    assert chart["mode"] == "overview"
    assert chart["nonfinite"] == 2
    assert chart["points"][0][0] == 5 and chart["points"][0][1] == 5
    loss_warnings = [rec for rec in caplog.records if "loss" in rec.getMessage()]
    assert len(loss_warnings) == 2
    # Dropped-only calls do not append _time.bin. Accepted calls did:
    # step 1 (acc/flag), step 2 (overflows), step 5 (loss).
    steps = [step for step, _seconds in reader.time_index()]
    assert steps == [1, 2, 5]

    edge = RunWriter.create(tmp_path, run_id="edge-step", meta={})
    edge.write_scalars([(2**32 - 1, {"edge": 1.0})])
    records, _ = RunReader(tmp_path, "edge-step").read_records("edge", 0)
    assert records[0][0] == 2**32 - 1


def test_bad_item_shape_writes_nothing_new(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    writer.write_scalars([(0, {"a": 1.0})])
    with pytest.raises(TypeError):
        writer.write_scalars([(1, {"a": 2.0}), "bad"])
    records, _ = RunReader(tmp_path, writer.run_id).read_records("a", 0)
    assert [step for step, _value in records] == [0]


def test_torn_bin_ignored_then_healed_on_resume(tmp_path):
    writer = RunWriter.create(tmp_path, meta={"project": "p"})
    writer.write_scalars([(0, {"loss": 1.0}), (1, {"loss": 2.0})])
    writer.close()
    path = writer.run_dir / "scalars" / "0.bin"
    with path.open("ab") as handle:
        handle.write(b"\x01\x02\x03")
    reader = RunReader(tmp_path, writer.run_id)
    records, offset = reader.read_records("loss", 0)
    assert records == [(0, 1.0), (1, 2.0)]
    assert offset == 16
    assert reader.series("loss")["total"] == 2

    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    assert path.stat().st_size % 8 == 0
    resumed.write_scalars([(2, {"loss": 3.0})])
    records, offset = RunReader(tmp_path, writer.run_id).read_records("loss", 0)
    assert [step for step, _value in records] == [0, 1, 2]
    assert offset == 24
    mid, mid_off = RunReader(tmp_path, writer.run_id).read_records("loss", 8)
    assert [step for step, _value in mid] == [1, 2]
    assert mid_off == 24
    empty, empty_off = RunReader(tmp_path, writer.run_id).read_records("loss", 24)
    assert empty == [] and empty_off == 24
    with pytest.raises(ValueError):
        RunReader(tmp_path, writer.run_id).read_records("loss", 3)


def test_torn_events_and_newline_repair(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agenticml.store.layout")
    writer = RunWriter.create(tmp_path, meta={})
    writer.set_config({"a": 1})
    writer.finish("finished", {"s": 1})
    path = writer.run_dir / "events.jsonl"
    # A finished JSON object that lost its newline is closed, not deleted.
    path.write_bytes(path.read_bytes()[:-1])
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    kinds = [event["kind"] for event in RunReader(tmp_path, writer.run_id).events()]
    assert "finish" in kinds and "resume" in kinds
    for line in raw.splitlines():
        json.loads(line)

    lines = raw.splitlines(keepends=True)
    lines.insert(-1, b"{not json}\n")
    path.write_bytes(b"".join(lines) + b'{"t":1,"kind":')
    caplog.clear()
    events = RunReader(tmp_path, writer.run_id).events()
    assert events[-1]["kind"] == "resume"
    assert sum("unparseable" in rec.getMessage() for rec in caplog.records) == 1
    resumed.close()
    again = RunWriter.resume(tmp_path, writer.run_id, must=True)
    repaired = (again.run_dir / "events.jsonl").read_bytes()
    bad = 0
    for line in repaired.splitlines():
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            assert line == b"{not json}"
    assert bad == 1
    assert b'{"t":1,"kind":' not in repaired


def test_resume_missing_and_last_step(tmp_path):
    assert RunWriter.resume(tmp_path, "missing-run", must=False) is None
    with pytest.raises(FileNotFoundError):
        RunWriter.resume(tmp_path, "missing-run", must=True)
    (tmp_path / "runs" / "empty-run").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        RunWriter.resume(tmp_path, "empty-run", must=False)

    writer = RunWriter.create(tmp_path, run_id="missing-run", meta={"project": "p"})
    with pytest.raises(FileExistsError):
        RunWriter.create(tmp_path, run_id="missing-run", meta={})
    src = tmp_path / "w.bin"
    src.write_bytes(b"w")
    writer.add_artifact(src, "model", "model", {}, copy=False)
    writer.add_artifact(src, "model", "model", {}, copy=False)
    writer.write_scalars([(1, {"a": 1.0}), (10, {"a": 2.0}), (3, {"a": 3.0})])
    assert writer.last_step == 10
    assert _index(writer)["series"]["a"]["monotonic"] is False
    writer.close()
    resumed = RunWriter.resume(tmp_path, "missing-run", must=True)
    assert resumed.last_step == 10
    third = resumed.add_artifact(src, "model", "model", {}, copy=False)
    assert third["version"] == 3
    assert RunReader(tmp_path, "missing-run").meta()["status"] == "running"

    mono = RunWriter.create(tmp_path, run_id="mono-run", meta={})
    mono.write_scalars([(1, {"a": 1.0}), (2, {"a": 2.0}), (10, {"a": 3.0})])
    mono.close()
    assert RunWriter.resume(tmp_path, "mono-run", must=True).last_step == 10


def test_resume_after_finish_clears_terminal_status(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    writer.write_scalars([(0, {"loss": 1.0}), (4, {"loss": 2.0})])
    writer.finish("finished", {"s": 1})
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    meta = RunReader(tmp_path, writer.run_id).meta()
    assert meta["status"] == "running"
    assert meta["finished_at"] is None
    assert meta["pid"] == os.getpid()
    assert any(event["kind"] == "resume" for event in RunReader(tmp_path, writer.run_id).events())
    assert resumed.last_step == 4
    resumed.write_scalars([(5, {"loss": 3.0})])
    resumed.finish("crashed", {"s": 2})
    done = RunReader(tmp_path, writer.run_id).meta()
    assert done["status"] == "crashed"
    assert done["summary"] == {"s": 2}
    assert RunReader(tmp_path, writer.run_id).series("loss", max_points=10)["total"] == 3


def test_range_query_monotonic_matches_scan(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    count = 5000
    writer.write_scalars([(step, {"mono": float(step)}) for step in range(count)])
    writer.write_scalars([(step, {"non": float(step)}) for step in range(count - 1, -1, -1)])
    assert _index(writer)["series"]["mono"]["monotonic"] is True
    assert _index(writer)["series"]["non"]["monotonic"] is False
    writer.finish("finished", {})
    reader = RunReader(tmp_path, writer.run_id)

    raw_m = reader.series("mono", max_points=1000, start=100, end=1099)
    raw_n = reader.series("non", max_points=1000, start=100, end=1099)
    assert raw_m["mode"] == raw_n["mode"] == "raw"
    assert raw_m["total"] == raw_n["total"] == 1000
    assert sorted(raw_m["points"]) == sorted(raw_n["points"])
    assert raw_m["points"] == [[step, float(step)] for step in range(100, 1100)]

    chart_m = reader.series("mono", max_points=10, start=0, end=count - 1)
    chart_n = reader.series("non", max_points=10, start=0, end=count - 1)
    assert chart_m["mode"] == chart_n["mode"] == "overview"
    assert chart_m["total"] == chart_n["total"] == count
    assert _strip_last(chart_m["points"]) == _strip_last(chart_n["points"])

    full_m = reader.series("mono", max_points=100)
    full_n = reader.series("non", max_points=100)
    assert full_m["mode"] == "overview"
    assert _strip_last(full_m["points"]) == _strip_last(full_n["points"])


def test_bisect_duplicate_steps_are_inclusive(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    points = [(1, 1.0), (2, 2.0), (2, 3.0), (2, 4.0), (5, 5.0)]
    writer.write_scalars([(step, {"m": value}) for step, value in points])
    # A decrease anywhere would flip the flag; this series stays monotonic.
    shuffled = [(5, 9.0), (2, 2.0), (1, 1.0), (2, 3.0), (2, 4.0)]
    writer.write_scalars([(step, {"n": value}) for step, value in shuffled])
    reader = RunReader(tmp_path, writer.run_id)
    mono = reader.series("m", max_points=10, start=2, end=2)
    assert mono["points"] == [[2, 2.0], [2, 3.0], [2, 4.0]]
    non = reader.series("n", max_points=10, start=2, end=2)
    assert sorted(non["points"]) == sorted([[2, 2.0], [2, 3.0], [2, 4.0]])
    assert reader.series("m", max_points=10, start=5)["points"] == [[5, 5.0]]
    assert reader.series("m", max_points=10, end=1)["points"] == [[1, 1.0]]
    assert reader.series("m", max_points=10, start=3, end=4)["points"] == []


def test_reader_while_writer_appends(tmp_path):
    writer = RunWriter.create(tmp_path, meta={"project": "p"})
    stop = threading.Event()
    errors = []
    produced = [0]

    def produce():
        step = 0
        try:
            while not stop.is_set():
                writer.write_scalars([(step, {"loss": float(step)})])
                step += 1
        except Exception as exc:
            errors.append(exc)
        finally:
            produced[0] = step

    thread = threading.Thread(target=produce)
    thread.start()
    reader = RunReader(tmp_path, writer.run_id)
    deadline = time.time() + 5
    seen = False
    while time.time() < deadline:
        if "loss" in reader.keys():
            snap = reader.series("loss", max_points=50)
            if snap["total"] >= 1 and snap["mode"] in ("raw", "overview"):
                seen = True
                break
        time.sleep(0.01)
    assert seen
    for _ in range(10):
        snap = reader.series("loss", max_points=50)
        assert snap["total"] >= 1
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    writer.finish("finished", {})
    records, _ = RunReader(tmp_path, writer.run_id).read_records("loss", 0)
    assert len(records) == produced[0]


def test_threads_share_the_writer_lock(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    errors = []

    def even():
        try:
            for step in range(0, 400, 2):
                writer.write_scalars([(step, {"loss": float(step), "even": 1.0})])
        except Exception as exc:
            errors.append(exc)

    def odd():
        try:
            for step in range(1, 400, 2):
                writer.write_scalars([(step, {"loss": float(step)})])
        except Exception as exc:
            errors.append(exc)

    def media_and_config():
        try:
            for i in range(40):
                writer.add_media("frame", i, bytes([i]), "application/octet-stream", None)
                writer.set_config({"i": i})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=fn) for fn in (even, odd, media_and_config)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    writer.finish("finished", {"ok": True})
    reader = RunReader(tmp_path, writer.run_id)
    records, _ = reader.read_records("loss", 0)
    assert len(records) == 400
    assert {step for step, _value in records} == set(range(400))
    assert len(reader.read_records("even", 0)[0]) == 200
    assert len(reader.media("frame")) == 40
    assert reader.meta()["config"] == {"i": 39}
    assert reader.meta()["summary"] == {"ok": True}


def test_handle_cap_keeps_later_series_readable(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    first = {f"m{i}": float(i) for i in range(300)}
    second = {f"m{i}": float(i) + 1 for i in range(300)}
    writer.write_scalars([(1, first)])
    writer.write_scalars([(2, second)])
    assert len(writer._handles) == 256
    reader = RunReader(tmp_path, writer.run_id)
    assert len(reader.keys()) == 300
    for key in ("m0", "m255", "m299"):
        points = reader.series(key, max_points=10)["points"]
        assert len(points) == 2
        assert points[0][0] == 1 and points[1][0] == 2


def test_running_ignores_overview_file(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(50)])
    ovw = writer.run_dir / "scalars" / "0.ovw"
    ovw.write_bytes(Overview().to_bytes())
    snap = RunReader(tmp_path, writer.run_id).series("loss", max_points=10)
    assert snap["total"] == 50
    assert snap["mode"] == "overview"


def test_finished_overview_and_fallback(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    count = _OVW_SERIES
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(count)])
    writer.finish("finished", {})
    ovw = writer.run_dir / "scalars" / "0.ovw"
    blob = ovw.read_bytes()
    ovw.unlink()
    assert RunReader(tmp_path, writer.run_id).series("loss", max_points=10)["total"] == count
    ovw.write_bytes(b"AMLO-this-is-not-a-valid-overview!!")
    assert RunReader(tmp_path, writer.run_id).series("loss", max_points=10)["total"] == count
    ovw.write_bytes(blob)
    bin_path = writer.run_dir / "scalars" / "0.bin"
    bin_path.write_bytes(bin_path.read_bytes()[: 10 * 8])
    assert RunReader(tmp_path, writer.run_id).series("loss", max_points=10)["total"] == 10
    bin_path.unlink()
    pruned = RunReader(tmp_path, writer.run_id).series("loss", max_points=10)
    assert pruned["total"] == count
    assert pruned["mode"] == "overview"


def test_incremental_overview_cache_reads_new_bytes(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(5000)])
    reader = RunReader(tmp_path, writer.run_id)
    first = reader.series("loss", max_points=100)
    assert first["total"] == 5000
    assert reader._cache["loss"][0] == "inc"
    cached = reader._cache["loss"][1]
    assert reader._cache["loss"][2] == 5000 * 8
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(5000, 6000)])
    second = reader.series("loss", max_points=100)
    assert second["total"] == 6000
    assert reader._cache["loss"][1] is cached
    assert reader._cache["loss"][2] == 6000 * 8
    fresh = RunReader(tmp_path, writer.run_id).series("loss", max_points=100)
    assert fresh["points"] == second["points"]


def test_list_runs_order_skip_and_liveness(tmp_path):
    older = RunWriter.create(tmp_path, run_id="older-run", meta={"project": "p"})
    newer = RunWriter.create(tmp_path, run_id="newer-run", meta={"project": "p"})
    older.set_meta_fields(created_at="2020-01-01T00:00:00.000000Z")
    newer.set_meta_fields(created_at="2021-01-01T00:00:00.000000Z")
    older.write_scalars([(0, {"loss": 1.0})])
    (tmp_path / "runs" / ".hidden").mkdir()
    (tmp_path / "runs" / ".hidden" / "meta.json").write_text("{}", encoding="utf-8")
    corrupt = tmp_path / "runs" / "corrupt1"
    corrupt.mkdir()
    (corrupt / "meta.json").write_text("{", encoding="utf-8")
    (tmp_path / "runs" / "notadir").write_text("x", encoding="utf-8")

    listed = list_runs(tmp_path)
    assert [item["run_id"] for item in listed] == ["newer-run", "older-run"]
    assert listed[0]["effective_status"] == "running"
    disk = json.loads((newer.run_dir / "meta.json").read_text(encoding="utf-8"))
    assert "effective_status" not in disk

    old = time.time() - (STALE_AFTER_SECONDS + 30)
    meta_path = older.run_dir / "meta.json"
    time_bin = older.run_dir / "scalars" / "_time.bin"
    os.utime(meta_path, (old, old))
    os.utime(time_bin, (old, old))
    by_id = {item["run_id"]: item for item in list_runs(tmp_path)}
    assert by_id["older-run"]["effective_status"] == "running"
    os.utime(time_bin, None)
    by_id = {item["run_id"]: item for item in list_runs(tmp_path)}
    assert by_id["older-run"]["effective_status"] == "running"
    os.utime(meta_path, (old, old))
    os.utime(time_bin, (old, old))
    older.finish("finished", {})
    os.utime(older.run_dir / "meta.json", (old, old))
    os.utime(time_bin, (old, old))
    by_id = {item["run_id"]: item for item in list_runs(tmp_path)}
    assert by_id["older-run"]["effective_status"] == "finished"

    quiet = RunWriter.create(tmp_path, run_id="quiet-run", meta={})
    os.utime(quiet.run_dir / "meta.json", (old, old))
    by_id = {item["run_id"]: item for item in list_runs(tmp_path)}
    assert by_id["quiet-run"]["effective_status"] == "running"
    assert list_runs(tmp_path / "does-not-exist") == []


def test_list_runs_thousand_is_fast(tmp_path):
    for i in range(1000):
        RunWriter.create(tmp_path, meta={"project": "p", "name": str(i)})
    started = time.perf_counter()
    found = list_runs(tmp_path)
    elapsed = time.perf_counter() - started
    assert len(found) == 1000
    assert elapsed < 1.0


def test_missing_reader(tmp_path):
    with pytest.raises(FileNotFoundError):
        RunReader(tmp_path, "no-such")


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "runs").mkdir(parents=True)
    (root / "runs" / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        RunReader(root, "linked")
    with pytest.raises(ValueError):
        RunWriter.resume(root, "linked", must=True)
    with pytest.raises(ValueError):
        RunWriter.create(root, run_id="linked", meta={})
    assert list(outside.iterdir()) == []

    writer = RunWriter.create(root, run_id="realrun", meta={})
    writer.write_scalars([(0, {"a": 1.0})])
    writer.finish("finished", {})
    scalars = writer.run_dir / "scalars"
    scalars.rename(tmp_path / "scalars-real")
    scalars.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        RunReader(root, "realrun").series("a")
    with pytest.raises(ValueError):
        RunWriter.resume(root, "realrun", must=True)
    assert list(outside.iterdir()) == []

    payload = b"zz-escape"
    sha = hashlib.sha256(payload).hexdigest()
    hh = root / "objects" / sha[:2]
    hh.parent.mkdir(parents=True, exist_ok=True)
    hh.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        object_path(root, sha)
    with pytest.raises(ValueError):
        put_bytes(root, payload)
    assert list(outside.iterdir()) == []


def test_pruned_run_cannot_resume_or_clobber_overview(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(_OVW_SERIES)])
    writer.finish("finished", {})
    ovw = writer.run_dir / "scalars" / "0.ovw"
    blob = ovw.read_bytes()
    (writer.run_dir / "scalars" / "0.bin").unlink()
    with pytest.raises(ValueError, match="was pruned after sync"):
        RunWriter.resume(tmp_path, writer.run_id, must=True)
    writer._write_overviews()
    assert ovw.read_bytes() == blob
    assert RunReader(tmp_path, writer.run_id).meta()["status"] == "finished"


def test_python_float_overflow_drops_point_and_keeps_series(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    writer.write_scalars([(0, {"good": 1, "huge": 10**1000})])
    writer.write_scalars([(1, {"good": 2})])
    reader = RunReader(tmp_path, writer.run_id)
    assert "huge" not in reader.keys()
    got = reader.series("good", max_points=10)
    assert got["total"] == 2
    assert [point[0] for point in got["points"]] == [0, 1]
    assert [point[1] for point in got["points"]] == [1.0, 2.0]


def test_index_failure_does_not_leave_unpublished_series(tmp_path, monkeypatch):
    writer = RunWriter.create(tmp_path, meta={})
    real = RunWriter._persist_index

    def boom(self):
        raise OSError("disk")

    monkeypatch.setattr(RunWriter, "_persist_index", boom)
    with pytest.raises(OSError):
        writer.write_scalars([(0, {"good": 1.0})])
    monkeypatch.setattr(RunWriter, "_persist_index", real)
    writer.write_scalars([(1, {"good": 2.0})])
    records, _ = RunReader(tmp_path, writer.run_id).read_records("good", 0)
    assert [step for step, _value in records] == [1]


def test_series_cache_is_thread_safe(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    writer.write_scalars([(0, {"loss": 0.0})])
    reader = RunReader(tmp_path, writer.run_id)
    errors = []
    stop = threading.Event()

    def hammer():
        try:
            while not stop.is_set():
                snap = reader.series("loss", max_points=1)
                if snap["total"] < 1:
                    errors.append(snap["total"])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    try:
        for step in range(1, 300):
            writer.write_scalars([(step, {"loss": float(step)})])
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    on_disk, _ = reader.read_records("loss", 0)
    assert reader.series("loss", max_points=1)["total"] == len(on_disk)


def test_range_reads_monotonic_after_stat(tmp_path, monkeypatch):
    import agenticml.store.reader as reader_mod

    writer = RunWriter.create(tmp_path, meta={})
    writer.write_scalars([(1, {"a": 1.0}), (2, {"a": 2.0}), (3, {"a": 3.0})])
    reader = RunReader(tmp_path, writer.run_id)
    state = {"done": False}
    real = reader_mod._file_size

    def file_size(path):
        if Path(path).name == "0.bin" and not state["done"]:
            state["done"] = True
            writer.write_scalars([(0, {"a": 0.0})])
        return real(path)

    monkeypatch.setattr(reader_mod, "_file_size", file_size)
    result = reader.series("a", max_points=10, start=0, end=0)
    assert state["done"]
    assert result["total"] == 1
    assert result["points"] == [[0, 0.0]]


def test_overview_cache_invalidates_on_resume_and_prune(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    count = _OVW_SERIES
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(count)])
    writer.finish("finished", {})
    reader = RunReader(tmp_path, writer.run_id)
    assert reader.series("loss", max_points=5)["total"] == count
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    resumed.write_scalars([(step, {"loss": float(step)}) for step in range(count, count + 1000)])
    resumed.finish("finished", {})
    assert reader.series("loss", max_points=5)["total"] == count + 1000
    (resumed.run_dir / "scalars" / "0.bin").unlink()
    pruned = reader.series("loss", max_points=5)
    assert pruned["total"] == count + 1000
    assert pruned["mode"] == "overview"


def test_event_lines_do_not_split_on_unicode_separators(tmp_path):
    writer = RunWriter.create(tmp_path, meta={})
    note = "line\u2028sep\x85next"
    writer.set_config({"note": note})
    src = tmp_path / "artifact.bin"
    src.write_bytes(b"v1")
    first = writer.add_artifact(src, "model", "model", {}, copy=False)
    second = writer.add_artifact(src, "model", "model", {}, copy=False)
    assert first["version"] == 1 and second["version"] == 2
    notes = [
        event["config"]["note"]
        for event in RunReader(tmp_path, writer.run_id).events()
        if event.get("kind") == "config_update"
    ]
    assert notes == [note]
    writer.close()
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    third = resumed.add_artifact(src, "model", "model", {}, copy=False)
    assert third["version"] == 3


def test_finish_fsyncs_scalar_and_media_dirs(tmp_path, monkeypatch):
    import agenticml.store.writer as writer_mod

    seen = []
    real = writer_mod.fsync_dir

    def spy(path):
        seen.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(writer_mod, "fsync_dir", spy)
    writer = RunWriter.create(tmp_path, meta={})
    writer.write_scalars([(0, {"a": 1.0})])
    writer.add_media("frame", 0, b"x", "application/octet-stream", None)
    writer.finish("finished", {})
    assert "scalars" in seen
    assert "media" in seen
    assert writer.run_id in seen


def test_event_heal_does_not_reread_on_append(tmp_path, monkeypatch):
    writer = RunWriter.create(tmp_path, meta={})
    reads = {"n": 0}
    real = Path.read_bytes

    def counting(self, *args, **kwargs):
        if self.name == "events.jsonl":
            reads["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", counting)
    writer.set_config({"a": 1})
    writer.set_config({"a": 2})
    writer.finish("finished", {})
    assert reads["n"] == 0
    reads["n"] = 0
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    after_open = reads["n"]
    assert after_open >= 1
    resumed.set_config({"a": 3})
    resumed.set_config({"a": 4})
    assert reads["n"] == after_open


def test_corrupt_overview_falls_back_then_empty(tmp_path, caplog):
    from agenticml.store.overview import _HEADER

    caplog.set_level(logging.WARNING, logger="agenticml.store.reader")
    writer = RunWriter.create(tmp_path, meta={})
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(_OVW_SERIES)])
    writer.finish("finished", {})
    ovw = writer.run_dir / "scalars" / "0.ovw"
    blob = bytearray(ovw.read_bytes())
    struct.pack_into("<f", blob, _HEADER.size + 16, float("nan"))
    ovw.write_bytes(blob)
    reader = RunReader(tmp_path, writer.run_id)
    rebuilt = reader.series("loss", max_points=5)
    assert rebuilt["mode"] == "overview"
    assert rebuilt["total"] == _OVW_SERIES
    assert all(math.isfinite(point[2]) and math.isfinite(point[3]) for point in rebuilt["points"])
    (writer.run_dir / "scalars" / "0.bin").unlink()
    empty = reader.series("loss", max_points=5)
    assert empty["total"] == 0
    assert empty["points"] == []
    assert any("overview" in rec.getMessage().lower() for rec in caplog.records)


def test_tmp_names_are_unique_per_thread(tmp_path, monkeypatch):
    from agenticml.store.layout import atomic_write

    names = []
    real = os.replace

    def capture(src, dst):
        names.append(Path(src).name)
        return real(src, dst)

    monkeypatch.setattr(os, "replace", capture)
    barrier = threading.Barrier(2)
    idents = []
    errors = []

    def worker():
        try:
            ident = threading.get_ident()
            barrier.wait()
            atomic_write(tmp_path / f"f{ident}.json", b"v")
            idents.append(ident)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(names) == 2 and names[0] != names[1]
    for ident in idents:
        matched = [name for name in names if f".{os.getpid()}.{ident}." in name and name.endswith(".tmp")]
        assert len(matched) == 1

    names.clear()
    blob_barrier = threading.Barrier(2)
    blob_idents = []

    def put_worker():
        try:
            ident = threading.get_ident()
            blob_barrier.wait()
            put_bytes(tmp_path, f"blob-{ident}".encode())
            blob_idents.append(ident)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=put_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    for ident in blob_idents:
        matched = [name for name in names if f".{os.getpid()}.{ident}." in name and name.endswith(".tmp")]
        assert len(matched) == 1

    names.clear()
    src = tmp_path / "src.bin"
    src.write_bytes(b"file-bytes")
    put_file(tmp_path, src)
    assert len(names) == 1
    assert names[0].startswith(".tmp.")
    assert f".{os.getpid()}.{threading.get_ident()}." in names[0]
    assert names[0].endswith(".tmp")


def test_two_processes_put_same_object(tmp_path):
    import subprocess
    import sys

    code = "import sys; from agenticml.store import put_bytes; put_bytes(sys.argv[1], b'abc')"
    procs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)]) for _ in range(2)]
    assert [proc.wait(timeout=30) for proc in procs] == [0, 0]
    sha = hashlib.sha256(b"abc").hexdigest()
    assert path_for(tmp_path, sha).read_bytes() == b"abc"
    names = [
        path.name
        for path in (tmp_path / "objects").rglob("*")
        if path.is_file() and not path.name.startswith(".")
    ]
    assert names == [sha]


def test_overview_min_points_threshold(tmp_path):
    assert OVERVIEW_MIN_POINTS == 65536

    # 1,000 points -> no .ovw after finish, series() still correct
    writer_small = RunWriter.create(tmp_path, run_id="small", meta={})
    writer_small.write_scalars([(step, {"loss": float(step)}) for step in range(1000)])
    writer_small.finish("finished", {})
    ovw_small = writer_small.run_dir / "scalars" / "0.ovw"
    assert not ovw_small.exists()
    reader_small = RunReader(tmp_path, "small")
    result_small = reader_small.series("loss", max_points=100)
    assert result_small["mode"] == "overview"
    assert result_small["total"] == 1000
    assert len(result_small["points"]) == 100

    # Exactly the threshold: still no .ovw; the reader builds it from .bin.
    writer_at = RunWriter.create(tmp_path, run_id="at-threshold", meta={})
    writer_at.write_scalars([(step, {"loss": float(step)}) for step in range(OVERVIEW_MIN_POINTS)])
    writer_at.finish("finished", {})
    assert not (writer_at.run_dir / "scalars" / "0.ovw").exists()
    at_result = RunReader(tmp_path, "at-threshold").series("loss", max_points=100)
    assert at_result["mode"] == "overview"
    assert at_result["total"] == OVERVIEW_MIN_POINTS

    # One past the threshold -> .ovw present and <= 2048*36 + 40 bytes
    writer_large = RunWriter.create(tmp_path, run_id="large", meta={})
    writer_large.write_scalars([(step, {"loss": float(step)}) for step in range(_OVW_SERIES)])
    writer_large.finish("finished", {})
    ovw_large = writer_large.run_dir / "scalars" / "0.ovw"
    assert ovw_large.is_file()
    max_expected_bytes = 2048 * 36 + 40
    assert ovw_large.stat().st_size <= max_expected_bytes
    reader_large = RunReader(tmp_path, "large")
    result_large = reader_large.series("loss", max_points=1000)
    assert result_large["mode"] == "overview"
    assert result_large["total"] == _OVW_SERIES


def test_overview_stale_removal_and_pruned_retention(tmp_path):
    writer = RunWriter.create(tmp_path, run_id="threshold_stale", meta={})
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(_OVW_SERIES)])
    writer.finish("finished", {})
    ovw_path = writer.run_dir / "scalars" / "0.ovw"
    assert ovw_path.is_file()

    # Truncate to 1,000 points (now <= 65536)
    bin_path = writer.run_dir / "scalars" / "0.bin"
    bin_path.write_bytes(bin_path.read_bytes()[: 1000 * 8])
    writer._write_overviews()
    # Stale .ovw must be removed because points <= 65536 and .bin exists
    assert not ovw_path.exists()

    # If .bin is deleted (pruned), .ovw is retained
    fake_blob = Overview().to_bytes()
    ovw_path.write_bytes(fake_blob)
    bin_path.unlink()
    writer._write_overviews()
    assert ovw_path.is_file()
    assert ovw_path.read_bytes() == fake_blob


def test_reader_rejects_version_1_overview_fallback_to_bin(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agenticml.store.reader")
    writer = RunWriter.create(tmp_path, run_id="v1_fallback", meta={})
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(_OVW_SERIES)])
    writer.finish("finished", {})
    ovw_path = writer.run_dir / "scalars" / "0.ovw"
    assert ovw_path.is_file()

    # Rewrite format version to 1 in the header (offset 4, uint16)
    blob = bytearray(ovw_path.read_bytes())
    struct.pack_into("<H", blob, 4, 1)
    ovw_path.write_bytes(blob)

    reader = RunReader(tmp_path, "v1_fallback")
    result = reader.series("loss", max_points=500)
    assert result["mode"] == "overview"
    assert result["total"] == _OVW_SERIES
    assert any("unreadable overview" in rec.getMessage() for rec in caplog.records)


def test_open_object_helper(tmp_path):
    # 1. Normal object
    sha = put_bytes(tmp_path, b"test object content")
    with open_object(tmp_path, sha) as f:
        assert f.read() == b"test object content"

    # 2. Refuses symlink
    link_sha = "1" * 64
    link_path = tmp_path / "objects" / link_sha[:2] / link_sha
    link_path.parent.mkdir(parents=True, exist_ok=True)
    real_path = object_path(tmp_path, sha)
    link_path.symlink_to(real_path)
    with pytest.raises(FileNotFoundError, match="symlink"):
        open_object(tmp_path, link_sha)

    # 3. Refuses hard links (st_nlink > 1)
    hard_sha = "2" * 64
    hard_path = tmp_path / "objects" / hard_sha[:2] / hard_sha
    hard_path.parent.mkdir(parents=True, exist_ok=True)
    os.link(real_path, hard_path)
    with pytest.raises(FileNotFoundError, match="hard-linked"):
        open_object(tmp_path, hard_sha)

    # 4. Nonexistent object
    missing_sha = "3" * 64
    with pytest.raises(FileNotFoundError):
        open_object(tmp_path, missing_sha)

    # 5. Invalid sha
    with pytest.raises(FileNotFoundError):
        open_object(tmp_path, "bad-sha")


def test_reader_refuses_symlinks(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    writer = RunWriter.create(tmp_path, run_id="target_run", meta={"project": "target"})
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(_OVW_SERIES)])
    writer.finish("finished", {})

    external_secret = tmp_path / "secret.json"
    external_secret.write_text('{"secret": "credentials"}')

    # Test symlinked meta.json in list_runs and RunReader
    run_with_symlink_meta = tmp_path / "runs" / "sym_meta"
    run_with_symlink_meta.mkdir(parents=True, exist_ok=True)
    (run_with_symlink_meta / "meta.json").symlink_to(external_secret)
    runs = list_runs(tmp_path)
    assert not any(r.get("run_id") == "sym_meta" for r in runs)
    assert any("skipping symlinked meta.json" in rec.getMessage() for rec in caplog.records)

    with pytest.raises(FileNotFoundError):
        RunReader(tmp_path, "sym_meta")

    # Test symlinked events.jsonl
    reader = RunReader(tmp_path, "target_run")
    events_path = writer.run_dir / "events.jsonl"
    events_path.unlink()
    events_path.symlink_to(external_secret)
    assert reader.events() == []
    assert any("refusing symlinked events" in rec.getMessage() for rec in caplog.records)

    # Test symlinked media/index.json
    media_dir = writer.run_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    media_index = media_dir / "index.json"
    media_index.unlink(missing_ok=True)
    media_index.symlink_to(external_secret)
    assert reader.media() == {}
    assert any("refusing symlinked media index" in rec.getMessage() for rec in caplog.records)

    # Test symlinked scalars/index.json
    scalars_index = writer.run_dir / "scalars" / "index.json"
    scalars_index_content = scalars_index.read_bytes()
    scalars_index.unlink()
    scalars_index.symlink_to(external_secret)
    reader_fresh = RunReader(tmp_path, "target_run")
    with pytest.raises(ValueError, match="corrupt scalars index"):
        reader_fresh._index()
    assert any("refusing symlinked scalars index" in rec.getMessage() for rec in caplog.records)
    scalars_index.unlink()
    scalars_index.write_bytes(scalars_index_content)

    # Test symlinked scalars/_time.bin
    time_bin = writer.run_dir / "scalars" / "_time.bin"
    time_bin.unlink()
    time_bin.symlink_to(external_secret)
    assert reader.time_index() == []
    assert any("refusing symlinked time file" in rec.getMessage() for rec in caplog.records)

    # Test symlinked scalars/0.ovw
    ovw_file = writer.run_dir / "scalars" / "0.ovw"
    ovw_file.unlink()
    ovw_file.symlink_to(external_secret)
    # Series query ignores symlinked .ovw and logs a warning
    assert reader._authoritative_overview("loss", 0, _OVW_SERIES, _OVW_SERIES * 8) is None
    assert any("symlinked overview" in rec.getMessage() for rec in caplog.records)

    # Test symlinked scalars/0.bin
    bin_file = writer.run_dir / "scalars" / "0.bin"
    bin_file.unlink()
    bin_file.symlink_to(external_secret)
    records, offset = reader.read_records("loss", 0)
    assert records == []


def test_list_runs_directory_name_as_run_id(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agenticml.store.reader")
    run_dir_path = tmp_path / "runs" / "dir_name_id"
    run_dir_path.mkdir(parents=True)
    meta = {
        "format_version": 1,
        "run_id": "meta_name_id",
        "created_at": "2026-10-27T00:00:00.000000Z",
        "status": "finished",
    }
    (run_dir_path / "meta.json").write_text(json.dumps(meta))

    caplog.clear()
    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "dir_name_id"
    assert any("has mismatched run_id 'meta_name_id' in meta.json" in rec.getMessage() for rec in caplog.records)

    # Calling list_runs again must not re-log the warning (warn once)
    caplog.clear()
    runs_again = list_runs(tmp_path)
    assert len(runs_again) == 1
    assert runs_again[0]["run_id"] == "dir_name_id"
    assert not any("has mismatched run_id" in rec.getMessage() for rec in caplog.records)


def test_fsync_outputs_fsyncs_index_files(tmp_path, monkeypatch):
    import agenticml.store.writer as writer_mod
    writer = RunWriter.create(tmp_path, meta={"project": "test"})
    writer.write_scalars([(0, {"loss": 1.0})])
    writer.add_media("img", 0, b"fake png data", "image/png", None)
    fsynced = []
    orig_fsync = writer_mod._fsync_file

    def fake_fsync(path):
        fsynced.append(Path(path).name)
        return orig_fsync(path)

    monkeypatch.setattr(writer_mod, "_fsync_file", fake_fsync)
    writer.finish("finished")
    assert fsynced.count("index.json") >= 2


def test_json_loads_robustness_large_int_and_deep_nesting(tmp_path):
    good = RunWriter.create(tmp_path, run_id="good-run", meta={"created_at": "2026-10-27T00:00:00.000000Z"})
    good.finish("finished")

    # Bad run 1: 5000-digit int in meta.json
    bad1_dir = tmp_path / "runs" / "bad-int-run"
    bad1_dir.mkdir(parents=True)
    big_int_str = "1" + "0" * 5000
    (bad1_dir / "meta.json").write_text(f'{{"format_version": 1, "run_id": "bad-int-run", "val": {big_int_str}}}')

    # Bad run 2: deeply nested array in meta.json
    bad2_dir = tmp_path / "runs" / "bad-nest-run"
    bad2_dir.mkdir(parents=True)
    (bad2_dir / "meta.json").write_text("[" * 200000)

    # Bad run 3: 5000-digit int in an events.jsonl line
    bad3 = RunWriter.create(tmp_path, run_id="bad-event-run", meta={"created_at": "2026-10-27T00:00:01.000000Z"})
    with (bad3.run_dir / "events.jsonl").open("a") as f:
        f.write(f'{{"kind": "step", "val": {big_int_str}}}\n')
    bad3.finish("finished")

    runs = list_runs(tmp_path)
    run_ids = {r["run_id"] for r in runs}
    assert "good-run" in run_ids
    assert "bad-event-run" in run_ids
    assert "bad-int-run" not in run_ids
    assert "bad-nest-run" not in run_ids

    # Reader loads events gracefully skipping the 5000-digit int line
    reader3 = RunReader(tmp_path, "bad-event-run")
    evs = reader3.events()
    assert isinstance(evs, list)

    with pytest.raises(ValueError, match="corrupt meta.json"):
        RunWriter.resume(tmp_path, "bad-int-run", must=True)
    with pytest.raises(ValueError, match="corrupt meta.json"):
        RunWriter.resume(tmp_path, "bad-nest-run", must=True)


def test_read_records_limit_and_offsets(tmp_path):
    writer = RunWriter.create(tmp_path)
    points = [(i, {"loss": float(i)}) for i in range(25)]
    writer.write_scalars(points)

    reader = RunReader(tmp_path, writer.run_id)
    # Read with limit=10
    recs, next_off = reader.read_records("loss", offset=0, limit=10)
    assert len(recs) == 10
    assert next_off == 10 * 8
    assert [r[0] for r in recs] == list(range(10))

    # Next 10
    recs2, next_off2 = reader.read_records("loss", offset=next_off, limit=10)
    assert len(recs2) == 10
    assert next_off2 == 20 * 8
    assert [r[0] for r in recs2] == list(range(10, 20))

    # Remaining 5
    recs3, next_off3 = reader.read_records("loss", offset=next_off2, limit=10)
    assert len(recs3) == 5
    assert next_off3 == 25 * 8
    assert [r[0] for r in recs3] == list(range(20, 25))

    # Beyond end
    recs4, next_off4 = reader.read_records("loss", offset=next_off3, limit=10)
    assert recs4 == []
    assert next_off4 == 25 * 8

    # Limit = 0
    recs0, off0 = reader.read_records("loss", offset=0, limit=0)
    assert recs0 == []
    assert off0 == 0

    with pytest.raises(ValueError):
        reader.read_records("loss", offset=0, limit=-1)
    with pytest.raises(ValueError):
        reader.read_records("loss", offset=0, limit="bad")


def test_index_stat_caching_and_unbounded_series_query(tmp_path, monkeypatch):
    import agenticml.store.reader as reader_mod
    writer = RunWriter.create(tmp_path)
    writer.write_scalars([(i, {"loss": float(i)}) for i in range(2000)])
    writer.finish("finished")

    reader = RunReader(tmp_path, writer.run_id)
    read_counts = {"n": 0}
    real_read = reader_mod.read_file_nofollow

    def counting_read(p):
        if "scalars/index.json" in str(p):
            read_counts["n"] += 1
        return real_read(p)

    monkeypatch.setattr(reader_mod, "read_file_nofollow", counting_read)

    assert "loss" in reader.keys()
    assert read_counts["n"] == 1
    # Second access uses cache
    assert "loss" in reader.keys()
    assert read_counts["n"] == 1

    # Unbounded series query vs query covering the whole series
    res_unbounded = reader.series("loss", max_points=100)
    assert res_unbounded["mode"] == "overview"

    # Query covering whole series: start=0, end=None
    res_start0_none = reader.series("loss", max_points=100, start=0, end=None)
    assert res_start0_none["mode"] == "overview"
    assert res_start0_none["total"] == 2000

    # Query covering whole series: start=0, end=5000 (steps are 0..1999)
    res_covers_all = reader.series("loss", max_points=100, start=0, end=5000)
    assert res_covers_all["mode"] == "overview"
    assert res_covers_all["total"] == 2000


def test_writer_opens_no_follow(tmp_path):
    r1 = RunWriter.create(tmp_path, run_id="run-one")
    r1.write_scalars([(0, {"loss": 1.0})])
    r1_bin = r1.run_dir / "scalars" / "0.bin"
    r1_bin_bytes_before = r1_bin.read_bytes()

    r2 = RunWriter.create(tmp_path, run_id="run-two")
    r2_bin = r2.run_dir / "scalars" / "0.bin"
    # Create symlink from r2's 0.bin pointing to r1's 0.bin
    r2_bin.symlink_to(r1_bin)

    with pytest.raises(OSError):
        r2.write_scalars([(0, {"loss": 2.0})])

    # r1's file must not have been modified
    assert r1_bin.read_bytes() == r1_bin_bytes_before


def test_pruned_run_resume_rejected_files_unmodified(tmp_path):
    writer = RunWriter.create(tmp_path)
    writer.write_scalars([(i, {"loss": float(i)}) for i in range(_OVW_SERIES)])
    writer.finish("finished")

    # Simulate prune
    bin_path = writer.run_dir / "scalars" / "0.bin"
    bin_path.unlink()

    # Capture state of all files in run directory
    before = {p: p.read_bytes() for p in writer.run_dir.rglob("*") if p.is_file()}

    with pytest.raises(ValueError, match="pruned"):
        RunWriter.resume(tmp_path, writer.run_id, must=True)

    after = {p: p.read_bytes() for p in writer.run_dir.rglob("*") if p.is_file()}
    assert before == after


def test_nits_warn_once_artifact_media_prune_race_and_objects(tmp_path, caplog, monkeypatch):
    import agenticml.store.reader as reader_mod
    # 1. _warn_once with different reasons
    caplog.set_level(logging.WARNING, logger="agenticml.store.writer")
    writer = RunWriter.create(tmp_path, run_id="nits-run")
    caplog.clear()
    writer._warn_once("keyA", "warning 1", reason="reason_1")
    writer._warn_once("keyA", "warning 2", reason="reason_2")
    writer._warn_once("keyA", "warning 1 dup", reason="reason_1")
    records = [r for r in caplog.records if "warning" in r.getMessage()]
    assert len(records) == 2

    # 2. add_artifact failure does not consume version
    art_file = tmp_path / "model.pt"
    art_file.write_bytes(b"model data")
    real_append = writer._append_event

    def failing_append(event):
        raise OSError("disk full simulated")

    monkeypatch.setattr(writer, "_append_event", failing_append)
    with pytest.raises(OSError):
        writer.add_artifact(art_file, "my_model", "model", {})
    assert writer._artifact_versions.get("my_model", 0) == 0

    monkeypatch.setattr(writer, "_append_event", real_append)
    art_rec = writer.add_artifact(art_file, "my_model", "model", {})
    assert art_rec["version"] == 1
    assert writer._artifact_versions["my_model"] == 1

    # 3. add_media rollback on failure
    def failing_persist():
        raise OSError("persist media failure")

    monkeypatch.setattr(writer, "_persist_media", failing_persist)
    with pytest.raises(OSError):
        writer.add_media("test_media", 0, b"data", "image/png", None)
    assert "test_media" not in writer._media or len(writer._media["test_media"]) == 0

    # 4. objects symlink replaced
    from agenticml.store.objects import has, put_bytes, put_file
    sha_data = b"content for object store"
    h = hashlib.sha256(sha_data).hexdigest()
    obj_dest = object_path(tmp_path, h)
    obj_dest.parent.mkdir(parents=True, exist_ok=True)
    external_dummy = tmp_path / "dummy.txt"
    external_dummy.write_bytes(b"dummy")
    obj_dest.symlink_to(external_dummy)

    assert not has(tmp_path, h)
    put_bytes(tmp_path, sha_data)
    assert has(tmp_path, h)
    assert not obj_dest.is_symlink()
    assert obj_dest.read_bytes() == sha_data

    # 5. prune race during series()
    writer2 = RunWriter.create(tmp_path, run_id="race-run")
    writer2.write_scalars([(i, {"loss": float(i)}) for i in range(_OVW_SERIES)])
    writer2.finish("finished")
    reader2 = RunReader(tmp_path, "race-run")

    real_open = reader_mod.open_nofollow

    def race_open(p, *args, **kwargs):
        if str(p).endswith("0.bin"):
            raise FileNotFoundError(f"simulated prune race: {p}")
        return real_open(p, *args, **kwargs)

    monkeypatch.setattr(reader_mod, "open_nofollow", race_open)
    res = reader2.series("loss", max_points=100)
    assert res["mode"] == "overview"
    assert res["total"] == _OVW_SERIES


def test_write_scalars_all_or_nothing_on_append_failure(tmp_path, monkeypatch):
    writer = RunWriter.create(tmp_path, run_id="atomic-run")
    # First write an initial batch
    writer.write_scalars([(0, {"s1": 10.0, "s2": 20.0})])
    assert writer.last_step == 0
    s1_bin = writer.run_dir / "scalars" / "0.bin"
    s2_bin = writer.run_dir / "scalars" / "1.bin"
    time_bin = writer.run_dir / "scalars" / "_time.bin"
    assert s1_bin.stat().st_size == 8
    assert s2_bin.stat().st_size == 8
    assert time_bin.stat().st_size == 8

    # Attempt a batch where appending the 2nd series fails with ENOSPC
    batch = [(1, {"s1": 11.0, "s2": 21.0}), (2, {"s1": 12.0, "s2": 22.0})]

    real_append = writer._append_series_bytes

    def fail_second_series(series_id, data):
        if series_id == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_append(series_id, data)

    monkeypatch.setattr(writer, "_append_series_bytes", fail_second_series)

    with pytest.raises(OSError) as excinfo:
        writer.write_scalars(batch)
    assert excinfo.value.errno == errno.ENOSPC

    # Verify no new bytes on disk for the 1st series (and 2nd, and _time.bin)
    assert s1_bin.stat().st_size == 8
    assert s2_bin.stat().st_size == 8
    assert time_bin.stat().st_size == 8
    # In-memory last_step was restored
    assert writer.last_step == 0

    # Retry the exact same batch without the monkeypatch
    monkeypatch.setattr(writer, "_append_series_bytes", real_append)
    writer.write_scalars(batch)

    # Now exactly one copy of points exists
    assert s1_bin.stat().st_size == 24  # 3 records: step 0, 1, 2
    assert s2_bin.stat().st_size == 24  # 3 records: step 0, 1, 2
    assert time_bin.stat().st_size == 16  # 2 flush records
    assert writer.last_step == 2

    reader = RunReader(tmp_path, "atomic-run")
    s1_data = reader.series("s1", max_points=100)
    s2_data = reader.series("s2", max_points=100)
    assert s1_data["total"] == 3
    assert s2_data["total"] == 3


def test_write_scalars_all_or_nothing_initial_batch(tmp_path, monkeypatch):
    writer = RunWriter.create(tmp_path, run_id="initial-batch-run")
    batch = [(0, {"k1": 1.0, "k2": 2.0})]

    real_append = writer._append_series_bytes

    def fail_second_series(series_id, data):
        if series_id == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_append(series_id, data)

    monkeypatch.setattr(writer, "_append_series_bytes", fail_second_series)

    with pytest.raises(OSError) as excinfo:
        writer.write_scalars(batch)
    assert excinfo.value.errno == errno.ENOSPC

    k1_bin = writer.run_dir / "scalars" / "0.bin"
    # No new bytes on disk for the 1st series (file truncated back to recorded size 0)
    assert not k1_bin.exists() or k1_bin.stat().st_size == 0
    assert writer.last_step is None

    # Retry the same batch
    monkeypatch.setattr(writer, "_append_series_bytes", real_append)
    writer.write_scalars(batch)

    assert k1_bin.stat().st_size == 8
    assert writer.last_step == 0
    reader = RunReader(tmp_path, "initial-batch-run")
    assert reader.series("k1", max_points=10)["total"] == 1
    assert reader.series("k2", max_points=10)["total"] == 1


def test_write_scalars_all_or_nothing_time_failure(tmp_path, monkeypatch):
    writer = RunWriter.create(tmp_path, run_id="time-fail-run")
    writer.write_scalars([(0, {"k1": 1.0})])
    k1_bin = writer.run_dir / "scalars" / "0.bin"
    assert k1_bin.stat().st_size == 8

    def fail_time(payload):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(writer, "_append_time", fail_time)
    with pytest.raises(OSError):
        writer.write_scalars([(1, {"k1": 2.0})])

    assert k1_bin.stat().st_size == 8
    assert writer.last_step == 0


def test_write_scalars_monotonic_flag_restoration_on_failure(tmp_path, monkeypatch):
    writer = RunWriter.create(tmp_path, run_id="monotonic-rollback")
    writer.write_scalars([(10, {"s": 1.0, "other": 2.0})])
    assert writer._series["s"].monotonic is True

    real_append = writer._append_series_bytes

    def fail_second_series(series_id, data):
        if series_id == 1:
            raise OSError(errno.ENOSPC, "Disk full")
        return real_append(series_id, data)

    monkeypatch.setattr(writer, "_append_series_bytes", fail_second_series)
    with pytest.raises(OSError):
        writer.write_scalars([(5, {"s": 0.5, "other": 1.5})])

    assert writer._series["s"].monotonic is True
    assert writer._series["s"].last_step == 10
    assert writer.last_step == 10


def test_run_finished_error_after_finish(tmp_path):
    assert issubclass(RunFinishedError, RuntimeError)
    writer = RunWriter.create(tmp_path, run_id="fin-run")
    writer.write_scalars([(0, {"loss": 1.0})])
    writer.finish("finished", {"loss": 1.0})

    with pytest.raises(RunFinishedError) as exc_info:
        writer.write_scalars([(1, {"loss": 0.5})])
    assert f"run {writer.run_id} is finished" in str(exc_info.value)

    with pytest.raises(RunFinishedError):
        writer.set_config({"lr": 0.01})

    with pytest.raises(RunFinishedError):
        writer.set_summary({"done": True})

    with pytest.raises(RunFinishedError):
        writer.set_meta_fields(notes="finished note")

    with pytest.raises(RunFinishedError):
        writer.add_code([], [])

    with pytest.raises(RunFinishedError):
        writer.add_media("img", 1, b"png", "image/png", None)

    dummy = tmp_path / "art.txt"
    dummy.write_text("art")
    with pytest.raises(RunFinishedError):
        writer.add_artifact(dummy, "model", "model", {})


def test_media_cap_and_thinning(tmp_path, monkeypatch):
    writer = RunWriter.create(tmp_path, run_id="media-cap")
    shas = []
    # Add 200 images to one key
    for i in range(200):
        sha = writer.add_media("pred", i, f"img-{i}".encode("utf-8"), "application/octet-stream", f"cap-{i}")
        shas.append(sha)

    reader = RunReader(tmp_path, writer.run_id)
    rows = reader.media("pred")
    # Must be at most MEDIA_MAX_PER_KEY = 64
    assert len(rows) <= 64
    # First and last entries kept
    assert rows[0]["step"] == 0
    assert rows[-1]["step"] == 199
    # Steps are strictly increasing and spread across the run
    steps = [r["step"] for r in rows]
    assert steps == sorted(steps)
    assert len(steps) == len(set(steps))

    # Dropped entries were removed from the index, but their objects are still in objects/
    dropped_sha = shas[1]  # step 1 is dropped on thinning
    assert not any(r["sha256"] == dropped_sha for r in rows)
    from agenticml.store import has
    assert has(tmp_path, dropped_sha)

    # Test exact cap boundaries with env override
    monkeypatch.setenv("AGENTICML_MEDIA_MAX_PER_KEY", "10")
    writer2 = RunWriter.create(tmp_path, run_id="media-cap-env")
    for i in range(10):
        writer2.add_media("frame", i, f"f-{i}".encode(), "application/octet-stream", None)
    assert len(writer2._media["frame"]) == 10

    # 11th arrives -> thinned to 6 (even indices 0, 2, 4, 6, 8, 10)
    writer2.add_media("frame", 10, b"f-10", "application/octet-stream", None)
    thinned_rows = writer2._media["frame"]
    assert len(thinned_rows) == 6
    assert thinned_rows[0]["step"] == 0
    assert thinned_rows[-1]["step"] == 10


def test_media_downscale_pillow(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    import io
    from PIL import Image

    writer = RunWriter.create(tmp_path, run_id="downscale-run")

    # Create a 2000x1000 test image
    img = Image.new("RGB", (2000, 1000), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    original_data = buf.getvalue()

    sha = writer.add_media("preview", 1, original_data, "image/png", "large")
    rows = writer._media["preview"]
    assert len(rows) == 1
    stored_entry = rows[0]
    assert stored_entry["content_type"] == "image/png"

    # Read back the stored object
    from agenticml.store import open_object
    with open_object(tmp_path, stored_entry["sha256"]) as handle:
        resized_img = Image.open(handle)
        w, h = resized_img.size
        assert max(w, h) == 1024
        assert w == 1024
        assert h == 512

    # Small image (500x300) should not be downscaled
    small_img = Image.new("RGB", (500, 300), color="red")
    buf_small = io.BytesIO()
    small_img.save(buf_small, format="PNG")
    small_data = buf_small.getvalue()
    small_sha = writer.add_media("preview", 2, small_data, "image/png", "small")
    with open_object(tmp_path, small_sha) as handle:
        read_small = Image.open(handle)
        assert read_small.size == (500, 300)

    # Test AGENTICML_MEDIA_MAX_PX override
    monkeypatch.setenv("AGENTICML_MEDIA_MAX_PX", "500")
    sha_custom = writer.add_media("custom", 1, original_data, "image/png", None)
    with open_object(tmp_path, sha_custom) as handle:
        read_custom = Image.open(handle)
        assert max(read_custom.size) == 500


def test_media_without_pillow(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no PIL")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    writer = RunWriter.create(tmp_path, run_id="no-pil-run")
    fake_png = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    sha = writer.add_media("frame", 1, fake_png, "image/png", None)
    from agenticml.store import open_object
    with open_object(tmp_path, sha) as handle:
        assert handle.read() == fake_png



@pytest.mark.parametrize("method", ["bytes", "file"])
def test_dedup_refreshes_object_mtime(tmp_path, method):
    data = b"old-object-used-again"
    sha = put_bytes(tmp_path, data)
    dest = object_path(tmp_path, sha)
    os.utime(dest, (1, 1))
    if method == "bytes":
        assert put_bytes(tmp_path, data) == sha
    else:
        src = tmp_path / "source.bin"
        src.write_bytes(data)
        assert put_file(tmp_path, src) == (sha, len(data))
    assert dest.stat().st_mtime >= time.time() - 10


def test_dedup_refreshes_object_published_during_put(tmp_path, monkeypatch):
    import agenticml.store.objects as objects_mod
    data = b"another-writer-published-first"
    sha = hashlib.sha256(data).hexdigest()
    dest = object_path(tmp_path, sha)
    real_check = objects_mod._is_regular_file
    calls = []

    def publish(path):
        calls.append(path)
        if len(calls) == 2:
            dest.write_bytes(data)
            os.utime(dest, (1, 1))
        return real_check(path)

    monkeypatch.setattr(objects_mod, "_is_regular_file", publish)
    assert put_bytes(tmp_path, data) == sha
    assert dest.stat().st_mtime >= time.time() - 10


@pytest.mark.parametrize("phase", ["bytes-initial", "bytes-publish", "file-publish"])
def test_put_republishes_when_gc_renames_before_dedup_utime(tmp_path, monkeypatch, phase):
    import agenticml.store.objects as objects_mod
    data = b"moved-before-dedup-utime"
    sha = put_bytes(tmp_path, data)
    dest = object_path(tmp_path, sha)
    trash = dest.with_name(f".trash-{sha}-123")
    real_utime = objects_mod.os.utime
    real_check = objects_mod._is_regular_file
    moved = []
    checks = []

    def check(path):
        checks.append(path)
        if phase == "bytes-publish" and len(checks) == 1:
            return False
        return real_check(path)

    def move_then_utime(path, *args, **kwargs):
        if Path(path) == dest and not moved:
            os.rename(dest, trash)
            moved.append(dest)
        return real_utime(path, *args, **kwargs)

    monkeypatch.setattr(objects_mod, "_is_regular_file", check)
    monkeypatch.setattr(objects_mod.os, "utime", move_then_utime)
    if phase == "file-publish":
        source = tmp_path / "source"
        source.write_bytes(data)
        assert put_file(tmp_path, source) == (sha, len(data))
    else:
        assert put_bytes(tmp_path, data) == sha
    assert moved == [dest]
    assert dest.read_bytes() == trash.read_bytes() == data


def test_event_append_rolls_back_enospc_partial_bytes(tmp_path, monkeypatch):
    import agenticml.store.writer as writer_mod
    writer = RunWriter.create(tmp_path)
    writer.set_config({"first": True})
    path = writer.run_dir / "events.jsonl"
    before = path.read_bytes()
    real_write = writer_mod._write_all

    def torn_write(handle, data):
        handle.write(data[:7])
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(writer_mod, "_write_all", torn_write)
    with pytest.raises(OSError) as exc:
        writer._append_event({"kind": "failed"})
    assert exc.value.errno == errno.ENOSPC
    assert path.read_bytes() == before
    monkeypatch.setattr(writer_mod, "_write_all", real_write)
    writer._append_event({"kind": "next"})
    assert [json.loads(line)["kind"] for line in path.read_bytes().split(b"\n") if line] == ["config_update", "next"]
    writer.close()


def test_event_append_heals_after_rollback_truncate_fails(tmp_path, monkeypatch):
    import agenticml.store.writer as writer_mod
    writer = RunWriter.create(tmp_path)
    writer.set_config({"first": True})
    path = writer.run_dir / "events.jsonl"
    before = path.read_bytes()

    def torn_write(handle, data):
        handle.write(data[:7])
        raise OSError(errno.ENOSPC, "disk full")

    def fail_truncate(fd, size):
        raise OSError(errno.EIO, "rollback failed")

    with monkeypatch.context() as patch:
        patch.setattr(writer_mod, "_write_all", torn_write)
        patch.setattr(writer_mod.os, "ftruncate", fail_truncate)
        with pytest.raises(OSError, match="rollback failed"):
            writer._append_event({"kind": "failed"})
    assert path.read_bytes() == before + b'{"kind"'
    assert writer._events_healed is False
    writer._append_event({"kind": "next"})
    assert writer._events_healed is True
    assert [json.loads(line)["kind"] for line in path.read_bytes().splitlines()] == ["config_update", "next"]
    writer.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
@pytest.mark.parametrize("teardown", ["close", "gc", "exit"])
def test_forked_writer_teardown_keeps_parent_locked(tmp_path, teardown):
    import subprocess
    import sys
    from agenticml.store.layout import try_lock_run, unlock_run
    code = """
import gc, os, sys
from agenticml.store import RunWriter
writer = RunWriter.create(sys.argv[1], run_id="fork-owner")
writer.write_scalars([(0, {"loss": 1})])
pid = os.fork()
if pid == 0:
    assert writer._run_lock.closed
    if sys.argv[2] == "close":
        writer.close()
    elif sys.argv[2] == "gc":
        del writer
        gc.collect()
    sys.exit(0)
_, status = os.waitpid(pid, 0)
assert status == 0, status
writer.write_scalars([(1, {"loss": 2})])
print("ready", flush=True)
sys.stdin.readline()
writer.close()
"""
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), teardown],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([proc.stdout], [], [], 10)[0]
        assert proc.stdout.readline().strip() == "ready"
        # This process opens a third descriptor, independent of owner and child.
        handle = try_lock_run(tmp_path / "runs/fork-owner")
        try:
            assert handle is None
        finally:
            unlock_run(handle)
        _, stderr = proc.communicate("exit\n", timeout=10)
        assert proc.returncode == 0, stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_surviving_fork_child_does_not_keep_dead_parent_locked(tmp_path):
    import select
    import subprocess
    import sys
    from agenticml.store.layout import try_lock_run, unlock_run
    code = """
import os, sys
from agenticml.store import RunWriter
writer = RunWriter.create(sys.argv[1], run_id="dead-owner")
pid = os.fork()
if pid == 0:
    print("ready", flush=True)
    for line in sys.stdin:
        if line.strip() != "ping":
            break
        print("alive", flush=True)
    os._exit(0)
os.waitpid(pid, 0)
"""
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 10)[0]
        assert proc.stdout.readline().strip() == "ready"
        assert try_lock_run(tmp_path / "runs/dead-owner") is None
        proc.kill()
        proc.wait(timeout=10)
        proc.stdin.write("ping\n")
        proc.stdin.flush()
        assert select.select([proc.stdout], [], [], 10)[0]
        assert proc.stdout.readline().strip() == "alive"
        handle = try_lock_run(tmp_path / "runs/dead-owner")
        try:
            assert handle is not None
        finally:
            unlock_run(handle)
    finally:
        # Closing stdin lets the surviving child exit even if an assertion fails.
        proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows has no-op run locks")
def test_unlock_run_only_unlocks_in_owner_process(tmp_path, monkeypatch):
    from agenticml.store.layout import try_lock_run, unlock_run
    tmp_path.joinpath("run").mkdir()
    handle = try_lock_run(tmp_path / "run")
    duplicate = os.dup(handle.fileno())
    owner_pid = os.getpid()
    try:
        assert handle.owner_pid == owner_pid
        with monkeypatch.context() as patch:
            patch.setattr(os, "getpid", lambda: owner_pid + 1)
            unlock_run(handle)
        assert handle.closed
        assert try_lock_run(tmp_path / "run") is None
    finally:
        os.close(duplicate)
    acquired = try_lock_run(tmp_path / "run")
    assert acquired is not None
    unlock_run(acquired)


@pytest.mark.skipif(os.name == "nt", reason="Windows has no-op run locks")
def test_writer_lifetime_lock_and_failed_resume_cleanup(tmp_path, monkeypatch):
    import agenticml.store.writer as writer_mod
    from agenticml.store.layout import try_lock_run, unlock_run
    writer = RunWriter.create(tmp_path, run_id="locked")
    assert try_lock_run(writer.run_dir) is None
    with pytest.raises(ValueError, match="^run locked is open in another process$"):
        RunWriter.resume(tmp_path, writer.run_id, must=True)
    writer.close()
    handle = try_lock_run(writer.run_dir)
    assert handle is not None
    unlock_run(handle)
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    assert try_lock_run(writer.run_dir) is None
    resumed.finish("finished")
    handle = try_lock_run(writer.run_dir)
    assert handle is not None
    unlock_run(handle)

    def fail_load(self):
        raise ValueError("load failed")

    monkeypatch.setattr(writer_mod.RunWriter, "_load_existing", fail_load)
    with pytest.raises(ValueError, match="load failed"):
        RunWriter.resume(tmp_path, writer.run_id, must=True)
    handle = try_lock_run(writer.run_dir)
    assert handle is not None
    unlock_run(handle)


@pytest.mark.skipif(os.name == "nt", reason="Windows has no-op run locks")
def test_writer_lock_released_on_process_exit(tmp_path):
    import subprocess
    import sys
    from agenticml.store.layout import try_lock_run, unlock_run
    code = """
import os, sys
from agenticml.store import RunWriter
writer = RunWriter.create(sys.argv[1], run_id="child")
print("ready", flush=True)
sys.stdin.readline()
os._exit(0)
"""
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        directory = tmp_path / "runs/child"
        assert try_lock_run(directory) is None
        with pytest.raises(ValueError, match="open in another process"):
            RunWriter.resume(tmp_path, "child", must=True)
        proc.communicate("exit\n", timeout=10)
        assert proc.returncode == 0
        handle = try_lock_run(directory)
        assert handle is not None
        unlock_run(handle)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_create_failure_releases_lock(tmp_path, monkeypatch):
    import agenticml.store.writer as writer_mod
    from agenticml.store.layout import try_lock_run, unlock_run

    def fail_persist(self):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(writer_mod.RunWriter, "_persist_index", fail_persist)
    with pytest.raises(OSError):
        RunWriter.create(tmp_path, run_id="failed-create")
    handle = try_lock_run(tmp_path / "runs/failed-create")
    assert handle is not None
    unlock_run(handle)


def test_media_ids_unique_stable_through_thinning_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICML_MEDIA_MAX_PER_KEY", "4")
    writer = RunWriter.create(tmp_path)
    seen = {}
    for i in range(20):
        key = "a" if i % 3 else "b"
        writer.add_media(key, i, str(i).encode(), "application/octet-stream", None)
        rows = RunReader(tmp_path, writer.run_id).media()
        newest = rows[key][-1]
        assert newest["id"] == i
        seen[i] = dict(newest)
        for entries in rows.values():
            for row in entries:
                assert row == seen[row["id"]]
    before = RunReader(tmp_path, writer.run_id).media()
    writer.close()
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    assert RunReader(tmp_path, writer.run_id).media() == before
    resumed.add_media("c", 20, b"next", "application/octet-stream", None)
    assert RunReader(tmp_path, writer.run_id).media("c")[0]["id"] == 20


def test_legacy_media_without_ids_stays_readable(tmp_path):
    writer = RunWriter.create(tmp_path)
    writer.add_media("old", 0, b"old", "application/octet-stream", None)
    writer.close()
    path = writer.run_dir / "media/index.json"
    data = json.loads(path.read_text())
    del data["old"][0]["id"]
    path.write_text(json.dumps(data))
    assert "id" not in RunReader(tmp_path, writer.run_id).media("old")[0]
    resumed = RunWriter.resume(tmp_path, writer.run_id, must=True)
    resumed.add_media("new", 1, b"new", "application/octet-stream", None)
    assert RunReader(tmp_path, writer.run_id).media("new")[0]["id"] == 0


def test_failed_media_persist_does_not_consume_id(tmp_path, monkeypatch):
    writer = RunWriter.create(tmp_path)
    real_persist = writer._persist_media

    def fail():
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(writer, "_persist_media", fail)
    with pytest.raises(OSError):
        writer.add_media("a", 0, b"lost", "application/octet-stream", None)
    monkeypatch.setattr(writer, "_persist_media", real_persist)
    writer.add_media("a", 1, b"kept", "application/octet-stream", None)
    assert RunReader(tmp_path, writer.run_id).media("a")[0]["id"] == 0


def test_shared_reference_extraction_includes_superseded_code(tmp_path):
    from agenticml.store import run_usage
    writer = RunWriter.create(tmp_path)
    writer.add_code([], [("a.py", b"old"), ("b.py", b"other")])
    writer.add_code([], [("a.py", b"new")])
    media_sha = writer.add_media("img", 0, b"image", "application/octet-stream", None)
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"artifact")
    copied = writer.add_artifact(artifact, "copy", "model", {}, copy=True)["sha256"]
    writer.add_artifact(artifact, "ref", "model", {}, copy=False)
    reader = RunReader(tmp_path, writer.run_id)
    expected = {hashlib.sha256(data).hexdigest() for data in (b"old", b"other", b"new")} | {media_sha, copied}
    assert reader.referenced_shas() == expected == set(run_usage(tmp_path, writer.run_id)["all_shas"])
    (writer.run_dir / "events.jsonl").write_bytes(b"{torn")
    with pytest.raises(ValueError):
        reader.referenced_shas()
    with pytest.raises(ValueError):
        run_usage(tmp_path, writer.run_id)


def test_meta_update_serializes_once_and_skips_unchanged_write(tmp_path, monkeypatch):
    import agenticml.store.writer as writer_module
    writer = RunWriter.create(tmp_path)
    original = writer_module._meta_bytes
    calls = []

    def serialize(meta):
        calls.append(meta)
        return original(meta)

    monkeypatch.setattr(writer_module, '_meta_bytes', serialize)
    writer.set_summary({'best': 0.2})
    assert len(calls) == 1
    path = writer.run_dir / 'meta.json'
    before = path.stat().st_mtime_ns
    writer.set_summary({'best': 0.2})
    assert len(calls) == 2
    assert path.stat().st_mtime_ns == before
    writer.close()

"""W&B cloud import (package 41.6) against a fake ``wandb`` module; no network, no account."""

import os
import signal
import stat
import struct
import sys
import types
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenticml import cli
from agenticml.importers import ImportOptions, ImportSourceError, run_import
from agenticml.importers import wandb as local_wandb
from agenticml.importers import wandb_cloud
from agenticml.store import RunReader, list_runs

CREATED = "2024-03-05T12:30:15"
CREATED_UNIX = datetime(2024, 3, 5, 12, 30, 15, tzinfo=timezone.utc).timestamp()


def png_bytes() -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")


class FakeFile:
    def __init__(self, run, name, size=None):
        self.run = run
        self.name = name
        self.size = size

    def download(self, root=".", replace=False, **kwargs):
        self.run.downloads.append((self.name, root))
        content = self.run.files.get(self.name)
        if content is None:
            raise RuntimeError("404 file not found")
        if isinstance(content, Exception):
            raise content
        # Like the real client: write to root/<name>, return an open handle.
        target = Path(root) / self.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if callable(content):
            content(target)
            # A directory or FIFO at the target cannot be returned as an open handle (a FIFO would block).
            if target.is_dir() or stat.S_ISFIFO(target.lstat().st_mode):
                return None
        else:
            target.write_bytes(content)
        return open(target, "rb")


class FakeRun:
    def __init__(self, run_id="abc123", **fields):
        self.id = run_id
        self.name = f"name-{run_id}"
        self.entity = "lab"
        self.project = "vision"
        self.state = "finished"
        self.created_at = CREATED
        self.heartbeat_at = "2024-03-05T12:40:15"
        self.tags = ["baseline"]
        self.notes = "from the cloud"
        self.config = {"lr": 0.1, "_wandb": {"version": "0.17"}}
        self.summary = {"loss": 0.25, "_runtime": 120.5, "_timestamp": CREATED_UNIX + 120.5, "_step": 2}
        self.history = [
            {"_step": 0, "_timestamp": CREATED_UNIX + 1.0, "loss": 1.0, "acc": 0.1},
            {"_step": 1, "_timestamp": CREATED_UNIX + 2.5, "loss": 0.5},
            {"_step": 2, "_timestamp": CREATED_UNIX + 4.0, "loss": 0.25, "acc": 0.9},
        ]
        self.files: dict = {}
        self.downloads: list = []
        self.scan_calls = 0
        self.file_sizes: dict = {}
        for key, value in fields.items():
            setattr(self, key, value)

    def scan_history(self, *args, **kwargs):
        self.scan_calls += 1
        for row in self.history:
            if isinstance(row, Exception):
                raise row
            yield row

    def file(self, name):
        return FakeFile(self, name, self.file_sizes.get(name))


class FakeApi:
    runs_by_path: dict = {}
    instances: list = []
    init_error: Exception | None = None

    def __init__(self, *args, **kwargs):
        if FakeApi.init_error is not None:
            raise FakeApi.init_error
        self.args = args
        self.kwargs = kwargs
        self.settings = {}
        FakeApi.instances.append(self)

    def runs(self, path):
        found = FakeApi.runs_by_path.get(path)
        if isinstance(found, Exception):
            raise found
        if found is None:
            raise RuntimeError(f"project {path} not found")
        return iter(found)

    def run(self, path):
        found = FakeApi.runs_by_path.get(path)
        if isinstance(found, Exception):
            raise found
        if found is None:
            raise RuntimeError(f"run {path} not found")
        return found


@pytest.fixture
def fake_wandb(monkeypatch):
    module = types.ModuleType("wandb")
    module.Api = FakeApi
    FakeApi.runs_by_path = {}
    FakeApi.instances = []
    FakeApi.init_error = None
    monkeypatch.setitem(sys.modules, "wandb", module)
    monkeypatch.delenv("WANDB_BASE_URL", raising=False)
    monkeypatch.delenv("WANDB_API_KEY", raising=False)
    return FakeApi


def do_import(tmp_path, path="lab/vision", **kwargs):
    options = kwargs.pop("options", None) or ImportOptions()
    return run_import(tmp_path / "store", "wandb", path, options, **kwargs), options


# ---------------------------------------------------------------------------
# dispatch and paths


def test_project_import_writes_runs_with_original_data(tmp_path, fake_wandb):
    good = FakeRun("good1")
    bad = FakeRun("bad2", state="failed", name="broken", tags=["x"])
    fake_wandb.runs_by_path["lab/vision"] = [good, bad]
    report, _ = do_import(tmp_path)
    assert (report.imported, report.skipped, report.failed) == (2, 0, 0)

    store = tmp_path / "store"
    by_name = {row["name"]: row for row in list_runs(store)}
    assert set(by_name) == {"name-good1", "broken"}
    row = next(r for r in report.rows if r.name == "name-good1")
    reader = RunReader(store, row.run_id)
    meta = reader.meta()
    assert row.run_id.startswith("20240305T123015-")
    assert meta["status"] == "finished"
    assert meta["project"] == "vision" and meta["name"] == "name-good1"
    assert meta["created_at"] == "2024-03-05T12:30:15.000000Z"
    assert meta["finished_at"] == "2024-03-05T12:32:15.500000Z"
    assert meta["config"] == {"lr": 0.1}  # W&B's own _wandb key is dropped
    assert meta["summary"] == {"loss": 0.25}  # keys starting with _ are not summary values
    assert meta["notes"] == "from the cloud"
    assert meta["hostname"] is None  # the importing machine's name is never recorded
    assert meta["tags"] == ["baseline", "imported:wandb"]
    assert meta["imported_from"]["source"] == "wandb"
    assert meta["imported_from"]["source_run_id"] == "good1"
    assert meta["imported_from"]["location"] == "wandb://wandb.ai/lab/vision"
    assert meta["imported_from"]["source_status"] == "finished"
    assert sorted(reader.keys()) == ["acc", "loss"]
    assert reader.series("loss")["points"] == [[0, 1.0], [1, 0.5], [2, 0.25]]
    assert [list(p) for p in reader.time_index()] == [[0, 1.0], [1, 2.5], [2, 4.0]]
    assert not any(e["kind"] == "code" for e in reader.events())

    other = next(r for r in report.rows if r.name == "broken")
    assert RunReader(store, other.run_id).meta()["status"] == "crashed"
    assert RunReader(store, other.run_id).meta()["imported_from"]["source_status"] == "failed"


def test_cloud_run_host_attribute_is_stored(tmp_path, fake_wandb):
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun("h1", host="gpu-node-7")]
    report, _ = do_import(tmp_path)
    (row,) = report.rows
    assert RunReader(tmp_path / "store", row.run_id).meta()["hostname"] == "gpu-node-7"


def test_credentials_are_left_to_wandb_api(tmp_path, fake_wandb, monkeypatch):
    monkeypatch.setenv("WANDB_API_KEY", "super-secret-key-1234")
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun()]
    report, options = do_import(tmp_path)
    assert report.imported == 1
    (api,) = fake_wandb.instances
    assert api.args == () and api.kwargs == {}  # no key, no settings are passed
    meta = RunReader(tmp_path / "store", report.rows[0].run_id).meta()
    assert "super-secret-key-1234" not in str(meta)
    assert "super-secret-key-1234" not in " ".join(options.warnings)


def test_single_run_path_and_same_id_as_project_import(tmp_path, fake_wandb):
    run = FakeRun("one")
    fake_wandb.runs_by_path["lab/vision/one"] = run
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path, "lab/vision/one")
    assert report.imported == 1
    again, _ = do_import(tmp_path, "lab/vision")  # same run through the project path: deduplicated
    assert (again.imported, again.skipped) == (0, 1)
    assert again.rows[0].run_id == report.rows[0].run_id


def test_reimport_is_skipped_and_replace_rewrites(tmp_path, fake_wandb):
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun()]
    first, _ = do_import(tmp_path)
    second, _ = do_import(tmp_path)
    assert first.imported == 1 and second.skipped == 1
    third, _ = do_import(tmp_path, replace=True)
    assert third.imported == 1 and third.rows[0].run_id == first.rows[0].run_id


def test_run_filter_and_project_override(tmp_path, fake_wandb):
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun("a1"), FakeRun("b2")]
    options = ImportOptions(project="mine", run_globs=["name-b*"], tags=["extra"])
    report, _ = do_import(tmp_path, options=options)
    assert report.imported == 1
    meta = RunReader(tmp_path / "store", report.rows[0].run_id).meta()
    assert meta["project"] == "mine" and meta["tags"] == ["baseline", "extra", "imported:wandb"]


def test_other_server_gets_other_run_ids(tmp_path, fake_wandb, monkeypatch):
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun()]
    first, _ = do_import(tmp_path)
    monkeypatch.setenv("WANDB_BASE_URL", "https://wandb.example.org:8443/")
    second, _ = do_import(tmp_path)
    assert second.imported == 1 and second.rows[0].run_id != first.rows[0].run_id
    meta = RunReader(tmp_path / "store", second.rows[0].run_id).meta()
    assert meta["imported_from"]["location"] == "wandb://wandb.example.org/lab/vision"


def test_local_dispatch_sends_only_missing_cloud_looking_paths_to_the_cloud(tmp_path, monkeypatch, fake_wandb):
    assert local_wandb._is_cloud_path("lab/vision") and local_wandb._is_cloud_path("lab/vision/run1")
    for not_cloud in ("./lab/vision", "../lab/vision", "lab", "a/b/c/d", "/abs/path", "lab/vi sion"):
        assert not local_wandb._is_cloud_path(not_cloud)
    # A path that exists on disk is a local directory, even when it looks like ENTITY/PROJECT.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "lab" / "vision").mkdir(parents=True)
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun()]
    with pytest.raises(ImportSourceError, match="no run-"):
        local_wandb.discover("lab/vision", ImportOptions())
    assert fake_wandb.instances == []
    # A missing path goes to the cloud.
    fake_wandb.runs_by_path["ent/proj"] = [FakeRun("z9")]
    found = list(local_wandb.discover("ent/proj", ImportOptions()))
    assert [r.source_run_id for r in found] == ["z9"]


@pytest.mark.parametrize(
    "path",
    ["lab", "lab/vision/run/extra", "lab/./x", "lab/vision/..", "../vision", "lab/vi sion", "", "lab//x", "/lab/x"],
)
def test_parse_path_rejects_malformed_paths(path):
    with pytest.raises(ImportSourceError):
        wandb_cloud.parse_path(path)


def test_parse_path_accepts_entity_project_run():
    assert wandb_cloud.parse_path("my-team/proj.v2") == ("my-team", "proj.v2", None)
    assert wandb_cloud.parse_path("a/b/c_1") == ("a", "b", "c_1")


# ---------------------------------------------------------------------------
# no package, no credentials, no access


def test_without_the_extra_the_install_hint_is_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "wandb", None)  # `import wandb` raises ImportError
    code = cli.main(["import", "wandb", "lab/vision", "--dir", str(tmp_path / "s")])
    assert code == 2
    err = capsys.readouterr().err
    assert "pip install 'agenticml-lite[wandb]'" in err
    assert not (tmp_path / "s" / "runs").exists() or not list((tmp_path / "s" / "runs").iterdir())


def test_a_local_wandb_directory_named_wandb_is_not_the_package(tmp_path, monkeypatch):
    # With the real package missing, a ./wandb run directory can be imported as a namespace package.
    monkeypatch.setitem(sys.modules, "wandb", types.ModuleType("wandb"))
    with pytest.raises(ImportSourceError, match=r"agenticml-lite\[wandb\]"):
        do_import(tmp_path)


def test_missing_credentials_are_one_clear_error(tmp_path, fake_wandb, monkeypatch):
    monkeypatch.setenv("WANDB_API_KEY", "super-secret-key-1234")
    fake_wandb.init_error = RuntimeError("api_key not configured (no-tty). call wandb.login(key=...)")
    with pytest.raises(ImportSourceError) as info:
        do_import(tmp_path)
    assert "wandb login" in str(info.value) and "WANDB_API_KEY" in str(info.value)
    assert "super-secret-key-1234" not in str(info.value)


def test_unknown_project_or_no_access_exits_two(tmp_path, fake_wandb, capsys):
    code = cli.main(["import", "wandb", "lab/missing", "--dir", str(tmp_path / "s")])
    assert code == 2
    err = capsys.readouterr().err
    assert "cannot read W&B lab/missing" in err and "RuntimeError" in err and "Traceback" not in err

    fake_wandb.runs_by_path["lab/secret"] = PermissionError("403 forbidden")
    with pytest.raises(ImportSourceError, match="403"):
        do_import(tmp_path, "lab/secret")
    fake_wandb.runs_by_path["lab/secret/r1"] = ConnectionError("network is unreachable")
    with pytest.raises(ImportSourceError, match="unreachable"):
        do_import(tmp_path, "lab/secret/r1")


def test_empty_project_imports_nothing_and_warns(tmp_path, fake_wandb):
    fake_wandb.runs_by_path["lab/vision"] = []
    report, options = do_import(tmp_path)
    assert (report.imported, report.failed) == (0, 0)
    assert any("no runs found" in w for w in report.warnings)


def test_cli_end_to_end(tmp_path, fake_wandb, capsys):
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun("cli1")]
    store = tmp_path / "s"
    assert cli.main(["import", "wandb", "lab/vision", "--dir", str(store)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("imported 20240305T123015-") and "5 points" in out[0]
    assert out[-1] == "1 imported, 0 skipped, 0 failed"
    assert cli.main(["import", "wandb", "lab/vision", "--dir", str(store)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "0 imported, 1 skipped, 0 failed"


# ---------------------------------------------------------------------------
# status


@pytest.mark.parametrize(
    ("state", "status"),
    [("finished", "finished"), ("crashed", "crashed"), ("failed", "crashed"), ("killed", "crashed"),
     ("FAILED", "crashed")],
)
def test_state_maps_to_status(tmp_path, fake_wandb, state, status):
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun(state=state)]
    report, _ = do_import(tmp_path)
    assert RunReader(tmp_path / "store", report.rows[0].run_id).meta()["status"] == status


def test_running_runs_are_skipped_unless_included(tmp_path, fake_wandb):
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun("live", state="running"), FakeRun("done")]
    report, _ = do_import(tmp_path)
    assert (report.imported, report.skipped) == (1, 1)
    skipped = next(r for r in report.rows if r.status == "skipped")
    assert "still running" in skipped.reason and skipped.source_run_id == "live"

    included, _ = do_import(tmp_path, options=ImportOptions(include_running=True))
    assert included.imported == 1
    row = next(r for r in included.rows if r.status == "imported")
    meta = RunReader(tmp_path / "store", row.run_id).meta()
    assert meta["status"] == "finished" and meta["imported_from"]["source_status"] == "running"


def test_unknown_state_is_treated_as_running_with_a_warning(tmp_path, fake_wandb):
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun(state="teleporting")]
    report, _ = do_import(tmp_path)
    assert report.skipped == 1
    assert any("teleporting" in w for w in report.warnings)


def test_end_time_falls_back_to_runtime_then_heartbeat(tmp_path, fake_wandb):
    only_runtime = FakeRun("r1", summary={"_runtime": 60})
    only_heartbeat = FakeRun("r2", summary={})
    nothing = FakeRun("r3", summary={}, heartbeat_at=None, history=[])
    before = FakeRun("r4", summary={"_timestamp": CREATED_UNIX - 5}, heartbeat_at="2024-03-05T13:00:00Z")
    fake_wandb.runs_by_path["lab/vision"] = [only_runtime, only_heartbeat, nothing, before]
    report, _ = do_import(tmp_path)
    finished = {
        row.source_run_id: RunReader(tmp_path / "store", row.run_id).meta()["finished_at"] for row in report.rows
    }
    assert finished["r1"] == "2024-03-05T12:31:15.000000Z"
    assert finished["r2"] == "2024-03-05T12:40:15.000000Z"
    assert finished["r3"] == "2024-03-05T12:30:15.000000Z"  # the core falls back to the start
    assert finished["r4"] == "2024-03-05T13:00:00.000000Z"  # a timestamp before the start is ignored


def test_end_time_follows_summary_timestamp_over_runtime(tmp_path, fake_wandb):
    run = FakeRun("ts", summary={"_runtime": 60, "_timestamp": CREATED_UNIX + 300})
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path)
    assert RunReader(tmp_path / "store", report.rows[0].run_id).meta()["finished_at"] == "2024-03-05T12:35:15.000000Z"


def boom_property(name, error):
    def getter(self):
        raise error

    return property(getter, lambda self, value: None)


def test_a_run_whose_state_or_id_raises_is_one_failed_row_and_the_rest_import(tmp_path, fake_wandb):
    class BadState(FakeRun):
        state = boom_property("state", RuntimeError("state boom"))

    class BadId(FakeRun):
        id = boom_property("id", RuntimeError("id boom"))

    fake_wandb.runs_by_path["lab/vision"] = [BadState("s1"), BadId("i1"), FakeRun("fine")]
    report, _ = do_import(tmp_path)
    assert (report.imported, report.failed) == (1, 2)
    reasons = [r.reason for r in report.rows if r.status == "failed"]
    assert any("RuntimeError: state boom" in reason for reason in reasons)
    assert any("the W&B run has no id" in reason for reason in reasons)
    assert [r["name"] for r in list_runs(tmp_path / "store")] == ["name-fine"]


def deep(levels, leaf):
    value = leaf
    for _ in range(levels):
        value = {"k": value}
    return value


def test_deeply_nested_history_and_summary_values_do_not_recurse_forever(tmp_path, fake_wandb):
    history = [{"_step": 0, "_timestamp": CREATED_UNIX + 1, "loss": 1.0, "deep": deep(5000, 3.0)}]
    summary = {"acc": 0.5, "deep": deep(5000, 3.0)}
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun(history=history, summary=summary)]
    report, _ = do_import(tmp_path)
    assert (report.imported, report.failed) == (1, 0)
    reader = RunReader(tmp_path / "store", report.rows[0].run_id)
    assert list(reader.keys()) == ["loss"]  # past the depth cap the value is dropped, not followed
    assert reader.meta()["summary"] == {"acc": 0.5}


# ---------------------------------------------------------------------------
# points


def test_history_values_nested_special_and_unsupported(tmp_path, fake_wandb):
    nan = float("nan")
    history = [
        {"_step": 0, "_timestamp": CREATED_UNIX + 1, "a": 1, "n": {"x": 2.0, "y": {"z": 3}}, "s": "NaN",
         "inf": "Infinity", "text": "hello", "flag": True, "none": None, "hist": {"_type": "histogram", "values": [1]},
         "tbl": {"_type": "table-file", "path": "x.json"}, "lst": [1, 2], "_private": 5, 7: 1},
        {"_timestamp": CREATED_UNIX + 2, "a": 2},  # no _step: position
        {"_step": 5.0, "a": 3},  # no _timestamp: previous wall time
        {"_step": 6, "_timestamp": CREATED_UNIX + 3, "hist": {"_type": "histogram"}},  # no metrics: no point
        "garbage",
        {"_step": 9, "_timestamp": nan, "a": nan},
    ]
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun(history=history)]
    report, options = do_import(tmp_path)
    row = report.rows[0]
    assert row.status == "imported"
    reader = RunReader(tmp_path / "store", row.run_id)
    assert sorted(reader.keys()) == ["a", "inf", "n.x", "n.y.z", "s"]
    assert [p[0] for p in reader.series("a")["points"]] == [0, 1, 5, 9]
    assert reader.series("s")["nonfinite"] == 1
    assert reader.series("inf")["nonfinite"] == 1
    assert [list(p) for p in reader.time_index()][:3] == [[0, 1.0], [1, 2.0], [5, 2.0]]
    joined = " ".join(options.warnings)
    assert "histogram" in joined and "table-file" in joined and "list" in joined


def test_history_error_mid_stream_fails_only_that_run(tmp_path, fake_wandb):
    broken = FakeRun("broken", history=[{"_step": 0, "x": 1.0}, ConnectionError("lost connection")])
    fake_wandb.runs_by_path["lab/vision"] = [broken, FakeRun("fine")]
    report, _ = do_import(tmp_path)
    assert (report.imported, report.failed) == (1, 1)
    failed = next(r for r in report.rows if r.status == "failed")
    assert "lost connection" in failed.reason
    assert [r["name"] for r in list_runs(tmp_path / "store")] == ["name-fine"]


def test_bad_created_at_fails_that_run_only(tmp_path, fake_wandb):
    fake_wandb.runs_by_path["lab/vision"] = [
        FakeRun("nodate", created_at=None),
        FakeRun("baddate", created_at="not a date"),
        FakeRun("ok"),
    ]
    report, _ = do_import(tmp_path)
    assert (report.imported, report.failed) == (1, 2)
    assert all("created_at" in r.reason for r in report.rows if r.status == "failed")


def test_created_at_formats(tmp_path, fake_wandb):
    runs = [
        FakeRun("z", created_at="2024-03-05T12:30:15Z"),
        FakeRun("frac", created_at="2024-03-05T12:30:15.5"),
        FakeRun("off", created_at="2024-03-05T14:30:15+02:00"),
        FakeRun("dt", created_at=datetime(2024, 3, 5, 12, 30, 15)),
        FakeRun("num", created_at=CREATED_UNIX),
        FakeRun("nocolon", created_at="2024-03-05T07:30:15-0500"),
        FakeRun("nocolon_plus", created_at="2024-03-05T14:30:15.250+0200"),
    ]
    fake_wandb.runs_by_path["lab/vision"] = runs
    report, _ = do_import(tmp_path)
    assert report.imported == 7
    created = {r.source_run_id: RunReader(tmp_path / "store", r.run_id).meta()["created_at"] for r in report.rows}
    assert created["z"] == created["off"] == created["dt"] == created["num"] == "2024-03-05T12:30:15.000000Z"
    assert created["frac"] == "2024-03-05T12:30:15.500000Z"
    assert created["nocolon"] == "2024-03-05T12:30:15.000000Z"
    assert created["nocolon_plus"] == "2024-03-05T12:30:15.250000Z"


def test_hostile_run_attributes_do_not_break_the_import(tmp_path, fake_wandb):
    class Exploding:
        def __getitem__(self, key):
            raise RuntimeError("boom")

        def items(self):
            raise RuntimeError("boom")

    run = FakeRun("odd", config=Exploding(), summary=Exploding(), tags="not-a-list", notes=None, name=None,
                  state="finished")
    nameless = FakeRun("plain")
    nameless.name = 5
    fake_wandb.runs_by_path["lab/vision"] = [run, nameless]
    report, _ = do_import(tmp_path)
    assert report.imported == 2 and report.failed == 0
    meta = RunReader(tmp_path / "store", report.rows[0].run_id).meta()
    assert meta["name"] == "odd" and meta["config"] == {} and meta["summary"] == {}
    assert meta["tags"] == ["imported:wandb"]


def test_summary_is_numbers_and_text_only(tmp_path, fake_wandb):
    summary = {"acc": 0.9, "label": "best", "flag": True, "nested": {"a": 1, "t": {"_type": "table-file"}},
               "img": {"_type": "image-file", "path": "m.png"}, "_wandb": {"runtime": 3}, "list": [1]}
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun(summary=summary)]
    report, options = do_import(tmp_path)
    meta = RunReader(tmp_path / "store", report.rows[0].run_id).meta()
    assert meta["summary"] == {"acc": 0.9, "label": "best", "nested.a": 1}
    assert any("skipped 3 summary values" in w for w in report.warnings)


def test_summary_metrics_attribute_and_json_dict_are_read(tmp_path, fake_wandb):
    class HTTPSummary:
        _json_dict = {"best": 0.7}

    fake_wandb.runs_by_path["lab/vision"] = [FakeRun("s1", summary=HTTPSummary())]
    report, _ = do_import(tmp_path)
    assert RunReader(tmp_path / "store", report.rows[0].run_id).meta()["summary"] == {"best": 0.7}


# ---------------------------------------------------------------------------
# images


def image_history(*paths, key="sample"):
    return [
        {"_step": step, "_timestamp": CREATED_UNIX + step, "loss": float(step),
         key: {"_type": "image-file", "path": path, "caption": f"cap{step}", "format": "png"}}
        for step, path in enumerate(paths)
    ]


def test_images_are_downloaded_into_a_private_temp_dir_and_cleaned_up(tmp_path, fake_wandb):
    png = png_bytes()
    run = FakeRun(history=image_history("media/images/a_0.png", "media/images/a_1.png"))
    run.files = {"media/images/a_0.png": png, "media/images/a_1.png": png}
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path)
    row = report.rows[0]
    assert (row.points, row.media) == (2, 2)
    reader = RunReader(tmp_path / "store", row.run_id)
    rows = reader.media("sample")
    assert [r["step"] for r in rows] == [0, 1]
    assert rows[0]["caption"] == "cap0" and rows[0]["content_type"] == "image/png"
    roots = {Path(root) for _name, root in run.downloads}
    assert len(roots) == 1
    (root,) = roots
    assert root.name.startswith("agenticml-wandb-") and not root.exists()
    assert not str(root).startswith(str(tmp_path / "store"))
    assert run.scan_calls == 1  # media refs come from the same history pass


def test_dry_run_downloads_nothing_to_the_store_but_counts(tmp_path, fake_wandb):
    run = FakeRun(history=image_history("media/images/a.png"))
    run.files = {"media/images/a.png": png_bytes()}
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path, dry_run=True)
    assert (report.rows[0].points, report.rows[0].media) == (1, 1)
    assert not (tmp_path / "store" / "runs").exists() or not list((tmp_path / "store" / "runs").iterdir())


@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "/etc/passwd", "media/../../escape.png", "..", ".", "", "a\x00b.png", "C:\\x.png",
     "a\\..\\b.png", "media//x.png", "media/./x.png", "x/" * 600 + "y.png"],
)
def test_hostile_image_paths_are_never_downloaded(tmp_path, fake_wandb, hostile):
    run = FakeRun(history=image_history(hostile))
    run.files = {hostile: png_bytes()}
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, options = do_import(tmp_path)
    assert report.rows[0].status == "imported" and report.rows[0].media == 0
    assert run.downloads == []
    assert not (tmp_path / "escape.png").exists()
    assert any("1 images" in w for w in report.warnings)


def test_a_download_that_plants_a_symlink_is_not_followed(tmp_path, fake_wandb):
    secret = tmp_path / "secret.png"
    secret.write_bytes(png_bytes())

    def plant(target):
        target.symlink_to(secret)

    run = FakeRun(history=image_history("media/link.png"))
    run.files = {"media/link.png": plant}
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path)
    assert report.rows[0].media == 0 and secret.exists()


def only_media_dropped(report, run, expected_downloads):
    assert report.rows[0].status == "imported" and report.rows[0].media == 0
    assert len(run.downloads) == expected_downloads
    assert any("skipped" in w and "images" in w for w in report.warnings)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_at_the_download_target_is_skipped_without_hanging(tmp_path, fake_wandb):
    def plant(target):
        os.mkfifo(target)

    run = FakeRun(history=image_history("media/fifo.png"))
    run.files = {"media/fifo.png": plant}
    fake_wandb.runs_by_path["lab/vision"] = [run]

    def hung(*_args):
        raise AssertionError("opening the FIFO blocked")

    previous = signal.signal(signal.SIGALRM, hung)
    signal.alarm(10)
    try:
        report, _ = do_import(tmp_path)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    only_media_dropped(report, run, 1)


def test_a_directory_at_the_download_target_is_skipped(tmp_path, fake_wandb):
    def plant(target):
        target.mkdir()

    run = FakeRun(history=image_history("media/dir.png"))
    run.files = {"media/dir.png": plant}
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path)
    only_media_dropped(report, run, 1)


def test_a_symlink_to_a_file_inside_the_temp_dir_is_not_followed(tmp_path, fake_wandb):
    def plant(target):
        (target.parent / "real.png").write_bytes(png_bytes())
        target.symlink_to(target.parent / "real.png")

    run = FakeRun(history=image_history("media/inner.png"))
    run.files = {"media/inner.png": plant}
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path)
    only_media_dropped(report, run, 1)


def test_a_symlinked_parent_directory_is_not_followed_or_written_through(tmp_path, fake_wandb):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.png").write_bytes(png_bytes())

    def plant(target):
        # The download swaps its directory for a link to somewhere outside the temp dir.
        target.parent.rmdir()
        target.parent.symlink_to(outside, target_is_directory=True)
        assert (target.parent / "x.png").exists()

    run = FakeRun(history=image_history("media/x.png", "media/y.png"))
    run.files = {"media/x.png": plant, "media/y.png": png_bytes()}
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path)
    assert report.rows[0].status == "imported" and report.rows[0].media == 0
    # The second image resolves outside the temp dir, so it is never downloaded; nothing is written or deleted outside.
    assert [name for name, _root in run.downloads] == ["media/x.png"]
    assert (outside / "x.png").exists() and not (outside / "y.png").exists()


def test_non_images_missing_and_oversized_files_are_skipped(tmp_path, fake_wandb, monkeypatch):
    run = FakeRun(history=image_history("a.png", "b.png", "c.png", "d.png", "e.png"))
    run.files = {
        "a.png": b"<html>not an image</html>" * 10,
        "b.png": png_bytes(),
        "c.png": RuntimeError("404"),
        "d.png": png_bytes(),
        # e.png is not on the server at all
    }
    run.file_sizes["d.png"] = wandb_cloud.MAX_MEDIA_BYTES + 1  # declared too large: never downloaded
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path)
    row = report.rows[0]
    assert row.status == "imported" and row.media == 1
    assert [r["step"] for r in RunReader(tmp_path / "store", row.run_id).media("sample")] == [1]
    assert "d.png" not in [name for name, _root in run.downloads]
    assert any("skipped 4 images" in w for w in report.warnings)


def test_a_downloaded_file_over_the_cap_is_skipped(tmp_path, fake_wandb, monkeypatch):
    monkeypatch.setattr(wandb_cloud, "MAX_MEDIA_BYTES", 100)
    run = FakeRun(history=image_history("big.png", "small.png"))
    run.files = {"big.png": png_bytes() + b"\0" * 200, "small.png": png_bytes()[:60]}
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path)
    assert report.rows[0].media == 1


def test_image_references_are_thinned_like_the_writer(tmp_path, fake_wandb, monkeypatch):
    monkeypatch.setenv("AGENTICML_MEDIA_MAX_PER_KEY", "4")
    paths = [f"img/{i}.png" for i in range(40)]
    run = FakeRun(history=image_history(*paths))
    run.files = {p: png_bytes() for p in paths}
    fake_wandb.runs_by_path["lab/vision"] = [run]
    report, _ = do_import(tmp_path)
    assert report.rows[0].media <= 4
    assert len(run.downloads) <= 8  # far fewer than 40 downloads
    steps = [r["step"] for r in RunReader(tmp_path / "store", report.rows[0].run_id).media("sample")]
    assert steps[-1] == 39


def test_temp_dir_is_removed_when_the_import_stops_early(tmp_path, fake_wandb):
    run = FakeRun(history=image_history("a.png", "b.png"))
    run.files = {"a.png": png_bytes(), "b.png": png_bytes()}
    fake_wandb.runs_by_path["lab/vision"] = [run]
    (imported,) = list(wandb_cloud.discover("lab/vision", ImportOptions()))
    list(imported.points)
    media = iter(imported.media)
    next(media)
    (root,) = {Path(r) for _n, r in run.downloads}
    media.close()  # an abandoned run (writer failed) releases the temp dir
    assert not root.exists()


def test_other_typed_media_is_counted_not_imported(tmp_path, fake_wandb):
    history = [{"_step": 0, "loss": 1.0, "vid": {"_type": "video-file", "path": "v.mp4"},
                "imgs": {"_type": "images/separated", "filenames": ["a.png"]},
                "bad": {"_type": "image-file", "path": 5}}]
    fake_wandb.runs_by_path["lab/vision"] = [FakeRun(history=history)]
    report, options = do_import(tmp_path)
    assert report.rows[0].media == 0
    joined = " ".join(options.warnings)
    assert "video-file" in joined and "images/separated" in joined and "image-file" in joined


def test_every_run_has_its_own_temp_dir_and_no_leftovers(tmp_path, fake_wandb, monkeypatch):
    import tempfile

    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    runs = []
    for name in ("r1", "r2"):
        run = FakeRun(name, history=image_history("a.png"))
        run.files = {"a.png": png_bytes()}
        runs.append(run)
    fake_wandb.runs_by_path["lab/vision"] = runs
    report, _ = do_import(tmp_path)
    assert report.imported == 2 and [r.media for r in report.rows] == [1, 1]
    assert list(scratch.iterdir()) == []
    assert os.path.isdir(tmp_path / "store")
